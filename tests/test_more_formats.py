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


def _fake_soffice(tmp_path, monkeypatch, make_output, enc=None):
    """enc: the encoding LibreOffice writes in; by default what the filter asked for (UTF-8 when ':UTF8' is in it)"""
    import shutil, subprocess
    exe = tmp_path / "soffice"; exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(shutil, "which", lambda name: str(exe) if name == "soffice" else None)
    calls = []
    real_run = subprocess.run

    def run(cmd, **kw):
        if cmd[0] == "soffice":
            calls.append(cmd)
            if make_output:
                e = enc or ("utf-8" if cmd[cmd.index("--convert-to") + 1].endswith(":UTF8") else "cp949")
                (Path(cmd[cmd.index("--outdir") + 1]) / "in.txt").write_bytes(make_output.encode(e))
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


def test_korean_survives_conversion(tmp_path, monkeypatch):
    """Korean Windows: plain 'txt:Text' came out in cp949 and was read as UTF-8, so every Korean character was lost"""
    monkeypatch.setenv("FINDR_DATA", str(tmp_path / "data"))
    p = tmp_path / "보고서.doc"; p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 600)
    calls = _fake_soffice(tmp_path, monkeypatch, make_output="분기 매출 보고서")
    assert index.convert_old(p) == "분기 매출 보고서"
    assert calls[0][calls[0].index("--convert-to") + 1].endswith(":UTF8"), "ask LibreOffice for UTF-8"
    q = tmp_path / "옛날.doc"; q.write_bytes(b"\xd0\xcf\x11\xe0" + b"\1" * 600)
    _fake_soffice(tmp_path, monkeypatch, make_output="옛 계약서 본문", enc="cp949")   # an older LibreOffice ignores the option
    assert index.convert_old(q) == "옛 계약서 본문"


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


# --- mail (#202): all messages below are invented and built in tmp ---------------------------------------------------------

def _mail(subject="주간 점검", body="본문", **hdr):
    m = EmailMessage()
    m["Subject"] = subject; m["From"] = "a@example.com"; m["To"] = "b@example.com"
    for k, v in hdr.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m


def test_mail_korean_charset_labels_read_as_cp949(tmp_path):
    for label in ("euc-kr", "ks_c_5601-1987", "ks_c_5601", "x-windows-949"):
        subj = "=?%s?B?%s?=" % (label, __import__("base64").b64encode("똠방 점검".encode("cp949")).decode())
        raw = (f"Subject: {subj}\r\nFrom: a@example.com\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset={label}\r\n"
               f"Content-Transfer-Encoding: 8bit\r\n\r\n").encode() + "똠 본문".encode("cp949")
        p = tmp_path / "k.eml"; p.write_bytes(raw)
        t = index.extract(p, index.MAXREAD)
        assert "똠방 점검" in t and "똠 본문" in t, label


def test_mail_thread_headers(tmp_path):
    m = _mail(Message_ID="<m2@example.com>", In_Reply_To="<m1@example.com>", References="<m0@example.com> <m1@example.com>")
    p = tmp_path / "t.eml"; p.write_bytes(bytes(m))
    t = index.extract(p, index.MAXREAD)
    assert "Message-ID: <m2@example.com>" in t and "In-Reply-To: <m1@example.com>" in t
    assert "References: <m0@example.com> <m1@example.com>" in t


def test_mail_attachment_text_is_indexed_with_caps(tmp_path, monkeypatch):
    m = _mail()
    m.add_attachment("납품 일정표 별첨내용".encode("utf-8"), maintype="text", subtype="plain", filename="일정.txt")
    m.add_attachment(b"MZ-binary", maintype="application", subtype="octet-stream", filename="tool.exe")
    p = tmp_path / "a.eml"; p.write_bytes(bytes(m))
    t = index.extract(p, index.MAXREAD)
    assert "별첨내용" in t and "일정.txt" in t and "tool.exe" in t
    monkeypatch.setattr(moretext, "MAIL_ATTACH_BYTES", 4)   # size cap: named only
    assert "별첨내용" not in index.extract(p, index.MAXREAD)
    monkeypatch.setattr(moretext, "MAIL_ATTACH_BYTES", 10_000)
    monkeypatch.setattr(moretext, "MAIL_MAX_ATTACH", 0)     # count cap
    assert "별첨내용" not in index.extract(p, index.MAXREAD)


def test_emlx(tmp_path):
    body = bytes(_mail("애플 메일 제목", "emlx 본문 내용", Message_ID="<x1@example.com>"))
    plist = b'<?xml version="1.0"?><plist><dict><key>flags</key><integer>8590195713</integer></dict></plist>'
    p = tmp_path / "12.emlx"; p.write_bytes(str(len(body)).encode() + b"\n" + body + plist)
    t = index.extract(p, index.MAXREAD)
    assert "애플 메일 제목" in t and "emlx 본문 내용" in t and "<x1@example.com>" in t and "flags" not in t


def test_mbox_plain_and_apple_export_folder(tmp_path):
    def env(m):
        return b"From a@example.com Sat Nov 14 10:00:00 2026\n" + bytes(m).replace(b"\r\n", b"\n") + b"\n"
    data = env(_mail("첫째 메일", "첫 본문")) + env(_mail("둘째 메일", "줄\nFrom 시작 줄\n끝"))
    p = tmp_path / "받은편지함.mbox"; p.write_bytes(data)
    t = index.extract(p, index.MAXREAD)
    assert "첫째 메일" in t and "둘째 메일" in t and "첫 본문" in t
    d = tmp_path / "내보내기.mbox"; d.mkdir()
    (d / "mbox").write_bytes(data)
    t = index.extract(d / "mbox", index.MAXREAD)
    assert "첫째 메일" in t and "둘째 메일" in t
    assert index.tier(p) == 1


def test_mail_hostile_nesting_falls_back_to_name_only(tmp_path):
    depth = 1200
    lines = ["Subject: x", "MIME-Version: 1.0"]
    for i in range(depth):
        lines += [f'Content-Type: multipart/mixed; boundary="b{i}"', "", f"--b{i}"]
        if i == 0:
            continue
    raw = ("\n".join(lines) + "\n\nhello\n").encode()
    p = tmp_path / "deep.eml"; p.write_bytes(raw)
    assert index.extract(p, index.MAXREAD) == ""
    assert moretext.eml_text(p) is None
    # wide: many parts is capped, not fatal
    m = _mail()
    for i in range(moretext.MAIL_MAX_PARTS + 50):
        m.add_attachment(b"x", maintype="application", subtype="octet-stream", filename=f"f{i}.bin")
    p2 = tmp_path / "wide.eml"; p2.write_bytes(bytes(m))
    assert "본문" in index.extract(p2, index.MAXREAD)


def test_mail_recursion_error_is_caught(tmp_path, monkeypatch):
    import email
    p = tmp_path / "r.eml"; p.write_bytes(bytes(_mail()))
    def boom(*a, **k):
        raise RecursionError
    monkeypatch.setattr(email, "message_from_bytes", boom)
    assert index.extract(p, index.MAXREAD) == ""
