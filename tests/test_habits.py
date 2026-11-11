"""habits (context priority + learning with use) as one capped bonus: the search decides, habits settle near-ties only.
No model and no engine here: results are plain rows with odds"""
import datetime as dt
import json
import sqlite3

import habits as H
import search

NOW = dt.datetime(2026, 11, 10, 15, 30)
DAY = 86400


def row(name, prob, folder="D:/work/a", date="2025-01-01 10:00"):
    return {"path": f"{folder}/{name}", "name": name, "ce": prob, "date": date, "why": []}   # prob: the reranker score here


def names(res):
    return [c["name"] for c in res["results"]]


def opened(h, q, path, n, days_ago=0, age=400):
    t = NOW.timestamp() - days_ago * DAY
    stem, ext = path.rsplit(".", 1)
    for i in range(n):   # n files of that folder and format (one file within the hour counts once)
        h.record("open", q, f"{stem}_{i}.{ext}", mtime=t - age * DAY, now=t)


def test_cap_odds_further_apart_never_change_places():
    h = H.Habits()
    opened(h, "발표 자료", "D:/work/deck/x.pptx", 10, age=1)      # every part at its most for the second file
    new = NOW.strftime("%Y-%m-%d 09:00")
    far = {"results": [row("notes.json", 0.59), row("talk.pptx", 0.50, "D:/work/deck", new)]}
    parts = h.bonuses("발표 자료", far["results"], NOW)[1]
    assert abs(parts["format"] - 0.04) < 1e-9 and abs(parts["folder"] - 0.03) < 1e-9 and abs(parts["recent"] - 0.02) < 1e-9
    assert names(h.apply("발표 자료", far, NOW)) == ["notes.json", "talk.pptx"]          # 0.09 apart, bonus cut at 0.08
    near = {"results": [row("notes.json", 0.55), row("talk.pptx", 0.50, "D:/work/deck", new)]}
    assert names(h.apply("발표 자료", near, NOW)) == ["talk.pptx", "notes.json"]         # 0.05 apart: can turn
    for a in range(0, 100, 7):                                                            # any pair: never past the cap
        for b in range(0, a - 8):
            r = {"results": [row("a.json", a / 100), row("b.pptx", b / 100, "D:/work/deck", new)]}
            assert names(h.apply("발표 자료", r, NOW)) == ["a.json", "b.pptx"], (a, b)


def test_day_one_common_sense_without_any_record():
    h = H.Habits()
    res = h.apply("발표 자료", {"results": [row("talk.json", 0.52), row("talk.pptx", 0.50), row("old.txt", 0.30)]}, NOW)
    assert names(res) == ["talk.pptx", "talk.json", "old.txt"]
    assert res["results"][0]["why"] == ["찾는 종류에 맞는 형식"] and res["results"][1]["why"] == []
    # no purpose word: nothing applies
    plain = {"results": [row("talk.json", 0.52), row("talk.pptx", 0.50)]}
    assert names(h.apply("지난주에 받은 자료", plain, NOW)) == ["talk.json", "talk.pptx"]


def test_purpose_kinds_and_format_groups():
    assert H.intent("3분기 발표 자료") == "present" and H.intent("budget for the trip") == "table"
    assert H.intent("용역 계약서") == "paper" and H.intent("워크숍에서 찍은 사진") == "photo"
    assert H.intent("보고서 발표 자료") == "paper"          # words of two kinds: the one said first
    assert H.intent("my playlist") is None and H.intent("어제 받은 파일") is None
    assert [H.group_of(x) for x in ("a.PPTX", ".csv", "hwp", "D:\\x\\y.pdf", "p.heic", "n.md", "x.json", "noext")] == \
        ["slides", "sheet", "doc", "pdf", "image", "text", "other", "other"]
    assert H.folder_of("C:\\a\\b\\c.txt") == H.folder_of("C:/a/b/c.txt") == "C:/a/b"


