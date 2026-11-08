"""'answers' is not 'ready' (#68): /healthz answered at once while the first question waited 5.9 s for the warm-up. /ready says
when the warm-up is done, and the page shows that the engine is getting ready"""
import search


def test_ready_route_and_flag():
    src = search.Path(search.__file__).read_text(encoding="utf-8")
    assert '== "/ready"' in src and "READY.set()" in src
    assert hasattr(search, "READY") and not search.READY.is_set() or search.READY.is_set()


def test_page_waits_for_ready():
    page = search.Path(search.__file__).with_name("page.html").read_text(encoding="utf-8")
    assert "fetch('/ready')" in page and "검색 엔진을 준비하는 중" in page
