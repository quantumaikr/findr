"""where the first stage loses answers: the answer's rank in the engine's own fused candidates (k=100), and in the
meaning lane and the word lane alone. Numbers only.
    FINDR_PROFILE=pc python tools/lane_recall.py INDEX.db QUESTIONS.jsonl   (with findr's own Python; a copy of the index is safest)
questions: {q, answers: [paths]} or {q, answer} (rows without answers skipped)"""
import collections
import json
import re
import sys
import unicodedata

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "findr" / "core"))
import search as S  # noqa: E402

f = S.Finder(sys.argv[1])
qs = [json.loads(l) for l in open(sys.argv[2], encoding="utf-8") if l.strip()]
for x in qs:
    if x.get("answer") and not x.get("answers"):
        x["answers"] = [x["answer"]]
qs = [x for x in qs if x.get("answers")]
nfc = lambda p: unicodedata.normalize("NFC", p or "")
path_fid = {nfc(p): i for i, p in f.con.execute("SELECT id, path FROM files")}
chunk_file = dict(f.con.execute("SELECT id, file FROM chunks"))
nchunks = collections.Counter(chunk_file.values())
B = (1, 5, 10, 15, 30, 50, 100)
tab = collections.defaultdict(collections.Counter)
depth = collections.Counter()   # for answers the meaning lane misses in its first 100 files: which piece of the answer would match best


def bucket(r):
    return next((f"<={b}" for b in B if r is not None and r <= b), "out")


for x in qs:
    q = nfc(x["q"])
    want = {path_fid.get(nfc(a)) for a in x["answers"]} - {None}
    if not want:
        tab["(answer not in the index)"]["n"] += 1
        continue
    r = f.search(q, k=100, mode="base")
    fused = [y["fid"] for y in r.get("results") or []]
    p = S.parse(q)
    s = f.dense_all(f.qvec(p["text"]))
    seen, dense = set(), []
    order = np.argsort(-s)
    for i in order[:3000]:
        fl = chunk_file.get(int(f.ids[i]))
        if fl not in seen:
            seen.add(fl); dense.append(fl)
        if len(dense) >= 100:
            break
    words = [w for w in re.split(r"[\s/\\,.()\[\]\"']+", p["text"]) if len(w) >= 3]
    lex = []
    if words:
        try:
            cids = [c[0] for c in f.con.execute("SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT 300",
                                                (" OR ".join('"' + w.replace('"', "") + '"' for w in words),))]
            lex = list(dict.fromkeys(chunk_file.get(c) for c in cids))
        except Exception:
            pass
    rk = lambda lst: next((i + 1 for i, d in enumerate(lst) if d in want), None)
    for nm, lst in (("fused (engine)", fused), ("meaning lane", dense), ("word lane", lex)):
        tab[nm][bucket(rk(lst))] += 1
    best = min([r_ for r_ in (rk(dense), rk(lex)) if r_] or [None], default=None) if (rk(dense) or rk(lex)) else None
    tab["best of the two lanes"][bucket(best)] += 1
    a = next(iter(want))
    tab["answer pieces"]["1" if nchunks[a] <= 1 else "2-8" if nchunks[a] <= 8 else "9+"] += 1
print(f"questions {len(qs)}, files {len(path_fid)}, pieces {len(chunk_file)}, encoder {getattr(f, 'encoder', '?')}")
cols = [f"<={b}" for b in B] + ["out"]
print(f"{'':26s}" + "".join(f"{c:>7s}" for c in cols) + "   (cumulative)")
for nm in ("fused (engine)", "meaning lane", "word lane", "best of the two lanes"):
    c = tab[nm]; run = 0; out = []
    for col in cols[:-1]:
        run += c[col]; out.append(run)
    print(f"{nm:26s}" + "".join(f"{v:7d}" for v in out) + f"{c['out']:7d}")
print("answer pieces", dict(tab["answer pieces"]), "| not in the index:", tab["(answer not in the index)"]["n"])
