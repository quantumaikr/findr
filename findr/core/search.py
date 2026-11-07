"""findr search (2026-11-03): natural-language file search over the index built by index.py, all local.

  $PY search.py --db ~/findr/ws.db serve [--port 8099]     # keeps the encoder and vectors in memory; page at /
  $PY search.py --db ~/findr/ws.db q "지난달 만든 LoRA 실험 보고서 pdf"

A query is split into conditions read by rules (file kind, time, library or not) and the rest, which is searched two
ways -- words (FTS5 trigram over name, path and text) and meaning (bge-m3 vectors) -- and fused by rank (RRF).
Files are ranked by their best chunk; bundled-library files are pushed down unless the query asks for them.
"""
import argparse
import episodic as EPI   # memory-trace cues (who, from where, what was done, while doing what)
EPI_G = 0.05   # min content score for overruling remembered history; two-author tuning sets (11-07)
EPI_AGREE = 0.15   # episodic-path best / content-path best; set on the episodic tuning set (11-07)
import os, re   # needed here: this block sits above the main imports
HISTORY_HINT = re.compile(r"메일|이메일|카톡|카카오톡|메신저|단톡|슬랙|다운받|다운로드|보낸|보냈|받은|받았|보내온|보내준|제출|회신|인쇄|출력|뽑은|"
                          r"회의|미팅|면접|발표|세미나|마지막으로|여러\s*번|에게|한테|께서|님이|님께|님한테|"
                          r"몇\s*번|고친|고쳐|수정한|손본|만든|만들었|작성한|열어\s*본|열어봤|이름\s*바|복사|저장한|저장해|나간|돌린|올린|올려|")   # clear history wording only (latency)
_QU = None


_EPI_POOL, _EPI_FUT = None, {}
MODEL_CONTENT = {}   # the model's reading of what the file is about, kept for a second opinion on agreement


def epi_prefetch(q):
    """start reading the request in the background so the model's ~140 ms overlaps retrieval"""
    global _EPI_POOL
    if _EPI_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _EPI_POOL = ThreadPoolExecutor(2)
    if len(_EPI_FUT) > 32:
        _EPI_FUT.clear()
    _EPI_FUT[q] = _EPI_POOL.submit(_epi_read, q)


def epi_read(q):
    f = _EPI_FUT.pop(q, None)
    if f is None:
        return _epi_read(q)
    try:   # do not keep the person waiting on the reading model: past QU_WAIT seconds, go on with the rules' reading
        return f.result(timeout=float(os.environ.get("QU_WAIT", "0.6")))
    except Exception:
        return EPI.cues(q), None


def _epi_read(q):
    """(episodic cues, content part) from the learned model when the question hints at history; rules otherwise / as fallback"""
    global _QU
    rules_cues = EPI.cues(q)
    if not rules_cues and not HISTORY_HINT.search(q) and os.environ.get("EPI_ALWAYS", "0") != "1":
        return [], None
    if os.environ.get("QU_URL", "http://127.0.0.1:8093/v1/chat/completions") != "off":
        if _QU is None:
            from qu_model import QUServer
            _QU = QUServer(os.environ.get("QU_URL", "http://127.0.0.1:8093/v1/chat/completions"))
        y = _QU.read(q)
        if y is not None:
            from qu_model import to_cues
            L, R = to_cues(y, q, EPI.action_dates), EPI.cues(q)
            if os.environ.get("QU_MODE", "agree") == "agree" and L and R:
                sig = lambda c: (c["kind"], c.get("role") or c.get("type") or c.get("side") or "")
                rs = {sig(c) for c in R}
                LA = [c for c in L if sig(c) in rs]
                if os.environ.get("QU_RULE_LITERAL", "1") == "1":   # literal rule reads (explicit dates, print/copy/rename verbs) the model left out
                    have = {c["kind"] for c in LA}
                    got = "received" in (y.get("actions") or []) or any(c.get("role") == "from" for c in L)
                    LA += [c for c in R if c["kind"] in ("when", "action") and c["kind"] not in have and not (got and c.get("type") == "sent")]
                L = LA
            if y.get("content"):
                if len(MODEL_CONTENT) > 64:
                    MODEL_CONTENT.clear()
                MODEL_CONTENT[q] = y["content"]
            return (L or R), (None if os.environ.get("QU_CONTENT") == "rules" else (y.get("content") or None))
    return EPI.cues(q), None   # episodic-path best / content-path best; set on the episodic tuning set (11-07)
import datetime as dt
import html
import json
import re
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

KINDS = [  # (words in the query, extensions); a format name filters, a topic-like word ("영상", "코드") only boosts
    (r"배치\s*파일|\.?bat\b|cmd\b", {".bat", ".cmd"}),
    (r"pdf|피디에프", {".pdf"}),
    (r"엑셀|스프레드시트|xlsx?|시트", {".xlsx", ".xls", ".csv"}),
    (r"csv", {".csv"}),
    (r"한글\s*(파일|문서)|hwpx?|아래아", {".hwp", ".hwpx"}),
    (r"워드|docx?", {".docx", ".doc"}),
    (r"파워\s*포인트|pptx?|발표\s*자료|슬라이드", {".pptx", ".ppt"}),
    (r"파이썬|python|\.py\b", {".py"}),
    (r"코드|스크립트|소스", {".py", ".js", ".ts", ".sh", ".ps1", ".bat", ".c", ".cpp", ".h", ".cu", ".go", ".rs", ".java"}),
    (r"로그|log", {".log"}),
    (r"이미지|사진|그림|스크린샷|jpe?g|png", {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}),
    (r"영상|동영상|비디오|mp4", {".mp4", ".mov", ".avi", ".mkv", ".webm"}),
    (r"설정\s*파일|config|yaml|json", {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}),
    (r"모델\s*파일|가중치|safetensors|gguf", {".safetensors", ".gguf", ".ckpt", ".pt", ".bin"}),
    (r"마크다운|md\b|메모|노트", {".md", ".txt"}),
    (r"웹\s*페이지|html", {".html", ".htm"}),
]
HARD = re.compile(r"로그|log|pdf|피디에프|엑셀|xlsx?|csv|hwpx?|한글\s*(파일|문서)|워드|docx?|pptx?|파워\s*포인트|배치\s*파일|\.?bat\b|mp4|jpe?g|png|safetensors|gguf|\.py\b|(\S+)\s*파일", re.I)
# artifacts people rarely mean: machine results, trainer checkpoints
ARTIFACT = re.compile(r"/(results|checkpoint-\d+|trainer|logs?|data)/|/day\d+\.json$|\.jsonl$")
# Korean words -> the English the code and file names use (code is written in English, people ask in Korean)
GLOSS = {"원장": "ledger", "개정": "rev revision", "매뉴얼": "manual", "사람": "human person", "시간": "minutes time",
         "넘긴": "routed handoff", "사전": "pre-registered", "판정 기준": "pre-registered criterion", "시드": "seed",
         "흔들": "noise variance", "서버": "serve server", "띄우": "serve start", "학습 데이터": "gen data train",
         "만드는": "gen build make", "생성": "gen generate", "공지": "notice", "추출": "extract", "프롬프트": "prompt",
         "평가": "eval judge", "채점": "judge", "독립": "independent", "질문 세트": "questions set", "시험지": "test set",
         "비교": "cmp compare", "결과": "results", "설정": "config args", "학습률": "lr learning rate", "벤치마크": "bench",
         "갤러리": "gallery", "보고서": "report", "요약": "summary", "로그": "log", "에러": "error Traceback", "메모리": "memory OOM",
         "워크플로": "workflow", "인코더": "enc encoder", "검색": "search retrieval rag", "판독": "read slot", "대상": "target",
         "누락": "missing vague", "확신": "confident", "오답": "wrong error", "시나리오": "scenario", "운영": "ops",
         "계산": "compute calc", "재학습": "retrain", "증류": "distill", "지도": "map jido", "체크포인트": "checkpoint",
         "백업": "bak backup old", "이전 버전": "old prev bak", "복사본": "copy bak", "옛날": "old"}
STOP = r"(수정한|수정된|만든|작성한|저장한|파일|문서|찾아\s*줘|찾아\s*주세요|찾아|어디\s*있(어|나요|지)?|있는|관련(된)?|에\s*대한|좀|것|거)"


PARTICLE = r"(?:을|를|이|가|은|는|에|의|로|과|와|도|만|에서|으로|이랑|랑)"


def word(pat):
    """pat as a whole word: no letter or digit glued in front; behind it the end, a non-letter, or a particle then a non-letter"""
    return r"(?<![A-Za-z0-9가-힣])(?:" + pat + r")(?=$|[^A-Za-z0-9가-힣]|" + PARTICLE + r"(?:$|[^가-힣]))"


EVAL_NOW = None   # set only while serving an evaluation request that pins 'today' (X-Findr-Today)


def today_now():
    return EVAL_NOW or EPI.TODAY or dt.datetime.now()   # EPI.TODAY is shared with ng.py (it imports a second copy of this module)


def parse(q, now=None):
    now = now or today_now(); exts, soft, since, until, lib = set(), set(), None, None, False
    rest = q
    for pat, ex in KINDS:
        m = re.search(word(pat), q, re.I)
        if m:
            if HARD.fullmatch(m.group(0)) or re.search(re.escape(m.group(0)) + r"\s*파일", q):
                exts |= ex; rest = re.sub(word(pat), " ", rest, flags=re.I)   # a format: filter, and drop the word
            else:
                soft |= ex                                                # a topic word: keep it, boost the kind
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    D = dt.timedelta
    for pat, f in [(r"(\d+)\s*(개월|달)\s*(이내|안에|동안|사이|간)", lambda m: (day0 - D(30.44 * int(m.group(1))), None)),
                   (r"(\d+)\s*(개월|달)\s*전", lambda m: (day0 - D(30.44 * (int(m.group(1)) + 0.5)), day0 - D(30.44 * (int(m.group(1)) - 0.5)))),
                   (r"반년\s*전", lambda m: (day0 - D(30.44 * 7), day0 - D(30.44 * 5))),
                   (r"(\d+)\s*주\s*전", lambda m: (day0 - D(7 * int(m.group(1)) + 4), day0 - D(7 * int(m.group(1)) - 4))),
                   (r"(\d+)\s*년\s*전", lambda m: (day0 - D(365 * int(m.group(1)) + 183), day0 - D(365 * int(m.group(1)) - 183))),
                   (r"(\d+)\s*일\s*(이내|안에|동안)", lambda m: (day0 - D(int(m.group(1))), None)),
                   (r"오늘", lambda m: (day0, None)), (r"어제", lambda m: (day0 - dt.timedelta(1), day0)),
                   (r"이번\s*주", lambda m: (day0 - dt.timedelta(day0.weekday()), None)),
                   (r"지난\s*주", lambda m: (day0 - dt.timedelta(day0.weekday() + 7), day0 - dt.timedelta(day0.weekday()))),
                   (r"이번\s*달", lambda m: (day0.replace(day=1), None)),
                   (r"지난\s*달", lambda m: ((day0.replace(day=1) - dt.timedelta(1)).replace(day=1), day0.replace(day=1))),
                   (r"최근\s*(\d+)\s*일", lambda m: (day0 - dt.timedelta(int(m.group(1))), None)),
                   (r"(\d+)\s*일\s*전", lambda m: (day0 - dt.timedelta(int(m.group(1))), day0 - dt.timedelta(int(m.group(1)) - 1))),
                   (r"최근|요즘", lambda m: (day0 - dt.timedelta(14), None))]:
        m = re.search(pat, rest)
        if m:
            since, until = f(m); rest = rest.replace(m.group(0), " "); break
    if since and until and (until - since) <= dt.timedelta(days=1):   # a single day: hours narrow it ("어제 오후 3시쯤", "새벽")
        h = re.search(r"(오전|오후)\s*(\d{1,2})\s*시", q); part = re.search(r"새벽|아침|점심|저녁|밤", q)
        if h:
            hh = int(h.group(2)) % 12 + (12 if h.group(1) == "오후" else 0)
            since, until = since + dt.timedelta(hours=max(0, hh - 2)), since + dt.timedelta(hours=min(24, hh + 2))
        elif part:
            a, b = {"새벽": (0, 7), "아침": (6, 11), "점심": (11, 14), "저녁": (17, 22), "밤": (19, 24)}[part.group(0)]
            since, until = since + dt.timedelta(hours=a), since + dt.timedelta(hours=b)
    if re.search(r"라이브러리|패키지|site-packages|설치된", q):
        lib = True
    rest = re.sub(word(STOP), " ", rest); rest = re.sub(r"\s+", " ", rest).strip(" ?.,")
    rest = re.sub(r"(?:^|\s)(에|에서|의|을|를|이|가|은|는|로|으로|과|와|도|만|이랑|랑|쯤|경|정도|좀|것|거|최근|요즘)(?=\s|$)", " ", " " + rest).strip()   # particles left by a removed period
    return {"exts": exts, "soft": soft - exts, "since": since, "until": until, "lib": lib, "text": rest or q, "only_cond": not rest}


