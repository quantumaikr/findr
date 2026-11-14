"""red-team pass (11/14): what search_files says must not depend on files it leaves out. The engine's note ('X is in some
files') and its odds were computed over its whole list, data folder and deny folders included; and a file the index keeps
as a secret by its content (tier 0) came back by name through the engine. Invented names only"""
import importlib
import io
import json
import sqlite3

import pytest


@pytest.fixture
def pc(tmp_path, monkeypatch):
    data = tmp_path / "data"; data.mkdir()
    (tmp_path / "docs").mkdir(); (tmp_path / "denyme").mkdir()
    calib = tmp_path / "calib.json"
    calib.write_text(json.dumps({"w": [0.1, 3.0, 2.0, -0.5, 0.4, 1.0, -1.0, -0.3, 0.2]}), encoding="utf-8")
    monkeypatch.setenv("CALIB_P", str(calib))
    monkeypatch.setenv("FINDR_HOME", str(data)); monkeypatch.setenv("FINDR_APP_PORT", "9")
    monkeypatch.setenv("FINDR_MCP_DENY", str(tmp_path / "denyme"))
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    docs = (tmp_path / "docs").as_posix()
    con = sqlite3.connect(str(data / "index.db"))
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);""")
    con.execute("INSERT INTO files VALUES(1,?,'notes.txt','.txt',1,1700000000,1)", (f"{docs}/notes.txt",))
    con.execute("INSERT INTO files VALUES(2,?,'export.csv','.csv',1,1700000000,0)", (f"{docs}/export.csv",))   # secret by its header
    con.commit(); con.close()
    m.docs, m.deny = docs, (tmp_path / "denyme").as_posix()
    return m


def _engine(m, monkeypatch, answer):
    class Op:
        def open(self, req, timeout=0):
            return io.BytesIO(json.dumps(answer).encode())
    monkeypatch.setattr(m, "_engine_up", lambda: True)
    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *a: Op())


def test_same_answer_whether_a_denied_file_matched_or_not(pc, monkeypatch):
    ok = {"name": "notes.txt", "path": f"{pc.docs}/notes.txt", "prob": 0.31, "ce": 0.2, "snippet": "alpha plan"}
    _engine(pc, monkeypatch, {"results": [{"name": "deny_plan.txt", "path": f"{pc.deny}/deny_plan.txt", "prob": 0.9, "ce": 0.95,
                                           "snippet": "zebracorn"}, dict(ok)], "p_none": 0.4,
                              "note": "'zebracorn' is in some files, but they fit the rest of your question less well."})
    with_denied = pc.search_files("zebracorn plan", limit=1)
    _engine(pc, monkeypatch, {"results": [dict(ok, prob=0.6)], "p_none": 0.4, "note": "'zebracorn' is not in any file. Showing the closest files."})
    without = pc.search_files("zebracorn plan", limit=1)
    assert with_denied == without, (with_denied, without)
    assert "zebracorn" not in json.dumps(with_denied)


def test_nothing_left_says_nothing_found(pc, monkeypatch):
    _engine(pc, monkeypatch, {"results": [{"name": "deny_plan.txt", "path": f"{pc.deny}/deny_plan.txt", "prob": 0.9}], "note": ""})
    r = pc.search_files("harborlight")
    assert r["results"] == [] and r["note"] == pc.NOTE_NONE


def test_engine_results_kept_secret_by_the_index_leave(pc, monkeypatch):
    _engine(pc, monkeypatch, {"results": [{"name": "export.csv", "path": f"{pc.docs}/export.csv", "prob": 0.9},
                                          {"name": "notes.txt", "path": f"{pc.docs}/notes.txt", "prob": 0.5}], "note": ""})
    assert [x["name"] for x in pc.search_files("password")["results"]] == ["notes.txt"]
    assert pc.search_files("password", kind="sheet")["results"] == []


def test_engine_p_none_and_note_never_reach_the_answer(pc, monkeypatch):
    """the engine's p_none and note are computed over its whole list: a denied match changes them. The answer must be the
    same whether or not a hidden file matched (0.2.24 red-team: chances and note moved with p_none)"""
    ok = {"name": "notes.txt", "path": f"{pc.docs}/notes.txt", "prob": 0.31, "ce": 0.2, "snippet": "alpha plan"}
    hidden = {"name": "deny_plan.txt", "path": f"{pc.deny}/deny_plan.txt", "prob": 0.9, "ce": 0.95, "snippet": "zebracorn"}
    _engine(pc, monkeypatch, {"results": [hidden, dict(ok)], "p_none": 0.02, "note": ""})
    a = pc.search_files("zebracorn plan", limit=3)
    _engine(pc, monkeypatch, {"results": [dict(ok, prob=0.05)], "p_none": 0.97, "note": "'zebracorn' is not in any file."})
    b = pc.search_files("zebracorn plan", limit=3)
    assert a == b, (a, b)
    assert a["results"][0]["chance"] is not None


def test_no_scores_no_chance_from_the_engine(pc, monkeypatch):
    """without scores the engine's own chances (from its whole list) are not passed on"""
    hidden = {"name": "deny_plan.txt", "path": f"{pc.deny}/deny_plan.txt", "prob": 0.9}
    ok = {"name": "notes.txt", "path": f"{pc.docs}/notes.txt", "prob": 0.4}
    _engine(pc, monkeypatch, {"results": [hidden, dict(ok)], "p_none": 0.1, "note": "x"})
    a = pc.search_files("quillplan", limit=3)
    _engine(pc, monkeypatch, {"results": [dict(ok, prob=0.8)], "p_none": 0.9, "note": ""})
    b = pc.search_files("quillplan", limit=3)
    assert a == b and a["results"][0]["chance"] is None, (a, b)
