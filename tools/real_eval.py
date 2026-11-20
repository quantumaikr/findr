"""Real-file evaluation on one PC (11/9 pilots, as a tool): a person's or an assistant's saved questions about files that
exist on this PC, run against the running engine, scored with numbers only. File names, paths and question texts stay in
the local record (--out); what is printed carries counts, ranks and times, so it can be shared.

Questions (JSONL, written before any search and not changed after seeing results):
  {"id": "A1", "kind": "known", "q": "...", "answer": "/path/of/the/file"}   a file the person means (or "answers": [...])
  {"id": "B1", "kind": "task",  "q": "..."}                                   a common task: judged by hand afterwards
  {"id": "N1", "kind": "none",  "q": "..."}                                   nothing on this PC: says so?
    python tools/real_eval.py questions.jsonl --out run_a.json [--port 8096] [--home ~/.findr] [--compare run_b.json]
A copy folded under an answer (DUP_COLLAPSE, 'copies') counts as the answer. Another version folded under a line
(VERSION_GROUP, 'versions') counts at that line, with one more file to open. --base also asks the first stage (k=50).
opens: files a person opens until the answer (11 when it is not in the first 10).
miss: when the first line is not the answer, what that first file is to the answer -- 'copy' (the same bytes), 'version'
(the same document in the same folder, as the engine groups versions), 'folder' (another file of the same folder) or 'other'
-- so a run says whether misses are wrong versions or wrong documents. Counted in the summary; the kinds hold no names.
"""
import argparse
import base64
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def norm(p):
    """one spelling for a path: Windows drive paths as WSL sees them, forward slashes, composed Korean"""
    import unicodedata
    p = unicodedata.normalize("NFC", str(p or ""))
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:
        p = f"/mnt/{m.group(1).lower()}/" + m.group(2)
    return p.replace("\\", "/")


def rank_of(results, answers, versions=False):
    """1-based rank of the first result that is an answer or holds one among its folded copies, or None. versions=True:
    (rank, True) when the answer is another version folded under that result, else (rank, False)"""
    want = {norm(a) for a in answers}
    for i, x in enumerate(results or [], 1):
        if norm(x.get("path")) in want or want & {norm(c) for c in x.get("copies") or []}:
            return (i, False) if versions else i
        if versions and want & {norm(v.get("path") if isinstance(v, dict) else v) for v in x.get("versions") or []}:
            return i, True
    return (None, False) if versions else None


MISS_KINDS = ("copy", "version", "folder", "other", "empty")
_FAMILY = []   # [search.family] once loaded, [None] when the engine's module cannot be loaded with this Python


def _family(path):
    """(folder, name without version marks) as the engine groups versions (search.family), or None"""
    if not _FAMILY:
        try:
            core = str(Path(__file__).resolve().parents[1] / "findr" / "core")
            if core not in sys.path:
                sys.path.insert(0, core)
            import search
            _FAMILY.append(search.family)
        except Exception:   # noqa: BLE001  (a Python without the engine's packages: versions are then told by folder only)
            _FAMILY.append(None)
    f = _FAMILY[0]
    return f({"path": path}) if f else None


