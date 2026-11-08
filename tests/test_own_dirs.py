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


def _fake_checkout(d, name="findr-ai"):
    """the shape of the findr repository: findr/core/ and a pyproject.toml naming the project"""
    (d / "findr" / "core").mkdir(parents=True)
    (d / "pyproject.toml").write_text(f'[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "{name}"\nversion = "0"\n', encoding="utf-8")
    (d / "tests").mkdir()
    (d / "tests" / "test_rules.py").write_text('QUESTIONS = ["회의록", "사업계획서"]\n', encoding="utf-8")   # findr's example questions
    return d


def _index(root, db, home):
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home)}
    return subprocess.run([sys.executable, str(Path(scope.__file__).with_name("index.py")), "--root", str(root), "--db", str(db), "--no-embed"],
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def test_is_findr_checkout(tmp_path):
    assert scope.is_findr_checkout(_fake_checkout(tmp_path / "a"))
    assert scope.is_findr_checkout(_fake_checkout(tmp_path / "b", name="findr"))
    assert not scope.is_findr_checkout(_fake_checkout(tmp_path / "c", name="findr-tools"))   # another project
    (tmp_path / "d").mkdir(); (tmp_path / "d" / "pyproject.toml").write_text('[project]\nname = "findr-ai"\n', encoding="utf-8")
    assert not scope.is_findr_checkout(tmp_path / "d")   # no findr/core: not the repository
    assert not scope.is_findr_checkout(tmp_path / "missing")


def test_other_findr_copies_are_not_the_persons_files(tmp_path):
    """#119: a second clone in the home folder; its tests hold findr's example questions and outranked the person's minutes"""
    root = tmp_path / "home"; (root / "문서").mkdir(parents=True)
    (root / "문서" / "회의록.txt").write_text("주간 회의록", encoding="utf-8")
    _fake_checkout(root / "findr-copy")
    db = tmp_path / "i.db"
    r = _index(root, db, tmp_path / "data")
    assert r.returncode == 0, r.stderr[-500:]
    assert {n for (n,) in sqlite3.connect(db).execute("SELECT name FROM files")} == {"회의록.txt"}


def test_a_copy_already_indexed_leaves_the_index(tmp_path):
    """an index made before this change: the copy's files go on the next run, found by its pyproject.toml row"""
    root = tmp_path / "home"; (root / "문서").mkdir(parents=True)
    (root / "문서" / "회의록.txt").write_text("주간 회의록", encoding="utf-8")
    cp = root / "findr-copy"; cp.mkdir()
    (cp / "pyproject.toml").write_text('[project]\nname = "findr-ai"\n', encoding="utf-8")
    (cp / "README.md").write_text("회의록 사업계획서 예시", encoding="utf-8")
    db = tmp_path / "i.db"
    assert _index(root, db, tmp_path / "data").returncode == 0
    assert "README.md" in {n for (n,) in sqlite3.connect(db).execute("SELECT name FROM files")}   # not yet a checkout: indexed
    (cp / "findr" / "core").mkdir(parents=True)   # now it has the repository's shape
    r = _index(root, db, tmp_path / "data")
    assert r.returncode == 0 and "FINDR_EXCLUDE removed" in r.stdout
    assert {n for (n,) in sqlite3.connect(db).execute("SELECT name FROM files")} == {"회의록.txt"}


def test_a_copy_chosen_as_the_root_stays(tmp_path):
    """a developer indexing a checkout on purpose: the chosen root itself is still indexed"""
    root = _fake_checkout(tmp_path / "findr-dev")
    db = tmp_path / "i.db"
    assert _index(root, db, tmp_path / "data").returncode == 0
    assert "test_rules.py" in {n for (n,) in sqlite3.connect(db).execute("SELECT name FROM files")}


def test_whole_pc_roots_leave_out_findr_copies(tmp_path, monkeypatch):
    """a clone in the home folder used to become a '홈 폴더 기타' root, and a chosen root is kept on purpose: the copy
    came back on every run. Whole-PC roots now leave it out; a folder given by hand still works (above)"""
    home = tmp_path / "home"; home.mkdir()
    (home / "프로젝트").mkdir(); (home / "프로젝트" / "a.txt").write_text("x", encoding="utf-8")
    _fake_checkout(home / "findr-copy")
    monkeypatch.setattr(scope.Path, "home", classmethod(lambda cls: home))
    labels = {Path(p).name for _, _, p in scope.user_roots(mnt=str(tmp_path / "nomnt"))}
    assert "프로젝트" in labels and "findr-copy" not in labels
