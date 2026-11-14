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


# ---------------------------------------------------------------- 6, 7. masking
P1 = "Gor" + "illa88zzQx"
MASKED = [
    "은행 비밀번호: " + P1, "공유기 암호 = " + P1, "패스워드：" + P1, "비번=" + P1,
    "password: correct horse " + P1, '{"password": "x' + P1 + '"}', '{"pwd": "' + P1[:3] + '"}', "token: " + P1[:2],
    "[db]\npasswd = " + P1, "client_secret: '" + P1 + "'", "<password>" + P1 + "</password>", "<api_key>" + P1 + "</api_key>",
    "<Key>Password</Key><Value>" + P1 + "</Value>",
    "DefaultEndpointsProtocol=https;AccountName=fakeacct;AccountKey=" + P1 + "==;EndpointSuffix=core.windows.net",
    "SharedAccessSignature=sv=2020&" + P1, "https://acct.blob.example/c?sv=2020-01-01&sig=" + P1 + "%3D&se=1",
    "npm" + "_" + "Q9wErTyUiOpAsDfGhJkL" + P1 + "1234567", "S" + "G." + "abcdefghijklmnopqrstuv." + P1 + "ABCDEFGHIJKLMN",
    "GOC" + "SPX-" + P1 + "AbCdEfGhIjKl", "ya" + "29." + P1 + "a0AfH6SMBexampleexample",
    "https://hooks.slack.com/services/T0000/B0000/" + P1, "postgres://admin:p@ss/" + P1 + "@db.example:5432/app",
    "-----BEGIN RSA PRIVATE\nKEY-----\nMIIEp" + P1 + "\n", "-----begin rsa private key-----\nMIIEp" + P1 + "\n-----end rsa private key-----",
    "-----BEGIN  RSA PRIVATE KEY-----\nMIIEp" + P1 + "\n-----END RSA PRIVATE KEY-----",
]


@pytest.mark.parametrize("leak", MASKED)
def test_masked_whatever_its_form(leak):
    out = index.mask("앞 글\n" + leak + "\n뒤 글")
    assert P1 not in out and "[가림]" in out and out.endswith("뒤 글"), out


def test_base64_pem_and_twilio():
    pem = "-----BEGIN RSA PRIVATE KEY-----\n" + "MIIEpAIBAAKCAQEAzQx7examplekeymaterial\n" * 4 + "-----END RSA PRIVATE KEY-----\n"
    b64 = base64.b64encode(pem.encode()).decode()
    out = index.mask("backup:\n" + b64[:76] + "\n" + b64[76:] + "\nafter")
    assert "LS0tLS1CRUdJTi" not in out and b64[80:120] not in out and out.endswith("after"), out
    hexid = "0123456789abcdef" * 2
    out = index.mask("twilio SK" + hexid + " auth " + hexid[::-1])
    assert hexid not in out and hexid[::-1] not in out, out
    assert index.mask("commit " + hexid) == "commit " + hexid   # a bare hash is not a token


def test_labels_keep_prose_and_code():
    for t in ("The password policy requires 12 characters.", "if password == expected:", "암호화 방식: AES", "max_tokens = 4096",
              "sig figures", "author: kim " + "0123456789abcdef" * 2):
        assert index.mask(t) == t, t


def test_mask_stays_fast_on_large_text():
    import time
    big = ("평범한 회의 메모 password policy token bucket auth flow sig figures https://example.com/a@b " * 20000)
    t0 = time.time(); index.mask(big)
    assert time.time() - t0 < 10


# ---------------------------------------------------------------- 8. near-miss secret names
@pytest.mark.parametrize("name", [".env​", "​.env", ".env﻿", ".e‍nv", ".env⁠", ".env　", ".env ",
                                  "secrets​.json", "id_rsa.txt", "id_ed25519-old", "id_ecdsa_backup", "id_dsa (1)", "id_rsa.pub"])
def test_invisible_characters_and_key_name_suffixes(name):
    assert index.secret_name(index.Path(name)), name


def test_ordinary_names_stay_readable():
    for name in ("envelope.txt", "identity.md", "id_card.pdf", "rsa_notes.md", "my env notes.txt"):
        assert not index.secret_name(index.Path(name)), name


