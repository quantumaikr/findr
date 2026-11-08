"""what indexing must never do (review of 0.2.14): forget a whole folder because its disk is unplugged, mix up folders whose
names differ by _ or %, run twice on one index, store a locked file as empty for good, or pair texts with the wrong files
after a reader worker died; secret folders and shell histories are never read"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import index

IDX = Path(index.__file__)


def run(roots, db, home):
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home)}
    return subprocess.run([sys.executable, str(IDX), "--root", *map(str, roots), "--db", str(db), "--no-embed"],
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)


def names(db):
    return sorted(n for (n,) in sqlite3.connect(db).execute("SELECT name FROM files"))


def test_unplugged_root_keeps_its_files(tmp_path):
    a = tmp_path / "usb"; a.mkdir(); (a / "보고서.txt").write_text("분기 보고", encoding="utf-8")
    db = tmp_path / "i.db"
    assert run([a], db, tmp_path).returncode == 0 and names(db) == ["보고서.txt"]
    a.rename(tmp_path / "usb_away")   # the disk is unplugged
    r = run([a], db, tmp_path)
    assert r.returncode == 0 and "FINDR_ROOT_MISSING" in r.stdout and names(db) == ["보고서.txt"]


def test_underscore_folder_is_not_its_neighbour(tmp_path):
    for d, f in (("my_docs", "a.txt"), ("my-docs", "b.txt")):
        (tmp_path / d).mkdir(); (tmp_path / d / f).write_text("x", encoding="utf-8")
    db = tmp_path / "i.db"
    run([tmp_path / "my-docs"], db, tmp_path)
    run([tmp_path / "my_docs"], db, tmp_path)   # used to claim my-docs' files as 'gone' (LIKE: _ is any character)
    assert names(db) == ["a.txt", "b.txt"]


def test_second_indexer_waits_its_turn(tmp_path):
    (tmp_path / "d").mkdir(); (tmp_path / "d" / "a.txt").write_text("x")
    db = tmp_path / "i.db"
    assert index.one_indexer(db)   # this test process holds the index
    r = run([tmp_path / "d"], db, tmp_path)
    assert r.returncode == 3 and "FINDR_BUSY" in r.stdout
    index._LOCK.close(); index._LOCK = None
    assert run([tmp_path / "d"], db, tmp_path).returncode == 0   # the lock went with its holder


def test_whole_pc_run_passes_on_the_indexer_exit_code(tmp_path):
    """`findr index` (whole PC) runs index_pc.py, which used to exit 0 even when index.py refused with FINDR_BUSY (3), so a
    script or timer could not tell a run that never happened from one that finished (chosen folders already passed it on)"""
    home = tmp_path / "home"; (home / "Desktop").mkdir(parents=True); (home / "Desktop" / "a.txt").write_text("x")
    db = tmp_path / "i.db"
    assert index.one_indexer(db)   # another run holds this index
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "FINDR_OCR": "0", "FINDR_HOME": str(tmp_path), "FINDR_DATA": str(tmp_path)}
    try:
        r = subprocess.run([sys.executable, str(IDX.with_name("index_pc.py")), "--db", str(db), "--model", "none"],
                           env=env, capture_output=True, text=True, timeout=180)
    finally:
        index._LOCK.close(); index._LOCK = None
    assert "FINDR_BUSY" in r.stdout and "(exit 3)" in r.stdout
    assert r.returncode == 3


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs a file the user cannot read")
def test_unreadable_file_stays_pending(tmp_path):
    f = tmp_path / "잠김.txt"; f.write_text("내용", encoding="utf-8"); f.chmod(0)
    try:
        assert index._read_one((str(f), 1000)) is None   # read again next run, not stored as empty for good
    finally:
        f.chmod(0o644)


def test_dead_worker_keeps_texts_with_their_files(tmp_path, monkeypatch):
    jobs = []
    for i in range(20):
        p = tmp_path / f"f{i}.txt"; p.write_text(f"text {i}", encoding="utf-8"); jobs.append((str(p), 1000))

    class Pool:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def map(self, fn, js, chunksize=1):
            for j in js[:6]:
                yield fn(j)
            raise RuntimeError("a worker died")

    import concurrent.futures
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Pool)
    monkeypatch.setenv("FINDR_READ_WORKERS", "4")
    out = list(index.read_all(jobs))
    assert len(out) == 20
    assert out[:6] == [f"text {i}" for i in range(6)] and out[6:10] == [None] * 4   # the chunk in the dead worker: next run
    assert out[10:] == [f"text {i}" for i in range(10, 20)]   # every later text with its own file


def test_secret_folders_and_histories_are_not_read(tmp_path):
    h = tmp_path / "home"
    (h / ".kube").mkdir(parents=True); (h / ".kube" / "config").write_text("client-key-data: AAAA", encoding="utf-8")
    (h / ".zsh_history").write_text("export TOKEN=abc", encoding="utf-8")
    (h / "메모.txt").write_text("회의", encoding="utf-8")
    db = tmp_path / "i.db"
    run([h], db, tmp_path)
    con = sqlite3.connect(db)
    assert "config" not in names(db)
    assert con.execute("SELECT tier FROM files WHERE name='.zsh_history'").fetchone() in (None, (0,))


def test_like_prefix_escapes_wildcards():
    assert index.like_pre("/a/my_docs/") == r"/a/my\_docs/%" and index.like_pre("/b/100%/") == r"/b/100\%/%"
