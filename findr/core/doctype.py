"""What kind of document a file is (minutes, contract, report, plan, application form, ...), read from the vector the index
already stores for each file's first piece ('name | folders' + opening text). No extra model and no extra encoding: a small
linear head per index encoder (doctype_heads.json, trained only on written examples, never on anyone's files).
Measured on real PCs it never saw (11/9): 0.73 with the GPU encoder, 0.50-0.72 with the small CPU encoder; name rules
alone were 0.24-0.58 there. Used as a filter an AI agent asks for, not as a ranking signal."""
import base64
import json
import re
import sqlite3
from pathlib import Path

import numpy as np

_HEADS = None

# words a person or an agent may use for each kind (Korean and English); matched against a kind filter
ALIASES = {
    "발표자료": "발표 발표자료 슬라이드 presentation deck ir 피치",
    "회의록": "회의록 회의 미팅 minutes meeting 의사록",
    "계약서·협약서": "계약 계약서 협약 협약서 mou nda agreement contract",
    "견적·발주·거래명세": "견적 견적서 발주 발주서 거래명세 거래명세서 세금계산서 청구서 invoice quote quotation po",
    "보고서": "보고서 결과보고 보고 report",
    "계획서·제안서": "계획서 계획 제안서 제안 기획서 기획안 예산서 proposal plan",
    "신청서·지원서": "신청서 지원서 이력서 application resume cv",
    "공문·안내·공지": "공문 안내 안내문 공지 공고 보도자료 가정통신문 notice letter",
    "규정·정관·매뉴얼": "규정 정관 규칙 지침 매뉴얼 설명서 약관 기준서 법령 manual rules policy regulation standard",
    "명단·목록·대장": "명단 목록 대장 출석부 명부 주소록 list roster",
    "서식·양식": "서식 양식 템플릿 form template",
    "증명·확인·승낙서": "증명서 증명 확인서 승낙서 각서 서약서 수료증 이수증 certificate",
    "강의·교육자료·논문": "강의 강의자료 교재 교안 교육자료 논문 시험지 기출 lecture paper thesis",
    "기타": "기타 other",
}


def heads():
    global _HEADS
    if _HEADS is None:
        _HEADS = json.loads((Path(__file__).parent / "doctype_heads.json").read_text(encoding="utf-8"))
    return _HEADS


def match(kind):
    """the document kinds a filter names ('회의록', 'contract,report'); [] when it names none (a format like 'pdf')"""
    out = []
    for w in re.split(r"[,\s]+", str(kind or "").lower()):
        if not w:
            continue
        for k, a in ALIASES.items():
            if (w == k.lower() or w in a.split()) and k not in out:
                out.append(k)
    return out


def _head(encoder):
    h = heads()["heads"].get(encoder or "")
    if not h:
        return None
    n = len(heads()["labels"])
    w = np.frombuffer(base64.b64decode(h["w"]), dtype=np.int8).astype(np.float32).reshape(n, h["dim"])
    s = np.frombuffer(base64.b64decode(h["scale"]), dtype=np.float16).astype(np.float32)
    b = np.frombuffer(base64.b64decode(h["bias"]), dtype=np.float16).astype(np.float32)
    return w * s[:, None], b


def of_files(con, fids):
    """{file id: (kind, probability)} from the stored first-piece vectors; {} when this index's encoder has no head"""
    try:
        r = con.execute("SELECT value FROM meta WHERE key='encoder'").fetchone() if con else None
    except sqlite3.OperationalError:   # an index without a meta table (very old, or a test's)
        r = None
    hd = _head(r[0] if r else None)
    if hd is None or not fids:
        return {}
    w, b = hd
    labels = heads()["labels"]
    out = {}
    q = ",".join("?" * len(fids))
    try:
        rows = con.execute(f"SELECT file, emb FROM chunks WHERE n=0 AND emb IS NOT NULL AND file IN ({q})", list(fids)).fetchall()
    except sqlite3.OperationalError:
        return {}
    for fid, emb in rows:
        v = np.frombuffer(emb, dtype=np.float16).astype(np.float32)
        if v.shape[0] != w.shape[1]:
            continue
        z = w @ v + b
        p = np.exp(z - z.max()); p /= p.sum()
        i = int(p.argmax())
        out[fid] = (labels[i], float(p[i]))
    return out
