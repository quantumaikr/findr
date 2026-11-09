"""What kind of document a file is (minutes, contract, report, plan, application form, ...), read from the vector the index
already stores for each file's first piece ('name | folders' + opening text). No extra model and no extra encoding: a small
linear head per index encoder (doctype_heads.json, trained only on written examples, never on anyone's files).
Measured on real PCs it never saw (11/9): 0.73 with the GPU encoder, 0.50-0.72 with the small CPU encoder; name rules
alone were 0.24-0.58 there. With the small encoder a name model (character n-grams of the name and folders, no encoder)
is mixed in: 0.55 -> 0.69 on two unseen PCs, as good as the GPU encoder there, at no extra time.
Used as a filter an AI agent asks for, not as a ranking signal."""
import base64
import json
import math
import re
import sqlite3
import unicodedata
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


_NAMES = None


def _names():
    """the name model: n-gram -> column, idf, weights (int8 + scale) and bias"""
    global _NAMES
    if _NAMES is None:
        h = heads().get("names")
        if not h:
            _NAMES = False
        else:
            d = lambda k, t: np.frombuffer(base64.b64decode(h[k]), dtype=t).astype(np.float32)
            n = len(heads()["labels"]); v = len(h["grams"])
            _NAMES = ({g: i for i, g in enumerate(h["grams"])}, d("idf", np.float16),
                      d("w", np.int8).reshape(n, v) * d("scale", np.float16)[:, None], d("bias", np.float16))
    return _NAMES


def name_text(path):
    """name | last three folders; NFC, as macOS keeps paths decomposed and the model learned composed Korean"""
    parts = unicodedata.normalize("NFC", str(path)).replace("\\", "/").split("/")
    return f"{parts[-1]} | {' '.join(parts[-4:-1])}"


def _grams(text):
    """character 2-4-grams inside each word padded with spaces, as the name model was trained (sklearn char_wb)"""
    out = []
    for w in text.lower().split():
        w = f" {w} "
        for n in (2, 3, 4):
            if len(w) <= n:
                out.append(w)
                break
            out += [w[i:i + n] for i in range(len(w) - n + 1)]
    return out


def name_probs(path):
    """probabilities over the kinds from the file's name and last folders alone; None without a name model"""
    m = _names()
    if not m:
        return None
    col, idf, w, b = m
    tf = {}
    for g in _grams(name_text(path)):
        if g in col:
            tf[col[g]] = tf.get(col[g], 0) + 1
    z = b.copy()
    if tf:
        ix = np.fromiter(tf, dtype=np.int64)
        x = np.array([1 + math.log(tf[i]) for i in tf], dtype=np.float32) * idf[ix]
        z = z + w[:, ix] @ (x / np.linalg.norm(x))
    p = np.exp(z - z.max())
    return p / p.sum()


def of_files(con, fids):
    """{file id: (kind, probability)} from the stored first-piece vectors; {} when this index's encoder has no head"""
    labels = heads()["labels"]
    return {f: (labels[int(p.argmax())], float(p.max())) for f, p in probs(con, fids).items()}


def chance_of(p, kinds):
    """how likely a file (its probabilities) is one of these kinds"""
    labels = heads()["labels"]
    return float(sum(p[labels.index(k)] for k in kinds if k in labels))


def probs(con, fids):
    """{file id: probabilities over the kinds (heads()['labels'] order)}"""
    try:
        r = con.execute("SELECT value FROM meta WHERE key='encoder'").fetchone() if con else None
    except sqlite3.OperationalError:   # an index without a meta table (very old, or a test's)
        r = None
    hd = _head(r[0] if r else None)
    if hd is None or not fids:
        return {}
    w, b = hd
    nw = float(heads()["heads"][r[0]].get("name_weight", 0))
    out = {}
    q = ",".join("?" * len(fids))
    try:
        rows = con.execute(f"SELECT c.file, c.emb, f.path FROM chunks c JOIN files f ON f.id=c.file "
                           f"WHERE c.n=0 AND c.emb IS NOT NULL AND c.file IN ({q})", list(fids)).fetchall()
    except sqlite3.OperationalError:
        return {}
    for fid, emb, path in rows:
        v = np.frombuffer(emb, dtype=np.float16).astype(np.float32)
        if v.shape[0] != w.shape[1]:
            continue
        z = w @ v + b
        p = np.exp(z - z.max()); p /= p.sum()
        pn = name_probs(path) if nw else None
        if pn is not None:   # a product of the two, the name model at weight nw
            p = np.exp((1 - nw) * np.log(p + 1e-9) + nw * np.log(pn + 1e-9)); p /= p.sum()
        out[fid] = p
    return out
