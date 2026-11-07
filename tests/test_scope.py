"""whole-PC folder rules (#4, #5): the person's folders in, system / vendor / game folders out, nested folders walked once"""
import os
import sys

import pytest

import scope


def make(tmp_path, home_dirs, top_dirs):
    home = tmp_path / "Users" / "kim"
    for d in home_dirs:
        (home / d).mkdir(parents=True, exist_ok=True)
    top = tmp_path / "drive"
    for d in top_dirs:
        (top / d).mkdir(parents=True, exist_ok=True)
    return home, top


def roots(monkeypatch, home, top):
    monkeypatch.setattr(scope, "_places", lambda mnt: ([home], [top]))
    return {p.name: (o, lab) for o, lab, p in scope.user_roots()}


def test_home_level_folders_and_skips(tmp_path, monkeypatch):
    home, top = make(tmp_path,
                     ["Desktop", "Downloads", "Documents/Google Drive", "Pictures", "Movies", "AppData/Local", ".vscode", ".config",
                      "py_class", "PycharmProjects", "source/repos", "workspace", "Contacts", "Favorites", "Saved Games", "Library", "Applications"],
                     ["Boot", "system.sav", "hp드라이버", "Dell Drivers", "Nexon", "Riot Games", "EFI", "Windows", "Program Files", "Users",
                      "Download", "자료"])
    r = roots(monkeypatch, home, top)
    for want in ["Desktop", "Downloads", "Documents", "Pictures", "Movies", "py_class", "PycharmProjects", "source", "workspace", "Download", "자료"]:
        assert want in r, f"{want} should be indexed"
    for skip in ["AppData", ".vscode", ".config", "Contacts", "Favorites", "Saved Games", "Library", "Applications", "Boot", "system.sav",
                 "hp드라이버", "Dell Drivers", "Nexon", "Riot Games", "EFI", "Windows", "Program Files", "Users"]:
        assert skip not in r, f"{skip} should be left out"


def test_nested_cloud_folder_walked_once(tmp_path, monkeypatch):
    home, top = make(tmp_path, ["Documents/Google Drive", "Documents/Dropbox"], [])
    r = roots(monkeypatch, home, top)
    assert "Documents" in r and "Google Drive" not in r and "Dropbox" not in r


@pytest.mark.skipif(os.name != "nt", reason="Windows file attributes")
def test_windows_hidden_system_folder_skipped(tmp_path, monkeypatch):
    import ctypes
    home, top = make(tmp_path, ["Desktop"], ["보이는폴더", "숨은폴더"])
    assert ctypes.windll.kernel32.SetFileAttributesW(str(top / "숨은폴더"), 0x2 | 0x4)
    r = roots(monkeypatch, home, top)
    assert "보이는폴더" in r and "숨은폴더" not in r


def _junction(link, target):
    import subprocess
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
def test_windows_junctions_are_not_roots(tmp_path, monkeypatch):
    """the legacy 'Application Data' / 'Local Settings' junctions lead into AppData: hidden and system themselves, but
    stat() follows them to a plain folder, and is_symlink() is False for a junction"""
    import ctypes
    home, top = make(tmp_path, ["Desktop", "py_class", "AppData/Roaming", "AppData/Local"], ["자료", "elsewhere"])
    _junction(home / "Application Data", home / "AppData" / "Roaming")
    _junction(home / "Local Settings", home / "AppData" / "Local")
    for j in ("Application Data", "Local Settings"):
        assert ctypes.windll.kernel32.SetFileAttributesW(str(home / j), 0x2 | 0x4)
    _junction(home / "바로가기", top / "elsewhere")   # a plain (not hidden) junction: still not a folder of its own
    _junction(top / "링크", home / "Desktop")
    assert scope.is_link(home / "Local Settings") and scope.is_link(home / "바로가기") and not scope.is_link(home / "py_class")
    r = roots(monkeypatch, home, top)
    assert "py_class" in r and "자료" in r
    for skip in ["Application Data", "Local Settings", "바로가기", "링크"]:
        assert skip not in r, f"{skip} is a junction and should be left out"


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions")
def test_index_walk_skips_junctions(tmp_path):
    """'Documents/My Music' -> Music: the indexer would walk Music a second time through it"""
    docs, music = tmp_path / "Documents", tmp_path / "Music"
    docs.mkdir(); music.mkdir()
    (docs / "보고서.txt").write_text("분기 보고서", encoding="utf-8")
    (music / "노래 가사.txt").write_text("가사", encoding="utf-8")
    _junction(docs / "My Music", music)
    names, out = _scan(tmp_path, [docs])
    assert "보고서.txt" in names and "노래 가사.txt" not in names, out


