"""the reranker on a Windows GPU that is not NVIDIA (DirectML): measured once in its own process, used only where it ranks
real questions' files exactly as the CPU does, is clearly faster and never holds the GPU long enough for Windows to reset
the display driver"""
import json
import sys
from pathlib import Path

import numpy as np

import gpu_dml

ROOT = Path(__file__).resolve().parent.parent


class FakeSession:
    def __init__(self, out, secs, dml=True):
        self.out, self.secs, self.dml = out, list(secs), dml

    def get_providers(self):
        return ["DmlExecutionProvider", "CPUExecutionProvider"] if self.dml else ["CPUExecutionProvider"]

    def run(self, _names, feed):
        return [self.out]


def _verdict(monkeypatch, cpu_s, gpu_s, gpu_max=None, diff=0.0, provider=True, reorder=False):
    base = np.linspace(-3, 3, 10, dtype=np.float32).reshape(-1, 1)
    gpu = base[::-1].copy() if reorder else base + diff
    sess = {False: FakeSession(base, []), True: FakeSession(gpu, [], dml=provider)}
    monkeypatch.setattr(gpu_dml, "_pairs", lambda model: ({"input_ids": None}, [(0, 5), (5, 10)]))
    monkeypatch.setattr(gpu_dml, "session", lambda model, dml, threads=None: sess[dml])
    times = {id(sess[False]): (cpu_s, cpu_s), id(sess[True]): (gpu_s, gpu_max if gpu_max is not None else gpu_s)}
    monkeypatch.setattr(gpu_dml, "_time", lambda s, feed, runs=3: times[id(s)] + (s.out,))
    return gpu_dml.measure("model.onnx")


def test_a_faster_gpu_that_agrees_is_used(monkeypatch):
    r = _verdict(monkeypatch, cpu_s=0.90, gpu_s=0.20)          # e.g. a Radeon 680M against 8 CPU cores
    assert r["use"] == "dml" and r["why"] == "faster"


def test_a_slower_gpu_is_not_used(monkeypatch):
    r = _verdict(monkeypatch, cpu_s=0.90, gpu_s=0.80)          # an Intel UHD 620: about the CPU's speed or less
    assert r["use"] == "cpu" and "not faster" in r["why"]


def test_int8_rounding_differences_are_allowed(monkeypatch):
    r = _verdict(monkeypatch, cpu_s=0.90, gpu_s=0.10, diff=0.5)   # same order, probabilities up to ~0.12 apart
    assert r["use"] == "dml"


def test_a_gpu_that_ranks_differently_is_not_used(monkeypatch):
    r = _verdict(monkeypatch, cpu_s=0.90, gpu_s=0.10, reorder=True)
    assert r["use"] == "cpu" and "differently" in r["why"]


def test_scores_far_off_are_not_used(monkeypatch):
    r = _verdict(monkeypatch, cpu_s=0.90, gpu_s=0.10, diff=2.0)   # same order but a broken scale (probabilities ~0.4 apart)
    assert r["use"] == "cpu" and "scores differ" in r["why"]


def test_agreement_pairs_cover_korean_and_english():
    assert any(q.isascii() for q in gpu_dml.PAIRS) and any(not q.isascii() for q in gpu_dml.PAIRS)
    assert all(len(v) == 5 for v in gpu_dml.PAIRS.values())


def test_a_long_gpu_pass_is_not_used(monkeypatch):
    r = _verdict(monkeypatch, cpu_s=4.0, gpu_s=0.9, gpu_max=1.6)   # past the line where Windows resets the display driver
    assert r["use"] == "cpu" and "GPU pass" in r["why"]


def test_no_provider_is_cpu(monkeypatch):
    r = _verdict(monkeypatch, cpu_s=0.9, gpu_s=0.1, provider=False)
    assert r["use"] == "cpu"


def test_the_verdict_is_kept_per_runtime_and_model(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    monkeypatch.delenv("FINDR_DML", raising=False)
    monkeypatch.setattr(gpu_dml, "available", lambda: True)
    model = tmp_path / "model_int8.onnx"; model.write_bytes(b"x" * 100)
    assert gpu_dml.decided(model) is None                      # not measured yet: the engine starts on the CPU

    class Done:
        returncode, stdout = 0, json.dumps({"use": "dml", "why": "faster"}) + "\n"
    monkeypatch.setattr(gpu_dml.subprocess, "run", lambda *a, **k: Done())
    assert gpu_dml.check(model, wait=True)["use"] == "dml"
    assert gpu_dml.decided(model) == "dml"
    model.write_bytes(b"y" * 200)                              # a new model: measured again
    assert gpu_dml.decided(model) is None


def test_a_crashed_measurement_means_cpu(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    monkeypatch.setattr(gpu_dml, "available", lambda: True)
    model = tmp_path / "m.onnx"; model.write_bytes(b"x")

    class Crashed:   # a GPU driver that took the measuring process down
        returncode, stdout = 3221225477, ""
    monkeypatch.setattr(gpu_dml.subprocess, "run", lambda *a, **k: Crashed())
    r = gpu_dml.check(model, wait=True)
    assert r["use"] == "cpu" and gpu_dml.decided(model) == "cpu"


def test_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))
    monkeypatch.setattr(gpu_dml, "available", lambda: True)
    monkeypatch.setenv("FINDR_DML", "1")
    assert gpu_dml.decided(tmp_path / "m.onnx") == "dml"
    monkeypatch.setattr(gpu_dml, "available", lambda: False)   # FINDR_DML=0, not Windows, or no DirectML build
    assert gpu_dml.decided(tmp_path / "m.onnx") == "cpu"


def test_engine_and_install_use_it():
    src = (ROOT / "findr/core/search.py").read_text(encoding="utf-8")
    body = src[src.index("def _cpu_ranker_for("):src.index("def _cpu_ranker(m)")]
    assert "gpu_dml.decided(ox)" in body and "gpu_dml.check(ox)" in body and "_OrtRanker(ox, dml=True)" in body
    assert "back to the CPU" in src                            # a lost device mid-question falls back
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "onnxruntime-directml>=1.18; sys_platform == 'win32'" in py and "onnxruntime>=1.18; sys_platform != 'win32'" in py
    rs = (ROOT / "app/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    assert "swap_onnx_runtime();" in rs and "fn swap_onnx_runtime()" in rs
    assert "onnxruntime-directml" in (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
