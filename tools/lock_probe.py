"""when the first stage is already sure: how often its #1 is the top of the meaning lane and high in the word lane, and
how often that #1 is the answer then. Also how often the same holds for questions with no answer on the PC (a lock
there would show a wrong file as sure). Numbers only.
    FINDR_PROFILE=pc python tools/lock_probe.py INDEX.db QUESTIONS.jsonl"""
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
    if x.get("pos") and not x.get("answers"):
        x["answers"] = x["pos"]
nfc = lambda p: unicodedata.normalize("NFC", p or "")
path_fid = {nfc(p): i for i, p in f.con.execute("SELECT id, path FROM files")}
chunk_file = dict(f.con.execute("SELECT id, file FROM chunks"))
CONDS = collections.OrderedDict([
    ("meaning #1", lambda d, l, m: d == 1),
    ("meaning #1, words <=10", lambda d, l, m: d == 1 and l <= 10),
    ("meaning #1, words <=3", lambda d, l, m: d == 1 and l <= 3),
    ("meaning #1, words #1", lambda d, l, m: d == 1 and l == 1),
    ("meaning #1, margin >=.03", lambda d, l, m: d == 1 and m >= 0.03),
    ("meaning #1, words <=3, margin >=.02", lambda d, l, m: d == 1 and l <= 3 and m >= 0.02),
])
T = {k: collections.Counter() for k in CONDS}
n_ans = n_none = base_right = 0
for x in qs:
    q = nfc(x["q"])
    want = {path_fid.get(nfc(a)) for a in x.get("answers") or []} - {None}
    r = f.search(q, k=30, mode="base")
    fused = [y["fid"] for y in r.get("results") or []]
    if not fused:
        continue
    top = fused[0]
    p = S.parse(q)
    s = f.dense_all(f.qvec(p["text"]))
    seen, dense, dsim = set(), [], []
    for i in np.argsort(-s)[:2000]:
        fl = chunk_file.get(int(f.ids[i]))
        if fl not in seen:
            seen.add(fl); dense.append(fl); dsim.append(float(s[i]))
        if len(dense) >= 30:
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
    d = dense.index(top) + 1 if top in dense else 99
    l = lex.index(top) + 1 if top in lex else 99
    m = (dsim[0] - dsim[1]) if len(dsim) > 1 else 0.0
    if want:
        n_ans += 1; base_right += top in want
    elif not x.get("answers"):
        n_none += 1
    for k, c in CONDS.items():
        if c(d, l, m):
            if want:
                T[k]["on"] += 1; T[k]["right"] += top in want
            elif not x.get("answers"):
                T[k]["none_on"] += 1
print(f"with an answer {n_ans} (first stage #1 right: {base_right}), without {n_none}, encoder {getattr(f, 'encoder', '?')}")
print(f"{'condition':38s} {'holds':>6s} {'#1 right':>9s} {'precision':>9s} {'holds on no-answer':>19s}")
for k, c in T.items():
    print(f"{k:38s} {c['on']:6d} {c['right']:9d} {(c['right'] / c['on'] if c['on'] else 0):9.2f} {c['none_on']:11d}/{n_none}")
