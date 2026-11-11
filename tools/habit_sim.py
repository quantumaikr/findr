"""does opening files teach the engine anything? A simulated person on a running engine (HABITS=1, PERSONAL=1).
The questions keep their order: the first half is what the person 'lived' (asked, then opened the right file), the second
half is the test. (a) ask the test half (b) ask the first half one by one and report the answer as opened (/fb verdict=open)
(c) ask the test half again (d) before and after: #1, in the first 3, files opened until the answer, questions better / worse.
Numbers only.   python3 habit_sim.py QUESTIONS.jsonl PORT [alt]
questions: {q, answers: [paths], type}; rows without answers are skipped. With HABITS=0 (c) must equal (a): the control"""
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
qs = [x for x in qs if x.get("answers") or x.get("answer")]
port = sys.argv[2]
home = os.environ.get("FINDR_HOME") or (os.path.join(os.environ["LOCALAPPDATA"], "findr") if os.name == "nt" and os.environ.get("LOCALAPPDATA")
                                       else os.path.expanduser("~/.findr"))
pw = open(os.path.join(home, ".password"), encoding="utf-8").read().strip() if os.path.exists(os.path.join(home, ".password")) else ""
HDR = {"Authorization": "Basic " + base64.b64encode(f"x:{pw}".encode()).decode(), "X-Findr": "1"}
if os.environ.get("FINDR_EVAL_TODAY"):
    HDR["X-Findr-Today"] = os.environ["FINDR_EVAL_TODAY"]
K = 10


def norm(p):
    p = unicodedata.normalize("NFC", str(p or ""))
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:
        p = f"/mnt/{m.group(1).lower()}/" + m.group(2)
    return p.replace("\\", "/")


def get(route, **params):
    u = f"http://127.0.0.1:{port}{route}?" + urllib.parse.urlencode(params)
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers=HDR), timeout=180))


def ask(x):
    """the results, and the place of the answer among them (None: not in the list). A copy folded under a line is that line"""
    rs = get("/api", q=x["q"], mode="auto", k=K).get("results") or []
    want = {norm(a) for a in (x.get("answers") or [x["answer"]])}
    for i, y in enumerate(rs):
        if ({norm(y.get("path"))} | {norm(c) for c in y.get("copies") or []}) & want:
            return rs, i + 1
    return rs, None


def run(part):
    """per question: the answer's place, and how many files the person opens (down the list until the answer; all of them in vain)"""
    out = []
    for x in part:
        rs, rank = ask(x)
        out.append((rank, rank if rank else len(rs), rs[0].get("habit") if rs else None))
    return out


half = len(qs) // 2
# 'alt' as a third argument: every other question is lived, the rest is the test (a file of several PCs one after another)
learn, test = (qs[::2], qs[1::2]) if sys.argv[3:4] == ["alt"] else (qs[:half], qs[half:])
before = run(test)
sent = shown = 0
for x in learn:
    rs, rank = ask(x)
    shown += bool(rank)
    path = rs[rank - 1]["path"] if rank else (x.get("answers") or [x["answer"]])[0]   # found elsewhere: the person still opened it
    r = get("/fb", verdict="open", q=x["q"], path=path, rank=rank or "")
    sent += bool(r.get("ok"))
after = run(test)


def line(name, rows):
    n = max(1, len(rows))
    return (f"{name:7s} n {len(rows):4d} | #1 {sum(r[0] == 1 for r in rows):4d} | in3 {sum(bool(r[0]) and r[0] <= 3 for r in rows):4d} | "
            f"in{K} {sum(bool(r[0]) for r in rows):4d} | opened avg {sum(r[1] for r in rows) / n:.2f}")


place = lambda r: r if r else K + 1
print(f"questions {len(qs)} | learned from {len(learn)} (answer shown {shown}, opens sent {sent}) | tested on {len(test)}")
print(line("before", before)); print(line("after", after))
better = sum(place(a) < place(b) for (b, *_), (a, *_) in zip(before, after))
worse = sum(place(a) > place(b) for (b, *_), (a, *_) in zip(before, after))
print(f"better {better} | worse {worse} | same {len(test) - better - worse}")
# the #1 results a habit moved up, by the reason given: right (the answer), wrong (it passed the answer), other (neither was #1)
moved = collections.OrderedDict()
for (b_, *_), (a_, _, why) in zip(before, after):
    if why:
        m = moved.setdefault(why, collections.Counter())
        m["n"] += 1; m["right"] += a_ == 1; m["wrong"] += b_ == 1 and a_ != 1
for why, m in moved.items():
    print(f"  #1 moved up by a habit ({why}): {m['n']} | the answer {m['right']} | passed the answer {m['wrong']}")
types = collections.OrderedDict()
for x, (b, *_), (a, *_) in zip(test, before, after):
    t = types.setdefault(x.get("type") or x.get("kind") or "-", collections.Counter())
    t["n"] += 1; t["b1"] += b == 1; t["a1"] += a == 1; t["better"] += place(a) < place(b); t["worse"] += place(a) > place(b)
if len(types) > 1:
    for k, t in types.items():
        print(f"  {str(k)[:24]:24s} n {t['n']:3d} | #1 {t['b1']:3d} -> {t['a1']:3d} | better {t['better']:2d} | worse {t['worse']:2d}")