def test_under_min_obs_nothing_is_learned_and_from_five_it_moves_to_pdf():
    h = H.Habits()
    two = lambda: {"results": [row("talk.pptx", 0.50, "D:/x"), row("talk.pdf", 0.50, "D:/y")]}
    assert names(h.apply("발표 자료", two(), NOW)) == ["talk.pptx", "talk.pdf"]          # day one: slides 1.0, pdf 0.5
    opened(h, "발표 자료", "D:/z1/a.pdf", 2); opened(h, "발표 자료", "D:/z2/a.pdf", 2)   # four PDFs: under MIN_OBS
    assert h.fit("present", NOW) == {"slides": 1.0, "pdf": 0.5}
    assert names(h.apply("발표 자료", two(), NOW)) == ["talk.pptx", "talk.pdf"]
    opened(h, "발표 자료", "D:/z3/a.pdf", 2)                                             # six: (6 + 4*0.5)/10 and (0 + 4*1)/10
    fit = h.fit("present", NOW)
    assert abs(fit["pdf"] - 0.8) < 1e-9 and abs(fit["slides"] - 0.4) < 1e-9
    assert names(h.apply("발표 자료", two(), NOW)) == ["talk.pdf", "talk.pptx"]
    assert h.fit("table", NOW) == {"sheet": 1.0, "pdf": 0.4}                              # another purpose: untouched


def test_decay_a_year_old_habit_hardly_counts():
    h = H.Habits()
    opened(h, "발표 자료", "D:/z/a.pdf", 6, days_ago=365)
    assert h.tally(NOW)["n"] < 0.4                           # 6 x 0.5 ** (365 / 90) = 0.36
    assert h.fit("present", NOW) == {"slides": 1.0, "pdf": 0.5}
    opened(h, "발표 자료", "D:/z/a.pdf", 6, days_ago=90)     # one half-life: 3.0 more, still under five
    assert 3.3 < h.tally(NOW)["n"] < 3.4 and h.fit("present", NOW)["pdf"] == 0.5


def test_the_clock_does_not_move_the_answer_within_a_day():
    h = H.Habits()
    opened(h, "발표 자료", "D:/z/a.pdf", 7, days_ago=40)
    morning, night = NOW.replace(hour=0, minute=5), NOW.replace(hour=23, minute=55)
    assert h.fit("present", morning) == h.fit("present", night)
    assert h.tally(morning)["n"] == h.tally(night)["n"] != h.tally(NOW + dt.timedelta(days=1))["n"]


def test_folder_opened_often():
    h = H.Habits()
    opened(h, "아무 자료", "D:/work/team/x.txt", 4)
    two = lambda: {"results": [row("a.txt", 0.50, "D:/else"), row("b.txt", 0.48, "D:/work/team"), row("c.txt", 0.47, "D:/work")]}
    assert names(h.apply("아무 자료", two(), NOW)) == ["a.txt", "b.txt", "c.txt"]        # four opened files: not yet
    opened(h, "아무 자료", "D:\\work\\team\\y.txt", 2); opened(h, "아무 자료", "D:/else/z.txt", 3)
    b = h.bonuses("아무 자료", two()["results"], NOW)
    assert abs(b[1]["folder"] - 0.03) < 1e-9 and abs(b[0]["folder"] - 0.015) < 1e-9 and b[2]["folder"] == 0   # the folder itself, not its parent
    res = h.apply("아무 자료", {"results": [row("a.txt", 0.50, "D:/else"), row("b.txt", 0.49, "D:/work/team")]}, NOW)
    assert names(res) == ["b.txt", "a.txt"] and res["results"][0]["why"] == ["자주 여는 폴더"]


