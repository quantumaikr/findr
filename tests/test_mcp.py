"""findr for AI tools: search without the engine (index only), and read_file limited to indexed files"""
import importlib
import sqlite3
import sys

import pytest


@pytest.fixture
def mcp_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_HOME", str(tmp_path)); monkeypatch.setenv("FINDR_APP_PORT", "9")   # no engine on port 9
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    con = sqlite3.connect(str(tmp_path / "index.db"))
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);
        CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    con.execute("INSERT INTO files VALUES(1,'/d/견적서_스마트센서.txt','견적서_스마트센서.txt','.txt',1,1700000000,1)")
    con.execute("INSERT INTO chunks VALUES(1,1,0,'스마트센서 게이트웨이 견적서 단가 520,000원')")
    con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(1,'견적서_스마트센서.txt','/d/견적서_스마트센서.txt','스마트센서 게이트웨이 견적서 단가 520,000원')")
    con.commit(); con.close()
    return m


def test_search_without_engine(mcp_mod):
    r = mcp_mod.search_files("견적서 스마트센서")
    assert r["engine"] == "index" and r["results"][0]["name"] == "견적서_스마트센서.txt" and "not running" in r["note"]
    assert mcp_mod.search_files("게이트웨이")["results"][0]["path"] == "/d/견적서_스마트센서.txt"   # a word inside the text


def test_read_only_indexed_files(mcp_mod, tmp_path):
    r = mcp_mod.read_file("/d/견적서_스마트센서.txt", 30)
    assert r["text"].startswith("스마트센서") and r["truncated"] is False
    assert "error" in mcp_mod.read_file(str(tmp_path / "index.db"))   # any other path is refused


def test_read_file_says_why_and_pages(mcp_mod, tmp_path):
    import zipfile
    docs = tmp_path.parent / (tmp_path.name + "_docs")   # outside the data folder, which the tools never answer from (#200)
    docs.mkdir()
    old, doc = docs / "old.hwp", docs / "a.docx"
    old.write_bytes(b"HWP Document File V3.00" + b"\0" * 100)
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml", "<w:document xmlns:w='x'><w:t>본문</w:t></w:document>")
        z.writestr("docProps/app.xml", "<Properties><Pages>9</Pages></Properties>")
    con = sqlite3.connect(str(tmp_path / "index.db"))
    con.execute("INSERT INTO files VALUES(2,?,'old.hwp','.hwp',1,1700000000,1)", (str(old),))
    con.execute("INSERT INTO files VALUES(3,?,'a.docx','.docx',1,1700000000,1)", (str(doc),))
    con.execute("INSERT INTO chunks VALUES(2,3,0,'본문')")
    con.commit(); con.close()
    r = mcp_mod.read_file(str(old))
    assert r["text"] == "" and "HWP 3.x" in r["unreadable"] and "HWP 3.x" in r["note"]
    r = mcp_mod.read_file(str(doc))
    assert r["text"] == "본문" and r["pages"] == 9 and r["pages_estimated"] is False


def test_mcp_server_lists_tools(tmp_path):
    pytest.importorskip("mcp")
    import asyncio, os
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "findr", "mcp"], env={**os.environ, "FINDR_HOME": str(tmp_path)})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return sorted(t.name for t in (await s.list_tools()).tools)
    assert asyncio.run(go()) == ["file_relations", "list_folder", "read_file", "search_files", "versions"]


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """a small PC: a project folder with versions and a copy elsewhere, photos with no text, a spreadsheet"""
    monkeypatch.setenv("FINDR_HOME", str(tmp_path)); monkeypatch.setenv("FINDR_APP_PORT", "9")
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    con = sqlite3.connect(str(tmp_path / "index.db"))
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);
        CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    rows = [(1, "/d/work/plan/plan_v1.docx", ".docx", 100, 1700000000, "first plan"),
            (2, "/d/work/plan/plan_v2.docx", ".docx", 120, 1710000000, "second plan"),
            (3, "/d/work/plan/plan_final.docx", ".docx", 130, 1720000000, "the final plan"),
            (4, "/d/old/plan_final.docx", ".docx", 130, 1720000500, "the final plan"),        # the same file, copied
            (5, "/d/work/trip/day1/IMG_0001.jpg", ".jpg", 999, 1715000000, None),
            (6, "/d/work/trip/day1/IMG_0002.jpg", ".jpg", 998, 1715000100, None),
            (7, "/d/work/budget plan.xlsx", ".xlsx", 50, 1716000000, "budget plan numbers")]
    for i, p, e, s, mt, t in rows:
        con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,1)", (i, p, p.rsplit("/", 1)[1], e, s, mt))
        if t:
            con.execute("INSERT INTO chunks VALUES(?,?,0,?)", (i, i, t))
            con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (i, p.rsplit("/", 1)[1], p, t))
    con.commit(); con.close()
    return m


