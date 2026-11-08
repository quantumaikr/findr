"""the pinned transformers wheel comes from the model repository at a fixed commit, with its hash: GitHub release downloads
answered 504 for hours on 2026-11-08 and every install and update that needed the wheel failed"""
import re
from pathlib import Path

PY = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_transformers_wheel_from_huggingface_pinned():
    line = next(l for l in PY.read_text(encoding="utf-8").splitlines() if '"transformers @' in l)
    assert "huggingface.co/quantumaikr/findr-models/resolve/" in line and "/resolve/main/" not in line
    assert re.search(r"#sha256=[0-9a-f]{64}\"", line)
