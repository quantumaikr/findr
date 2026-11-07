"""Index a whole PC's user files (2026-11-07): the folders people search first are done first, so searching can start in minutes.
    python index_pc.py --db DB [--dry-run]
Each root goes through index.py (scan -> read text -> meaning vectors) with FINDR_SCOPE=user (no system/program/cache folders).
Prints progress by kind and timings only (no names)."""
import argparse, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from scope import user_roots

ap = argparse.ArgumentParser(); ap.add_argument("--db", required=True); ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--model", default="auto", help="auto: embeddinggemma-2 with a GPU, e5-small on CPU-only PCs")
a = ap.parse_args()
if a.model == "auto":
    try:
        import torch; a.model = "embeddinggemma-2" if torch.cuda.is_available() else "e5-small"
    except ImportError:
        a.model = "e5-small"
roots = user_roots(); t0 = time.time()
print("PC_MODEL", a.model, flush=True)
print(f"PC_ROOTS {len(roots)} folders:", ", ".join(sorted({lab for _, lab, _ in roots})), flush=True)
if a.dry_run:
    sys.exit(0)
env = {**os.environ, "FINDR_SCOPE": "user"}
done_by = {}
for i, (order, label, p) in enumerate(roots):
    t = time.time()
    last_of_order = i + 1 == len(roots) or roots[i + 1][0] != order   # load the meaning model once per order group, not per folder
    out = subprocess.run([sys.executable, str(Path(__file__).with_name("index.py")), "--root", str(p), "--db", a.db, "--model", a.model] + ([] if last_of_order else ["--no-embed"]), env=env,
                         capture_output=True, text=True)
    if out.returncode:
        print(f"PC_FAIL order {order} {label}: exit {out.returncode}:", out.stderr.strip().splitlines()[-1][:200] if out.stderr.strip() else "", flush=True)
    out = out.stdout
    stats = [l for l in out.splitlines() if l.startswith(("FINDR_SCAN", "FINDR_READ", "FINDR_EMBED"))]
    nums = " | ".join(l.replace("FINDR_", "").lower() for l in stats)
    done_by[order] = time.time() - t0
    print(f"PC_STEP order {order} {label}: {time.time() - t:.0f}s ({nums}) | elapsed {time.time() - t0:.0f}s", flush=True)
print("PC_DONE", {f"searchable after order {o}": f"{s:.0f}s" for o, s in sorted(done_by.items())})
