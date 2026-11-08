"""two sources for what install and update download, so one host's outage does not stop people (2026-11-08: GitHub release
downloads answered 504 for hours): the pinned wheel from Hugging Face, then the same file (same hash) from GitHub; uv for the
tests and the app bundle from Astral's own server, then GitHub"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _hash(s):
    return re.search(r"#sha256=([0-9a-f]{64})", s).group(1)


def test_wheel_second_source_is_the_same_file():
    py = next(l for l in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines() if '"transformers @' in l)
    lib = (ROOT / "app" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    alt = re.search(r'const ALT_DEPS: &str = "([^"]+)', lib).group(1)
    assert "huggingface.co" in py and "github.com" in alt and _hash(py) == _hash(alt)
    assert 'c2.env("UV_OVERRIDE", &alt)' in lib and '!stream(&app, "setup", "engine", c2)' in lib


def test_uv_from_astral_first():
    s = (ROOT / ".github" / "get-uv.sh").read_text(encoding="utf-8")
    assert s.index("releases.astral.sh") < s.index("github.com/astral-sh")
    assert "\r" not in s
    for wf in ("tests.yml", "desktop.yml"):
        y = (ROOT / ".github" / "workflows" / wf).read_text(encoding="utf-8")
        assert "get-uv.sh" in y and "setup-uv" not in y and "releases/latest/download/uv" not in y