FKINDS = ["계약서", "견적서", "발주서", "거래명세서", "회의록", "업무보고", "출장보고서", "클레임보고서", "점검일지", "교육보고",
          "비밀유지계약서", "제안서", "공지", "양식", "개인메모"]
COND_PROMPT = """오늘은 {today}입니다. 아래는 회사 PC에서 파일을 찾는 질문입니다. 질문에 실제로 나온 조건만 JSON으로 쓰세요.
질문에 없는 조건은 null로 두세요. 추측하지 마세요.
- "종류": 찾는 문서 종류. {kinds} 중 해당하는 것의 목록 (여러 개 가능, 없으면 [])
- "거래처": 상대 회사 이름 (없으면 null)
- "기간": 질문에 시기 표현이 있을 때만 {{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}} (오늘 기준으로 계산, 계절·분기는 넉넉히)
- "금액": 금액 조건이 있을 때만 {{"min": 원 단위 정수 또는 null, "max": 원 단위 정수 또는 null}} ("쯤/정도"는 위아래 30%)
- "사람": 작성자·담당자 이름 (없으면 null)
- "상태": 서명본·최종본을 찾으면 "최종", 초안·수정 전 버전을 찾으면 "초안", 아니면 null
질문: {q}"""


import os  # noqa: E402
PROFILE = os.environ.get("FINDR_PROFILE", "biz")

def _auto_device():
    """cuda (NVIDIA), mps (Apple), else cpu"""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _has_facts(f):
    """the understanding-first route reads work facts; an index built without them uses the reranker route"""
    if getattr(f, "_facts_ok", None) is None:
        try:
            f._facts_ok = bool(f.con.execute("SELECT 1 FROM facts LIMIT 1").fetchone())
        except Exception:
            f._facts_ok = False
    return f._facts_ok

DEVICE = os.environ.get("FINDR_DEVICE") or _auto_device()   # cpu: when the GPU is busy (2026-11-05: the 12B teacher held it)   # biz: company documents; pc: a researcher's own workspace
if PROFILE == "pc":
    FKINDS = ["보고서", "문서", "코드", "설정", "데이터", "실험결과", "로그", "프롬프트", "메모"]
    COND_PROMPT = """오늘은 {today}입니다. 아래는 내 PC에서 파일을 찾는 질문입니다. 질문에 실제로 나온 조건만 JSON으로 쓰세요.
질문에 없는 조건은 null로 두세요. 추측하지 마세요.
- "종류": 찾는 파일 종류. {kinds} 중 해당하는 것의 목록 (스크립트·프로그램은 코드, 결과 json·점수표는 실험결과; 확실할 때만, 없으면 [])
- "거래처": null
- "기간": 질문에 시기 표현(지난주, 11월 초, 어제 등)이 있을 때만 {{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}}
- "금액": null
- "사람": 질문에 사람 이름이 있을 때만 그 이름 (없으면 null)
- "상태": null
질문: {q}"""


def parse_cond(q, today=None):
    today = today or today_now().date().isoformat()
    try:
        j = json.loads(chat_json(COND_PROMPT.format(today=today, kinds=", ".join(FKINDS), q=q), 200))
    except Exception:
        return {}
    c = {}
    ks = j.get("종류") or []
    ks = [ks] if isinstance(ks, str) else ks
    ks = [k for k in ks if k in FKINDS]
    if ks:
        c["kinds"] = ks + (["비밀유지계약서"] if "계약서" in ks else [])
    if isinstance(j.get("거래처"), str) and j["거래처"].strip():
        c["partner"] = re.sub(r"\(주\)|주식회사|\s", "", j["거래처"])
    per = j.get("기간") if isinstance(j.get("기간"), dict) else None
    if per and any(isinstance(per.get(x), str) and re.match(r"\d{4}-\d{2}-\d{2}$", per[x]) for x in ("from", "to")):
        c["from"] = per.get("from") if isinstance(per.get("from"), str) else None
        c["to"] = per.get("to") if isinstance(per.get("to"), str) else None
    amt = j.get("금액") if isinstance(j.get("금액"), dict) else None
    if amt and (isinstance(amt.get("min"), (int, float)) or isinstance(amt.get("max"), (int, float))):
        c["amin"] = amt.get("min") if isinstance(amt.get("min"), (int, float)) else None
        c["amax"] = amt.get("max") if isinstance(amt.get("max"), (int, float)) else None
    w = period_by_code(q, dt.date.fromisoformat(today))   # common period words: computed by code, not by the model
    if w:
        c["from"], c["to"] = w
    if isinstance(j.get("사람"), str) and j["사람"].strip():
        c["person"] = re.sub(r"\s|(대리|과장|부장|차장|사원|팀장|대표|님|씨)$", "", j["사람"].strip())
    if j.get("상태") in ("최종", "초안"):
        c["status"] = j["상태"]
    return c


def period_by_code(q, today):
    """(from, to) ISO dates for the period words people use, or None. The 4B got '작년' wrong (2025-11-05~) on dev."""
    import calendar
    y = today.year
    m = re.search(r"(재작년|작년|지난\s*해|올해|올\s|금년)", q)
    yr = {"재작년": y - 2, "작년": y - 1, "지난해": y - 1, "올해": y, "올": y, "금년": y}.get(re.sub(r"\s", "", m.group(1))) if m else None
    seasons = {"봄": (3, 5), "여름": (6, 8), "가을": (9, 11), "초": (1, 4), "상반기": (1, 6), "하반기": (7, 12), "말": (10, 12), "연말": (11, 12)}
    def span(yy, a, b):
        return f"{yy}-{a:02d}-01", f"{yy}-{b:02d}-{calendar.monthrange(yy, b)[1]}"
    s = re.search(r"(봄|여름|가을|상반기|하반기|연말(?!\s*정산)|겨울)|(?:해|년)\s*(초|말)|(?<![가-힣])(초|말)(?:에|쯤|경)?(?![가-힣])", q)
    mo = re.search(r"(?<!\d)(1[0-2]|[1-9])\s*월", q)
    if re.search(r"지난\s*달|저번\s*달", q):
        first = today.replace(day=1); prev = first - dt.timedelta(1)
        return prev.replace(day=1).isoformat(), prev.isoformat()
    if re.search(r"이번\s*달", q):
        return today.replace(day=1).isoformat(), today.isoformat()
    if mo:
        mm = int(mo.group(1)); yy = yr or (y if mm <= today.month else y - 1)
        a, b = span(yy, mm, mm)
        if re.search(rf"{mo.group(1)}\s*월\s*초", q):
            b = f"{yy}-{mm:02d}-10"
        if re.search(rf"{mo.group(1)}\s*월\s*말", q):
            a = f"{yy}-{mm:02d}-20"
        return a, b
    if s and (yr or s.group(1)):
        key = s.group(1) or s.group(2) or s.group(3)
        yy = yr or y
        if key == "겨울":   # 작년 겨울 = Dec of that year to Feb next; plain 겨울 = the last one
            a = yy if yr else yy - 1
            return f"{a}-12-01", f"{a + 1}-02-{calendar.monthrange(a + 1, 2)[1]}"
        if key in seasons:
            return span(yy, *seasons[key])
    if yr:
        return f"{yr}-01-01", (today.isoformat() if yr == y else f"{yr}-12-31")
    return None


KIND_GROUPS = [{"데이터", "실험결과", "로그"}, {"보고서", "문서", "메모"}] if PROFILE == "pc" else []


def kind_group(ks):
    """on a PC the line between a result file and a data file (or a report and a note) is fuzzy: accept the group"""
    out = set(ks)
    for g in KIND_GROUPS:
        if out & g:
            out |= g
    return out


def fact_ok(f, c, slack=10, loose=False):
    if "kinds" in c and f["kind"] not in kind_group(c["kinds"]):
        return False
    if "partner" in c:   # the card's partner list, or the name written in the file itself (code-checked)
        pn = c["partner"]
        if not (any(pn in re.sub(r"\(주\)|\s", "", x) or re.sub(r"\(주\)|\s", "", x) in pn for x in f["partners"] if len(x) >= 2)
                or pn in f["text"] or pn in f["path"]):
            return False
    if c.get("from") or c.get("to"):
        d = dt.date.fromisoformat(f["date"])
        if c.get("from") and d < dt.date.fromisoformat(c["from"]) - dt.timedelta(slack):
            return False
        if c.get("to") and d > dt.date.fromisoformat(c["to"]) + dt.timedelta(slack):
            return False
    if "amin" in c or "amax" in c:
        a = f["amount"]; w = 0.3 if loose else 0.1
        if a is None:
            return False
        if c.get("amin") and a < c["amin"] * (1 - w):
            return False
        if c.get("amax") and a > c["amax"] * (1 + w):
            return False
    if "person" in c:
        pn = c["person"]
        names = f["people"] + re.findall(r"/(?:PC|퇴사자백업)/([가-힣]{2,4})", f["path"])
        if len(pn) == 1:
            if not any(x.startswith(pn) for x in names):
                return False
        elif not any(pn in x or x in pn for x in names if len(x) >= 2):
            return False
    if "status" in c and not loose and f["status"] != c["status"]:
        return False
    return True


def chat_json(prompt, max_tokens):
    import urllib.request
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0,
                       "response_format": {"type": "json_object"}, "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(LLM, body, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"]


