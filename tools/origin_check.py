"""where files came from (findr/core/origin.py), checked on this PC without touching its index: the index is copied, the
source marks are collected on the copy, and only counts are printed (no file names, paths, sites or apps).
Also checks that no online-only cloud file was downloaded by the check (Windows: OneDrive 'online-only' and similar).
    python tools/origin_check.py [INDEX.db] [--keep COPY.db]"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "findr" / "core"))
import origin  # noqa: E402

CLOUD = 0x00400000 | 0x00040000 | 0x00001000


def home():
    if os.environ.get("FINDR_HOME"):
        return Path(os.environ["FINDR_HOME"])
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "findr"
    return Path.home() / ".findr"


def cloud_count(paths):
    n = 0
    for p in paths:
        try:
            if getattr(os.stat(origin._long(p) if os.name == "nt" else p), "st_file_attributes", 0) & CLOUD:
                n += 1
        except OSError:
            pass
    return n


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    db = Path(args[0]) if args else home() / "index.db"
    keep = sys.argv[sys.argv.index("--keep") + 1] if "--keep" in sys.argv else None
    out = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="findr-origin-")) / "copy.db"
    src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(out); src.backup(dst); src.close()
    paths = [p for (p,) in dst.execute("SELECT path FROM files")]
    before = cloud_count(paths)
    runs = []
    for budget in (0, 0):   # a full first pass, then a second run that should find nothing new
        t = time.time(); got = origin.collect(dst, budget=budget); runs.append({**got, "secs": round(time.time() - t, 1)})
    after = cloud_count(paths)
    kinds = dict(dst.execute("SELECT type, count(*) FROM events WHERE source LIKE 'pc:%' GROUP BY type").fetchall())
    no_time = dst.execute("SELECT count(*) FROM events WHERE source LIKE 'pc:%' AND time IS NULL").fetchone()[0]
    t = time.time(); dst.execute("DELETE FROM origin_seen"); dst.commit()
    bounded = origin.collect(dst, budget=2); bounded["secs"] = round(time.time() - t, 1)   # the per-run limit holds
    print(json.dumps({"os": sys.platform, "files": len(paths), "first": runs[0], "second": runs[1], "kinds": kinds,
                      "no_time": no_time, "cloud_online_only_before": before, "cloud_online_only_after": after,
                      "bounded_2s": bounded}, ensure_ascii=False))
    dst.close()
    if not keep:
        os.remove(out); os.rmdir(out.parent)


if __name__ == "__main__":
    main()
