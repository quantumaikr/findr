"""key-like values masked wherever text leaves findr (red-team pass, 11/14): AWS secret settings, JSON "password": "...",
Stripe / GitHub / GitLab / Slack tokens, Bearer and bare JWTs, PGP and PuTTY private keys, a PEM key with no END line,
and the password in a URL. Prose and ordinary code stay as they are. All values here are made up, and built from pieces so
that no whole token sits in the source"""
import importlib
import json
import sqlite3

import pytest

import index

FAKE = "FAKE0abcdefghijklmnopqrstuv"
LEAKS = [
    "AWS_SECRET_ACCESS_KEY=" + "wJalrXUtnFEMI" + FAKE,
    "aws_secret_access_key = " + "wJalrXUtnFEMI" + FAKE,
    '{"client_secret": "' + "cs_" + FAKE + '"}',
    '{"password": "' + "Hunter2" + FAKE + '"}',
    '{"api_key": "' + "AK" + FAKE + '"}',
    "{'secret': '" + FAKE + "'}",
    '"token":"' + FAKE + '"',
    "sk_" + "live_" + "51" + FAKE,
    "rk_" + "live_" + "51" + FAKE,
    "github_" + "pat_" + "11" + FAKE + "_" + FAKE,
    "gh" + "o_" + FAKE + "0123456789",
    "gh" + "s_" + FAKE + "0123456789",
    "gl" + "pat-" + FAKE,
    "xo" + "xp-" + "1234-5678-" + FAKE,
    "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJmYWtlIn0" + "." + FAKE,
    "token in a log " + "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJmYWtlIn0" + "." + FAKE,
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: x\n\nlQOYB" + FAKE + "\n" + FAKE + "\n-----END PGP PRIVATE KEY BLOCK-----",
    "PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 1\nAAAAI" + FAKE,
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEp" + FAKE + "\nMIIEp" + FAKE,   # no END line: the note was cut short
    "postgres://appuser:" + "Pw" + FAKE + "@db.example:5432/app",
]


@pytest.mark.parametrize("leak", LEAKS)
def test_masked(leak):
    out = index.mask("앞 글\n" + leak + "\n")
    assert FAKE not in out and "[가림]" in out, out


def test_url_keeps_its_address():
    assert index.mask("https://kim:" + "Pw" + FAKE + "@example.com/x") == "https://kim:[가림]@example.com/x"


def test_pem_without_end_stops_at_a_paragraph():
    out = index.mask("-----BEGIN PRIVATE KEY-----\nMIIEv" + FAKE + "\n\n회의 메모는 여기서 이어짐")
    assert FAKE not in out and out.endswith("회의 메모는 여기서 이어짐")


PROSE = """The password policy requires 12 characters. Use a token bucket for rate limiting.
tokenizer_config = load_tokenizer_from_disk()
max_tokens = 4096
See https://example.com:8080/path@x and mailto:someone@example.com and https://user@example.com/a
The secret to success is persistence and preparation every day.
Bearer of bad news. 비밀번호를 바꾸세요. 토큰 사용량 보고서.
-----BEGIN PUBLIC KEY-----
MIIBIjAN
-----END PUBLIC KEY-----
email: kim@example.com"""


def test_prose_and_code_unchanged():
    assert index.mask(PROSE) == PROSE


def test_chunks_are_masked():
    assert all(FAKE not in c for c in index.chunks("\n".join(LEAKS)))


def test_serve_answers_are_masked():
    import search
    body = json.dumps({"results": [{"path": "/d/a.txt", "name": "a.txt", "snippet": LEAKS[0]}], "note": ""}).encode()
    out = json.loads(search.masked_json(body))
    assert FAKE not in out["results"][0]["snippet"] and out["results"][0]["path"] == "/d/a.txt"
    assert search.masked_json(b"not json") == b"not json"


def test_mcp_masks_an_index_made_before_the_rule(tmp_path, monkeypatch):
    """chunks stored unmasked (an index made by an older findr): read_file and the index search still mask"""
    monkeypatch.setenv("FINDR_HOME", str(tmp_path)); monkeypatch.setenv("FINDR_APP_PORT", "9")
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    con = sqlite3.connect(str(tmp_path / "index.db"))
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);
        CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    con.execute("INSERT INTO files VALUES(1,'/d/config_dump.txt','config_dump.txt','.txt',1,1700000000,1)")
    con.execute("INSERT INTO chunks VALUES(1,1,0,?)", ("alpha project\n" + "\n".join(LEAKS),))
    con.commit(); con.close()
    r = m.read_file("/d/config_dump.txt", 20000)
    assert r["text"].startswith("alpha project") and FAKE not in r["text"]