class Finder:
    def __init__(self, db, model="bge-m3"):
        self.con = sqlite3.connect(Path(db).expanduser(), check_same_thread=False)
        self.con.execute("CREATE TABLE IF NOT EXISTS cards(file INTEGER PRIMARY KEY, outline TEXT, summary TEXT, done INT DEFAULT 0)")
        rows = self.con.execute("SELECT c.id, c.emb FROM chunks c WHERE c.emb IS NOT NULL").fetchall()
        self.ids = np.array([r[0] for r in rows], dtype=np.int64)   # a fresh index may have no vectors yet (words only)
        self.M = np.stack([np.frombuffer(r[1], np.float16) for r in rows]).astype(np.float32) if rows else np.zeros((0, 1024), np.float32)
        Enc = None   # the app builds EmbeddingGemma-2 or e5-small indexes only
        from enc_eg2 import EG2, encoder_of
        self.encoder = encoder_of(self.con)   # the index says which meaning encoder built its vectors
        from enc_eg2 import make_encoder
        self.enc = make_encoder(self.encoder, DEVICE, max_len=128) if self.encoder in ("embeddinggemma-2", "e5-small") else Enc(model, device=DEVICE)
        # installed package names: a question that names one (peft, trl, bitsandbytes) means the library
        self.pkgs = {m.group(1).lower() for (p,) in self.con.execute("SELECT path FROM files WHERE tier=2")
                     for m in [re.search(r"site-packages/([A-Za-z0-9_]+)/", p)] if m and len(m.group(1)) >= 3}
        self.pkgs -= {"lora", "gemma", "comfyui", "prompt", "test", "tests", "data", "model", "models", "utils", "docs", "json", "html"}
        print(f"FINDR_READY chunks {len(self.ids)} packages {len(self.pkgs)}", flush=True)

    def refresh(self, every=60):
        """an index that grew while the page was open (background indexing): pick up new vectors without a restart"""
        now = time.time()
        if now - getattr(self, "_seen_at", 0) < every:
            return
        self._seen_at = now
        n = self.con.execute("SELECT count(*) FROM chunks WHERE emb IS NOT NULL").fetchone()[0]
        top = self.con.execute("SELECT max(id) FROM chunks").fetchone()[0] or 0
        if n == len(self.ids) and top == getattr(self, "_top", top):
            self._top = top
            return
        rows = self.con.execute("SELECT c.id, c.emb FROM chunks c WHERE c.emb IS NOT NULL").fetchall()
        self.ids = np.array([r[0] for r in rows], dtype=np.int64)
        self.M = np.stack([np.frombuffer(r[1], np.float16) for r in rows]).astype(np.float32) if rows else np.zeros((0, 1024), np.float32)
        self.Mg = None; self.name_rows = None; self._top = top
        print(f"FINDR_RELOAD vectors {len(self.ids)}", flush=True)

    def load_scan(self):
        if getattr(self, "name_rows", None) is not None:
            return
        self.card_txt = [(cid, (t or "").lower()) for cid, t in self.con.execute(
            "SELECT c.id, c.text FROM chunks c JOIN files f ON f.id=c.file WHERE c.n=-1 AND f.tier=1 ORDER BY c.id")]
        self.name_rows = [(cid, nm.lower(), tr) for cid, nm, tr in self.con.execute(
            "SELECT c.id, f.name, f.tier FROM files f JOIN chunks c ON c.file=f.id AND c.n=0 ORDER BY f.id")]

    def dense_all(self, qv):
        """query x every chunk vector on the GPU (2026-11-05: 256k chunks took most of the 1.1 s on the CPU)"""
        import torch
        if DEVICE == "cpu":
            return self.M @ qv
        if getattr(self, "Mg", None) is None:
            self.Mg = torch.from_numpy(self.M).to(DEVICE, torch.float16)
        with torch.no_grad():
            return (self.Mg @ torch.from_numpy(qv).to(DEVICE, torch.float16)).float().cpu().numpy()

    def qvec(self, t):
        import torch
        with torch.no_grad():
            return self.enc.emb([t]).cpu().numpy()[0]

    def find_specific(self, q, sp, cap=8):
        """files that literally contain a must-have of the question, ordered by the reranker"""
        key = sp[2] if sp[0] == "word" else sp[1]
        ids = set()
        if len(key) >= 3:
            try:
                for (cid,) in self.con.execute("SELECT rowid FROM fts WHERE fts MATCH ? LIMIT 400", ('"' + key.replace('"', '') + '"',)):
                    f_ = self.con.execute("SELECT file FROM chunks WHERE id=?", (cid,)).fetchone()
                    if f_:
                        ids.add(f_[0])
            except sqlite3.OperationalError:
                pass
        self.load_scan()
        ids |= {fid for fid, in self.con.execute("SELECT id FROM files WHERE lower(name) LIKE ? LIMIT 200", (f"%{key.lower()}%",))}
        rows = [x for x in self.files_out(sorted(ids)[:200]) if x["kind"] != "라이브러리"
                and has_specific(sp, x["name"] + " " + x["path"], self.full_text(x["fid"]))]
        if not rows:
            return []
        for x in rows:
            x["head"] = self.full_text(x["fid"], 700); x["summary"] = ""
        sc = ce_scores(q, rows[:40])
        ranked = sorted(zip(sc, rows[:40]), key=lambda t: -t[0])
        self.last_repair_score = ranked[0][0]
        return [x["fid"] for _, x in ranked[:cap]]

    def content_words(self, text):
        ws = []
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9._\-]*|[가-힣]+", text):
            w = re.sub(PARTICLE + "$", "", w) if re.fullmatch(r"[가-힣]+", w) and len(w) > 2 else w
            if len(w) >= 2:
                ws.append(w)
        return ws

    def literal_files(self, text, limit=30):
        """for a 1-3 word question: files whose name, path or text contains every word (case-insensitive), newest first"""
        ws = self.content_words(text)
        if not 1 <= len(ws) <= 3:
            return []
        sets = None
        for w in ws:
            like = "%" + w.replace("%", "").replace("_", "\\_") + "%"
            fs = {f_ for (f_,) in self.con.execute("SELECT id FROM files WHERE (name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\') AND tier IN (1,3)", (like, like))}
            if len(w) >= 3:   # trigram index: fast
                fs |= {f_ for (f_,) in self.con.execute("SELECT DISTINCT c.file FROM fts JOIN chunks c ON c.id = fts.rowid WHERE fts MATCH ? LIMIT 5000",
                                                        ('"' + w.replace('"', '') + '"',))}
            elif len(ws) == 1:   # a 2-char single keyword ("대산"): the trigram index cannot hold it, scan the text once
                fs |= {f_ for (f_,) in self.con.execute("SELECT DISTINCT file FROM chunks WHERE text LIKE ? ESCAPE '\\'", (like,))}
            sets = fs if sets is None else sets & fs
            if not sets:
                return []
        if len(sets) > 200:   # a common word is not a keyword search
            return []
        return [f_ for (f_,) in self.con.execute(f"SELECT id FROM files WHERE id IN ({','.join('?' * len(sets))}) ORDER BY mtime DESC LIMIT ?", (*sets, limit))]

    def coverage(self, text, top):
        """share of the question's content words (2+ chars, particles stripped) present in the file's name, path or text"""
        ws = {w.lower() for w in self.content_words(text)}
        if not ws:
            return 1.0
        hay = ((top.get("name") or "") + " " + top.get("path", "") + " " + self.full_text(top["fid"])).lower()
        return sum(w in hay for w in ws) / len(ws)

    def has_all_words(self, text, top):
        """every content word of the question (2+ chars, particles stripped) is in the top file's name or text"""
        ws = []
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9._\-]*|[가-힣]+", text):
            w = re.sub(PARTICLE + "$", "", w) if re.fullmatch(r"[가-힣]+", w) and len(w) > 2 else w
            if len(w) >= 2:
                ws.append(w.lower())
        if not ws or len(ws) > 2:   # only 1-2 word keyword searches (dev 11/6: a 3-word "2단계 설계 문서" that does not exist had all its words in a README)
            return False
        hay = ((top.get("name") or "") + " " + top.get("path", "") + " " + self.full_text(top["fid"])).lower()
        return all(w in hay for w in ws)

    def stats(self):
        if getattr(self, "_stats", None) is None:
            self._stats = {"files": self.con.execute("SELECT count(*) FROM files").fetchone()[0],
                           "chunks": self.con.execute("SELECT count(*) FROM chunks").fetchone()[0]}
        return self._stats

    def sim_scale(self, v):
        """meaning-similarity on the scale the 'not here' gate was fitted on (EG2); an e5 index maps its own scale"""
        if getattr(self, "encoder", "") != "e5-small" or os.environ.get("SIMMAP") == "off":
            return v
        if not hasattr(self, "_simmap"):
            try:
                self._simmap = json.load(open(Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "gate" / "simmap_e5.json"))
            except Exception:
                self._simmap = {"k": 1.0, "c": 0.0}
        return self._simmap["k"] * v + self._simmap["c"]

    def full_text(self, fid, limit=60000):
        t = self.con.execute("SELECT group_concat(text, ' ') FROM chunks WHERE file=? AND n>=0", (fid,)).fetchone()[0] or ""
        return t[:limit]

    def load_photos(self):
        """photo vectors (photo_emb table, written by the indexer with PHOTO_URL); reloaded when their count changes"""
        try:
            n = self.con.execute("SELECT count(*) FROM photo_emb").fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        if getattr(self, "_ph_n", -1) != n:
            cols = [r[1] for r in self.con.execute("PRAGMA table_info(photo_emb)")]
            when = "COALESCE(p.taken, f.mtime)" if "taken" in cols else "f.mtime"
            rows = self.con.execute(f"SELECT p.file, p.emb, {when} FROM photo_emb p JOIN files f ON f.id=p.file").fetchall() if n else []
            self.ph_fids = [r[0] for r in rows]; self.ph_mt = np.array([r[2] or 0 for r in rows], dtype=np.float64)
            self.PH = np.stack([np.frombuffer(r[1], np.float16).astype(np.float32) for r in rows]) if rows else None
            self._ph_n = n
        return self.PH is not None

    def photo_merge(self, q, r, fut, k, p=None):
        """photos whose content matches the request go first (a photo has no text for the reranker to read)"""
        try:
            v, scale = fut.result(timeout=float(os.environ.get("PHOTO_WAIT", "1.0")))
        except Exception:
            return
        if not self.load_photos():
            return
        s = scale * (self.PH @ v)
        T = float(os.environ.get("PHOTO_T_WORD", "3.0")) if PHOTO_WORD.search(q) else float(os.environ.get("PHOTO_T", "3.0"))
        a_ = b_ = None
        if getattr(fut, "window", None):
            a_, b_ = fut.window[0].timestamp(), fut.window[1].timestamp()
        elif p and (p.get("since") or p.get("until")):
            a_ = p["since"].timestamp() if p.get("since") else None; b_ = p["until"].timestamp() if p.get("until") else None
        if a_ is not None or b_ is not None:   # a remembered time narrows the photos, unless no photo in it fits
            out_ = np.zeros(len(s), dtype=bool)
            if a_ is not None:
                out_ |= self.ph_mt < a_
            if b_ is not None:
                out_ |= self.ph_mt >= b_
            if (~out_).any() and float(s[~out_].max()) >= T:
                s = s.copy(); s[out_] = -99.0; r["photo_when"] = True
        order = np.argsort(-s)[:k]; s1 = float(s[order[0]])
        r["photo_top"] = round(s1, 2)
        if s1 < T:
            return
        keep = [i for i in order if s[i] >= T and s[i] >= s1 - float(os.environ.get("PHOTO_MARGIN", "1.5"))][:int(os.environ.get("PHOTO_N", "5"))]
        fids = [int(self.ph_fids[i]) for i in keep]; sim = {int(self.ph_fids[i]): float(s[i]) for i in keep}
        outs = self.files_out(fids)
        mid, w = float(os.environ.get("PHOTO_MID", "3.5")), float(os.environ.get("PHOTO_W", "0.7"))
        for x in outs:
            x["photo"] = round(sim[x["fid"]], 2); x["photo_p"] = round(1 / (1 + math.exp(-(sim[x["fid"]] - mid) / w)), 3)
        r["results"] = (outs + [x for x in r["results"] if x["fid"] not in sim])[:k]
        r["photo_hit"] = len(outs)
        for key in ("note", "hard_none", "none_msg"):   # a photo that clearly matches is an answer
            r.pop(key, None)
        if r.get("results"):
            r["note"] = ""

    def files_out(self, fids):
        """result rows for a list of file ids, in that order (used by the understanding-first search)"""
        if not fids:
            return []
        qm = ",".join("?" * len(fids))
        rows = {r[5]: r for r in self.con.execute(f"""SELECT f.path, f.name, f.size, f.mtime, f.tier, f.id,
                    (SELECT text FROM chunks c WHERE c.file=f.id AND c.n=0) FROM files f WHERE f.id IN ({qm})""", list(fids))}
        return [{"path": winpath(r[0]), "name": r[1], "size": r[2], "date": dt.datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d %H:%M"),
                 "kind": {1: "문서·코드", 2: "라이브러리", 3: "이름만"}.get(r[4], ""), "snippet": (r[6] or "")[:160], "score": 0, "fid": r[5]}
                for f in fids for r in [rows.get(f)] if r]

    def recent(self, p, k, t0):
        cond, args = ["f.tier IN (1,3)"], []
        if p["exts"]:
            cond.append(f"f.ext IN ({','.join('?' * len(p['exts']))})"); args += sorted(p["exts"])
        if p["since"]:
            cond.append("f.mtime>=?"); args.append(p["since"].timestamp())
        if p["until"]:
            cond.append("f.mtime<?"); args.append(p["until"].timestamp())
        rows = self.con.execute(f"""SELECT f.path, f.name, f.size, f.mtime, f.tier, f.id, (SELECT text FROM chunks c WHERE c.file=f.id AND c.n=0)
                                    FROM files f WHERE {' AND '.join(cond)} ORDER BY f.mtime DESC LIMIT ?""", args + [max(k, 30)]).fetchall()
        out = [{"path": winpath(path), "name": name, "size": size, "date": dt.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M"),
                "kind": {1: "문서·코드", 2: "라이브러리", 3: "이름만"}.get(tr, ""), "snippet": (txt or "")[:160], "score": 0, "fid": fid}
               for path, name, size, mt, tr, fid, txt in rows]
        n = self.con.execute(f"SELECT count(*) FROM files f WHERE {' AND '.join(cond)}", args).fetchone()[0]
        return {"parsed": show(p), "results": out, "ms": int(1000 * (time.time() - t0)),
                "note": "" if out else "그 기간에 만든 파일이 없습니다.", "total": n}

    def load_jenc(self):
        """the JEPA-style small encoder (jtrain.py) and its one vector per file (table jvec)"""
        if getattr(self, "J", None) is not None:
            return
        import torch
        from transformers import AutoModel, AutoTokenizer
        d = os.environ.get("JENC_DIR", str(Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "jenc"))
        self.jtok = AutoTokenizer.from_pretrained(d); self.jm = AutoModel.from_pretrained(d).cuda().eval().half()
        rows = self.con.execute("SELECT file, emb FROM jvec").fetchall()
        self.jfids = [r[0] for r in rows]; self.J = np.stack([np.frombuffer(r[1], np.float16) for r in rows]).astype(np.float32)
        self.fc0 = dict(self.con.execute("SELECT file, id FROM chunks WHERE n=0").fetchall())

    def jq(self, t):
        import torch
        with torch.no_grad():
            b = self.jtok([t], padding=True, truncation=True, max_length=64, return_tensors="pt").to("cuda")
            h = self.jm(**b).last_hidden_state; m = b["attention_mask"].unsqueeze(-1).to(h.dtype)
            v = (h * m).sum(1) / m.sum(1)
            return torch.nn.functional.normalize(v.float(), dim=-1).cpu().numpy()[0]

    def load_facts(self):
        if getattr(self, "facts", None) is not None:
            return
        self.facts = {}
        for fid, path, kind, ps, d, amt, pe, st, mt, text in self.con.execute(
                """SELECT f.id, f.path, k.kind, k.partners, k.date, k.amount, k.people, k.status, f.mtime,
                          (SELECT group_concat(c.text, ' ') FROM chunks c WHERE c.file=f.id AND c.n>=0)
                   FROM files f JOIN facts k ON k.file=f.id"""):
            day = d or dt.datetime.fromtimestamp(mt).strftime("%Y-%m-%d")
            self.facts[fid] = dict(path=path, kind=kind, partners=json.loads(ps or "[]"), date=day, amount=amt,
                                   people=json.loads(pe or "[]"), status=st, text=text or "")

    def fact_filter(self, q):
        """(file ids that satisfy every condition the question states, the conditions) -- (None, {}) when it states none"""
        self.load_facts()
        cond = parse_cond(q)
        if PROFILE == "pc":   # on a researcher's PC the kind is guessed, not said: it misled the filter on dev (13 vs 18)
            cond.pop("kinds", None)
        if not cond:
            return None, {}
        strict = [fid for fid, f in self.facts.items() if fact_ok(f, cond, slack=10)]
        if strict:
            return set(strict), cond
        loose = [fid for fid, f in self.facts.items() if fact_ok(f, cond, slack=45, loose=True)]
        return set(loose), dict(cond, relaxed=True)

    def search(self, q, k=10, mode="base"):
        self.refresh()
        t0 = time.time(); p = parse(q); text = p["text"]
        pfut = None
        if PHOTO_URL and mode in ("xce", "fxce", "exce", "base") and self.load_photos():
            pw = EPI.action_dates(q)
            pq = re.sub(r"\s+", " ", q.replace(pw[2], " ")).strip() if pw else q
            pfut = POOL.submit(photo_text_vec, pq or q); pfut.window = pw
        if mode in ("xce", "fxce", "exce"):
            epi_prefetch(q)
        p["pkgs"] = {w for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", q.lower()) if w in self.pkgs}
        p["lib"] = p["lib"] or bool(p["pkgs"])
        eng =" ".join(dict.fromkeys(" ".join(v for k, v in GLOSS.items() if k in q).split()))
        if p.get("only_cond") and (p["since"] or p["exts"]):   # "6개월 전에 만든 파일": no topic, just the window, newest first
            return self.recent(p, k, t0)
        rer = {"x4b": "list", "x4bp": "1tok", "fx4b": "list", "pick1": "pick1", "fpick1": "pick1", "xce": "ce", "fxce": "ce",
               "jenc": None, "fjenc": None, "exce": "ce", "expick1": "pick1"}.get(mode)
        use4b = rer is not None
        if mode.endswith("jenc"):
            p["jenc"] = True
        cond = None
        if mode in ("facts", "fx4b", "fpick1", "fxce", "fjenc"):   # the 4B reads the question's conditions; code checks them against each file's fact card
            fids, cond = self.fact_filter(q)
            if cond:
                p["since"] = p["until"] = None   # the 4B's period replaces the rule's
            if fids is not None and not fids:
                r = self._search(q, p, text, eng, k, t0)
                r["note"] = "조건에 맞는 파일이 없습니다. 가장 가까운 파일을 보여 드립니다."
                r["cond"] = cond
                return r
            p["fids"] = fids
        # the 4B's English keywords run alongside the meaning search; they join the word search when ready
        late = POOL.submit(expand4b, q) if rer in ("list", "1tok") or mode.startswith("ex") else None
        r = self._search(q, p, text, eng, (len(LABELS) if rer == "pick1" else NCAND) if use4b else k, t0, late)
        if use4b and r["results"]:
            ids = [x["fid"] for x in r["results"]]; qm = ",".join("?" * len(ids))
            sm = dict(self.con.execute(f"SELECT file, summary FROM cards WHERE file IN ({qm})", ids).fetchall())
            hd = dict(self.con.execute(f"SELECT file, text FROM chunks WHERE n=0 AND file IN ({qm})", ids).fetchall())
            for x in r["results"]:
                x["summary"] = sm.get(x["fid"]) or ""; x["head"] = hd.get(x["fid"]) or ""
            if rer == "ce":
                sc = ce_scores(q, r["results"])
                d_ = os.environ.get("CE_DIR", str(Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "rank"))
                keep = sorted(range(len(sc)), key=lambda i: -sc[i]); none = max(sc) < CE_BIAS.get(d_, CE_NONE)
                for x, s_ in zip(r["results"], sc):
                    x["ce"] = round(s_, 4)
                r["top_ce"] = round(max(sc), 4)
            elif rer == "pick1":
                keep, none, pz = rerank_pick1(q, r["results"]); r["p_none"] = round(pz, 3)
            else:
                keep, none = (rerank1tok if rer == "1tok" else rerank4b)(q, r["results"])
            r["results"] = ([r["results"][i] for i in keep] + [x for i, x in enumerate(r["results"]) if i not in keep])[:k]
            for x in r["results"]:
                x.pop("head", None)
            r["note"] = "확실히 맞는 파일을 찾지 못했습니다. 가장 가까운 파일을 보여 드립니다." if none else ""
            if os.environ.get("VERIFY", "1") == "1" and not none:
                hd = {x["fid"]: t_ for x in r["results"][:3] for t_ in [self.full_text(x["fid"])]}
                miss = verify_none(q, r["results"], hd)
                if miss:   # repair first: look for the must-have itself across the whole index
                    found = self.find_specific(q, miss)
                    # the file holding the must-have must also fit the whole question about as well as what was shown
                    if found and self.last_repair_score >= REPAIR_RATIO * max(r.get("top_ce") or 0, 1e-6):
                        r["results"] = self.files_out(found) + [x for x in r["results"] if x["fid"] not in set(found)]
                        r["results"] = r["results"][:k]; r["repaired"] = miss[2]
                    else:
                        r["note"] = f"'{miss[2]}'이(가) 들어간 파일은 없습니다. 가장 가까운 파일을 보여 드립니다."
            if rer == "ce" and r["results"]:
                need = strong_needs(q)
                if need:
                    ok = [x for x in r["results"][:3] if all(meets(n, x, self.full_text(x["fid"])) for n in need)]
                    if ok:
                        r["results"] = [ok[0]] + [x for x in r["results"] if x is not ok[0]]
                    else:
                        r["hard_none"] = need[0][1]
            if rer == "ce" and r["results"] and not r.get("hard_none"):
                ecs, cq_model = epi_read(q)
                if ecs:
                    r["_cq"] = cq_model or EPI.content_part(q, ecs) or None   # what the file is about, history words removed
                pairs = [(c_, EPI.files_for(self.con, c_)) for c_ in ecs]
                pairs = [(c_, x_) for c_, x_ in pairs if x_ is not None]
                sets = [x_ for _, x_ in pairs]
                if ecs and sets:
                    allok = set.intersection(*sets)
                    if not allok and any(c_.get("_soft") for c_, _ in pairs):   # a soft date that fits nothing: ignore it, keep the rest
                        pairs = [(c_, x_) for c_, x_ in pairs if not c_.get("_soft")]
                        sets = [x_ for _, x_ in pairs]
                        ecs = [c_ for c_, _ in pairs] or ecs
                        allok = set.intersection(*sets) if sets else set()
                        if not sets:
                            ecs = []
                    r["episodic"] = [c_["text"] for c_ in ecs]
                    if allok:
                        cq = cq_model or EPI.content_part(q, ecs) or q
                        ids_ = list({x["fid"] for x in r["results"]} | set(list(allok)[:60]))
                        qm_ = ",".join("?" * len(ids_))
                        meta_ = {i: (n, pth, h or "") for i, n, pth, h in self.con.execute(
                            f"SELECT f.id, f.name, f.path, (SELECT text FROM chunks c WHERE c.file=f.id AND c.n=0) FROM files f WHERE f.id IN ({qm_})", ids_)}
                        pool_ = [{"fid": i, "name": v[0], "path": v[1], "head": v[2]} for i, v in meta_.items()]
                        sc_ = dict(zip([x["fid"] for x in pool_], ce_scores(cq, pool_)))
                        epi_best = max((sc_[f_] for f_ in allok if f_ in sc_), default=0.0)
                        agree = epi_best / max(max(sc_.values()), 1e-6)
                        r["agree"] = round(agree, 3)
                        r["content_best"] = round(max(sc_.values()), 4); r["epi_best"] = round(epi_best, 4)
                        order = sorted([f_ for f_ in allok if f_ in sc_], key=lambda f_: -sc_[f_])
                        rest_ = [x for x in r["results"] if x["fid"] not in allok]
                        r["results"] = (self.files_out(order[:k]) + rest_)[:k]
                        rank_ = sorted(sc_.values(), reverse=True)
                        K_ = int(os.environ.get("EPI_RANK", "0"))   # scale-free agreement: a history-consistent file among the content top K
                        r["epi_rank"] = 1 + sum(1 for v_ in rank_ if v_ > epi_best)
                        mc_ = MODEL_CONTENT.get(q)
                        if agree < EPI_AGREE and max(sc_.values()) >= EPI_G and mc_ and mc_ != cq and os.environ.get("EPI_SECOND", "1") == "1":
                            # second opinion: the model's own reading of the topic; the history still has to be among its best
                            sc2_ = dict(zip([x["fid"] for x in pool_], ce_scores(mc_, pool_)))
                            a2_ = max((sc2_[f_] for f_ in allok if f_ in sc2_), default=0.0) / max(max(sc2_.values()), 1e-6)
                            r["agree2"] = round(a2_, 3)
                            if a2_ >= float(os.environ.get("EPI_AGREE2", "0.3")):   # a second look needs a higher bar than the first
                                agree = max(agree, a2_ if a2_ >= EPI_AGREE else agree)
                        if agree >= EPI_AGREE or max(sc_.values()) < EPI_G or (K_ and r["epi_rank"] <= K_):   # weak content evidence cannot overrule the history
                            r["episodic_ok"] = True   # history and content point to the same file
                        else:
                            r["hard_none"] = "기억하신 이력(" + ", ".join(c_["text"] for c_ in ecs) + ")"
                            r["none_msg"] = ("기억하신 이력(" + ", ".join(c_["text"] for c_ in ecs) + ")에 맞는 파일은 있지만, 말씀하신 내용과는 달라 보입니다. "
                                             "이력에 맞는 파일을 먼저 보여 드립니다.")   # the history did match: say so, do not deny it
                    elif len(sets) >= 2:   # memory of one detail can be off: files meeting all cues but one
                        cnt = {}
                        for x_ in sets:
                            for f_ in x_:
                                cnt[f_] = cnt.get(f_, 0) + 1
                        near = {f_ for f_, n_ in cnt.items() if n_ >= len(sets) - 1}
                        if near:
                            have = [x for x in r["results"] if x["fid"] in near]
                            extra = [f_ for f_ in near if f_ not in {x["fid"] for x in have}][:max(0, k - len(have))]
                            r["results"] = (have + self.files_out(extra) + [x for x in r["results"] if x["fid"] not in near])[:k]
                            r["episodic_partial"] = True
                            if os.environ.get("EPI_NEAR_HONEST", "1") == "1":   # say which remembered detail no file has, then show the closest
                                f0 = r["results"][0]["fid"]
                                miss = [c_["text"] for c_, x_ in pairs if f0 not in x_]
                                if miss:
                                    r["hard_none"] = miss[0]
                        else:
                            r["hard_none"] = ecs[0]["text"]
                    elif ecs:   # (nothing left after dropping a soft date: no history judgement)
                        r["hard_none"] = ecs[0]["text"]
            if rer == "ce" and OLDQ.search(q):
                r["results"] = older_sibling_first(r["results"])
            if rer == "ce":
                lit = self.literal_files(p.get("text") or q)
                if lit:
                    have = [x for x in r["results"] if x["fid"] in lit]
                    extra = [f_ for f_ in lit if f_ not in {x["fid"] for x in have}][:max(0, k - len(have))]
                    rest_ = [x for x in r["results"] if x["fid"] not in lit]
                    r["results"] = (have + self.files_out(extra) + rest_)[:k]; r["literal"] = len(lit)
                ces = sorted((x.get("ce") or 0 for x in r["results"]), reverse=True) + [0, 0]
                unchecked = bool(r.get("_cq")) and not r.get("episodic_ok") and not r.get("hard_none") and not r.get("agree")
                gate_text = r["_cq"] if (unchecked and os.environ.get("GATE_CONTENT", "0") == "1") else (p.get("text") or q)
                nf = {"ce1": ces[0], "ce2": ces[1], "gap": ces[0] - ces[1], "sim1": self.sim_scale(float(r.get("top_sim") or 0)),
                      "miss": float(r.get("note", "").startswith("'")), "rep": float(bool(r.get("repaired"))),
                      "spec": float(bool(re.search(r"\d|[A-Za-z]{2,}", q))), "qlen": min(len(q), 80) / 80,
                      "cov": self.coverage(gate_text, r["results"][0]) if r["results"] else 0.0}
                r["nf"] = {k_: round(v_, 4) for k_, v_ in nf.items()}
                if NONE_M:
                    z = NONE_M["b"] + sum(NONE_M["w"][k_] * (nf[k_] - NONE_M["mu"][k_]) / NONE_M["sd"][k_] for k_ in NONE_M["w"])
                    pz = 1 / (1 + math.exp(-z)); r["p_none"] = round(pz, 3)
                    if pz >= NONE_T and r["results"] and self.has_all_words(gate_text, r["results"][0]):
                        pz = 0.0; r["evidence"] = "질문의 단어가 모두 1위 파일에 있음"
                    if r.get("episodic_ok") and not r.get("hard_none"):   # remembered history matches: the file exists
                        pz = 0.0; r["evidence"] = "기억 단서(" + ", ".join(r.get("episodic", [])) + ")가 모두 맞음"
                    if r.get("hard_none"):   # a condition the question insists on is in none of the top files
                        pz = 1.0; r["note"] = r.get("none_msg") or f"'{r['hard_none']}'에 맞는 파일은 찾지 못했습니다. 가장 가까운 파일을 보여 드립니다."
                    if unchecked and r.get("episodic"):   # be honest about what could not be checked; the person decides
                        r["info"] = ("기억하신 이력(" + ", ".join(r["episodic"]) + ")은 이 PC 기록으로 확인할 수 없어 내용으로만 찾았습니다. "
                                     "맞는 파일인지 확인해 주세요.")
                    if pz >= (float(os.environ.get("NONE_T_UNCHECKED", "0.97")) if unchecked else NONE_T):
                        if not r.get("note"):
                            r["note"] = "확실히 맞는 파일을 찾지 못했습니다. 가장 가까운 파일을 보여 드립니다."
                    else:
                        r["note"] = ""
            r["ms"] = int(1000 * (time.time() - t0))
        if pfut is not None:
            self.photo_merge(q, r, pfut, k, p)
        if not r["results"]:
            r["note"] = "조건에 맞는 파일이 없습니다."
        if cond is not None:
            r["cond"] = cond
        return r

    def _search(self, q, p, text, eng, k, t0, late=None):
        # conditions first: with a kind or a time, search only inside the files that satisfy them
        pool = None
        fids = p.get("fids")
        if p["exts"] or p["since"] or fids is not None:
            cond, args = ["f.tier IN (1,3)" if not p["lib"] else "f.tier>0"], []
            if fids is not None:
                cond.append(f"f.id IN ({','.join(str(int(i)) for i in fids)})")
            if p["exts"]:
                cond.append(f"f.ext IN ({','.join('?' * len(p['exts']))})"); args += sorted(p["exts"])
            if p["since"]:
                cond.append("f.mtime>=?"); args.append(p["since"].timestamp())
            if p["until"]:
                cond.append("f.mtime<?"); args.append(p["until"].timestamp())
            ok = {r[0] for r in self.con.execute(f"SELECT c.id FROM chunks c JOIN files f ON f.id=c.file WHERE {' AND '.join(cond)}", args)}
            pool = np.array([i for i, cid in enumerate(self.ids) if cid in ok], dtype=int)
            okf = {r[0] for r in self.con.execute(f"SELECT f.id FROM files f WHERE {' AND '.join(cond)}", args)}
        if p.get("jenc"):   # meaning by the small JEPA-style encoder: one vector per file, the whole question
            self.load_jenc()
            js = self.J @ self.jq(q)
            order = np.argsort(-js)
            dense = [self.fc0[self.jfids[i]] for i in order[:300]
                     if (pool is None or self.jfids[i] in okf) and self.jfids[i] in self.fc0]
            ok_i = [i for i in order if pool is None or self.jfids[i] in okf]
            top_sim = float(js[ok_i[0]]) if ok_i else -1.0
        else:
            # meaning (the question plus its English words)
            qv = self.qvec(text + (" " + eng if eng else ""))
            if pool is not None:
                s = np.full(len(self.ids), -1.0, dtype=np.float32)
                if len(pool):
                    s[pool] = self.M[pool] @ qv
            else:
                s = self.dense_all(qv)
            top = np.argsort(-s)[:300]
            dense = [int(self.ids[i]) for i in top if s[i] > -1]
            top_sim = float(s[top[0]]) if len(top) else 0.0
        # words: trigram terms (3+ chars) incl. the English ones, short Korean words by LIKE
        if late is not None:
            try:
                eng = (eng + " " + late.result(timeout=10)).strip()
            except Exception:
                pass
        words = [w for w in re.split(r"[\s/\\,.()\[\]\"']+", text + " " + eng) if w]
        lex = []
        long_ = [w for w in words if len(w) >= 3]
        if long_:
            fq = " OR ".join('"' + w.replace('"', "") + '"' for w in long_)
            try:
                lex = [r[0] for r in self.con.execute("SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT 300", (fq,))]
            except sqlite3.OperationalError:
                lex = []
        for w in [w for w in words if len(w) == 2][:3]:   # 2-char words: only file cards and names (a full scan took 400 ms)
            self.load_scan(); wl = w.lower()   # in memory: the same scans took 0.15 s each in SQLite (2026-11-05)
            lex += [cid for cid, t in self.card_txt if wl in t][:200]
            lex += [cid for cid, nm, tr in self.name_rows if tr in (1, 3) and wl in nm][:100]
        # names: files whose name holds two or more of the words come in as candidates by their first chunk
        named = []
        nw = [w.lower() for w in words if len(w) >= 3][:12]
        if nw:
            self.load_scan()
            named = [cid for cid, nm, tr in self.name_rows if tr != 2 and sum(1 for w in nw if w in nm) >= 2][:200]
        score = {}
        for lst in (dense, lex, named):
            for r, cid in enumerate(dict.fromkeys(lst)):
                score[cid] = score.get(cid, 0) + 1 / (60 + r)
        if not score:
            return {"parsed": show(p), "results": [], "ms": int(1000 * (time.time() - t0))}
        qm = ",".join("?" * len(score))
        rows = self.con.execute(f"""SELECT c.id, c.text, f.path, f.name, f.ext, f.size, f.mtime, f.tier, f.id FROM chunks c
                                    JOIN files f ON f.id=c.file WHERE c.id IN ({qm})""", list(score)).fetchall()
        best = {}
        for cid, ctext, path, name, ext, size, mtime, tr, fid in rows:
            if fids is not None and fid not in fids:
                continue
            if p["exts"] and ext not in p["exts"]:
                continue
            if p["since"] and mtime < p["since"].timestamp():
                continue
            if p["until"] and mtime >= p["until"].timestamp():
                continue
            sc = score[cid] * (0.5 if tr == 2 and not p["lib"] else 1.0)
            sc *= 0.6 if ARTIFACT.search(path) and not re.search(r"결과|로그|log|체크포인트", q) else 1.0
            sc *= 0.5 if tr == 1 and not (ctext or "").strip() else 1.0     # empty files match on their name only
            sc *= 1.3 if ext in p["soft"] else 1.0
            # backups: kept and read, but below the current file unless the question asks for an earlier one
            sc *= 0.6 if BAKNAME.search(name) and not OLDWORD.search(q) else 1.0
            sc *= 1.6 if p.get("pkgs") and any(f"site-packages/{g}/" in path.lower() for g in p["pkgs"]) else 1.0
            nl = name.lower()   # a name that carries the question's words (manual_rev5.md for "매뉴얼 개정판")
            hits = sum(1 for w in words if len(w) >= 2 and w.lower() in nl)
            sc *= 1 + 0.4 * min(hits, 3)
            if path not in best or sc > best[path][0]:
                best[path] = (sc, ctext, name, ext, size, mtime, tr, fid)
        res, seen = [], set()
        for path, v in sorted(best.items(), key=lambda x: -x[1][0]):   # copies (same name, same text) shown once
            key = (v[2], (v[1] or "")[:200])
            if key in seen:
                continue
            seen.add(key); res.append((path, v))
            if len(res) == k:
                break
        out = [{"path": winpath(path), "name": v[2], "size": v[4], "date": dt.datetime.fromtimestamp(v[5]).strftime("%Y-%m-%d %H:%M"),
                "kind": {1: "문서·코드", 2: "라이브러리", 3: "이름만"}.get(v[6], ""), "snippet": snip(v[1], words), "score": round(v[0] * 1000, 2),
                "fid": v[7]}
               for path, v in res]
        # say so when nothing is close in meaning and no words matched (the answer may be outside the indexed folders)
        weak = top_sim < (JWEAK if p.get("jenc") else WEAK) and not lex
        return {"parsed": show(p), "results": out, "ms": int(1000 * (time.time() - t0)), "top_sim": round(top_sim, 3),
                "note": "가까운 파일이 없습니다. 색인 범위 밖에 있을 수 있습니다." if weak else ""}


def specifics(q):
    """the parts of a question that must literally be in the right file: numbers with their unit or marker
    (6판, rev6, v3, 28.1%, 70B, 1080p, Q-2026-123, 3호기), English model/product names (Llama, GPT-4o)."""
    out = []
    for m in re.finditer(r"(?:rev\.?|v|ver\.?|버전\s*)(\d+)|(\d+)\s*(?:판|차|호기|번째)|(\d+(?:\.\d+)?)\s*(%|퍼센트|[bB]\b|[pP]\b|ms|초|명|개|톤|kg|KB|MB|GB)", q, re.I):
        n = m.group(1) or m.group(2) or m.group(3)
        out.append(("num", n, m.group(0)))
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9\-\.]*\d[A-Za-z0-9\-\.]*|[A-Z][a-z]{2,}[A-Za-z]*)(?![A-Za-z0-9])", q):
        w = m.group(1)
        if w.lower() not in ("pdf", "docx", "xlsx", "json", "jsonl", "md", "py", "txt", "csv", "html", "mp4", "png", "jpg"):
            out.append(("word", w.lower(), w))
    return out


