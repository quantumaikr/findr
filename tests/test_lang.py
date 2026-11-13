"""the command line speaks Korean or English (findr/lang.py): FINDR_LANG first, then the system's language, English for
everyone else, and Korean (what it always printed) when the language cannot be read at all"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from findr import lang as fl

ROOT = Path(__file__).resolve().parent.parent
VARS = ("FINDR_LANG", "LC_ALL", "LC_MESSAGES", "LANG")


@pytest.fixture
def bare(monkeypatch):
    """no language said anywhere, on a PC that is not Windows"""
    for k in VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fl, "_NT", False)
    return monkeypatch


def test_findr_lang_wins(bare):
    bare.setenv("LANG", "ko_KR.UTF-8")
    for v, want in (("en", "en"), ("EN", "en"), ("en_US", "en"), ("ko", "ko"), ("ko-KR", "ko"), (" Ko ", "ko")):
        bare.setenv("FINDR_LANG", v)
        assert fl.lang() == want, v
    bare.setenv("LANG", "en_US.UTF-8"); bare.setenv("FINDR_LANG", "ko")
    assert fl.lang() == "ko" and fl.L("없음", "none") == "없음"


def test_unknown_findr_lang_falls_to_the_system(bare):
    for v in ("", "fr", "auto"):
        bare.setenv("FINDR_LANG", v)
        bare.setenv("LANG", "ko_KR.UTF-8"); assert fl.lang() == "ko", v
        bare.setenv("LANG", "de_DE.UTF-8"); assert fl.lang() == "en", v


def test_system_locale_in_order(bare):
    assert fl.lang() == "en" and fl.L("없음", "none") == "none"          # nothing said: English
    bare.setenv("LANG", "ko_KR.UTF-8"); assert fl.lang() == "ko"
    bare.setenv("LC_MESSAGES", "en_US.UTF-8"); assert fl.lang() == "en"    # LC_MESSAGES before LANG
    bare.setenv("LC_ALL", "ko_KR.eucKR"); assert fl.lang() == "ko"         # LC_ALL before both
    bare.setenv("LC_ALL", ""); assert fl.lang() == "en"                    # an empty one is not set
    for c in ("C", "C.UTF-8", "POSIX"):                                    # 'C' says nothing: the next one is read
        bare.setenv("LC_ALL", c); bare.setenv("LC_MESSAGES", ""); bare.setenv("LANG", "ko_KR.UTF-8")
        assert fl.lang() == "ko", c
        bare.setenv("LANG", c); assert fl.lang() == "en", c


def test_windows_display_language(bare):
    bare.setattr(fl, "_NT", True)
    bare.setattr(fl, "_windows_korean", lambda: True); assert fl.lang() == "ko"
    bare.setattr(fl, "_windows_korean", lambda: False); assert fl.lang() == "en"
    bare.setenv("LANG", "en_US.UTF-8"); bare.setattr(fl, "_windows_korean", lambda: True)
    assert fl.lang() == "en"                                               # a shell on Windows that sets LANG is believed
    bare.setenv("FINDR_LANG", "ko"); assert fl.lang() == "ko"


def test_korean_when_the_language_cannot_be_read(bare):
    def boom():
        raise OSError("no such call")
    bare.setattr(fl, "_NT", True); bare.setattr(fl, "_windows_korean", boom)
    assert fl.lang() == "ko" and fl.L("없음", "none") == "없음"


def test_L_reads_the_language_each_time(bare):
    bare.setenv("FINDR_LANG", "en"); assert fl.L("색인", "index") == "index"
    bare.setenv("FINDR_LANG", "ko"); assert fl.L("색인", "index") == "색인"


def test_the_tests_run_in_korean():
    assert os.environ.get("FINDR_LANG") == "ko"   # conftest.py: the tests that look for a sentence look for the Korean one


def _run(args, lg, home):
    env = {**os.environ, "FINDR_LANG": lg, "FINDR_HOME": str(home), "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, "-m", "findr", *args], capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
                          stdin=subprocess.DEVNULL)


def _hangul(s):
    return [ln for ln in s.splitlines() if any("가" <= ch <= "힣" for ch in ln)]


def test_help_in_both_languages(tmp_path):
    ko = _run(["--help"], "ko", tmp_path / "h").stdout
    en = _run(["--help"], "en", tmp_path / "h").stdout
    assert "기억나는 대로 물으면, 내 PC가 찾아 줍니다." in ko and "설치 상태 점검" in ko
    assert "check the installation" in en and not _hangul(en)
    for cmd in ("setup", "index", "serve", "ask", "doctor", "uninstall", "upgrade", "roots", "mcp"):
        assert not _hangul(_run([cmd, "--help"], "en", tmp_path / "h").stdout), cmd


def test_uninstall_without_data_in_both_languages(tmp_path):
    home = tmp_path / "none"
    ko = _run(["uninstall"], "ko", home); en = _run(["uninstall"], "en", home)
    assert ko.returncode == 0 and ko.stdout.strip() == f"지울 데이터가 없습니다: {home}"
    assert en.returncode == 0 and en.stdout.strip() == f"No data to remove: {home}"


def test_reasons_and_folder_names_in_english(bare):
    from findr import cli
    whys = ["꺼짐", "작은 모델을 직접 고름 (setup --lite)", "색인 없음", "이미 큰 모델", "해당 없음", "메모리 4GB (8GB 이상에서만)", "디스크 여유 3GB 미만",
            "이 기기에서 큰 모델이 느림 (초당 9.1조각)", "이 기기에서 큰 모델이 느림", "이 PC에서 312시간 걸림: 작은 모델 유지"]
    labels = ["바탕화면", "다운로드", "문서", "클라우드", "사진", "동영상", "음악", "홈 폴더 기타", "D: 기타 폴더"]
    bare.setenv("FINDR_LANG", "ko")
    assert [cli._why_say(w) for w in whys] == whys and [cli._label_say(x) for x in labels] == labels   # Korean: as they are
    bare.setenv("FINDR_LANG", "en")
    assert not _hangul("\n".join(cli._why_say(w) for w in whys)) and not _hangul("\n".join(cli._label_say(x) for x in labels))
    assert cli._why_say("메모리 4GB (8GB 이상에서만)").startswith("4GB") and "9.1" in cli._why_say(whys[7]) and "312" in cli._why_say(whys[9])
    assert cli._label_say("바탕화면") == "Desktop" and cli._label_say("D: 기타 폴더") == "D: other folders"
    assert cli._why_say("처음 보는 이유") == "처음 보는 이유"   # an unknown reason is shown as it is, not lost


def test_install_scripts_say_both():
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    ps = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert 'case "${FINDR_LANG:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}" in ko*' in sh and "$env:FINDR_LANG" in ps and "Get-UICulture" in ps
    for src, show in ((sh, "echo"), (ps, "Write-Host")):   # every Korean sentence is said through say, with its English twin
        code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
        assert not [ln for ln in _hangul("\n".join(code)) if "say " not in ln.lower()]
        assert not re.search(show + r' "[^"]*[가-힣]', "\n".join(code))


def test_doctor_counts_pieces_left_for_the_deep_run(tmp_path):
    """#66-10: doctor says how much a deep run would still do, and when it helps"""
    import sqlite3
    db = tmp_path / "index.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, tier INT, pending INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);")
    con.executemany("INSERT INTO files VALUES(?,?,?,'.txt',100,1,0)", [(1, "/d/a.txt", "a.txt"), (2, "/d/b.txt", "b.txt")])
    con.executemany("INSERT INTO chunks(file, n, text, emb) VALUES(?,?,?,?)",
                    [(1, i, "x", b"v" if i < 8 else None) for i in range(12)] + [(2, 0, "y", None)])   # b: not embedded yet, not deferred
    con.commit(); con.close()
    env = {**os.environ, "FINDR_LANG": "ko", "PYTHONUTF8": "1", "FINDR_HOME": str(tmp_path / "home")}
    out = subprocess.run([sys.executable, "-m", "findr", "--db", str(db), "doctor"], env=env, capture_output=True, text=True,
                         encoding="utf-8", cwd=str(Path(__file__).resolve().parents[1]), timeout=300).stdout
    assert "긴 문서 1개의 뒷부분 약 4조각" in out and "findr index --deep" in out, out[-800:]