def _same_bytes(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        import hashlib
        h = lambda p: hashlib.sha1(Path(p).read_bytes()).hexdigest()
        return h(a) == h(b)
    except OSError:   # not on this PC (a run read elsewhere): not counted as a copy
        return False


def miss_kind(top, answers):
    """what the first result is to the answer when it is not the answer: copy / version / folder / other / empty"""
    if not top:
        return "empty"
    t, A = norm(top), [norm(a) for a in answers]
    if any(_same_bytes(t, a) for a in A):
        return "copy"
    ft = _family(t)
    if ft and ft[1] and any(_family(a) == ft for a in A):
        return "version"
    if any(t.rsplit("/", 1)[0] == a.rsplit("/", 1)[0] for a in A):
        return "folder"
    return "other"


def opens_of(r):
    rk = r.get("rank")
    return min(11, rk + (1 if r.get("in_versions") else 0)) if rk and rk <= 10 else 11


def summarize(rows):
    known = [r for r in rows if r["kind"] == "known"]
    none = [r for r in rows if r["kind"] == "none"]
    ms = sorted(r["ms"] for r in rows if r.get("ms") is not None)
    s = {"questions": len(rows), "known": len(known),
         "hit1": sum(1 for r in known if r.get("rank") == 1),
         "top3": sum(1 for r in known if r.get("rank") and r["rank"] <= 3),
         "top10": sum(1 for r in known if r.get("rank") and r["rank"] <= 10),
         "in_versions": sum(1 for r in known if r.get("in_versions")),
         "opens": round(sum(opens_of(r) for r in known) / max(len(known), 1), 2),
         "none": len(none), "none_said": sum(1 for r in none if r.get("note")),
         "p50_ms": ms[len(ms) // 2] if ms else None, "max_ms": ms[-1] if ms else None,
         "miss": {k: sum(1 for r in known if r.get("miss") == k) for k in MISS_KINDS}}
    if any("base_rank" in r for r in known):
        s["base_hit1"] = sum(1 for r in known if r.get("base_rank") == 1)
        s["base_in50"] = sum(1 for r in known if r.get("base_rank"))
    return s


def compare(rows, prev):
    """per known question: better / worse / same rank against an earlier run of the same questions"""
    old = {r["id"]: r for r in prev}; out = {"better": [], "worse": [], "same": 0}
    big = 10 ** 6
    for r in rows:
        if r["kind"] != "known" or r["id"] not in old:
            continue
        a, b = old[r["id"]].get("rank") or big, r.get("rank") or big
        if b < a:
            out["better"].append(r["id"])
        elif b > a:
            out["worse"].append(r["id"])
        else:
            out["same"] += 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("questions"); ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=int(os.environ.get("FINDR_APP_PORT") or 8096))
    ap.add_argument("--home", default=os.environ.get("FINDR_HOME") or os.path.expanduser("~/.findr"))
    ap.add_argument("--base", action="store_true", help="also ask the first stage (k=50) for the answer's rank there")
    ap.add_argument("--compare", help="an earlier --out of the same questions")
    a = ap.parse_args()
    pw = (Path(a.home) / ".password").read_text(encoding="utf-8").strip() if (Path(a.home) / ".password").exists() else ""
    hdr = {"Authorization": "Basic " + base64.b64encode(f"real_eval:{pw}".encode()).decode(), "X-Findr": "1"}

    def ask(q, mode, k):
        u = f"http://127.0.0.1:{a.port}/api?" + urllib.parse.urlencode({"q": q, "mode": mode, "k": k})
        t = time.time()
        r = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=180))
        return r, int((time.time() - t) * 1000)

    rows = []
    for l in open(a.questions, encoding="utf-8"):
        if not l.strip():
            continue
        x = json.loads(l); r, ms = ask(x["q"], "auto", 10)
        row = {"id": x["id"], "kind": x.get("kind", "known"), "ms": ms, "note": r.get("note", ""), "route": r.get("route"),
               "top": [y.get("path") for y in (r.get("results") or [])[:5]]}
        answers = x.get("answers") or ([x["answer"]] if x.get("answer") else [])
        if row["kind"] == "known" and answers:
            row["rank"], row["in_versions"] = rank_of(r.get("results"), answers, versions=True)
            row["folded_versions"] = sum(len(y.get("versions") or []) for y in (r.get("results") or [])[:5])
            if row["rank"] != 1:
                row["miss"] = miss_kind(row["top"][0] if row["top"] else None, answers)
            if a.base:
                b, _ = ask(x["q"], "base", 50); row["base_rank"] = rank_of(b.get("results"), answers)
        rows.append(row)
    Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")   # names stay here, on this PC
    rep = {"summary": summarize(rows)}
    if a.compare:
        rep["compare"] = compare(rows, json.loads(Path(a.compare).read_text(encoding="utf-8")))
    print(json.dumps(rep, ensure_ascii=False))   # numbers and question ids only: safe to share


if __name__ == "__main__":
    sys.exit(main())
