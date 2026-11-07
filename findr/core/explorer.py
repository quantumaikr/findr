"""Virtual explorer (2026-11-06, prototype 4-1): AI-organized views over the index; real files are never moved or renamed.
Views: projects (folder anchors + loose files from Desktop/Downloads attached by meaning), version families
(name rule + content similarity, across folders), this week, cleanup suggestions (exact duplicates, throwaway names).
CPU only: uses the vectors already stored in the index.
    python explorer.py DB [--json OUT] [--eval MANIFEST_ROOT] [--now 2026-11-06]
"""
import argparse, collections, datetime as dt, hashlib, json, re, sqlite3
from pathlib import Path
import numpy as np

VMARK = re.compile(r"([._\- ]?(rev|v|ver)\.?\d+(\.\d+)?|[._\- ]?bak\w*|[._\- ]?(old|orig|backup|final|draft|최종|진짜최종|초안|수정본?|백업|복사본|사본|\d+차본?)|\s*\(\d+\)|[._\-]\d{4,8})(?=[._\- ]|$)", re.I)
GENERIC = re.compile(r"^(desktop|downloads?|documents?|바탕\s*화면|다운로드|문서|내\s*문서|home|users?|c|mnt|files|drive|tmp|temp|새\s*폴더)$", re.I)
THROWAWAY = re.compile(r"^(untitled|제목\s*없음|새\s*(텍스트\s*)?(문서|파일)|temp|tmp|scratch|test\d*|복사본|사본|다운로드\s*\(\d+\)|copy)|\(\d+\)\.|복사본", re.I)
VAGUE = re.compile(r"^(memo|메모|할일|todo|to-?do|note|notes|final|최종|진짜\s*최종|final_final|최종_진짜최종|new|새|test|tmp|temp|a+|b+|x+|asdf|qwer|1|123|file|문서|자료|정리|기타|misc|draft|초안)$", re.I)


def meaningless(name):
    """a name that says nothing about the content: 새 텍스트 문서 (3).txt, untitled.py, aaa.txt, memo.txt, final_final.txt"""
    if THROWAWAY.search(name):
        return True
    c = core(name).replace("_", " ").strip()
    return len(c) <= 3 or bool(VAGUE.match(c)) or bool(VAGUE.match(c.replace(" ", "_")))


TOK = re.compile(r"[A-Za-z]{3,}|[가-힣]{2,}|\d{3,}")
# set on the ml and dev fictional PCs only (2026-11-06); office and lab are the check
SIM_FAMILY = 0.90      # a throwaway-named copy (복사본, 최종_진짜최종) joins a document at this first-text similarity
SIM_DIR = 0.97         # shared name word or same folder: near-identical text only (set on ml+dev; office .91/.83, lab .88/.44 check)
SIM_PROJECT = 0.80     # nearest single file; measured vs teacher gold 2026-11-06: centroid placement was mostly wrong (ml 15/16 wrong at .65),
#                        nearest-file at .80 places only near-duplicates (0 wrong on all four PCs). Real placement needs a model that reads.
_OLD_SIM_PROJECT = 0.65     # a loose file joins the nearest project only this close; below, it stays unsorted (conservative; placement accuracy still to be measured)


# EmbeddingGemma 2 similarities run on a different scale: chosen on the synthetic PCs' family gold, keeping precision
# at the bge-m3 level (0.87 vs 0.89) with higher recall (0.63 vs 0.55)
ENC_SIM = {"embeddinggemma-2": (0.92, 0.95)}


