"""#106: on battery the app takes in new files by name only. They are found by name at once and stay pending, so the next
full run (on the charger, or '지금 반영') reads their content"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import index

IDX = Path(index.__file__)


def run(root, db, home, *extra):
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(IDX), "--root", str(root), "--db", str(db), "--no-embed", *extra],
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)


def test_names_only_then_full_run_reads_content(tmp_path):
    a = tmp_path / "dl"; a.mkdir(); (a / "회의록.txt").write_text("분기 매출 회의", encoding="utf-8")
    db = tmp_path / "i.db"
    r = run(a, db, tmp_path, "--names-only")
    assert r.returncode == 0, r.stderr
    assert "FINDR_STAGE content" not in r.stdout and "FINDR_STAGE done" in r.stdout
    con = sqlite3.connect(db)
    assert con.execute("SELECT name, pending FROM files").fetchall() == [("회의록.txt", 1)]
    con.close()
    r = run(a, db, tmp_path)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    assert con.execute("SELECT pending FROM files").fetchone()[0] == 0
    assert any("분기 매출" in t for (t,) in con.execute("SELECT text FROM chunks"))
