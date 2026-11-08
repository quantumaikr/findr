"""one copy of the meaning model on the PC: the indexer asks the running engine for document vectors (POST /embed) instead of
loading torch and the model itself (1.7 of its 1.75 GB; next to the engine's 2.7 GB the OS killed the engine in the 3 GB
reproduction). No engine, another encoder, or an engine that goes away: the indexer's own copy, as before"""
import base64
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

import enc_eg2
import index
import search


def _db(tmp_path):
    con = sqlite3.connect(tmp_path / "i.db")
    con.executescript("""
      CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT, pending INT DEFAULT 0);
      CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB, emb2 BLOB);
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);""")
    con.executemany("INSERT INTO files VALUES(?,?,?,'.txt',1,?,1,0)", [(i, f"/d/{i}.txt", f"{i}.txt", i) for i in range(5)])
    con.executemany("INSERT INTO chunks(file,n,text) VALUES(?,0,?)", [(i, f"본문 {i}") for i in range(5)])
    con.execute("INSERT INTO meta VALUES('encoder','e5-small')"); con.commit()
    return con


def _engine(have="e5-small", calls=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            r = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if calls is not None:
                calls.append(len(r["texts"]))
            if r["encoder"] != have:
                body, code = b'{"ok": false, "why": "encoder"}', 409
            else:
                v = np.ones((len(r["texts"]), 4), np.float16)
                body, code = json.dumps({"ok": True, "dim": 4, "vecs": base64.b64encode(v.tobytes()).decode()}).encode(), 200
            self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _env(monkeypatch, tmp_path, srv):
    (tmp_path / ".password").write_text("pw")
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); monkeypatch.setenv("FINDR_APP_PORT", str(srv.server_port))


def test_indexer_uses_the_engine_and_loads_no_model(tmp_path, monkeypatch):
    calls = []
    srv = _engine(calls=calls)
    _env(monkeypatch, tmp_path, srv)
    monkeypatch.setattr(enc_eg2, "make_encoder", lambda *a, **k: (_ for _ in ()).throw(AssertionError("loaded its own model")))
    con = _db(tmp_path)
    try:
        index.embed(con, "e5-small")
    finally:
        srv.shutdown()
    assert con.execute("SELECT count(*) FROM chunks WHERE emb IS NOT NULL").fetchone()[0] == 5 and sum(calls) >= 5


def test_other_encoder_or_local_setting_keeps_its_own_copy(tmp_path, monkeypatch):
    srv = _engine(have="embeddinggemma-2")   # the engine holds another encoder: its vectors would not fit this index
    _env(monkeypatch, tmp_path, srv)
    used = []

    class Own:
        st = True

        def emb(self, texts, query=False):
            import torch
            used.append(len(texts)); return torch.zeros(len(texts), 4)
    monkeypatch.setattr(enc_eg2, "make_encoder", lambda *a, **k: Own())
    monkeypatch.setattr(enc_eg2, "EG2", Own)
    con = _db(tmp_path)
    try:
        index.embed(con, "e5-small")
    finally:
        srv.shutdown()
    assert used and con.execute("SELECT count(*) FROM chunks WHERE emb IS NOT NULL").fetchone()[0] == 5
    monkeypatch.setenv("FINDR_EMBED_VIA", "local")
    assert index.EngineEncoder.connect("e5-small") is None


def test_engine_serves_document_vectors_at_512_tokens():
    src = Path(search.__file__).read_text(encoding="utf-8")
    assert 'urlparse(self.path).path == "/embed"' in src and "enc.st.max_seq_length = 512" in src
    assert "enc.st.max_seq_length = old" in src   # the questions get their 128 back
