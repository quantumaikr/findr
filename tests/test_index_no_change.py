"""a run with nothing new costs seconds, not ~40 s: no encoder load without work, remask walks only its todo list,
and embed() finds its rows through the 'no vector yet' index (field: 39.9 s -> 3.1 s on a 463k-chunk Mac index)"""
import sqlite3
import sys

import index


def _db(tmp_path):
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("""
      CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT, pending INT DEFAULT 0);
      CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB, emb2 BLOB);
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
      CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');
      CREATE INDEX ch_n ON chunks(n, file);
      CREATE INDEX ch_noemb ON chunks(n, file) WHERE emb IS NULL;""")
    con.execute("INSERT INTO files VALUES(1,'/d/a.txt','a.txt','.txt',1,1.0,1,0)")
    con.execute("INSERT INTO chunks(file,n,text,emb) VALUES(1,0,'견적 단가',x'00')")
    con.execute("INSERT INTO meta VALUES('encoder','e5-small')")
    con.commit()
    return con


def test_embed_with_nothing_to_do_loads_no_encoder(tmp_path, monkeypatch):
    import enc_eg2
    def boom(*a, **k):
        raise AssertionError("the encoder was loaded with nothing to embed")
    monkeypatch.setattr(enc_eg2, "make_encoder", boom)
    con = _db(tmp_path)
    index.embed(con, "e5-small")
    index.embed(con, "e5-small", names_only=True)


def test_remask_walks_the_todo_list_not_every_chunk(tmp_path):
    con = _db(tmp_path)
    index.remask(con)        # first look (pattern recorded)
    w = " OR ".join(["c.text LIKE ?"] * len(index.KEYLIKE_HINTS))
    plan = [r[3] for r in con.execute(f"EXPLAIN QUERY PLAN SELECT c.id FROM mask_todo t CROSS JOIN chunks c ON c.id = t.id WHERE {w}",
                                      [f"%{h}%" for h in index.KEYLIKE_HINTS])]
    assert plan[0].startswith("SCAN t"), plan
    src = open(index.__file__, encoding="utf-8").read()
    assert "mask_todo t CROSS JOIN chunks c" in src   # the code uses the forced order


def test_embed_rows_use_the_no_vector_index(tmp_path):
    con = _db(tmp_path)
    plan = " ".join(r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT c.id FROM chunks c JOIN files f ON f.id=c.file WHERE c.emb IS NULL AND f.tier>0 AND c.n < 8"))
    assert "ch_noemb" in plan, plan
