"""red-team pass on 0.2.24 (11/14): leaks closed after it. Every name, value and key here is invented"""
import importlib
import json
import os
import sqlite3
import sys

import pytest

import index
import relations


def _mcp(tmp_path, monkeypatch, deny):
    data = tmp_path / "data"; data.mkdir(exist_ok=True)
    monkeypatch.setenv("FINDR_HOME", str(data)); monkeypatch.setenv("FINDR_APP_PORT", "9")
    monkeypatch.setenv("FINDR_MCP_DENY", str(deny))
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    monkeypatch.setattr(m, "_engine_up", lambda: False)
    return m, data


def _index(data, files):
    """files: [(path, text, tier)] -> data/index.db with a word index as the indexer writes it"""
    con = sqlite3.connect(str(data / "index.db"))
    con.executescript("""CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    for p, text, tr in files:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        fid = con.execute("INSERT INTO files(path,name,ext,size,mtime,tier) VALUES(?,?,?,?,?,?)",
                          (p.as_posix(), p.name, p.suffix, len(text), 1.7e9, tr)).lastrowid
        cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, 0, text)).lastrowid
        con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, p.name, p.as_posix(), text))
    con.commit(); con.close()


# ---------------------------------------------------------------- 2. ordering oracles
def _relations_terms(tmp_path, monkeypatch, with_hidden):
    root = tmp_path / ("w" if with_hidden else "wo"); root.mkdir()
    deny = root / "denyme"; deny.mkdir()
    m, data = _mcp(root, monkeypatch, deny)
    files = [(root / "docs" / "rel.txt", "orbquill ledger harbor " * 3 + "vexmint plandor", 1)]
    files += [(root / "docs" / f"o{i}.txt", f"harbor memo {i}", 1) for i in range(4)]
    if with_hidden:   # a hidden file full of one of the shown file's words: it must not change their order
        files += [(deny / f"h{i}.txt", "orbquill " * 5, 1) for i in range(6)]
    _index(data, files)
    return m.file_relations((root / "docs" / "rel.txt").as_posix(), 10)["terms"]


def test_terms_order_does_not_depend_on_hidden_files(tmp_path, monkeypatch):
    relations._cache.clear()
    a = _relations_terms(tmp_path, monkeypatch, True)
    relations._cache.clear()
    b = _relations_terms(tmp_path, monkeypatch, False)
    assert a == b and a, (a, b)


def _index_order(tmp_path, monkeypatch, with_hidden):
    root = tmp_path / ("w" if with_hidden else "wo"); root.mkdir()
    deny = root / "denyme"; deny.mkdir()
    m, data = _mcp(root, monkeypatch, deny)
    files = [(root / "docs" / "a.txt", "plumtrevix status note", 1), (root / "docs" / "b.txt", "zorblaxquint zorblaxquint status note", 1)]
    if with_hidden:
        files += [(deny / f"z{i}.txt", "zorblaxquint " * 3, 1) for i in range(8)]
    _index(data, files)
    return [x["name"] for x in m.search_files("zorblaxquint plumtrevix", limit=5)["results"]]


def test_index_search_order_does_not_depend_on_hidden_files(tmp_path, monkeypatch):
    a = _index_order(tmp_path, monkeypatch, True)
    b = _index_order(tmp_path, monkeypatch, False)
    assert a == b == ["b.txt", "a.txt"], (a, b)


def test_secret_files_leave_the_word_index(tmp_path):
    data = tmp_path / "d"; data.mkdir()
    _index(data, [(tmp_path / "x" / "plain.txt", "glimmerfax", 1), (tmp_path / "x" / "creds.csv", "", 0)])
    con = sqlite3.connect(str(data / "index.db"))
    assert index.fts_no_secrets(con) == 1
    assert con.execute("SELECT count(*) FROM fts WHERE fts MATCH 'creds'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM fts WHERE fts MATCH 'glimmerfax'").fetchone()[0] == 1


# ---------------------------------------------------------------- 3, 4. credential exports by their header
@pytest.mark.parametrize("text", [
    "Saved sign-ins (exported 2026-10-02)\nurl,username,password\nhttps://plum.example,trent,Marm0t!77\n",
    "\n\nMy accounts\n\nsite;user;pw\nkelp.example;quill;B4dger\n",
    "사이트,아이디,PW\nhttps://kelp.example,quill,B4dger#55\n",
    "주소\t사용자\t비번\nkelp.example\tquill\tx\n",
    "도메인,이메일,암호\nkelp.example,q@example.invalid,x\n",
    "Domain|ID|Pwd\nkelp.example|quill|x\n",
    "계정,패스워드\nquill,x\n",
])
def test_credential_header_below_a_title_or_with_short_names(tmp_path, text):
    p = tmp_path / "list.csv"
    p.write_text(text, encoding="utf-8")
    assert index.cred_header(text) and index.is_secret(p) and index.secret_text(p.name, text)


def test_ordinary_tables_are_not_credentials(tmp_path):
    for text in ("name,phone,city\nkim,010,seoul\n", "Report\na\nb\nc\nd\ne\nurl,username,password\n", "pass rate,year\n0.8,2026\n"):
        assert not index.cred_header(text), text
