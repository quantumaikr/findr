"""Questions made from file facts, each with the file it came from as the answer: the second half of the KG2KV idea from
AtlasKV (2025). AtlasKV turned knowledge-graph triples into questions by masking one side, and found such questions far more
varied than template-made ones (diversity 0.003% -> 7.9%) and better at teaching what holds on data never seen. Here one
side is always the file: a few of its own words, plus what is remembered about it (who sent it and how, when, what was done
with it, which version it is), said the way a person asks.
    python tools/gen_questions.py FACTS.jsonl --now 2026-11-20 --n 2000 --out QUESTIONS.jsonl [--seed 0]

A question is kept only when no other file fits everything it says (judged on each file's first ten characteristic words
and its facts), so its answer is right by construction; the files that fit part of it -- the other versions of the
document, files with the same words in the same folder -- are kept as hard negatives for tuning a ranker.
QUESTIONS.jsonl is in real_eval's format ({id, kind: known, q, answers}) plus facts, negatives and shape, so real_eval,
stage_value and lane_recall run on it as they are. It holds names and paths and stays on this PC. What is printed is
counts and diversity only: questions, files, cues used, distinct shapes, distinct word pairs (distinct-2), mean length.
The same facts, --now and --seed give the same questions."""
import argparse
import collections
import datetime as dt
import json
import random
import re
import sys
from pathlib import Path

ANCHOR_TERMS = 3   # a question names its file by one or two of its first three words
KIND_WORD = {"pdf": ["pdf"], "hwp": ["한글 파일", "hwp"], "hwpx": ["한글 파일"], "docx": ["워드 문서", "워드 파일"],
             "doc": ["워드 문서"], "xlsx": ["엑셀", "엑셀 파일"], "xls": ["엑셀"], "csv": ["csv"], "pptx": ["발표자료", "ppt"],
             "ppt": ["ppt"], "jpg": ["사진"], "jpeg": ["사진"], "heic": ["사진"], "png": ["캡처"], "txt": ["메모"],
             "md": ["메모"], "eml": ["메일"]}
ENDINGS = ["", " 찾아줘", " 어디 있지", " 어디 뒀더라", " 좀 찾아 줘"]
# (with a person or source, without one): {p} the person, {pi} the person with the subject particle, {s} the source
CUE = {
    "ev:saved_from_mail": (["{pi} 메일로 보낸", "{p}한테 메일로 받은", "{p} 님이 메일로 보내 준"], ["메일로 받은", "메일 첨부로 온"]),
    "ev:saved_from_messenger": (["{pi} 카톡으로 보낸", "{p}한테 카톡으로 받은"], ["카톡으로 받은", "메신저로 받은"]),
    "ev:sent": (["{p}에게 보낸", "{p}한테 보냈던"], ["메일로 보냈던"]),
    "ev:downloaded": (["{s}에서 다운받은"], ["다운받은", "인터넷에서 내려받은"]),
    "ev:printed": ([], ["인쇄했던", "출력해 둔"]),
    "ev:renamed": ([], ["이름 바꾼"]),
    "ev:copied": ([], ["복사해 둔"]),
    "ev:created": ([], ["만든"]),
    "ev:modified": ([], ["고친", "수정한"]),
    "ev:opened": ([], ["열어 봤던"]),
    "author": (["{pi} 만든", "{pi} 작성한", "{p} 님이 쓴"], []),
    "last_by": (["{pi} 마지막으로 고친", "{p} 님이 마지막에 수정한"], []),
}
VERSION = {"latest": ["최종본", "제일 최신 버전", "마지막 버전"],
           "before_latest": ["최종본 직전 버전", "최종본 말고 그 바로 전 버전", "마지막 거 바로 전 버전"],
           "first": ["맨 처음 버전", "처음 초안"]}
HANGUL_NAME = re.compile(r"^[가-힣]{2,4}$")


def batchim(word):
    """does the word end in a consonant (Korean particle choice: 이 / 가)"""
    c = word.strip()[-1:]
    if c and "가" <= c <= "힣":
        return (ord(c) - 0xAC00) % 28 != 0
    return c in "013678"   # 영, 일, 삼, 육, 칠, 팔


def ig(w):
    return w + ("이" if batchim(w) else "가")