def test_kind_filter_keeps_only_that_format(pc):
    r = pc.search_files("plan", kind="sheet")
    assert [x["name"] for x in r["results"]] == ["budget plan.xlsx"] and r["filters"] == {"kind": "sheet"}
    assert all(x["name"].endswith(".docx") for x in pc.search_files("plan", kind="word")["results"])
    assert pc.search_files("plan", kind="pdf")["results"] == [] and "list_folder" in pc.search_files("plan", kind="pdf")["note"]


def test_folder_and_period_filters(pc):
    r = pc.search_files("plan", folder="/d/work/plan", since="2024-01-01")
    assert {x["name"] for x in r["results"]} == {"plan_v2.docx", "plan_final.docx"}   # v1 is from 2023, the copy is in /d/old


def test_list_folder_shows_photos_without_text_and_subfolders(pc):
    r = pc.list_folder("/d/work/trip/day1")
    assert [x["name"] for x in r["files"]] == ["IMG_0002.jpg", "IMG_0001.jpg"]   # newest first
    top = pc.list_folder("/d/work")
    assert {d["name"]: d["files"] for d in top["folders"]} == {"plan": 3, "trip": 2} and [x["name"] for x in top["files"]] == ["budget plan.xlsx"]
    assert pc.list_folder("/d/work/trip/day1/IMG_0001.jpg")["folder"] == "/d/work/trip/day1"   # a file: its folder


def test_versions_finds_copies_versions_and_namesakes(pc):
    r = pc.versions("/d/work/plan/plan_final.docx")
    rel = {x["path"]: x["relation"] for x in r["others"]}
    assert rel["/d/old/plan_final.docx"] == "copy"
    assert rel["/d/work/plan/plan_v2.docx"] == "version" and rel["/d/work/plan/plan_v1.docx"] == "version"
    assert [x["relation"] for x in r["others"]][0] == "copy"
    assert "error" in pc.versions("/nowhere.docx")



