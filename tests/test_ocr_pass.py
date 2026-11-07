"""OCR passes (#21): copies are read once; the first pass reads the first pages, a deep run the rest"""
import shutil
import sqlite3
import sys

import pypdfium2 as pdfium

import index


def pdf(path, pages):
    d = pdfium.PdfDocument.new()
    for _ in range(pages):
        d.new_page(300, 400)
    d.save(str(path)); d.close()
    return path


def run(monkeypatch, root, db, deep):
    calls = []
    monkeypatch.setattr(index, "ocr_engine", lambda: "vision")
    monkeypatch.setattr(index, "ocr_lines", lambda img: calls.append(1) or ["견적서 스마트센서 납품 단가 조건"])
    monkeypatch.setenv("FINDR_OCR", "1")
    if deep:
        monkeypatch.delenv("FINDR_FIRST_PIECES", raising=False)
    else:
        monkeypatch.setenv("FINDR_FIRST_PIECES", "8")
    monkeypatch.setattr(sys, "argv", ["index.py", "--root", str(root), "--db", str(db), "--no-embed"])
    index.main()
    return len(calls)


def test_first_pages_then_deep_and_copies(tmp_path, monkeypatch):
    root = tmp_path / "docs"; root.mkdir()
    pdf(root / "스캔_견적서.pdf", 7)
    shutil.copy(root / "스캔_견적서.pdf", root / "스캔_견적서 (1).pdf")   # a downloaded copy
    pdf(root / "한장.pdf", 1)
    db = tmp_path / "i.db"

    n1 = run(monkeypatch, root, db, deep=False)
    con = sqlite3.connect(db)
    st = dict(con.execute("SELECT name, ocr FROM files"))
    assert n1 == index.OCR_FIRST_PAGES + 1            # 3 pages of the 7-page scan, 1 page of the other; the copy is not OCR'd
    assert st["스캔_견적서.pdf"] == index.OCR_PARTIAL and st["스캔_견적서 (1).pdf"] == index.OCR_PARTIAL and st["한장.pdf"] == 1
    copy_text = con.execute("SELECT group_concat(c.text) FROM chunks c JOIN files f ON f.id=c.file WHERE f.name='스캔_견적서 (1).pdf'").fetchone()[0]
    assert "스마트센서" in copy_text

    n2 = run(monkeypatch, root, db, deep=True)
    st = dict(sqlite3.connect(db).execute("SELECT name, ocr FROM files"))
    assert n2 == 7                                      # the deep run reads the whole 7-page scan once; the copy and the 1-page file are done
    assert st["스캔_견적서.pdf"] == 1 and st["스캔_견적서 (1).pdf"] == 1