def period(day, now):
    """(key, phrases) for a day as people say it relative to now, or None (unknown or after now)"""
    try:
        d = dt.date.fromisoformat(str(day)[:10])
    except ValueError:
        return None
    if d > now:
        return None
    prev = (now.replace(day=1) - dt.timedelta(days=1))
    if (d.year, d.month) == (now.year, now.month):
        return f"m:{d:%Y-%m}", ["이번 달", "이번 달에"]
    if (d.year, d.month) == (prev.year, prev.month):
        return f"m:{d:%Y-%m}", ["지난달", "저번 달"]
    if d.year == now.year:
        return f"m:{d:%Y-%m}", [f"올해 {d.month}월", f"{d.month}월에"]
    if d.year == now.year - 1:
        return f"y:{d.year}", ["작년", "작년에"]
    return f"y:{d.year}", [f"{d.year}년", f"{d.year}년에"]


def load(path):
    """facts by file: {fid: {"path", "terms" [first ten, best first], "ext", "folder", "date", "author", "last_by",
    "ev": [(type, value, time, source)], "version", "family"}}"""
    F = collections.defaultdict(lambda: {"terms": [], "ev": []})
    for l in open(path, encoding="utf-8"):
        if not l.strip():
            continue
        x = json.loads(l); f = F[x["file"]]; f["path"] = x["path"]; r = x["rel"]
        if r == "term":
            f["terms"].append((x.get("rank", len(f["terms"])), x["value"]))
        elif r.startswith("ev:"):
            f["ev"].append((r, x.get("value") or "", x.get("time"), x.get("source")))
        elif r == "version":
            f["version"], f["family"] = x["value"], x.get("family")
        else:
            f[r] = x["value"]
    for f in F.values():
        f["terms"] = [w for _, w in sorted(f["terms"])]
    return dict(F)


def fits(f, parts, now):
    """does file f fit every part of a question"""
    for p in parts:
        k = p["kind"]
        if k == "term" and p["value"] not in f["terms"]:
            return False
        if k == "ext" and f.get("ext") not in p["exts"]:
            return False
        if k == "version" and f.get("version") != p["value"]:
            return False
        if k in ("author", "last_by") and f.get(k) != p["value"]:
            return False
        if k == "date" and (period(f.get("date"), now) or ("",))[0] != p["period"]:
            return False
        if k == "ev" and not any(t == p["type"] and (p["value"] is None or v == p["value"]) and
                                 (p.get("source") is None or s == p["source"]) and
                                 (p["period"] is None or (period(tm, now) or ("",))[0] == p["period"]) for t, v, tm, s in f["ev"]):
            return False
    return True


def person_forms(name):
    return [name, name + " 님"] if HANGUL_NAME.match(name) else [name]


def cue_text(rnd, rel, value, source):
    """a phrase for one remembered fact, and whether it named the person (or source)"""
    named, bare = CUE[rel]
    who = source if rel == "ev:downloaded" else value
    if who and named and (rnd.random() < 0.75 or not bare):
        p = rnd.choice(person_forms(who))
        return rnd.choice(named).format(p=p, pi=ig(p), s=who), True
    return (rnd.choice(bare), False) if bare else (None, False)


