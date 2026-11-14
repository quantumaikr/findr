"""red-team pass on 0.2.25 (11/14): leaks closed after it, by general rules rather than one more literal each. Every
name, value and key here is invented, and tokens are built from pieces so that no whole one sits in the source"""
import base64
import importlib
import os
import sqlite3
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

import index
import moretext

V = "Wom" + "batQ7x"   # the invented secret value every case hides


# ---------------------------------------------------------------- 1, 2. credential tables by stems, anywhere
HEADERS = ["Email Address,Password", "Account Name,Password", "이메일 주소,비밀번호", "계정,비번", "ID,PW", "Login;Passcode",
           "User Name\tPassword", "사용자 이름,암호", "사이트,아이디,패스워드", "Service|Login ID|PIN", "LoginID,Pwd",
           "url,username,password,notes for this account written by the export tool",   # a long column is left out
           '"Site","E-mail","Password (encrypted)"', "도메인,메일,비밀번호", "Host,User,Pass"]


@pytest.mark.parametrize("head", HEADERS)
def test_credential_headers_by_stems(tmp_path, head):
    assert index.cred_header(head + "\n"), head
    p = tmp_path / "export.csv"
    p.write_text(head + "\nx,y," + V + "\n", encoding="utf-8")
    assert index.is_secret(p) and index.secret_text(p.name, p.read_text(encoding="utf-8"))


@pytest.mark.parametrize("text", [
    "name,phone,city\n", "id,title,date\n", "pass rate,year\n", "암호화 방식,사용자\n", "Change the password, then tell the user.\n",
    "Password, username and email are required for every account\n", "def f(self, url, pw, name):\n", "url, pw, name = row\n",
    "항목,담당,금액\n", "policy,owner,status\n", "Enter your user name, password\n"])
def test_ordinary_lines_are_not_credential_headers(text):
    assert not index.cred_header(text), text


def test_header_below_many_title_lines(tmp_path):
    text = "Saved logins\n" + "".join(f"note {i}\n" for i in range(15)) + "url,username,password\nhttps://a.example,kim," + V + "\n"
    p = tmp_path / "creds.csv"
    p.write_text(text, encoding="utf-8")
    assert index.is_secret(p)
    assert not index.cred_header("t\n" * 25 + "url,username,password\n")   # past the first CRED_LINES lines


