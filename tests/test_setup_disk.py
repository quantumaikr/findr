"""findr setup says what it will download and stops before downloading when the drive is too small (field, #67: a clean
install took 2.3-4.9 GB and a whole-PC index grew to 6-9 GB; setup used to start downloading whatever the free space)"""
import argparse
import collections

import pytest

from findr import cli, models, paths

Usage = collections.namedtuple("Usage", "total used free")
GB = 10 ** 9


def args(**kw):
    d = dict(no_photo=False, photo=False, lite=False, full=False, token=None, no_llm=True)
    d.update(kw)
    return argparse.Namespace(**d)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME", tmp_path)
    monkeypatch.setattr(paths, "MODELS", tmp_path / "models")
    monkeypatch.setattr(models.paths, "MODELS", tmp_path / "models")
    monkeypatch.delenv("FINDR_SKIP_DISK_CHECK", raising=False)
    return tmp_path


def fake_disk(monkeypatch, free_gb):
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: Usage(500 * GB, 0, int(free_gb * GB)))


def test_plan_counts_only_what_is_missing(home, monkeypatch):
    (home / "models" / "multilingual-e5-small").mkdir(parents=True)
    (home / "models" / "multilingual-e5-small" / "model.safetensors").write_bytes(b"x")   # there
    half = home / "models" / "qu" / ".cache" / "huggingface" / "download"
    half.mkdir(parents=True); (half / "x.gguf.incomplete").write_bytes(b"")   # a dropped download: missing, as have() says
    monkeypatch.setattr(models, "_repo_bytes", lambda repo, allow, ignore, token=None: 600 * 10 ** 6)
    monkeypatch.setattr(models, "gpu", lambda: ("mps", 0.0))
    plan = models.download_plan(["rank", "qu", "multilingual-e5-small"])
    assert [f for f, _, _ in plan] == ["rank", "qu"] and all(x for _, _, x in plan)


def test_offline_falls_back_to_estimates(home, monkeypatch):
    def down(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(models, "_repo_bytes", down)
    monkeypatch.setattr(models, "gpu", lambda: ("cpu", 0.0))
    plan = dict((f, (b, x)) for f, b, x in models.download_plan(["rank", "multilingual-e5-small"]))
    assert plan["rank"] == (models.APPROX_MB["rank-int8"] * 10 ** 6, False)   # a CPU PC fetches the int8 reranker only
    assert plan["multilingual-e5-small"] == (models.APPROX_MB["multilingual-e5-small"] * 10 ** 6, False)


def test_disk_free_before_the_first_setup(home, monkeypatch):
    seen = []
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: seen.append(p) or Usage(1, 0, 7))
    assert models.disk_free(home / "models" / "not" / "yet") == 7 and seen == [home]   # the nearest folder that exists


def _setup(monkeypatch, plan, free_gb):
    pulled = []
    monkeypatch.setattr(models, "plan", lambda photo=None, lite=False: ("mps", 0.0, [f for f, _, _ in plan] or ["rank"]))
    monkeypatch.setattr(models, "download_plan", lambda want, token=None: plan)
    monkeypatch.setattr(models, "pull", lambda want, token=None: pulled.append(list(want)))
    monkeypatch.setattr(cli, "_check_reranker", lambda: "")
    monkeypatch.setattr(cli, "_check_runtime", lambda: "stop here")   # the rest of setup is not this test's business
    fake_disk(monkeypatch, free_gb)
    return pulled


def test_too_small_stops_before_downloading(home, monkeypatch, capsys):
    pulled = _setup(monkeypatch, [("rank", int(1.2 * GB), True), ("qu", int(0.6 * GB), False)], free_gb=3)
    assert cli.cmd_setup(args()) == 5 and pulled == []
    out = capsys.readouterr().out
    assert "새로 받을 용량: 약 1.8GB (일부는 추정치)" in out and "디스크 여유: 3.0GB" in out
    assert "findr setup --lite" not in out and "FINDR_MODELS=" in out   # the stubbed plan has nothing --lite would drop
    assert out.strip().splitlines()[-1].startswith("  디스크 공간이 모자라 받지 않았습니다")   # the app shows the last line


def test_lite_is_offered_only_where_it_fetches_less(home, monkeypatch, capsys):
    pulled = _setup(monkeypatch, [("embeddinggemma-2", int(1.5 * GB), True), ("checker", int(1.8 * GB), True)], free_gb=3)
    big = ["rank", "gate", "embeddinggemma-2", "checker"]
    monkeypatch.setattr(models, "plan", lambda photo=None, lite=False: ("cuda", 8.0, ["rank", "gate", "multilingual-e5-small"] if lite else big))
    assert cli.cmd_setup(args()) == 5 and pulled == []
    assert "findr setup --lite   (빠지는 것: embeddinggemma-2, checker)" in capsys.readouterr().out


