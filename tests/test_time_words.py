"""calendar years as people say them: 작년 / 지난해 / 재작년 / 올해 (TIME_WORDS=1). On the real PCs (11/9) "작년에 만든
발표자료" read no period at all, and one PC with no files from last year showed other years without a word."""
import datetime as dt

import search

NOW = dt.datetime(2026, 11, 9, 1, 0)


def test_years(monkeypatch):
    monkeypatch.setenv("TIME_WORDS", "1")
    p = search.parse("작년에 만든 발표자료", now=NOW)
    assert (p["since"], p["until"]) == (dt.datetime(2025, 1, 1), dt.datetime(2026, 1, 1))
    assert "작년" not in p["text"]
    assert search.parse("지난해 계약서", now=NOW)["since"] == dt.datetime(2025, 1, 1)
    p = search.parse("재작년 회의록", now=NOW)
    assert (p["since"], p["until"]) == (dt.datetime(2024, 1, 1), dt.datetime(2025, 1, 1))   # not read as 작년
    p = search.parse("올해 견적서", now=NOW)
    assert (p["since"], p["until"]) == (dt.datetime(2026, 1, 1), None)


def test_on_by_default_and_can_be_turned_off(monkeypatch):
    """on since the real-PC sealed sets (11/9: three PCs, none worse); TIME_WORDS=0 is the way back"""
    monkeypatch.delenv("TIME_WORDS", raising=False)
    assert search.parse("작년에 만든 발표자료", now=NOW)["since"] == dt.datetime(2025, 1, 1)
    monkeypatch.setenv("TIME_WORDS", "0")
    assert search.parse("작년에 만든 발표자료", now=NOW)["since"] is None


def test_period_holds_at_the_end_and_shows_its_years(monkeypatch):
    """Mac Air (11/9): with 작년 read, this month's code files holding the word came first (a later stage looked for the word
    across the whole index), and the period showed as '01-01~01-01'"""
    monkeypatch.setenv("TIME_WORDS", "1")
    monkeypatch.setattr(search, "today_now", lambda: NOW)
    rs = [{"name": "a.py", "date": "2026-11-08 10:00"}, {"name": "발표.pptx", "date": "2025-06-11 09:00"},
          {"name": "b.py", "date": "2026-11-07 10:00"}, {"name": "발표2.pdf", "date": "2025-02-01 09:00"}]
    out = search.period_first("작년에 만든 발표자료", rs)
    assert [x["name"] for x in out] == ["발표.pptx", "발표2.pdf", "a.py", "b.py"]   # inside the period first, the rest kept
    assert search.show(search.parse("작년에 만든 발표자료"))["기간"] == "2025-01-01~2026-01-01"
    assert search.period_first("발표자료", rs) == rs                                   # no period: nothing moves


def test_relative_years_are_soft(monkeypatch):
    """작년 / 재작년 may be misremembered (a file three years old asked as 작년): marked soft, so an empty period falls back"""
    monkeypatch.setenv("TIME_WORDS", "1")
    assert search.parse("작년 공모전 발표 자료", now=NOW)["soft_time"] is True
    assert search.parse("재작년 회의록", now=NOW)["soft_time"] is True
    assert search.parse("3개월 전 회의록", now=NOW)["soft_time"] is False    # other periods keep their filter as before
    src = open(search.__file__, encoding="utf-8").read()
    assert 'if p.get("soft_time") and not r["results"]:' in src and 'r["time_miss"] = True' in src


def test_period_candidates_come_back_at_the_end():
    """Mac Air (11/9): base found ten files of 작년, the full route ended with none (later stages replaced them)"""
    src = open(search.__file__, encoding="utf-8").read()
    assert "pre_period = list(r[\"results\"])" in src and "back_ = [x for x in pre_period if fit(x)" in src
