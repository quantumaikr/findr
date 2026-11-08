"""the app's basic journeys (review of 0.2.14): the window must not freeze while the engine starts, going back to search must
not reload the models, the app's folders are the person's own files, half-done downloads are not 'installed', a failed upgrade
is tried again, the person's indexing run comes first, a failed update leaves search usable"""
import os
from pathlib import Path

from findr import cli, models, paths

ROOT = Path(__file__).resolve().parents[1]
LIB = (ROOT / "app" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "src" / "main.ts").read_text(encoding="utf-8")


def test_waiting_commands_off_the_main_thread():
    for fn in ("fn app_state(", "fn list_roots(", "fn start_engine(", "fn stop_engine("):
        i = LIB.index(fn)
        assert "#[tauri::command(async)]" in LIB[max(0, i - 400):i], fn


def test_running_engine_is_reused():
    assert "alive && *ENGINE_PHOTO.lock().unwrap() == Some(photo)" in LIB


def test_app_indexing_is_user_scope():
    assert LIB.count('c.env("FINDR_SCOPE", "user").args(["-m", "findr", "index"])') == 2


def test_person_first_and_upgrade_codes():
    assert "PERSON.store(true" in LIB and "kill_tree(pid); // a background refresh or deep run gives way" in LIB
    assert LIB.count("if person_waiting()") >= 1 and "person_waiting() || on_battery()" in LIB
    assert "code == Some(0) || code == Some(3)" in LIB and "code == Some(1)" not in LIB


def test_battery_codes():
    assert "codes.iter().all(|c| matches!(c, 1 | 4 | 5))" in LIB


def test_update_can_wait():
    assert "findr.updateLater" in MAIN and "지금은 이전 버전으로 검색" in MAIN


def test_half_downloaded_model_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MODELS", tmp_path)
    d = tmp_path / "multilingual-e5-small"
    (d / ".cache" / "huggingface" / "download").mkdir(parents=True)
    assert not models.have("multilingual-e5-small")   # only huggingface_hub's cache
    (d / "config.json").write_text("{}")
    (d / ".cache" / "huggingface" / "download" / "model.safetensors.incomplete").write_text("")
    assert not models.have("multilingual-e5-small")   # a download left half done
    (d / ".cache" / "huggingface" / "download" / "model.safetensors.incomplete").unlink()
    assert models.have("multilingual-e5-small")
    r = tmp_path / "rank"; r.mkdir(); (r / "config.json").write_text("{}")
    assert not models.have("rank")   # json files without the weights
    (r / "model_int8.onnx").write_text("")
    assert models.have("rank")


def test_upgrade_says_not_now_or_never(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME", tmp_path)
    a = type("A", (), {"db": str(tmp_path / "index.db"), "minutes": 1})()
    assert cli.cmd_upgrade(a) == 4   # no index yet: later
    (tmp_path / "upgrade_off").write_text("꺼짐", encoding="utf-8")
    assert cli.cmd_upgrade(a) == 3   # turned off: not for this PC
