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


# ---------------------------------------------------------------- 5, 9. mail attachments and nested mail
import base64  # noqa: E402

import moretext  # noqa: E402

_HEAD = ("From: Tess Varno <tess@example.invalid>\r\nTo: me@example.invalid\r\nSubject: files\r\nMIME-Version: 1.0\r\n"
         "Content-Type: multipart/mixed; boundary=BB\r\n\r\n--BB\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nsee attached\r\n")


def _att(fn, data, ctype="application/octet-stream"):
    return (f"--BB\r\nContent-Type: {ctype}\r\nContent-Disposition: attachment; filename=\"{fn}\"\r\n"
            f"Content-Transfer-Encoding: base64\r\n\r\n{base64.b64encode(data.encode()).decode()}\r\n")


def _mail(tmp_path, body):
    p = tmp_path / "m.eml"
    p.write_bytes(body.encode())
    return moretext.eml_text(p)


@pytest.mark.parametrize("fn", ["C:\\\\Users\\\\kim\\\\secrets.json", "D:\\\\keys\\\\.env.txt", "../x/.env", "..\\\\cfg\\\\credentials.csv"])
def test_attachment_named_with_a_folder_is_judged_by_its_last_part(tmp_path, fn):
    t = _mail(tmp_path, _HEAD + _att(fn, "DB_PASS=Quokka4471") + "--BB--\r\n")
    assert "Quokka4471" not in t and "첨부:" in t
    assert index.secret_name(index.Path(fn.replace("\\\\", "\\")))


def test_top_level_message_rfc822_is_a_nested_mail(tmp_path):
    inner = ("From: a@example.invalid\r\nSubject: inner\r\nMIME-Version: 1.0\r\nContent-Type: text/plain\r\n\r\ninner body Quokka80\r\n")
    t = _mail(tmp_path, "From: b@example.invalid\r\nSubject: wrapper\r\nMIME-Version: 1.0\r\nContent-Type: message/rfc822\r\n\r\n" + inner)
    assert "Quokka80" not in t and "wrapper" in t


def test_mail_attached_as_text_is_a_nested_mail(tmp_path):
    inner = "From: a@example.invalid\r\nSubject: inner\r\nContent-Type: text/plain\r\n\r\ninner attach Quokka81\r\n"
    t = _mail(tmp_path, _HEAD + _att("fwd2.txt", inner, "text/plain") + _att("plain.txt", "Subject line: not a mail\nbody Wombat9") + "--BB--\r\n")
    assert "Quokka81" not in t and "fwd2.txt" in t and "Wombat9" in t
    # an index from before: the stored attachment block is cut to its name when returned
    assert "Quokka81" not in index.mask("x\n\n[첨부 fwd2.txt]\n" + inner.replace("\r\n", "\n"))


# ---------------------------------------------------------------- 6. password-manager exports by content
PM = {
    "vault.json": '{"encrypted": false, "items": [{"type": 1, "name": "Plum", "login": {"username": "ogil", "password": "Tulip-7x", "uris": [{"uri": "https://plum.example"}]}}]}',
    "dump.xml": "<KeePassFile><Root><Group><Entry><String><Key>Password</Key><Value>Lynx*66</Value></String></Entry></Group></Root></KeePassFile>",
    "sites.json": '{"nextId": 3, "logins": [{"hostname": "https://kelp.example", "encryptedUsername": "MDIE", "encryptedPassword": "MDoE"}]}',
    "export.1pif": '{"title":"Kelp","secureContents":{"fields":[{"value":"quill","name":"username"},{"value":"x","designation":"password"}]}}',
    "backup.txt": "<entries><entry><url>https://kelp.example</url><username>q</username><password>x</password></entry></entries>",
}


@pytest.mark.parametrize("name", sorted(PM))
def test_password_manager_exports_are_secret_by_content(tmp_path, name):
    p = tmp_path / name
    p.write_text(PM[name], encoding="utf-8")
    assert index.secret_text(name, PM[name]) and index.is_secret(p, sniff=True)
    assert index.is_secret(index.Path(name), head=PM[name].encode())   # a mail attachment


def test_ordinary_json_and_xml_are_not_password_exports():
    for name, t in (("pkg.json", '{"name": "x", "version": "1.0", "author": {"email": "a@example.invalid"}}'),
                    ("feed.xml", "<rss><channel><title>t</title><link>https://example.invalid</link></channel></rss>"),
                    ("form.json", '{"fields": ["user", "password"]}')):
        assert not index.secret_text(name, t), name
