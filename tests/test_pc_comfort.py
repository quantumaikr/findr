"""findr on a PC the person is also using (field, 11-13: a 16 GB CPU laptop with a video and a browser open froze while the
first index ran and the person searched): the first index at low priority with half the cores, reader processes bounded by
the thread share and by free memory, texts read only a little ahead of storing, the indexer letting go of its model and
waiting when memory runs out, the second meaning lane only with memory to spare, and the engine dropping that lane when
memory turns critical"""
import sys
import time
from pathlib import Path

import governor
import index

ROOT = Path(__file__).resolve().parent.parent


def test_readers_follow_threads_and_free_memory(monkeypatch):
    monkeypatch.delenv("FINDR_READ_WORKERS", raising=False)
    monkeypatch.setattr(index.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(index, "mem_free_gb", lambda: 20.0)
    monkeypatch.delenv("FINDR_THREADS", raising=False)
    assert index.read_workers() == 8                      # one per core less one, at most 8
    monkeypatch.setenv("FINDR_THREADS", "4")
    assert index.read_workers() == 4                      # the app's share for indexing
    monkeypatch.setattr(index, "mem_free_gb", lambda: 2.5)
    assert index.read_workers() == 1                      # 2.5 GB free: one reader, 2 GB stay for the person and the engine
    monkeypatch.setattr(index, "mem_free_gb", lambda: 3.1)
    assert index.read_workers() == 3
    monkeypatch.setattr(index, "mem_free_gb", lambda: None)
    assert index.read_workers() == 4                      # memory unknown: the thread share alone
    monkeypatch.setenv("FINDR_READ_WORKERS", "2")
    assert index.read_workers() == 2                      # said outright


def test_read_all_keeps_order_and_reads_only_a_little_ahead(tmp_path, monkeypatch):
    files = []
    for i in range(40):
        p = tmp_path / f"f{i:02d}.txt"; p.write_text(f"text number {i}", encoding="utf-8"); files.append(p)
    monkeypatch.setenv("FINDR_READ_WORKERS", "2")
    got = [t for t in index.read_all([(str(p), 1000) for p in files])]
    assert [t.strip() for t in got] == [f"text number {i}" for i in range(40)]
    src = Path(index.__file__).read_text(encoding="utf-8")
    body = src[src.index("def read_all("):src.index("def read_wave(")]
    assert "ex.map(" not in body and "n * 3" in body   # Executor.map submits every job at once


def test_indexer_lets_go_and_waits_when_memory_runs_out(monkeypatch, capsys):
    seq = iter([0.5, 0.6, 1.5, 2.4])
    monkeypatch.setattr(index, "mem_free_gb", lambda: next(seq))
    # a 16 GB PC: it goes on at 2 GB free. The level depends on the PC's RAM (#198: 1.2 GB on 12 GB or less), and the
    # macOS CI runner has 7 GB, where 1.5 GB is already enough (test_an_8gb_pc_resumes_at_its_own_level covers that)
    monkeypatch.delenv("FINDR_MEM_BACK_GB", raising=False)
    monkeypatch.setattr(index, "ram_gb", lambda: 16.0)
    monkeypatch.setattr(index.time, "sleep", lambda s: None)
    dropped = []
    index._RELEASE.append(lambda: dropped.append(1))
    try:
        index.memory_wait()
    finally:
        index._RELEASE.clear()
    out = capsys.readouterr().out
    assert dropped == [1] and "FINDR_MEMWAIT" in out and "FINDR_MEMBACK 2.4" in out
    monkeypatch.setattr(index, "mem_free_gb", lambda: 6.0)
    index.memory_wait()                                   # enough memory: nothing happens
    assert "FINDR_MEMWAIT" not in capsys.readouterr().out


def test_memory_wait_has_a_limit(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(index, "mem_free_gb", lambda: 0.3)  # another program keeps the memory for good
    monkeypatch.setattr(index.time, "time", lambda: clock[0])
    monkeypatch.setattr(index.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    index.memory_wait()
    assert clock[0] <= index.MEM_WAIT_MAX + 5


def test_pause_wait_checks_memory_first():
    src = Path(index.__file__).read_text(encoding="utf-8")
    body = src[src.index("def pause_wait("):src.index("def _repeat_progress(")]
    assert "memory_wait()" in body
    emb = src[src.index("def embed(con"):]
    assert "_RELEASE.append(let_go)" in emb and "LOCAL_STEP" in emb


def test_governor_acts_once_when_memory_turns_critical(monkeypatch):
    calls = []
    g = governor.Governor(every=999, dev="cpu", gpu_gb=0, log=lambda s: None, act=lambda w: calls.append(w) or "dropped")
    load = iter([10.0, 10.0, 95.0, 10.0, 95.0, 10.0, 10.0])   # the CPU load moves while memory stays critical
    monkeypatch.setattr(g, "_smooth", lambda k, v, a=0.4: next(load) if k == "cpu" else v)
    for gb in (3.0, 0.5, 0.6, 0.4, 2.5, 0.5):
        monkeypatch.setattr(governor, "mem_available_gb", lambda gb=gb: gb)
        g.sample()
    assert calls == ["memory", "memory"]                  # on entering critical, not on every sample while it lasts
    st = g.state()
    assert st["observe_only"] is False and len(st["acted"]) == 2
    bad = governor.Governor(every=999, dev="cpu", gpu_gb=0, log=lambda s: None, act=lambda w: 1 / 0)
    monkeypatch.setattr(governor, "mem_available_gb", lambda: 0.2)
    bad.sample()                                          # an action that fails never takes the engine down


def test_second_lane_needs_free_memory(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from findr import cli
    monkeypatch.delenv("LANE2", raising=False)
    monkeypatch.setattr(cli, "_ram_gb", lambda: 16.0)
    assert cli._lane2(free=8.0) is True
    assert cli._lane2(free=3.5) is False                  # 16 GB, but a video and a browser already hold most of it
    monkeypatch.setattr(cli, "_ram_gb", lambda: 8.0)
    assert cli._lane2(free=8.0) is False
    monkeypatch.setenv("LANE2", "1"); assert cli._lane2(free=1.0) is True


def test_engine_drops_the_second_lane_under_pressure():
    src = (ROOT / "findr/core/search.py").read_text(encoding="utf-8")
    enc = src[src.index("def lane2_enc(self):"):src.index("def lane2_off(self, e):")]
    assert "LANE2_FREE_GB" in enc and "MemoryError" in enc
    assert "act=act" in src and "f.lane2_off(MemoryError" in src


def test_first_index_runs_low_with_half_the_cores():
    rs = (ROOT / "app/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    body = rs[rs.index("fn start_index("):rs.index("fn start_engine(")]
    assert "low_priority(python())" in body and "FINDR_THREADS" in body and "command(python())" not in body
