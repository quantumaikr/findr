"""a kind of document or a format in the question is a condition (photos and other formats do not jump over it), and
the same file saved twice is one result. Both found on three real PCs (11/9); both behind flags until the PCs agree."""
import sqlite3

import search


def test_photos_wait_when_a_document_kind_or_format_is_asked(monkeypatch):
    monkeypatch.setenv("KIND_GATE", "1")
    p = lambda q: search.parse(q)
    assert search.photos_wanted("회의록", p("회의록")) is False                  # thirteen minutes, not meeting photos
    assert search.photos_wanted("강의 자료 pdf", p("강의 자료 pdf")) is False     # a pdf, not a png
    assert search.photos_wanted("회의 사진", p("회의 사진")) is True              # photos asked for
    assert search.photos_wanted("영수증 찍은 거", p("영수증 찍은 거")) is True     # a document kind, photographed
    assert search.photos_wanted("바닷가에서 찍은", p("바닷가에서 찍은")) is True
    assert search.photos_wanted("작년 여름 바다", p("작년 여름 바다")) is True     # no kind, no format: photos may answer
    monkeypatch.setenv("KIND_GATE", "0")
    assert search.photos_wanted("회의록", p("회의록")) is True                   # off: as before


def _con():
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);")
    for i, name, size, text in [(1, "규정_v3.hwp", 900, "회사 규정 제1장 총칙 제1조 목적 이 규정은"),
                                (2, "규정_v3 (1).hwp", 900, "회사 규정 제1장 총칙 제1조 목적 이 규정은"),
                                (3, "규정_v2.hwp", 880, "회사 규정 제1장 총칙 제1조 목적 이 규정을"),
                                (4, "a.pdf", 50, "파일: a.pdf 위치: x 구조: "), (5, "b.pdf", 50, "파일: b.pdf 위치: y 구조: ")]:
        con.execute("INSERT INTO files VALUES(?,?,?,?,?,0,1)", (i, "/d/" + name, name, "." + name.rsplit(".", 1)[1], size))
        con.execute("INSERT INTO chunks VALUES(?,?,0,?,NULL)", (i, i, text))
    return con


def test_copies_fold_into_one_line():
    res = {"results": [{"fid": f, "path": f"/d/{f}"} for f in (2, 1, 3, 4, 5)]}
    out = search.collapse_copies(_con(), res)
    assert [x["fid"] for x in out["results"]] == [2, 3, 4, 5]     # 1 is 2's copy: under it, not a line of its own
    assert out["results"][0]["copies"] == ["/d/1"] and out["copies_folded"] == 1
    # a different version (v2: other size, other text) stays; files with no text of their own are never folded
    assert any(x["fid"] == 3 for x in out["results"]) and {4, 5} <= {x["fid"] for x in out["results"]}


def test_name_floor_keeps_the_asked_format(monkeypatch):
    """'강의 자료 pdf': the xlsx whose name says 강의자료 does not go first over the pdfs"""
    monkeypatch.setenv("KIND_GATE", "1")
    f = search.Finder.__new__(search.Finder)
    f.con = _con()
    res = [{"fid": 9, "name": "강의자료 신청.xlsx", "path": "/d/x.xlsx"}, {"fid": 8, "name": "발표.pdf", "path": "/d/y.pdf"}]
    monkeypatch.setattr(search, "name_words", lambda q: ["강의자료"])
    assert f.name_floor("강의자료 pdf", res)[0]["name"] == "발표.pdf"
    monkeypatch.setenv("KIND_GATE", "0")
    assert f.name_floor("강의자료 pdf", res)[0]["name"] == "강의자료 신청.xlsx"
