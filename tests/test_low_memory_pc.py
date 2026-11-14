"""8 GB PCs (#198): what findr holds while it indexes and while it waits. `findr index` chooses the model without torch in
its own process; the indexer waits for an engine that is starting instead of loading a second copy, resumes after a
memory wait at a level an 8 GB PC reaches, says it is waiting in progress.json and lets go of its model after each stage;
the engine lets go of the reranker when nobody asks, measures DirectML only when the PC is not busy, and opens its port
before it loads, with its version and start time in /healthz"""
import base64
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import types
import urllib.request
import weakref
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

import enc_eg2
import governor
import gpu_dml
import index
import search
from findr import cli


# --- findr index: the parent does not hold torch -----------------------------------------------------------------------

def test_index_chooses_the_model_in_a_process_of_its_own(tmp_path, monkeypatch, capsys):
    ran, called = [], []

    def run(cmd, **kw):
        ran.append(cmd)
        compile(cmd[-1], "<child>", "exec")   # the child's code is valid Python
        return subprocess.CompletedProcess(cmd, 0, "  모델 받는 중 ...\n" + cli.ENC_LINE + "e5-small\n")
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd, **kw: called.append(cmd) or 0)
    monkeypatch.setattr(cli, "_encoder", lambda: pytest.fail("chosen in the parent: torch stays loaded for the whole run"))
    a = types.SimpleNamespace(paths=[str(tmp_path)], db=tmp_path / "i.db", deep=False, names_only=False, gone_only=False)
    assert cli.cmd_index(a) == 0
    assert ran and ran[0][1] == "-c"
    assert called[0][called[0].index("--model") + 1] == "e5-small"
    assert "모델 받는 중" in capsys.readouterr().out   # what the child said is passed on


def test_index_chooses_here_when_the_child_fails(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "Traceback ...\n"))
    monkeypatch.setattr(cli, "_encoder", lambda: "embeddinggemma-2")
    assert cli._encoder_apart() == "embeddinggemma-2"


# --- the indexer waits for an engine that is starting ------------------------------------------------------------------

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


