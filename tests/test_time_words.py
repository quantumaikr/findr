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


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("TIME_WORDS", raising=False)
    assert search.parse("작년에 만든 발표자료", now=NOW)["since"] is None
