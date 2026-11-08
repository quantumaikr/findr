"""tools/field_check.py: one set of numbers from every field PC, and none of the PC's file names in them"""
import importlib.util
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TOOL = Path(__file__).resolve().parents[1] / "tools" / "field_check.py"


def load():
    spec = importlib.util.spec_from_file_location("field_check", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            body = b"ok"
        elif u.path == "/ready":
            body = b'{"ready": true}'
        elif u.path == "/progress":
            body = b"{}"
        elif u.path == "/stats":
            body = b'{"files": 40, "rss_mb": 900, "peak_mb": 1200}'
        else:
            k = int(parse_qs(u.query)["k"][0])
            res = [{"name": f"secret_{i}.pdf", "path": f"/home/x/secret_{i}.pdf", "prob": 0.9} for i in range(k)]
            body = json.dumps({"results": res, "route": "xce", "ms": 5}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


def test_counts_and_times_only(tmp_path, monkeypatch):
    con = sqlite3.connect(tmp_path / "index.db")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, name TEXT); CREATE TABLE chunks(id INTEGER PRIMARY KEY);")
    con.executemany("INSERT INTO files(name) VALUES (?)", [(f"f{i}",) for i in range(40)]); con.commit(); con.close()
    (tmp_path / ".password").write_text("pw")
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("FINDR_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["field_check", "--port", str(srv.server_port), "--rounds", "1"])
    try:
        code = load().main()
    finally:
        srv.shutdown()
    out = (tmp_path / "field_check.json").read_text(encoding="utf-8")
    rep = json.loads(out)
    assert "secret" not in out and "/home/x" not in out   # the PC's file names stay on the PC
    assert rep["index"] == {"files": 40, "pieces": 0}
    assert rep["engine"]["memory"] == {"rss_mb": 900, "peak_mb": 1200}   # the engine's own memory, now and at its peak
    assert rep["summary"]["short_lists"] == 0 and rep["summary"]["k30_count"] == 30
    assert rep["checks"]["엔진 준비"] and rep["checks"]["결과 개수(k=10)"]
    assert not rep["checks"]["없는 파일 질문"] and code == 1   # the fake answers everything with 90 %: flagged


def test_engine_down(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["field_check", "--port", "9", "--rounds", "1"])
    assert load().main() == 1
    assert json.loads((tmp_path / "field_check.json").read_text(encoding="utf-8"))["engine"]["health"] is False


def test_engine_reports_its_memory():
    import search
    m = search.proc_memory()
    assert m.get("peak_mb", 0) > 0 and m.get("peak_mb", 0) >= m.get("rss_mb", 0)