def load(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    files = {fid: dict(path=p, name=n, ext=(e or "").lower(), size=s, mtime=m) for fid, p, n, e, s, m in
             con.execute("SELECT id, path, name, ext, size, mtime FROM files WHERE tier IN (1,3)")}
    vec, first = collections.defaultdict(list), {}
    for fid, n, emb, text in con.execute("SELECT file, n, emb, text FROM chunks WHERE emb IS NOT NULL AND n < 8"):
        if fid in files:
            v = np.frombuffer(emb, np.float16).astype(np.float32); vec[fid].append(v)
            if n == 0:
                first[fid] = (v, hashlib.sha1((text or "").strip().encode()).hexdigest() if text and len(text.strip()) > 40 else None)
    mean = {}
    for fid, vs in vec.items():
        m = np.mean(vs, 0); mean[fid] = m / (np.linalg.norm(m) + 1e-9)
    return files, first, mean


def core(name):
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return VMARK.sub("", stem).strip(" ._-").lower()


def families(files, first, sim=SIM_FAMILY, dir_sim=None):
    """union of (a) same folder + same name core + same ext and (b) near-identical opening text + same ext + a shared name word
    or the same folder (a renamed copy on the desktop is still the document)"""
    par = {f: f for f in files}
    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]; x = par[x]
        return x
    def union(a, b):
        par[find(a)] = find(b)
    key = collections.defaultdict(list)
    for f, d in files.items():
        c = core(d["name"])
        if len(c) >= 3 and not THROWAWAY.match(c) and c not in ("다운로드", "download", "document", "문서", "memo", "메모", "note", "notes"):   # 다운로드.txt / 다운로드 (2).txt are unrelated
            key[(d["path"].rsplit("/", 1)[0], c, d["ext"])].append(f)
    for fs in key.values():
        if 2 <= len(fs) <= 20:
            for x in fs[1:]:
                union(fs[0], x)
    ids = [f for f in files if f in first]
    if ids:
        M = np.stack([first[f][0] / (np.linalg.norm(first[f][0]) + 1e-9) for f in ids]); S = M @ M.T
        toks = {f: set(TOK.findall(core(files[f]["name"]))) for f in ids}
        for i in range(len(ids)):
            for j in np.nonzero(S[i, i + 1:] >= sim)[0] + i + 1:
                a, b = ids[i], ids[j]
                if files[a]["ext"] != files[b]["ext"]:
                    continue
                same_dir = files[a]["path"].rsplit("/", 1)[0] == files[b]["path"].rsplit("/", 1)[0]
                # a throwaway-named copy (복사본, 최종_진짜최종, untitled) is linked by content alone; otherwise a shared name word
                # or the same folder is weak evidence (추석 예산 vs 설 예산, monthly reports) and needs near-identical text
                if THROWAWAY.search(files[a]["name"]) or THROWAWAY.search(files[b]["name"]):
                    union(a, b)
                elif (same_dir or toks[a] & toks[b]) and S[i, j] >= (dir_sim or SIM_DIR):
                    union(a, b)
    g = collections.defaultdict(list)
    for f in files:
        g[find(f)].append(f)
    return [sorted(v, key=lambda f: files[f]["mtime"]) for v in g.values() if 2 <= len(v) <= 20]


def projects(files, mean, sim=SIM_PROJECT):
    """anchor = a non-generic folder holding >= 4 files (its own name is the project name); a file in a generic place
    (Desktop, Downloads, home) or a tiny folder joins the closest anchor by meaning, else stays unsorted"""
    def anchor_dir(p):
        parts = p.split("/")[:-1]
        while parts and GENERIC.match(parts[-1]):
            parts = parts[:-1]
        return "/".join(parts)
    by = collections.defaultdict(list); drop = []
    for f, d in files.items():
        parent = d["path"].rsplit("/", 2)[-2] if d["path"].count("/") >= 2 else ""
        if GENERIC.match(parent):   # Desktop, Downloads, Documents: never a project by itself, placed by meaning below
            drop.append(f)
        else:
            by[anchor_dir(d["path"])].append(f)
    # a project is the deepest folder with enough files; tiny subfolders roll up into their parent project
    anchors = {d: fs for d, fs in by.items() if len(fs) >= 3 and d and not all(GENERIC.match(x) for x in d.split("/") if x)}
    for d, fs in list(by.items()):
        if d in anchors:
            continue
        up = d
        while up and up not in anchors:
            up = up.rsplit("/", 1)[0] if "/" in up else ""
        if up:
            anchors[up] = anchors[up] + fs; by[d] = []
    # an anchor nested in another anchor stays its own project (deepest wins)
    proj = {d: list(fs) for d, fs in anchors.items()}
    cent = {d: (lambda m: m / (np.linalg.norm(m) + 1e-9))(np.mean([mean[f] for f in fs if f in mean], 0)) for d, fs in proj.items() if any(f in mean for f in fs)}
    placed = {f for fs in proj.values() for f in fs}
    loose, unsorted, moved = [f for f in files if f not in placed], [], {}   # includes the Desktop/Downloads files
    members = [(d, x) for d, fs in proj.items() for x in fs if x in mean]
    MM = np.stack([mean[x] for _, x in members]) if members else None
    for f in loose:
        if f in mean and MM is not None:
            s = MM @ mean[f]; i = int(np.argmax(s))
            if s[i] >= sim:
                proj[members[i][0]].append(f); moved[f] = round(float(s[i]), 2); continue
        unsorted.append(f)
    return proj, unsorted, moved


