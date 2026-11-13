"""the engine over a long session and after a frozen PC (field, 11-13: a 16 GB CPU laptop froze while the first index ran):
a half-written memory line, a file time Windows cannot say, a key file left empty, a wrong key from a tool on this PC, two
views built at once, a network share that does not answer, the index locked by another writer, and files that only grow"""
import datetime as _dt
import json
import sqlite3
import sys
import threading
import time
import types

import episodic
import index
import personal
import search


# --- personal memory (#3, #15) ---------------------------------------------------------------------------------------

def test_a_cut_line_is_left_out_and_the_file_is_mended(tmp_path):
    p = tmp_path / "personal_memory.jsonl"
    p.write_text(json.dumps({"q": "예산 보고서", "chosen": 3, "rejected": []}, ensure_ascii=False) + "\n"
                 + '{"q": "회의록", "chosen": 7, "rej', encoding="utf-8")   # power cut in the middle of a line
    m = personal.Memory(str(p))
    assert [r["q"] for r in m.rows] == ["예산 보고서"]
    assert p.read_text(encoding="utf-8").endswith("\n") and "회의록" not in p.read_text(encoding="utf-8")
    m.add("회의록", 7)
    again = personal.Memory(str(p))
    assert [r["q"] for r in again.rows] == ["예산 보고서", "회의록"]
    assert not list(tmp_path.glob("*.tmp"))


def test_odd_lines_do_not_stop_the_search(tmp_path, monkeypatch):
    d = tmp_path / "data"; d.mkdir()
    (d / "personal_memory.jsonl").write_text('[1, 2]\n{"chosen": 1}\nnot json\n{"q": "x", "chosen": "a"}\n', encoding="utf-8")
    monkeypatch.setenv("PERSONAL", "1"); monkeypatch.setenv("FINDR_DATA", str(d))
    monkeypatch.setattr(search, "_PM", None)
    pm = search.personal()
    assert pm is not None and pm.rows == []


