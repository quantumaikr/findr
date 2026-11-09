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
import unicodedata
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
    soft_time = False
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
    yr = lambda n: (day0.replace(year=day0.year - n, month=1, day=1), day0.replace(year=day0.year - n + 1, month=1, day=1))
    # calendar years as people say them (TIME_WORDS=1): "작년에 만든 발표자료" read no period at all (real PCs, 11/9)
    years = [(r"재작년", lambda m: yr(2)), (r"작년|지난\s*해|지난해", lambda m: yr(1)), (r"올해|금년|이번\s*해", lambda m: (yr(0)[0], None))] \
        if os.environ.get("TIME_WORDS", "1") == "1" else []
    for pat, f in years + [(r"(\d+)\s*(개월|달)\s*(이내|안에|동안|사이|간)", lambda m: (day0 - D(30.44 * int(m.group(1))), None)),
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
            since, until = f(m); rest = rest.replace(m.group(0), " ")
            soft_time = any(pat == y[0] for y in years)   # 작년, 재작년: people misplace a year; a fallback keeps the answer
            break
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
    return {"exts": exts, "soft": soft - exts, "since": since, "until": until, "lib": lib, "text": rest or q, "only_cond": not rest,
            "soft_time": soft_time}


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


_QT = [0.0]   # when the current question started (the whole-question reranker budget on a CPU)


class _OrtRanker:
    """the reranker as int8 ONNX on the CPU (about 2.7x faster than PyTorch fp32, 543 MB instead of 2.2 GB);
    called like the PyTorch model, so the scoring code is the same"""
    def __init__(self, path):
        import onnxruntime as ort
        import torch
        so = ort.SessionOptions(); so.intra_op_num_threads = int(os.environ.get("OMP_NUM_THREADS") or max(1, (os.cpu_count() or 2) // 2))
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.s = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"]); self.device = torch.device("cpu")

    def eval(self):
        return self

    def __call__(self, input_ids=None, attention_mask=None, **_):
        import torch
        from types import SimpleNamespace
        out = self.s.run(None, {"input_ids": input_ids.cpu().numpy().astype("int64"), "attention_mask": attention_mask.cpu().numpy().astype("int64")})[0]
        return SimpleNamespace(logits=torch.from_numpy(out))


def _cpu_ranker_for(d, load_fp32):
    """int8 ONNX when it is there and onnxruntime imports (CE_ONNX=0 turns it off), else the fp32 model"""
    ox = Path(d) / "model_int8.onnx"
    if ox.exists() and os.environ.get("CE_ONNX", "1") != "0":
        try:
            m = _OrtRanker(ox)
            print(f"FINDR_RERANKER cpu int8-onnx ({ox.stat().st_size // 2**20} MB)", flush=True)
            return m
        except Exception as e:
            print(f"FINDR_RERANKER cpu fp32 (int8 model could not load: {type(e).__name__})", flush=True)
    else:
        print("FINDR_RERANKER cpu fp32" + ("" if ox.exists() else " (no int8 model in the model folder)"), flush=True)
    return _cpu_ranker(load_fp32())


def _cpu_ranker(m):
    """on a PC without a GPU the reranker can run with 8-bit weights (CE_INT8=1): several times faster on laptop CPUs"""
    if os.environ.get("CE_INT8", "0") == "1":
        import torch
        m = torch.quantization.quantize_dynamic(m, {torch.nn.Linear}, dtype=torch.qint8, inplace=True)   # no second copy in memory
    return m


def _has_facts(f):
    """the understanding-first route reads work facts; an index built without them uses the reranker route"""
    # "no facts" is asked again every 5 minutes: a server started while the first index was still running kept 이해 우선 off
    # until a restart
    if getattr(f, "_facts_ok", None) is None or (not f._facts_ok and time.time() - getattr(f, "_facts_t", 0) > 300):
        f._facts_t = time.time()
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


_LLM_DOWN = [0.0]   # until when the planner server is taken as down


def chat_json(prompt, max_tokens):
    """the condition / plan reader (FINDR_LLM). Callers fall back to their rules on any error. Not set: no call at all.
    Down: no new try for a minute. On Windows a refused localhost connection takes ~2 s (it retries), and the installed app
    has no such server, so every understanding-first question waited 2 s for nothing."""
    import urllib.request
    if not LLM or time.time() < _LLM_DOWN[0]:
        raise ConnectionError("no condition reader")
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0,
                       "response_format": {"type": "json_object"}, "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(LLM, body, {"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"]
    except OSError:   # refused, unreachable, timed out (URLError is an OSError)
        _LLM_DOWN[0] = time.time() + 60
        raise


class Finder:
    def __init__(self, db, model="bge-m3"):
        self.db_uri = Path(db).expanduser().resolve().as_uri() + "?mode=ro"   # the reload thread reads on its own connection
        self.con = sqlite3.connect(Path(db).expanduser(), check_same_thread=False)
        import unicodedata as _ud
        self.con.create_function("nfc", 1, lambda s: _ud.normalize("NFC", s) if s else s, deterministic=True)
        self.con.execute("CREATE TABLE IF NOT EXISTS cards(file INTEGER PRIMARY KEY, outline TEXT, summary TEXT, done INT DEFAULT 0)")
        self._dv = self.con.execute("PRAGMA data_version").fetchone()[0]   # read before loading: a commit during the load is still seen (#21)
        rows = self.con.execute("SELECT c.id, c.emb FROM chunks c WHERE c.emb IS NOT NULL").fetchall()
        self.ids = np.array([r[0] for r in rows], dtype=np.int64)   # a fresh index may have no vectors yet (words only)
        self.M = np.stack([np.frombuffer(r[1], np.float16) for r in rows]).astype(np.float32) if rows else np.zeros((0, 1024), np.float32)
        Enc = None   # the app builds EmbeddingGemma-2 or e5-small indexes only
        from enc_eg2 import EG2, encoder_of
        self.encoder = encoder_of(self.con)   # the index says which meaning encoder built its vectors
        from enc_eg2 import make_encoder
        if self.encoder not in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta"):   # a first index still being built: nothing recorded yet
            self.encoder = os.environ.get("FINDR_ENCODER") or "e5-small"
        self.enc = make_encoder(self.encoder, DEVICE, max_len=128)
        if not len(self.ids):   # no vectors yet: an empty matrix of this encoder's width (words and names still answer)
            self.M = np.zeros((0, len(self.qvec("준비"))), np.float32)
        # installed package names: a question that names one (peft, trl, bitsandbytes) means the library
        self.pkgs = {m.group(1).lower() for (p,) in self.con.execute("SELECT path FROM files WHERE tier=2")
                     for m in [re.search(r"site-packages/([A-Za-z0-9_]+)/", p)] if m and len(m.group(1)) >= 3}
        self.pkgs -= {"lora", "gemma", "comfyui", "prompt", "test", "tests", "data", "model", "models", "utils", "docs", "json", "html"}
        print(f"FINDR_READY chunks {len(self.ids)} packages {len(self.pkgs)}", flush=True)

    def refresh(self, every=60):
        """an index that grew while the page was open (background indexing): pick up new vectors without a restart.
        The question only looks whether the index changed; reading the vectors happens on a thread, and the question is
        answered from the vectors already loaded (#67: questions during indexing stalled 8-11 s in the reload)"""
        now = time.time()
        if now - getattr(self, "_seen_at", 0) < every:
            return
        self._seen_at = now
        dv = self.con.execute("PRAGMA data_version").fetchone()[0]   # changes only when another connection (the indexer) commits
        if dv == getattr(self, "_dv", None):
            return   # nothing new: skip the count below, a full scan of chunks (~25 s on a 5 GB index, every minute) (#21)
        self._dv = dv
        if getattr(self, "_reloading", False):
            return   # the thread already running picks this up when it reads
        self._reloading = True
        threading.Thread(target=self._reload, daemon=True).start()

    def _reload(self):
        """read the vectors on its own connection, then swap them in at once (a search sees the old set or the new one)"""
        try:
            from enc_eg2 import encoder_of, make_encoder
            con = sqlite3.connect(self.db_uri, uri=True, check_same_thread=False)
            try:
                n = con.execute("SELECT count(*) FROM chunks WHERE emb IS NOT NULL").fetchone()[0]
                top = con.execute("SELECT max(id) FROM chunks").fetchone()[0] or 0
                e_ = encoder_of(con)
                if n == len(self.ids) and top == getattr(self, "_top", top) and e_ == self.encoder:   # (an idle-time upgrade switches the encoder, counts unchanged)
                    self._top = top
                    return
                enc = make_encoder(e_, DEVICE, max_len=128) if e_ in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") and e_ != self.encoder else None
                rows = con.execute("SELECT c.id, c.emb FROM chunks c WHERE c.emb IS NOT NULL").fetchall()
            finally:
                con.close()
            ids = np.array([r[0] for r in rows], dtype=np.int64)
            M = np.stack([np.frombuffer(r[1], np.float16) for r in rows]).astype(np.float32) if rows else None
            with SEARCH_LOCK:
                if enc is not None:   # the index now records another encoder (#9)
                    self.encoder = e_; self.enc = enc
                self.ids = ids
                self.M = M if M is not None else np.zeros((0, len(self.qvec("준비"))), np.float32)
                self.Mg = None; self.name_rows = None; self._top = top
            print(f"FINDR_RELOAD vectors {len(ids)}", flush=True)
        except Exception as e:   # a failed reload keeps the vectors the page already has
            print(f"FINDR_RELOAD failed {type(e).__name__}", flush=True)
        finally:
            self._reloading = False

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

    def scan_text(self, like, seconds=None, every=20000):
        """files whose text holds a 2-letter word, read without an index. After a deep run (920k pieces) one such question took
        24-31 s on a Mac (사진, 예산, 계약) and the time showed in no stage. Two bounds: stop at 201 files (more than 200 is a
        common word, which literal_files drops anyway: the same answer, at once), and stop reading after LIT_SCAN_S (1 s) for a
        rare word on a large index (then names and paths alone)"""
        t0 = time.time(); limit = float(os.environ.get("LIT_SCAN_S") or 1.0) if seconds is None else seconds
        self.con.set_progress_handler(lambda: int(time.time() - t0 > limit), every)   # checked every N SQLite steps
        try:
            out = {f_ for (f_,) in self.con.execute("SELECT DISTINCT file FROM chunks WHERE text LIKE ? ESCAPE '\\' LIMIT 201", (like,))}
        except sqlite3.OperationalError:   # interrupted at the time limit
            out = set()
        finally:
            self.con.set_progress_handler(None, 0)
            if getattr(_ST, "d", None) is not None:   # shown in FINDR_SLOW: this time had no stage before
                _ST.d["literal_scan"] = time.time() - t0
        return out

    def literal_files(self, text, limit=30):
        """for a 1-3 word question: files whose name, path or text contains every word (case-insensitive), newest first"""
        ws = self.content_words(text)
        if not 1 <= len(ws) <= 3:
            return []
        sets = None
        for w in ws:
            like = "%" + w.replace("%", "").replace("_", "\\_") + "%"
            fs = {f_ for (f_,) in self.con.execute("SELECT id FROM files WHERE (name LIKE ? ESCAPE '\\' OR nfc(path) LIKE ? ESCAPE '\\') AND tier IN (1,3)", (like, like))}
            if len(w) >= 3:   # trigram index: fast
                fs |= {f_ for (f_,) in self.con.execute("SELECT DISTINCT c.file FROM fts JOIN chunks c ON c.id = fts.rowid WHERE fts MATCH ? LIMIT 5000",
                                                        ('"' + w.replace('"', '') + '"',))}
            elif len(ws) == 1:   # a 2-char single keyword ("대산"): the trigram index cannot hold it, scan the text once
                fs |= self.scan_text(like)
            sets = fs if sets is None else sets & fs
            if not sets:
                return []
        if len(sets) > 200:   # a common word is not a keyword search
            return []
        return [f_ for (f_,) in self.con.execute(f"SELECT id FROM files WHERE id IN ({','.join('?' * len(sets))}) ORDER BY mtime DESC LIMIT ?", (*sets, limit))]

    def xling_named(self, q, res=(), limit=50):
        """files whose NAME carries every idea of a Korean question through XLING, one at least by translation, newest first.
        The rescue in the top 10 is not enough when neither the meaning nor the word search brought the name in
        ('갤럭시 광고 문구' -> Galaxy_Ad_Copy.docx: no Korean word of the question is in it). Every idea first, then the
        English-spelled ones alone; nothing when a name in the top 10 (res) already carries them"""
        sets = xling_sets(q)
        self._xl_partial = False
        if sets:
            self.load_scan()
        for ideas in sets:
            if any(xling_cover(x.get("name"), ideas)[0] for x in res[:10]):
                return []
            cids = [cid for cid, nm, tr in self.name_rows if tr != 2
                    and all(any(sp[:1] == "~" or sp in nm for sp in idea) for idea in ideas)   # cheap substring pass first: 100k names
                    and xling_cover(nm, ideas) == (True, True)]
            if len(cids) > limit:   # a pair of common words is not a name search
                return []
            if cids:
                qm = ",".join("?" * len(cids))
                return [f_ for (f_,) in self.con.execute(
                    f"SELECT f.id FROM chunks c JOIN files f ON f.id=c.file WHERE c.id IN ({qm}) ORDER BY f.mtime DESC", cids)]
        if sets and os.environ.get("XLING_PARTIAL") == "1":   # no name carries every idea: the names that carry most of them
            ideas = sets[0]
            # the top 10 already holds a name that carries as much of the question (in Korean too: '…데이터 분석….ipynb' for
            # '파이썬 데이터 분석 예제'): the first stage found the name, nothing to bring in ('data.py' would have been)
            top_ = max((xling_carried(x.get("name"), ideas) for x in res[:10]), default=0)
            scored = [(xling_partial(nm, ideas), xling_carried(nm, ideas), cid) for cid, nm, tr in self.name_rows if tr != 2
                      and sum(any(sp[:1] == "~" or sp in nm for sp in idea[1:]) for idea in ideas) >= 2]   # cheap pass first
            scored = [(n, c, cid) for n, c, cid in scored if n and c > top_]
            best = max((n for n, _, _ in scored), default=0)
            cids = [cid for n, _, cid in scored if n == best]
            if cids and len(cids) <= limit:
                qm = ",".join("?" * len(cids)); self._xl_partial = True
                return [f_ for (f_,) in self.con.execute(
                    f"SELECT f.id FROM chunks c JOIN files f ON f.id=c.file WHERE c.id IN ({qm}) ORDER BY f.mtime DESC", cids)]
        return []

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
        if not photos_wanted(q, p):
            r["photo_skip"] = "kind"
            return
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

    def name_floor(self, q, res):
        """the files a system file search would list for these words (every word in the NAME) are not lost: if one
        exists and #1 is not one of them, the best of them goes first (the one already ranked highest, else the newest)"""
        words = name_words(q)
        if not words or not res:
            return res
        # a format the question names holds here too: '강의 자료 pdf' is not the xlsx whose name says 강의자료 (real PC, 11/9)
        fmt = parse(q)["exts"] if os.environ.get("KIND_GATE", "1") == "1" else set()
        fits = lambda name: not fmt or os.path.splitext(name or "")[1].lower() in fmt
        key = lambda s: re.sub(r"[\s_\-.()\[\]]+", "", unicodedata.normalize("NFC", s or "").lower())
        if all(w in key(res[0].get("name")) for w in words) and fits(res[0].get("name")):
            return res
        for i, x in enumerate(res[1:], 1):   # one already among the results: move it up
            if all(w in key(x.get("name")) for w in words) and fits(x.get("name")):
                return [res[i]] + res[:i] + res[i + 1:]
        if fmt and not fits(res[0].get("name")):   # no name holds every word in that format: the first that fits goes first
            j = next((i for i, x in enumerate(res) if fits(x.get("name"))), None)
            if j is not None:
                return [res[j]] + res[:j] + res[j + 1:]
        import unicodedata as _ud
        self.con.create_function("nfc", 1, lambda s: _ud.normalize("NFC", s) if s else s, deterministic=True)
        sql = ("SELECT id FROM files WHERE tier IN (1,3) AND " + " AND ".join(["lower(nfc(name)) LIKE ?"] * len(words))
               + " ORDER BY mtime DESC LIMIT 20")
        try:
            hit = [fid for (fid,) in self.con.execute(sql, [f"%{w}%" for w in words])]
        except sqlite3.OperationalError:
            return res
        hit = [h for h in hit for nm in [self.con.execute("SELECT name FROM files WHERE id=?", (h,)).fetchone()[0]]
               if all(w in key(nm) for w in words) and fits(nm)]
        if not hit:
            return res
        top = self.files_out(hit[:1])
        if top:
            top[0]["why"] = (top[0].get("why") or []) + ["이름에 찾는 말이 모두 있음"]
        return (top + [x for x in res if x.get("fid") != hit[0]])[:len(res)]

    def version_first(self, q, res, cap=50, latest=False):
        """version_pick over the whole family, not only the versions that happened to be among the candidates
        (v2..v7 in one folder, but only v3 and v4 retrieved: '바로 전 판' must be v6)"""
        n0 = len(res)
        import unicodedata as _ud   # names compared composed (NFC); the app registers this too, other entry points may not
        self.con.create_function("nfc", 1, lambda s: _ud.normalize("NFC", s) if s else s, deterministic=True)
        words = [w for w in re.findall(r"[0-9A-Za-z가-힣_]{2,}", q) if w not in _QSTOP]
        if words:   # the question names the document (A사 사업계획서): files whose names hold every word, even if not retrieved
            have = {x.get("fid") for x in res}
            sql = "SELECT id FROM files WHERE tier IN (1,3) AND " + " AND ".join(["nfc(name) LIKE ?"] * len(words)) + " LIMIT 50"
            named = [fid for (fid,) in self.con.execute(sql, [f"%{w}%" for w in words]) if fid not in have]
            res = res + self.files_out(named)
        key = version_family(q, res, top=len(res))
        if key is None:
            return res[:n0]
        folder, _ = key
        have = {x.get("fid") for x in res}
        pre = folder.rstrip("/") + "/"
        sib = [fid for fid, path in self.con.execute("SELECT id, path FROM files WHERE nfc(path) LIKE ?", (pre + "%",))
               if fid not in have and family({"path": path}) == key]   # family() compares the folder too
        res = res + self.files_out(sib[:cap])
        return version_pick(q, res, top=len(res), key=key, latest=latest)[:n0]   # as many results as before: the family goes first

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
        import unicodedata as _ud; q = _ud.normalize("NFC", q or "")
        self.refresh()
        t0 = time.time(); p = parse(q); text = p["text"]; _QT[0] = t0
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
        # the extra words join the word search only: in the meaning query they blurred English questions (held-out
        # virtual PCs 11/9: two English questions from #1 to out of the list, while Korean ones gained)
        p["xl"] = xl_expand(q) if os.environ.get("XLING2") == "1" else ""
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
        # reranked routes fetch at least k candidates and score the first NCAND: with NCAND 5 on a CPU (#78) the page asked
        # for 10 or 30 and got 5, and answers at ranks 6-10 disappeared (#66 rerun)
        n_fetch = len(LABELS) if rer == "pick1" else (max(NCAND, k) if rer == "ce" else NCAND)
        r = self._search(q, p, text, eng, n_fetch if use4b else k, t0, late)
        if p.get("soft_time") and not r["results"]:
            # 작년 / 재작년 with nothing saved then: people misplace a year (Mac, 11/9: a file from three years back asked as
            # 작년 vanished from rank 3 to an empty list). Search without the period; the end says no file is from it
            r = self._search(q, dict(p, since=None, until=None), text, eng, n_fetch if use4b else k, t0, late)
            r["time_miss"] = True
        pre_period = list(r["results"]) if p["since"] and not r.get("time_miss") else None   # inside the period, before later stages
        if use4b and r["results"]:
            ids = [x["fid"] for x in r["results"]]; qm = ",".join("?" * len(ids))
            sm = dict(self.con.execute(f"SELECT file, summary FROM cards WHERE file IN ({qm})", ids).fetchall())
            hd = dict(self.con.execute(f"SELECT file, text FROM chunks WHERE n=0 AND file IN ({qm})", ids).fetchall())
            for x in r["results"]:
                x["summary"] = sm.get(x["fid"]) or ""; x["head"] = hd.get(x["fid"]) or ""
            if rer == "ce":
                sc = ce_scores(q, r["results"], top=NCAND)
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
                    elif found:   # the word is in some file, which fits the rest of the question less
                        r["note"] = f"'{miss[2]}'이(가) 들어간 파일은 있지만 질문 전체와는 덜 맞습니다. 가장 가까운 파일을 보여 드립니다."
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
            if rer == "ce":
                lit = self.literal_files(p.get("text") or q)
                if lit:
                    have = [x for x in r["results"] if x["fid"] in lit]
                    extra = [f_ for f_ in lit if f_ not in {x["fid"] for x in have}][:max(0, k - len(have))]
                    rest_ = [x for x in r["results"] if x["fid"] not in lit]
                    r["results"] = (have + self.files_out(extra) + rest_)[:k]; r["literal"] = len(lit)
                j = xling_name_first(q, r["results"])   # Korean words, English name: Remote_Access_Design.pdf (#19)
                if j:
                    r["results"] = [r["results"][j]] + r["results"][:j] + r["results"][j + 1:]; r["xling"] = 1
                elif r["results"]:
                    xf = self.xling_named(q, r["results"])   # not in the top 10: from all names (field: 0.2.13 found it only below rank 10)
                    if len(xf) > 1 and self._xl_partial:   # names that carry the same part of the question: their content decides
                        pool_ = self.files_out(xf[:NCAND])   # ('<brand> 스마트폰 …': one product's copy deck, not another's)
                        xf = [x["fid"] for _, x in sorted(zip(ce_scores(q, pool_), pool_), key=lambda t: -t[0])]
                    if xf:
                        at = next((i for i, x in enumerate(r["results"]) if x["fid"] == xf[0]), None)
                        top_ = [r["results"].pop(at)] if at is not None else self.files_out(xf[:1])
                        if top_:
                            top_[0]["why"] = (top_[0].get("why") or []) + ["이름에 찾는 뜻이 모두 있음(영어)"]
                            r["results"] = (top_ + r["results"])[:k]; r["xling"] = 2
                # version questions last: a page that quotes the question ('…최종본이 아닙니다') must not undo the version choice
                if PREVQ.search(q):   # "최종본 말고 그 직전 판": the version right before the latest
                    r["results"] = self.version_first(q, r["results"])
                elif LATESTQ.search(q):   # "A사 사업계획서 최종본": the newest version of the named document
                    r["results"] = self.version_first(q, r["results"], latest=True)
                elif OLDQ.search(q):
                    r["results"] = older_sibling_first(r["results"])
                r["results"] = self.name_floor(q, r["results"])   # never worse than the system's own name search
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
        if pre_period and os.environ.get("TIME_WORDS", "1") == "1":
            # later stages (repair across the whole index, history, cross-language and name passes) bring files from outside
            # the period; on a Mac "작년에 만든 발표자료" ended with ten files of this year while the period held ten (11/9).
            # Files of the period come first: those the stages kept, then the ones they pushed out, then the rest
            a_ = p["since"].strftime("%Y-%m-%d %H:%M"); b_ = p["until"].strftime("%Y-%m-%d %H:%M") if p["until"] else "9999"
            fit = lambda x: a_ <= (x.get("date") or "") < b_
            now_ = [x for x in r["results"] if fit(x)]
            have_ = {x.get("fid") for x in now_}
            back_ = [x for x in pre_period if fit(x) and x.get("fid") not in have_]
            r["results"] = (now_ + back_ + [x for x in r["results"] if not fit(x)])[:k]
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
        split_ = lambda t: [w for w in re.split(r"[\s/\\,.()\[\]\"']+", t) if w]
        words = split_(text) + distinct_words(split_(eng))   # English expansions: one idea once (gen + generate), #32
        words += [w for w in distinct_words(split_(p.get("xl") or "")) if w not in words]
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
    import unicodedata as _ud   # macOS indexes made before #12 hold decomposed (NFD) names; questions are NFC
    path = _ud.normalize("NFC", x["path"].replace("\\", "/")); name = re.sub(r"\.[^.]+$", "", path.rsplit("/", 1)[-1].lower())
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


# "the one right before the latest": 최종본 말고 그 직전 판, 마지막 거 바로 전, 직전 버전
LATESTQ = re.compile(r"최종\s*(?:본|판|버전)|최신\s*(?:본|판|버전)|마지막\s*(?:본|판|버전)|final", re.I)   # 'the newest one' (unless PREVQ)
PREVQ = re.compile(r"직전|바로\s*(?:앞|전)|(?:최종|마지막|최신)\S*\s*(?:말고|빼고|아닌|이전|전)", re.I)
_VERNUM = re.compile(r"(?:^|[_\-\s.(])v(?:er)?\s*(\d+)|\((\d+)\)", re.I)
_QSTOP = {"최종본", "최종", "말고", "빼고", "직전", "그", "판", "버전", "바로", "전", "이전", "마지막", "최신", "파일", "문서", "아닌", "거", "것"}


def _version_key(x):
    """order inside one family: an explicit version number (v3, ver2, (2)) first, then the file's date"""
    name = x["path"].replace("\\", "/").rsplit("/", 1)[-1]
    nums = [int(a or b) for a, b in _VERNUM.findall(name)]
    return (max(nums) if nums else -1, x.get("date") or "")


def _families(res, top):
    fams = {}
    for i, x in enumerate(res[:top]):
        d, s = family(x)
        if s:
            fams.setdefault((d, s), []).append(i)
    return fams


def version_family(q, res, top=30):
    """(folder, stem) of the document a version question is about, or None. Chosen by the words of the question found in
    the file names, not by whatever ranked first: a file whose text says '최종본이 아닙니다' can outrank every version of the
    document that was asked for. Without such words, #1's family when it has 2+ members among the candidates."""
    words = [w.lower() for w in re.findall(r"[0-9A-Za-z가-힣_]{2,}", q) if w not in _QSTOP]
    fams = _families(res, top)
    named = [k for k in fams if any(w in k[1] for w in words)]
    if named:   # most question words in the name (A사 + 사업계획서 over A사 alone), then a family named exactly by
        # a word (doc1) before one that only contains it (make_doc1_v3.py), then rank
        return min(named, key=lambda k: (-sum(w in k[1] for w in words), k[1] not in words, fams[k][0]))
    return next((k for k, v in fams.items() if 0 in v and len(v) >= 2), None)


def version_pick(q, res, top=30, key=None, latest=False):
    """the question asks for the version right before the latest (PREVQ). Within the family (same folder, same name
    without version marks) the second newest goes first, by version number, then date. Nothing changes without a family of 2+."""
    if len(res) < 2:
        return res
    key = key or version_family(q, res, top)
    fams = _families(res, top)
    pick = key if key in fams and len(fams[key]) >= 2 else None
    if pick is None:
        return res
    members = sorted(fams[pick], key=lambda i: _version_key(res[i]))
    j = members[-1] if latest else members[-2]   # the newest, or the one before it
    order = [j] + [i for i in reversed(members) if i != j]   # then the rest of the family, newest first
    out = [dict(res[i]) for i in order] + [x for i, x in enumerate(res) if i not in set(order)]
    ces = [x.get("ce") for x in res[:top] if x.get("ce") is not None]
    if ces:   # the shown chance follows the ranker score: the chosen version takes the best one, so #1 is not shown at 1%
        out[0]["ce"] = max(ces)
    return out


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
LLM = os.environ.get("FINDR_LLM", "")   # condition / plan reader (a research 4B server, e.g. http://127.0.0.1:8090/v1/chat/completions); not shipped: off


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
# a kind of document: a person asking for one means a document of that kind, not a picture that looks like the scene
# (real PCs, 11/9: "회의록" brought five meeting photos over thirteen minutes; "강의 자료 pdf" a png first)
DOCKIND = re.compile(r"회의록|계약서|견적서|발주서|명세서|보고서|계획서|제안서|신청서|지원서|승낙서|각서|확인서|증명서|"
                     r"정관|양식|서식|서류|문서|공문|기안|규정|매뉴얼|안내문|공지|발표\s*자료|강의\s*자료|자료집|교안|논문|"
                     r"이력서|명단|목록|리스트|영수증|청구서|내역|설문", re.I)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}


def format_first(q, rs):
    """the format the question names, at the very end (KIND_GATE=1): later stages (repair across the whole index, history,
    versions, cross-language names, personal) bring files in without the search's own format filter; on a Mac
    '강의 자료 pdf' showed .key files at 2-5 (11/9). Files in the asked format keep their order and go first; the rest
    follow (kept, not dropped)"""
    exts = parse(q)["exts"]
    if not exts:
        return rs
    fit = lambda x: os.path.splitext(x.get("name") or x.get("path") or "")[1].lower() in exts
    return [x for x in rs if fit(x)] + [x for x in rs if not fit(x)]


def collapse_copies(con, res):
    """the same file saved twice (a download kept twice, 'name (1)', a copy in another folder) is one thing to a person:
    one line, the best-ranked copy, the others under it as 'copies'. Seen on all three real PCs (11/9): a copy took #1
    over its twin, and two slots of five. Same = same format, same size, same opening text (not names: '규정_v3' and
    '규정_v3 (1)' are the same file, '규정_v2' is not)"""
    rs = res.get("results") or []
    fids = [x.get("fid") for x in rs if x.get("fid") is not None]
    if len(fids) < 2:
        return res
    qm = ",".join("?" * len(fids))
    meta = {fid: (ext, size, head) for fid, ext, size, head in con.execute(
        f"SELECT f.id, f.ext, f.size, (SELECT text FROM chunks c WHERE c.file=f.id AND c.n=0) FROM files f WHERE f.id IN ({qm})", fids)}
    seen, out = {}, []
    for x in rs:
        m = meta.get(x.get("fid"))
        body = re.sub(r"^파일: .*?구조: ", "", (m[2] or "") if m else "")[:400]   # the header names the file: compare the text
        k = (m[0], m[1], body) if m and m[1] and len(body) >= 20 else None
        if k is not None and k in seen:
            seen[k].setdefault("copies", []).append(x.get("path"))
            continue
        if k is not None:
            seen[k] = x
        out.append(x)
    if len(out) < len(rs):
        res = dict(res); res["results"] = out; res["copies_folded"] = len(rs) - len(out)
    return res


_VMARK = re.compile(r"(\d*\s*작성중[\s_\-]*\d*|v\d+(?:\.\d+)*|ver\d+(?:\.\d+)*|old|new|final|updated|최종|초안|수정|코멘트반영|검토의견반영|(?:인쇄|이메일전송|메일|제출|배포|출력|게시|보관|검토|발표|공유)용|발송본|내부용|압축본|서명본?|사인포함|\(\d+\)|copy|사본|bak|제출|확정|검토|완료|전처리|테스트용|샘플|pdf|clean|merged|google\s*docs)", re.I)
_VDATE = re.compile(r"\d{4}-?\d{2}-?\d{2}|\d{6}(?!\d)|(?<=[_\-\s])\d{4}$")
# a tag in brackets before the name ((작성중), (메타_2_수정), [결과보고서]) and an outline number (1-2. ) say which copy
# or which state, not which document (M1 Pro 11/9: such pairs were not folded)
_VHEAD = re.compile(r"^(?:\s*[\(\[][^\)\]]{1,24}[\)\]]\s*|\s*\d+(?:[\-.]\d+)*\.\s+)+")


def _vname(path):
    """a file name without its extension, folder and version marks, and the marks it had (dates apart)"""
    import unicodedata as _ud
    name = re.sub(r"\.[^.]+$", "", _ud.normalize("NFC", path.replace("\\", "/")).rsplit("/", 1)[-1].lower())
    head = _VHEAD.match(name)
    marks = tuple(sorted(m.lower() for m in _VMARK.findall(name))) + ((re.sub(r"\s+", "", head.group(0)),) if head else ())
    s = re.sub(r"[\s_\-.()\[\]]+", " ", _VDATE.sub(" ", _VMARK.sub(" ", _VHEAD.sub("", name)))).strip()
    return s, marks


def _ids(stem):
    """numbers and part letters that stay in a name once version marks and dates are off: 제1115호 vs 제1116호, 3월 vs
    4월, Part A vs Part B are different documents, whatever their text has in common"""
    return set(re.findall(r"\d+", stem)) | {m.lower() for m in re.findall(r"(?:part|부|편|권)\s*([a-z]|[ivx]+)\b", stem, re.I)}


def _grams(t):
    # spaces and line breaks taken out: a PDF made from a .hwp often splits words or adds odd spaces (Windows 11/9: an
    # edited .hwp and its signed .pdf were not seen as one document)
    t = re.sub(r"\s+", "", t or "")
    return {t[i:i + 3] for i in range(max(len(t) - 2, 0))}


_VDOCS = {"pdf", "hwp", "hwpx", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "key", "pages", "numbers", "odt", "ods", "odp", "rtf"}


def group_versions(con, res, top=20):
    """versions of one document are one line to a person: the best-ranked version, the others under it as 'versions',
    newest first. On real PCs (11/9) the version families of a document were spread over folders two times in three
    (a working folder and a submitted one), and a version above the asked one was the first cause of misses; folding
    them also lets other documents up into the first lines. One document = (a) the same name once version marks are
    taken off, in the same folder or the folder above it, when the names differ by version marks (v2, 최종, 수정, (1))
    (a short common name like 제안서 with some text in common) or the openings are alike; or (b) nearly the same opening text whatever the name or format (a .hwp and the .pdf
    made from it). Names that differ by a date only, with other text, are a series (weekly minutes), not versions."""
    rs = res.get("results") or []
    if len(rs) < 2:
        return res
    head = rs[:top]
    fids = [x.get("fid") for x in head if x.get("fid") is not None]
    txt = {}
    if fids:
        qm = ",".join("?" * len(fids))
        txt = {fid: re.sub(r"^파일: .*?구조: ", "", t or "")[:900] for fid, t in
               con.execute(f"SELECT file, text FROM chunks WHERE n = 0 AND file IN ({qm})", fids)}
    info = []
    for x in head:
        path = (x.get("path") or "").replace("\\", "/")
        if path.rsplit(".", 1)[-1].lower() not in _VDOCS:   # logs, code and data have runs, not versions (M1 Pro 11/9:
            info.append(("", "", (), None, None, set(), ""))   # the same log in folders named by the time of each run)
            continue
        s, marks = _vname(path)
        body = txt.get(x.get("fid")) or ""
        info.append((path.rsplit("/", 2)[0], s if len(s.replace(" ", "")) >= 2 else "", marks,
                     _grams(body) if len(body) >= 80 else None, _grams(body[300:]) if len(body) >= 500 else None,
                     _grams(s), (x.get("path") or "").rsplit(".", 1)[-1].lower()))
    parent = list(range(len(head)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def alike(a, b, need):
        return a is not None and b is not None and len(a & b) / max(len(a | b), 1) >= need

    ids = [_ids(x[1]) for x in info]
    gids = {i: [ids[i]] if ids[i] else [] for i in range(len(head))}   # the numbers each group holds

    def clash(a, b):   # a group never takes a file whose name numbers differ from any of its members (no chains via N.pdf)
        return any(x != y for x in gids[a] for y in gids[b])

    for i in range(len(head)):
        for j in range(i):
            (ai, si, mi, gi, bi, ni, ei), (aj, sj, mj, gj, bj, nj, ej) = info[i], info[j]
            ri, rj = find(i), find(j)
            if ri == rj or clash(ri, rj):
                continue
            # the same name: versions when the marks differ (a short common name like 제안서 needs some text in common);
            # when only a date differs, the same form filled again (another year, another applicant: M1 Pro 11/9 7 of 20
            # wrong) is not a version, so the text must be nearly the same
            short = len(si.replace(" ", "")) < 4
            # the same name in another format (a .hwp and the .pdf made from it, a date tail on one) reads differently: 0.6
            same_name = si and si == sj and ai == aj and ((mi != mj and (not short or alike(gi, gj, 0.3)))
                                                         or alike(gi, gj, 0.6 if ei != ej else 0.85))
            # any name: nearly the same text, past a standard opening (a copyright paragraph, a resolution header, a form
            # line chained 274 files on one PC), and a name alike in part or another format (a .hwp and its .pdf).
            # Names that differ need the text all but identical: the same form filled by another school is 0.85 alike.
            # Different numbers in the names (standards 1115 and 1116, a March and an April menu) are never one document
            # (M1 Pro 11/9: 4 of 12 folded lines wrong, all of these kinds)
            # names built the same way that differ in one place (기관A_2023 / 기관B_2023: another school, company or
            # person in the same slot) are the same form for someone else, however alike their text (M1 Pro 11/9)
            ti, tj = si.split(), sj.split()
            if si != sj and len(ti) == len(tj) >= 2 and sum(a != b for a, b in zip(ti, tj)) == 1:
                continue
            # another format of the same document (.pptx and its .pdf) reads a little differently: 0.88
            same_text = (alike(gi, gj, 0.85 if si == sj else 0.88 if ei != ej else 0.92) and (bi is None or bj is None or alike(bi, bj, 0.7))
                         and (ei != ej or alike(ni, nj, 0.3)))
            if same_name or same_text:
                parent[ri] = rj
                gids[rj] = gids[rj] + gids.pop(ri)
    groups = {}
    for i in range(len(head)):
        groups.setdefault(find(i), []).append(i)
    if all(len(g) == 1 for g in groups.values()):
        return res
    out, folded = [], 0
    for i, x in enumerate(head):
        g = groups[find(i)]
        if g[0] != i:
            folded += 1
            continue
        if len(g) > 1:
            x = dict(x)
            others = sorted((head[k] for k in g[1:]), key=lambda y: y.get("date") or "", reverse=True)
            x["versions"] = [{"path": y.get("path"), "date": y.get("date"), "size": y.get("size")} for y in others]
        out.append(x)
    res = dict(res)
    res["results"] = out + rs[top:]
    res["versions_folded"] = folded
    return res


def photos_wanted(q, p):
    """the question leaves room for a photo: it names no format other than images, and no kind of document unless it also
    says photo ('회의 사진', '영수증 찍은 거'). On by default since the real-PC sealed sets (11/9: three PCs, none worse); KIND_GATE=0 turns it off"""
    if os.environ.get("KIND_GATE", "1") != "1":
        return True
    if PHOTO_WORD.search(q):
        return True
    exts = (p or {}).get("exts") or set()
    if exts and not (exts & IMAGE_EXTS):
        return False
    return not DOCKIND.search(q)


_PHOTO_UP = [0.0, False]   # (checked at, answering)


def _photo_up():
    """is the photo server answering? It loads for 1-2 minutes after start (search does not wait, #57), and on Windows a
    refused localhost connection takes ~2 s: without this look every question then waited PHOTO_WAIT for nothing.
    Down: look again after 10 s; up: trusted until a request fails"""
    import socket
    if _PHOTO_UP[1] or time.time() - _PHOTO_UP[0] < 10:
        return _PHOTO_UP[1]
    u = urlparse(PHOTO_URL)
    try:
        socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=0.3).close()
        _PHOTO_UP[:] = [time.time(), True]
    except OSError:
        _PHOTO_UP[:] = [time.time(), False]
    return _PHOTO_UP[1]


def photo_text_vec(q):
    import base64, urllib.request
    if not _photo_up():
        raise ConnectionError("photo server not answering yet")
    req = urllib.request.Request(PHOTO_URL.rstrip("/") + "/text", json.dumps({"q": q}).encode(), {"Content-Type": "application/json"})
    try:
        o = json.loads(urllib.request.urlopen(req, timeout=3).read())
    except OSError:   # the server went away: back to looking every 10 s
        _PHOTO_UP[:] = [time.time(), False]
        raise
    return np.frombuffer(base64.b64decode(o["vec"]), np.float16).astype(np.float32), float(o["scale"])
# candidates reranked. CPU: the first stage's top 5 only, a light check that keeps the 'not here' judgement and the
# name / version rules (2026-11-08, 4 cores, 120 real-PC questions: 15 candidates 2.5 s median and 3.8 s slowest tenth,
# 5 candidates within 1.5 s per question 1.1 s and 1.4 s at the same accuracy; no reranking 0.04 s but top-1 25 -> 17
# and no 'not here' at all)
NCAND = int(os.environ.get("NCAND") or (5 if DEVICE == "cpu" else 30))
CE_MAXLEN = int(os.environ.get("CE_MAXLEN") or (256 if DEVICE == "cpu" else 384))   # ranker input length (256 for the CPU ranker)
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
CE_RATE = {}   # seconds per candidate on the CPU, per reranker


def ce_scores(q, res, d=None, nosumm=None, top=None):
    """cross-encoder (bge-reranker-v2-m3, 568M): one batched pass over the candidates. d: model dir (cached per dir).
    top: score only the first `top`; the rest keep their first-stage order below them"""
    d = d or os.environ.get("CE_DIR", str(Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "rank"))
    nosumm = bool(os.environ.get("CE_NOSUMM")) if nosumm is None else nosumm
    if d not in CE:
        with _CE_LOCK:   # the warm-up loads it outside the search lock: a question at the same moment must not load a second copy
            if d not in CE:
                _ce_load(d)
    tok, m = CE[d]
    return _ce_score(q, res, d, tok, m, nosumm, top)


def _ce_load(d):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(d)
    m = (AutoModelForSequenceClassification.from_pretrained(d, torch_dtype=torch.float16).to(DEVICE) if DEVICE == "cuda"
         else AutoModelForSequenceClassification.from_pretrained(d, torch_dtype=torch.float32).to(DEVICE) if DEVICE == "mps"
         else _cpu_ranker_for(d, lambda: AutoModelForSequenceClassification.from_pretrained(d, torch_dtype=torch.float32))).eval()
    nb = Path(d) / "none_bias.txt"   # a none-aware reranker carries its learned 'no answer' score
    if nb.exists():
        import math
        CE_BIAS[d] = 1 / (1 + math.exp(-float(nb.read_text().strip())))
    CE[d] = (tok, m)


import collections  # noqa: E402
import hashlib  # noqa: E402
import threading as _th  # noqa: E402

# reranker scores by (model, question, candidate text): people ask the same thing again, the page asks again for more results,
# and one question scores overlapping candidates in its repair passes. The text is the key, so an edited file is scored anew.
# The number of candidates scored does not change with the cache: the same question keeps the same answer (#74).
GOV = None   # the resource manager, started with the server (governor.py)
CE_CACHE = collections.OrderedDict()
CE_CACHE_MAX = int(os.environ.get("CE_CACHE") or 50000)   # 0: off
CE_CACHE_LOCK = _th.Lock()   # the warm-up scores outside the search lock


def _ce_key(d, q, doc):
    if not CE_CACHE_MAX:
        return object()   # never found: every score is computed
    return hashlib.blake2b(f"{d}\x00{q}\x00{doc}".encode("utf-8", "replace"), digest_size=16).digest()


def _ce_score(q, res, d, tok, m, nosumm, top=None):
    import torch
    if nosumm:   # the stage-A reranker was trained on name | folder | opening text
        # CE_TAIL=1: candidates past the first CE_TAIL_FROM (10) read a shorter opening (CE_TAIL_CHARS, 220). On a Mac the
        # time is the candidates times their length (M1 Pro alone: 10 x 200 tokens 0.35 s, 10 x 384 0.65 s, 30 x 384 1.9 s;
        # no shape or fp16 effect, 11/9); the page shows the first ten, and the rest still get scored, more cheaply
        # on by default on an Apple GPU only (a Mac M1 Pro, 11/9: p50 2.37 -> 1.76 s, the 2 s line met, the known-file answers
        # and their ranks unchanged, one point lost at rank 5 of a vague task); an NVIDIA GPU scores 30 in a few ms anyway
        tail_on = os.environ.get("CE_TAIL", "1" if DEVICE == "mps" else "0") == "1"
        tail_from = int(os.environ.get("CE_TAIL_FROM") or 10) if tail_on else 10 ** 9
        tail_n = int(os.environ.get("CE_TAIL_CHARS") or 220)
        docs = [f"{x['name']} | {'/'.join(x['path'].replace(chr(92), '/').split('/')[-3:-1])} | {x.get('head', '')[:700 if i < tail_from else tail_n]}"
                for i, x in enumerate(res)]
    else:
        docs = [f"{x['name']} | {'/'.join(x['path'].replace(chr(92), '/').split('/')[-3:-1])} | {(x.get('summary') or '')[:200]} {x.get('head', '')[:500]}"
                for x in res]
    # on the CPU: as many candidates as fit the time budget (CE_BUDGET s), learned from the previous queries;
    # the rest keep their first-stage order below the scored ones
    n = len(docs) if top is None else min(len(docs), top)
    if m.device.type == "cpu" and CE_RATE.get(d):
        # one budget for the whole question (CE_TOTAL s): the first pass gets up to CE_BUDGET, and later passes (repair,
        # memory cues) only what is left, at least 3 candidates. Per-call budgets used to add up to 15-20 s on a laptop.
        left = float(os.environ.get("CE_TOTAL") or 1.5) - (time.time() - _QT[0])   # 1.5 s per question (was 4: 3.8 s slowest tenth)
        cap = min(float(os.environ.get("CE_BUDGET") or 3), max(left, 0.0))
        # at least 3 candidates, else what fits the time left: a 4-core 2019 laptop scores about 0.66 s a candidate, and a floor
        # of 5 made every question 3.3 s against the 1.5 s budget (#66 rerun); a recent CPU still scores 5 within it
        n = min(n, max(3, int(cap / CE_RATE[d])))
    t_ = time.time()

    def run(part):
        # scores already computed for this question and this text come from the cache; only the rest go to the model
        keys = [_ce_key(d, q, s) for s in part]
        with CE_CACHE_LOCK:   # hits are taken now: another thread may push them out before this one is done
            got = {i: CE_CACHE[k] for i, k in enumerate(keys) if k in CE_CACHE}
        miss = [i for i in range(len(keys)) if i not in got]
        if miss:
            b = tok([q] * len(miss), [part[i] for i in miss], padding=True, truncation=True, max_length=CE_MAXLEN,
                    return_tensors="pt").to(m.device)
            t_m = time.time()
            got.update(zip(miss, torch.sigmoid(m(**b).logits.view(-1).float()).cpu().tolist()))
            if GOV is not None:   # the model's own time per candidate (cached scores left out): heat or sharing shows as a slowdown
                GOV.rerank_time(time.time() - t_m, int(b["input_ids"].numel()))
        with CE_CACHE_LOCK:
            out = [got[i] for i in range(len(keys))]
            for i, k in enumerate(keys):
                CE_CACHE[k] = out[i]; CE_CACHE.move_to_end(k)
            while len(CE_CACHE) > CE_CACHE_MAX:
                CE_CACHE.popitem(last=False)
        return out

    with torch.no_grad():
        if m.device.type == "mps":
            # Apple GPU: 30 candidates took 4-5 s, and 7-12 s while indexing or a hot fanless laptop shared the GPU (field,
            # 0.2.12-13). In steps of CE_STEP_MPS (10), first ones first; past CE_TOTAL_MPS (3 s) for the question no more steps.
            # The page's 10 are always scored; the rest keep their first-stage order below them. A step starts only when the
            # last one's time says it ends within the budget: checked at the start alone, a 2-3 s step begun at 2.9 s ended
            # at 5-6 s while indexing shared the GPU (field, Mac Air)
            step, limit = int(os.environ.get("CE_STEP_MPS") or 10), float(os.environ.get("CE_TOTAL_MPS") or 3)
            sc, last = [], 0.0
            while len(sc) < n and (len(sc) < step or time.time() - _QT[0] + last <= limit):
                t_s = time.time()
                sc += run(docs[len(sc):min(n, len(sc) + step)])
                last = time.time() - t_s
            n = len(sc)
        else:
            sc = run(docs[:n])
    if getattr(_ST, "d", None) is not None:   # how many candidates the reranker really scored: a Mac's 3 s budget can stop
        _ST.d["ce_n"] = float(n)               # before 30, and the answer then depends on timing (#74; Mac Air, 11/9)
    if m.device.type == "cpu" and d not in CE_RATE:
        # the rate is measured once (the warm-up's full-size batch) and then kept: learned from each question, it made the
        # number of candidates, and so the answer, depend on the question before (#66: rank 1, then rank 8 a moment later)
        CE_RATE[d] = (time.time() - t_) / max(n, 1)
    low = min(sc) if sc else 0.0
    return sc + [low * 0.5 - 0.001 * i for i in range(len(docs) - n)]


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
    # a period across years shows its years: '01-01~01-01' read as one day for "작년" (Mac Air, 11/9)
    f = "%Y-%m-%d" if p["since"] and p.get("until") and p["since"].year != p["until"].year else "%m-%d"
    return {"찾을 내용": p["text"], "형식": sorted(p["exts"]) or "전체",
            "기간": f"{p['since']:{f}}~{p['until']:{f}}" if p["since"] and p["until"] else (f"{p['since']:{f}} 이후" if p["since"] else "전체"),
            "라이브러리 포함": p["lib"]}


def period_first(q, rs, now=None):
    """the period the question names, at the very end (TIME_WORDS=1): a later stage looks for a word of the question across
    the whole index, and on a Mac "작년에 만든 발표자료" brought this month's code files holding the word 작년 (11/9). Files
    saved inside the period keep their order and go first; the rest follow (kept, not dropped)"""
    p = parse(q, now=now)
    if not p["since"]:
        return rs
    a, b = p["since"].strftime("%Y-%m-%d %H:%M"), p["until"].strftime("%Y-%m-%d %H:%M") if p["until"] else "9999"
    fit = lambda x: a <= (x.get("date") or "") < b
    return [x for x in rs if fit(x)] + [x for x in rs if not fit(x)]


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
_CE_LOCK = threading.Lock()   # loading a reranker (once per model folder)
READY = threading.Event()   # set when the warm-up is done: /healthz answers at once, /ready says when a question gets full speed (#68)
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


def indexer_alive(p):
    """progress.json still describes a running indexer. A stopped or killed one leaves its last line behind.
    POSIX: the recorded pid must exist (signal 0 only checks). Windows: os.kill(pid, 0) would end the process,
    so there only the age of the record is used, as the page already does (15 min)."""
    if not p.get("stage") or p.get("stage") == "done":
        return True   # nothing to hide; the page decides
    pid = p.get("pid")
    if pid and os.name != "nt":
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError, ValueError):
            pass
    return True


# everyday Korean words and the English people name files with (Korean question, English file name: #19).
# Loanwords and common work words only; a name has to carry EVERY idea of the question through this list.
XLING = {
    "로컬": "local", "원격": "remote", "접속": "access connect", "터널링": "tunnel", "터널": "tunnel", "설계": "design blueprint architecture", "설계서": "design spec blueprint",
    "아키텍처": "architecture", "구조": "architecture structure", "배포": "deploy release", "자동화": "automation automate", "자동": "auto",
    "영상": "video", "동영상": "video", "사진": "photo image", "이미지": "image", "음성": "voice audio", "오디오": "audio",
    "보고서": "report", "리포트": "report", "계획서": "plan", "계획": "plan", "사업계획서": "business plan", "제안서": "proposal",
    "견적서": "quote quotation estimate", "견적": "quote estimate", "계약서": "contract agreement", "계약": "contract", "회의록": "minutes meeting",
    "회의": "meeting", "발표": "presentation slides deck", "발표자료": "presentation slides deck", "슬라이드": "slides", "이력서": "resume cv",
    "논문": "paper thesis", "요약": "summary", "매뉴얼": "manual", "설명서": "manual guide", "가이드": "guide", "안내": "guide",
    "데이터": "data", "분석": "analysis", "모델": "model", "학습": "training train", "서버": "server", "클라우드": "cloud",
    "보안": "security", "인증": "auth", "네트워크": "network", "데이터베이스": "database", "인프라": "infra", "마케팅": "marketing",
    "예산": "budget", "매출": "sales revenue", "재무": "finance financial", "투자": "invest", "송장": "invoice", "청구서": "invoice bill",
    "영수증": "receipt", "일정": "schedule", "로드맵": "roadmap", "템플릿": "template", "양식": "form template", "번역": "translation",
    "테스트": "test", "실험": "experiment", "결과": "result", "설정": "config setting", "초안": "draft", "최종": "final",
    "검토": "review", "피드백": "feedback", "사양": "spec", "명세서": "spec", "요구사항": "requirement", "디자인": "design",
    "프로토타입": "prototype", "데모": "demo", "튜토리얼": "tutorial", "강의": "lecture course", "교안": "lecture slides",
    "워크숍": "workshop", "세미나": "seminar", "통합": "integration", "연동": "integration", "엔지니어링": "engineering",
    "파이프라인": "pipeline", "에이전트": "agent", "챗봇": "chatbot", "검색": "search", "추천": "recommend", "대시보드": "dashboard",
    "앱": "app", "웹": "web", "모바일": "mobile", "결제": "payment", "고객": "customer client", "회사소개서": "company profile",
    "브로셔": "brochure", "포트폴리오": "portfolio", "백서": "whitepaper", "가격": "price pricing", "주문": "order",
    "특허": "patent", "연구": "research", "개발": "dev development", "운영": "operation ops", "전략": "strategy", "쇼츠": "shorts",
    "유튜브": "youtube", "썸네일": "thumbnail", "자막": "subtitle caption", "대본": "script", "스크립트": "script",
    "인터뷰": "interview", "설문": "survey", "통계": "statistic stats", "정책": "policy", "체크리스트": "checklist",
    "생성기": "generator", "생성": "generat", "엔진": "engine", "시스템": "system", "플랫폼": "platform", "솔루션": "solution",
    "블루프린트": "blueprint",
    # students' and developers' PCs name files in English (#66): code, notebooks, image processing, machine learning, crawling
    "코드": "code", "파이썬": "python py", "실습": "practice lab exercise", "노트북": "notebook ipynb", "과제": "assignment homework",
    "예제": "example sample", "윤곽선": "contour", "외곽선": "contour", "블러": "blur", "흐림": "blur", "스무딩": "smoothing smooth",
    "엣지": "edge", "경계선": "edge", "필터": "filter", "히스토그램": "histogram", "이진화": "threshold binary", "형태학": "morphology",
    "모폴로지": "morphology", "얼굴": "face", "인식": "recognition detect", "탐지": "detection detect", "객체": "object",
    "분류": "classification classify", "회귀": "regression", "군집": "cluster", "신경망": "neural network", "딥러닝": "deep learning",
    "머신러닝": "machine learning ml", "전처리": "preprocess preprocessing", "시각화": "visualization visualize plot",
    "크롤링": "crawl crawling crawler", "크롤러": "crawler", "스크래핑": "scraping scrape", "셀레니움": "selenium",
    "웹캠": "webcam", "카메라": "camera", "번호판": "plate license", "손글씨": "handwriting mnist", "감정": "emotion sentiment",
    "추출": "extract extraction", "변환": "convert transform", "압축": "compress zip", "엑셀": "excel xlsx", "워드": "word docx",
    "갤럭시": "galaxy", "아이폰": "iphone", "스마트폰": "smartphone phone mobile", "광고": "ad ads advert advertising", "문구": "copy",
    "카피": "copy", "배너": "banner", "상세페이지": "detail page", "캠페인": "campaign", "브랜드": "brand", "로고": "logo",
    "자료": "", "문서": "", "파일": "", "관련": "", "내": "", "그": "", "거": "", "것": "", "처리": "", "하기": "", "찾기": "",
}
_XL_JOSA = re.compile(r"(으로|에서|에게|이랑|하고|까지|부터|이나|은|는|이|가|을|를|의|에|로|와|과|도|만|랑)$")
# a loanword sounds like the English name word (히스토그램 ~ histogram, 넘파이 ~ numpy): both reduced to consonants, so no list
# has to hold every tool and product name. Korean: initial/final consonants (ㅇ final = ng), ㅎ dropped; English: c/g hard,
# th t, tion sn, r silent before a consonant (카트 ~ cart), l = r, voiced = voiceless
_CHO = ["k", "k", "n", "t", "t", "l", "m", "p", "p", "s", "s", "", "j", "j", "j", "k", "t", "p", "h"]
_JONG = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "p", "l", "l", "p", "l", "m", "p", "p", "t", "t", "N", "t", "t", "k", "t", "p", "t"]
_NOT_LOAN = re.compile(r"(기|는|다|고|요|서|며|던|게|지|면|한|된|할|들|님|해|했|하|려|본|봐|운|된)$")   # verb endings: 불러오기 is not 'block'
XL_GENERIC = {"실습", "코드", "데이터", "분석", "연구", "개발", "정리"}   # rarely in a name (relax: optional)


def ko_sound(w):
    out = []
    for ch in w:
        c = ord(ch) - 0xAC00
        if not 0 <= c < 11172:
            return ""
        out += [_CHO[c // 588], _JONG[c % 28]]
    return re.sub(r"(.)\1+", r"\1", "".join(out).replace("h", ""))


import functools  # noqa: E402


@functools.lru_cache(maxsize=200000)
def en_sound(t):
    t = re.sub(r"ng(?![aeiou])", "N", t.lower())
    t = re.sub(r"(.)\1+", r"\1", t); t = re.sub(r"[ts]ion", "sn", t); t = re.sub(r"c(?=[eiy])", "s", t)
    t = t.replace("ph", "p").replace("tch", "j").replace("ch", "j").replace("sh", "s").replace("th", "t").replace("ck", "k").replace("gh", "")
    t = re.sub(r"r(?![aeiouy])", "", t)
    t = t.translate(str.maketrans({"c": "k", "q": "k", "g": "k", "d": "t", "b": "p", "f": "p", "v": "p", "z": "s", "r": "l", "x": "ks"}))
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[aeiouywh]", "", t))


def _sounds_in(sk, nm):
    """a name word of 4+ letters that sounds like the loanword (or holds it, for a 4+ consonant loanword: instacartorders)"""
    return any(en_sound(t) == sk or (len(sk) >= 4 and sk in en_sound(t)) for t in re.findall(r"[a-z]{4,}", nm))


_XL_EXTRA = None


def _xl_extra():
    """more Korean words with the spellings English file names use (xling_extra.json, written by a teacher model for
    office, school, research and IT work; XLING2=1): a Korean question with one word the short list lacks matched no name
    (M1 Pro 11/9: 4 of 4 Korean-English questions not among the candidates)"""
    global _XL_EXTRA
    if _XL_EXTRA is None:
        try:
            _XL_EXTRA = json.load(open(Path(__file__).with_name("xling_extra.json"), encoding="utf-8"))
        except (OSError, ValueError):
            _XL_EXTRA = {}
    return _XL_EXTRA


def _xl_get(w):
    v = XLING.get(w)
    if v is None and os.environ.get("XLING2") == "1":
        v = _xl_extra().get(w)
    return v


# numbered parts of a course or a book, as English names write them: 3강 lec03, 2장 ch2, 1주차 week1 (Windows 11/9)
_XL_ORD = re.compile(r"(?:제\s*)?(\d{1,2})\s*(강|장|주차|회차|차시|단원|분기|학기|부|편|권)")
_XL_ORD_EN = {"강": ("lec", "lecture"), "장": ("ch", "chap", "chapter"), "주차": ("week", "wk"), "회차": ("session", "ep"),
              "차시": ("session", "lesson"), "단원": ("unit", "lesson"), "부": ("part",), "편": ("part", "vol"), "권": ("vol", "book"),
              "분기": ("q", "quarter"), "학기": ("semester", "sem", "term")}


def _xl_ordinals(q):
    """the numbered parts in a question, each an idea with its Korean and English spellings (none shorter than 4)"""
    ideas = []
    for n, suf in _XL_ORD.findall(q):
        n = int(n)
        sp = [f"{n}{suf}", f"{n:02d}{suf}"] + [s for pre in _XL_ORD_EN[suf] for s in (f"{pre}{n}", f"{pre}{n:02d}", f"{pre}_{n}", f"{pre}_{n:02d}")
                                               if len(s) >= 4 or pre == "q"]
        ideas.append(list(dict.fromkeys(sp)))
    return ideas


def xl_expand(q):
    """words to add to the word search for a question (XLING2=1): the English spellings of its Korean words (a word or the
    word without its particle, so 지도 does not come from 지도안) and the parts it numbers (3강 lec03, 1분기 q1). On a real
    Mac (11/9) a paper named by its arXiv number and a standard saved under its English title were found by neither
    name: only their English text could match"""
    out = []
    for w in re.findall(r"[가-힣]{2,}", _XL_ORD.sub(" ", q)):
        for k in (w, _XL_JOSA.sub("", w)):
            v = _xl_get(k)
            if v:
                out += v.split(); break
    out += [s for idea in _xl_ordinals(q) for s in idea[2:]]
    # no Korean words for an English question: they pulled Korean documents over the English one asked for (held-out
    # virtual PCs 11/9: two English questions from #1 to out of the list)
    return " ".join(dict.fromkeys(out))


def xling_ideas(q, relax=False):
    """the ideas of a Korean question, each with the spellings that count for it in a file name (XLING);
    [] for a question without Korean or with fewer than two ideas.
    relax: only the ideas a name can carry in English (words of the list, English words). People ask with words no
    file name has ('히스토그램 그려 보는 실습': 그려, 보는), and each of them blocked the name (field, 0.2.13: 5 of 10 out of
    the top 10)"""
    ords = _xl_ordinals(q) if os.environ.get("XLING2") == "1" else []
    if ords:
        q = _XL_ORD.sub(" ", q)
    words = re.findall(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9]+", q)
    if not (words or ords) or not (ords or any(re.match(r"[가-힣]", w) for w in words)):
        return []
    ideas = list(ords)
    for w in words:
        if re.match(r"[A-Za-z]", w):
            ideas.append([w.lower()]); continue
        stem = w if _xl_get(w) is not None else _XL_JOSA.sub("", w)
        if _xl_get(stem) == "" or _xl_get(w) == "":
            continue   # 자료, 문서, 파일 …: not an idea of the content
        if not stem:
            continue   # a particle alone (에서)
        # sound spellings are off unless XLING_SOUND=1: the GPU PC gate moved nothing, the field sets decide (2026-11-08)
        sk = "" if _xl_get(stem) or _NOT_LOAN.search(stem) or os.environ.get("XLING_SOUND") != "1" else ko_sound(stem)
        sp = (_xl_get(stem) or "").split() + (["~" + sk] if len(sk) >= 3 else [])
        if relax and (not sp or stem in XL_GENERIC):
            continue
        # relax: a word with only a sound spelling may be a native word (경사도 sounds like nothing): it counts when it matches
        ideas.append((["?"] if relax and not _xl_get(stem) else []) + [stem] + sp)
    return ideas if len(ideas) >= 2 else []


def xling_sets(q):
    """every idea first; then, if that drops some, only the ideas with an English spelling. The looser set is off unless
    XLING_RELAX=1: on the GPU PC's held-out test set it moved one answer from rank 1 to 2 (2026-11-08); field sets decide"""
    strict = xling_ideas(q)
    loose = xling_ideas(q, relax=True) if os.environ.get("XLING_RELAX") == "1" else strict
    return [i for i in (strict, loose if loose != strict else []) if i]


def _xling_hits(name, ideas):
    """for each idea, the spelling of it found in the name (None if none), with the optional flags and the bare ideas"""
    nm = unicodedata.normalize("NFC", name or "").lower()
    opt = [idea[:1] == ["?"] for idea in ideas]
    ideas = [idea[1:] if o else idea for idea, o in zip(ideas, opt)]
    hit = [next((sp for sp in idea if (_sounds_in(sp[1:], nm) if sp[:1] == "~" else sp in nm if len(sp) > 3 or re.match(r"[가-힣]", sp)
                                       else re.search(rf"(?<![a-z]){re.escape(sp)}(?![a-z])", nm))), None) for idea in ideas]
    return hit, opt, ideas


def xling_cover(name, ideas):
    """(the name carries every idea, at least one of them by an English translation or sound); an idea starting with '?'
    is optional, and at least two ideas must be carried"""
    hit, opt, ideas = _xling_hits(name, ideas)
    full = all(h or o for h, o in zip(hit, opt)) and sum(bool(h) for h in hit) >= 2
    return full, any(h and h != idea[0] for h, idea in zip(hit, ideas))


def xling_partial(name, ideas):
    """XLING_PARTIAL=1 (off by default): how many ideas the name carries by English translation, when it carries at least
    half of a 3+ idea question and at least two of them in English; else 0. A file name is short and people describe more
    than it holds: a question names the product, the kind of device and what the file is ('<brand> 스마트폰 광고 문구'), the
    name keeps the brand and one word for the content ('<Brand>_<model>_copy deck'). Every-idea rules then never fire"""
    if len(ideas) < 3:
        return 0
    hit, _, ideas = _xling_hits(name, ideas)
    en = sum(1 for h, idea in zip(hit, ideas) if h and h != idea[0])
    return en if en >= 2 and sum(bool(h) for h in hit) * 2 >= len(ideas) else 0


def xling_carried(name, ideas):
    """how many ideas of the question the name carries, in either script"""
    return sum(bool(h) for h in _xling_hits(name, ideas)[0])


def xling_name_first(q, res, top=10):
    """index (in res[:top], > 0) of the file whose name covers every idea of a Korean question through XLING,
    when at least one idea needed a translation and #1's name does not cover them; else 0. Every idea first; the English-
    spelled ideas alone only when no name in the top carries every idea ('원격 설계 메모.txt' for '원격 설계 메모' stays)"""
    if not res:
        return 0
    for ideas in xling_sets(q):
        if xling_cover(res[0].get("name"), ideas)[0]:
            return 0
        for j, x in enumerate(res[1:top], 1):
            if xling_cover(x.get("name"), ideas) == (True, True):
                return j
        if any(xling_cover(x.get("name"), ideas)[0] for x in res[1:top]):
            return 0
    return 0


_NAME_STOP = {"파일", "문서", "자료", "찾아", "찾아줘", "줘", "주세요", "그", "거", "것", "좀", "있는", "관련", "어디", "있어", "있나요"}


def name_words(q):
    """the words a system file search would look for in names: the question's words without particles' noise
    (2+ letters, no request words); none when the question reads like a sentence (5+ words): then it is not a name search"""
    ws = [w.lower() for w in re.findall(r"[0-9A-Za-z가-힣]{2,}", unicodedata.normalize("NFC", q or "")) if w not in _NAME_STOP]
    return ws if 0 < len(ws) <= 4 else []


def refine_skip(res):
    """why the page's side-by-side second pass is not worth it now ('' = run it). About 1 s on an NVIDIA GPU, so it always runs
    there; on an Apple GPU or a CPU it takes ~10 s (#20): only when the top two are close, and not while indexing runs.
    FINDR_REFINE=always|off overrides (off: no checker model on this PC)."""
    m = os.environ.get("FINDR_REFINE", "auto")
    if m == "off":
        return "off"
    if m == "always" or DEVICE == "cuda":
        return ""
    try:
        p = json.loads((Path(os.environ.get("FINDR_DATA") or Path.home() / ".findr") / "progress.json").read_text(encoding="utf-8"))
        if p.get("stage") not in (None, "done") and time.time() - p.get("t", 0) < 900 and indexer_alive(p):
            return "indexing"
    except (OSError, ValueError):
        pass
    ps = [x.get("prob") for x in res["results"][:2]]
    if None not in ps and ps[0] >= 0.6 and ps[0] - ps[1] >= 0.3:
        return "clear"
    return ""


def distinct_words(ws):
    """one idea counts once among expansions: a word inside another word of the list is dropped. GLOSS turns 생성 into
    "gen generate", and the name rule counting 'two question words' then took every Gemini_Generated_Image.png as a match (#32).
    Used on the English expansions only: the person's own words stay as typed (보고서 and 월간보고서 are both kept)."""
    ws = list(dict.fromkeys(ws))
    return [w for w in ws if not any(w != v and w in v for v in ws)]


def _has_facts(f):
    """the understanding-first route reads work facts; an index built without them uses the reranker route"""
    # "no facts" is asked again every 5 minutes: a server started while the first index was still running kept 이해 우선 off
    # until a restart
    if getattr(f, "_facts_ok", None) is None or (not f._facts_ok and time.time() - getattr(f, "_facts_t", 0) > 300):
        f._facts_t = time.time()
        try:
            f._facts_ok = bool(f.con.execute("SELECT 1 FROM facts LIMIT 1").fetchone())
        except Exception:
            f._facts_ok = False
    return f._facts_ok


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


_ST = threading.local()   # per-request stage times, for the slow-question line
SLOW_MS = int(os.environ.get("FINDR_SLOW_MS") or 1500)


def _stage_timers():
    """time the main stages of a search, so a rare slow question can say where its time went (FINDR_SLOW, no question text)"""
    def wrap(owner, name):
        fn = getattr(owner, name, None)
        if fn is None or getattr(fn, "_timed", False):
            return
        def w(*a, **kw):
            t = time.time()
            try:
                return fn(*a, **kw)
            finally:
                d = getattr(_ST, "d", None)
                if d is not None:
                    d[name] = d.get(name, 0.0) + time.time() - t
        w._timed = True
        setattr(owner, name, w)
    for n in ("refresh", "qvec", "_search", "name_floor", "version_first"):
        wrap(Finder, n)
    mod = sys.modules[__name__]
    for n in ("parse", "_epi_read", "ce_scores", "add_probs"):
        wrap(mod, n)
    import gc
    gc_t = {}
    def on_gc(phase, info):   # a collection pauses whatever stage is running: count it on its own
        if phase == "start":
            gc_t[threading.get_ident()] = time.time()
        else:
            t = gc_t.pop(threading.get_ident(), None); d = getattr(_ST, "d", None)
            if t is not None and d is not None:
                k = f"gc{info.get('generation', '')}"; d[k] = d.get(k, 0.0) + time.time() - t
    gc.callbacks.append(on_gc)


RESULTS = collections.OrderedDict()   # whole answers by question and state: people ask the same things again (#R0)
RESULTS_MAX = int(os.environ.get("RESULT_CACHE") or 300)   # 0: off
RESULTS_LOCK = _th.Lock()   # the server answers on several threads


def result_key(f, q, mode, k, prev, pin):
    """the key of a whole answer, or None (cache off). It holds what can change the answer: the question, the route, how
    many, the question before, a pinned 'today' and the day itself (relative dates), the index (SQLite data_version moves
    when the indexer commits), and the personal memory (a choice made since lifts another file). On a Mac a repeated
    question still took 1.3-1.5 s (only some reranker scores cached); the same answer comes back at once"""
    if not RESULTS_MAX:
        return None
    try:
        ver = f.con.execute("PRAGMA data_version").fetchone()[0]
    except Exception:   # noqa: BLE001
        return None
    pm = personal()
    mem = len(getattr(pm, "rows", []) or []) if pm is not None else 0
    flags = tuple(os.environ.get(x, "") for x in ("KIND_GATE", "DUP_COLLAPSE", "TIME_WORDS", "CE_TAIL", "CE_MATCHED", "VERSION_GROUP", "XLING2"))
    return (q, mode, k, prev, pin, dt.date.today().isoformat(), ver, mem, flags)


def _slow_line(res, mode, total):
    d = getattr(_ST, "d", None) or {}
    st = {k: int(v * 1000) for k, v in sorted(d.items(), key=lambda kv: -kv[1]) if v >= 0.01 and k != "ce_n"}
    extra = {"ce_n": int(d["ce_n"])} if "ce_n" in d else {}   # a count, not a time
    line = "FINDR_SLOW " + json.dumps({"ms": int(total * 1000), "route": mode, "n": len(res.get("results") or []), **extra, "stages": st})
    print(line, flush=True)
    try:   # also in the data folder's logs, where the tester guide looks (#67: CLI serve printed it to the terminal only)
        lg = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "logs" / "slow.log"
        lg.parent.mkdir(parents=True, exist_ok=True)
        if lg.exists() and lg.stat().st_size > 1_000_000:
            lg.unlink()
        with open(lg, "a", encoding="utf-8") as fo:
            fo.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def _local_request(h):
    """a browser on this PC itself: only then may the page open files here. A request through a tunnel or proxy carries
    its forwarding header and is refused, even though it arrives from 127.0.0.1"""
    return h.client_address[0] in ("127.0.0.1", "::1") and not any(
        h.headers.get(x) for x in ("CF-Connecting-IP", "X-Forwarded-For", "Forwarded", "X-Real-IP"))


def proc_memory():
    """this engine's memory now and at its peak (MB), and the GPU's peak when one is in use: the field reports had a 20 GB
    engine with no number to compare against (report the peak, not a plan)"""
    out = {}
    try:
        if sys.platform.startswith("linux"):
            for line in open("/proc/self/status", encoding="utf-8"):
                if line.startswith(("VmRSS:", "VmHWM:")):
                    out["rss_mb" if line.startswith("VmRSS") else "peak_mb"] = int(line.split()[1]) // 1024
        elif os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                            ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            m = PMC(); m.cb = ctypes.sizeof(PMC)
            k32 = ctypes.windll.kernel32; k32.GetCurrentProcess.restype = wintypes.HANDLE
            gpmi = ctypes.windll.psapi.GetProcessMemoryInfo   # the 64-bit handle needs its type spelled out (OverflowError)
            gpmi.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]; gpmi.restype = wintypes.BOOL
            if gpmi(k32.GetCurrentProcess(), ctypes.byref(m), m.cb):
                out = {"rss_mb": m.WorkingSetSize // 2**20, "peak_mb": m.PeakWorkingSetSize // 2**20}
        else:   # macOS: the peak (ru_maxrss is in bytes there)
            import resource
            out["peak_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 2**20
    except Exception:   # noqa: BLE001  (a number we cannot read is left out)
        pass
    try:
        if "torch" in sys.modules and sys.modules["torch"].cuda.is_initialized():
            out["gpu_peak_mb"] = sys.modules["torch"].cuda.max_memory_allocated() // 2**20
    except Exception:   # noqa: BLE001
        pass
    return out


def open_on_pc(path, reveal=False):
    """open a file with its program, or show it in its folder, as the desktop app does"""
    import subprocess
    p = str(Path(path))
    if not Path(p).exists():   # moved or deleted since it was indexed: say so (macOS "open" reported success for nothing)
        raise FileNotFoundError(p)
    if os.name == "nt":
        if reveal:
            subprocess.Popen(["explorer", f"/select,{p}"])
        else:
            os.startfile(p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", p] if reveal else ["open", p])
    else:
        subprocess.Popen(["xdg-open", str(Path(p).parent) if reveal else p])


def serve(f, port):
    _stage_timers()
    global GOV
    try:   # the resource manager: observes this PC (profile, memory, CPU, power, heat) and says what it would do (/status)
        from governor import Governor
        GOV = Governor(dev=DEVICE, log=lambda s: print(s, flush=True)); GOV.start()
    except Exception as e:   # noqa: BLE001  (search works without it)
        print(f"FINDR_GOV off {type(e).__name__}", flush=True)

    class H(BaseHTTPRequestHandler):
        def reply(self, code, body, ct):
            self.send_response(code); self.send_header("Content-Type", ct)
            for k_, v_ in SEC_HEADERS:
                self.send_header(k_, v_)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path == "/healthz":   # for the watchdog: says nothing but "ok"
                return self.reply(200, b"ok", "text/plain")
            if urlparse(self.path).path == "/ready":   # 'answers' is not 'ready' (#68): the page says so while the engine warms up
                return self.reply(200, json.dumps({"ready": READY.is_set()}).encode(), "application/json")
            if urlparse(self.path).path == "/canopen":   # the page shows 열기 / 폴더 only in a browser on this PC
                if not self.authed():
                    return
                return self.reply(200, json.dumps({"local": _local_request(self)}).encode(), "application/json")
            if urlparse(self.path).path == "/progress":   # indexing still running: the page shows where it stands
                if not self.authed():
                    return
                data_ = Path(os.environ.get("FINDR_DATA") or Path.home() / ".findr")
                try:
                    p_ = json.loads((data_ / "progress.json").read_bytes())
                    if not indexer_alive(p_):
                        p_ = {}   # the indexer was stopped: no '색인 진행 중' banner left behind
                    else:
                        # paused is the pause file itself: a paused indexer waits without writing progress, and a page opened
                        # after a restart showed '진행 중' with a pause button that paused it again (field, Mac)
                        p_["paused"] = (data_ / "pause").exists()
                except (OSError, ValueError):
                    p_ = {}
                # #106: in the app, the page offers '지금 반영' and says when new files were last taken in, idle or not
                if os.environ.get("FINDR_APP") == "1":
                    p_["app"] = True
                    try:
                        p_["last_refresh"] = int(float((data_ / "last_refresh").read_text().strip()))
                    except (OSError, ValueError):
                        pass
                    p_["refresh_asked"] = (data_ / "refresh_now").exists()
                return self.reply(200, json.dumps(p_, ensure_ascii=False).encode(), "application/json; charset=utf-8")
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
                # paths whole (cut at 300 they matched no file on deep Windows folders, so ✔/✘ was never learned)
                rec = {k: qs.get(k, [""])[0][:4096 if k == "path" else MAX_Q] for k in ("q", "mode", "path", "rank", "verdict", "prob", "first_rank", "secs", "page")}
                rec["shown"] = [x[:4096] for x in qs.get("shown", [""])[0].split(chr(10)) if x][:30]   # what the person saw, in order
                rec["tried"] = [x[:MAX_Q] for x in qs.get("tried", [""])[0].split(chr(10)) if x][:8]   # earlier requests in this session
                fbp = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "feedback.jsonl"
                if rec["verdict"] not in ("hit", "none", "reject"):
                    return self.reply(400, b'{"ok": false}', "application/json")
                if fbp.exists() and fbp.stat().st_size > 5_000_000:   # set aside, keep learning (it used to refuse every click from then on)
                    fbp.rename(fbp.with_name(f"feedback.{dt.datetime.now():%Y%m%d%H%M%S}.jsonl"))
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
                    # back to /learned: a reload of /learned/forget?i=N forgot the entry that had moved into slot N
                    self.send_response(303); self.send_header("Location", "/learned"); self.send_header("Content-Length", "0")
                    self.end_headers(); return
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
                skip = refine_skip(res)
                if skip:   # the order is already clear, or indexing needs the memory: the page just drops its note
                    return self.reply(200, json.dumps({"changed": False, "skipped": skip}).encode(), "application/json; charset=utf-8")
                import explore as XP
                with SEARCH_LOCK:
                    prompt, n = XP.v2_prompt(f, q_, res, today_now())
                out = XP._chat(XP.SYS2, prompt, 80)
                if not out:   # the checker did not answer (not running, timed out): the page must not say it looked
                    return self.reply(200, json.dumps({"changed": False, "skipped": "checker"}).encode(), "application/json; charset=utf-8")
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
                no_facts = mode == "ng" and not _has_facts(f)   # '이해 우선' on an index without work facts: no such table -> 500 (#32)
                if no_facts:
                    mode = "xce"   # the reranker answers, and route / the /refine cache say so
                t_req = time.time()
                rkey = result_key(f, q_, mode, k, prev_, self.headers.get("X-Findr-Today"))
                with RESULTS_LOCK:
                    hit = RESULTS.get(rkey) if rkey else None
                    if hit is not None:
                        RESULTS.move_to_end(rkey)
                if hit is not None:   # the same question again, nothing changed since: the same answer at once
                    res = json.loads(hit); res["cached"] = True; res["ms"] = int((time.time() - t_req) * 1000)
                    return self.reply(200, json.dumps(res, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
                _ST.d = {}
                with SEARCH_LOCK:
                  _ST.d["lock_wait"] = time.time() - t_req
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
                wait_ = (_ST.d or {}).get("lock_wait", 0.0)
                if wait_ >= 0.05:   # the time the person waited counts: a question queued behind another one or the warm-up
                    res["wait_ms"] = int(wait_ * 1000); res["ms"] = int(res.get("ms") or 0) + res["wait_ms"]   # (Mac 에어: 13.9 s shown as 5.1)
                if no_facts:
                    res["info"] = "이 PC의 색인에는 업무 정보(사람·기관·금액 등)가 없어 정밀 검색으로 찾았습니다."
                if personal() is not None and res.get("results"):
                    with SEARCH_LOCK:
                        res = personal().apply(f, q_, res)
                res = add_probs(res)
                if res.get("results") and os.environ.get("TIME_WORDS", "1") == "1":
                    try:   # the same 'today' the search used (an evaluation pins it; EVAL_NOW is already reset here)
                        now_ = dt.datetime.fromisoformat(self.headers.get("X-Findr-Today")) if self.headers.get("X-Findr-Today") else None
                    except ValueError:
                        now_ = None
                    res["results"] = period_first(q_, res["results"], now=now_)
                    p_ = parse(q_, now=now_)
                    if p_["since"] and res["results"] and not (p_["since"].strftime("%Y-%m-%d %H:%M") <= (res["results"][0].get("date") or "")
                                                               < (p_["until"].strftime("%Y-%m-%d %H:%M") if p_["until"] else "9999")):
                        # nothing of that period among the answers: said as information beside the list, not as 'nothing here'
                        # (a Mac with no files from last year showed other years silently; a note counted as 'none' at the gate)
                        msg_ = "말씀하신 기간의 파일은 찾지 못해, 다른 시기의 비슷한 파일을 보여 드려요."
                        res["info"] = (res["info"] + " " + msg_) if res.get("info") else msg_
                if os.environ.get("DUP_COLLAPSE", "1") == "1":
                    with SEARCH_LOCK:
                        res = collapse_copies(f.con, res)
                # on by default after the sealed sets of two real PCs (11/9: 38 -> 40 of 58 at #1, none worse by it); =0 turns it off
                if os.environ.get("VERSION_GROUP", "1") == "1":
                    with SEARCH_LOCK:
                        res = group_versions(f.con, res)
                if res.get("results") and os.environ.get("KIND_GATE", "1") == "1":
                    res["results"] = format_first(q_, res["results"])
                # the page's '서버' time: the whole request, not the search alone (a Mac page showed 서버 3.4 s · 화면 7.1 s and
                # the rest had no name: the index refresh check before the search and the steps after it were left out)
                res["search_ms"] = int(res.get("ms") or 0); res["ms"] = max(res["search_ms"], int((time.time() - t_req) * 1000))
                if time.time() - t_req > SLOW_MS / 1000:
                    _slow_line(res, mode, time.time() - t_req)
                _ST.d = None
                if len(_LAST) > 50:
                    _LAST.clear()
                _LAST[(q_, mode)] = res
                body = json.dumps(res, ensure_ascii=False, default=str).encode()
                if rkey:
                    with RESULTS_LOCK:
                        RESULTS[rkey] = body
                        while len(RESULTS) > RESULTS_MAX:
                            RESULTS.popitem(last=False)
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
                    body = json.dumps({**f.stats(), **proc_memory()}).encode(); ct = "application/json"
            elif u.path == "/status":   # this PC as findr sees it now: tier, signals, pressure, what it would do
                body = json.dumps(GOV.state() if GOV is not None else {}, ensure_ascii=False).encode(); ct = "application/json; charset=utf-8"
            else:
                pg = Path(__file__).with_name("page.html")
                body = (pg.read_text(encoding="utf-8") if pg.exists() else PAGE).encode(); ct = "text/html; charset=utf-8"
            self.reply(200, body, ct)

        def do_POST(self):
            if not self.authed():
                return
            if self.headers.get("X-Findr") == "1" and urlparse(self.path).path == "/embed":
                # document vectors for the indexer, from the encoder this engine already holds: one copy of the model on the
                # PC instead of two (the indexer's own copy, with torch, was 1.7 of its 1.75 GB; with the engine's 2.7 GB it
                # went past what an 8 GB laptop leaves and the OS killed the engine). Same vectors as the indexer's own:
                # the document side, 512 tokens.
                try:
                    r = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4 << 20)) or b"{}")
                    texts, name = [str(t) for t in r["texts"]][:64], str(r.get("encoder") or "")
                except Exception:
                    return self.reply(400, b'{"ok": false}', "application/json")
                enc = getattr(f, "enc", None)
                if enc is None or name != getattr(f, "encoder", None) or not hasattr(enc, "st"):
                    return self.reply(409, json.dumps({"ok": False, "why": "encoder", "have": getattr(f, "encoder", None)}).encode(), "application/json")
                import base64
                with SEARCH_LOCK:   # the encoder is shared with the questions: one short batch at a time
                    old = enc.st.max_seq_length
                    try:
                        enc.st.max_seq_length = 512
                        v = enc.emb(texts, query=False).cpu().numpy().astype("float16")
                    finally:
                        enc.st.max_seq_length = old
                return self.reply(200, json.dumps({"ok": True, "dim": int(v.shape[1]) if len(v) else 0,
                                                   "vecs": base64.b64encode(v.tobytes()).decode()}).encode(), "application/json")
            if self.headers.get("X-Findr") == "1" and urlparse(self.path).path == "/pause":
                try:
                    on = bool(json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 256)) or b"{}").get("on"))
                except Exception:
                    return self.reply(400, b'{"ok": false}', "application/json")
                f_ = Path(os.environ.get("FINDR_DATA") or Path.home() / ".findr") / "pause"
                f_.write_text("1") if on else f_.unlink(missing_ok=True)
                return self.reply(200, json.dumps({"ok": True, "paused": on}).encode(), "application/json")
            if self.headers.get("X-Findr") == "1" and urlparse(self.path).path == "/refresh":
                # '지금 반영' (#106): the app's refresher looks for this file every few seconds and takes in new files at once
                f_ = Path(os.environ.get("FINDR_DATA") or Path.home() / ".findr") / "refresh_now"
                f_.write_text(str(int(time.time())))
                return self.reply(200, b'{"ok": true}', "application/json")
            if self.headers.get("X-Findr") == "1" and urlparse(self.path).path == "/open":   # 열기 / 폴더 in a browser on this PC
                if not _local_request(self):
                    return self.reply(403, b'{"ok": false, "why": "remote"}', "application/json")
                try:
                    r = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 8192)) or b"{}")
                    path_, reveal = str(r["path"]), bool(r.get("reveal"))
                except Exception:
                    return self.reply(400, b'{"ok": false}', "application/json")
                with SEARCH_LOCK:
                    known = f.con.execute("SELECT 1 FROM files WHERE path=?", (path_,)).fetchone()
                if not known:   # only files of the index: the page cannot be used to start arbitrary programs
                    return self.reply(404, b'{"ok": false, "why": "not indexed"}', "application/json")
                try:
                    open_on_pc(path_, reveal)
                except OSError as e:
                    return self.reply(500, json.dumps({"ok": False, "why": type(e).__name__}).encode(), "application/json")
                return self.reply(200, b'{"ok": true}', "application/json")
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
    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    if os.environ.get("FINDR_READY_LINE"):
        print(os.environ["FINDR_READY_LINE"], flush=True)
    if os.environ.get("FINDR_WARM", "1") == "1":   # the first question loaded the reranker and GPU kernels: 2.3 s instead of 0.2 s
        def warm():
            try:
                t = time.time()
                # the reranker at full size (NCAND candidates, the longest input): it loads, compiles its kernels for that
                # shape and, on a CPU, measures its rate once. A plain search may score few candidates, or none on an index
                # still being built (#66: FINDR_WARM 0.1s, then 20 s on the first question; #67: 3 s more on Apple GPU).
                # Outside the search lock: it uses no index, so fast-mode questions are answered meanwhile (Mac 에어: 3.4 s wait)
                ce_scores("지난달 회의 자료", [{"name": "warm.txt", "path": "/warm/warm.txt", "head": "가나다라 " * 400,
                                               "summary": ""} for _ in range(NCAND)])
                with SEARCH_LOCK:
                    f.search("지난달 회의 자료", mode="xce", k=10)
                    if getattr(f, "ng", None) is None and _has_facts(f):   # understanding-first route: its planner loads once
                        from ng import NG
                        f.ng = NG(f)
                print(f"FINDR_WARM {time.time() - t:.1f}s", flush=True)
            except Exception as e:   # a failed warm-up only means the first question is slower
                print(f"FINDR_WARM failed {type(e).__name__}", flush=True)
            finally:
                READY.set()
        threading.Thread(target=warm, daemon=True).start()
    else:
        READY.set()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:   # Ctrl+C: one line, not a traceback (#67); the helper servers stop with the process
        print("검색 화면을 껐습니다.", flush=True)


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
