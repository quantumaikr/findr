"""idle-time move to the larger meaning model: side vectors in time-boxed runs, then one switch (no model download here)"""
import sqlite3

import numpy as np
import pytest

torch = pytest.importorskip("torch")
import enc_eg2  # noqa: E402
import index  # noqa: E402


class Fake:
    """stands in for the larger encoder: 8-wide vectors"""
    st = None
    dev = "cpu"
    bs = 8

    def emb(self, texts, query=True):
        return torch.nn.functional.normalize(torch.ones(len(texts), 8), dim=-1)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(enc_eg2, "make_encoder", lambda name, *a, **k: Fake())
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    c = sqlite3.connect(str(tmp_path / "index.db"))
    c.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB, emb2 BLOB);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('encoder', 'e5-small');""")
    small = np.ones(4, np.float16).tobytes()
    for i in range(100):
        c.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)", (i, f"/d/f{i}.txt", f"f{i}.txt", ".txt", 1, i, 1))
        c.execute("INSERT INTO chunks(file,n,text,emb) VALUES(?,?,?,?)", (i, 0, f"text {i}", small))
    c.execute("INSERT INTO chunks(file,n,text,emb) VALUES(0,1,'no vector yet',NULL)")   # a piece not computed yet stays so
    c.commit()
    return c


def test_pause_keeps_small_model(con):
    assert index.upgrade(con, "embeddinggemma-2", minutes=0) == 2   # no time this run: nothing switched
    assert enc_eg2.encoder_of(con) == "e5-small"
    assert con.execute("SELECT count(*) FROM chunks WHERE length(emb)=8").fetchone()[0] == 100   # search still on the small vectors


def test_switch_when_all_done(con, tmp_path):
    assert index.upgrade(con, "embeddinggemma-2", minutes=10) == 0
    assert enc_eg2.encoder_of(con) == "embeddinggemma-2"
    assert con.execute("SELECT count(*) FROM chunks WHERE length(emb)=16").fetchone()[0] == 100   # all at the new width at once
    assert con.execute("SELECT count(*) FROM chunks WHERE emb IS NULL").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM chunks WHERE emb2 IS NOT NULL").fetchone()[0] == 0
    assert (tmp_path / "upgrade.json").exists()
    assert index.upgrade(con, "embeddinggemma-2") == 1   # nothing left to do


def test_person_back_stops_the_app_run(con, tmp_path, monkeypatch):
    """the app's idle-time run gives the PC back when the keyboard or mouse is used, and keeps the last speed estimate"""
    import json
    (tmp_path / "upgrade.json").write_text(json.dumps({"target": "embeddinggemma-2", "done": 0, "total": 100, "rate": 1.5, "hours_left": 2.5}))
    monkeypatch.setenv("FINDR_STOP_ON_INPUT", "60"); monkeypatch.setattr(index, "LOOK_EVERY", 0)
    calls = iter([900, 900, 3])   # idle for two steps (progress is written meanwhile), then someone moves the mouse
    monkeypatch.setattr(index, "user_idle_seconds", lambda: next(calls, 3))
    assert index.upgrade(con, "embeddinggemma-2", minutes=10) == 2
    assert enc_eg2.encoder_of(con) == "e5-small"
    assert con.execute("SELECT count(*) FROM chunks WHERE emb2 IS NOT NULL").fetchone()[0] == 32   # two steps of 16 kept
    st = json.loads((tmp_path / "upgrade.json").read_text())
    assert st["back"] and st["rate"] == 1.5 and st["hours_left"] == 2.5   # not 'thousands of hours' from a 0-piece run


def test_person_back_needs_the_app_flag(con, monkeypatch):
    """`findr upgrade` typed by the person (no FINDR_STOP_ON_INPUT) runs on, and an unknown idle time never stops a run"""
    monkeypatch.delenv("FINDR_STOP_ON_INPUT", raising=False)
    monkeypatch.setattr(index, "user_idle_seconds", lambda: 3)
    assert index.upgrade(con, "embeddinggemma-2", minutes=10) == 0
    con.execute("UPDATE meta SET value='e5-small' WHERE key='encoder'"); con.commit()
    monkeypatch.setenv("FINDR_STOP_ON_INPUT", "60")
    monkeypatch.setattr(index, "user_idle_seconds", lambda: None)
    assert index.upgrade(con, "embeddinggemma-2", minutes=10) == 0


def test_user_idle_seconds_is_a_number_or_none():
    s = index.user_idle_seconds()
    assert s is None or s >= 0


def test_changed_piece_is_recomputed(con):
    con.execute("UPDATE chunks SET emb2=? WHERE n=0", (np.zeros(8, np.float16).tobytes(),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('upgrade', 'embeddinggemma-2')"); con.commit()
    con.execute("UPDATE chunks SET emb=?, emb2=NULL WHERE id=1", (np.ones(4, np.float16).tobytes(),)); con.commit()   # as embed() does
    assert index.upgrade(con, "embeddinggemma-2", minutes=10) == 0
    v = np.frombuffer(con.execute("SELECT emb FROM chunks WHERE id=1").fetchone()[0], np.float16)
    assert v.any(), "a piece whose text changed kept its stale side vector"
