"""the same answers before and after a change: record what a running engine answers to a fixed list of questions, or
compare with a record. Kept on this PC (paths inside); prints counts only.
    python tools/same_answers.py record QUESTIONS.jsonl PORT OUT.json
    python tools/same_answers.py compare QUESTIONS.jsonl PORT OUT.json"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

mode, qf, port, out = sys.argv[1:5]
home = os.environ.get("FINDR_HOME") or (os.path.join(os.environ["LOCALAPPDATA"], "findr") if os.name == "nt" and os.environ.get("LOCALAPPDATA") else os.path.expanduser("~/.findr"))
pw = open(os.path.join(home, ".password"), encoding="utf-8").read().strip()


def ask(q):
    u = f"http://127.0.0.1:{port}/api?" + urllib.parse.urlencode({"q": q, "mode": "auto", "k": 10})
    hdr = {"Authorization": "Basic " + base64.b64encode(f"x:{pw}".encode()).decode(), "X-Findr": "1",
           "X-Findr-Today": os.environ.get("FINDR_EVAL_TODAY", "2026-11-06T12:00")}
    r = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=180))
    return {"paths": [y.get("path") for y in r.get("results") or []], "note": r.get("note", ""), "info": r.get("info", ""),
            "folded": [[len(y.get("copies") or []), len(y.get("versions") or [])] for y in r.get("results") or []],
            "prob": [y.get("prob") for y in r.get("results") or []]}


qs = [json.loads(l)["q"] for l in open(qf, encoding="utf-8") if l.strip()]
got = {q: ask(q) for q in dict.fromkeys(qs)}
if mode == "record":
    json.dump(got, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"recorded {len(got)} questions")
else:
    old = json.load(open(out, encoding="utf-8"))
    d = {k: sum(1 for q in got if got[q][k] != old.get(q, {}).get(k)) for k in ("paths", "note", "info", "folded", "prob")}
    top1 = sum(1 for q in got if got[q]["paths"][:1] != old.get(q, {}).get("paths", [])[:1])
    print(f"compared {len(got)} questions: different order {d['paths']} (different #1: {top1}), note {d['note']}, info {d['info']}, "
          f"folds {d['folded']}, odds {d['prob']}")
