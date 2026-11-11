"""the second meaning lane (LANE2=1): the idle-time upgrade fills each file's first piece first, the engine reads those
first pieces as one more candidate list that takes turns with the fused order. No model is loaded here."""
import sqlite3

import numpy as np
import pytest

torch = pytest.importorskip("torch")
import enc_eg2  # noqa: E402
import index  # noqa: E402
import search  # noqa: E402


class Fake:
    """stands in for the larger encoder: 8-wide vectors; remembers what it was given, in order"""
    st = None
    dev = "cpu"
    bs = 8

    def __init__(self):
        self.seen = []

    def emb(self, texts, query=True):
        self.seen += list(texts)
        return torch.nn.functional.normalize(torch.ones(len(texts), 8), dim=-1)


# (file id, name, mtime, pieces): a recent script, an old report, a recent slide deck, an older note
FILES = [(1, "run.py", 400, 3), (2, "report.pdf", 100, 3), (3, "deck.PPTX", 300, 2), (4, "note.md", 200, 1), (5, "data.json", 500, 1)]


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(enc_eg2, "make_encoder", lambda name, *a, **k: f)
    return f


@pytest.fixture
def con(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    monkeypatch.delenv("FINDR_STOP_ON_INPUT", raising=False)
    c = sqlite3.connect(str(tmp_path / "index.db"))
    c.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB, emb2 BLOB);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('encoder', 'e5-small');""")
    small = np.ones(4, np.float16).tobytes()
    for fid, name, mtime, pieces in FILES:
        c.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)", (fid, f"/d/{name}", name, "." + name.rsplit(".", 1)[1], 1, mtime, 1))
        for n in range(pieces - 1, -1, -1):   # later pieces get the lower ids: the order must not come from the ids
            c.execute("INSERT INTO chunks(file,n,text,emb) VALUES(?,?,?,?)", (fid, n, f"{name}#{n}", small))
    c.commit()
    return c


def _order(fake):
    return [t.rsplit("\n", 1)[-1] for t in fake.seen]


def test_first_pieces_first_documents_first_newest_first(con, fake):
    assert index.upgrade(con, "embeddinggemma-2", minutes=10, bs=8) == 0
    got = _order(fake)
    # documents by date (deck 300, note 200, report 100), then the other first pieces by date (data 500, run 400)
    assert got[:5] == ["deck.PPTX#0", "note.md#0", "report.pdf#0", "data.json#0", "run.py#0"]
    # the rest in the order used before: second pieces by date, then the deeper ones
    assert got[5:] == ["run.py#1", "deck.PPTX#1", "report.pdf#1", "run.py#2", "report.pdf#2"]
    assert enc_eg2.encoder_of(con) == "embeddinggemma-2"   # all there: switched, as before


def test_first_only_fills_first_pieces_and_does_not_switch(con, fake, tmp_path):
    assert index.upgrade(con, "embeddinggemma-2", minutes=10, first_only=True) == index.FIRST_DONE == 6
    assert _order(fake) == ["deck.PPTX#0", "note.md#0", "report.pdf#0", "data.json#0", "run.py#0"]
    assert enc_eg2.encoder_of(con) == "e5-small"
    assert con.execute("SELECT count(*) FROM chunks WHERE emb2 IS NOT NULL AND n=0").fetchone()[0] == 5
    assert con.execute("SELECT count(*) FROM chunks WHERE emb2 IS NOT NULL AND n!=0").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM chunks WHERE length(emb)=8").fetchone()[0] == 10   # the small vectors, untouched
    assert con.execute("SELECT value FROM meta WHERE key='upgrade'").fetchone()[0] == "embeddinggemma-2"
    assert (tmp_path / "lane2.json").exists() and not (tmp_path / "upgrade.json").exists()   # not read as the whole upgrade's progress
    assert index.upgrade(con, "embeddinggemma-2", minutes=10, first_only=True) == 6   # again: nothing to do, still not switched
    assert len(fake.seen) == 5
    assert index.upgrade(con, "embeddinggemma-2", minutes=10) == 0   # the whole upgrade goes on from there
    assert len(fake.seen) == 10 and enc_eg2.encoder_of(con) == "embeddinggemma-2"


def test_first_only_out_of_time_asks_for_another_run(con, fake):
    assert index.upgrade(con, "embeddinggemma-2", minutes=0, first_only=True) == 2
    assert fake.seen == [] and enc_eg2.encoder_of(con) == "e5-small"


def test_interleave_takes_turns():
    score = {"a": 0.03, "b": 0.02, "c": 0.01}
    search.lane2_interleave(score, ["x", "b", "y"])
    assert sorted(score, key=lambda c: -score[c]) == ["a", "x", "b", "c", "y"]   # x right after a; b stays; y after c
    assert score["a"] == 0.03 and score["b"] == 0.02 and score["c"] == 0.01     # the fused scores themselves are kept


def test_interleave_lifts_but_never_lowers():
    score = {1: 0.05, 2: 0.04, 3: 0.03, 4: 0.02}
    search.lane2_interleave(score, [4, 1])      # 4: the lane's best, comes right after the fused best; 1 is already above its turn
    assert sorted(score, key=lambda c: -score[c]) == [1, 4, 2, 3] and score[1] == 0.05


def test_interleave_longer_than_the_fused_list_and_alone():
    score = {1: 0.02}
    search.lane2_interleave(score, [7, 8, 9])
    assert sorted(score, key=lambda c: -score[c]) == [1, 7, 8, 9]
    alone = search.lane2_interleave({}, [7, 8, 9])
    assert sorted(alone, key=lambda c: -alone[c]) == [7, 8, 9]


def test_interleave_only_the_first_hundred_and_the_same_each_time():
    base = {i: 1 / (60 + i) for i in range(300)}
    lane = list(range(1000, 1250))
    a = search.lane2_interleave(dict(base), lane); b = search.lane2_interleave(dict(base), lane)
    assert a == b and len(a) == 300 + search.LANE2_TOP and 1099 in a and 1100 not in a


def _finder(con, encoder="e5-small"):
    f = search.Finder.__new__(search.Finder)
    f.con = con; f.encoder = encoder
    f.lane2_set(f.lane2_read(con, encoder))
    return f


class Q:
    """the larger encoder for a question: points along the first axis"""
    def emb(self, texts, query=True):
        v = torch.zeros(len(texts), 300); v[:, 0] = 1.0
        return v


def _fill(con):
    """first pieces by a 300-wide 'larger' encoder: report closest to the question, then deck, then the rest"""
    con.execute("INSERT OR REPLACE INTO meta VALUES('upgrade', 'embeddinggemma-2')")
    for name, x in (("report.pdf", 0.9), ("deck.PPTX", 0.5), ("note.md", 0.2), ("run.py", 0.1), ("data.json", 0.0)):
        v = np.zeros(300, np.float16); v[0] = x; v[1] = 1 - x; v[299] = 5.0   # the last axis lies beyond the cut
        con.execute("UPDATE chunks SET emb2=? WHERE text=?", (v.tobytes(), name + "#0"))
    con.commit()
    return {t.split("#")[0]: i for i, t in con.execute("SELECT id, text FROM chunks WHERE n=0")}


def test_off_by_default_the_score_is_untouched(con, monkeypatch):
    cid = _fill(con)
    monkeypatch.delenv("LANE2", raising=False)
    f = _finder(con)
    assert len(f.l2ids) == 0 and f._l2sig is None       # nothing read from the index
    f._l2enc = ("", Q())
    assert f.lane2("보고서") == []
    monkeypatch.setenv("LANE2", "0")
    assert _finder(con).lane2("보고서") == []
    score = {cid["run.py"]: 0.03}; before = dict(score)
    assert search.lane2_interleave(score, f.lane2("보고서")) == before


def test_lane_reads_first_pieces_cut_and_normalized(con, monkeypatch):
    cid = _fill(con)
    monkeypatch.setenv("LANE2", "1")
    f = _finder(con)
    assert f.l2target == "embeddinggemma-2" and len(f.l2ids) == 5 and f.l2M.shape == (5, search.LANE2_DIM)
    assert np.allclose(np.linalg.norm(f.l2M, axis=1), 1.0, atol=1e-3)   # normalized after the cut (the 5.0 beyond it is gone)
    f._l2enc = (f.l2target, Q())
    assert f.lane2("보고서") == [cid[n] for n in ("report.pdf", "deck.PPTX", "note.md", "run.py", "data.json")]
    assert f.lane2("보고서", ok={cid["deck.PPTX"], cid["run.py"]}) == [cid["deck.PPTX"], cid["run.py"]]   # the question's conditions
    assert f.lane2("보고서") == f.lane2("보고서")
    score = {cid["run.py"]: 0.03, cid["data.json"]: 0.02}
    search.lane2_interleave(score, f.lane2("보고서"))
    assert sorted(score, key=lambda c: -score[c])[:4] == [cid["run.py"], cid["report.pdf"], cid["data.json"], cid["deck.PPTX"]]


def test_no_lane_on_an_index_already_on_the_larger_encoder_or_without_the_column(con, monkeypatch, tmp_path):
    _fill(con)
    monkeypatch.setenv("LANE2", "1")
    assert len(_finder(con, encoder="embeddinggemma-2").l2ids) == 0
    old = sqlite3.connect(str(tmp_path / "old.db"))   # an index from before the upgrade existed: no emb2 column, no meta
    old.executescript("CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);")
    f = _finder(old)
    assert len(f.l2ids) == 0 and f.lane2("보고서") == []


def test_encoder_that_cannot_load_turns_the_lane_off_with_one_line(con, monkeypatch, capsys, tmp_path):
    _fill(con)
    monkeypatch.setenv("LANE2", "1")
    calls = []

    def boom(name, *a, **k):
        calls.append(name); raise MemoryError()
    monkeypatch.setattr(enc_eg2, "make_encoder", boom)
    monkeypatch.setattr(enc_eg2, "PATH", str(tmp_path))   # a model folder that is there
    f = _finder(con)
    assert f.lane2("보고서") == [] and f.lane2("보고서") == [] and f.lane2("다른 질문") == []
    assert len(calls) == 1                                  # tried once
    err = capsys.readouterr().err
    assert err.count("FINDR_LANE2 off") == 1
    monkeypatch.setattr(enc_eg2, "PATH", "no/such/folder")  # no model folder: no download is tried
    g = _finder(con)
    assert g.lane2("보고서") == [] and len(calls) == 1


def test_the_answer_cache_tells_the_two_apart(con, monkeypatch):
    monkeypatch.setattr(search, "personal", lambda: None)
    f = _finder(con)
    monkeypatch.setenv("LANE2", "0"); a = search.result_key(f, "보고서", "base", 10, None, None)
    monkeypatch.setenv("LANE2", "1"); b = search.result_key(f, "보고서", "base", 10, None, None)
    assert a is not None and a != b