def test_enough_room_downloads(home, monkeypatch, capsys):
    pulled = _setup(monkeypatch, [("rank", int(1.2 * GB), True)], free_gb=50)
    assert cli.cmd_setup(args()) == 1 and pulled == [["rank"]]   # 1: the stubbed runtime check, after the download
    assert "디스크 여유: 50.0GB" in capsys.readouterr().out


def test_nothing_new_never_blocks_an_update(home, monkeypatch, capsys):
    """the app runs `findr setup` on every update: with every model already there, a nearly full drive must not stop it"""
    pulled = _setup(monkeypatch, [], free_gb=0.1)
    assert cli.cmd_setup(args()) == 1 and pulled == [["rank"]]
    assert "디스크" not in capsys.readouterr().out


def test_the_person_can_skip_the_check(home, monkeypatch):
    pulled = _setup(monkeypatch, [("rank", int(1.2 * GB), True)], free_gb=1)
    monkeypatch.setenv("FINDR_SKIP_DISK_CHECK", "1")
    assert cli.cmd_setup(args()) == 1 and pulled == [["rank"]]


def test_index_room_is_checked_on_the_data_folders_drive(home, monkeypatch, capsys):
    """FINDR_MODELS on a roomy drive must not hide a full drive under the data folder (the index goes there)"""
    pulled = _setup(monkeypatch, [("rank", int(1.2 * GB), True)], free_gb=0)
    big = home / "big"; big.mkdir()
    monkeypatch.setattr(paths, "MODELS", big / "models"); monkeypatch.setattr(models.paths, "MODELS", big / "models")
    monkeypatch.setattr(models, "same_drive", lambda a, b: False)
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: Usage(1, 0, int((50 if str(p).startswith(str(big)) else 1) * GB)))
    assert cli.cmd_setup(args()) == 5 and pulled == []
    out = capsys.readouterr().out
    assert "디스크 공간이 모자랍니다 (색인:" in out and "FINDR_HOME=" in out and "FINDR_MODELS=" not in out
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: Usage(1, 0, int((1.0 if str(p).startswith(str(big)) else 50) * GB)))
    assert models.room_check(int(1.2 * GB)) == [("모델", big / "models", int(1.0 * GB), 1.2 * GB * 1.1)]   # the models' own drive
    monkeypatch.setattr(models, "same_drive", lambda a, b: True)   # one drive: the sum (1.32 + 2 GB) must fit
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: Usage(1, 0, int(3 * GB)))
    assert [w[0] for w in models.room_check(int(1.2 * GB))] == ["모델과 색인"]


def test_offline_asks_the_repository_once(home, monkeypatch):
    """a dropped network or a hanging proxy: the first timeout decides, the other folders use their estimates at once"""
    calls = []

    def down(*a, **k):
        calls.append(a[0]); raise OSError("timed out")
    monkeypatch.setattr(models, "_repo_bytes", down)
    monkeypatch.setattr(models, "gpu", lambda: ("mps", 0.0))
    plan = models.download_plan(["rank", "gate", "qu", "multilingual-e5-small"])
    assert len(calls) == 1 and len(plan) == 4 and not any(x for _, _, x in plan)


@pytest.mark.parametrize("dev", ["cpu", "mps", "cuda"])
def test_pull_fetches_what_the_plan_sizes(home, monkeypatch, dev):
    """pull() and download_plan() select files with the same rule (_patterns), so the size shown is the size fetched"""
    import sys
    import types
    got = []
    hub = types.SimpleNamespace(snapshot_download=lambda repo, **k: got.append((repo, k.get("allow_patterns"), k.get("ignore_patterns"))))
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(models, "gpu", lambda: (dev, 8.0 if dev == "cuda" else 0.0))
    folders = ["rank", "gate", "qu", "multilingual-e5-small"]
    models.pull(folders)
    own = [models._patterns(f) for f in ("rank", "gate", "qu")]
    assert got[0] == (models.OWN, [p for _, a, _, _ in own for p in a], own[0][2])
    repo, allow, _, _ = models._patterns("multilingual-e5-small")
    assert got[1] == (repo, allow, None)
    if dev == "cpu":
        assert "rank/model_int8.onnx" in got[0][1] and got[0][2] is None
    else:
        assert got[0][2] == ["*.onnx"]
