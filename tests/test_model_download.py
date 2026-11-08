"""setup downloads every model the plan names. 0.2.10 lost the public ones (meaning search, photos): the loop that fetched
them had slid into pull_rank_fp32, so a new PC fetched the meaning model from the Hub at search time, and offline not at all"""
import sys
import types

from findr import models, paths


def _fake_hub(monkeypatch):
    calls = []
    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = lambda repo, **kw: calls.append((repo, kw))
    hub.hf_hub_download = lambda repo, name, **kw: calls.append((repo, {"file": name, **kw}))
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    return calls


def test_cpu_setup_fetches_meaning_model(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MODELS", tmp_path / "models")
    monkeypatch.setattr(models, "gpu", lambda: ("cpu", 0.0))
    calls = _fake_hub(monkeypatch)
    _, _, want = models.plan()
    models.pull(want)
    repos = [r for r, _ in calls]
    assert models.OWN in repos, "findr's own models (rank, gate)"
    assert "intfloat/multilingual-e5-small" in repos, "the meaning model is part of setup, not fetched at search time"
    e5 = next(kw for r, kw in calls if r == "intfloat/multilingual-e5-small")
    assert e5["local_dir"] == paths.MODELS / "multilingual-e5-small"


def test_present_models_are_not_fetched_again(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MODELS", tmp_path / "models")
    for f in ("rank", "gate", "multilingual-e5-small"):
        (tmp_path / "models" / f).mkdir(parents=True); (tmp_path / "models" / f / "x").write_text("1")
    calls = _fake_hub(monkeypatch)
    models.pull(["rank", "gate", "multilingual-e5-small"])
    assert calls == []


def test_full_precision_reranker_fetch_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MODELS", tmp_path / "models")
    calls = _fake_hub(monkeypatch)
    models.pull_rank_fp32()   # raised NameError in 0.2.10
    assert calls and calls[0][1]["allow_patterns"] == ["rank/*"]
