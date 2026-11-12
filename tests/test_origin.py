"""where a file came from, read from what the PC recorded when it was saved (origin.py): mail, messenger or a site. Files
unpacked from a downloaded archive are not counted as received, and these partial records lift files, never rule them out."""
import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import episodic as EPI
import index
import origin

mac = pytest.mark.skipif(sys.platform != "darwin", reason="extended attributes as macOS writes them")


def test_addresses_and_apps_say_where_a_file_came_from():
    assert origin.classify(["https://mail.google.com/mail/u/0/?ui=2&view=att"]) == ("saved_from_mail", "mail.google.com")
    assert origin.classify(["https://attachments.office.net/owa/x"])[0] == "saved_from_mail"
    assert origin.classify(["Sender <a@example.com>", "Subject", "message:%3Cid@example.com%3E"]) == ("saved_from_mail", "Mail")
    assert origin.classify([], "KakaoTalk") == ("saved_from_messenger", "KakaoTalk")
    assert origin.classify(["https://files.slack.com/files-pri/T1/x.pdf"])[0] == "saved_from_messenger"
    assert origin.classify(["https://www.example.go.kr/down?id=1", "https://www.example.go.kr/"]) == ("downloaded", "www.example.go.kr")
    assert origin.classify([], "Microsoft\\x20Outlook") == ("saved_from_mail", "Microsoft Outlook")
    # a mark with no address from a browser or an editor: an unpacked archive or a file the app made, not a received file
    assert origin.classify([], "Chrome") is None and origin.classify([], "Keynote") is None
    assert origin.classify(["https://notmail.google.com.example.org/x"])[0] == "downloaded"


def test_windows_zone_stream_addresses():
    z = "[ZoneTransfer]\r\nZoneId=3\r\nReferrerUrl=https://mail.google.com/\r\nHostUrl=https://mail-attachment.googleusercontent.com/a\r\n"
    urls = origin.zone_urls(z)
    assert origin.classify(urls)[0] == "saved_from_mail"
    assert origin.zone_urls("[ZoneTransfer]\nZoneId=3\n") == []
    assert origin.zone_urls(z)[0] == "https://mail.google.com/"   # the page names the service, so it comes first
    assert origin._text("[ZoneTransfer]\r\nHostUrl=https://a.example/\r\n".encode("utf-16")).count("HostUrl") == 1
    assert "HostUrl" in origin._text(b"\xef\xbb\xbf[ZoneTransfer]\nHostUrl=https://a.example/\n")


def test_mail_and_messenger_hosts_worldwide():
    mail = ["https://webmail.example.ac.kr/download?f=1", "https://mail.example.co.jp/att/2", "https://www.icloud.com/mail/x",
            "https://mail.proton.me/api/x", "https://exmail.qq.com/cgi-bin/download", "https://outlook.office365.com/owa/x"]
    for u in mail:
        assert origin.classify([u])[0] == "saved_from_mail", u
    for u in ("https://cdn.discordapp.com/attachments/1/2/a.pdf", "https://web.telegram.org/a/", "https://teams.microsoft.com/l/f"):
        assert origin.classify([u])[0] == "saved_from_messenger", u
    for u in ("https://mail.com/", "https://www.kakaobank.com/doc.pdf", "https://www.icloud.com/drive/x", "https://mailchimp.com/x"):
        assert origin.classify([u])[0] == "downloaded", u   # a product named 'mail', a bank, a cloud drive: not mail
    assert origin.classify(["not a url", "ftp://x.example/y", "https://[broken"]) is None
    assert origin.classify([], "Telegram\\x20Desktop") == ("saved_from_messenger", "Telegram Desktop")


def test_a_damaged_stamp_falls_back_to_the_file_time(tmp_path):
    p = tmp_path / "a.txt"; p.write_text("x"); st = os.stat(p)
    assert origin._when("zz", st) and origin._when("ffffffffff", st) and origin._when(None, None) is None
    assert origin._when("6553f100", st).startswith("2023-11-")


def test_an_online_only_cloud_file_is_never_opened(tmp_path, monkeypatch):
    class St:
        st_file_attributes = 0x00400000   # RECALL_ON_DATA_ACCESS: OneDrive 'online-only'
    monkeypatch.setattr(origin.os, "stat", lambda p: St())
    opened = []
    monkeypatch.setattr("builtins.open", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(OSError()))
    assert origin._zone(str(tmp_path / "cloud.pdf")) == "" and opened == []


def _setx(path, name, value):
    import ctypes, ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    assert libc.setxattr(os.fsencode(str(path)), name, value, len(value), 0, 0) == 0


def _tag(path, urls=None, agent=None, stamp="6553f100"):
    if urls is not None:
        _setx(path, b"com.apple.metadata:kMDItemWhereFroms", plistlib.dumps(urls, fmt=plistlib.FMT_BINARY))
    if agent:
        _setx(path, b"com.apple.quarantine", f"0083;{stamp};{agent};".encode())