def build(db, now=None, corr=None):
    """corr: the user's corrections {"move": {fid: project dir}, "rename": {project dir: name}}; they always win over the AI"""
    files, first, mean = load(db)
    now = (dt.datetime.fromisoformat(now) if now else dt.datetime.now()).timestamp()
    tp = Path(db).with_name("titles.json")   # AI titles made in advance by gen_titles.py (local model); only shown, never applied
    titles = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {}
    from enc_eg2 import encoder_of
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c_:
        enc_ = encoder_of(c_)
    sim_, dsim_ = ENC_SIM.get(enc_, (SIM_FAMILY, SIM_DIR))   # thresholds follow the index's meaning encoder
    fam = families(files, first, sim_, dsim_); proj, unsorted, moved = projects(files, mean)
    corr = corr or {}; user = set()
    for fid, to in (corr.get("move") or {}).items():
        fid = int(fid)
        if fid not in files:
            continue
        for fs in proj.values():
            if fid in fs:
                fs.remove(fid)
        if fid in unsorted:
            unsorted.remove(fid)
        proj.setdefault(to, []).append(fid); moved.pop(fid, None); user.add(fid)
    proj = {d: fs for d, fs in proj.items() if fs}
    # suggestions only: for a file not in any project, the three projects whose files read most alike (the user decides;
    # automatic placement lost to 'place nothing' against the teacher's gold, 2026-11-06)
    near = {}
    members = [(d, x) for d, fs in proj.items() for x in fs if x in mean]
    if members:
        MM = np.stack([mean[x] for _, x in members])
        for f in unsorted:
            if f in mean:
                order = []
                for i in np.argsort(-(MM @ mean[f])):
                    if members[i][0] not in order:
                        order.append(members[i][0])
                    if len(order) == 3:
                        break
                near[f] = order
    infam = {f: i for i, fs in enumerate(fam) for f in fs}
    def fi(f):
        d = files[f]; g = fam[infam[f]] if f in infam else None
        newer = files[g[-1]] if g and g[-1] != f else None
        return {"fid": f, "path": d["path"], "name": d["name"], "size": d["size"], "mtime": d["mtime"], "family": infam.get(f),
                "ai": moved.get(f), "user": f in user, "ext": d["ext"], "near": near.get(f),
                "title": (titles.get(d["path"]) or {}).get("title") if (titles.get(d["path"]) or {}).get("mtime") == d["mtime"] else None,
                "newer": {"name": newer["name"], "mtime": newer["mtime"]} if newer else None}
    import os
    root = os.path.commonpath([d["path"] for d in files.values()]) if files else ""
    def pname(d):
        if d in (corr.get("rename") or {}):
            return corr["rename"][d]
        rel = d[len(root):] if d.startswith(root) and d != root else d.rsplit("/", 1)[-1]
        segs = [s for s in rel.split("/") if s and not GENERIC.match(s)]
        return " / ".join(segs[-2:]) if segs else d
    P = []
    for d, fs in proj.items():
        ms = [files[f]["mtime"] for f in fs]
        spark = [sum(1 for m in ms if now - (w + 1) * 7 * 86400 < m <= now - w * 7 * 86400) for w in range(11, -1, -1)]   # 12 weeks
        P.append({"name": pname(d), "dir": d, "n": len(fs), "first": min(ms), "last": max(ms), "spark": spark,
                  "files": [fi(f) for f in sorted(fs, key=lambda f: -files[f]["mtime"])]})
    P.sort(key=lambda p: -p["last"])
    # resume: projects touched in the last 48 hours (else the two most recent), each with its latest files (latest versions only)
    recent = [p for p in P if now - p["last"] <= 2 * 86400] or P[:2]
    resume = []
    for p in recent[:3]:
        latest = [f for f in p["files"] if not f["newer"]][:3]
        n48 = sum(1 for f in p["files"] if now - f["mtime"] <= 2 * 86400)
        resume.append({"name": p["name"], "dir": p["dir"], "last": p["last"], "changed": n48, "files": latest})
    dup = collections.defaultdict(list)
    for f, (v, h) in first.items():
        if h:
            dup[h].append(f)
    dups = [[fi(f) for f in sorted(fs, key=lambda f: files[f]["mtime"])] for fs in dup.values() if len(fs) > 1]
    throw = [fi(f) for f in files if meaningless(files[f]["name"])]
    week = [fi(f) for f in sorted(files, key=lambda f: -files[f]["mtime"]) if now - files[f]["mtime"] <= 7 * 86400 and files[f]["mtime"] <= now + 3600]
    return {"resume": resume, "now": now, "projects": P, "unsorted": [fi(f) for f in unsorted], "families": [[fi(f) for f in fs] for fs in fam],
            "week": week, "cleanup": {"duplicates": dups, "throwaway": throw}, "n_files": len(files)}


