"""a redraw after the results (the second look, 열기 turning on, filters, ✘) must not replay the cards' fade-in: the list emptied
for a moment a few seconds after the results (field, Mac). Only a new answer fades in. And the page's server time is the whole
request (서버 3.4 s · 화면 7.1 s left 3.7 s unexplained)"""
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "findr" / "core"


def test_only_a_new_answer_fades_in():
    page = (CORE / "page.html").read_text(encoding="utf-8")
    assert ".card.still{animation:none;opacity:1;transform:none}" in page
    assert "function list(fresh){" in page and "(fresh?'':' still')" in page
    assert " facets(r.results||[]); list(fresh);" in page and "function show(r,fresh=true){" in page   # a new answer fades in
    # the reranked order replacing the quick list (Macs, 11/9) is drawn in place: no fade, no empty moment
    assert "show(r,!pre);" in page
    # the last answer stays (dimmed) while the next one is asked: clearing it left the list empty for a moment
    assert "if($('#res .card')){$('#res').classList.add('dim')}" in page and "$('#res').classList.remove('dim');" in page


def test_server_time_is_the_whole_request():
    src = (CORE / "search.py").read_text(encoding="utf-8")
    assert 'res["search_ms"] = int(res.get("ms") or 0); res["ms"] = max(res["search_ms"], int((time.time() - t_req) * 1000))' in src
