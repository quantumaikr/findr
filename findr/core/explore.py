"""stage 3: search as finding your way, with a small local LLM.
v1 (EXPLORE_V=1): open candidates one by one, judge yes/partly/no, look nearby.  -> no gain on big_mkt (11-07): versions look alike one at a time.
v2 (default): what a person does with a list of near-identical files — lay them side by side and compare.
    1. first candidates from the engine (k=30)
    2. the engine adds plain-language facts the model should not have to compute: season/year, "newest of 3 versions", "a copy in Downloads"
    3. one listwise call: "which of these is the file the person means? best and second" over the top LIST_N
    4. the model's picks go first, the rest keep the engine order. A trail is kept for the person.
LLM: OpenAI-compatible llama.cpp server (EXPLORE_URL, default :8095)."""
import json, os, re, time, urllib.request, datetime as dt
from pathlib import Path

URL = os.environ.get("EXPLORE_URL", "http://127.0.0.1:8095/v1/chat/completions")
OPEN_N = int(os.environ.get("EXPLORE_OPEN", "4"))
NEAR_N = int(os.environ.get("EXPLORE_NEAR", "6"))
LIST_N = int(os.environ.get("EXPLORE_LIST", "10"))
SEASON = {3: "봄", 4: "봄", 5: "봄", 6: "여름", 7: "여름", 8: "여름", 9: "가을", 10: "가을", 11: "가을", 12: "겨울", 1: "겨울", 2: "겨울"}
VER = re.compile(r"[_\s-]*(초안|v\d+|최종_수정|최종|수정본?|final|draft)(\s*\(\d+\))?$", re.I)


