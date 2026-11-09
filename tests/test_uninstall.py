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
