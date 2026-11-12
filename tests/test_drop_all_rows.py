"""a file that leaves the index leaves every table that keeps rows by its id. drop() cleared files, chunks, fts and
photo_emb but not cards, docmeta or events; SQLite gives the highest deleted id to the next new file, which then
showed the deleted file's summary, author and history."""
import sqlite3

import index


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript("""
      CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
      CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
      CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');
      CREATE TABLE cards(file INTEGER PRIMARY KEY, outline TEXT, summary TEXT, done INT DEFAULT 0);
      CREATE TABLE photo_emb(file INTEGER PRIMARY KEY, emb BLOB, taken REAL);
      CREATE TABLE docmeta(file INTEGER PRIMARY KEY, author TEXT, last_by TEXT, revision INT);
      CREATE TABLE events(id INTEGER PRIMARY KEY, file INT, type TEXT, time TEXT, person TEXT, source TEXT);""")
    return con


def _add(con, path):
    fid = con.execute("INSERT INTO files(path,name) VALUES(?,?)", (path, path)).lastrowid
    cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,0,'x')", (fid,)).lastrowid
    con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,'x')", (cid, path, path))
    return fid


def test_drop_clears_every_table_of_the_file():
    con = _db()
    fid = _add(con, "/a/old.docx")
    con.execute("INSERT INTO cards(file,summary) VALUES(?, 'old summary')", (fid,))
    con.execute("INSERT INTO docmeta(file,author) VALUES(?, 'someone')", (fid,))
    con.execute("INSERT INTO events(file,type) VALUES(?, 'opened')", (fid,))
    con.execute("INSERT INTO photo_emb(file) VALUES(?)", (fid,))
    index.drop(con, fid)
    for t in ("files", "chunks", "cards", "docmeta", "photo_emb"):
        key = "id" if t == "files" else "file"
        assert con.execute(f"SELECT count(*) FROM {t} WHERE {key}=?", (fid,)).fetchone()[0] == 0, t
    assert con.execute("SELECT count(*) FROM events WHERE file=?", (fid,)).fetchone()[0] == 0


def test_the_next_file_with_the_same_id_starts_clean():
    con = _db()
    fid = _add(con, "/a/old.docx")
    con.execute("INSERT INTO cards(file,summary) VALUES(?, 'old summary')", (fid,))
    index.drop(con, fid)
    new = _add(con, "/a/new.docx")
    assert new == fid   # SQLite reuses the highest deleted id
    assert con.execute("SELECT count(*) FROM cards WHERE file=?", (new,)).fetchone()[0] == 0


def test_an_index_without_the_optional_tables():
    con = sqlite3.connect(":memory:")
    con.executescript("""
      CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
      CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
      CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    fid = _add(con, "/a/x.pdf")
    index.drop(con, fid)
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 0