def test_real_roots_run(monkeypatch):
    """on this runner's real home and drives: no crash, and only existing folders"""
    rs = scope.user_roots()
    assert all(p.is_dir() for _, _, p in rs)


def test_app_browser_profile_marker():
    assert scope.app_profile(["SingletonLock", "SingletonSocket", "cef_log.log"])
    assert not scope.app_profile(["Local State.txt", "회의록.docx"])   # one look-alike name is not enough


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="special files (FIFO) on macOS / Linux")
def test_scan_skips_app_profile_and_special_files(tmp_path):
    """CapCut keeps a Chromium (CEF) profile in ~/Movies; its files and its socket are not the person's"""
    import sqlite3, subprocess
    movies = tmp_path / "Movies"
    proj = movies / "CapCut" / "User Data" / "Projects"; proj.mkdir(parents=True)
    (proj / "여행 영상 편집.txt").write_text("제주 여행 브이로그 컷 편집 메모")
    cef = movies / "CapCut" / "User Data" / "CEF"; (cef / "Dictionaries").mkdir(parents=True)
    for n in ("SingletonLock", "SingletonCookie", "LocalPrefs.json"):
        (cef / n).write_text("x")
    (cef / "Dictionaries" / "en.bdic").write_text("x")
    os.mkfifo(tmp_path / "s")   # a special file next to real files (like CEF's SingletonSocket); reading it would block
    db = tmp_path / "i.db"
    env = {**os.environ, "FINDR_SCOPE": "user", "FINDR_OCR": "0"}
    r = subprocess.run([sys.executable, str(scope.Path(scope.__file__).with_name("index.py")), "--root", str(movies), str(tmp_path),
                        "--db", str(db), "--no-embed"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    names = {n for (n,) in sqlite3.connect(db).execute("SELECT name FROM files")}
    assert "여행 영상 편집.txt" in names
    assert not names & {"SingletonLock", "SingletonCookie", "LocalPrefs.json", "en.bdic", "s"}


def _scan(tmp_path, roots, **env):
    import sqlite3, subprocess
    db = tmp_path / "i.db"
    e = {**os.environ, "FINDR_SCOPE": "user", "FINDR_OCR": "0", **env}
    r = subprocess.run([sys.executable, str(scope.Path(scope.__file__).with_name("index.py")), "--root", *map(str, roots),
                        "--db", str(db), "--no-embed"], env=e, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return {n for (n,) in sqlite3.connect(db).execute("SELECT name FROM files")}, r.stdout


def test_exclude_and_dropped_root(tmp_path):
    """exclude.txt folders leave the index (any case on Windows); a folder no longer searched leaves it too (#17)"""
    docs, dev, old = tmp_path / "Documents", tmp_path / "Dev", tmp_path / "miniforge3"
    for d, names in ((docs, ["견적서.txt", "회의록.txt", "계약서.txt"]), (dev / "proj", ["main.txt"]), (old, ["conda.txt"])):
        d.mkdir(parents=True)
        for n in names:
            (d / n).write_text("내용 " + n, encoding="utf-8")
    names, _ = _scan(tmp_path, [docs, dev, old])
    assert {"견적서.txt", "main.txt", "conda.txt"} <= names
    ex = str(dev / "proj").upper() if os.name == "nt" else str(dev / "proj")
    names, out = _scan(tmp_path, [docs, dev], FINDR_EXCLUDE=ex)   # miniforge3 is no longer a root; Dev/proj is excluded
    assert "main.txt" not in names and "conda.txt" not in names, out
    assert {"견적서.txt", "회의록.txt", "계약서.txt"} <= names
    names, out = _scan(tmp_path, [old])   # most of the index outside the roots (a missing drive?): nothing removed
    assert "견적서.txt" in names and "FINDR_OUTSIDE kept" in out
