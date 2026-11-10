"""findr uninstall: the data folder only, and never a folder that is not findr's"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(env_home, *args):
    env = dict(os.environ, FINDR_HOME=str(env_home), PYTHONUTF8="1")
    return subprocess.run([sys.executable, "-m", "findr", "uninstall", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_removes_the_data_folder_and_nothing_else(tmp_path):
    h = tmp_path / "findr"; (h / "models").mkdir(parents=True); (h / "index.db").write_text("x")
    mine = tmp_path / "my_document.txt"; mine.write_text("keep me")
    r = run(h, "--yes")
    assert r.returncode == 0 and not h.exists() and mine.exists()


def test_a_folder_without_findr_in_it_is_never_removed(tmp_path):
    """FINDR_HOME pointed at a person's own folder (a typo, FINDR_HOME=~): nothing is removed"""
    h = tmp_path / "home"; h.mkdir(); (h / "thesis.docx").write_text("keep me")
    r = run(h, "--yes")
    assert r.returncode == 1 and (h / "thesis.docx").exists()


def test_the_windows_app_installed_in_the_same_folder_stays_for_its_uninstaller(tmp_path):
    """Windows (11/11): the app lives in %LOCALAPPDATA%\\findr with the data; its files and uninstaller must stay"""
    h = tmp_path / "findr"; (h / "runtime").mkdir(parents=True); (h / "engine").mkdir(); (h / "index.db").write_text("x")
    for n in ("findr-desktop.exe", "uninstall.exe", "uv.exe"):
        (h / n).write_text("app")
    r = run(h, "--yes")
    assert r.returncode == 0 and sorted(p.name for p in h.iterdir()) == ["engine", "findr-desktop.exe", "uninstall.exe", "uv.exe"]
