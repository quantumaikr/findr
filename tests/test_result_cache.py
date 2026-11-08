"""whole answers cached by question and state (#R0): the same question again returns the same answer at once, and anything
that can change the answer (the index, the personal memory, the day, the flags) makes a new key"""
import sqlite3

import search


class F:
    def __init__(self, con):
        self.con = con


def test_key_follows_the_state(monkeypatch, tmp_path):
    db = tmp_path / "i.db"
    a = sqlite3.connect(db); a.execute("CREATE TABLE t(x)"); a.commit()
    f = F(sqlite3.connect(db))
    monkeypatch.setattr(search, "personal", lambda: None)
    k1 = search.result_key(f, "회의록", "xce", 10, None, None)
    assert k1 == search.result_key(f, "회의록", "xce", 10, None, None)          # same state: same key
    assert k1 != search.result_key(f, "회의록", "xce", 30, None, None)          # how many
    assert k1 != search.result_key(f, "회의록 pdf", "xce", 10, None, None)
    a.execute("INSERT INTO t VALUES(1)"); a.commit()                            # the indexer commits
    assert k1 != search.result_key(f, "회의록", "xce", 10, None, None)
    monkeypatch.setenv("KIND_GATE", "1")
    k2 = search.result_key(f, "회의록", "xce", 10, None, None)
    monkeypatch.setenv("KIND_GATE", "0")
    assert k2 != search.result_key(f, "회의록", "xce", 10, None, None)          # a flag changes the answer


def test_personal_memory_and_off_switch(monkeypatch, tmp_path):
    f = F(sqlite3.connect(tmp_path / "i.db"))
    mem = type("M", (), {"rows": [1]})()
    monkeypatch.setattr(search, "personal", lambda: mem)
    k1 = search.result_key(f, "q", "xce", 10, None, None)
    mem.rows = [1, 2]                                                           # a choice made since
    assert k1 != search.result_key(f, "q", "xce", 10, None, None)
    monkeypatch.setattr(search, "RESULTS_MAX", 0)
    assert search.result_key(f, "q", "xce", 10, None, None) is None


def test_engine_answers_from_the_cache():
    src = open(search.__file__, encoding="utf-8").read()
    assert 'res["cached"] = True' in src and "RESULTS[rkey] = body" in src
