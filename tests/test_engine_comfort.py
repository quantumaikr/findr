"""the engine and the background runs on a PC the person is also using (field, 11-13: a 16 GB CPU laptop, a video lecture and
a browser open): vectors read again only where they changed and not while memory is short, the indexer's batches after
the person's questions and on the indexer's share of the cores, background runs that stop when the person is back, on
battery or on a nearly full disk and keep what they stored, the waiting pieces' texts not all held at once, the write-ahead
file cut back, the upgrade's order taken once, LibreOffice profiles of their own, the reranker in 16 bits on small Macs"""
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import index
import search

ROOT = Path(__file__).resolve().parent.parent


# ---------- #5 vectors read again only where they changed ----------

def _vdb(path, n=50, d=8, seed=0):
    rng = np.random.default_rng(seed)
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);"
                      "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES('encoder','e5-small');")
    for i in range(n):
        v = rng.standard_normal(d).astype(np.float16).tobytes() if i % 7 else None   # some pieces have no vector yet
        con.execute("INSERT INTO chunks(file,n,text,emb) VALUES(?,?,?,?)", (i, 0, f"t{i}", v))
    con.commit()
    return con, rng


def _old_way(con):
    rows = con.execute("SELECT c.id, c.emb FROM chunks c WHERE c.emb IS NOT NULL").fetchall()
    return np.array([r[0] for r in rows], dtype=np.int64), np.stack([np.frombuffer(r[1], np.float16) for r in rows]).astype(np.float32)


def test_full_read_is_what_it_was(tmp_path):
    con, _ = _vdb(tmp_path / "i.db")
    ids, M, read = search.read_vectors(con)
    oi, oM = _old_way(con)
    assert np.array_equal(ids, oi) and M.dtype == np.float32 and np.array_equal(M, oM) and read == len(oi)
    assert not con.in_transaction


def test_changed_rows_only_and_the_same_result(tmp_path):
    con, rng = _vdb(tmp_path / "i.db")
    ids, M, _ = search.read_vectors(con)
    new = lambda: rng.standard_normal(8).astype(np.float16).tobytes()
    con.execute("UPDATE chunks SET emb=? WHERE id=1", (new(),))                 # computed again in place (same id)
    con.execute("UPDATE chunks SET emb=? WHERE id=7", (new(),))                 # had none, has one now (an old id)
    con.execute("DELETE FROM chunks WHERE id IN (3, 50)")                       # gone, the last one too
    con.execute("INSERT INTO chunks(id,file,n,text,emb) VALUES(50,1,0,'reused id',?)", (new(),))   # its id taken again
    for _ in range(5):
        con.execute("INSERT INTO chunks(file,n,text,emb) VALUES(1,0,'new',?)", (new(),))
    con.commit()
    ids2, M2, read = search.read_vectors(con, ids, M)
    oi, oM = _old_way(con)
    assert np.array_equal(ids2, oi) and np.array_equal(M2, oM)                 # what a full read gives, in the same order
    assert read == 1 + 1 + 1 + 5                                                # only what changed was read from the index


def test_another_width_reads_everything(tmp_path):
    con, _ = _vdb(tmp_path / "i.db")
    ids, M, _ = search.read_vectors(con)
    con.execute("UPDATE chunks SET emb=?", (np.ones(16, np.float16).tobytes(),)); con.commit()   # another encoder
    ids2, M2, read = search.read_vectors(con, ids, M)
    assert M2.shape[1] == 16 and read == len(ids2)


def _finder(path):
    f = object.__new__(search.Finder)
    f.db_uri = path.resolve().as_uri() + "?mode=ro"
    f.con = sqlite3.connect(path, check_same_thread=False)
    f._dv = f.con.execute("PRAGMA data_version").fetchone()[0]
    f.ids, f.M, _ = search.read_vectors(f.con)
    f.encoder = "e5-small"; f._top = int(f.ids.max())
    return f


def test_reload_waits_when_memory_is_short(tmp_path, monkeypatch, capsys):
    p = tmp_path / "i.db"
    w, _ = _vdb(p)
    f = _finder(p)
    w.execute("INSERT INTO chunks(file,n,text,emb) VALUES(1,0,'new',?)", (np.ones(8, np.float16).tobytes(),)); w.commit()
    monkeypatch.setattr(search, "_mem_free_gb", lambda: 0.2)
    n0 = len(f.ids)
    f._reloading = True; f._reload()
    assert len(f.ids) == n0 and f._dv is None and "FINDR_RELOAD later" in capsys.readouterr().out   # looked at again later
    monkeypatch.setattr(search, "_mem_free_gb", lambda: 8.0)
    f._reloading = True; f._reload()
    out = capsys.readouterr().out
    assert len(f.ids) == n0 + 1 and "(read 1)" in out


