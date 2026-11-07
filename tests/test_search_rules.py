"""search-side rules that need no model: version questions, the indexing banner"""
import os
import subprocess
import sys

import search


def r(name, folder, date, ce):
    return {"name": name, "path": f"/Users/kim/{folder}/{name}", "date": date, "ce": ce}


def test_prev_version_picks_one_before_latest_of_the_named_family():
    # what a real Mac returned for 'doc1 최종본 말고 그 직전 판': a page saying '최종본이 아닙니다' ranked first
    res = [r("index.html", "film/review-v2", "2026-09-19 11:25", 0.96),
           r("make_doc1_v3.py", "docs/v2_build", "2026-10-15 00:16", 0.40),
           r("doc1_v2.txt", "docs/v2_build", "2026-10-15 00:02", 0.20),
           r("doc1_v3.txt", "docs/v2_build", "2026-10-15 00:16", 0.10),
           r("의견서_초안_v5.docx", "법무", "2026-10-18 16:48", 0.05),
           r("doc1_v4.txt", "docs/v2_build", "2026-10-15 01:42", 0.04)]
    q = "doc1 최종본 말고 그 직전 판"
    assert search.PREVQ.search(q)
    out = search.version_pick(q, res)
    assert [x["name"] for x in out[:3]] == ["doc1_v3.txt", "doc1_v4.txt", "doc1_v2.txt"]
    assert out[0]["ce"] == 0.96 and len(out) == len(res)


def test_prev_version_by_number_not_by_text_order():
    res = [r("보고서_v10.hwp", "a", "2026-01-01", 0.9), r("보고서_v9.hwp", "a", "2026-01-02", 0.5), r("보고서_v2.hwp", "a", "2026-01-03", 0.4)]
    assert search.version_pick("보고서 최신 거 말고 바로 전", res)[0]["name"] == "보고서_v9.hwp"


def test_prev_version_no_family_no_change():
    res = [r("a.txt", "x", "2026-01-01", 0.9), r("b.txt", "y", "2026-01-02", 0.5)]
    assert search.version_pick("최종본 말고 그 직전 판", res) == res


def test_generic_old_question_keeps_oldest_rule():
    assert not search.PREVQ.search("예전 버전 계약서")
    assert search.OLDQ.search("예전 버전 계약서")


def test_banner_hidden_when_indexer_gone():
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
    assert not search.indexer_alive({"stage": "meaning", "done": 1, "total": 2, "pid": p.pid}) or os.name == "nt"
    assert search.indexer_alive({"stage": "meaning", "done": 1, "total": 2, "pid": os.getpid()})
    assert search.indexer_alive({"stage": "meaning", "done": 1, "total": 2})   # an older indexer without a pid


def test_version_first_uses_every_version_in_the_folder(tmp_path):
    """only v3 and v4 were among the candidates; the folder holds v2..v7, so '바로 전 판' is v6"""
    import sqlite3, unicodedata
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);")
    d = "/Users/kim/docs/법무"
    for v in range(2, 8):
        nm = f"의견서_초안_v{v}_2026101{v}.docx"
        if v % 2:   # some names as an old macOS index holds them: decomposed (NFD), before #12
            nm = unicodedata.normalize("NFD", nm)
        con.execute("INSERT INTO files(path,name,size,mtime,tier) VALUES(?,?,?,?,1)", (f"{d}/{nm}", nm, 1, 1.79e9 + v))
    con.execute("INSERT INTO files(path,name,size,mtime,tier) VALUES(?,?,?,?,1)", (f"{d}/old/의견서_초안_v9.docx", "의견서_초안_v9.docx", 1, 1.79e9))
    import unicodedata
    con.create_function("nfc", 1, lambda s: unicodedata.normalize("NFC", s) if s else s, deterministic=True)   # as Finder does
    f = search.Finder.__new__(search.Finder); f.con = con
    res = [dict(r("index.html", "x", "2026-10-01", 0.9), fid=99),
           dict(r("의견서_초안_v3_20261013.docx", "docs/법무", "2026-10-13", 0.3), fid=2),
           dict(r("의견서_초안_v4_20261014.docx", "docs/법무", "2026-10-14", 0.2), fid=3)]
    out = f.version_first("의견서 최신 거 말고 바로 전 판", res)
    assert unicodedata.normalize("NFC", out[0]["name"]) == "의견서_초안_v6_20261016.docx" and len(out) == len(res)


