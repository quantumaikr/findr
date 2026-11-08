"""the condition / plan reader (FINDR_LLM) is a research server the app does not ship. On Windows a refused localhost
connection takes ~2 s, so every understanding-first question waited 2 s for it: not set means no call, and a server that
is down is not tried again for a minute"""
import socket
import time
import urllib.request

import pytest

import search


def test_not_set_means_no_network(monkeypatch):
    monkeypatch.setattr(search, "LLM", "")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("called the network"))
    with pytest.raises(ConnectionError):
        search.chat_json("{}", 10)
    assert search.parse_cond("지난달 견적서") == {}   # callers fall back to their rules


def test_down_server_is_not_retried_for_a_minute(monkeypatch):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()   # a port nobody listens on
    monkeypatch.setattr(search, "LLM", f"http://127.0.0.1:{port}/v1/chat/completions")
    monkeypatch.setattr(search, "_LLM_DOWN", [0.0])
    with pytest.raises(OSError):
        search.chat_json("{}", 10)
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(1))
    t = time.time()
    with pytest.raises(ConnectionError):
        search.chat_json("{}", 10)
    assert calls == [] and time.time() - t < 0.05


def test_installed_default_is_off():
    import importlib, os
    if os.environ.get("FINDR_LLM"):
        pytest.skip("FINDR_LLM set in this environment")
    assert importlib.reload(search).LLM == ""
