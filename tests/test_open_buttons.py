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
