"""the search page's basic uses (review of 0.2.14): feedback goes to the question that made the list, only the newest answer
is shown, a Hangul Enter is one search, folder filters do not take neighbours, learning does not stop at 5 MB, long paths are
learned, a reload does not forget another entry, 이해 우선 turns on once facts arrive, a moved file says so, a silent checker
is not reported as a second look"""
import time
import types
from pathlib import Path

import search

CORE = Path(search.__file__).parent
PAGE = (CORE / "page.html").read_text(encoding="utf-8")
SRC = (CORE / "search.py").read_text(encoding="utf-8")


def test_page_feedback_and_order():
    assert "q:L.q0||q.value,mode:L.route||mode" in PAGE
    assert "const my=++seq;" in PAGE and "if(my!==seq)return;" in PAGE
    assert "if(e.isComposing||e.keyCode===229)return;go()" in PAGE
    assert "dir(x.path)===fDir||dir(x.path).startsWith(fDir+'/')" in PAGE
    assert "toast('기록하지 못했습니다')" in PAGE and "j.why=='FileNotFoundError'" in PAGE


def test_server_feedback_learning():
    assert '[:4096 if k == "path" else MAX_Q]' in SRC and 'fbp.rename(fbp.with_name(f"feedback.' in SRC
    assert 'self.send_header("Location", "/learned")' in SRC
    assert '"skipped": "checker"' in SRC


def test_facts_are_asked_again():
    calls = []

    class Con:
        def execute(self, q):
            calls.append(q)
            return types.SimpleNamespace(fetchone=lambda: (1,) if len(calls) > 1 else None)

    f = types.SimpleNamespace(con=Con())
    assert search._has_facts(f) is False
    assert search._has_facts(f) is False and len(calls) == 1   # not every question
    f._facts_t = time.time() - 301
    assert search._has_facts(f) is True   # the first index brought work facts in: 이해 우선 works without a restart


def test_moved_file_is_reported(tmp_path):
    try:
        search.open_on_pc(str(tmp_path / "옮겨진.pdf"))
    except FileNotFoundError:
        return
    raise AssertionError("a missing file must not be 'opened'")
