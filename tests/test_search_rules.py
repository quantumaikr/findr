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
           r("A사_계획서_개정사항_정리.pdf", "docs/사업계획", "2026-10-15 11:03", 0.5),   # 'A사' only
           r("1_A사_사업계획서.docx", "docs/사업계획", "2026-10-14 23:16", 0.3),
           r("1_A사_사업계획서_v4.docx", "docs/사업계획", "2026-10-15 01:50", 0.2)]
    q = "A사 사업계획서 최종본"
    assert search.LATESTQ.search(q) and not search.PREVQ.search(q)
    assert search.version_pick(q, res, latest=True)[0]["name"] == "1_A사_사업계획서_v4.docx"


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
    """#19: '원격 접속 설계 문서' means Remote_Access_Design.pdf, not a deck whose text says 로컬 … 설계"""
    res = [{"name": "Video_Automation_Notes.pdf"}, {"name": "Hosting_Options.pdf"},
           {"name": "원격 설계 메모.txt"}, {"name": "notes.md"}, {"name": "Remote_Access_Design.pdf"}]
    assert search.xling_name_first("원격 접속 설계 문서", res) == 4
    assert search.xling_name_first("원격 설계 메모", res) == 0   # same-script name: left to the usual rules
    assert search.xling_name_first("접속 설계 문서", [res[4], res[0]]) == 0   # already first
    assert search.xling_name_first("사진", res) == 0   # one idea is not enough to override the ranker


def test_name_floor_never_worse_than_system_search(tmp_path):
    """stage 3: a file whose name holds every word of the question is first, as a system file search would list it"""
    import sqlite3
    assert search.name_words("온누리식품 견적서") == ["온누리식품", "견적서"]
    assert search.name_words("작년에 만든 그 발표 자료 어디 있었더라 기억이 안 나") == []   # a sentence is not a name search
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);")
    con.execute("INSERT INTO files VALUES(1,'/d/견적서_온누리식품_v2.pdf','견적서_온누리식품_v2.pdf','.pdf',1,2,1)")
    con.execute("INSERT INTO files VALUES(2,'/d/메모.txt','메모.txt','.txt',1,1,1)")
    f = search.Finder.__new__(search.Finder); f.con = con
    res = [{"name": "메모.txt", "path": "/d/메모.txt", "fid": 2}]
    assert f.name_floor("온누리식품 견적서", res)[0]["fid"] == 1   # not among the results: brought in and put first
    res2 = [{"name": "메모.txt", "fid": 2}, {"name": "견적서_온누리식품_v2.pdf", "fid": 1}]
    assert [x["fid"] for x in f.name_floor("온누리식품 견적서", res2)] == [1, 2]   # among them: moved up
    assert f.name_floor("메모", res2)[0]["fid"] == 2   # #1 already fits: unchanged


def test_xling_named_beyond_the_top_10(tmp_path):
    """a Korean question whose English-named file the first stage never fetched ('갤럭시 광고 문구' -> Galaxy_Ad_Copy.docx):
    found from all names when every idea is in the name, one by translation; common pairs and same-script names are left alone"""
    import sqlite3
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);")
    rows = [(1, "Galaxy_Ad_Copy_v1.docx", 1, 1), (2, "Galaxy_Ad_Copy_v2.docx", 5, 1), (3, "galaxy_wallpaper.png", 9, 1),
            (4, "Galaxy_Ad_Copy_lib.py", 9, 2), (5, "headroad_copy.txt", 9, 1)]
    for fid, nm, mt, tier in rows:
        con.execute("INSERT INTO files VALUES(?,?,?,?,1,?,?)", (fid, "/d/" + nm, nm, nm.rsplit(".", 1)[1], mt, tier))
        con.execute("INSERT INTO chunks VALUES(?,?,0,'')", (fid * 10, fid))
    f = search.Finder.__new__(search.Finder); f.con = con; f.name_rows = None
    assert f.xling_named("갤럭시 광고 문구") == [2, 1]   # newest first; library file (tier 2) and 'headroad' (no 'ad' word) out
    assert f.xling_named("갤럭시 배경화면") == []   # 배경화면 has no English entry: not every idea is carried
    assert f.xling_named("광고") == []   # one idea is not a name search
    assert search.xling_cover("갤럭시_광고_문구.hwp", search.xling_ideas("갤럭시 광고 문구")) == (True, False)   # same script
