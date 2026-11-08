"""findr ask asks the running engine of this data folder (#66 #67 #68: ask took 14-29 s and ranked differently from the page);
an engine of another data folder (wrong password) or none at all: ask loads the models itself, as before"""
import json
import socket
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

from findr import cli, mcp_server, paths


def _engine(status):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"results": [{"name": "견적서.xlsx", "path": "C:/문서/견적서.xlsx", "date": "2026-11-01", "prob": 0.9}],
                               "note": ""}).encode()
            self.send_response(status); self.end_headers()
            if status == 200:
                self.wfile.write(body)

        def log_message(self, *a):
            pass
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def _setup(monkeypatch, tmp_path, port):
    monkeypatch.setattr(paths, "HOME", tmp_path); monkeypatch.setattr(paths, "DB", tmp_path / "index.db")
    (tmp_path / ".password").write_text("pw", encoding="utf-8"); (tmp_path / "index.db").write_text("")
    monkeypatch.setattr(mcp_server, "ENGINE", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(mcp_server, "_DOWN", [0.0])


def test_ask_uses_the_running_engine(monkeypatch, tmp_path, capsys):
    s = _engine(200)
    try:
        _setup(monkeypatch, tmp_path, s.server_address[1])
        monkeypatch.setattr(cli, "_engine_env", lambda *a: (_ for _ in ()).throw(AssertionError("loaded the models")))
        cli.cmd_ask(types.SimpleNamespace(query=["견적서"], k=5, local=False, db=paths.DB))
        out = capsys.readouterr().out
        assert "견적서.xlsx" in out and "실행 중인 검색 엔진" in out
    finally:
        s.shutdown()


def test_engine_of_another_data_folder_is_not_used(monkeypatch, tmp_path):
    s = _engine(401)
    try:
        _setup(monkeypatch, tmp_path, s.server_address[1])
        assert cli._ask_engine("견적서", 5) is None
    finally:
        s.shutdown()


def test_no_engine_returns_quickly(monkeypatch, tmp_path):
    sk = socket.socket(); sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]; sk.close()
    _setup(monkeypatch, tmp_path, port)
    import time
    t = time.time()
    assert cli._ask_engine("견적서", 5) is None and time.time() - t < 1.0
