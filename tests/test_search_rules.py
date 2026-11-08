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


def test_xling_partial_names(tmp_path, monkeypatch):
    """XLING_PARTIAL=1: a 3+ idea question whose file name keeps only some ideas ('갤럭시 스마트폰 광고 문구' for
    Galaxy_X1_copy_deck.pptx: galaxy and copy, no ad, no phone) is found from all names when at least half the ideas are
    carried, two of them in English. Off by default; a name that carries every idea still comes first; two ideas or one
    English word are not enough; too many equally good names are not a name search"""
    import sqlite3
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);")
    rows = [(1, "Galaxy_X1_copy_deck_v1.pptx", 1, 1), (2, "Galaxy_X1_copy_deck_v2.pptx", 5, 1), (3, "galaxy_wallpaper.png", 9, 1),
            (4, "Galaxy_copy_lib.py", 9, 2), (5, "copy_of_notes.txt", 9, 1)]
    for fid, nm, mt, tier in rows:
        con.execute("INSERT INTO files VALUES(?,?,?,?,1,?,?)", (fid, "/d/" + nm, nm, nm.rsplit(".", 1)[1], mt, tier))
        con.execute("INSERT INTO chunks VALUES(?,?,0,'')", (fid * 10, fid))
    f = search.Finder.__new__(search.Finder); f.con = con; f.name_rows = None
    q = "갤럭시 스마트폰 광고 문구"
    assert f.xling_named(q) == []   # off by default: every idea, as before
    monkeypatch.setenv("XLING_PARTIAL", "1")
    assert f.xling_named(q) == [2, 1]   # galaxy + copy (2 of 4, both in English), newest first; library file (tier 2) out
    assert f.xling_named("갤럭시 광고 문구") == [2, 1]   # 2 of 3
    assert f.xling_named("갤럭시 배경화면") == []   # two ideas: the every-idea rule decides, not this one
    assert f.xling_named(q, [{"name": "Galaxy_copy_notes.md"}]) == []   # the top 10 already holds such a name
    assert f.xling_named(q, [{"name": "갤럭시 광고 초안.hwp"}]) == []   # ... or one that carries as much in Korean
    assert f.xling_named(q, [{"name": "광고_메모.txt"}]) == [2, 1]   # one idea in the top 10 is less than two
    assert search.xling_partial("갤럭시_광고_문구_초안.hwp", search.xling_ideas(q)) == 0   # same script: no English idea
    assert search.xling_partial("galaxy_wallpaper.png", search.xling_ideas(q)) == 0   # one English idea of four
    for i in range(60):   # sixty names that carry the same two ideas: a pair of common words, not a name search
        con.execute("INSERT INTO files VALUES(?,?,?,'txt',1,1,1)", (100 + i, f"/d/galaxy_copy_{i}.txt", f"galaxy_copy_{i}.txt"))
        con.execute("INSERT INTO chunks VALUES(?,?,0,'')", (1000 + i, 100 + i))
    f.name_rows = None
    assert f.xling_named(q) == []
    con.execute("INSERT INTO files VALUES(9,'/d/Galaxy_Ad_Copy.docx','Galaxy_Ad_Copy.docx','docx',1,2,1)")
    con.execute("INSERT INTO chunks VALUES(90,9,0,'')"); f.name_rows = None
    assert f.xling_named("갤럭시 광고 문구") == [9]   # every idea carried: the strict rule, before any partial one
    assert f.xling_named(q) == [9]   # 3 of 4 beats the sixty names with 2 of 4: only the names that carry most count


def test_xling_words_no_name_has(monkeypatch):
    """people ask with words no file name carries ('블러 적용해 보는 예제': 적용해, 보는); those no longer block the English name,
    but a name that carries every word of the question, in Korean, still wins over the looser English match"""
    monkeypatch.setenv("XLING_RELAX", "1")
    res = [{"name": "notes.md"}, {"name": "readme.txt"}, {"name": "blur_example.py"}]
    assert search.xling_name_first("블러 적용해 보는 예제", res) == 2
    assert search.xling_sets("블러 적용해 보는 예제")[1] == [["블러", "blur"], ["예제", "example", "sample"]]
    res2 = res + [{"name": "블러 적용해 보는 예제.ipynb"}]
    assert search.xling_name_first("블러 적용해 보는 예제", res2) == 0   # the same-script name: left to the usual rules
    assert search.xling_name_first("온누리식품 견적서", [{"name": "a.txt"}, {"name": "quote.pdf"}]) == 0   # one English idea
    assert len(search.xling_sets("메모 정리")) == 1   # nothing translatable: no looser set (the strict one never fires)
    monkeypatch.delenv("XLING_RELAX")
    assert search.xling_name_first("블러 적용해 보는 예제", res) == 0   # off by default: every idea, as before


