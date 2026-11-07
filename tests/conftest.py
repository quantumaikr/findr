"""shared test setup: the engine's own folder on sys.path (its modules import each other by name)"""
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "findr" / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
