"""a question during indexing does not wait for the vectors to be read again (#67: 8-11 s in refresh); the new vectors swap in
once a thread has read them, and a failed read keeps the old ones"""
import sqlite3
import time

import numpy as np

import search


def _db(path, n):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB)")
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO meta VALUES('encoder','e5-small')")
    for i in range(n):
        con.execute("INSERT INTO chunks(file,n,text,emb) VALUES(?,?,?,?)", (i, 0, "x", np.ones(4, np.float16).tobytes()))
    con.commit()
    return con


def _finder(path):
    f = object.__new__(search.Finder)
    f.db_uri = path.resolve().as_uri() + "?mode=ro"
    f.con = sqlite3.connect(path, check_same_thread=False)
    f._dv = f.con.execute("PRAGMA data_version").fetchone()[0]
    rows = f.con.execute("SELECT id, emb FROM chunks WHERE emb IS NOT NULL").fetchall()
    f.ids = np.array([r[0] for r in rows]); f.M = np.ones((len(rows), 4), np.float32)
    f.encoder = "e5-small"; f._top = len(rows)
    return f


def test_refresh_returns_at_once_and_swaps_later(tmp_path):
    p = tmp_path / "i.db"
    w = _db(p, 3)
    f = _finder(p)
    for i in range(5):   # the indexer commits more vectors
        w.execute("INSERT INTO chunks(file,n,text,emb) VALUES(?,?,?,?)", (10 + i, 0, "y", np.ones(4, np.float16).tobytes()))
    w.commit()
    t = time.time()
    f.refresh(every=0)
    assert time.time() - t < 0.2, "the question must not wait for the reload"
    for _ in range(100):
        if not getattr(f, "_reloading", False):
            break
        time.sleep(0.05)
    assert len(f.ids) == 8 and f.M.shape == (8, 4)


def test_failed_reload_keeps_old_vectors(tmp_path):
    p = tmp_path / "i.db"
    _db(p, 3)
    f = _finder(p)
    f.db_uri = (tmp_path / "missing.db").resolve().as_uri() + "?mode=ro"
    f._reloading = True
    f._reload()
    assert len(f.ids) == 3 and not f._reloading
