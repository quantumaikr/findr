"""a Korean question about an English document: the word lane does not vote against what it cannot read (XFAIR=1)"""
import sqlite3

import search


def _finder():
    f = search.Finder.__new__(search.Finder)
    f.con = sqlite3.connect(":memory:")
    f.con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT);"
                        "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);")
    rows = [(1, "lease_agreement_screen.pdf", ".pdf", "EQUIPMENT LEASE AGREEMENT This agreement is made between the lessor"),
            (2, "용역계약서.docx", ".docx", "용역 계약서 제1조 목적 갑과 을은 다음과 같이 계약을 체결한다"),
            (3, "build.gradle", ".gradle", "apply plugin android dependencies implementation")]
    for i, name, ext, text in rows:
        f.con.execute("INSERT INTO files VALUES(?,?,?,?)", (i, "/h/" + name, name, ext))
        f.con.execute("INSERT INTO chunks VALUES(?,?,0,?)", (i * 10, i, text))
    f._dv = 1
    return f


def test_an_english_document_found_by_meaning_is_not_buried_by_korean_word_matches():
    f = _finder()
    dense, lex = [10, 20], [20, 30]          # meaning: the English lease first; words: Korean contract and a code file
    score = {}
    for lst in (dense, lex):
        for r, cid in enumerate(lst):
            score[cid] = score.get(cid, 0) + 1 / (60 + r)
    assert max(score, key=score.get) == 20    # as before: the Korean one wins on two lanes
    f.fair_fusion("방호스크린 빌리는 영어 계약서", dense, lex, score)
    assert max(score, key=score.get) == 10    # the English one, which the word lane could not read
    assert f.en_files() == {1}                # code files are not office documents


def test_nothing_changes_without_a_sign_of_english():
    f = _finder()
    dense, lex = [20, 10], [20]
    score = {20: 2 / 60, 10: 1 / 61}
    before = dict(score)
    f.fair_fusion("용역 계약서", dense, lex, score)   # no language word, and the best meaning match is Korean
    assert score == before
