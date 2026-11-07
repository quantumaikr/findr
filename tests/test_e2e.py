"""the whole path a person goes through, on this runner's OS (#3, #6, #7, #9):
setup check -> search before meaning vectors -> index -> search -> the search page (sign-in, search, progress).
Models come from FINDR_MODELS (cached by the workflow)."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "findr" / "core"
pytestmark = pytest.mark.skipif(not os.environ.get("FINDR_E2E"), reason="set FINDR_E2E=1 (downloads models)")


def env(home):
    e = {**os.environ, "FINDR_HOME": str(home), "FINDR_DATA": str(home), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
         "FINDR_OCR": "0", "FINDR_PORTS": "8593,8595,8599"}
    return e


def findr(home, *args, timeout=1800):
    r = subprocess.run([sys.executable, "-m", "findr", *args], env=env(home), capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
    return r.returncode, r.stdout + r.stderr


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    docs = tmp_path_factory.mktemp("docs")
    (docs / "견적서_스마트센서.txt").write_text("스마트센서 게이트웨이 견적서. 수량 12대, 단가 520,000원, 납품 10월 말.", encoding="utf-8")
    (docs / "회의록_3분기.md").write_text("# 3분기 영업 전략 회의록\n참석: 김부장, 박과장. 결정: 온누리식품 냉장물류 재검토.", encoding="utf-8")
    with zipfile.ZipFile(docs / "휴가신청서.docx", "w") as z:
        z.writestr("word/document.xml", "<w:document><w:t>휴가 신청서 2025년 12월 24일부터 26일까지 가족 행사</w:t></w:document>")
    with zipfile.ZipFile(docs / "출장비_정산.xlsx", "w") as z:
        z.writestr("xl/sharedStrings.xml", "<sst><si><t>부산 출장 교통비 숙박비 정산</t></si></sst>")
    (docs / ".env").write_text("OPENAI_API_KEY=sk-test-should-never-be-indexed-000000", encoding="utf-8")
    rc, out = findr(home, "setup", "--lite", "--no-photo", "--no-llm")
    assert rc == 0, "setup failed:\n" + out[-3000:]
    return home, docs


def top(out):
    m = re.search(r"^\s*1\.\s+\S*\s+(.+?)\s+\(", out, re.M)
    return m.group(1) if m else ""


def test_doctor_loads_torch(world):   # #3: a broken compute library must be visible
    home, _ = world
    rc, out = findr(home, "doctor")
    assert "✓ 연산 라이브러리" in out, out


def test_search_before_meaning_vectors(world):   # #9: names and words answer while meaning is not computed yet
    home, docs = world
    r = subprocess.run([sys.executable, str(CORE / "index.py"), "--root", str(docs), "--db", str(home / "index.db"),
                        "--model", "e5-small", "--no-embed"], env=env(home), capture_output=True, text=True, cwd=str(CORE), timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    rc, out = findr(home, "ask", "스마트센서 견적서")
    assert rc == 0 and "Traceback" not in out, out[-2000:]
    assert "견적서_스마트센서" in top(out), out[-1500:]


def test_index_then_search(world):   # #6: progress is printed; the right file comes first
    home, docs = world
    rc, out = findr(home, "index", str(docs))
    assert rc == 0, out[-3000:]
    assert "FINDR_STAGE meaning" in out and "FINDR_STAGE done" in out, out[-2000:]
    for q, want in (("스마트센서 견적서", "견적서_스마트센서"), ("3분기 회의록", "회의록_3분기"), ("휴가 신청서", "휴가신청서"), ("출장비 정산", "출장비_정산")):
        rc, o = findr(home, "ask", q)
        assert want in top(o), f"{q} -> {top(o)!r}\n{o[-1200:]}"
    import sqlite3
    con = sqlite3.connect(str(home / "index.db"))
    assert not con.execute("SELECT count(*) FROM chunks WHERE text LIKE '%sk-test%'").fetchone()[0], "a secret reached the index"


def test_search_page(world):   # the app's page: sign-in, search, progress (#7: reranking runs without a device error)
    home, _ = world
    p = subprocess.Popen([sys.executable, "-m", "findr", "serve", "--app", "--no-browser", "--no-photo", "--no-llm", "--port", "8596"],
                         env=env(home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(ROOT))
    try:
        t = time.time(); ready = False
        while time.time() - t < 600:
            line = p.stdout.readline()
            if "FINDR_APP_READY" in line:
                ready = True; break
            if not line and p.poll() is not None:
                break
        assert ready, "engine did not start"
        key = (home / ".password").read_text().strip()
        base = "http://127.0.0.1:8596"
        try:
            urllib.request.urlopen(base + "/", timeout=30); assert False, "page open without sign-in"
        except urllib.error.HTTPError as e:
            assert e.code == 401
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        assert op.open(f"{base}/auth?t={key}", timeout=30).status == 200
        r = json.loads(op.open(base + "/api?" + urllib.parse.urlencode({"q": "스마트센서 견적서"}), timeout=300).read())
        assert r["results"] and "견적서_스마트센서" in r["results"][0]["name"], r.get("results", [])[:2]
        assert isinstance(json.loads(op.open(base + "/progress", timeout=30).read()), dict)
    finally:
        p.terminate()
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