@pytest.fixture
def lectures(tmp_path, monkeypatch):
    """a course given twice (sibling folders), a contest folder with two entries saved the same day, and unrelated files"""
    monkeypatch.setenv("FINDR_HOME", str(tmp_path)); monkeypatch.setenv("FINDR_APP_PORT", "9")
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    con = sqlite3.connect(str(tmp_path / "index.db"))
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);""")
    day = 1700049600   # noon UTC (2023-11-15): +1 h and +2 h stay on the same date in every time zone the tests run in (CI is UTC)
    later = day + 40 * 86400
    rows = [(1, "/d/강의/1차 과정/08. 강화학습.key", ".key", day, "강화학습 에이전트 보상 탐험률 할인률"),
            (2, "/d/강의/2차 과정/17. 실용강의 - 강화학습.key", ".key", later, "강화학습 에이전트 보상 탐험률"),     # the course again
            (3, "/d/강의/2차 과정/03. 자연어 전처리.key", ".key", later, "토큰화 형태소 분석"),                 # another subject
            (4, "/d/강의/1차 과정/09. 정책 경사.key", ".key", day, "에이전트 보상 정책 경사 강화학습 기초"),   # same opening ideas
            (10, "/d/대회/2023 팀가_발표.pptx", ".pptx", day + 3600, "배경 및 목적 제작계획"),
            (11, "/d/대회/2023 팀나_최종 제출.pptx", ".pptx", day + 7200, "다른 팀의 발표 내용"),             # same day, other name
            (12, "/d/대회/참가 신청서.hwp", ".hwp", day + 7200, "신청서 양식"),                               # same day, not slides
            (13, "/d/대회/2023 팀다_발표.pptx", ".pptx", later, "다음 달 발표")]                              # another day
    for i, p, e, mt, t in rows:
        con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,1)", (i, p, p.rsplit("/", 1)[1], e, 100 + i, mt))
        con.execute("INSERT INTO chunks VALUES(?,?,0,?)", (i, i, t))
    con.commit(); con.close()
    return m


def test_versions_similar_finds_the_course_given_again(lectures):
    r = lectures.versions("/d/강의/1차 과정/08. 강화학습.key")
    sim = {x["name"]: x for x in r["others"] if x["relation"] == "similar"}
    assert "17. 실용강의 - 강화학습.key" in sim and sim["17. 실용강의 - 강화학습.key"]["folder"] == "이웃 폴더"
    assert sim["17. 실용강의 - 강화학습.key"]["why"].startswith("비슷한 이름") and sim["17. 실용강의 - 강화학습.key"]["score"] >= 0.6
    assert "03. 자연어 전처리.key" not in sim   # another subject in the sibling folder


def test_versions_similar_finds_another_entry_saved_the_same_day(lectures):
    r = lectures.versions("/d/대회/2023 팀가_발표.pptx")
    sim = {x["name"]: x["why"] for x in r["others"] if x["relation"] == "similar"}
    assert sim.get("2023 팀나_최종 제출.pptx") == "같은 날 같은 폴더"
    assert "참가 신청서.hwp" not in sim   # same day, but not slides
    assert "2023 팀다_발표.pptx" not in sim   # another team, another day: only the year and '발표' in common


def test_versions_similar_is_capped_and_skips_crowded_parents(lectures, monkeypatch, tmp_path):
    import sqlite3 as sq
    con = sq.connect(str(tmp_path / "index.db"))
    for i in range(40):   # forty more runs of the course in sibling folders
        p = f"/d/강의/{i + 3}차 과정/08. 강화학습.key"
        con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,1)", (100 + i, p, "08. 강화학습 복습.key", ".key", 1, 1700000000 + i))
    con.commit(); con.close()
    sim = [x for x in lectures.versions("/d/강의/1차 과정/08. 강화학습.key")["others"] if x["relation"] == "similar"]
    assert len(sim) == lectures.SIMILAR_MAX
    monkeypatch.setattr(lectures, "SIBLING_MAX", 5)   # a crowded parent (the home or Downloads folder): the same folder only
    sim = [x for x in lectures.versions("/d/강의/1차 과정/08. 강화학습.key")["others"] if x["relation"] == "similar"]
    assert sim and all(x["folder"] == "같은 폴더" for x in sim)


def _docs(tmp_path):
    d = tmp_path.parent / (tmp_path.name + "_docs")   # outside the data folder
    d.mkdir(exist_ok=True)
    return d


def _add(tmp_path, fid, p, tier):
    con = sqlite3.connect(str(tmp_path / "index.db"))
    con.execute("INSERT INTO files VALUES(?,?,?,?,1,1700000000,?)", (fid, str(p), p.name, p.suffix, tier))
    con.execute("INSERT INTO chunks VALUES(?,?,0,'')", (fid, fid))
    con.commit(); con.close()


def test_read_file_reads_now_only_what_the_indexer_reads_and_masks_keys(mcp_mod, tmp_path, monkeypatch):
    """review of #204: a name-only file (a VPN profile with a private key) was read as raw bytes, and keys came back unmasked"""
    d = _docs(tmp_path)
    vpn, note, cloud = d / "office.ovpn", d / "notes.txt", d / "online.txt"
    vpn.write_text("client\n<key>\n-----BEGIN PRIVATE KEY-----\nNOTAREALKEY\n-----END PRIVATE KEY-----\n</key>\n")
    note.write_text("the access key sk-ant-" + "x" * 30 + " is here")
    cloud.write_text("cloud placeholder text")
    _add(tmp_path, 10, vpn, 3); _add(tmp_path, 11, note, 1); _add(tmp_path, 12, cloud, 1)
    r = mcp_mod.read_file(str(vpn))
    assert r["text"] == "" and "NOTAREALKEY" not in str(r)
    r = mcp_mod.read_file(str(note))
    assert r["text"] == "the access key [가림] is here"
    from findr.core import index
    monkeypatch.setattr(index, "cloud_only_path", lambda s: s == str(cloud))
    assert mcp_mod.read_file(str(cloud))["text"] == ""   # an online-only file is not downloaded
    from findr.core import deepread   # the reader itself refuses them too
    assert deepread.deep_read(str(vpn))["text"] == "" and "[가림]" in deepread.deep_read(str(note))["text"]