class _Engine(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        r = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        v = np.ones((len(r["texts"]), 4), np.float16)
        body = json.dumps({"ok": True, "dim": 4, "vecs": base64.b64encode(v.tobytes()).decode()}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


def _starting(tmp_path, monkeypatch, port, pid=None, age=0.0):
    (tmp_path / ".password").write_text("pw")
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); monkeypatch.setenv("FINDR_APP_PORT", str(port))
    monkeypatch.delenv("FINDR_EMBED_WAIT", raising=False); monkeypatch.delenv("FINDR_EMBED_VIA", raising=False)
    (tmp_path / index.STARTING_FILE).write_text(json.dumps({"pid": pid or os.getpid(), "port": port, "t": time.time() - age}))
    monkeypatch.setattr(index, "EMBED_WAIT_STEP", 0.1)


def test_indexer_waits_for_a_starting_engine(tmp_path, monkeypatch, capsys):
    port = _free_port()
    _starting(tmp_path, monkeypatch, port)
    srv, waiting, sleep = [], threading.Event(), time.sleep

    def step(s):   # the indexer has said it waits: only now does the engine come up
        waiting.set(); sleep(s)
    monkeypatch.setattr(index.time, "sleep", step)

    def up_later():
        # not after a fixed time: on Windows a refused localhost connection is retried for ~2 s, so an engine that came up
        # 0.8 s in was reached by the first try, which then never waited (CI, windows-latest)
        waiting.wait(30); sleep(0.2)
        s = HTTPServer(("127.0.0.1", port), _Engine); srv.append(s); s.serve_forever()
    threading.Thread(target=up_later, daemon=True).start()
    try:
        e = index.EngineEncoder.connect("e5-small")
    finally:
        while not srv:
            time.sleep(0.05)
        srv[0].shutdown()
    assert e is not None and "FINDR_EMBED_WAIT" in capsys.readouterr().out


def test_no_starting_engine_means_no_wait(tmp_path, monkeypatch):
    port = _free_port()
    _starting(tmp_path, monkeypatch, port)
    (tmp_path / index.STARTING_FILE).unlink()
    t = time.time()
    assert index.EngineEncoder.connect("e5-small") is None and time.time() - t < 5


def test_a_mark_left_behind_is_not_waited_for(tmp_path, monkeypatch):
    port = _free_port()
    _starting(tmp_path, monkeypatch, port, age=index.EMBED_WAIT_START + 120)   # an engine that died while starting
    assert not index.engine_starting(port)
    _starting(tmp_path, monkeypatch, port)
    assert index.engine_starting(port) and not index.engine_starting(port + 1)   # another port: another engine
    import psutil
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
    assert not index.engine_starting(port)


def test_embed_wait_setting(tmp_path, monkeypatch):
    port = _free_port()
    _starting(tmp_path, monkeypatch, port)
    monkeypatch.setenv("FINDR_EMBED_WAIT", "0")   # never wait
    t = time.time()
    assert index.EngineEncoder.connect("e5-small") is None and time.time() - t < 5
    monkeypatch.setenv("FINDR_EMBED_WAIT", "45")
    assert index.embed_wait() == 45
    monkeypatch.delenv("FINDR_EMBED_WAIT")
    assert index.embed_wait() == index.EMBED_WAIT_START


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


def test_an_engine_that_comes_back_is_used_again(tmp_path, monkeypatch):
    class Gone:
        def vectors(self, texts):
            raise ConnectionError

    class Back:
        def vectors(self, texts):
            return np.ones((len(texts), 4), np.float16)
    got = iter([Gone(), Back()])
    monkeypatch.setattr(index.EngineEncoder, "connect", classmethod(lambda cls, name, wait=None: next(got)))
    monkeypatch.setattr(enc_eg2, "make_encoder", lambda *a, **k: pytest.fail("loaded its own copy"))
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    con = _db(tmp_path)
    index.embed(con, "e5-small")
    assert con.execute("SELECT count(*) FROM chunks WHERE emb IS NOT NULL").fetchone()[0] == 5


# --- memory waits ------------------------------------------------------------------------------------------------------

def test_resume_level_follows_the_pc(monkeypatch):
    monkeypatch.delenv("FINDR_MEM_BACK_GB", raising=False)
    monkeypatch.setattr(index, "ram_gb", lambda: 7.8)
    assert index.mem_back_gb() == index.MEM_BACK_SMALL_GB < index.MEM_BACK_GB   # an 8 GB laptop never got 2 GB back
    monkeypatch.setattr(index, "ram_gb", lambda: 15.9)
    assert index.mem_back_gb() == index.MEM_BACK_GB
    monkeypatch.setattr(index, "ram_gb", lambda: None)
    assert index.mem_back_gb() == index.MEM_BACK_GB
    monkeypatch.setenv("FINDR_MEM_BACK_GB", "1.5")
    assert index.mem_back_gb() == 1.5
    monkeypatch.setenv("FINDR_MEM_BACK_GB", "0.5")   # at or below the stop level it would stop again at once
    assert index.mem_back_gb() > index.MEM_LOW_GB


def test_an_8gb_pc_resumes_at_its_own_level(monkeypatch):
    monkeypatch.delenv("FINDR_MEM_BACK_GB", raising=False)
    monkeypatch.setattr(index, "ram_gb", lambda: 7.8)
    seq = iter([0.5, 1.3])   # 1.3 GB free is as good as it gets next to the engine
    monkeypatch.setattr(index, "mem_free_gb", lambda: next(seq))
    slept = []
    monkeypatch.setattr(index.time, "sleep", lambda s: slept.append(s))
    index.memory_wait()
    assert len(slept) == 1


def _clock(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(index.time, "time", lambda: clock[0])
    monkeypatch.setattr(index.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(index, "mem_free_gb", lambda: 0.3)
    return clock


def test_without_a_model_the_wait_is_short(monkeypatch):
    clock = _clock(monkeypatch)
    index._RELEASE.clear()
    index.memory_wait()
    assert clock[0] - 1000 <= index.MEM_WAIT_LIGHT + 5
    clock[0] = 1000.0
    index._RELEASE.append(lambda: True)   # it held its model: letting go of it is worth the longer wait
    try:
        index.memory_wait()
    finally:
        index._RELEASE.clear()
    assert index.MEM_WAIT_LIGHT + 5 < clock[0] - 1000 <= index.MEM_WAIT_MAX + 5


def test_the_wait_is_in_progress_json(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); index._P.update(stages={}, files=None, paused=False)
    index._P.pop("memwait", None)
    index.progress("meaning", 10, 100)
    seq = iter([0.5] + [0.6] * 20 + [2.5])
    monkeypatch.setattr(index, "mem_free_gb", lambda: next(seq))
    seen, clock = [], [1000.0]
    monkeypatch.setattr(index.time, "time", lambda: clock[0])

    def sleep(s):
        clock[0] += s
        seen.append(json.loads((tmp_path / "progress.json").read_text(encoding="utf-8")))
    monkeypatch.setattr(index.time, "sleep", sleep)
    index.memory_wait()
    assert all(p.get("memwait") for p in seen) and seen[0]["stage"] == "meaning" and seen[0]["done"] == 10
    assert seen[-1]["memwait"]["free_gb"] == 0.6    # refreshed while it waits
    after = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert "memwait" not in after and after["stage"] == "meaning" and after["done"] == 10


def test_the_page_says_why_indexing_waits():
    page = (index.Path(search.__file__).with_name("page.html")).read_text(encoding="utf-8")
    assert "P.memwait" in page and "메모리" in page


# --- the indexer lets go of its model after each stage -----------------------------------------------------------------

def test_the_model_goes_with_the_stage(tmp_path, monkeypatch):
    refs, freed = [], []

    class Own:
        st = True

        def emb(self, texts, query=False):
            import torch
            return torch.zeros(len(texts), 4)

    def make(*a, **k):
        o = Own(); o.cycle = o; refs.append(weakref.ref(o))   # models hold reference cycles: only the collector frees them
        return o
    monkeypatch.setattr(enc_eg2, "make_encoder", make)
    monkeypatch.setattr(enc_eg2, "EG2", Own)
    monkeypatch.setenv("FINDR_EMBED_VIA", "local")
    monkeypatch.setattr(governor, "give_back", lambda: (__import__("gc").collect(), freed.append(1)))
    con = _db(tmp_path)
    index.embed(con, "e5-small")
    assert refs and all(r() is None for r in refs) and freed == [1] and not index._RELEASE
    assert con.execute("SELECT count(*) FROM chunks WHERE emb IS NOT NULL").fetchone()[0] == 5


def test_give_back_is_safe_everywhere():
    governor.give_back()   # each step is best effort: no error on any system


# --- the engine lets go of the reranker when nobody asks ---------------------------------------------------------------

def test_idle_unload_setting(monkeypatch):
    monkeypatch.delenv("FINDR_IDLE_UNLOAD", raising=False)
    monkeypatch.setattr(governor, "ram_total_gb", lambda: 7.8)
    assert search.idle_unload_minutes() == search.IDLE_UNLOAD_MIN
    monkeypatch.setattr(governor, "ram_total_gb", lambda: 31.9)
    assert search.idle_unload_minutes() == 0
    monkeypatch.setenv("FINDR_IDLE_UNLOAD", "3")
    assert search.idle_unload_minutes() == 3
    monkeypatch.setenv("FINDR_IDLE_UNLOAD", "0")
    assert search.idle_unload_minutes() == 0


def test_reranker_is_let_go_when_idle_and_loaded_again(monkeypatch, tmp_path):
    d = str(tmp_path / "rank")
    monkeypatch.setenv("CE_DIR", d)
    monkeypatch.setattr(search, "CE", {})
    monkeypatch.setattr(search, "_CE_USED", [0.0])
    monkeypatch.setattr(governor, "give_back", lambda: None)
    loads = []
    monkeypatch.setattr(search, "_ce_load", lambda d_: (loads.append(d_), search.CE.__setitem__(d_, ("tok", "model"))))
    monkeypatch.setattr(search, "_ce_score", lambda q, res, d_, tok, m, nosumm, top: [m])
    assert search.ce_scores("견적", []) == ["model"] and loads == [d]
    used = search._CE_USED[0]
    assert not search.idle_unload(now=used + 9 * 60, minutes=10)       # asked 9 minutes ago: kept
    f = types.SimpleNamespace(_l2enc=("embeddinggemma-2", object()))
    assert search.idle_unload(f, now=used + 11 * 60, minutes=10)       # 11 minutes: let go
    assert search.CE == {} and f._l2enc is None
    assert not search.idle_unload(now=used + 20 * 60, minutes=10)      # nothing left to let go
    assert not search.idle_unload(now=used + 99 * 60, minutes=0)       # off
    search.ce_scores("견적", [])
    assert loads == [d, d]                                               # the next question loads it again


# --- DirectML is measured when the PC is not busy ----------------------------------------------------------------------

def test_dml_measurement_waits_while_indexing_or_short_of_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    monkeypatch.setattr(gpu_dml, "_free_gb", lambda: 8.0)
    assert gpu_dml.busy() == ""
    (tmp_path / "progress.json").write_text(json.dumps({"stage": "content", "pid": os.getpid(), "t": time.time()}))
    assert gpu_dml.busy() == "indexing"
    (tmp_path / "progress.json").write_text(json.dumps({"stage": "done", "pid": os.getpid(), "t": time.time()}))
    assert gpu_dml.busy() == ""
    (tmp_path / "progress.json").write_text(json.dumps({"stage": "content", "pid": os.getpid(), "t": time.time() - 3600}))
    assert gpu_dml.busy() == ""   # a line left behind by an indexer that stopped
    monkeypatch.setattr(gpu_dml, "_free_gb", lambda: 1.1)
    assert "GB free" in gpu_dml.busy()


def test_dml_check_runs_once_the_pc_is_free(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    monkeypatch.setattr(gpu_dml, "available", lambda: True)
    monkeypatch.setattr(gpu_dml, "decided", lambda m: None)
    states = iter(["indexing", "1.0 GB free", ""])
    monkeypatch.setattr(gpu_dml, "busy", lambda: next(states))
    slept, ran = [], []
    monkeypatch.setattr(gpu_dml.time, "sleep", lambda s: slept.append(s))

    class Done:
        returncode, stdout = 0, json.dumps({"use": "cpu", "why": "slower"}) + "\n"
    monkeypatch.setattr(gpu_dml.subprocess, "run", lambda *a, **k: ran.append(1) or Done())

    class Now:   # the background thread, run here
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(threading, "Thread", Now)
    gpu_dml.check(tmp_path / "m.onnx")
    assert len(slept) == 2 and ran == [1]


def test_dml_check_gives_up_for_this_run_when_always_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    monkeypatch.setattr(gpu_dml, "available", lambda: True)
    monkeypatch.setattr(gpu_dml, "decided", lambda m: None)
    monkeypatch.setattr(gpu_dml, "busy", lambda: "indexing")
    clock = [0.0]
    monkeypatch.setattr(gpu_dml.time, "time", lambda: clock[0])
    monkeypatch.setattr(gpu_dml.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(gpu_dml.subprocess, "run", lambda *a, **k: pytest.fail("measured while busy"))

    class Now:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(threading, "Thread", Now)
    gpu_dml.check(tmp_path / "m.onnx")
    assert not (tmp_path / "dml_check.json").exists()   # no verdict kept: the next start measures


# --- the engine opens its port before it loads -------------------------------------------------------------------------

def _get(url, auth=None, timeout=10):
    h = {"Authorization": "Basic " + base64.b64encode(f"x:{auth}".encode()).decode()} if auth else {}
    r = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(urllib.request.Request(url, headers=h), timeout=timeout)
    return r.status, dict(r.headers), r.read()


def test_port_opens_before_the_models_load(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); monkeypatch.setenv("FINDR_WARM", "0"); monkeypatch.setenv("FINDR_IDLE_UNLOAD", "0")
    monkeypatch.delenv("FINDR_READY_LINE", raising=False)
    (tmp_path / search.STARTING_FILE).write_text("{}")
    monkeypatch.setattr(search, "LOADED", threading.Event()); monkeypatch.setattr(search, "READY", threading.Event())
    monkeypatch.setattr(search, "_set_threads", lambda n: None)
    monkeypatch.setattr(governor, "Governor", lambda **kw: types.SimpleNamespace(start=lambda: None, state=lambda: {"tier": "test"}))
    stops = []
    monkeypatch.setattr(search, "watch_parent", lambda stop, **kw: stops.append(stop))
    go = threading.Event()

    def make():
        go.wait(20)
        return types.SimpleNamespace(search=lambda *a, **k: [])
    port = _free_port()
    t = threading.Thread(target=search.serve, args=(make, port), daemon=True); t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            st, hd, body = _get(base + "/healthz", timeout=2); break
        except OSError:
            time.sleep(0.05)
    else:
        go.set(); pytest.fail("the port did not open while the models load")
    try:
        assert st == 200 and body == b"ok" and hd["X-Findr-Loaded"] == "0"     # answers while loading; the body stays "ok"
        assert hd["X-Findr-Version"] == search._version() and float(hd["X-Findr-Started"]) == round(search.STARTED, 3)
        j = json.loads(_get(base + "/healthz?json=1")[2])
        assert j["ok"] and j["started"] == round(search.STARTED, 3) and j["pid"] == os.getpid() and j["loaded"] is False
        assert json.loads(_get(base + "/ready")[2]) == {"ready": False}
        assert (tmp_path / search.STARTING_FILE).exists()
        pw = (tmp_path / ".password").read_text().strip()
        got = []
        q = threading.Thread(target=lambda: got.append(_get(base + "/status", auth=pw, timeout=20)))
        q.start(); time.sleep(0.5)
        assert not got                                                      # a request that needs the index waits
        go.set(); q.join(20)
        assert got and got[0][0] == 200
        assert json.loads(_get(base + "/healthz?json=1")[2])["loaded"] is True
        assert not (tmp_path / search.STARTING_FILE).exists()               # an indexer stops waiting for it
    finally:
        go.set()
        for s in stops:
            s()
        t.join(10)


def test_cli_serve_hands_the_load_to_the_engine():
    src = cli.Path(cli.__file__).read_text(encoding="utf-8")
    body = src[src.index("def cmd_serve("):src.index("def cmd_mcp(")]
    assert "search.serve(lambda: search.Finder(" in body and "search.Finder(str(a.db))\n" not in body.replace("lambda: search.Finder(str(a.db))", "")
    assert "_mark_starting(a.port)" in body


def test_lane2_model_read_once_against_the_idle_unload():
    """review of #198: the idle unload may set _l2enc to None between the check and the use; it is read once"""
    class Racy:
        l2target = "embeddinggemma-2"
        reads = 0

        @property
        def _l2enc(self):
            Racy.reads += 1
            return ("embeddinggemma-2", "the model") if Racy.reads == 1 else None
    assert search.Finder.lane2_enc(Racy()) == "the model"


def test_a_question_marks_the_reranker_used_at_its_start(monkeypatch):
    """review of #198: a long question started before the idle limit is not unloaded under it"""
    class Stop(Exception):
        pass

    class F:
        def refresh(self):
            raise Stop
    monkeypatch.setattr(search, "_CE_USED", [1.0])
    with pytest.raises(Stop):
        search.Finder.search(F(), "회의록")
    assert time.time() - search._CE_USED[0] < 5
    assert search.idle_unload(minutes=1) is False
