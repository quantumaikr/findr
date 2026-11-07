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
    assert asyncio.run(go()) == ["read_file", "search_files"]