def test_a_slow_reload_waits_longer(tmp_path):
    p = tmp_path / "i.db"
    _vdb(p)
    f = _finder(p)
    f._reload_secs = 30.0; f._seen_at = time.time() - 120   # the last reload took 30 s, two minutes ago
    seen = []
    f.con.set_trace_callback(seen.append)
    f.refresh()
    assert not seen   # 60 s would have passed; ten times 30 s has not
    f._seen_at = time.time() - 301
    f.refresh()
    assert seen       # now it looks


# ---------- #1 the engine's threads and the indexer's batches ----------

def test_background_waits_for_a_question():
    lock = search.PersonFirstLock()
    order = []
    lock.acquire()                                   # a question is running
    asking = threading.Thread(target=lambda: (lock.acquire(), order.append("question"), time.sleep(0.1), lock.release()))
    asking.start()
    while not lock.waiting():
        time.sleep(0.01)
    bg = threading.Thread(target=lambda: (lock.acquire_background(), order.append("embed"), lock.release()))
    bg.start(); time.sleep(0.1)
    lock.release()
    asking.join(); bg.join()
    assert order == ["question", "embed"]


def test_background_is_not_starved_for_good():
    lock = search.PersonFirstLock()
    lock._waiting = 1                                # someone is always said to wait
    t = time.time()
    assert lock.acquire_background(patience=0.2)
    assert time.time() - t < 2
    lock.release()