def _chat(system, user, max_tokens=60, url=None):
    body = json.dumps({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": max_tokens,
                       "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}).encode()
    try:
        r = json.load(urllib.request.urlopen(urllib.request.Request(url or URL, body, {"Content-Type": "application/json"}), timeout=60))
        return r["choices"][0]["message"]["content"]
    except Exception:
        return ""


def facts(cands, now=None):
    """plain-language facts per candidate, computed by the engine (dates, versions, copies)"""
    now = now or dt.datetime.now()
    stem = lambda c: VER.sub("", Path(c["name"]).stem.replace(" (1)", "")).replace("_작업중", "")
    groups = {}
    for c in cands:
        groups.setdefault(stem(c), []).append(c)
    out = {}
    for c in cands:
        try:
            d = dt.datetime.strptime(c.get("date", "")[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            d = None
        f = []
        if d:
            yr = "올해" if d.year == now.year else ("작년" if d.year == now.year - 1 else f"{d.year}년")
            f.append(f"{d.year}년 {d.month}월({yr} {SEASON[d.month]})")
        g = sorted(groups[stem(c)], key=lambda x: x.get("date", ""))
        if len(g) > 1:
            i = g.index(c) + 1
            f.append(f"같은 문서 {len(g)}개 중 {'가장 최근' if i == len(g) else '가장 처음' if i == 1 else str(i) + '번째'}")
        p = c["path"].replace("\\", "/")
        if "/다운로드/" in p: f.append("다운로드 폴더 사본")
        if "/바탕화면/" in p: f.append("바탕화면")
        if "백업" in p: f.append("예전 PC 백업")
        out[c["fid"]] = ", ".join(f) if os.environ.get("EXPLORE_FACTS", "1") == "1" else ""   # ablation switch
    return out


SYS2 = ("당신은 사용자의 PC에서 파일을 찾아 주는 비서입니다. 사용자의 요청과 후보 목록(번호, 이름, 폴더, 사실 정보, 내용 앞부분)을 보고, "
        "사용자가 찾는 바로 그 파일의 번호를 고르세요. '최종본'은 같은 문서 중 가장 최근, '초안/처음'은 가장 처음입니다. "
        "시기, 사람, 숫자 같은 조건이 내용이나 사실 정보와 맞는지 확인하세요. 맞는 것이 없으면 0. "
        "JSON 한 줄로만: {\"best\": 번호, \"second\": 번호, \"why\": \"짧은 이유\"}")


def v2_prompt(F, q, r, now=None, list_n=None, facts_on=True):
    """main thread (sqlite): the listwise prompt for precomputed engine results"""
    rs = r.get("results") or []
    top = rs[:list_n or LIST_N]; fx = facts(rs, now) if facts_on else {c["fid"]: "" for c in rs}
    lines = []
    for i, c in enumerate(top, 1):
        txt = (F.full_text(c["fid"]) or c.get("snippet", ""))[:160].replace("\n", " / ")
        folder = str(Path(c["path"].replace("\\", "/")).parent).split("/files/")[-1]
        lines.append(f"{i}. {c['name']} | 폴더: {folder} | {fx[c['fid']]} | 내용: {txt}")
    return f"요청: {q}\n\n후보:\n" + "\n".join(lines), len(top)


def v2_pick(r, out, n):
    """any thread: apply the model's answer to the results"""
    rs = r.get("results") or []; top = rs[:n]
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        y = json.loads(m.group(0)) if m else {}
    except ValueError:
        y = {}
    pick = [int(v) for v in (y.get("best"), y.get("second")) if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit())]
    pick = [p for p in dict.fromkeys(pick) if 1 <= p <= len(top)]
    first = [top[p - 1] for p in pick]
    return {**r, "results": first + [c for c in rs if c not in first],
            "trail": [{"listed": len(top), "picked": pick, "why": (y.get("why") or "")[:80], "none": y.get("best") in (0, "0")}]}


def explore_v2(F, q, k=30, mode="xce", now=None):
    t0 = time.time()
    r = F.search(q, mode=mode, k=k)
    rs = r.get("results") or []
    if len(rs) < 2:
        return r
    top = rs[:LIST_N]; fx = facts(rs, now)
    lines = []
    for i, c in enumerate(top, 1):
        txt = (F.full_text(c["fid"]) or c.get("snippet", ""))[:160].replace("\n", " / ")
        folder = str(Path(c["path"].replace("\\", "/")).parent).split("/files/")[-1]
        lines.append(f"{i}. {c['name']} | 폴더: {folder} | {fx[c['fid']]} | 내용: {txt}")
    out = _chat(SYS2, f"요청: {q}\n\n후보:\n" + "\n".join(lines), 80)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        y = json.loads(m.group(0)) if m else {}
    except ValueError:
        y = {}
    pick = [int(v) for v in (y.get("best"), y.get("second")) if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit())]
    pick = [p for p in dict.fromkeys(pick) if 1 <= p <= len(top)]
    first = [top[p - 1] for p in pick]
    r["results"] = first + [c for c in rs if c not in first]
    for c in first:
        c["checked"] = "picked"
    r["trail"] = [{"listed": len(top), "picked": pick, "why": (y.get("why") or "")[:80], "none": y.get("best") in (0, "0")}]
    r["explore_ms"] = int(1000 * (time.time() - t0))
    return r


SYS = ("당신은 사용자의 PC에서 파일을 대신 열어 보고 확인해 주는 비서입니다. 사용자가 찾는 파일의 설명과 실제 파일(이름, 폴더, 날짜, 내용 앞부분)을 비교해 "
       "판정하세요. 설명의 모든 조건(주제, 문서 종류, 버전, 시기, 사람)이 맞으면 yes, 주제는 맞지만 일부 조건이 다르면 partly, 주제가 다르면 no. "
       "JSON 한 줄로만 답하세요: {\"verdict\": \"yes|partly|no\", \"why\": \"짧은 이유\"}")


def _ask(q, cand, text):
    user = (f"찾는 파일: {q}\n\n파일 이름: {cand['name']}\n폴더: {str(Path(cand['path'].replace(chr(92), '/')).parent)}\n수정 날짜: {cand.get('date','')}\n"
            f"내용 앞부분:\n{text[:700]}")
    m = re.search(r"\{.*\}", _chat(SYS, user), re.S)
    try:
        y = json.loads(m.group(0)) if m else {}
    except ValueError:
        y = {}
    v = y.get("verdict") if y.get("verdict") in ("yes", "partly", "no") else None
    return v, (y.get("why") or "")[:80]


def explore_v1(F, q, k=30, mode="xce", now=None):
    t0 = time.time()
    r = F.search(q, mode=mode, k=k)
    rs = r.get("results") or []
    if not rs:
        return r
    judged, trail = {}, []
    def judge(c):
        if c["fid"] in judged:
            return judged[c["fid"]][0]
        v, why = _ask(q, c, F.full_text(c["fid"]) or c.get("snippet", ""))
        judged[c["fid"]] = (v, why); trail.append({"name": c["name"], "verdict": v, "why": why})
        return v
    pool = {c["fid"]: c for c in rs}
    for c in rs[:OPEN_N]:
        judge(c)
    if not any(judged[c["fid"]][0] == "yes" for c in rs[:OPEN_N]):
        near_dirs = [str(Path(c["path"].replace(chr(92), "/")).parent) for c in rs[:OPEN_N] if judged[c["fid"]][0] == "partly"] or \
                    [str(Path(rs[0]["path"].replace(chr(92), "/")).parent)]
        seen = 0
        for d in dict.fromkeys(near_dirs):
            sep = "\\" if "\\" in rs[0]["path"] else "/"
            like = d.replace("/", sep)
            rows = F.con.execute("SELECT id FROM files WHERE path LIKE ? AND path NOT LIKE ? ORDER BY mtime DESC LIMIT 20",
                                 (like + sep + "%", like + sep + "%" + sep + "%")).fetchall()
            for c in F.files_out([x[0] for x in rows if x[0] not in pool]):
                pool[c["fid"]] = c
                if seen < NEAR_N:
                    judge(c); seen += 1
    rank = {"yes": 0, "partly": 1, None: 2, "no": 3}
    order = sorted(pool.values(), key=lambda c: (rank[judged[c["fid"]][0]] if c["fid"] in judged else 2, -(c.get("prob") or c.get("ce") or 0)))
    r["results"] = order[:k]; r["trail"] = trail; r["explore_ms"] = int(1000 * (time.time() - t0))
    return r


def explore(F, q, k=30, mode="xce", now=None):
    return (explore_v1 if os.environ.get("EXPLORE_V") == "1" else explore_v2)(F, q, k, mode, now)
