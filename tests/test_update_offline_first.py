"""an app update installs the engine from uv's cache first: on 2026-11-08 GitHub answered 504 for a dependency wheel and the
update left the engine on the old version; online only when the cache does not have everything (a first install)"""
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "app" / "src-tauri" / "src" / "lib.rs"


def test_engine_step_tries_offline_first():
    src = LIB.read_text(encoding="utf-8")
    assert '"--offline", "--reinstall-package", "findr-ai"' in src
    assert '!stream(&app, "setup", "engine", off) && !stream(&app, "setup", "engine", c)' in src
