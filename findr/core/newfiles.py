"""new files while findr runs (#221): the engine looks at the time of every indexed folder every few seconds. A folder's
time moves when a file in it is added, removed or renamed, on every system and in cloud-synced folders too, so no
system-specific watcher is needed (no FSEvents, no ReadDirectoryChangesW). A changed folder is listed; a file in it that
the index does not have asks the app for a names-only run (the `names_now` file), whatever the battery or idle state:
the file is then found by its name within seconds, and its content comes with the next full run.

The page shows how many new files are waiting (`waiting()`); a file the index still leaves out after two runs (a temp
file, an excluded kind) stops counting, so it never asks again."""
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

EVERY = float(os.environ.get("FINDR_WATCH_EVERY") or 5)   # seconds between looks (longer when a look itself takes long)
FULL_EVERY = 30.0   # seconds between full re-reads of the indexed paths while the index keeps changing (renames, deletions)
NEW_DIR_FILES = 200   # files counted in a folder that appeared since the last look (deeper folders: the run finds them)
# not the person's files, or files the index leaves out: they never ask for a run (index.JUNK, office lock and temp files)
SKIP = re.compile(r"^(\.|~\$|~WRL)|\.(tmp|crdownload|part|download|partial|swp|lock)$|^(Thumbs\.db|desktop\.ini)$", re.I)