def test_loanwords_by_sound(monkeypatch):
    """off unless XLING_SOUND=1. a loanword matches the English name word it sounds like, with no list entry (판다스 ~ pandas, 쿠버네티스 ~ kubernetes);
    verb forms are not loanwords (불러오기 is not 'block'), and short sounds (2 consonants) do not count"""
    for ko, en in [("판다스", "pandas"), ("쿠버네티스", "kubernetes"), ("깃허브", "github"), ("노션", "notion"), ("태깅", "tagging"),
                   ("텐서플로", "tensorflow"), ("레시피", "recipe"), ("인스타카트", "instacart")]:
        assert search.ko_sound(ko) == search.en_sound(en), (ko, en)
    assert search.xling_ideas("판다스 예제")[0] == ["판다스"]   # off by default
    monkeypatch.setenv("XLING_SOUND", "1")
    assert search.xling_ideas("불러오기 예제")[0] == ["불러오기"]   # no sound spelling for a verb form
    assert search.xling_ideas("도커 예제")[0] == ["도커"]   # tk: too short to trust
    res = [{"name": "notes.md"}, {"name": "Pandas_Example.ipynb"}]
    assert search.xling_name_first("판다스 예제", res) == 1
    assert search.xling_cover("instacartorders_analysis.ipynb", search.xling_ideas("인스타카트 분석"))[0]   # held in a longer word
    assert not search.xling_cover("blocks.txt", [["불러오기"]])[0]


def test_relax_sound_only_words_are_optional(monkeypatch):
    """(sounds on) relaxed: a word with only a sound spelling (a native word sounds like nothing) does not block the name, but counts when it
    matches; two carried ideas at least"""
    monkeypatch.setenv("XLING_RELAX", "1"); monkeypatch.setenv("XLING_SOUND", "1")
    loose = search.xling_sets("경사도 이미지 노트북")[-1]
    assert loose[0][0] == "?" and search.xling_cover("image_gradient.ipynb", loose) == (True, True)
    assert not search.xling_cover("image.png", loose)[0]   # one idea is not enough


def test_particle_alone_is_no_idea():
    """'에서' alone strips to nothing: no idea, no crash (a gate run hit IndexError on it)"""
    assert search.xling_ideas("서버 에서 설정") == [["서버", "server"], ["설정", "config", "setting"]]
    assert search.xling_name_first("서버 에서 설정", [{"name": "a.txt"}, {"name": "server_config.yaml"}]) == 1


def test_two_letter_scan_is_bounded(tmp_path):
    """a one-word 2-letter question reads the text without an index: after a deep run (920k pieces) it took 24-31 s on a
    Mac. A common word stops at 201 files (literal_files drops more than 200 anyway: the same answer), a rare word is still
    found, and a scan past its time limit gives names and paths alone instead of an error"""
    import sqlite3
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);")
    con.executemany("INSERT INTO files VALUES(?,?,?,'.txt',1,?,1)", [(i, f"/d/f{i}.txt", f"f{i}.txt", i) for i in range(400)])
    con.executemany("INSERT INTO chunks VALUES(?,?,0,?)", [(i, i, "사진 정리" if i < 300 else ("대산 견적" if i == 350 else "메모")) for i in range(400)])
    con.commit()
    import unicodedata as _ud
    con.create_function("nfc", 1, lambda s: _ud.normalize("NFC", s) if s else s, deterministic=True)
    f = search.Finder.__new__(search.Finder); f.con = con
    assert len(f.scan_text("%사진%")) == 201   # stops early
    assert f.literal_files("사진") == []   # a common word, as before
    assert f.literal_files("대산") == [350]   # a rare word, still found in the text
    assert f.scan_text("%대산%", seconds=-1, every=1) == set()   # past the time limit: no text matches, no error
    assert con.execute("SELECT count(*) FROM chunks").fetchone()[0] == 400   # the connection still works
