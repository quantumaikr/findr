"""shared test setup: the engine's own folder on sys.path (its modules import each other by name), and the command line in
Korean (findr/lang.py): the tests look for its sentences as they are written in Korean, whatever the language of the PC they run on"""
import os
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "findr" / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

os.environ["FINDR_LANG"] = "ko"   # also for the commands the tests start (they inherit it)