def has_specific(sp, name, text):
    hay = (name + " " + text).lower()
    if sp[0] == "word":
        return sp[1] in hay
    n, raw = sp[1], sp[2].lower()
    if re.search(r"판|rev|ver|버전|^v", raw):   # a revision number counts only with a revision marker next to it
        if re.search(rf"[a-z_]{re.escape(n)}(?!\d)", name.lower()):   # rulec4.py, manual_rev5.md: the number glued to the name
            return True
        return re.search(rf"(rev\.?\s*|v|ver\.?\s*|버전\s*|제\s*|개정\s*){re.escape(n)}(?!\d)|(?<!\d){re.escape(n)}\s*(판|차\s*개정)", hay) is not None
    return re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", hay) is not None


def verify_none(q, results, heads, k=3):
    """the first must-have of the question that is in none of the top k files, or None"""
    sp = specifics(q)
    if not sp or not results:
        return None
    for s_ in sp:
        if not any(has_specific(s_, x["name"] + " " + x["path"], heads.get(x["fid"], "")) for x in results[:k]):
            return s_
    return None


REPAIR_RATIO = float(os.environ.get("REPAIR_RATIO", 0.5))   # set on the PC dev set (2026-11-06)
JWEAK = 0.3   # small-encoder cosine for "nothing here"; to be set on dev only
BAKNAME = re.compile(r"(\.(bak\w*|old|orig|backup|swp|tmp)|~)$", re.I)
OLDWORD = re.compile(r"백업|예전|이전|옛날|구버전|원본|처음|초안|전\s*(버전|판|것|거)|넣기\s*전|바뀌기\s*전|bak|old", re.I)
OLDQ = re.compile(OLDWORD.pattern + r"|첫\s*번째|원래|직전|들어가기\s*전|이전", re.I)
VERTAG = re.compile(r"(v\d+|ver\d+|old|new|final|최종|초안|수정|코멘트반영|\d{4}-?\d{2}-?\d{2}|\(\d+\)|copy|사본|bak)", re.I)


