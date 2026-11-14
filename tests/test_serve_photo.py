"""serve does not hold search back for the photo server: it loads for 1-2 minutes, and a server that died at start (a missing
package) used to keep the page closed for 5 minutes on every start of a GPU PC"""
import types

import pytest

from findr import cli, models, paths


class _Proc:
    def __init__(self, alive):
        self.alive = alive

    def poll(self):
        return None if self.alive else 1


class _Stop(Exception):
    pass


def _run(monkeypatch, tmp_path, alive):
    db = tmp_path / "index.db"; db.write_text("")
    monkeypatch.setattr(models, "plan", lambda *a, **k: ("cuda", 16.0, []))
    monkeypatch.setattr(models, "llama_server", lambda: None)
    monkeypatch.setattr(models, "have", lambda f: f == "kanana-2-v-decide-3b-2611")
    monkeypatch.setattr(paths, "MODELS", tmp_path / "models")
    monkeypatch.setattr(paths, "LOGS", tmp_path)
    monkeypatch.setenv("FINDR_DATA", str(tmp_path))   # the engine's starting mark goes here, not in the real data folder
    monkeypatch.setattr(cli, "_start", lambda name, cmd, log, env=None: _Proc(alive))
    monkeypatch.setattr(cli, "_wait", lambda *a, **k: pytest.fail("serve waited for the photo server"))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    got = {}

    def engine_env(photo_url=""):
        got["photo_url"] = photo_url
        raise _Stop

    monkeypatch.setattr(cli, "_engine_env", engine_env)
    with pytest.raises(_Stop):
        cli.cmd_serve(types.SimpleNamespace(db=db, no_llm=True, no_photo=False, port=8096))
    return got["photo_url"]


def test_photo_server_loading_does_not_hold_search(tmp_path, monkeypatch):
    assert _run(monkeypatch, tmp_path, alive=True).endswith(str(cli.PORTS["photo"]))


def test_photo_server_that_died_is_left_out_at_once(tmp_path, monkeypatch, capsys):
    assert _run(monkeypatch, tmp_path, alive=False) == ""
    assert "사진 서버가 시작하지 못했습니다" in capsys.readouterr().out


def test_photo_model_dependency_is_declared():
    """the photo model's image processor imports einops"""
    text = (cli.Path(cli.__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert '"einops' in text
