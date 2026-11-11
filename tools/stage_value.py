"""what each stage of a search changed, whether it helped, and what it cost: for every question with a known answer,
the answer's rank before and after each stage and the time the stage took (from the 'trace' of STAGE_TRACE=1).
Numbers only.   python3 stage_value.py QUESTIONS.jsonl PORT
questions: {q, answers: [paths]} or real_eval's {q, answer / answers, kind}; rows without answers are skipped"""
import base64
import collections
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request

qs = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
port = sys.argv[2]
home = os.environ.get("FINDR_HOME") or (os.path.join(os.environ["LOCALAPPDATA"], "findr") if os.name == "nt" and os.environ.get("LOCALAPPDATA")
                                       else os.path.expanduser("~/.findr"))
pw = open(os.path.join(home, ".password"), encoding="utf-8").read().strip() if os.path.exists(os.path.join(home, ".password")) else ""


def norm(p):
    p = unicodedata.normalize("NFC", str(p or ""))
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:
        p = f"/mnt/{m.group(1).lower()}/" + m.group(2)
    return p.replace("\\", "/")


def ask(q):
    u = f"http://127.0.0.1:{port}/api?" + urllib.parse.urlencode({"q": q, "mode": "auto", "k": 10})
    hdr = {"Authorization": "Basic " + base64.b64encode(f"x:{pw}".encode()).decode(), "X-Findr": "1"}
    if os.environ.get("FINDR_EVAL_TODAY"):
        hdr["X-Findr-Today"] = os.environ["FINDR_EVAL_TODAY"]
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=180))


S = collections.OrderedDict()
ms = collections.defaultdict(list)
n = 0
for x in qs:
    answers = x.get("answers") or ([x["answer"]] if x.get("answer") else [])
    if not answers:
        continue
    tr = ask(x["q"]).get("trace") or []
    if not tr:
        continue
    n += 1
    want = {norm(a) for a in answers}
    prev, t_prev = None, 0
    for stage, paths, *tm in tr:
        cur = next((i + 1 for i, p in enumerate(paths) if norm(p) in want), 11)
        if tm:
            ms[stage].append(tm[0] - t_prev); t_prev = tm[0]
        s = S.setdefault(stage, collections.Counter())
        s["ran"] += 1; s["hit1"] += cur == 1; s["in3"] += cur <= 3; s["in10"] += cur <= 10
        if prev is not None:
            s["better"] += cur < prev; s["worse"] += cur > prev
            s["to1"] += cur == 1 and prev != 1; s["from1"] += prev == 1 and cur != 1
        prev = cur
print(f"questions with an answer and a trace: {n}")
print(f"{'stage':11s} {'ran':>4s} {'#1':>4s} {'in3':>4s} {'in10':>4s} {'better':>6s} {'worse':>5s} {'to#1':>4s} {'from#1':>6s} {'ms p50':>6s} {'ms p90':>6s}")
for k, s in S.items():
    m = sorted(ms.get(k) or [0])
    print(f"{k:11s} {s['ran']:4d} {s['hit1']:4d} {s['in3']:4d} {s['in10']:4d} {s['better']:6d} {s['worse']:5d} {s['to1']:4d} {s['from1']:6d} "
          f"{m[len(m) // 2]:6d} {m[int(len(m) * .9)]:6d}")
