"""Kinds of documents from the stored first-piece vectors, and the MCP search filter that uses them."""
import importlib
import sqlite3

import numpy as np
import pytest

from findr.core import doctype


def test_match_names_kinds_not_formats():
    assert doctype.match("회의록") == ["회의록"]
    assert doctype.match("contract, pdf") == ["계약서·협약서"]
    assert doctype.match("pdf") == [] and doctype.match("") == []
    assert doctype.match("minutes,report") == ["회의록", "보고서"]


def test_heads_ship_for_both_index_encoders():
    h = doctype.heads()
    assert len(h["labels"]) == 14 and "no real files" in h["trained_on"]
    assert h["heads"]["embeddinggemma-2"]["dim"] == 768 and h["heads"]["e5-small"]["dim"] == 384


def _vec(kind, enc="e5-small"):
    """a vector the head reads as this kind: the head's own row for it"""
    w, _ = doctype._head(enc)
    v = w[doctype.heads()["labels"].index(kind)]
    return (v / np.linalg.norm(v)).astype(np.float16)


def _index(path, encoder, files):
    con = sqlite3.connect(str(path))
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    con.execute("INSERT INTO meta VALUES('encoder', ?)", (encoder,))
    for i, (name, text, kind) in enumerate(files, 1):
        p = f"/d/{name}"
        con.execute("INSERT INTO files VALUES(?,?,?,?,1,?,1)", (i, p, name, "." + name.rsplit(".", 1)[1], 1700049600 + i))
        con.execute("INSERT INTO chunks VALUES(?,?,0,?,?)", (i, i, text, _vec(kind).tobytes()))
        con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (i, name, p, text))
    con.commit()
    return con


def test_of_files_reads_the_stored_vectors(tmp_path):
    con = _index(tmp_path / "i.db", "e5-small", [("a.pdf", "프로젝트 회의", "회의록"), ("b.pdf", "프로젝트 계약", "계약서·협약서")])
    k = doctype.of_files(con, [1, 2])
    assert k[1][0] == "회의록" and k[2][0] == "계약서·협약서" and 0 < k[1][1] <= 1
    con.execute("UPDATE meta SET value='some-other-encoder'")
    assert doctype.of_files(con, [1, 2]) == {}   # no head for that encoder: nothing is claimed


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_HOME", str(tmp_path)); monkeypatch.setenv("FINDR_APP_PORT", "9")   # no engine on port 9
    import findr.paths
    importlib.reload(findr.paths)
    return importlib.reload(importlib.import_module("findr.mcp_server")), tmp_path


def test_search_files_filters_by_kind_of_document(mcp):
    m, home = mcp
    _index(home / "index.db", "e5-small", [("프로젝트_0312.docx", "프로젝트 킥오프 정리", "회의록"),
                                          ("프로젝트_최종.docx", "프로젝트 용역 조항", "계약서·협약서")]).close()
    r = m.search_files("프로젝트", kind="회의록")
    assert [x["name"] for x in r["results"]] == ["프로젝트_0312.docx"] and r["results"][0]["doc_kind"] == "회의록"
    assert r["filters"] == {"kind": "회의록"}
    r = m.search_files("프로젝트", kind="contract,docx")   # a kind of document and a format together
    assert [x["name"] for x in r["results"]] == ["프로젝트_최종.docx"]


def test_search_files_kind_without_a_head_says_so(mcp):
    m, home = mcp
    _index(home / "index.db", "unknown-encoder", [("프로젝트_0312.docx", "프로젝트 킥오프 정리", "회의록")]).close()
    r = m.search_files("프로젝트", kind="회의록")
    assert r["results"] and "cannot be told" in r["note"]


def test_search_files_drops_only_files_surely_of_another_kind(mcp, monkeypatch):
    m, home = mcp
    _index(home / "index.db", "e5-small", [("프로젝트_0312.docx", "프로젝트 킥오프 정리", "회의록"),
                                          ("프로젝트_정리.docx", "프로젝트 정리", "보고서"),
                                          ("프로젝트_사진.docx", "프로젝트 사진", "기타")]).close()
    labels = doctype.heads()["labels"]

    def fake(con, fids):   # file 2 is mostly a report but fairly likely minutes; file 3 is surely not minutes
        out = {}
        for f in fids:
            p = np.full(len(labels), 0.01)
            p[labels.index({1: "회의록", 2: "보고서", 3: "기타"}[f])] = 0.6
            if f == 2:
                p[labels.index("회의록")] = 0.3
            out[f] = p / p.sum()
        return out
    monkeypatch.setattr(doctype, "probs", fake)
    r = m.search_files("프로젝트", kind="회의록")
    got = {x["name"]: x for x in r["results"]}
    assert set(got) == {"프로젝트_0312.docx", "프로젝트_정리.docx"}   # the photo is surely another kind: it leaves
    assert "doc_kind_match" not in got["프로젝트_0312.docx"] and got["프로젝트_정리.docx"]["doc_kind_match"] == "maybe" and "may be" in r["note"]


def test_name_model_reads_kind_words_in_names_and_folders():
    labels = doctype.heads()["labels"]
    for path, kind in ((r"C:\work\운영위원회\2024 3월 정기 회의록.hwp", "회의록"), ("/Users/a/계약/용역 계약서_최종.pdf", "계약서·협약서"),
                       ("/home/a/docs/물품 견적서 0312.xlsx", "견적·발주·거래명세")):
        p = doctype.name_probs(path)
        assert abs(p.sum() - 1) < 1e-4 and labels[int(p.argmax())] == kind


def test_grams_match_char_wb():   # the n-grams the name model was trained on (sklearn char_wb, 2-4)
    assert doctype._grams("Ab") == [" a", "ab", "b ", " ab", "ab ", " ab "]
    assert doctype._grams("가") == [" 가", "가 ", " 가 "]
    assert doctype.name_text(r"C:\a\b\c\d\회의록.hwp") == "회의록.hwp | b c d"
    import unicodedata   # a macOS path, decomposed, reads the same
    assert doctype.name_text(unicodedata.normalize("NFD", "/a/회의/회의록.hwp")) == "회의록.hwp | a 회의"


def test_small_encoder_mixes_the_name_model_in(tmp_path):
    con = _index(tmp_path / "i.db", "e5-small", [("2024 정기 회의록.hwp", "", "회의록")])
    p = doctype.probs(con, [1])[1]
    assert doctype.heads()["heads"]["e5-small"]["name_weight"] > 0 and abs(p.sum() - 1) < 1e-4