def test_liking_for_recent_files():
    h = H.Habits()
    new, old = (NOW - dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M"), "2024-03-01 10:00"
    two = lambda: {"results": [row("a.txt", 0.500, "D:/p", old), row("b.txt", 0.495, "D:/q", new)]}
    opened(h, "아무 자료", "D:/r1/x.txt", 7, age=2)
    assert h.recent_share(NOW) is None and names(h.apply("아무 자료", two(), NOW)) == ["a.txt", "b.txt"]   # seven: not yet
    opened(h, "아무 자료", "D:/r2/x.txt", 1, age=2); opened(h, "아무 자료", "D:/r3/x.txt", 2, age=300)
    assert abs(h.recent_share(NOW) - 0.8) < 1e-9                                         # 8 of 10 were recent when opened
    b = h.bonuses("아무 자료", two()["results"], NOW)
    assert b[0]["recent"] == 0 and abs(b[1]["recent"] - 0.01) < 1e-9                     # 0.02 x (0.8 - 0.6) / 0.4
    res = h.apply("아무 자료", two(), NOW)
    assert names(res) == ["b.txt", "a.txt"] and res["results"][0]["why"] == ["최근 파일을 주로 여심"]
    h2 = H.Habits(); opened(h2, "아무 자료", "D:/r1/x.txt", 5, age=2); opened(h2, "아무 자료", "D:/r2/x.txt", 5, age=300)
    assert h2.bonuses("아무 자료", two()["results"], NOW)[1]["recent"] == 0              # half and half: no liking


def test_nothing_changes_without_odds_or_without_a_bonus():
    h = H.Habits()
    res = {"results": [row("talk.json", 0.52), dict(row("talk.pptx", 0.50), ce=None)]}
    before = json.dumps(res, ensure_ascii=False)
    assert h.apply("발표 자료", res, NOW) is res and json.dumps(res, ensure_ascii=False) == before
    del res["results"][1]["ce"]; before = json.dumps(res, ensure_ascii=False)
    assert json.dumps(h.apply("발표 자료", res, NOW), ensure_ascii=False) == before
    # a list the earlier stages ordered against the odds (a name match first) is not re-sorted when no bonus applies
    odd = {"results": [row("a.json", 0.30), row("b.json", 0.70), row("c.json", 0.50)]}
    before = json.dumps(odd, ensure_ascii=False)
    assert json.dumps(h.apply("발표 자료", odd, NOW), ensure_ascii=False) == before
    assert h.apply("발표 자료", {"results": []}, NOW) == {"results": []}


def test_ties_keep_their_order():
    h = H.Habits()
    res = h.apply("발표 자료", {"results": [row("a.pptx", 0.5), row("b.pptx", 0.5), row("c.json", 0.53), row("d.pptx", 0.5)]}, NOW)
    assert names(res) == ["a.pptx", "b.pptx", "d.pptx", "c.json"]


def test_stored_lines_hold_no_question_and_no_file_name(tmp_path):
    p = tmp_path / "habits.jsonl"
    h = H.Habits(str(p))
    r = h.record("open", "하늘빛상사 발표 자료", "D:/work/team/하늘빛상사_제안발표_v3.pptx", mtime=NOW.timestamp() - 10 * DAY,
                 ext=".pptx", from_versions=True, now=NOW)
    h.record("copy", "어제 받은 것", "D:\\work\\etc\\메모.txt", now=NOW)
    assert h.record("none", "q", "D:/x.txt") is None and h.record("open", "q", "") is None
    text = p.read_text(encoding="utf-8")
    lines = [json.loads(x) for x in text.splitlines()]
    assert len(lines) == 2 and set(lines[0]) == {"t", "kind", "intent", "group", "folder", "age_days", "from_versions"}
    assert lines[0] == r == {"t": int(NOW.timestamp()), "kind": "open", "intent": "present", "group": "slides", "folder": "D:/work/team",
                             "age_days": 10.0, "from_versions": True}
    assert lines[1]["intent"] is None and lines[1]["group"] == "text" and lines[1]["age_days"] is None and lines[1]["folder"] == "D:/work/etc"
    for secret in ("하늘빛상사", "제안발표", "발표 자료", "메모", "어제", ".pptx", ".txt"):
        assert secret not in text, secret
    again = H.Habits(str(p))                                 # the next start reads the same counts
    assert again.state()[0] == 2 and again.tally(NOW)["n"] == h.tally(NOW)["n"] and again.summary(NOW)["versions"] == 1.0


def test_forget_empties_the_file(tmp_path):
    p = tmp_path / "habits.jsonl"
    h = H.Habits(str(p)); opened(h, "발표 자료", "D:/z/a.pdf", 6)
    s0 = h.state(); assert h.fit("present", NOW)["pdf"] > 0.5
    h.forget()
    assert p.read_text(encoding="utf-8") == "" and h.rows == [] and h.state() != s0
    assert h.fit("present", NOW) == {"slides": 1.0, "pdf": 0.5} and H.Habits(str(p)).rows == []


def test_a_file_over_the_limit_keeps_the_recent_half(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "MAX_BYTES", 2000)
    p = tmp_path / "habits.jsonl"; h = H.Habits(str(p)); seen = set()
    for i in range(40):
        h.record("open", "발표 자료", f"D:/f{i}/a.pdf", now=NOW.timestamp() + i)
        assert h.state() not in seen; seen.add(h.state())    # the cache key never returns to an earlier state
    assert p.stat().st_size <= 2000 + 200 and len(h.rows) < 40
    assert h.rows[-1]["folder"] == "D:/f39" and len(p.read_text(encoding="utf-8").splitlines()) == len(h.rows)


def test_summary_for_the_learned_page():
    h = H.Habits()
    opened(h, "발표 자료", "D:/a/x.pdf", 3); opened(h, "발표 자료", "D:/b/x.pptx", 1); opened(h, "예산 표", "D:/a/y.xlsx", 2)
    s = h.summary(NOW)
    assert s["n"] == 6 and s["formats"]["present"] == [("pdf", 3.0), ("slides", 1.0)] and s["formats"]["table"] == [("sheet", 2.0)]
    assert s["folders"] == [("D:/a", 5.0), ("D:/b", 1.0)] and s["recent"] is None


def test_engine_store_and_cache_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); monkeypatch.setattr(search, "personal", lambda: None)
    f = type("F", (), {"con": sqlite3.connect(tmp_path / "i.db")})()
    monkeypatch.setenv("HABITS", "0")
    off = search.result_key(f, "발표 자료", "xce", 10, None, None)
    search.habits().record("open", "발표 자료", "D:/z/a.pdf")
    assert (tmp_path / "habits.jsonl").exists()
    assert off == search.result_key(f, "발표 자료", "xce", 10, None, None)               # not used for the order: the answer stands
    monkeypatch.setenv("HABITS", "1")
    k1 = search.result_key(f, "발표 자료", "xce", 10, None, None)
    assert k1 != off and k1 == search.result_key(f, "발표 자료", "xce", 10, None, None)
    search.habits().record("open", "발표 자료", "D:/z/b.pdf")                            # a file opened since: computed again
    k2 = search.result_key(f, "발표 자료", "xce", 10, None, None)
    search.habits().forget()
    assert len({k1, k2, search.result_key(f, "발표 자료", "xce", 10, None, None)}) == 3


