"""a file moved or renamed between two runs keeps what was read from it: its row takes the new path and name (text,
summary and history stay), only its vectors are made again. Two files that look the same are left as new files."""
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import index

IDX = Path(index.__file__)


def run(roots, db, home):
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home)}
    return subprocess.run([sys.executable, str(IDX), "--root", *map(str, roots), "--db", str(db), "--no-embed"],
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)


def rows(db):
    con = sqlite3.connect(db)
    return {n: (fid, p) for fid, n, p in con.execute("SELECT id, name, path FROM files")}


def text(db, fid):
    return sqlite3.connect(db).execute("SELECT text FROM chunks WHERE file=? AND n=0", (fid,)).fetchone()[0]


def test_a_renamed_file_keeps_its_row_and_text(tmp_path):
    root = tmp_path / "docs"; root.mkdir()
    (root / "분기 보고.txt").write_text("3분기 매출 보고 내용입니다", encoding="utf-8")
    db = tmp_path / "i.db"
    assert run([root], db, tmp_path).returncode == 0
    fid0, _ = rows(db)["분기 보고.txt"]
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS cards(file INTEGER PRIMARY KEY, outline TEXT, summary TEXT, done INT DEFAULT 0)")
    con.execute("INSERT INTO cards(file, summary) VALUES(?, 'kept summary')", (fid0,)); con.commit(); con.close()
    (root / "sub").mkdir()
    (root / "분기 보고.txt").rename(root / "sub" / "3분기 보고_최종.txt")
    r = run([root], db, tmp_path)
    assert r.returncode == 0 and "FINDR_MOVED 1" in r.stdout
    got = rows(db)
    assert list(got) == ["3분기 보고_최종.txt"]
    fid1, path1 = got["3분기 보고_최종.txt"]
    assert fid1 == fid0 and path1.endswith("sub/3분기 보고_최종.txt")
    assert "매출" in text(db, fid1)
    con = sqlite3.connect(db)
    assert con.execute("SELECT summary FROM cards WHERE file=?", (fid1,)).fetchone()[0] == "kept summary"
    assert con.execute("SELECT count(*) FROM chunks WHERE file=? AND emb IS NOT NULL", (fid1,)).fetchone()[0] == 0
    # the full-text index knows the new name, not the old one
    assert con.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", ('"보고_최종"',)).fetchone()[0] >= 1


def test_a_move_between_roots_is_found(tmp_path):
    a, b = tmp_path / "Downloads", tmp_path / "Documents"; a.mkdir(); b.mkdir()
    (a / "계약서.txt").write_text("갑과 을은 다음과 같이 계약한다", encoding="utf-8")
    db = tmp_path / "i.db"
    run([a, b], db, tmp_path)
    fid0, _ = rows(db)["계약서.txt"]
    shutil.move(str(a / "계약서.txt"), str(b / "계약서.txt"))
    r = run([a, b], db, tmp_path)
    assert "FINDR_MOVED 1" in r.stdout
    fid1, p1 = rows(db)["계약서.txt"]
    assert fid1 == fid0 and "/Documents/" in p1


def test_look_alike_files_are_not_guessed(tmp_path):
    root = tmp_path / "d"; root.mkdir()
    for n in ("a.txt", "b.txt"):
        (root / n).write_text("same", encoding="utf-8")
    t = 1700000000
    for n in ("a.txt", "b.txt"):
        os.utime(root / n, (t, t))   # same size, same time: which went where cannot be told
    db = tmp_path / "i.db"
    run([root], db, tmp_path)
    old = {fid for fid, _ in rows(db).values()}
    (root / "a.txt").rename(root / "x.txt"); (root / "b.txt").rename(root / "y.txt")
    r = run([root], db, tmp_path)
    assert "FINDR_MOVED" not in r.stdout
    assert sorted(rows(db)) == ["x.txt", "y.txt"] and not old & {fid for fid, _ in rows(db).values()}


def test_a_deleted_file_still_leaves(tmp_path):
    root = tmp_path / "d"; root.mkdir()
    (root / "a.txt").write_text("one", encoding="utf-8"); (root / "b.txt").write_text("two two", encoding="utf-8")
    db = tmp_path / "i.db"
    run([root], db, tmp_path)
    (root / "a.txt").unlink()
    r = run([root], db, tmp_path)
    assert "FINDR_MOVED" not in r.stdout and sorted(rows(db)) == ["b.txt"]
