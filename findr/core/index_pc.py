"""Index a whole PC's user files (2026-11-07): the folders people search first are done first, so searching can start in minutes.
    python index_pc.py --db DB [--dry-run]
Each root goes through index.py (scan -> read text -> meaning vectors) with FINDR_SCOPE=user (no system/program/cache folders).
Prints progress by kind and timings only (no names)."""
import argparse, os, subprocess, sys, time, unicodedata
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from scope import user_roots


def excluded():
    """this PC's own choice of folders not to search: exclude.txt in the data folder, one folder per line (~ allowed, # comments)"""
    f = Path(os.environ.get("FINDR_DATA") or Path.home() / ".findr") / "exclude.txt"
    if not f.exists():
        return []
    lines = (s.strip() for s in f.read_text(encoding="utf-8").splitlines())
    return [unicodedata.normalize("NFC", str(Path(s).expanduser())).rstrip("/\\") for s in lines if s and not s.startswith("#")]


def under(p, folders):
    k = (lambda x: x.casefold()) if os.name == "nt" else (lambda x: x)   # Windows folder names ignore case
    s = k(unicodedata.normalize("NFC", str(p)))
    return any(s == k(x) or s.startswith(k(x) + os.sep) for x in folders)


ap = argparse.ArgumentParser(); ap.add_argument("--db", required=True); ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--gone-only", action="store_true", help="only forget deleted files (index.py --gone-only)")
ap.add_argument("--model", default="auto", help="auto: embeddinggemma-2 with a GPU, e5-small on CPU-only PCs")
a = ap.parse_args()
if a.model == "auto":
    try:
        import torch; a.model = "embeddinggemma-2" if torch.cuda.is_available() else "e5-small"
    except ImportError:
        a.model = "e5-small"
ex = excluded()
roots = [r for r in user_roots() if not under(r[2], ex)]; t0 = time.time()
if ex:
    print(f"PC_EXCLUDE {len(ex)} folders (exclude.txt)", flush=True)
print("PC_MODEL", a.model, flush=True)
print(f"PC_ROOTS {len(roots)} folders:", ", ".join(sorted({lab for _, lab, _ in roots})), flush=True)
if a.dry_run:
    sys.exit(0)
env = {**os.environ, "FINDR_SCOPE": "user", "FINDR_EXCLUDE": os.pathsep.join(ex)}
# one run for every folder: names of the whole PC first, then content, then meaning (models are loaded once)
t = time.time()
rc = subprocess.call([sys.executable, str(Path(__file__).with_name("index.py")), "--root", *[str(p) for _, _, p in roots],
                      "--db", a.db, "--model", a.model, *(["--gone-only"] if a.gone_only else [])], env=env)
print(f"PC_DONE {len(roots)} folders in {time.time() - t:.0f}s (exit {rc})", flush=True)
sys.exit(rc)   # FINDR_BUSY (3) and crashes reach `findr index` (whole PC) and the scripts that run it, as with chosen folders