def test_why_line_follows_the_language(monkeypatch):
    h = H.Habits()
    monkeypatch.setattr(search._LANG, "v", "en", raising=False)
    res = h.apply("presentation for the board", {"results": [row("talk.json", 0.52), row("talk.pptx", 0.50)]}, NOW)
    assert res["results"][0]["why"] == ["The format that fits what you asked for"]


def test_index_path_forms():
    assert search.index_path_forms("C:\\a\\b.txt") == ("C:\\a\\b.txt", "C:/a/b.txt", "/mnt/c/a/b.txt")
    assert search.index_path_forms("/home/u/b.txt") == ("/home/u/b.txt",) * 3


def test_page_reports_what_was_opened():
    page = (search.Path(search.__file__).with_name("page.html")).read_text(encoding="utf-8")
    for s in ("fb(rv?'reveal':'open',x.path", "fb('copy',x.path", "fb('open',vp,", "ver:'1'"):
        assert s in page, s


def test_one_file_acted_on_twice_is_one_observation(tmp_path):
    """opened and then marked right: counted once. A second file in the same folder, or the same file an hour later, counts"""
    h = H.Habits(str(tmp_path / "habits.jsonl"))
    t0 = 1_800_000_000
    assert h.record("open", "발표 자료", "C:" + chr(92) + "w" + chr(92) + "a.pptx", now=t0)
    assert h.record("hit", "발표 자료", "C:/w/a.pptx", now=t0 + 60) is None
    assert h.record("open", "발표 자료", "C:/w/b.pptx", now=t0 + 60)
    assert h.record("open", "발표 자료", "C:/w/a.pptx", now=t0 + 3601)
    assert len(h.rows) == 3
    assert "a.pptx" not in (tmp_path / "habits.jsonl").read_text(encoding="utf-8")


def test_a_rule_placed_result_and_a_remembered_first_are_not_passed():
    """a result an earlier stage put above a higher score stays there; so does a #1 the remembered history confirmed"""
    h = H.Habits()
    ruled = {"results": [row("named.json", 0.40), row("talk.pptx", 0.60), row("x.json", 0.61)]}
    assert names(h.apply("발표 자료", ruled, NOW)) == ["named.json", "talk.pptx", "x.json"]
    held = {"results": [row("talk.json", 0.52), row("talk.pptx", 0.50)], "episodic_ok": True}
    assert names(h.apply("발표 자료", held, NOW)) == ["talk.json", "talk.pptx"]
    free = {"results": [row("talk.json", 0.52), row("talk.pptx", 0.50)]}
    res = h.apply("발표 자료", free, NOW)
    assert names(res) == ["talk.pptx", "talk.json"] and res["results"][0]["habit"]
