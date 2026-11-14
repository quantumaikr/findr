"""remask looks only at chunks whose text was written since its last look (#67: a run with no change scanned 920k chunks in
Python for 58 s), and its SQL prefilter keeps every chunk a KEYLIKE pattern could match"""
import sqlite3

import index

EXAMPLES = ["sk-" + "a" * 24, "sk-ant-" + "b" * 24, "AKIA" + "A" * 16, "gh" + "p_" + "c" * 32, "gh" + "o_" + "d" * 32, "hf_" + "e" * 32,
            "xoxb-" + "1" * 12, "AIza" + "f" * 35, "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
            "API_KEY=" + "g" * 16, "apikey: " + "h" * 16, "Secret = '" + "i" * 16, "PASSWORD=" + "j" * 16, "passwd:" + "k" * 16,
            "token=" + "l" * 16, "sk_" + "live_" + "m" * 20, "rk_" + "test_" + "m" * 20, "github_" + "pat_" + "n" * 24,
            "gl" + "pat-" + "o" * 20, "xo" + "xp-" + "p" * 12, "Bearer " + "q" * 20, "eyJ" + "r" * 8 + ".eyJ" + "s" * 8 + "." + "t" * 8,
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\nabc", "Private-Lines: 1\nAAAA" + "u" * 20, "https://kim:" + "v" * 8 + "@example.com",
            '{"client_secret": "' + "w" * 16 + '"}', "DB_PWD=" + "x" * 16, "private_key: " + "y" * 16]


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);"
                      "CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');"
                      "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);")
    con.execute("INSERT INTO files VALUES(1, '/d/메모.txt', '메모.txt', '.txt', 1, 0, 1)")
    return con


def test_prefilter_covers_every_keylike_form():
    con = sqlite3.connect(":memory:"); con.execute("CREATE TABLE t(text TEXT)")
    w = " OR ".join(["text LIKE ?"] * len(index.KEYLIKE_HINTS))
    for ex in EXAMPLES:
        s = "앞 글 " + ex + " 뒤 글"
        assert index.KEYLIKE.search(s), ex
        con.execute("DELETE FROM t"); con.execute("INSERT INTO t VALUES(?)", (s,))
        assert con.execute(f"SELECT count(*) FROM t WHERE {w}", [f"%{h}%" for h in index.KEYLIKE_HINTS]).fetchone()[0] == 1, ex


def test_only_new_or_changed_chunks_are_looked_at(capsys):
    con = _db()
    con.execute("INSERT INTO chunks VALUES(1, 1, 0, 'hf_" + "x" * 32 + "', NULL)")
    con.execute("INSERT INTO chunks VALUES(2, 1, 1, '회의 메모', NULL)")
    index.remask(con)
    assert "FINDR_MASK 1 chunks masked (all" in capsys.readouterr().out
    assert "hf_" not in con.execute("SELECT text FROM chunks WHERE id=1").fetchone()[0]
    index.remask(con)   # nothing written since: nothing looked at
    assert "FINDR_MASK 0 chunks masked (new or changed" in capsys.readouterr().out
    con.execute("UPDATE chunks SET text='token=" + "y" * 16 + "' WHERE id=2")   # a later stage fills a chunk in place
    index.remask(con)
    assert "FINDR_MASK 1 chunks masked (new or changed" in capsys.readouterr().out
    assert "yyyy" not in con.execute("SELECT text FROM chunks WHERE id=2").fetchone()[0]


def test_changed_pattern_looks_at_everything_again(capsys):
    con = _db()
    con.execute("INSERT INTO chunks VALUES(1, 1, 0, '평범한 글', NULL)")
    index.remask(con); capsys.readouterr()
    con.execute("UPDATE meta SET value='old' WHERE key='mask_pattern'")
    index.remask(con)
    assert "(all chunks looked at)" in capsys.readouterr().out
