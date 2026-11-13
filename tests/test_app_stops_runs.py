"""the app's own runs stop with it (field, Mac: quitting during indexing left the indexer running; after three restarts three
wrote to one index and the engine reached 20 GB with 52 s answers), and a paused indexer shows as paused at once"""
import json
import time
from pathlib import Path

import search

LIB = Path(__file__).resolve().parents[1] / "app" / "src-tauri" / "src" / "lib.rs"


def test_runs_tracked_and_stopped():
    src = LIB.read_text(encoding="utf-8")
    assert "c.process_group(0)" in src and "fn kill_tree(pid: u32)" in src and '"-TERM", "--", &grp' in src
    assert src.count("own_group(&mut") >= 3   # every streamed step, the refresh and deep runs, the upgrade
    assert "track(pid);" in src and "untrack(pid);" in src
    assert "reap_left_runs(); //" in src   # at start: what a killed app left behind
    assert src.count("stop_runs();") >= 2   # the quit menu and the exit event
    assert "kill_tree(c.id()); // with the index.py it started" in src
    assert '.contains("findr"))' in src and '"pgrep").args(["-g"' in src   # only findr's own processes


def test_progress_says_paused(tmp_path, monkeypatch):
    src = Path(search.__file__).read_text(encoding="utf-8")
    # paused is the pause file itself; pausing: asked, and the indexer has not said it holds yet (up to 14 s, #66-10)
    assert 'asked = (data_ / "pause").exists()' in src and 'p_["pausing"] = asked and not p_.get("paused")' in src
    assert 'p_["paused"] = asked' in src
    lib = LIB.read_text(encoding="utf-8")
    assert 'let asked = home().join("pause").exists();' in lib and 'm.insert("paused".into(), serde_json::Value::Bool(asked));' in lib
    assert 'm.insert("pausing".into(), serde_json::Value::Bool(asked && !held));' in lib