@pytest.mark.parametrize("name,text", [
    ("logins.md", "# Logins\n\n| Site | User | Password |\n|---|---|---|\n| bank | kim | " + V + " |\n"),
    ("dump.log", "export run\nsite\tlogin\tpasscode\nbank\tkim\t" + V + "\n"),
    ("note.txt", "Bank\nID;PW\nkim;" + V + "\n"),
])
def test_tables_in_notes_and_logs(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    assert index.is_secret(p), name
    assert V not in index.mask(text)


def test_table_in_any_text_has_its_password_column_masked():
    text = ("Hi, my logins below.\n\nurl,username,password\nhttps://a.example,kim," + V + "\nhttps://b.example,lee,\"x," + V + "\"\n"
            "https://c.example,park,pa," + V + "\n\nThanks, see you.\n")
    out = index.mask(text)
    assert V not in out and "https://a.example,kim,[가림]" in out and "Thanks, see you." in out and "url,username,password" in out
    md = "| Site | Login | Password |\n| --- | --- | --- |\n| bank | kim | " + V + " |\n"
    assert index.mask(md) == "| Site | Login | Password |\n| --- | --- | --- |\n| bank | kim | [가림] |\n"
    code = "def __init__(self, url, pw, name):\n    self.url, self.name = url, name\n"
    assert index.mask(code) == code


def _eml(tmp_path, body, atts=()):
    m = EmailMessage(); m["From"] = "a@example.invalid"; m["To"] = "b@example.invalid"; m["Subject"] = "logins"
    m.set_content(body)
    for fn, data, (mt, st) in atts:
        m.add_attachment(data, maintype=mt, subtype=st, filename=fn)
    p = tmp_path / "m.eml"
    p.write_bytes(m.as_bytes())
    return p


def test_mail_body_table_is_masked_and_attachment_left_out(tmp_path):
    p = _eml(tmp_path, "see below\n\nurl,username,password\nhttps://bank.example,gina," + V + "\n",
             [("accts.csv", ("Email Address,Password\nh@example.invalid," + V + "2\n").encode(), ("text", "csv"))])
    t = moretext.eml_text(p)
    assert "see below" in t and "accts.csv" in t and V + "2" not in t
    assert V not in "".join(index.chunks(t))


# ---------------------------------------------------------------- 3, 7, 8. labels glued to other words, and more forms
P1 = V
MASKED = [
    '{"dbPassword": "' + P1 + '"}', '"smtpPassword": "' + P1 + '"', "accessToken: " + P1, '"refreshToken":"' + P1 + '"',
    "clientSecret = " + P1, "apiKey: " + P1, "export PGPASSWORD=" + P1, "MYSQL_PWD=" + P1, "db.password=" + P1,
    "x-api-key: " + P1, "secretKey = '" + P1 + "'", "spring.datasource.password=" + P1, "basic_auth: " + P1,
    "password => " + P1, "token -> " + P1, "PW: " + P1, "pw=" + P1, "P/W: " + P1, "ID: kim  PW: " + P1,
    "define( 'DB_PASSWORD', '" + P1 + "' );", 'os.environ.setdefault("API_TOKEN", "' + P1 + '")',
    '<add key="DbPassword" value="' + P1 + '"/>', '<entry name="password">' + P1 + "</entry>",
    "Authorization: Basic " + base64.b64encode(("u:" + P1).encode()).decode(), "-H 'Authorization: Basic " + P1 + "'",
    "curl -u admin:" + P1 + " https://api.example.invalid", "mysql -uroot -p" + P1 + " app", "mysqldump -p'" + P1 + "' db",
    "--password " + P1, "--password=" + P1, "redis://:" + P1 + "@localhost:6379/0",
    "shp" + "at_" + "0a1b" * 8, "shp" + "ss_" + "0a1b" * 8, "dop" + "_v1_" + "0a1b" * 16, "xa" + "pp-1-A0-123-" + P1 + "abcdef",
    "xo" + "xe-1-" + P1 + "abcdef",
]


@pytest.mark.parametrize("leak", MASKED)
def test_masked_whatever_its_form(leak):
    out = index.mask("앞 글\n" + leak + "\n뒤 글")
    hidden = P1 if P1 in leak else leak[-12:]
    assert hidden not in out and "[가림]" in out and out.endswith("뒤 글"), out


@pytest.mark.parametrize("text", [
    "max_tokens = 4096", "tokenizer_config = load()", "TOKENIZERS_PARALLELISM=false", "BATCH, TOKENS = 5, 384",
    "TokenOpt = Annotated[str]", "LocalTokenNotFoundError: x", "secretary: Kim", "author: Lee", "oauth = OAuth()",
    "token_expiry_unix_secs=3600", "pw = paths.HOME / 'x'", "The password policy requires 12 characters.",
    "if password == expected:", "bypass = true", "pass: 12A", "email: kim@example.com", "Pass --token for a login.",
    "curl https://example.com/a", "mysql -uroot -p app", "https://example.com:8080/path@x", "Basic information: none",
    '_KEYS = ("subject_token", "access_token")', 'if path == "/auth":   # a comment', "doc = '''hello'''",
])
def test_prose_and_code_stay(text):
    assert index.mask(text) == text, text


def test_mask_stays_fast_on_code():
    import time
    code = ("def f(token=None, pw=None):\n    '''doc'''\n    max_tokens = 4096  # tokens\n    x = {\"a\": 1, \"b\": [1, 2]}\n"
            "    return g(url, name, pw)\n") * 20000
    t0 = time.time(); index.mask(code)
    assert time.time() - t0 < 10


# ---------------------------------------------------------------- 4. multi-line values
@pytest.mark.parametrize("text", [
    "db:\n  host: localhost\n  password:\n    " + P1 + "\napi:\n  x: 1\n",
    "api:\n  secret: |\n    " + P1 + "\n    second " + P1 + "\n  other: 1\n",
    "key:\n  token: >-\n    " + P1 + "\n",
    'title = "svc"\npassword = """\n' + P1 + '\n"""\nport = 1\n',
    "password = '''" + P1 + "'''\n",
    "password = \\\n  " + P1 + "\n",
])
def test_multiline_values(text):
    out = index.mask(text)
    assert P1 not in out and "[가림]" in out, out
    assert ("api:" not in text or "api:" in out) and ("port = 1" not in text or "port = 1" in out)


def test_yaml_siblings_stay():
    text = "db:\n  password: |\n    " + P1 + "\n  host: db.example\n"
    assert index.mask(text) == "db:\n  password: |\n    [가림]\n  host: db.example\n"


# ---------------------------------------------------------------- 5. text decoding
@pytest.mark.parametrize("enc,bom", [("utf-32-le", b"\xff\xfe\x00\x00"), ("utf-32-be", b"\x00\x00\xfe\xff"),
                                     ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
                                     ("utf-16-le", b""), ("utf-16-be", b""), ("utf-32-le", b""), ("utf-32-be", b"")])
def test_wide_text_is_decoded(tmp_path, enc, bom):
    text = "password: " + P1 + "\nurl,username,password\nhttps://z.example,eve," + P1 + "\n"
    raw = bom + text.encode(enc)
    assert index._decode_text(raw) == text
    p = tmp_path / "notes.txt"
    p.write_bytes(raw)
    assert index.extract(p, 100000) == text
    assert index.is_secret(p)   # its credential header is seen
    assert P1 not in index.mask(index.extract(p, 100000))


def test_binary_with_zero_bytes_is_still_binary(tmp_path):
    p = tmp_path / "blob.txt"
    p.write_bytes(bytes(range(256)) * 8)
    assert index.extract(p, 100000) == ""


# ---------------------------------------------------------------- 3. terms from masked text
def test_file_relations_terms_never_hold_a_masked_value(tmp_path, monkeypatch):
    data = tmp_path / "data"; data.mkdir()
    monkeypatch.setenv("FINDR_HOME", str(data)); monkeypatch.setenv("FINDR_APP_PORT", "9")
    monkeypatch.setenv("FINDR_MCP_DENY", str(tmp_path / "nothing"))
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    monkeypatch.setattr(m, "_engine_up", lambda: False)
    p = tmp_path / "docs" / "cfg.json"; p.parent.mkdir()
    text = '{"service": "billing", "dbPassword": "' + P1 + '", "accessToken": "' + P1 + 'b"}'   # stored by an older findr
    p.write_text(text, encoding="utf-8")
    con = sqlite3.connect(str(data / "index.db"))
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
        CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    con.execute("INSERT INTO files VALUES(1,?,?,?,?,?,1)", (p.as_posix(), p.name, ".json", len(text), os.path.getmtime(p)))
    con.execute("INSERT INTO chunks VALUES(1,1,0,?,NULL)", (text,))
    con.commit(); con.close()
    r = m.file_relations(p.as_posix())
    assert "billing" in r["terms"] and not any(P1.lower() in w for w in r["terms"]), r
    assert P1 not in m.read_file(p.as_posix())["text"]


# ---------------------------------------------------------------- the whole indexer on such files
def test_indexer_keeps_none_of_them(tmp_path):
    root = tmp_path / "docs"; root.mkdir(); home = tmp_path / "home"; home.mkdir()
    files = {"accounts.csv": "Email Address,Password\nalice@example.invalid," + P1 + "a\n",
             "logins.md": "| Site | ID | PW |\n|--|--|--|\n| bank | kim | " + P1 + "b |\n",
             "cfg.json": '{"dbPassword": "' + P1 + 'c", "service": "plumvex"}',
             "app.yaml": "api:\n  secret: |\n    " + P1 + "d\n",
             "wp.php.txt": "define('DB_PASSWORD', '" + P1 + "e');\n"}
    for n, t in files.items():
        (root / n).write_text(t, encoding="utf-8")
    (root / "wide.txt").write_bytes(b"\xff\xfe\x00\x00" + ("password: " + P1 + "f\n").encode("utf-32-le"))
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_ORIGIN": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home)}
    db = home / "index.db"
    r = subprocess.run([sys.executable, str(Path(index.__file__)), "--root", str(root), "--db", str(db), "--no-embed"],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0, r.stderr[-800:]
    con = sqlite3.connect(db)
    stored = "\n".join(t or "" for (t,) in con.execute("SELECT text FROM chunks"))
    assert P1 not in stored and "plumvex" in stored, stored
    tiers = dict(con.execute("SELECT name, tier FROM files"))
    assert tiers["accounts.csv"] == 0 and tiers["logins.md"] == 0, tiers
