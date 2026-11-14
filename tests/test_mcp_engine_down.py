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


def _healthz(body):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = body.encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


@pytest.mark.parametrize("body", ['{"ok": true, "loaded": true, "ready": false}', "ok"])   # an older engine: 'ok' only
def test_engine_up_is_asked(monkeypatch, body):
    srv = _healthz(body)
    monkeypatch.setattr(mcp_server, "ENGINE", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(mcp_server, "_DOWN", [0.0])
    try:
        assert mcp_server._engine_up() is True
    finally:
        srv.shutdown(); srv.server_close()


def test_an_engine_still_loading_is_taken_as_down_briefly(monkeypatch):
    """review of #198: the port opens before the index and models are in, and /api then holds the call until they are (up
    to 10 minutes); the MCP server reads /healthz?json=1 and searches the index meanwhile"""
    srv = _healthz('{"ok": true, "loaded": false, "ready": false}')
    monkeypatch.setattr(mcp_server, "ENGINE", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(mcp_server, "_DOWN", [0.0])
    try:
        t = time.time()
        assert mcp_server._engine_up() is False and time.time() - t < 3
        assert 0 < mcp_server._DOWN[0] - time.time() <= mcp_server.LOADING_RETRY
        calls = []
        monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: calls.append(1))
        assert mcp_server._engine_search("회의록", 5) is None and calls == []
    finally:
        srv.shutdown(); srv.server_close()
