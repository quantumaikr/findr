"""first index: fast and rough, then filled in. Name vectors before any content; the opening of each document first and the
rest on the deep run; the properties the saving program wrote; scans on a CPU wait for the deep run."""
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
import enc_eg2  # noqa: E402
import index  # noqa: E402

CORE = Path(index.__file__).parent


def scan(root, db, deep=False):
    env = {**os.environ, "FINDR_OCR": "0", "PYTHONUTF8": "1"}
    env.pop("FINDR_FIRST_PIECES", None)
    if not deep:
        env["FINDR_FIRST_PIECES"] = "8"
    r = subprocess.run([sys.executable, str(CORE / "index.py"), "--root", str(root), "--db", str(db), "--no-embed"],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    return sqlite3.connect(str(db))


def test_opening_first_rest_later(tmp_path):
    d = tmp_path / "docs"; d.mkdir()
    (d / "긴_보고서.txt").write_text("앞부분 요약 " + "본문 내용입니다. " * 6000 + " 맨끝의고유단어", encoding="utf-8")
    (d / "짧은_메모.txt").write_text("짧은 메모", encoding="utf-8")
    con = scan(d, tmp_path / "i.db")
    long_ = con.execute("SELECT id, pending FROM files WHERE name='긴_보고서.txt'").fetchone()
    assert long_[1] == 2   # read in part: the deep run finishes it
    assert con.execute("SELECT pending FROM files WHERE name='짧은_메모.txt'").fetchone()[0] == 0
    n1 = con.execute("SELECT sum(length(text)) FROM chunks WHERE file=?", (long_[0],)).fetchone()[0]
    assert n1 <= index.GIST + 2000
    con.close(); con = scan(d, tmp_path / "i.db", deep=True)
    assert con.execute("SELECT pending FROM files WHERE id=?", (long_[0],)).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM chunks WHERE file=? AND text LIKE '%맨끝의고유단어%'", (long_[0],)).fetchone()[0] >= 1   # (pieces overlap)


def test_document_properties(tmp_path):
    p = tmp_path / "보고서.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:t>본문</w:t></w:document>")
        z.writestr("docProps/core.xml", '<cp:coreProperties xmlns:dc="x" xmlns:cp="y"><dc:title>2025 스마트팩토리 구축 제안서</dc:title>'
                                        '<dc:subject></dc:subject><cp:keywords>MES, 센서</cp:keywords><dc:creator>user</dc:creator></cp:coreProperties>')
    t = index.ooxml_text(p, 10000)
    assert t.startswith("제목: 2025 스마트팩토리 구축 제안서 | 키워드: MES, 센서") and "작성자" not in t   # 'user' is generic
    assert index.props_line("Microsoft Word - 문서1", None, None, None) == ""


class Fake(enc_eg2.EG2):
    def __init__(self, *a, **k):
        self.st = None; self.bs = 8; self.dev = "cpu"

    def emb(self, texts, bs=None, query=True):
        return torch.nn.functional.normalize(torch.ones(len(texts), 8), dim=-1)


def test_name_vectors_before_content(tmp_path, monkeypatch):
    d = tmp_path / "docs"; d.mkdir()
    for n in ("견적서_스마트센서.txt", "여름휴가_사진.jpg", "회의록.md"):
        (d / n).write_text("내용", encoding="utf-8")
    con = scan(d, tmp_path / "i.db")   # names and content, no vectors yet
    con.execute("UPDATE files SET pending=1 WHERE ext!='.jpg'"); con.execute("UPDATE chunks SET text=''"); con.commit()   # as right after the file list
    monkeypatch.setattr(enc_eg2, "make_encoder", lambda *a, **k: Fake())
    con.execute("INSERT OR REPLACE INTO meta VALUES('encoder','e5-small')"); con.commit()
    index.embed(con, "e5-small", names_only=True)
    assert con.execute("SELECT count(*) FROM chunks WHERE n=0 AND emb IS NOT NULL").fetchone()[0] == 3   # every file, read or not
