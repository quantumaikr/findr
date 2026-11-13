"""열기 / 폴더 in a browser on this PC (#66 item 9): only for a local browser, never through a tunnel, only indexed files"""
import os
import subprocess
import sys
import types

import search


def _h(addr, **headers):
    return types.SimpleNamespace(client_address=(addr, 50000), headers=headers)


def test_local_browser_only():
    assert search._local_request(_h("127.0.0.1"))
    assert search._local_request(_h("::1"))
    assert not search._local_request(_h("192.168.0.7"))
    for hdr in ("CF-Connecting-IP", "X-Forwarded-For", "Forwarded", "X-Real-IP"):   # a tunnel arrives from 127.0.0.1 too
        assert not search._local_request(_h("127.0.0.1", **{hdr: "203.0.113.5"})), hdr


def _fresh(monkeypatch):
    monkeypatch.setattr(search, "FAILS", {})
    monkeypatch.setattr(search, "TUNNEL_FAILS", [0, 0.0])


def test_forged_headers_cannot_dodge_the_tunnel_limit(monkeypatch):
    """a tunnel visitor forging a new header value on every try gets a fresh per-client count each time, but every
    wrong try through the tunnel counts toward one shared limit (#177)"""
    _fresh(monkeypatch)
    for hdr in search.FWD_HEADERS:
        assert not search._fail_blocked(_h("127.0.0.1", **{hdr: "198.51.100.9"}), now=1000), hdr
    for i in range(search.FAIL_TUNNEL):
        h = _h("127.0.0.1", **{"CF-Connecting-IP": f"198.51.100.{i % 250}-{i}"})
        assert not search._fail_blocked(h, now=1000 + i)
        search._fail_note(h, now=1000 + i)
    assert search._fail_blocked(_h("127.0.0.1", **{"X-Forwarded-For": "203.0.113.77"}), now=1200)
    assert not search._fail_blocked(_h("127.0.0.1", **{"X-Forwarded-For": "203.0.113.77"}), now=1000 + 99 + 601)


def test_outsiders_cannot_lock_out_this_pc_or_each_other(monkeypatch):
    """the page listens on 127.0.0.1, so every tunnel visitor shares one peer address: ten wrong tries from one of
    them must not lock out another visitor, nor the browser, CLI or app on this PC itself"""
    _fresh(monkeypatch)
    bad = _h("127.0.0.1", **{"CF-Connecting-IP": "198.51.100.9"})
    for _ in range(search.FAIL_PER_CLIENT):
        search._fail_note(bad, now=1000)
    assert search._fail_blocked(bad, now=1001)
    assert not search._fail_blocked(_h("127.0.0.1", **{"CF-Connecting-IP": "203.0.113.5"}), now=1001)
    assert not search._fail_blocked(_h("127.0.0.1"), now=1001)
    monkeypatch.setattr(search, "TUNNEL_FAILS", [search.FAIL_TUNNEL, 1000.0])   # the tunnel as a whole is held
    assert not search._fail_blocked(_h("127.0.0.1"), now=1001)
    assert not search._fail_blocked(_h("::1"), now=1001)


def test_a_local_client_still_has_its_own_ten_tries(monkeypatch):
    _fresh(monkeypatch)
    for _ in range(search.FAIL_PER_CLIENT):
        search._fail_note(_h("127.0.0.1"), now=1000)
    assert search._fail_blocked(_h("127.0.0.1"), now=1001)
    assert not search._fail_blocked(_h("127.0.0.1"), now=1000 + 601)
    assert search.TUNNEL_FAILS[0] == 0   # local tries never count against the tunnel


def test_forged_keys_do_not_grow_without_bound(monkeypatch):
    _fresh(monkeypatch)
    for i in range(1500):
        search._fail_note(_h("127.0.0.1", **{"X-Real-IP": f"k{i}"}), now=1000)
    search._fail_note(_h("127.0.0.1", **{"X-Real-IP": "late"}), now=1000 + 601)
    assert len(search.FAILS) < 1100


def test_open_and_reveal_use_the_system(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    if os.name == "nt":
        monkeypatch.setattr(os, "startfile", lambda p: calls.append(["startfile", p]), raising=False)
    f = tmp_path / "보고서.txt"; f.write_text("x", encoding="utf-8")
    search.open_on_pc(f.as_posix(), reveal=False)
    search.open_on_pc(f.as_posix(), reveal=True)
    if os.name == "nt":
        assert calls[0] == ["startfile", str(f)] and calls[1] == ["explorer", f"/select,{f}"]
    elif sys.platform == "darwin":
        assert calls == [["open", str(f)], ["open", "-R", str(f)]]
    else:
        assert calls == [["xdg-open", str(f)], ["xdg-open", str(f.parent)]]


def test_page_shows_buttons_for_local_browser():
    page = (search.Path(search.__file__).with_name("page.html")).read_text(encoding="utf-8")
    assert "fetch('/canopen')" in page and "(APP||LOCAL?'<button data-open=" in page and "fetch('/open'" in page