def make_one(rnd, fid, f, now):
    """one question about file fid, or None when its facts do not make one: (text, parts, shape)"""
    anchors = f["terms"][:ANCHOR_TERMS]
    if not anchors:
        return None
    parts, words, shape = [], [], []
    for w in rnd.sample(anchors, k=min(len(anchors), rnd.choice([1, 1, 2]))):
        parts.append({"kind": "term", "value": w}); words.append(w)
    shape.append(f"term{len(words)}")
    lead = []
    options = [("ev", e) for e in f["ev"] if e[0] in CUE] + [(k, f[k]) for k in ("author", "last_by") if f.get(k)]
    if options and rnd.random() < 0.75:
        kind, x = rnd.choice(options)
        if kind == "ev":
            rel, value, tm, source = x
            phrase, named = cue_text(rnd, rel, value, source)
            if phrase:
                per = period(tm, now) if tm and rnd.random() < 0.6 else None
                if per:
                    lead.append(rnd.choice(per[1])); shape.append("when")
                lead.append(phrase); shape.append(rel)
                site = rel == "ev:downloaded"   # a download names where it came from, not a person
                parts.append({"kind": "ev", "type": rel, "value": value if named and not site else None,
                              "source": source if named and site else None, "period": per[0] if per else None})
        else:
            phrase, _ = cue_text(rnd, kind, x, None)
            if phrase:
                lead.append(phrase); shape.append(kind)
                parts.append({"kind": kind, "value": x})
    elif f.get("date") and rnd.random() < 0.5:
        per = period(f["date"], now)
        if per:
            lead.append(rnd.choice(per[1]) + rnd.choice(["", " 고친"])); shape.append("date")
            parts.append({"kind": "date", "period": per[0]})
    tail = []
    if f.get("version") and rnd.random() < 0.6:
        tail.append(rnd.choice(VERSION[f["version"]])); shape.append("version:" + f["version"])
        parts.append({"kind": "version", "value": f["version"]})
    ext = f.get("ext")
    if ext in KIND_WORD and rnd.random() < 0.5:
        word = rnd.choice(KIND_WORD[ext])
        tail.append(word); shape.append("kind")
        parts.append({"kind": "ext", "exts": sorted(e for e, ws in KIND_WORD.items() if word in ws)})
    text = " ".join(lead + words + tail) + rnd.choice(ENDINGS)
    return text, parts, "+".join(shape)


def negatives(fid, f, F, parts, k=5):
    """files that fit part of the question: the document's other versions first, then files sharing its words in its folder"""
    out = [g for g, h in F.items() if g != fid and f.get("family") and h.get("family") == f["family"]]
    words = {p["value"] for p in parts if p["kind"] == "term"}
    out += [g for g, h in F.items() if g != fid and g not in out and h.get("folder") == f.get("folder") and words & set(h["terms"])]
    return [F[g]["path"] for g in out[:k]]


def generate(F, now, n, seed=0):
    """(questions, stats): up to n questions whose facts fit only their own file"""
    rnd = random.Random(seed)
    order = sorted(F); rnd.shuffle(order)
    if not order:
        return [], stats([], 0, 0)
    out, seen, dropped = [], set(), 0
    for i in range(n * 20):
        if len(out) >= n:
            break
        fid = order[i % len(order)]; f = F[fid]
        got = make_one(rnd, fid, f, now)
        if not got:
            continue
        text, parts, shape = got
        if text in seen:
            continue
        if any(g != fid and fits(h, parts, now) for g, h in F.items()):
            dropped += 1   # another file fits everything said: no right answer by construction
            continue
        seen.add(text)
        out.append({"id": f"G{len(out) + 1:05d}", "kind": "known", "q": text, "answers": [f["path"]], "shape": shape,
                    "facts": parts, "negatives": negatives(fid, f, F, parts)})
    return out, stats(out, dropped, len(F))


def stats(qs, dropped, files):
    """numbers only: how many, how varied (distinct shapes, distinct-2 over word pairs), how long, which cues"""
    pairs = [tuple(q["q"].split()[i:i + 2]) for q in qs for i in range(max(0, len(q["q"].split()) - 1))]
    cues = collections.Counter(c for q in qs for c in q["shape"].split("+") if not c.startswith("term"))
    return {"questions": len(qs), "files": files, "files_asked": len({q["answers"][0] for q in qs}),
            "dropped_ambiguous": dropped, "distinct_shapes": len({q["shape"] for q in qs}),
            "distinct2": round(len(set(pairs)) / len(pairs), 3) if pairs else 0.0,
            "mean_chars": round(sum(len(q["q"]) for q in qs) / len(qs), 1) if qs else 0.0,
            "with_negatives": sum(1 for q in qs if q["negatives"]), "cues": dict(sorted(cues.items()))}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("facts"); ap.add_argument("--out", required=True)
    ap.add_argument("--now", default=dt.date.today().isoformat(), help="the day the questions are asked (YYYY-MM-DD)")
    ap.add_argument("--n", type=int, default=2000); ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    qs, st = generate(load(a.facts), dt.date.fromisoformat(a.now), a.n, a.seed)
    with open(a.out, "w", encoding="utf-8") as f:   # questions name files and people: they stay here, on this PC
        for q in qs:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    print(json.dumps(st, ensure_ascii=False))
    return st


if __name__ == "__main__":
    main()
