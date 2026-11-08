"""Windows: files deeper than 260 characters were left out of the index silently (LongPathsEnabled is off by default, #60)"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import index

DEEP = ["very_long_project_folder_name_for_path_test_%d" % i for i in range(1, 10)]


def test_short_and_long_forms():
    if os.name == "nt":
        assert index._long("C:\\a\\b") == "\\\\?\\C:\\a\\b" and index._short("\\\\?\\C:\\a\\b") == "C:\\a\\b"
        assert index._long("\\\\srv\\share\\x") == "\\\\?\\UNC\\srv\\share\\x" and index._short("\\\\?\\UNC\\srv\\share\\x") == "\\\\srv\\share\\x"
        assert index._long("rel\\x") == "rel\\x"
    else:
        assert index._long("/a/b") == "/a/b" and index._short("/a/b") == "/a/b"


@pytest.mark.skipif(os.name != "nt", reason="the 260-character limit is Windows'")
def test_deep_file_is_indexed_and_read(tmp_path):
    deep = os.path.join(str(tmp_path), *DEEP)
    os.makedirs(index._long(deep))
    f = os.path.join(deep, "long_path_report.txt")
    assert len(f) > 300
    with open(index._long(f), "w", encoding="utf-8") as fo:
        fo.write("quarterly results deep inside")
    (tmp_path / "near.txt").write_text("near the top", encoding="utf-8")
    db = tmp_path / "i.db"
    r = subprocess.run([sys.executable, str(Path(index.__file__)), "--root", str(tmp_path), "--db", str(db), "--no-embed"],
                       env={**os.environ, "FINDR_OCR": "0"}, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    rows = dict(sqlite3.connect(db).execute("SELECT name, path FROM files").fetchall())
    assert "long_path_report.txt" in rows and "near.txt" in rows, r.stdout[-500:]
    assert not rows["long_path_report.txt"].startswith("//?/"), "stored in the usual form"
    assert "quarterly results" in index.extract(Path(f), index.MAXREAD)
    assert "FINDR_SKIPPED" not in r.stdout