def _mark_url(path, url):
    """the source address as this OS's browsers record it; False when the file system here keeps no such record"""
    if sys.platform == "darwin":
        _tag(path, [url], "Chrome"); return True
    if os.name == "nt":
        with open(str(path) + ":Zone.Identifier", "w", encoding="utf-8") as f:
            f.write(f"[ZoneTransfer]\nZoneId=3\nReferrerUrl={url}\nHostUrl={url}download\n")
        return True
    try:
        os.setxattr(str(path), "user.xdg.origin.url", url.encode()); return True
    except (OSError, AttributeError):
        return False


def _db(paths):
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, mtime REAL);")
    for p in paths:
        con.execute("INSERT INTO files(path, name, mtime) VALUES(?,?,?)", (str(p), p.name, p.stat().st_mtime))
    return con


@mac
def test_marked_files_are_recorded_once(tmp_path):
    m, k, z, plain = (tmp_path / n for n in ("견적.pdf", "사진.jpg", "압축에서.txt", "직접.txt"))
    for p in (m, k, z, plain):
        p.write_text("x")
    _tag(m, ["https://mail.google.com/mail/u/0/"], "Chrome")
    _tag(k, agent="KakaoTalk")
    _tag(z, agent="Chrome")   # unpacked from a downloaded zip: the mark without an address
    con = _db([m, k, z, plain])
    got = origin.collect(con)
    assert got == {"looked": 4, "left": 0, "saved_from_mail": 1, "saved_from_messenger": 1}
    ev = {Path(p).name: (t, s, tm) for p, t, s, tm in con.execute(
        "SELECT f.path, e.type, e.source, e.time FROM events e JOIN files f ON f.id=e.file")}
    assert ev["견적.pdf"][:2] == ("saved_from_mail", "pc:mail.google.com") and ev["견적.pdf"][2].startswith("2023-11-")
    assert ev["사진.jpg"][:2] == ("saved_from_messenger", "pc:KakaoTalk")
    assert origin.collect(con) == {"looked": 0, "left": 0}   # nothing new: nothing read again
    con.execute("DELETE FROM files WHERE path=?", (str(plain),))
    con.execute("UPDATE files SET path=? WHERE path=?", (str(plain), str(m)))   # the id now names another, unmarked file
    origin.collect(con)
    assert con.execute("SELECT count(*) FROM events WHERE type='saved_from_mail'").fetchone()[0] == 0


def test_each_os_record_is_read(tmp_path):
    a, b = tmp_path / "첨부.pdf", tmp_path / "그냥.pdf"
    a.write_text("x"); b.write_text("x")
    if not _mark_url(a, "https://mail.google.com/mail/u/0/"):
        pytest.skip("this file system keeps no source attributes")
    con = _db([a, b])
    assert origin.collect(con) == {"looked": 2, "left": 0, "saved_from_mail": 1}
    assert con.execute("SELECT f.name, e.source FROM events e JOIN files f ON f.id=e.file").fetchall() == [("첨부.pdf", "pc:mail.google.com")]


