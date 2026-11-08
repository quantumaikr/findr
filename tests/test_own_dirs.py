"""findr's own checkout and data folder are not the person's files (#66: findr's README came up first for a question about a
file that is not on the PC)"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import scope


def test_own_dirs_include_checkout_and_data(monkeypatch, tmp_path):
    monkeypatch.setenv("FINDR_HOME", str(tmp_path / "findr-data"))
    own = scope.own_dirs()
    assert Path(scope.__file__).resolve().parents[2] in own
    assert (tmp_path / "findr-data").resolve() in own


def test_walk_skips_a_findr_checkout_and_data_folder_inside_a_root(tmp_path):
    root = tmp_path / "home"
    (root / "문서").mkdir(parents=True); (root / "문서" / "견적서.txt").write_text("견적", encoding="utf-8")
    data = root / "findr-test-data"; data.mkdir(); (data / "progress.txt").write_text("x", encoding="utf-8")
    db = tmp_path / "i.db"
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_HOME": str(data)}
    r = subprocess.run([sys.executable, str(Path(scope.__file__).with_name("index.py")), "--root", str(root), "--db", str(db), "--no-embed"],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    names = {n for (n,) in sqlite3.connect(db).execute("SELECT name FROM files")}
    assert "견적서.txt" in names and "progress.txt" not in names, names


def test_a_chosen_root_that_is_own_stays(tmp_path):
    """indexing the checkout on purpose (a developer) still works: only folders inside a chosen root are left out"""
    root = tmp_path / "findr-data"; root.mkdir(); (root / "메모.txt").write_text("메모", encoding="utf-8")
    db = tmp_path / "i.db"
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_HOME": str(root)}
    r = subprocess.run([sys.executable, str(Path(scope.__file__).with_name("index.py")), "--root", str(root), "--db", str(db), "--no-embed"],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    assert "메모.txt" in {n for (n,) in sqlite3.connect(db).execute("SELECT name FROM files")}
