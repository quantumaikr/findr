"""deleted files leave the index without a walk of every folder: only indexed folders whose time changed since the last
check are listed (index.py --gone-only, findr index --gone-only)."""
import os
import sqlite3
import time
from pathlib import Path

import index


def _db(paths):
    con = sqlite3.connect(":memory:")
    con.executescript("""
      CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
      CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
      CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    for p in paths:
        fid = con.execute("INSERT INTO files(path,name) VALUES(?,?)", (Path(p).as_posix(), Path(p).name)).lastrowid
        con.execute("INSERT INTO chunks(file,n,text) VALUES(?,0,'')", (fid,))
    return con


def _tree(root, n_keep=4):
    (root / "a").mkdir(parents=True); (root / "b" / "c").mkdir(parents=True)
    fs = [root / "a" / "x.pdf", root / "a" / "y.pdf", root / "b" / "c" / "z.hwp"] + [root / f"k{i}.txt" for i in range(n_keep)]
    for f in fs:
        f.write_text("x")
    return fs


def _left(con):
    return sorted(Path(p).name for (p,) in con.execute("SELECT path FROM files"))


def test_a_deleted_file_leaves(tmp_path):
    fs = _tree(tmp_path)
    con = _db(fs)
    fs[0].unlink()
    st = index.gone_check(con, [tmp_path])
    assert st["removed"] == 1 and "x.pdf" not in _left(con)
    assert con.execute("SELECT count(*) FROM chunks WHERE file NOT IN (SELECT id FROM files)").fetchone()[0] == 0


def test_a_deleted_folder_takes_its_files(tmp_path):
    fs = _tree(tmp_path)
    con = _db(fs)
    for f in (fs[2],):
        f.unlink()
    (tmp_path / "b" / "c").rmdir()
    st = index.gone_check(con, [tmp_path])
    assert st["removed"] == 1 and "z.hwp" not in _left(con)


def test_unchanged_folders_are_not_listed(tmp_path):
    fs = _tree(tmp_path)
    con = _db(fs)
    index.gone_check(con, [tmp_path], now=time.time() + 60)   # as if checked a minute from now: every folder is older
    st = index.gone_check(con, [tmp_path])
    assert st["folders"] == 3 and st["listed"] == 0 and st["removed"] == 0


def test_only_the_changed_folder_is_listed(tmp_path):
    fs = _tree(tmp_path)
    con = _db(fs)
    index.gone_check(con, [tmp_path], now=time.time() + 60)
    fs[1].unlink()
    later = time.time() + 120
    os.utime(tmp_path / "a", (later, later))   # the folder's time moves with the deletion (set here, past the stamp)
    st = index.gone_check(con, [tmp_path])
    assert st["listed"] == 1 and st["removed"] == 1 and "y.pdf" not in _left(con)


def test_a_missing_root_is_kept(tmp_path):
    root = tmp_path / "usb"
    fs = _tree(root)
    con = _db(fs)
    root.rename(tmp_path / "elsewhere")   # the drive is not there now
    st = index.gone_check(con, [root])
    assert st["removed"] == 0 and len(_left(con)) == len(fs)


def test_more_than_half_at_once_is_kept(tmp_path):
    fs = _tree(tmp_path, n_keep=0)   # three files; two gone is more than half
    con = _db(fs)
    fs[0].unlink(); fs[1].unlink()
    st = index.gone_check(con, [tmp_path])
    assert st["removed"] == 0 and st["kept"] == 2 and len(_left(con)) == 3


def test_files_outside_the_roots_are_not_touched(tmp_path):
    a, b = tmp_path / "one", tmp_path / "two"
    fa, fb = _tree(a), _tree(b)
    con = _db(fa + fb)
    fb[0].unlink()
    index.gone_check(con, [a])   # only root one is checked
    assert "x.pdf" in _left(con) and _left(con).count("x.pdf") == 2
