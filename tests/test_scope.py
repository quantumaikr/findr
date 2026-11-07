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


def test_real_roots_run(monkeypatch):
    """on this runner's real home and drives: no crash, and only existing folders"""
    rs = scope.user_roots()
    assert all(p.is_dir() for _, _, p in rs)