def test_engine_threads(monkeypatch):
    monkeypatch.setattr(search, "ENGINE_THREADS", [None])
    monkeypatch.setenv("FINDR_ENGINE_THREADS", "3")
    assert search.engine_threads() == 3
    assert search.embed_threads(2) == 2 and search.embed_threads(0) == 0 and search.embed_threads(8) == 0
    monkeypatch.setattr(search, "ENGINE_THREADS", [None])
    monkeypatch.delenv("FINDR_ENGINE_THREADS")
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(search.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(torch, "get_num_threads", lambda: 4)
    assert search.engine_threads() == 3              # 4 cores, no hyper-threading: one left for the rest of the PC
    monkeypatch.setattr(search, "ENGINE_THREADS", [None])
    monkeypatch.setattr(search.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(torch, "get_num_threads", lambda: 8)
    assert search.engine_threads() == 8              # torch's own number where there are more logical cores


def test_indexer_sends_its_share(monkeypatch):
    sent = []

    class R:
        def read(self):
            return json.dumps({"ok": True, "dim": 2, "vecs": __import__("base64").b64encode(np.zeros((1, 2), np.float16).tobytes()).decode()}).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: sent.append(json.loads(req.data)) or R())
    e = index.EngineEncoder("http://x", "pw", "e5-small")
    monkeypatch.setenv("FINDR_THREADS", "4")
    e.vectors(["a"])
    monkeypatch.delenv("FINDR_THREADS")
    e.vectors(["b"])
    assert sent[0]["threads"] == 4 and "threads" not in sent[1]


def test_embed_route_uses_the_background_turn():
    src = Path(search.__file__).read_text(encoding="utf-8")
    body = src[src.index('path == "/embed"'):src.index('path == "/pause"')]
    assert "acquire_background()" in body and "embed_threads(asked)" in body and "with SEARCH_LOCK" not in body


# ---------- #2 #8 background runs give the PC back, stop before the disk is full ----------

@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(index, "_GB", {"t0": 0.0, "looked": 0.0, "disk": 0.0, "db": None})
    for k in ("FINDR_STOP_ON_INPUT", "FINDR_STOP_ON_BATTERY", "FINDR_STOP_GRACE"):
        monkeypatch.delenv(k, raising=False)


def test_give_back_only_when_asked(clock, monkeypatch):
    monkeypatch.setattr(index, "person_is_back", lambda within: True)
    index.give_back_check(now=100.0)                  # the person's own run: nothing set, never stops
    monkeypatch.setenv("FINDR_STOP_ON_INPUT", "60")
    with pytest.raises(index.GiveBack) as e:
        index.give_back_check(now=100.0)
    assert e.value.why == "input"


def test_grace_and_battery(clock, monkeypatch):
    import governor
    monkeypatch.setattr(index, "person_is_back", lambda within: False)
    monkeypatch.setattr(governor, "on_battery", lambda: True)
    monkeypatch.setenv("FINDR_STOP_ON_BATTERY", "1"); monkeypatch.setenv("FINDR_STOP_GRACE", "120")
    index.give_back_check(now=60.0)                   # the first two minutes of a refresh are always done
    with pytest.raises(index.GiveBack) as e:
        index.give_back_check(now=130.0)
    assert e.value.why == "battery"


def test_disk_check(clock, monkeypatch):
    monkeypatch.setattr(index, "disk_free_gb", lambda path=None: 0.4)
    with pytest.raises(index.GiveBack) as e:
        index.disk_check(now=100.0)
    assert e.value.why == "disk"
    index.disk_check(now=105.0)                       # looked at every DISK_EVERY seconds, not at every file


def _files(root, n):
    root.mkdir()
    for i in range(n):
        (root / f"보고서_{i:02d}.txt").write_text(f"회의록 내용 {i} " * 40, encoding="utf-8")


def _run(monkeypatch, root, db):
    monkeypatch.setenv("FINDR_READ_WORKERS", "1")
    monkeypatch.setattr(sys, "argv", ["index.py", "--root", str(root), "--db", str(db), "--no-embed"])
    return index.main()


def test_a_deep_run_gives_the_pc_back_and_keeps_what_it_read(tmp_path, monkeypatch, capsys):
    root, db = tmp_path / "docs", tmp_path / "i.db"
    _files(root, 30)
    monkeypatch.setenv("FINDR_OCR", "0")
    monkeypatch.setenv("FINDR_STOP_ON_INPUT", "60")
    monkeypatch.setattr(index, "LOOK_EVERY", 0)
    looks = []
    monkeypatch.setattr(index, "person_is_back", lambda within: looks.append(1) or len(looks) > 10)   # back after 10 files
    assert _run(monkeypatch, root, db) == 7
    assert "FINDR_YIELD input" in capsys.readouterr().out
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 30                     # every name is in
    read = con.execute("SELECT count(*) FROM files WHERE pending=0").fetchone()[0]
    assert 5 <= read < 30                                                                     # what was read is kept
    con.close()
    monkeypatch.setattr(index, "person_is_back", lambda within: False)
    assert not _run(monkeypatch, root, db)                                                    # the next run finishes
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM files WHERE pending!=0").fetchone()[0] == 0


def test_a_nearly_full_disk_stops_cleanly(tmp_path, monkeypatch, capsys):
    root, db = tmp_path / "docs", tmp_path / "i.db"
    _files(root, 20)
    monkeypatch.setenv("FINDR_OCR", "0")
    monkeypatch.setattr(index, "disk_free_gb", lambda path=None: 0.3)
    assert _run(monkeypatch, root, db) == 8
    assert "FINDR_DISKFULL" in capsys.readouterr().out


def test_a_full_disk_error_is_undone_not_a_crash(tmp_path, monkeypatch, capsys):
    root, db = tmp_path / "docs", tmp_path / "i.db"
    _files(root, 20)
    monkeypatch.setenv("FINDR_OCR", "0")

    def full(*a, **k):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(index, "remask", full)
    assert _run(monkeypatch, root, db) == 8
    assert "FINDR_DISKFULL" in capsys.readouterr().out


def test_the_app_asks_background_runs_to_give_back():
    rs = (ROOT / "app" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    body = rs[rs.index("fn start_refresher()"):rs.index("fn mark_refreshed()")]
    assert '"FINDR_STOP_ON_INPUT", "60"' in body and '"FINDR_STOP_ON_BATTERY", "1"' in body and '"FINDR_STOP_GRACE", "120"' in body
    assert "s.code() == Some(7)" in body


# ---------- #4 the waiting pieces' texts are not all held at once ----------

class FakeEngine:
    def __init__(self):
        self.batches = []

    def vectors(self, texts):
        self.batches.append(list(texts))
        return np.stack([np.full(4, len(t) % 7, np.float16) for t in texts])


def test_embed_reads_texts_a_batch_at_a_time(tmp_path, monkeypatch):
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT, pending INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB, emb2 BLOB);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES('encoder','e5-small');""")
    for i in range(300):
        con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,0)", (i, f"/d/f{i % 150}.txt", f"f{i % 150}.txt", ".txt", 1, i, 1))
        con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (i, 0, f"내용 {i % 150}"))   # each input twice (copies)
    con.commit()
    fake = FakeEngine()
    monkeypatch.setattr(index.EngineEncoder, "connect", classmethod(lambda cls, name: fake))
    index.embed(con, "e5-small")
    assert sum(len(b) for b in fake.batches) == 150                                           # copies computed once
    assert fake.batches[0][0] == index.embed_input("내용 149", "/d/f149.txt")                  # newest first, as before
    assert con.execute("SELECT count(*) FROM chunks WHERE emb IS NULL").fetchone()[0] == 0
    src = Path(index.__file__).read_text(encoding="utf-8")
    body = src[src.index("def embed(con"):src.index("def user_idle_seconds(")]
    assert ".fetchall()" not in body


# ---------- #10 the write-ahead file, VACUUM ----------

def test_write_ahead_file_cut_back(tmp_path):
    src = Path(index.__file__).read_text(encoding="utf-8")
    assert "PRAGMA journal_size_limit" in src and "wal_checkpoint(TRUNCATE)" in src
    con = sqlite3.connect(tmp_path / "i.db")
    con.execute("PRAGMA journal_mode=WAL"); con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES(?)", [(b"x" * 4000,) for _ in range(2000)]); con.commit()
    assert (tmp_path / "i.db-wal").stat().st_size > 1_000_000
    assert index.wal_trim(con) is not None
    assert (tmp_path / "i.db-wal").stat().st_size == 0


def test_vacuum_only_with_room(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT))
    from findr import cli
    import shutil
    db = tmp_path / "i.db"
    c = sqlite3.connect(db); c.execute("CREATE TABLE t(x)"); c.commit()
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    assert cli._vacuum(c, db) is False and "VACUUM" in capsys.readouterr().out
    monkeypatch.undo()
    assert cli._vacuum(c, db) is True


def test_switch_waits_for_disk_space(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    import enc_eg2

    class Fake:
        st, dev, bs = None, "cpu", 8

        def emb(self, texts, query=True):
            return torch.nn.functional.normalize(torch.ones(len(texts), 8), dim=-1)
    monkeypatch.setattr(enc_eg2, "make_encoder", lambda name, *a, **k: Fake())
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    c = sqlite3.connect(str(tmp_path / "index.db"))
    c.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB, emb2 BLOB);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES('encoder', 'e5-small');""")
    for i in range(40):
        c.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)", (i, f"/d/f{i}.txt", f"f{i}.txt", ".txt", 1, i, 1))
        c.execute("INSERT INTO chunks(file,n,text,emb) VALUES(?,?,?,?)", (i, i % 3, f"text {i}", np.ones(4, np.float16).tobytes()))
    c.commit()
    sorts = []
    c.set_trace_callback(lambda s: sorts.append(s) if "ORDER BY (c.n != 0)" in s else None)
    monkeypatch.setattr(index, "disk_free_gb", lambda path=None: 0.0)
    monkeypatch.setattr(index, "switch_room_gb", lambda con: 2.0)
    monkeypatch.setattr(index, "DISK_MIN_GB", -1.0)    # steps go on; only the switch needs room
    assert index.upgrade(c, "embeddinggemma-2", minutes=10) == 2
    assert enc_eg2.encoder_of(c) == "e5-small"                                                # not switched without room
    assert len(sorts) == 1                                                                     # #11: the order taken once
    monkeypatch.setattr(index, "disk_free_gb", lambda path=None: 50.0)
    assert index.upgrade(c, "embeddinggemma-2", minutes=10) == 0


# ---------- #13 LibreOffice, #12 the reranker on an Apple GPU ----------

def test_libreoffice_profile_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    monkeypatch.setattr(index, "_LO", [])
    held = open(tmp_path / "lo" / "slot0.lock", "a+") if (tmp_path / "lo").mkdir() is None else None
    import fcntl
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)   # another reader process holds the first profile
    arg = index.lo_profile()
    assert arg and arg[0].startswith("-env:UserInstallation=file://") and arg[0].endswith("/lo/slot1")
    assert index.lo_profile() == arg                             # the same one for the rest of this process
    held.close()


def test_mps_ranker_in_16_bits_on_small_macs(monkeypatch):
    torch = pytest.importorskip("torch")
    psutil = pytest.importorskip("psutil")
    monkeypatch.delenv("CE_MPS_FP16", raising=False)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("V", (), {"total": 8 * 2 ** 30})())
    assert search.mps_ranker_dtype() == torch.float16
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("V", (), {"total": 16 * 2 ** 30})())
    assert search.mps_ranker_dtype() == torch.float32            # 16 GB Macs keep their answers
    monkeypatch.setenv("CE_MPS_FP16", "1")
    assert search.mps_ranker_dtype() == torch.float16
