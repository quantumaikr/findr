"""the resource manager (observe first): the comfort tier from what the PC has, pressure judged with hysteresis so a value near
a line does not switch back and forth, a reranker slowdown read as heat or sharing, and what it would do recorded only"""
from pathlib import Path

import governor
import search


def test_tiers_from_what_the_pc_has():
    assert governor.tier_of("cpu", 0, 6)[0] == "minimal"
    assert governor.tier_of("cpu", 0, 8)[0] == "light"        # most office laptops
    assert governor.tier_of("mps", 0, 8)[0] == "light"        # an 8 GB Mac
    assert governor.tier_of("mps", 0, 16)[0] == "standard"
    assert governor.tier_of("cuda", 6, 16)[0] == "standard"
    assert governor.tier_of("cuda", 16, 31)[0] == "full"
    assert "메모리 8GB" in governor.tier_of("cpu", 0, 8)[1]   # the reason is shown in plain words


def test_memory_pressure_with_hysteresis(monkeypatch):
    log = []
    g = governor.Governor(every=999, dev="cpu", gpu_gb=0, log=log.append)
    monkeypatch.setattr(g, "_smooth", lambda k, v, a=0.4: v)   # the raw values, to see the lines themselves
    seen = []
    for gb in (3.0, 1.2, 1.8, 2.2, 0.5, 0.9, 1.2):
        monkeypatch.setattr(governor, "mem_available_gb", lambda gb=gb: gb)
        seen.append(g.sample()["pressure"]["memory"])
    assert seen == ["ok", "tight", "tight", "ok", "critical", "critical", "tight"]   # 1.8 and 0.9 sit between the lines: kept
    assert any(l.startswith("FINDR_GOV") and "memory=critical" in l for l in log)
    st = g.state()
    assert st["observe_only"] is True and "would" in st and st["profile"]["tier"] in governor.TIERS


def test_reranker_slowdown_reads_as_heat(monkeypatch):
    g = governor.Governor(every=999, dev="mps", gpu_gb=0, log=lambda s: None)
    monkeypatch.setattr(governor, "mem_available_gb", lambda: 8.0)
    g.rerank_time(0.10, 5000)
    assert g.sample()["pressure"]["heat"] == "ok"
    for _ in range(8):
        g.rerank_time(0.25, 5000)   # the same work now takes 2.5x as long (a fanless Mac after a run of questions)
    st = g.sample()
    assert st["pressure"]["heat"] == "slow" and any("재정렬" in w for w in st["would"])


def test_engine_serves_status_and_reports_rerank_time():
    src = Path(search.__file__).read_text(encoding="utf-8")
    assert 'elif u.path == "/status":' in src and "GOV.rerank_time(" in src and "Governor(dev=DEVICE" in src


def test_batch_size_is_not_heat(monkeypatch):
    """a GPU has a fixed cost per batch: 3 candidates (the rest cached) cost more per candidate than 30. Observed on the GPU PC:
    'heat=slow' (4.4x, then 4.7x) on a cool desktop. Per token within each size, that is no slowdown."""
    g = governor.Governor(every=999, dev="cpu", gpu_gb=0, log=lambda s: None)
    monkeypatch.setattr(governor, "mem_available_gb", lambda: 8.0)
    for i in range(10):
        g.rerank_time(0.060, 30 * 384)           # a full batch of long texts
        g.rerank_time(0.030, 3 * 384)            # a small batch: more per candidate, fixed cost per batch
        g.rerank_time(0.020 + 0.002 * (i % 3), 30 * 40)   # short names only: few tokens, about the same per token
    assert g.sample()["pressure"]["heat"] == "ok"


def test_nvidia_gpu_has_no_reranker_heat_signal(monkeypatch):
    """a few ms per batch on an NVIDIA GPU: the ratio swung past the line on a cool desktop, so it is not used there"""
    g = governor.Governor(every=999, dev="cuda", gpu_gb=16, log=lambda s: None)
    monkeypatch.setattr(governor, "mem_available_gb", lambda: 8.0)
    g.rerank_time(0.001, 5000)
    for _ in range(8):
        g.rerank_time(0.010, 5000)
    assert g.sample()["signals"]["rerank_slowdown"] is None