# ---------------------------------------------------------------- 10. hard links
def _link_or_skip(src, dst):
    try:
        os.link(src, dst)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"no hard links here ({type(e).__name__})")


def test_hard_link_to_a_data_folder_file_is_not_indexed(tmp_path):
    import subprocess
    from pathlib import Path
    home, root = tmp_path / "fhome", tmp_path / "root"
    (home / "notes").mkdir(parents=True); root.mkdir()
    (home / ".password").write_text("synthpw-Quokka91\n", encoding="utf-8")
    (home / "notes" / "memo.txt").write_text("hidden memo Quokka90\n", encoding="utf-8")
    (root / "plain.txt").write_text("plain memo\n", encoding="utf-8")
    _link_or_skip(home / ".password", root / "pw_hard.txt")
    _link_or_skip(home / "notes" / "memo.txt", root / "memo_hard.txt")
    db = tmp_path / "i.db"
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home)}
    r = subprocess.run([sys.executable, str(Path(index.__file__)), "--root", str(root), "--db", str(db), "--no-embed"],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    con = sqlite3.connect(db)
    names = {n for (n,) in con.execute("SELECT name FROM files")}
    assert names == {"plain.txt"}, names
    assert not any("Quokka" in (t or "") for (t,) in con.execute("SELECT text FROM chunks"))


def test_mcp_hides_a_hard_link_to_a_denied_file(tmp_path, monkeypatch):
    deny = tmp_path / "denyme"; deny.mkdir()
    m, data = _mcp(tmp_path, monkeypatch, deny)
    docs = tmp_path / "docs"
    _index(data, [(deny / "plan.txt", "quarry plan Quokka92", 1), (docs / "other.txt", "quarry note", 1)])
    _link_or_skip(deny / "plan.txt", docs / "plan_hard.txt")
    con = sqlite3.connect(str(data / "index.db"))
    fid = con.execute("INSERT INTO files(path,name,ext,size,mtime,tier) VALUES(?,?,?,?,?,1)",
                      ((docs / "plan_hard.txt").as_posix(), "plan_hard.txt", ".txt", 20, 1.7e9)).lastrowid
    cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,0,'quarry plan Quokka92')", (fid,)).lastrowid
    con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, "plan_hard.txt", "x", "quarry plan Quokka92"))
    con.commit(); con.close()
    p = (docs / "plan_hard.txt").as_posix()
    assert "error" in m.read_file(p) and "error" in m.file_relations(p) and "file" not in m.versions(p)
    assert [x["name"] for x in m.search_files("quarry")["results"]] == ["other.txt"]
    assert "plan_hard.txt" not in json.dumps(m.list_folder(docs.as_posix()))
    _link_or_skip(docs / "other.txt", tmp_path / "other_copy.txt")   # a hard link between two shown files: shown
    assert "text" in m.read_file((docs / "other.txt").as_posix())
    monkeypatch.setattr(m, "HARDLINK_SCAN", 0)   # too many files to look through: every hard-linked file is left out
    assert "error" in m.read_file((docs / "other.txt").as_posix())


# ---------------------------------------------------------------- 11. FINDR_MCP_DENY entries that look wrong
def test_deny_entries_warn_once_and_fail_safe(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    deny = tmp_path / "deny me"; deny.mkdir()
    m, data = _mcp(tmp_path, monkeypatch, "relative/folder")
    capsys.readouterr()
    raw = ";".join(["relative/folder", Path(deny).as_uri(), str(tmp_path / "deny%20me"), str(tmp_path / "not-there-yet")])
    got = m._deny_entries(raw); m._deny_entries(raw)
    err = capsys.readouterr().err
    assert "relative/folder" not in got and err.count("is not an absolute path") == 1, (got, err)
    assert err.count("is a URL") == 1 and err.count("%-encoded") == 1 and err.count("does not exist") == 1, err
    assert os.path.normcase(str(deny)) in {os.path.normcase(os.path.normpath(x)) for x in got}, got
    monkeypatch.setenv("FINDR_MCP_DENY", str(tmp_path / "deny%20me"))
    assert m._denied(str(deny / "plan.txt"))
    monkeypatch.setenv("FINDR_MCP_DENY", Path(deny).as_uri())
    assert m._denied(str(deny / "plan.txt"))