def family(x):
    """folder + name without version marks: ch3_v2_코멘트반영.tex and ch3_v3.tex are one family"""
    path = x["path"].replace("\\", "/"); name = re.sub(r"\.[^.]+$", "", path.rsplit("/", 1)[-1].lower())
    return path.rsplit("/", 1)[0], re.sub(r"[_\-\s.]+", "", VERTAG.sub("", name))


def older_sibling_first(res):
    """the question asks for an earlier version: among the top 3, the oldest file of #1's family goes first"""
    if len(res) < 2:
        return res
    d0, s0 = family(res[0])
    sib = [i for i, x in enumerate(res[:3]) if family(x)[0] == d0 and s0 and (family(x)[1] in s0 or s0 in family(x)[1])]
    if len(sib) < 2:
        return res
    j = min(sib, key=lambda i: res[i].get("date") or "")
    return [res[j]] + [x for i, x in enumerate(res) if i != j]


ORGQ = re.compile(r"([가-힣A-Za-z0-9]{2,}?(?:지방법원|고등법원|가정법원|노동위원회|테크|정보통신|정보|전자|모빌리티|고등학교|중학교|대학교|병원|은행|협회|재단))")
SURN = "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민나진지엄채원천방공현함변염여추도소석선설마길연위표명기반라왕금옥육인맹제모탁국어은편용예경봉"
PERSONQ = re.compile(rf"(?<![가-힣])([{SURN}][가-힣]{{2}})(?=\s*(?:씨|님|변호사|대표님|선생님|과장님|팀장님|부장님|교수님|원장님)|\s+[가-힣]+\s*사건|\s*사건)")
DOCTYPES = ["재심신청서", "항소장", "판결문", "결정문", "신청서", "위임장", "내용증명", "회의록", "계약서", "검토의견", "답변서", "준비서면",
            "소장", "지도안", "가정통신문", "결과보고서", "계획서", "견적서"]
