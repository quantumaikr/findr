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
    assert asyncio.run(go()) == ["list_folder", "read_file", "search_files", "versions"]


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
