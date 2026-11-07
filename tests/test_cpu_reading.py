"""without a GPU, questions are read by rules: the 0.6B reading model took 3.1 s per question on 4 cores, past the engine's
0.6 s wait, and slowed the reranker next to it"""
from findr import models


def test_cpu_plan_has_no_reading_model(monkeypatch):
    monkeypatch.setattr(models, "gpu", lambda: ("cpu", 0.0))
    _, _, want = models.plan()
    assert "qu" not in want and "checker" not in want
    assert "rank" in want and "multilingual-e5-small" in want


def test_gpu_plan_keeps_reading_model(monkeypatch):
    monkeypatch.setattr(models, "gpu", lambda: ("cuda", 16.0))
    _, _, want = models.plan(photo=False)
    assert "qu" in want and "checker" in want


def test_qu_ok():
    assert models.qu_ok("cuda") and models.qu_ok("mps") and not models.qu_ok("cpu")