def test_latest_version_question():
    res = [r("index.html", "x/개정사항_정리", "2026-10-15", 0.9),
           r("H3Lab_사업계획서_개정사항_정리.pdf", "docs/사업계획", "2026-10-15 11:03", 0.5),   # 'H3Lab' only
           r("1_H3Lab_기술사업계획서.docx", "docs/사업계획", "2026-10-14 23:16", 0.3),
           r("1_H3Lab_기술사업계획서_v4.docx", "docs/사업계획", "2026-10-15 01:50", 0.2)]
    q = "H3Lab 기술사업계획서 최종본"
    assert search.LATESTQ.search(q) and not search.PREVQ.search(q)
    assert search.version_pick(q, res, latest=True)[0]["name"] == "1_H3Lab_기술사업계획서_v4.docx"


def test_second_pass_only_when_worth_it(tmp_path, monkeypatch):
    """#20: on an Apple GPU or CPU the side-by-side pass (~10 s) runs only for a close call, and not while indexing"""
    import json, time
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); monkeypatch.delenv("FINDR_REFINE", raising=False)
    clear = {"results": [{"prob": 0.9}, {"prob": 0.1}]}; close = {"results": [{"prob": 0.5}, {"prob": 0.4}]}
    monkeypatch.setattr(search, "DEVICE", "cuda")
    assert search.refine_skip(clear) == ""   # about 1 s on NVIDIA: always
    monkeypatch.setattr(search, "DEVICE", "mps")
    assert search.refine_skip(clear) == "clear" and search.refine_skip(close) == ""
    (tmp_path / "progress.json").write_text(json.dumps({"stage": "meaning", "t": time.time(), "pid": os.getpid()}))
    assert search.refine_skip(close) == "indexing"
    monkeypatch.setenv("FINDR_REFINE", "off")
    assert search.refine_skip(close) == "off"


def test_korean_words_english_name():
    """#19: '로컬 AI 터널링 설계 문서' means Local_AI_Tunneling_Blueprint.pdf, not a deck whose text says 로컬 … 설계"""
    res = [{"name": "Cleaned-00. Local_AI_Video_Automation_Engineering.pdf"}, {"name": "18. Vercel_Railway_zrok_Integration.pdf"},
           {"name": "로컬 설계 메모.txt"}, {"name": "notes.md"}, {"name": "20. Local_AI_Tunneling_Blueprint.pdf"}]
    assert search.xling_name_first("로컬 AI 터널링 설계 문서", res) == 4
    assert search.xling_name_first("로컬 설계 메모", res) == 0   # same-script name: left to the usual rules
    assert search.xling_name_first("터널링 설계 문서", [res[4], res[0]]) == 0   # already first
    assert search.xling_name_first("사진", res) == 0   # one idea is not enough to override the ranker


def test_expansions_of_one_word_count_once():
    # 생성기 -> GLOSS "gen generate": an image named Gemini_Generated_Image must not hold "two" question words
    assert search.distinct_words(["배포하는", "생성기", "gen", "generate"]) == ["배포하는", "생성기", "generate"]
    assert search.distinct_words(["report", "report", "eval"]) == ["report", "eval"]


def test_no_facts_table_means_no_understanding_first(tmp_path):
    import sqlite3
    f = search.Finder.__new__(search.Finder)
    f.con = sqlite3.connect(tmp_path / "i.db")
    assert search._has_facts(f) is False   # the page's '이해 우선' then falls back instead of failing on 'no such table: facts'