def test_an_index_run_records_where_files_came_from(tmp_path):
    root = tmp_path / "Downloads"; root.mkdir()
    (root / "계약서.txt").write_text("갑과 을은 계약한다", encoding="utf-8")
    if not _mark_url(root / "계약서.txt", "https://outlook.office.com/mail/"):
        pytest.skip("this file system keeps no source attributes")
    db = tmp_path / "i.db"
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_HOME": str(tmp_path), "FINDR_DATA": str(tmp_path)}
    r = subprocess.run([sys.executable, str(Path(index.__file__)), "--root", str(root), "--db", str(db), "--no-embed"],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0 and "FINDR_ORIGIN" in r.stdout
    con = sqlite3.connect(db)
    assert con.execute("SELECT type, source FROM events").fetchall() == [("saved_from_mail", "pc:outlook.office.com")]


def _epi_db(tmp_path):
    con = sqlite3.connect(":memory:")
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, mtime REAL);
      CREATE TABLE events(id INTEGER PRIMARY KEY, file INT, type TEXT, time TEXT, person TEXT, source TEXT);""")
    for i in range(1, 4):
        con.execute("INSERT INTO files(id, path, name, mtime) VALUES(?,?,?,?)", (i, f"/d/{i}.pdf", f"{i}.pdf", 1700000000.0 + i))
    con.execute("INSERT INTO events(file, type, time, source) VALUES(1, 'saved_from_mail', '2023-11-14T10:00', 'pc:mail.google.com')")
    return con


def test_the_pc_marks_lift_and_never_rule_out(tmp_path):
    con = _epi_db(tmp_path); EPI._CACHE.clear()
    c = {"kind": "source", "type": "saved_from_mail", "text": "메일"}
    assert EPI.files_for(con, c) == {1} and c["_lift"] and c["_soft"]
    # kinds the PC does not mark stay unknown, as before: no 'not here' from an empty record
    assert EPI.files_for(con, {"kind": "source", "type": "saved_from_messenger", "text": "카톡"}) is None
    assert EPI.files_for(con, {"kind": "action", "type": "printed", "text": "인쇄"}) is None
    w = {"kind": "when", "range": ("2023-11-01T00:00", "2023-12-01T00:00"), "types": ["created"], "text": "11월"}
    assert EPI.files_for(con, w) == {1, 2, 3} and w["_soft"]   # 'made in November' still reads the files' own times


def test_full_records_are_used_as_before(tmp_path):
    con = _epi_db(tmp_path); EPI._CACHE.clear()
    con.execute("INSERT INTO events(file, type, time, source) VALUES(2, 'saved_from_mail', '2023-11-15T10:00', 'outlook')")
    c = {"kind": "source", "type": "saved_from_mail", "text": "메일"}
    assert EPI.files_for(con, c) == {2} and not c.get("_lift")


def test_a_long_first_pass_goes_on_next_run(tmp_path, monkeypatch):
    fs = [tmp_path / f"f{i}.txt" for i in range(5)]
    for p in fs:
        p.write_text("x")
    con = _db(fs)
    calls = []
    monkeypatch.setattr(origin, "read", lambda p: calls.append(p) or ("downloaded", "www.example.org", "2024-01-01T00:00"))
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(origin.time, "monotonic", lambda: next(clock))
    first = origin.collect(con, budget=15)   # each file 'takes' 10 s: the budget stops after two
    assert first["looked"] == 2 and first["left"] == 3
    second = origin.collect(con, budget=0)   # 0: no limit
    assert second["looked"] == 3 and second["left"] == 0 and len(calls) == 5   # nothing read twice
    assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 5


def test_one_unreadable_file_does_not_stop_the_rest(tmp_path, monkeypatch):
    fs = [tmp_path / f"f{i}.txt" for i in range(3)]
    for p in fs:
        p.write_text("x")
    con = _db(fs)
    def read(p):
        if p.endswith("f1.txt"):
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad")
        return "downloaded", "www.example.org", None
    monkeypatch.setattr(origin, "read", read)
    assert origin.collect(con, budget=0)["downloaded"] == 2


def test_marks_of_files_that_left_go_too(tmp_path, monkeypatch):
    fs = [tmp_path / "a.txt"]; fs[0].write_text("x")
    con = _db(fs)
    monkeypatch.setattr(origin, "read", lambda p: ("saved_from_mail", "mail.example.org", "2024-01-01T00:00"))
    origin.collect(con, budget=0)
    con.execute("DELETE FROM files")   # an older drop() that does not know the events table
    origin.collect(con, budget=0)
    assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM origin_seen").fetchone()[0] == 0


def test_received_last_week_reads_the_marks_times(tmp_path):
    con = _epi_db(tmp_path); EPI._CACHE.clear()
    con.execute("INSERT INTO events(file, type, time, source) VALUES(2, 'downloaded', '2023-12-20T09:00', 'pc:www.example.org')")
    w = {"kind": "when", "range": ("2023-11-01T00:00", "2023-12-01T00:00"),
         "types": ["downloaded", "saved_from_mail", "saved_from_messenger"], "text": "11월"}
    assert EPI.files_for(con, w) == {1} and w["_lift"]   # received in November: only the mail file
    assert EPI.pc_recent(con, {1, 2, 3}, 2) == [2, 1]   # the latest received first; unmarked files after


def test_marks_fill_a_kind_the_full_records_lack(tmp_path):
    con = _epi_db(tmp_path); EPI._CACHE.clear()
    con.execute("INSERT INTO events(file, type, time, person, source) VALUES(3, 'printed', '2023-11-15T10:00', NULL, NULL)")
    c = {"kind": "source", "type": "saved_from_mail", "text": "메일"}
    assert EPI.files_for(con, c) == {1} and c["_lift"]   # the full record has no mail kind: the PC's marks lift, never rule out
    assert EPI.files_for(con, {"kind": "action", "type": "printed", "text": "인쇄"}) == {3}   # the full record as before


def test_only_marked_files_close_to_the_best_go_first():
    import search
    sc = {1: 0.9, 2: 0.6, 3: 0.2, 4: 0.95}   # 4 is the best by content but has no mark
    assert search.lift_marked([1, 2, 3], sc, 10, ratio=0.5) == [1, 2]
    assert search.lift_marked([3], sc, 10, ratio=0.5) == []   # far off by content: the content order stays
    assert search.lift_marked([1, 2], sc, 1, ratio=0.5) == [1]
    assert search.lift_marked([7], {}, 10) == []
