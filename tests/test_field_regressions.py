"""regressions found by field tests on a real M1 Pro (2026-11-07): each test names the issue it guards"""
import json
import os
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path

import numpy as np

CORE = Path(__file__).resolve().parent.parent / "findr" / "core"
NFD = lambda s: unicodedata.normalize("NFD", s)


def _index(tmp_path, root, **env):
    db = tmp_path / "i.db"
    e = {**os.environ, "FINDR_OCR": "0", **env}
    r = subprocess.run([sys.executable, str(CORE / "index.py"), "--root", str(root), "--db", str(db), "--no-embed"],
                       env=e, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return db, r.stdout


def test_nfc_move_resumes_where_it_stopped(tmp_path):
    """#23: a stopped NFC move goes on from meta.nfc_upto instead of starting over"""
    root = tmp_path / "docs"; root.mkdir()
    for i in range(5):
        (root / f"회의록_{i}.txt").write_text(f"회의 {i}", encoding="utf-8")
    db, _ = _index(tmp_path, root)
    con = sqlite3.connect(db)
    ids = [i for (i,) in con.execute("SELECT id FROM files ORDER BY id")]
    for fid, name in con.execute("SELECT id, name FROM files").fetchall():   # back to how a pre-#12 macOS index held them
        con.execute("UPDATE files SET name=? WHERE id=?", (NFD(name), fid))
    con.execute("DELETE FROM meta WHERE key='nfc'")
    con.execute("INSERT OR REPLACE INTO meta VALUES('nfc_upto', ?)", (str(ids[1]),))   # stopped after the first two files
    con.commit(); con.close()
    _, out = _index(tmp_path, root)
    assert f"FINDR_NFC_PROGRESS {len(ids) - 2}/{len(ids) - 2}" in out, out   # only the rest was walked
    con = sqlite3.connect(db)
    names = dict(con.execute("SELECT id, name FROM files"))
    assert all(names[i] == unicodedata.normalize("NFC", names[i]) for i in ids[2:])
    assert all(names[i] != unicodedata.normalize("NFC", names[i]) for i in ids[:2])   # done before the stop: not redone
    meta = dict(con.execute("SELECT key, value FROM meta"))
    assert meta.get("nfc") == "1" and "nfc_upto" not in meta


def test_name_list_and_cards_use_an_index(tmp_path):
    """#21: load_scan's queries must not walk all of chunks (25 s each on a 5.7 GB index)"""
    root = tmp_path / "docs"; root.mkdir(); (root / "a.txt").write_text("x", encoding="utf-8")
    db, _ = _index(tmp_path, root)
    con = sqlite3.connect(db)
    for q in ("SELECT c.id, c.text FROM chunks c JOIN files f ON f.id=c.file WHERE c.n=-1 AND f.tier=1 ORDER BY c.id",
              "SELECT c.id, f.name, f.tier FROM files f JOIN chunks c ON c.file=f.id AND c.n=0 ORDER BY f.id"):
        plan = " ".join(r[-1] for r in con.execute("EXPLAIN QUERY PLAN " + q))
        assert "SCAN c" not in plan and "ch_n" in plan, plan


def test_refresh_counts_only_after_the_indexer_commits(tmp_path):
    """#21: refresh() must not run the full-table count when nothing was committed since the last look"""
    import search
    db = tmp_path / "s.db"
    w = sqlite3.connect(db)
    w.executescript("CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);"
                    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES('encoder', 'e5-small');")
    v = np.zeros(4, np.float16).tobytes()
    w.execute("INSERT INTO chunks VALUES(1, 1, 0, 'a', ?)", (v,)); w.commit()
    f = search.Finder.__new__(search.Finder)
    f.db_uri = db.resolve().as_uri() + "?mode=ro"
    f.con = sqlite3.connect(db, check_same_thread=False)
    f._dv = f.con.execute("PRAGMA data_version").fetchone()[0]
    f.ids, f.encoder, f._top = np.array([1]), "e5-small", 1
    seen = []
    f.con.set_trace_callback(seen.append)
    f.refresh(every=0)
    assert not any("count(*)" in s for s in seen), seen   # unchanged: no scan
    w.execute("INSERT INTO chunks VALUES(2, 2, 0, 'b', ?)", (v,)); w.commit()   # the indexer adds a vector
    seen.clear()
    f.refresh(every=0)
    assert not any("count(*)" in s for s in seen), seen   # changed: still no scan on the question's connection (#67)
    import time
    for _ in range(100):
        if not getattr(f, "_reloading", False):
            break
        time.sleep(0.05)
    assert len(f.ids) == 2   # counted and reloaded on the reload thread


def test_upgrade_skips_where_the_gpu_self_check_chose_small(tmp_path, monkeypatch):
    """#27: the idle-time switch to the large model respects device_check.json (big_ok=false)"""
    sys.path.insert(0, str(CORE.parent.parent))
    from findr import cli, models, paths
    monkeypatch.setattr(paths, "HOME", tmp_path)
    (tmp_path / "device_check.json").write_text(json.dumps({"v": 2, "device": "mps", "big_ok": False, "pieces_per_s": 9.1}))
    db = tmp_path / "index.db"
    c = sqlite3.connect(db); c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO meta VALUES('encoder', 'e5-small')"); c.commit(); c.close()
    monkeypatch.setattr(cli, "_ram_gb", lambda: 16.0)
    monkeypatch.setattr(models, "gpu", lambda: ("mps", 0.0))
    monkeypatch.setattr(models, "big_ok", lambda: False)
    assert "9.1" in cli._upgrade_why_not(db)
    monkeypatch.setattr(models, "big_ok", lambda: True)
    assert cli._upgrade_why_not(db) == ""
    monkeypatch.setattr(models, "gpu", lambda: ("cuda", 24.0))
    assert cli._upgrade_why_not(db) == ""


def test_mac_starts_small_and_measures_after_the_download(tmp_path, monkeypatch):
    """an Apple GPU installs the small meaning model (both field Macs ran the large one 12-13.5x slower and fell back after
    downloading 1.2 GB and measuring for minutes); the idle-time upgrade downloads first, then measures: asked before the
    download, a missing model was judged slow for good"""
    sys.path.insert(0, str(CORE.parent.parent))
    from findr import cli, models, paths
    monkeypatch.setattr(models, "gpu", lambda: ("mps", 0.0))
    monkeypatch.setattr(models, "ram_gb", lambda: 16.0)
    _, _, want = models.plan()
    assert "multilingual-e5-small" in want and "embeddinggemma-2" not in want
    monkeypatch.setattr(models, "ram_gb", lambda: 32.0)
    assert "checker" in models.plan()[2]   # a 32 GB Mac still gets the side-by-side checker
    monkeypatch.setattr(models, "gpu", lambda: ("cuda", 16.0))
    assert "embeddinggemma-2" in models.plan()[2]   # an NVIDIA GPU keeps the large model from the start

    monkeypatch.setattr(models, "gpu", lambda: ("mps", 0.0))
    monkeypatch.setattr(paths, "HOME", tmp_path); monkeypatch.setattr(paths, "MODELS", tmp_path / "models")
    db = tmp_path / "index.db"
    c = sqlite3.connect(db); c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO meta VALUES('encoder', 'e5-small')"); c.commit(); c.close()
    monkeypatch.setattr(cli, "_ram_gb", lambda: 16.0)
    asked = []
    monkeypatch.setattr(models, "big_ok", lambda: asked.append(1) or False)
    assert cli._upgrade_why_not(db) == "" and asked == []   # not measured yet: no verdict before the model is here
    pulled = []
    monkeypatch.setattr(models, "pull", lambda fs, **kw: pulled.extend(fs))
    a = type("A", (), {"db": str(db), "minutes": 1})()
    monkeypatch.setenv("LANE2", "0")
    assert cli.cmd_upgrade(a) == 3 and pulled == ["embeddinggemma-2"] and asked   # downloaded, measured, slow: stays small
    assert (tmp_path / "upgrade_off").exists()
    # the second meaning lane (on by itself with this much memory): the same slow Mac still reads each file's first piece
    import governor
    monkeypatch.setattr(governor, "mem_available_gb", lambda: 8.0)   # free memory pinned (a CI runner has little)
    monkeypatch.delenv("LANE2")
    monkeypatch.setattr(models, "have", lambda name: True)
    ran = []
    monkeypatch.setattr(cli.subprocess, "call", lambda args, **kw: ran.append(args) or 2)
    assert cli.cmd_upgrade(a) == 2 and "--upgrade-first" in ran[0]
    monkeypatch.setattr(cli, "_ram_gb", lambda: 7.9)   # an 8 GB PC: no second encoder beside the first
    (tmp_path / "upgrade_off").write_text("이 기기에서 큰 모델이 느림", encoding="utf-8")
    assert cli.cmd_upgrade(a) == 3 and len(ran) == 1


def test_second_lane_is_on_by_memory_unless_said(monkeypatch):
    """LANE2 not set: on from 12 GB of memory (a PC sold as 12 GB reports a little less), off when memory cannot be read"""
    sys.path.insert(0, str(CORE.parent.parent))
    from findr import cli
    import governor
    monkeypatch.setattr(governor, "mem_available_gb", lambda: 8.0)   # free memory is its own test (test_pc_comfort)
    monkeypatch.delenv("LANE2", raising=False)
    for gb, on in ((0.0, False), (7.9, False), (11.8, True), (64.0, True)):
        monkeypatch.setattr(cli, "_ram_gb", lambda gb=gb: gb)
        assert cli._lane2() is on, gb
    monkeypatch.setenv("LANE2", "0"); assert not cli._lane2()
    monkeypatch.setattr(cli, "_ram_gb", lambda: 4.0)
    monkeypatch.setenv("LANE2", "1"); assert cli._lane2()