def evaluate(db, root):
    """pairwise precision/recall of version families against the teacher's manifest (numbers only)"""
    files, first, mean = load(db)
    man = json.load(open(Path(root) / "manifest.json", encoding="utf-8"))
    by_rel = {d["path"].split("/drive/", 1)[-1]: f for f, d in files.items()}
    gold = collections.defaultdict(set)
    for m in man:
        f = by_rel.get(m["path"].split("/", 1)[1])
        if f and m.get("family"):
            gold[m["family"]].add(f)
    gp = {frozenset((a, b)) for g in gold.values() for a in g for b in g if a < b}
    out = {}
    for sim, dsim in ((0.85, 0.93), (0.85, 0.95), (0.85, 0.97), (0.90, 0.93), (0.90, 0.95), (0.90, 0.97), (1.01, 1.01)):
        fam = families(files, first, sim, dsim)
        pp = {frozenset((a, b)) for g in fam for a in g for b in g if a < b}
        tp = len(gp & pp)
        out[(sim, dsim)] = (round(tp / max(1, len(pp)), 2), round(tp / max(1, len(gp)), 2), len(pp), len(gp))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("db"); ap.add_argument("--json"); ap.add_argument("--eval"); ap.add_argument("--now")
    a = ap.parse_args()
    if a.eval:
        for sim, (p, r, npred, ngold) in evaluate(a.db, a.eval).items():
            print(f"  sim {sim[0]:.2f}/dir {sim[1]:.2f}: family pairs precision {p} recall {r} (pred {npred}, gold {ngold})" + ("  [name rule only]" if sim[0] > 1 else ""))
    else:
        v = build(a.db, a.now)
        print("EXPLORER", {"files": v["n_files"], "projects": len(v["projects"]), "unsorted": len(v["unsorted"]), "families": len(v["families"]),
                           "week": len(v["week"]), "dups": len(v["cleanup"]["duplicates"]), "throwaway": len(v["cleanup"]["throwaway"])})
        if a.json:
            json.dump(v, open(a.json, "w", encoding="utf-8"), ensure_ascii=False)
