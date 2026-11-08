"""after an app update on a PC with an index and chosen folders, the app goes back to search; the folder screen again was a
needless question (field, 0.2.13). A first install still goes to the folder screen."""
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "app" / "src" / "main.ts"


def test_update_goes_back_to_search():
    src = MAIN.read_text(encoding="utf-8")
    assert "const keep = s.update && s.indexed && s.folders.length > 0" in src
    assert "const to = keep ? 'search' : 'folders'" in src
    assert "location.hash = 'folders'; return" not in src   # the old unconditional jump
