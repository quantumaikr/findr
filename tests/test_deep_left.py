"""what a deep run will still embed (#67-1: the first pass said 127,674 pieces, the deep run embedded 725,756 on a Mac)"""
import sqlite3

import index


def db(rows, pieces):
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, ext TEXT, size INT, tier INT, pending INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);")
    con.executemany("INSERT INTO files VALUES(?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO chunks(file, n, text, emb) VALUES(?,?,?,?)", pieces)
    return con


def test_counts_the_unread_rest_of_long_files():
    rows = [(1, "/d/a.txt", ".txt", 1000, 1, 0),     # read in full: 12 pieces, 8 with vectors
            (2, "/d/b.txt", ".txt", 10000, 1, 2),    # read in part: 2 pieces of 100 bytes out of 10,000 -> about 100 pieces
            (3, "/d/c.pdf", ".pdf", 2_000_000, 1, 2),   # a document cut at the gist: 17 pieces per MB -> 34
            (4, "/d/d.bin", "", 50000, 1, 2)]        # nothing read from its opening: nothing more (its one piece is read again)
    pieces = [(1, i, "x" * 10, b"v" if i < 8 else None) for i in range(12)]
    pieces += [(2, i, "y" * 100, b"v") for i in range(2)] + [(3, i, "z", b"v") for i in range(3)] + [(4, 0, "", None)]
    work, files = index.deep_left(db(rows, pieces), 8)
    assert (work, files) == (4 + 100 + 34 + 1, 4)


def test_nothing_left_after_a_deep_run():
    rows = [(1, "/d/a.txt", ".txt", 1000, 1, 0)]
    assert index.deep_left(db(rows, [(1, i, "x", b"v") for i in range(12)]), 8) == (0, 0)