def test_read_file_ocr_runs_in_a_child_and_is_cut_off(mcp_mod, tmp_path, monkeypatch):
    """review of #204: OCR in the MCP server kept EasyOCR / torch for good and held the assistant for minutes. It runs in a
    process of its own, stopped after FINDR_MCP_DEEP_READ_SECONDS (+ the page in progress)"""
    import time
    d = _docs(tmp_path)
    scan, photo = d / "scan.pdf", d / "receipt.png"
    scan.write_bytes(b"%PDF-1.4\n%%EOF\n"); photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    _add(tmp_path, 20, scan, 1); _add(tmp_path, 21, photo, 3)   # a photo is name-only in the index, but OCR may read it
    seen = []

    def fake(path, max_chars, seconds):
        seen.append((path, seconds))
        return [sys.executable, "-c", "import json; print('noise'); print(json.dumps({'text': 'scan text sk-ant-" + "y" * 30 + "', 'ocr': {'pages_read': 1, 'pages_total': 1}}))"]
    monkeypatch.setattr(mcp_mod, "_deep_cmd", fake)
    r = mcp_mod.read_file(str(scan))
    assert r["text"] == "scan text [가림]" and r["ocr"]["pages_read"] == 1 and seen[-1] == (str(scan), 30.0)
    assert mcp_mod.read_file(str(photo))["text"].startswith("scan text") and seen[-1][0] == str(photo)
    monkeypatch.setenv("FINDR_MCP_DEEP_READ_SECONDS", "1")
    monkeypatch.setattr(mcp_mod, "DEEP_GRACE", 1)
    monkeypatch.setattr(mcp_mod, "_deep_cmd", lambda p, m, s: [sys.executable, "-c", "import time; time.sleep(60)"])
    t = time.time()
    r = mcp_mod.read_file(str(scan))
    assert time.time() - t < 15 and r["text"] == "" and "stopped" in r["note"]


def test_deep_read_child_entry_prints_one_json_line(tmp_path):
    import json, subprocess
    from pathlib import Path
    p = tmp_path / "a.txt"; p.write_text("hello child")
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-m", "findr.core.deepread", str(p), "100", "5"], capture_output=True, text=True, cwd=root, timeout=120)
    got = json.loads([x for x in out.stdout.splitlines() if x.startswith("{")][-1])
    assert got["text"] == "hello child"



def test_excerpt_place_maps_into_read_file_text(mcp_mod, tmp_path):
    """review: excerpt_start counts inside one stored piece; excerpt_offset is the same place in read_file's text"""
    con = sqlite3.connect(str(tmp_path / "index.db"))
    con.execute("INSERT INTO files VALUES(30,'/d/long.txt','long.txt','.txt',1,1700000000,1)")
    pieces = ["first piece of text", "", "second piece here", "third piece with the target word"]
    for n, t in enumerate(pieces):
        con.execute("INSERT INTO chunks VALUES(?,30,?,?)", (300 + n, n, t))
    con.commit(); con.close()
    ro = mcp_mod._db()
    start = pieces[3].index("target")
    got = mcp_mod._excerpt_place(ro, 303, start)
    text = mcp_mod.read_file("/d/long.txt", 20000)["text"]
    assert got["excerpt_chunk"] == 3 and text[got["excerpt_offset"]:].startswith("target")
    assert mcp_mod._excerpt_place(ro, None, 0) == {}
