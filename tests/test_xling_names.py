"""#220: a Korean question finds English-named files (and an English question Korean-named ones) by name: the extra word
list, short forms and case changes in names, months, and a name lane in the first stage"""
import sqlite3

import search


def test_extra_words_count_for_names_not_for_the_word_search(monkeypatch):
    monkeypatch.delenv("XLING2", raising=False)
    assert ["직원", "employee", "staff", "emp"] == search.xling_ideas("직원 핸드북")[0]
    assert search.xl_expand("직원 핸드북") == ""   # the word search over text stays as it was (XLING2 noise, 11/9)
    assert search.xling_cover("employee_handbook.pdf", search.xling_ideas("직원 핸드북")) == (True, True)


def test_short_forms_and_case_changes():
    ideas = search.xling_ideas("주간 회의 메모")
    assert search.xling_cover("weekly_mtg_notes.txt", ideas) == (True, True)
    assert search.xling_cover("WeeklyMtgNotes.txt", ideas) == (True, True)   # camelCase: 'mtg' is a word of it
    assert not search.xling_cover("weeklymtgnotes.txt", ideas)[0]   # no word edge at all: a short form is not guessed
    assert not search.xling_cover("inventory_meeting.txt", search.xling_ideas("인보이스 회의"))[0]   # 'inv' only as a word
    assert search.name_split("QuarterlyRevenueForecast.xlsx") == "quarterly revenue forecast.xlsx"
    assert search.name_split("APISpec_v3.md") == "api spec_v3.md"


def test_month_counts_when_the_name_has_it():
    ideas = search.xling_ideas("10월 회의록")
    assert ideas[0][0] == "?" and "oct" in ideas[0]
    assert search.xling_cover("meeting_minutes_oct.txt", ideas) == (True, True)
    assert not search.xling_cover("meeting_minutes.txt", ideas)[0]   # one idea is not enough
    assert search.xling_ideas("10월") == []


def test_english_question_korean_name():
    assert search.xling_cover("회의록_11월.txt", search.xling_ideas("november meeting minutes")) == (True, True)
    assert search.xling_cover("고객명단.xlsx", search.xling_ideas("customer list")) == (True, True)
    assert search.xling_cover("customer_list_old.csv", search.xling_ideas("customer list")) == (True, False)   # same script
    assert search.xling_ideas("the readme") == []   # one idea


def test_off_switches(monkeypatch):
    monkeypatch.setenv("XLING_NAMES", "0")
    assert search.xling_ideas("직원 핸드북") == [["직원"], ["핸드북"]]
    monkeypatch.setenv("XLING_EN", "0")
    assert search.xling_ideas("customer list") == []


def _finder(tmp_path, names):
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);")
    for fid, (nm, tier) in enumerate(names, 1):
        con.execute("INSERT INTO files VALUES(?,?,?,?,1,?,?)", (fid, "/d/" + nm, nm, nm.rsplit(".", 1)[1], fid, tier))
        con.execute("INSERT INTO chunks VALUES(?,?,0,'')", (fid * 10, fid))
    f = search.Finder.__new__(search.Finder); f.con = con; f.name_rows = None; f.card_txt = []
    return f


def test_name_lane(tmp_path):
    """the first stage brings in names that carry two ideas or more, one by translation: most ideas first; one idea, a
    same-script name or a library file does not come in"""
    f = _finder(tmp_path, [("onboardingChecklist.docx", 1), ("checklist_offboarding.docx", 1), ("신입_온보딩_체크리스트.hwp", 1),
                           ("onboarding_checklist_lib.py", 2), ("new_hire_onboarding_checklist.pdf", 1)])
    lane = f.xling_lane(search.xling_ideas("신입 온보딩 체크리스트"))
    assert lane[:2] == [10, 50] and 30 not in lane and 40 not in lane and 20 not in lane
    assert f.xling_lane([["직원", "employee"]]) == []   # one idea


def test_boost_counts_translated_ideas_only_when_asked():
    ideas = search.xling_ideas("직원 핸드북")
    assert search.xling_translated("employee_handbook.pdf", ideas) == 2
    assert search.xling_translated("handbook.pdf", ideas) == 1
    assert search.xling_translated("handbook.pdf", ideas, strong=True) == 0   # one idea: not a name of the question
    assert search.xling_translated("직원_핸드북.hwp", ideas) == 0   # the question's own words
