"""with the app closed, search_files goes to the index at once. On Windows a refused localhost connection takes ~2 s, and
every call paid it before the index search"""
import socket
import time
import urllib.request

import pytest

from findr import mcp_server


def _closed_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def test_engine_down_is_seen_quickly_and_remembered(monkeypatch):
    monkeypatch.setattr(mcp_server, "ENGINE", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(mcp_server, "_DOWN", [0.0])
    t = time.time()
    assert mcp_server._engine_search("회의록", 5) is None
    assert time.time() - t < 1.0
    calls = []
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("looked again within 15 s"))
    assert mcp_server._engine_search("회의록", 5) is None and calls == []


def test_engine_up_is_asked(monkeypatch):
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    monkeypatch.setattr(mcp_server, "ENGINE", f"http://127.0.0.1:{srv.getsockname()[1]}")
    monkeypatch.setattr(mcp_server, "_DOWN", [0.0])
    try:
        assert mcp_server._engine_up() is True
    finally:
        srv.close()