def test_add_and_forget_at_once_keep_a_whole_file(tmp_path):
    p = tmp_path / "pm.jsonl"
    m = personal.Memory(str(p))
    def adder(k):
        for i in range(60):
            m.add(f"질문 {k} {i}", i)
    def forgetter():
        for _ in range(30):
            m.forget(0)
    ts = [threading.Thread(target=adder, args=(k,)) for k in range(3)] + [threading.Thread(target=forgetter)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert all(isinstance(json.loads(x), dict) for x in lines)   # every line whole
    assert len(personal.Memory(str(p)).rows) == len(m.rows) >= 180 - 30


def test_the_memory_keeps_the_newest_choices(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_MAX", "50")
    p = tmp_path / "pm.jsonl"
    m = personal.Memory(str(p))
    for i in range(51):
        m.add(f"q{i}", i)
    assert len(m.rows) == 45 and m.rows[-1]["q"] == "q50" and m.rows[0]["q"] == "q6"
    assert len(personal.Memory(str(p)).rows) == 45
    p.write_text("".join(json.dumps({"q": f"o{i}", "chosen": i, "rejected": []}) + "\n" for i in range(80)), encoding="utf-8")
    m2 = personal.Memory(str(p))   # a file longer than the bound (an older version, a raised PM_MAX lowered again)
    assert len(m2.rows) == 50 and m2.rows[0]["q"] == "o30"


# --- file times Windows cannot say (#4) -------------------------------------------------------------------------------

class _WinDT(_dt.datetime):
    """datetime as on Windows: a time before 1970 raises OSError (Errno 22)"""
    @classmethod
    def fromtimestamp(cls, t, tz=None):
        if t < 0:
            raise OSError(22, "Invalid argument")
        return super().fromtimestamp(t, tz)


def test_a_file_time_the_system_cannot_say_is_left_blank(monkeypatch):
    monkeypatch.setattr(search, "dt", types.SimpleNamespace(datetime=_WinDT))
    assert search.fmt_time(-11644473600) == ""          # a zeroed Windows file time (1601)
    assert search.fmt_time(1e20) == "" and search.fmt_time(None) == "" and search.fmt_time(float("nan")) == ""
    t = _dt.datetime(2026, 11, 13, 9, 30).timestamp()
    assert search.fmt_time(t) == "2026-11-13 09:30" and search.fmt_time(t, "%Y-%m-%d") == "2026-11-13"


def test_rows_with_such_a_time_still_come_back(monkeypatch):
    monkeypatch.setattr(search, "dt", types.SimpleNamespace(datetime=_WinDT))
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);")
    con.execute("INSERT INTO files VALUES(1, '/d/old.hwp', 'old.hwp', 10, -11644473600, 1)")
    con.execute("INSERT INTO files VALUES(2, '/d/new.hwp', 'new.hwp', 10, ?, 1)", (_dt.datetime(2026, 1, 2).timestamp(),))
    f = types.SimpleNamespace(con=con)
    out = search.Finder.files_out(f, [1, 2])
    assert [x["date"] for x in out] == ["", "2026-01-02 00:00"]


def test_the_period_reading_compares_numbers(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE files(id INTEGER PRIMARY KEY, mtime REAL)")
    days = {1: -11644473600, 2: _dt.datetime(2026, 11, 3, 12).timestamp(), 3: _dt.datetime(2026, 11, 10).timestamp(),
            4: _dt.datetime(2026, 11, 9, 23, 59, 59).timestamp(), 5: 1e20}
    con.executemany("INSERT INTO files VALUES(?, ?)", days.items())
    monkeypatch.setattr(episodic, "_FS", {})
    monkeypatch.setattr(episodic, "dt", types.SimpleNamespace(datetime=_WinDT))
    assert episodic._fs_in_range(con, "2026-11-03", "2026-11-10") == {2, 4}   # [a, b): the 10th itself is out
    assert episodic._fs_in_range(con, "2026-11-03T12:00:00", "2026-11-10T00:00:01") == {2, 3, 4}


# --- the password limiter and the key file (#7, #11) --------------------------------------------------------------------

def _h(addr, **headers):
    return types.SimpleNamespace(client_address=(addr, 50000), headers=headers)


def test_tools_on_this_pc_with_an_old_key_lock_nobody_out(monkeypatch):
    """MCP or the CLI with another data folder's key, or a tab with an old cookie polling every 1.5 s, made ten wrong
    tries from 127.0.0.1: the app, the indexer and MCP were all refused (429) for ten minutes, with the right key too"""
    monkeypatch.setattr(search, "FAILS", {}); monkeypatch.setattr(search, "TUNNEL_FAILS", [0, 0.0])
    for i in range(50):
        search._fail_note(_h("127.0.0.1"), now=1000 + i)
        search._fail_note(_h("::1"), now=1000 + i)
    assert not search._fail_blocked(_h("127.0.0.1"), now=1100) and not search._fail_blocked(_h("::1"), now=1100)
    assert search.FAILS == {} and search.TUNNEL_FAILS[0] == 0
    tunnel = _h("127.0.0.1", **{"CF-Connecting-IP": "198.51.100.9"})   # the tunnel keeps its limit (#177)
    for _ in range(search.FAIL_PER_CLIENT):
        search._fail_note(tunnel, now=1000)
    assert search._fail_blocked(tunnel, now=1001) and not search._fail_blocked(_h("127.0.0.1"), now=1001)


def test_an_empty_key_file_gets_a_key(tmp_path):
    pf = tmp_path / "d" / ".password"
    k1 = search.read_or_make_password(pf)
    assert len(k1) >= 12 and pf.read_text(encoding="utf-8").strip() == k1
    assert search.read_or_make_password(pf) == k1        # kept
    pf.write_text("  \n", encoding="utf-8")              # left empty by a crash between making and writing
    k2 = search.read_or_make_password(pf)
    assert k2 and k2 != k1 and pf.read_text(encoding="utf-8").strip() == k2
    assert [x.name for x in pf.parent.iterdir()] == [".password"]


def test_the_engine_and_the_cli_use_it():
    from pathlib import Path
    root = Path(search.__file__).resolve().parents[2]
    assert "read_or_make_password(" in (root / "findr" / "cli.py").read_text(encoding="utf-8")
    src = Path(search.__file__).read_text(encoding="utf-8")
    body = src[src.index("def serve("):]
    assert "read_or_make_password(" in body and "pf.write_text" not in body


# --- the explorer's view (#9) ------------------------------------------------------------------------------------------

def test_the_view_is_built_once_for_requests_at_once(monkeypatch):
    calls = []
    def build(db, now, corr):
        calls.append(1); time.sleep(0.2)
        return {"projects": []}
    monkeypatch.setitem(sys.modules, "explorer", types.SimpleNamespace(build=build))
    monkeypatch.setattr(search, "EXPLORE_CACHE", {})
    f = types.SimpleNamespace(con=sqlite3.connect(":memory:", check_same_thread=False))
    got = []
    ts = [threading.Thread(target=lambda: got.append(search.explore_json(f))) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1 and len(set(got)) == 1 and json.loads(got[0])["src"] == "live"


# --- files on a share that does not answer (#10) -----------------------------------------------------------------------

def test_a_share_that_does_not_answer_holds_the_answer_briefly(monkeypatch):
    looked = []
    def gone(p):
        looked.append(p)
        if p.startswith("/mnt/nas/"):
            time.sleep(1.0)   # an offline share: the system waits for its timeout
        return p.endswith("deleted.txt")
    monkeypatch.setattr(search, "_gone", gone)
    monkeypatch.setattr(search, "_SLOW_ROOTS", {})
    res = {"results": [{"path": "/mnt/nas/a.docx"}, {"path": "/home/u/deleted.txt"}, {"path": "/home/u/kept.txt",
                                                                                        "copies": ["/mnt/nas/b.docx"]}]}
    t = time.time()
    gm = search.gone_map(res, wait=0.2)
    assert time.time() - t < 0.6
    assert gm == {"/mnt/nas/a.docx": False, "/home/u/deleted.txt": True, "/home/u/kept.txt": False, "/mnt/nas/b.docx": False}
    assert "/mnt/nas" in search._SLOW_ROOTS and "/" not in search._SLOW_ROOTS
    looked.clear()
    t = time.time()
    gm2 = search.gone_map({"results": [{"path": "/mnt/nas/c.pdf"}, {"path": "/home/u/deleted.txt"}]}, wait=0.2)
    assert time.time() - t < 0.15 and looked == ["/home/u/deleted.txt"]   # the slow share is not looked at again
    assert gm2 == {"/mnt/nas/c.pdf": False, "/home/u/deleted.txt": True}
    out = search.drop_gone(None, res, gone=gm)
    assert [x["path"] for x in out["results"]] == ["/mnt/nas/a.docx", "/home/u/kept.txt"] and out["gone"] == 1
    looked.clear()
    later = search.gone_map({"results": [{"path": "/mnt/nas/c.pdf"}]}, wait=0.2, now=time.time() + search.GONE_SLOW_S + 1)
    assert later == {"/mnt/nas/c.pdf": False} and looked == ["/mnt/nas/c.pdf"]   # looked at again after a while


def test_roots(monkeypatch):
    monkeypatch.setattr(search.os, "name", "posix")
    assert search._root("/mnt/z/팀/a.hwp") == "/mnt/z" and search._root("/Volumes/NAS/a") == "/Volumes/NAS"
    assert search._root("/media/u/USB/a") == "/media/u/USB" and search._root("/home/u/a") == "/"
    assert search._root("Z:\\팀\\a.hwp") == "/mnt/z"


def test_the_engine_looks_outside_the_lock():
    from pathlib import Path
    src = Path(search.__file__).read_text(encoding="utf-8")
    body = src[src.index('if u.path == "/api":'):src.index('elif u.path == "/explore":')]
    assert body.count("gm_ = gone_map(res)") == 2 and body.count("drop_gone(f.con, res, gone=gm_)") == 2


# --- the photo thread and a locked index (#12) -------------------------------------------------------------------------

def test_a_locked_index_is_waited_for(tmp_path, monkeypatch):
    db = tmp_path / "i.db"
    a = sqlite3.connect(db, isolation_level=None); a.execute("PRAGMA journal_mode=WAL"); a.execute("CREATE TABLE t(x)")
    b = sqlite3.connect(db, timeout=0)
    a.execute("BEGIN IMMEDIATE"); a.execute("INSERT INTO t VALUES(1)")   # the indexer holds the write lock
    waits = []
    def sleep(s):
        waits.append(s)
        if len(waits) == 2:
            a.execute("COMMIT")   # and lets go during the second wait
    monkeypatch.setattr(index.time, "sleep", sleep)
    def store():
        b.execute("INSERT INTO t VALUES(2)"); b.commit()
    index.db_retry(b, store)
    assert waits == list(index.DB_WAITS[:2]) and sorted(x for (x,) in b.execute("SELECT x FROM t")) == [1, 2]
    calls = []
    def other():
        calls.append(1); raise sqlite3.OperationalError("no such table: q")
    try:
        index.db_retry(b, other)
        assert False
    except sqlite3.OperationalError:
        assert calls == [1]   # another error: no waiting


# --- feedback files (#15) ----------------------------------------------------------------------------------------------

def test_feedback_is_set_aside_once_and_old_files_go(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "FEEDBACK_MAX", 200); monkeypatch.setattr(search, "FEEDBACK_KEEP", 3)
    fbp = tmp_path / "feedback.jsonl"
    for i in range(5):   # files set aside before
        (tmp_path / f"feedback.2025010{i}000000.jsonl").write_text("{}\n", encoding="utf-8")
    def click(k):
        for i in range(40):
            search.feedback_write(fbp, {"q": f"{k}-{i}", "verdict": "hit"})
    ts = [threading.Thread(target=click, args=(k,)) for k in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    kept = sorted(tmp_path.glob("feedback.2*.jsonl"))
    assert len(kept) == 3 and not (tmp_path / "feedback.20250100000000.jsonl").exists()
    lines = [json.loads(x) for p in [fbp, *kept] for x in p.read_text(encoding="utf-8").splitlines()]
    assert all(isinstance(x, dict) for x in lines)