CONTRA = re.compile(r"중간고사|기말고사|(?:중간|기말)(?=\s*(?:시험|평가|고사))|[1-4]\s*분기|[12]\s*학기|상반기|하반기")   # 중간 alone (중간보고) is not exam-exclusive


def strong_needs(q):
    """conditions a person does not paraphrase: (kind, text). Document type = the longest type word in the question."""
    need = [("org", m.group(1)) for m in ORGQ.finditer(q)] + [("person", m.group(1)) for m in PERSONQ.finditer(q)]
    t = max((d for d in DOCTYPES if d in q.replace(" ", "")), key=len, default=None)
    if t:
        need.append(("type", t))
    need += [("term", re.sub(r"\s+", "", m.group(0))) for m in CONTRA.finditer(q)]
    return need


def meets(n, x, text):
    kind, w = n
    if kind == "type":   # the document's own kind: its name or its opening lines
        hay = (x["name"] + (text or "")[:400]).replace(" ", "")
    else:
        hay = (x["name"] + " " + x["path"] + " " + (text or "")).replace(" ", "")
    return w.replace(" ", "") in hay


WEAK = 0.45   # bge-m3 cosine; set from the trial queries (2026-11-03), to be checked on the independent query set
LLM = os.environ.get("FINDR_LLM", "http://127.0.0.1:8090/v1/chat/completions")   # base 4B (no adapter), started by serve4b.sh


def chat(prompt, max_tokens):
    import urllib.request
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()   # Gemma 4 thinks by default: content came back empty
    req = urllib.request.Request(LLM, body, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"]


def expand4b(q):
    out = chat("파일 검색 질문입니다. 이 파일을 찾는 데 도움이 될 영어 키워드와 파일 이름 조각(예: ledger, rev, serve, gen, cmp)을 "
               "8개 이내로, 공백으로만 구분해 한 줄로 쓰세요. 설명 없이 단어만.\n질문: " + q, 40)
    return " ".join(w for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{1,30}", out)[:10])


from concurrent.futures import ThreadPoolExecutor  # noqa: E402
POOL = ThreadPoolExecutor(8)
PHOTO_URL = os.environ.get("PHOTO_URL", "")   # local photo model server (photo_srv.py); empty = photos by name only
PHOTO_WORD = re.compile(r"사진|이미지|그림|스크린샷|캡처|캡쳐|찍은|찍었|셀카|풍경|photo|jpe?g|png", re.I)


def photo_text_vec(q):
    import base64, urllib.request
    req = urllib.request.Request(PHOTO_URL.rstrip("/") + "/text", json.dumps({"q": q}).encode(), {"Content-Type": "application/json"})
    o = json.loads(urllib.request.urlopen(req, timeout=3).read())
    return np.frombuffer(base64.b64decode(o["vec"]), np.float16).astype(np.float32), float(o["scale"])
NCAND = int(os.environ.get("NCAND", 30))   # candidates reranked (15 for the CPU ranker, 2026-11-06)
CE_MAXLEN = int(os.environ.get("CE_MAXLEN", 384))   # ranker input length (256 for the CPU ranker)
_OLD_NCAND = 30   # candidates handed to the 4B (40 cost 1.1 s per rerank on 2026-11-04; set 1 had every found answer within 20)


def cand_line(x):   # short: name, folder, and the file card's summary when there is one (else the best piece)
    folder = "/".join(x["path"].replace("\\", "/").split("/")[-3:-1])
    return f"{x['name']} ({folder}, {x['date'][:10]}) — {(x.get('summary') or x['snippet'])[:140]}"


def rerank4b(q, res):
    lines = [f"[{i + 1}] {cand_line(x)}" for i, x in enumerate(res)]
    out = chat("사용자가 아래 질문으로 파일을 찾고 있습니다. 후보 중 질문이 찾는 파일로 보이는 것의 번호를 "
               "가장 맞는 것부터 최대 5개, 쉼표로만 구분해 쓰세요. 맞는 것이 하나도 없으면 '없음'이라고만 쓰세요.\n"
               f"질문: {q}\n\n후보:\n" + "\n".join(lines) + "\n\n답:", 30)
    if "없음" in out and not re.search(r"\d", out):
        return [], True
    keep = []
    for n in re.findall(r"\d+", out):
        i = int(n) - 1
        if 0 <= i < len(res) and i not in keep:
            keep.append(i)
    return keep, False


def p_yes(q, x):
    """one-token read (as J1): probability that this file is the one asked for; the 6 llama-server slots run these at once"""
    import urllib.request
    prompt = (f"파일 검색 질문: {q}\n후보 파일: {cand_line(x)}\n"
              "이 후보가 질문이 찾는 바로 그 파일입니까? 예 또는 아니오 한 단어로만 답하세요.")
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": 1, "temperature": 0, "logprobs": True,
                       "top_logprobs": 20, "chat_template_kwargs": {"enable_thinking": False}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(LLM, body, {"Content-Type": "application/json"}), timeout=60))
    import math
    yes = no = 0.0
    for t in r["choices"][0]["logprobs"]["content"][0]["top_logprobs"]:
        s = t["token"].strip()
        if s.startswith("예"):
            yes += math.exp(t["logprob"])
        elif s.startswith("아"):
            no += math.exp(t["logprob"])
    return yes / (yes + no) if yes + no > 0 else 0.0


P_KEEP = 0.5   # first guess; to be set on a calibration split, not on the test

# ---- fast rerankers (2026-11-05 study) ----
LABELS = "ABCDEFGHIJKLMNOPQRST"   # one token each; Z = none of them (Jev-style: one typed choice with probabilities)


def rerank_pick1(q, res):
    """Jev-style: the candidates and 'none' are a fixed set of choices; one forward pass, read the first token's probabilities"""
    import math
    import urllib.request
    res = res[:len(LABELS)]
    lines = [f"{LABELS[i]}. {cand_line(x)}" for i, x in enumerate(res)]
    prompt = ("사용자가 아래 질문으로 파일을 찾고 있습니다. 질문이 찾는 바로 그 파일의 기호 하나만 쓰세요. "
              "맞는 파일이 없으면 Z라고 쓰세요.\n" f"질문: {q}\n\n후보:\n" + "\n".join(lines) + "\nZ. 맞는 파일 없음\n\n답:")
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": 1, "temperature": 0, "logprobs": True,
                       "top_logprobs": 20, "chat_template_kwargs": {"enable_thinking": False}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(LLM, body, {"Content-Type": "application/json"}), timeout=60))
    pr = {}
    for t in r["choices"][0]["logprobs"]["content"][0]["top_logprobs"]:
        s = t["token"].strip().rstrip(".")
        if len(s) == 1 and (s in LABELS[:len(res)] or s == "Z"):
            pr[s] = pr.get(s, 0) + math.exp(t["logprob"])
    tot = sum(pr.values()) or 1.0
    order = sorted([i for i in range(len(res)) if LABELS[i] in pr], key=lambda i: -pr[LABELS[i]])
    for i in range(len(res)):
        res[i]["p"] = round(pr.get(LABELS[i], 0) / tot, 3)
    pz = pr.get("Z", 0) / tot
    return order, pz >= P_NONE1, pz


P_NONE1 = 0.5   # 'none' when its share is at least this; to be set on dev only
CE = {}
CE_BIAS = {}


def ce_scores(q, res, d=None, nosumm=None):
    """cross-encoder (bge-reranker-v2-m3, 568M): one batched pass over all candidates. d: model dir (cached per dir)"""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    d = d or os.environ.get("CE_DIR", str(Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "rank"))
    nosumm = bool(os.environ.get("CE_NOSUMM")) if nosumm is None else nosumm
    if d not in CE:
        tok = AutoTokenizer.from_pretrained(d)
        m = (AutoModelForSequenceClassification.from_pretrained(d, torch_dtype=torch.float16).cuda() if DEVICE == "cuda"
             else AutoModelForSequenceClassification.from_pretrained(d)).eval()
        CE[d] = (tok, m)
        nb = Path(d) / "none_bias.txt"   # a none-aware reranker carries its learned 'no answer' score
        if nb.exists():
            import math
            CE_BIAS[d] = 1 / (1 + math.exp(-float(nb.read_text().strip())))
    tok, m = CE[d]
    if nosumm:   # the stage-A reranker was trained on name | folder | opening text
        docs = [f"{x['name']} | {'/'.join(x['path'].replace(chr(92), '/').split('/')[-3:-1])} | {x.get('head', '')[:700]}" for x in res]
    else:
        docs = [f"{x['name']} | {'/'.join(x['path'].replace(chr(92), '/').split('/')[-3:-1])} | {(x.get('summary') or '')[:200]} {x.get('head', '')[:500]}"
                for x in res]
    with torch.no_grad():
        b = tok([q] * len(docs), docs, padding=True, truncation=True, max_length=CE_MAXLEN, return_tensors="pt").to(DEVICE)
        return torch.sigmoid(m(**b).logits.view(-1).float()).cpu().tolist()


NONE_M = json.load(open(os.environ["NONE_MODEL"])) if os.environ.get("NONE_MODEL") else None   # A2 none classifier (logistic, numpy)
NONE_T = float(os.environ.get("NONE_T", 0.5))   # decision threshold, set on PC dev only
CE_NONE = float(os.environ.get("CE_NONE", 0.02 if PROFILE == "pc" else 0.1))   # PC 0.02 set on PC dev 2026-11-06 (0.1 gave 6 false "none"); 'none' when the best candidate scores below this; to be set on dev only


def rerank1tok(q, res):
    ps = list(POOL.map(lambda x: p_yes(q, x), res))
    for x, pv in zip(res, ps):
        x["p"] = round(pv, 3)
    order = sorted(range(len(res)), key=lambda i: -ps[i])
    keep = [i for i in order if ps[i] >= P_KEEP]
    return (keep or order), not keep   # nothing likely: still shown by probability, with the "nothing here" note


def show(p):
    return {"찾을 내용": p["text"], "형식": sorted(p["exts"]) or "전체",
            "기간": f"{p['since']:%m-%d}~{p['until']:%m-%d}" if p["since"] and p["until"] else (f"{p['since']:%m-%d} 이후" if p["since"] else "전체"),
            "라이브러리 포함": p["lib"]}


def winpath(s):
    return re.sub(r"^/mnt/([a-z])/", lambda m: m.group(1).upper() + ":\\", s).replace("/", "\\") if s.startswith("/mnt/") else s


def snip(t, words, n=160):
    t = re.sub(r"\s+", " ", t or "")
    pos = min([i for i in (t.lower().find(w.lower()) for w in words) if i >= 0] or [0])
    a = max(0, pos - 40); return ("…" if a else "") + t[a:a + n] + ("…" if a + n < len(t) else "")


PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>findr 검색</title><style>
:root{--bg:#f7f6f2;--fg:#1d1c1a;--mut:#6b6862;--card:#fff;--line:#e3e0d9;--acc:#2f5d8a;--warn:#b5651d}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--fg:#ebe8e2;--mut:#a29e96;--card:#1f1e1c;--line:#34322e;--acc:#8db4dc;--warn:#e0a46a}}
body{font:15px/1.55 -apple-system,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif;max-width:860px;margin:0 auto;padding:18px 16px 40px;background:var(--bg);color:var(--fg)}
h1{font-size:20px;margin:4px 0 2px}.sub{color:var(--mut);font-size:13px;margin-bottom:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap}input{flex:1 1 260px;font-size:17px;padding:11px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
select,button{font-size:15px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}button{background:var(--acc);color:#fff;border:0;padding:10px 16px}
.q{color:var(--mut);font-size:13px;margin:12px 0 4px}.note{color:var(--warn);font-weight:600;margin:8px 0}
.r{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0}
.n{font-weight:700;word-break:break-all}.p{color:var(--acc);font-size:13px;word-break:break-all}.m{color:var(--mut);font-size:12.5px}.s{font-size:14px;margin-top:4px;white-space:pre-wrap;word-break:break-word}
.fb,.fb2{font-size:12px;padding:3px 9px;margin-left:6px;background:transparent;color:var(--acc);border:1px solid var(--line);border-radius:12px;cursor:pointer}
#t{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--acc);color:#fff;padding:8px 14px;border-radius:8px;opacity:0;transition:opacity .3s}
.pb{display:inline-block;color:#fff;font-size:12px;font-weight:600;border-radius:10px;padding:1px 8px;margin-right:8px;vertical-align:1px}
.rej{opacity:.45}
.ex{color:var(--mut);font-size:13px;margin-top:10px}.ex span{display:inline-block;border:1px solid var(--line);border-radius:14px;padding:2px 10px;margin:3px 4px 0 0;cursor:pointer;background:var(--card)}
</style>
<h1>findr · 내 PC 파일 찾기 <a href="/learned" style="font-size:13px;font-weight:400;margin-left:8px">findr가 배운 것</a></h1><div class="sub">자연어로 찾기 · 모든 처리는 이 PC 안에서 · 범위: C:\\workspace, WSL 실험 폴더, 구글 드라이브(받은 폴더)</div>
<div class="bar"><input id="q" placeholder="예: 어제 만든 임원 보고용 요약 pdf" autofocus>
<select id="mode"><option value="auto" selected>자동 (질문에 맞게: 업무 조건이면 이해 우선, 아니면 재정렬)</option><option value="xce">재정렬 (학습판)</option><option value="ng">이해 우선 (관계·버전·퇴사자·없음 판단)</option><option value="base">빠른 찾기 (글자·의미)</option><option value="x4b">4B 정밀 (느림, 4B 켜져 있을 때)</option><option value="pick1">Jev식 고르기 (4B 켜져 있을 때)</option></select>
<button id="go">찾기</button></div>
<div class="ex">예시: <span>어제 만든 임원 보고용 현장 AI 실험 요약 pdf</span><span>음성 받아쓰기 설치 스크립트</span><span>쓰레기 소각장 운영 매뉴얼</span><span>규칙 컴파일러 v4</span></div>
<div id="o"></div><div id="t"></div><script>
const q=document.getElementById('q'),o=document.getElementById('o'),md=document.getElementById('mode');
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const fmt=n=>n>1e6?(n/1e6).toFixed(1)+'MB':n>1e3?(n/1e3).toFixed(0)+'KB':n+'B';
async function go(){if(!q.value.trim())return;if(last&&last.q0&&last.q0!==q.value)tried.push(last.q0);o.innerHTML='<div class="q">찾는 중…</div>';
 try{const r=await (await fetch('/api?k=30&q='+encodeURIComponent(q.value)+'&mode='+md.value)).json();last=r;last.q0=q.value;last.first=(r.results||[]).map(x=>x.path);last.t0=Date.now();shownN=10;
 const name=p=>p.split('/').pop().split(String.fromCharCode(92)).pop();
 o.innerHTML='<div class="q">'+Object.entries(r.parsed||{}).map(([k,v])=>esc(k)+': '+esc(String(v))).join(' · ')+' · '+r.ms+'ms</div>'+
 (r.note?'<div class="note">'+esc(r.note)+'</div>':'')+((r.why&&r.why.length)?'<div class="q">근거: '+r.why.map(esc).join(' → ')+'</div>':'')+'<div class="q"><button class="fb2" id="none">✘ 찾는 파일이 여기 없음</button></div>'+
 '<div class="q" id="rf"></div><div id="list"></div>';render();refine();}
 catch(e){o.innerHTML='<div class="note">오류: 이 방식에 필요한 모델이 꺼져 있을 수 있습니다. 기본으로 다시 시도해 보세요.</div>';}}
let tried=[];async function refine(){const qq=q.value,m=last&&last.route;if(!m||!last.results||last.results.length<2)return;
 const st=document.getElementById('rf');if(st)st.textContent='후보를 열어 보고 비교하는 중…';
 try{const r=await (await fetch('/refine?'+new URLSearchParams({q:qq,mode:m}))).json();if(q.value!==qq)return;
  if(r.results){last.results=r.results;render();}if(st)st.textContent=r.changed?'열어 보고 순서를 다듬었습니다.':'열어 보고 확인했습니다.';}catch(e){if(st)st.textContent='';}}
let shownN=10;const name=p=>p.split('/').pop().split(String.fromCharCode(92)).pop();
const odds=x=>x.prob==null?'':'<span class="pb" style="background:'+(x.prob>=.7?'var(--acc)':x.prob>=.3?'var(--warn)':'var(--mut)')+'">가능성 '+Math.round(x.prob*100)+'%</span>';
function render(){const rs=(last&&last.results)||[],L=document.getElementById('list');if(!L)return;
 L.innerHTML=(rs.slice(0,shownN).map((x,i)=>'<div class="r'+(x.no?' rej':'')+'"><div class="n">'+odds(x)+(i+1)+'. '+esc(name(x.path))+'</div><div class="p">'+esc(x.path)+'</div><div class="m">'+x.date+' · '+fmt(x.size)+' · '+esc(x.kind)+
 ' <button class="fb" data-i="'+i+'">✔ 이 파일이 맞음</button><button class="fb rj" data-i="'+i+'">✘ 아님</button></div>'+((x.why&&x.why.length)?'<div class="m">'+x.why.map(esc).join(' · ')+'</div>':'')+'<div class="s">'+esc(x.snippet)+'</div></div>').join('')||'<div class="q">결과 없음</div>')+
 (rs.length>shownN?'<div class="q"><button class="fb2" id="more">다음 후보 '+Math.min(10,rs.length-shownN)+'개 더 보기 ('+shownN+'/'+rs.length+')</button></div>':'');}
document.getElementById('go').onclick=go;
let last=null;const send=(v,p,k,pr)=>fetch('/fb?'+new URLSearchParams({q:q.value,mode:md.value,path:p||'',rank:k||'',verdict:v,prob:pr==null?'':pr,
 first_rank:(last&&last.first&&p)?(last.first.indexOf(p)+1):'',secs:last&&last.t0?Math.round((Date.now()-last.t0)/1000):'',page:Math.ceil(shownN/10),
 shown:((last&&last.results)||[]).slice(0,shownN).map(x=>x.path).join(String.fromCharCode(10)),tried:tried.join(String.fromCharCode(10))})).then(()=>{if(v==='hit')tried=[];toast(v==='none'?'기록: 여기 없음':v==='reject'?'기록: '+k+'위는 아님':'기록: '+k+'위가 정답');});
const toast=t=>{let d=document.getElementById('t');d.textContent=t;d.style.opacity=1;setTimeout(()=>d.style.opacity=0,1500)};
o.addEventListener('click',e=>{const b=e.target.closest('.fb');if(b&&last){const i=+b.dataset.i,x=last.results[i];
  if(b.classList.contains('rj')){x.no=true;send('reject',x.path,i+1,x.prob);render();}else send('hit',x.path,i+1,x.prob);}
 if(e.target.id==='more'){shownN+=10;render();}
 if(e.target.id==='none')send('none');});q.addEventListener('keydown',e=>{if(e.key==='Enter')go();});
document.querySelectorAll('.ex span').forEach(s=>s.onclick=()=>{q.value=s.textContent;go();});
</script></html>"""


PASSWORD = ""
import math  # noqa: E402
import threading  # noqa: E402
import traceback  # noqa: E402
SEARCH_LOCK = threading.Lock()   # the finder shares one DB connection, GPU models and per-search state: one search at a time
MODES = {"auto", "ng", "xce", "base"}   # what the web page may ask for; 4B modes stay CLI-only
MAX_Q = 300
_CALIB = None
_LAST = {}          # (q, mode) -> last results, for /refine
_PM = None          # personal memory (stage 5), this PC only


def personal():
    global _PM
    if _PM is None and os.environ.get("PERSONAL", "0") == "1":
        import personal as PMOD
        _PM = PMOD.Memory(str(Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "personal_memory.jsonl"))
    return _PM


def add_probs(res):
    """calibrated chance that each shown candidate is the wanted file (stage 1, fit on tuning sets), plus short reasons.
    The person makes the final call, so 'not here' is never final: candidates stay visible with their odds."""
    global _CALIB
    rs = res.get("results") or []
    if not rs:
        return res
    if _CALIB is None:
        try:
            _CALIB = json.load(open(os.environ.get("CALIB_P", str(Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "gate" / "calibP.json"))))["w"]
        except Exception:
            _CALIB = []
    ce = [x.get("ce") if x.get("ce") is not None else -1.0 for x in rs]
    if not _CALIB or max(ce) == -1.0:
        return res
    top = max(ce); second = sorted(ce, reverse=True)[1] if len(ce) > 1 else -1.0
    pn = res.get("p_none") if res.get("p_none") is not None else 0.5
    import math
    for i, (x, c) in enumerate(zip(rs, ce)):
        pos = i + 1
        f = [1.0, c, c - top, math.log(pos), 1.0 if pos == 1 else 0.0, (top - second) if pos == 1 else 0.0, pn,
             1.0 if res.get("note") else 0.0, 1.0 if res.get("episodic_ok") and pos == 1 else 0.0]
        z = sum(a * b for a, b in zip(_CALIB, f))
        x["prob"] = round(1 / (1 + math.exp(-z)), 3)
        why = []
        if pos == 1 and res.get("episodic_ok") and res.get("episodic"):
            why.append("기억하신 이력과 맞음: " + ", ".join(res["episodic"]))
        if c > 0.5:
            why.append("내용이 질문과 잘 맞음")
        elif c > 0.1:
            why.append("내용이 일부 맞음")
        if x.get("photo_p") is not None:   # a photo: its own odds from the photo model
            x["prob"] = x["photo_p"]; why = ["사진 내용이 질문과 맞음" if x["photo_p"] >= 0.5 else "사진 내용이 일부 맞음"]
        x["why"] = why
    if res.get("note") and rs:   # soften: keep the reason, but say the list is ordered by likelihood
        res["note"] = res["note"].replace("가장 가까운 파일을 보여 드립니다.", "").strip() + " 가능성이 높은 순서로 보여 드립니다. 맞는 파일을 골라 주세요."
    return res
FAILS = {}   # client -> [count, first failure time]; slows password guessing through the tunnel
SEC_HEADERS = [("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
               ("Cache-Control", "no-store"),
               ("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                          "img-src 'self' data:; connect-src 'self' ipc: http://ipc.localhost; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")]
ROUTE_NG = re.compile(r"그거\s*말고|그\s*전\s*버전|바로\s*전|맨\s*처음|퇴사|그만둔|인계|넘겨받|후임|회의록|회의한|협의|견적|발주|명세서|계약|거래처|업체|만원|억원?|원짜리|점검일지|출장\s*보고|제안서|클레임|교육\s*보고|NDA|비밀유지|인계서|작업일보|배차표|장애보고")


EXPLORE_CORR = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "explorer_corrections_live.json"   # kept on this PC only
EXPLORE_CACHE = {}


def explore_corr():
    try:
        c = json.loads(EXPLORE_CORR.read_text(encoding="utf-8"))
    except Exception:
        c = None
    return c if isinstance(c, dict) and "move" in c else {"move": {}, "rename": {}}


def explore_json(f):
    """the explorer's views of this index; built on CPU from stored vectors, cached for 10 minutes"""
    c = EXPLORE_CACHE.get("live")
    if not c or time.time() - c[0] > 600:
        import explorer
        db = f.con.execute("PRAGMA database_list").fetchone()[2]
        v = explorer.build(db, None, explore_corr())
        c = EXPLORE_CACHE["live"] = (time.time(), json.dumps({**v, "src": "live", "srcs": []}, ensure_ascii=False).encode())
    return c[1]


def serve(f, port):
    class H(BaseHTTPRequestHandler):
        def reply(self, code, body, ct):
            self.send_response(code); self.send_header("Content-Type", ct)
            for k_, v_ in SEC_HEADERS:
                self.send_header(k_, v_)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path == "/healthz":   # for the watchdog: says nothing but "ok"
                return self.reply(200, b"ok", "text/plain")
            if urlparse(self.path).path == "/auth":   # the desktop app opens the page once with its key; a cookie keeps it signed in
                import hmac
                t_ = parse_qs(urlparse(self.path).query).get("t", [""])[0]
                if PASSWORD and hmac.compare_digest(t_.encode(), PASSWORD.encode()):
                    from urllib.parse import quote as _q
                    q0 = parse_qs(urlparse(self.path).query).get("q", [""])[0][:200]
                    to = "/" + ("?q=" + _q(q0) if q0 else "")
                    # a page of this site moves on by itself: the strict cookie is sent from then on (a redirect from the app would not carry it)
                    body = f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={to}"><title>findr</title>'.encode()
                    self.send_response(200); self.send_header("Set-Cookie", f"findr_t={PASSWORD}; Path=/; HttpOnly; SameSite=Strict")
                    self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                self.send_response(401); self.end_headers(); return
            if not self.authed():
                return
            try:
                self.route()
            except Exception:
                traceback.print_exc()
                self.reply(500, json.dumps({"error": "검색 중 오류가 났습니다. 잠시 후 다시 시도해 주세요.", "results": []}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")

        def route(self):
            u = urlparse(self.path)
            if u.path == "/fb":   # user's verdict on a search: kept only on this PC, becomes the gold set
                qs = parse_qs(u.query)
                rec = {k: qs.get(k, [""])[0][:MAX_Q] for k in ("q", "mode", "path", "rank", "verdict", "prob", "first_rank", "secs", "page")}
                rec["shown"] = [x[:MAX_Q] for x in qs.get("shown", [""])[0].split(chr(10)) if x][:30]   # what the person saw, in order
                rec["tried"] = [x[:MAX_Q] for x in qs.get("tried", [""])[0].split(chr(10)) if x][:8]   # earlier requests in this session
                fbp = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "feedback.jsonl"
                if rec["verdict"] not in ("hit", "none", "reject") or (fbp.exists() and fbp.stat().st_size > 5_000_000):
                    return self.reply(400, b'{"ok": false}', "application/json")
                rec["time"] = dt.datetime.now().isoformat(timespec="seconds")
                with open(Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "feedback.jsonl", "a", encoding="utf-8") as fo:
                    fo.write(json.dumps(rec, ensure_ascii=False) + chr(10))
                if personal() is not None and rec["verdict"] in ("hit", "reject") and rec["path"]:
                    with SEARCH_LOCK:
                        row = f.con.execute("SELECT id FROM files WHERE path=? OR path=?", (rec["path"], rec["path"].replace(chr(92), "/"))).fetchone()
                    if row:
                        if rec["verdict"] == "hit":
                            for q0 in dict.fromkeys([rec["q"]] + rec["tried"]):
                                personal().add(q0, row[0])
                        else:
                            personal().add(rec["q"], None, [row[0]])
                return self.reply(200, b'{"ok": true}', "application/json")
            if u.path in ("/learned", "/learned/forget"):   # what findr learned about this person (this PC only)
                M = personal()
                if M is None:
                    return self.reply(200, "<meta charset=utf-8><p>나에게 맞추는 학습이 꺼져 있습니다.</p>".encode(), "text/html; charset=utf-8")
                if u.path == "/learned/forget":
                    i = parse_qs(u.query).get("i", [""])[0]
                    M.forget(None if i == "all" else int(i) if i.isdigit() else -1)
                with SEARCH_LOCK:
                    ids = {r["chosen"] for r in M.rows if r.get("chosen")} | {x for r in M.rows for x in r.get("rejected", [])}
                    names = {i_: n for i_, n in f.con.execute(f"SELECT id, name FROM files WHERE id IN ({','.join('?' * len(ids))})", list(ids))} if ids else {}
                e = html.escape
                rows = "".join(f"<tr><td>{e(r['q'])}</td><td>{'→ ' + e(names.get(r['chosen'], '(지워진 파일)')) if r.get('chosen') else ''}"
                               f"{'아님: ' + ', '.join(e(names.get(x, '?')) for x in r.get('rejected', [])) if r.get('rejected') else ''}</td>"
                               f"<td><a href='/learned/forget?i={j}'>잊기</a></td></tr>" for j, r in enumerate(M.rows))
                page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>findr가 배운 것</title>"
                        "<style>body{font:15px/1.5 sans-serif;max-width:860px;margin:0 auto;padding:16px}td{border-bottom:1px solid #ddd;padding:6px}</style>"
                        "<h2>findr가 배운 것</h2><p>고르신 파일과 그때의 요청을 이 PC 안에만 기억합니다. 비슷하게 물으시면 그 파일을 먼저 보여 드립니다. "
                        "검색 엔진이 확실한 답을 찾았을 때는 기억보다 요청을 따릅니다.</p>"
                        f"<p>{len(M.rows)}개 · <a href='/learned/forget?i=all' onclick=\"return confirm('배운 것을 모두 잊을까요?')\">모두 잊기</a> · <a href='/'>검색으로</a></p>"
                        f"<table>{rows or '<tr><td>아직 배운 것이 없습니다.</td></tr>'}</table>")
                return self.reply(200, page.encode(), "text/html; charset=utf-8")
            if u.path == "/refine":
                qs = parse_qs(u.query)
                q_ = qs.get("q", [""])[0].strip()[:MAX_Q]; mode = qs.get("mode", ["xce"])[0]
                res = _LAST.get((q_, mode))
                if not res or len(res.get("results") or []) < 2:
                    return self.reply(200, b'{"changed": false}', "application/json; charset=utf-8")
                import explore as XP
                with SEARCH_LOCK:
                    prompt, n = XP.v2_prompt(f, q_, res, today_now())
                out = XP._chat(XP.SYS2, prompt, 80)
                new = XP.v2_pick(res, out, n)
                tr = (new.get("trail") or [{}])[0]
                for c in new["results"]:
                    if c["fid"] in {new["results"][i]["fid"] for i in range(len(tr.get("picked") or []))}:
                        c["why"] = (c.get("why") or []) + ["열어 보고 비교함" + (": " + tr["why"] if tr.get("why") else "")]
                new["changed"] = bool(tr.get("picked")) and new["results"][0]["fid"] != res["results"][0]["fid"]
                _LAST[(q_, mode)] = new
                body = json.dumps({"changed": new["changed"], "results": new["results"], "trail": new.get("trail")}, ensure_ascii=False, default=str).encode()
                return self.reply(200, body, "application/json; charset=utf-8")
            if u.path == "/api":
                qs = parse_qs(u.query)
                mode = qs.get("mode", ["auto"])[0]
                q_ = qs.get("q", [""])[0].strip()[:MAX_Q]
                prev_ = (qs.get("prev", [None])[0] or None) and qs["prev"][0][:MAX_Q]
                if mode not in MODES:
                    mode = "auto"
                try:
                    k = max(1, min(50, int(qs.get("k", ["10"])[0] or 10)))
                except ValueError:
                    k = 10
                if not q_:
                    return self.reply(200, b'{"results": [], "note": ""}', "application/json; charset=utf-8")
                if mode == "auto":   # route by what the question asks: business conditions -> understanding-first, else reranker
                    mode = "ng" if ((ROUTE_NG.search(q_) or prev_) and _has_facts(f)) else "xce"   # understanding-first needs work facts in the index
                with SEARCH_LOCK:
                  global EVAL_NOW
                  try:
                    pin = self.headers.get("X-Findr-Today")
                    EVAL_NOW = dt.datetime.fromisoformat(pin) if pin else None
                    EPI.TODAY = EVAL_NOW
                  except ValueError:
                    EVAL_NOW = None
                  try:
                    if mode == "ng":   # understanding-first: plan -> execute over facts and links
                        if getattr(f, "ng", None) is None:
                            from ng import NG
                            f.ng = NG(f)
                        res = f.ng.search(q_, prev=prev_, k=k)
                    else:
                        res = f.search(q_, mode=mode, k=k)
                  finally:
                    EVAL_NOW = None; EPI.TODAY = None
                res["route"] = mode
                if personal() is not None and res.get("results"):
                    with SEARCH_LOCK:
                        res = personal().apply(f, q_, res)
                res = add_probs(res)
                if len(_LAST) > 50:
                    _LAST.clear()
                _LAST[(q_, mode)] = res
                body = json.dumps(res, ensure_ascii=False, default=str).encode()
                ct = "application/json; charset=utf-8"
            elif u.path == "/explore":
                body = (Path(__file__).with_name("explore.html")).read_bytes(); ct = "text/html; charset=utf-8"
            elif u.path == "/explore.json":
                body = explore_json(f); ct = "application/json; charset=utf-8"
            elif u.path == "/summary.json":   # one-page project summary by the local model, numbers checked against the sources
                import projsum
                d = parse_qs(u.query).get("dir", [""])[0]
                pr = next((p for p in json.loads(explore_json(f))["projects"] if p["dir"] == d), None)
                if pr is None:
                    return self.reply(404, b"{}", "application/json")
                db = f.con.execute("PRAGMA database_list").fetchone()[2]
                body = json.dumps(projsum.summarize(db, pr), ensure_ascii=False).encode(); ct = "application/json; charset=utf-8"
            elif u.path == "/stats":
                with SEARCH_LOCK:
                    body = json.dumps(f.stats()).encode(); ct = "application/json"
            else:
                pg = Path(__file__).with_name("page.html")
                body = (pg.read_text(encoding="utf-8") if pg.exists() else PAGE).encode(); ct = "text/html; charset=utf-8"
            self.reply(200, body, ct)

        def do_POST(self):
            if not self.authed():
                return
            if self.headers.get("X-Findr") != "1" or urlparse(self.path).path != "/correct":
                return self.reply(403, b"{}", "application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                return self.reply(413, b"{}", "application/json")
            try:
                r = json.loads(self.rfile.read(n)); op = r["op"]
                assert op in ("move", "rename", "reset")
                c = explore_corr()
                if op == "move":
                    c["move"][str(int(r["fid"]))] = str(r["to"])[:500]
                elif op == "rename":
                    c["rename"][str(r["dir"])[:500]] = str(r["name"]).strip()[:60]
                else:
                    c = {"move": {}, "rename": {}}
                c["rename"] = {d: v for d, v in c["rename"].items() if v}
                EXPLORE_CORR.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8"); EXPLORE_CACHE.clear()
                return self.reply(200, b'{"ok": true}', "application/json")
            except Exception:
                return self.reply(400, b'{"ok": false}', "application/json")

        def authed(self):
            """HTTP Basic (any user name): a tunnel makes this page reachable from outside, so it never runs without one"""
            import base64
            import hmac
            who = self.headers.get("CF-Connecting-IP") or self.client_address[0]
            n_, t_ = FAILS.get(who, [0, time.time()])
            if time.time() - t_ > 600:
                n_, t_ = 0, time.time()
            if n_ >= 10:   # ten wrong passwords in ten minutes: refuse that client for the rest of the window
                self.send_response(429); self.end_headers(); return False
            ck_ = re.search(r"(?:^|;\s*)findr_t=([^;]+)", self.headers.get("Cookie", ""))
            if PASSWORD and ck_ and hmac.compare_digest(ck_.group(1).encode(), PASSWORD.encode()):
                return True
            got = self.headers.get("Authorization", "")
            try:
                pw = base64.b64decode(got.split(" ", 1)[1]).decode().split(":", 1)[1] if got.startswith("Basic ") else ""
            except Exception:
                pw = ""
            if PASSWORD and hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                return True
            if got:
                FAILS[who] = [n_ + 1, t_]
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="findr", charset="UTF-8"'); self.end_headers()
            return False

        def log_message(self, *a):
            pass
    global PASSWORD
    pf = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / ".password"; pf.parent.mkdir(parents=True, exist_ok=True)
    if not pf.exists():
        import secrets
        pf.write_text(secrets.token_urlsafe(12)); pf.chmod(0o600)
    PASSWORD = pf.read_text().strip()
    print(f"FINDR_SERVE http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--db", required=True); ap.add_argument("cmd"); ap.add_argument("query", nargs="*")
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args(); f = Finder(a.db)
    if a.cmd == "serve":
        serve(f, a.port)
    else:
        for q in a.query:
            r = f.search(q); print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
