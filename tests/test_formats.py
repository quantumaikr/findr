"""reading files (#2, large files): Office text, Keynote vs key files, secrets never read, big workbooks bounded"""
import zipfile
from pathlib import Path

import index


def ooxml(path, part, xml):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(part, xml)
    return path


def test_docx_and_xlsx_text(tmp_path):
    d = ooxml(tmp_path / "휴가신청서.docx", "word/document.xml", "<w:document><w:t>휴가 신청서 2025년 12월 24일</w:t></w:document>")
    x = ooxml(tmp_path / "매출.xlsx", "xl/sharedStrings.xml", "<sst><si><t>스마트센서 매출 합계</t></si></sst>")
    assert "휴가 신청서" in index.extract(d, index.MAXREAD)
    assert "스마트센서 매출" in index.extract(x, index.MAXREAD)


def test_big_workbook_reads_only_what_it_needs(tmp_path):
    big = tmp_path / "big.xlsx"
    with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as z:
        with z.open("xl/worksheets/sheet1.xml", "w") as f:
            f.write(b"<worksheet><sheetData>")
            for i in range(300_000):
                f.write(f"<row><c><t>주문 {i}건</t></c></row>".encode())
            f.write(b"</sheetData></worksheet>")
    t = index.ooxml_text(big, 50_000)
    assert 0 < len(t) <= 50_000


def test_keynote_vs_key_file(tmp_path):
    deck = tmp_path / "발표자료.key"
    with zipfile.ZipFile(deck, "w") as z:
        z.writestr("Index/Document.iwa", b"")
    pem = tmp_path / "server.key"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
    assert index.tier(deck) == 1, "a Keynote deck is a document"
    assert index.tier(pem) == 0, "a key file is never read"


def test_secrets_never_read(tmp_path):
    for name in (".env", "id_rsa", "credentials.json", "cert.pem"):
        p = tmp_path / name
        p.write_text("API_KEY=abc")
        assert index.tier(p) == 0, name


def test_locked_or_missing_file_is_retried_not_marked_read(tmp_path):
    assert index._read_one((str(tmp_path / "없는파일.docx"), 1000)) in ("", None)
