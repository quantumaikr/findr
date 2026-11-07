"""formats read without LibreOffice: OpenDocument / old Excel sheets (calamine), RTF, e-mail; and the conversion cache"""
import zipfile
from email.message import EmailMessage
from pathlib import Path

import index
import moretext


def test_ods_sheet(tmp_path):
    p = tmp_path / "매출.ods"
    content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
               'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2"><office:body><office:spreadsheet>'
               '<table:table table:name="1분기"><table:table-row><table:table-cell office:value-type="string"><text:p>스마트센서 납품</text:p>'
               '</table:table-cell><table:table-cell office:value-type="float" office:value="1200"><text:p>1200</text:p></table:table-cell>'
               '</table:table-row></table:table></office:spreadsheet></office:body></office:document-content>')
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", content)
        z.writestr("META-INF/manifest.xml", '<?xml version="1.0"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
                   '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
                   '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')
    t = index.extract(p, index.MAXREAD)
    assert "[1분기]" in t and "스마트센서 납품" in t and "1200" in t


def test_rtf_korean_codepage_and_unicode(tmp_path):
    p = tmp_path / "메모.rtf"
    hexko = "".join(f"\\'{b:02x}" for b in "견적".encode("cp949"))      # 8-bit text in the document's code page
    uni = "".join(f"\\u{ord(c)}?" for c in "생산 일정")                    # Unicode with one fallback character each
    body = "{\\rtf1\\ansi\\ansicpg949{\\fonttbl{\\f0 Malgun Gothic;}}{\\*\\generator Riched20;}\\f0 " + hexko + "\\par \\uc1" + uni + "\\tab 6}"
    p.write_bytes(body.encode("ascii"))
    t = moretext.rtf_text(p)
    assert "견적" in t and "생산" in t and "일정" in t and "Malgun" not in t and "Riched" not in t


def test_eml_html_body_and_attachments(tmp_path):
    m = EmailMessage()
    m["Subject"] = "BR-300 브라켓 견적 요청"; m["From"] = "박서준 <park@example.com>"; m["To"] = "kim@example.com"
    m.set_content("본문 텍스트")
    m.add_alternative("<html><body><p>단가 <b>12,000원</b>으로 부탁드립니다</p></body></html>", subtype="html")
    m.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="도면_v2.pdf")
    p = tmp_path / "견적.eml"; p.write_bytes(bytes(m))
    t = index.extract(p, index.MAXREAD)
    assert "BR-300 브라켓 견적 요청" in t and "박서준" in t and "본문 텍스트" in t and "도면_v2.pdf" in t


def test_failed_conversion_is_not_cached(tmp_path, monkeypatch):
    """without LibreOffice nothing is cached, so installing it later reads the file (an empty cache used to stick forever)"""
    import shutil
    monkeypatch.setenv("FINDR_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    p = tmp_path / "old.ppt"; p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 600)
    assert index.convert_old(p) == ""
    assert not list((tmp_path / "data").rglob("*.txt"))
