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


def test_rtf_large_file_is_linear(tmp_path):
    import time
    p = tmp_path / "big.rtf"
    p.write_text("{\\rtf1\\ansi " + "".join(f"word{i} \\par " for i in range(120_000)) + "}")   # ~1.5 MB
    t = time.time(); out = moretext.rtf_text(p)
    assert time.time() - t < 3 and "word119999" in out   # was quadratic: 1.2 MB took ~110 s


def test_rtf_surrogate_pairs_and_group_scoped_fallbacks(tmp_path):
    p = tmp_path / "s.rtf"
    p.write_text("{\\rtf1\\ansi \\uc1\\u-10179?\\u-8704? ok {\\uc2{\\u49373}abc}}")
    assert moretext.rtf_text(p) == "😀 ok 생abc"


def _fake_soffice(tmp_path, monkeypatch, make_output):
    import shutil, subprocess
    exe = tmp_path / "soffice"; exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(shutil, "which", lambda name: str(exe) if name == "soffice" else None)
    calls = []
    real_run = subprocess.run

    def run(cmd, **kw):
        if cmd[0] == "soffice":
            calls.append(cmd)
            if make_output:
                (Path(cmd[cmd.index("--outdir") + 1]) / "in.txt").write_text(make_output)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return real_run(cmd, **kw)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_old_empty_cache_is_retried_and_failure_is_remembered(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path / "data"))
    p = tmp_path / "old.doc"; p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 600)
    calls = _fake_soffice(tmp_path, monkeypatch, make_output=None)    # LibreOffice runs but produces nothing
    assert index.convert_old(p) == "" and len(calls) == 1
    assert index.convert_old(p) == "" and len(calls) == 1             # .fail: not 120 s again with the same LibreOffice
    for f in (tmp_path / "data" / "conv").glob("*.fail"):
        f.write_text("an older soffice")                               # LibreOffice updated: try once more
    calls2 = _fake_soffice(tmp_path, monkeypatch, make_output="옛 문서 본문")
    assert index.convert_old(p) == "옛 문서 본문" and len(calls2) == 1


def test_really_empty_document_is_not_converted_again(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path / "data"))
    p = tmp_path / "blank.doc"; p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 600)
    calls = _fake_soffice(tmp_path, monkeypatch, make_output="  ")    # converted, but no text
    index.convert_old(p); index.convert_old(p)
    assert len(calls) == 1                                             # the .ok marker


def test_new_readers_requeue_empty_files_once(tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, tier INT, pending INT DEFAULT 0);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);"
                      "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);")
    for i, (ext, text) in enumerate([(".xls", ""), (".xls", "매출 합계"), (".hwp", ""), (".pdf", "")], 1):
        con.execute("INSERT INTO files(id,path,name,ext,tier) VALUES(?,?,?,?,1)", (i, f"/d/{i}{ext}", f"{i}{ext}", ext))
        con.execute("INSERT INTO chunks(file,n,text) VALUES(?,0,?)", (i, text))
    assert index.reread_for_new_readers(con) == 2                     # the empty .xls and the empty .hwp
    assert dict(con.execute("SELECT id, pending FROM files")) == {1: 1, 2: 0, 3: 1, 4: 0}
    assert index.reread_for_new_readers(con) == 0                     # once per reader version
