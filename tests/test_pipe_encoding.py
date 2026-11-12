"""findr's piped output on Windows is in the encoding its reader expects (#66-10): a console window's output code page
(PowerShell / cmd decode with it: UTF-8 there showed as mojibake on Korean Windows), UTF-8 with no console window (the
app, AI assistants, editors), and never an error on a character the code page lacks."""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from findr import textout  # noqa: E402


def _pipe():
    b = io.BytesIO()
    return b, io.TextIOWrapper(b, encoding="cp1252", errors="strict", write_through=True)


def test_a_console_window_gets_its_code_page():
    b, s = _pipe(); env = {}
    assert textout.fit([s], console_cp=949, environ=env, windows=True) == "cp949"
    s.write("분기 보고 ✓"); s.flush()
    assert b.getvalue() == "분기 보고 ?".encode("cp949")   # a character cp949 lacks: '?', not a crash
    assert env["PYTHONIOENCODING"] == "cp949:replace"   # the indexer started from here writes the same


def test_no_console_window_gets_utf8():
    for cp in (0, 65001, 99999):   # none (app, assistant), 'chcp 65001', a code page Python does not know
        b, s = _pipe()
        assert textout.fit([s], console_cp=cp, environ={}, windows=True) == "utf-8"
        s.write("분기 보고 ✓"); s.flush()
        assert b.getvalue().decode("utf-8") == "분기 보고 ✓"


def test_an_explicit_choice_and_other_systems_are_left_alone():
    _, s = _pipe()
    assert textout.fit([s], console_cp=949, environ={"PYTHONIOENCODING": "utf-8"}, windows=True) is None
    assert textout.fit([s], console_cp=949, environ={}, windows=False) is None
    assert s.encoding == "cp1252"


def test_a_console_itself_is_left_alone():
    class Tty(io.StringIO):
        def isatty(self):
            return True
    t = Tty()
    assert textout.fit([t], console_cp=949, environ={}, windows=True) is None


def test_the_mcp_channel_is_not_touched():
    src = (ROOT / "findr" / "cli.py").read_text(encoding="utf-8")
    assert '!= ["mcp"]' in src and "textout.fit()" in src


@pytest.mark.skipif(os.name != "nt", reason="Windows code pages")
def test_windows_piped_output_reads_back():
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    code = "from findr import textout; textout.fit(); print('분기 보고 ✓')"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    enc = textout.pipe_encoding()   # what this test's own console (if any) would decode with
    want = "분기 보고 ✓".encode(enc, "replace")
    assert r.stdout.strip() == want