class Watch:
    def __init__(self, db, data, every=EVERY):
        self.db, self.data, self.every = str(db), Path(data), every
        self.con = None
        self.dv = None
        self.known = {}       # folder -> names the index has in it
        self.max_id = 0       # the newest file row read
        self.full_at = 0.0
        self.mt = {}          # folder -> its time at the last look
        self.wait = {}        # path -> [first seen, runs seen since]
        self.ignored = set()  # paths two runs left out
        self.lock = threading.Lock()
        self.took = 0.0       # seconds the last look took
        self.last_run = self._last_run()  # the app's last_refresh at the last look (none yet: the first run counts too)

    # the index's side ----------------------------------------------------------------------------------------------
    def _connect(self):
        if self.con is None:
            uri = Path(self.db).expanduser().resolve().as_uri() + "?mode=ro"
            self.con = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self.con.execute("PRAGMA busy_timeout=2000")
        return self.con

    def _read(self, full):
        con = self._connect()
        if full:
            rows = con.execute("SELECT id, path FROM files").fetchall()
            self.known = {}
        else:   # new rows only: a file the indexer just added (renames and deletions wait for the next full read)
            rows = con.execute("SELECT id, path FROM files WHERE id > ?", (self.max_id,)).fetchall()
        for fid, p in rows:
            d, n = os.path.split(p)
            self.known.setdefault(d, set()).add(n)
            self.max_id = max(self.max_id, fid)
        roots = self.roots()
        for r in roots:
            self.known.setdefault(r, set())
        # the folders between a root and the folders with files (Documents/Projects holds only folders): a new file there
        # changes their time, not that of a folder below
        for d in list(self.known):
            up = os.path.dirname(d)
            while up and up != d and up not in self.known and any(up.startswith(r.rstrip("/") + "/") for r in roots):
                self.known[up] = set()
                d, up = up, os.path.dirname(up)
        if full:
            self.full_at = time.time()

    def _look_up(self):
        """the waiting files the index has now under the same path: a renamed or moved file is an old row with a new path
        (reuse_moved), which the read of new rows does not see"""
        with self.lock:
            ps = list(self.wait)
        for p in ps[:500]:
            try:
                if self.con.execute("SELECT 1 FROM files WHERE path=?", (p,)).fetchone():
                    d, n = os.path.split(p)
                    self.known.setdefault(d, set()).add(n)
            except sqlite3.Error:
                return

    def roots(self):
        try:
            rs = [Path(x).as_posix() for x in json.loads((self.data / "folders.json").read_text(encoding="utf-8"))]
            return [r.rstrip("/") if len(r.rstrip("/")) > 2 else r for r in rs]   # (/ and C:/ keep their slash)
        except (OSError, ValueError, TypeError):
            return []

    def has(self, path):
        d, n = os.path.split(path)
        return n in self.known.get(d, ())

    # one look --------------------------------------------------------------------------------------------------------
    def step(self):
        """look once; returns the number of new files waiting"""
        t0 = time.time()
        try:
            dv = self._connect().execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error:
            self.con = None
            return self.waiting()
        if self.dv is None or dv != self.dv:
            self._read(full=self.dv is None or time.time() - self.full_at >= FULL_EVERY)
            self.dv = dv
            self._look_up()
        run = self._last_run()
        if run != self.last_run:   # a run ended since the last look
            with self.lock:
                for v in self.wait.values():
                    v[1] += 1
            self.last_run = run
        changed = []
        for d in list(self.known):
            try:
                t = os.stat(d).st_mtime_ns
            except OSError:
                continue   # gone, or a drive that is not there now: the full run decides
            if d not in self.mt:
                self.mt[d] = t   # first look: the time to compare with
            elif t != self.mt[d]:
                self.mt[d] = t
                changed.append(d)
        for d in changed:
            self._list(d, depth=0)
        with self.lock:
            for p in [p for p in self.wait if self.has(p) or not os.path.exists(p)]:
                del self.wait[p]   # in the index now, or gone again
            for p in [p for p, v in self.wait.items() if v[1] >= 2]:
                self.ignored.add(p); del self.wait[p]   # two runs left it out: the index's rules, not a delay
        n = self.waiting()
        if n:
            self._ask(n)
        first = not self.took
        self.took = time.time() - t0
        if first or self.took > 1:   # what a look costs on this PC (the field check reads it from the engine's log)
            print(f"FINDR_WATCH {len(self.mt)} folders {self.took * 1000:.0f} ms", flush=True)
        return n

    def _list(self, d, depth):
        try:
            es = list(os.scandir(d))
        except OSError:
            return
        for e in es:
            if SKIP.search(e.name):
                continue
            try:
                if e.is_file(follow_symlinks=False):
                    p = Path(d, e.name).as_posix()
                    if not self.has(p) and p not in self.ignored:
                        with self.lock:
                            self.wait.setdefault(p, [time.time(), 0])
                elif e.is_dir(follow_symlinks=False) and Path(d, e.name).as_posix() not in self.known and depth < 2:
                    sub = Path(d, e.name).as_posix()
                    if sub not in self.mt:   # a folder that came with the change: its files count, and it is watched
                        try:
                            self.mt[sub] = os.stat(sub).st_mtime_ns
                        except OSError:
                            continue
                        self.known.setdefault(sub, set())
                        if len(self.wait) < NEW_DIR_FILES:
                            self._list(sub, depth + 1)
            except OSError:
                continue

    def _last_run(self):
        try:
            return (self.data / "last_refresh").read_text().strip()
        except OSError:
            return None

    def _ask(self, n):
        """the app's refresher looks for this file every few seconds and takes the new names in at once"""
        f = self.data / "names_now"
        if not f.exists():
            try:
                f.write_text(str(n), encoding="utf-8")
            except OSError:
                pass

    def waiting(self):
        with self.lock:
            return len(self.wait)

    # the thread ------------------------------------------------------------------------------------------------------
    def run(self, stop=None):
        while not (stop and stop.is_set()):
            try:
                self.step()
            except Exception as e:   # noqa: BLE001  (a look that fails is tried again; search never depends on it)
                print(f"FINDR_WATCH failed {type(e).__name__}", flush=True)
            # a look over many folders on a slow disk takes long: it never takes more than about 1/30 of the time
            time.sleep(max(self.every, 30 * self.took))


WATCH = None


def start(db, data):
    """the engine's watch, once (the app's engine only: a plain `findr serve` has no refresher to answer it)"""
    global WATCH
    if WATCH is None and os.environ.get("FINDR_WATCH", "1") != "0":
        WATCH = Watch(db, data)
        threading.Thread(target=WATCH.run, daemon=True, name="findr-watch").start()
    return WATCH


def waiting():
    return WATCH.waiting() if WATCH is not None else 0
