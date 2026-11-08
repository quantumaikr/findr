"""doctor said 'embeddinggemma-2' on a Mac whose Apple GPU self-check had chosen the small model (field, 0.2.13): the model
list is what the PC may have; doctor now also says which meaning model the index uses, and why"""
import sqlite3

from findr import cli


def test_index_encoder(tmp_path):
    assert cli._index_encoder(tmp_path / "none.db") == ""
    db = tmp_path / "index.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO meta VALUES('encoder', 'e5-small')"); con.commit(); con.close()
    assert cli._index_encoder(db) == "e5-small"
