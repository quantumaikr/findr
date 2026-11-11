"""context priority and a little learning with use, as ONE device: a capped bonus on each result's reranker score (no model,
this PC only). A bonus b <= CAP is added to the score (0-1) and near-ties are settled by it, so two files whose scores differ
by more than CAP never change places: the search decides, habits only settle near-ties.
The score, not the odds shown on the page: the odds are computed from the place in the list (#1 leads #2 by 0.6-0.75 at the
median), so a bonus on them could never reach #1 (11/11: 0 of 200 questions moved).
The bonus has three capped parts:
  purpose -> format (0.04): '발표 자료' is more often a .pptx than a .json. Common sense on day one; after MIN_OBS opened
                            files for that purpose, what this person really opens is mixed in
  a folder opened often (0.03), and a liking for recent files (0.02): only from what the person opened
What is kept (habits.jsonl, one line per opened file): time, kind of click, purpose kind, format group, folder, the file's
age when opened, whether it was opened from 'other versions'. Never the question, never the file's name.
    H = Habits(path); H.record("open", q, file_path, mtime=..., ext=...); res = H.apply(q, res)
An experiment, off unless HABITS=1 (then, and only then, opened files are counted). Measured 11/11 with a simulated person
who asks half of a PC's questions, opens the right file each time, and is tested on the other half (tools/habit_sim.py):
  ten virtual PCs, 1,000 test questions: #1 768 off, 767 on day one, 763 after living half. The folder part moved a result
  to #1 39 times (8 the answer, 10 past the answer), the format part 11 times (3 and 3). One real index (45): 32 -> 34.
The caps were not tuned to these numbers. Before it can be on: a folder signal that depends on the question."""
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path

CAP = 0.08                                         # the whole bonus: scores further apart than this keep their order
SCORE = "ce"                                       # the reranker's score of a result (0-1)
W_FORMAT, W_FOLDER, W_RECENT = 0.04, 0.03, 0.02    # the most each part can give (their sum is cut at CAP)
MIN_OBS = 5          # opened files for one purpose before this person's formats are mixed into the day-one values
PRIOR = 4            # the day-one values weigh as much as this many opened files
FOLDER_MIN = 5       # opened files before a folder can count as 'opened often'
RECENT_MIN = 8       # opened files before 'mostly opens recent files' can be said
RECENT_DAYS = 30
RECENT_FROM = 0.6    # share of recent files among the opened ones where the liking starts (1.0 gives the full part)
HALF_LIFE = 90       # days: an observation counts half after this long
MAX_BYTES = 5_000_000
ONCE_SECS = 3600     # one file opened, revealed and marked right within this long is one observation, not three
KINDS = ("open", "reveal", "copy", "hit")

GROUPS = {"slides": ("ppt", "pptx", "key"), "sheet": ("xls", "xlsx", "csv", "numbers", "ods"),
          "doc": ("hwp", "hwpx", "doc", "docx", "pages", "rtf", "odt"), "pdf": ("pdf",),
          "image": ("jpg", "jpeg", "png", "heic", "gif", "webp"), "text": ("md", "txt")}
EXT_GROUP = {e: g for g, es in GROUPS.items() for e in es}

# purpose kind, the words that say it, and the day-one fit of each format group (common sense, before anything is learned)
INTENTS = (
    ("present", ("발표", "슬라이드", "피피티", "ppt", "presentation", "deck", "slides"), {"slides": 1.0, "pdf": 0.5}),
    ("table", ("예산", "명단", "견적", "정산", "일정표", "목록", "통계", "budget", "roster", "schedule", "spreadsheet", "list"),
     {"sheet": 1.0, "pdf": 0.4}),
    ("paper", ("계약", "공문", "보고서", "회의록", "신청서", "기획안", "제안서", "규정", "contract", "report", "minutes", "proposal",
               "policy", "agreement"), {"doc": 1.0, "pdf": 1.0}),
    ("photo", ("사진", "찍은", "스크린샷", "캡처", "photo", "screenshot", "picture"), {"image": 1.0}),
)
DEFAULTS = {k: d for k, _, d in INTENTS}
# an English word counts as a whole word ('list' is not in 'playlist'); a Korean one anywhere ('발표자료', '보고서를')
_WORDS = [(k, re.compile(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])" if w.isascii() else re.escape(w)))
          for k, ws, _ in INTENTS for w in ws]
WHY = {"format": ("찾는 종류에 맞는 형식", "The format that fits what you asked for"),
       "folder": ("자주 여는 폴더", "A folder you open often"),
       "recent": ("최근 파일을 주로 여심", "You mostly open recent files")}


def intent(q):
    """the purpose kind a question names, or None. Words of several kinds: the one said first ('보고서 발표 자료' -> paper)"""
    s = unicodedata.normalize("NFC", q or "").lower()
    best = None
    for k, pat in _WORDS:
        m = pat.search(s)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), k)
    return best[1] if best else None


def group_of(name_or_ext):
    """format group of a file name, a path or an extension ('.PPTX', 'pptx', 'a/b/c.pptx' -> slides); the rest is 'other'"""
    s = str(name_or_ext or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return EXT_GROUP.get(s.rsplit(".", 1)[-1] if "." in s else s, "other")


def folder_of(path):
    """the folder a file is in, written one way for every kind of path (C:\\a\\b\\c.txt and C:/a/b/c.txt -> C:/a/b)"""
    s = unicodedata.normalize("NFC", str(path or "")).replace("\\", "/")
    return s.rsplit("/", 1)[0] if "/" in s else ""


def _secs(now):
    return time.time() if now is None else now.timestamp() if isinstance(now, dt.datetime) else float(now)


def day_start(now=None):
    """'now' for every count: the start of that day. The same question asked at 10:00 and at 16:00 gets the same answer
    (decay by the minute moved near-ties during the day, and a cached answer differed from a fresh one)"""
    return dt.datetime.fromtimestamp(_secs(now)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Habits:
    def __init__(self, path=None):
        self.path, self.rows, self.epoch, self._tally, self._lock = path, [], 0, None, threading.Lock()
        self._once = {}   # file -> when it was last counted; in memory only (the file's name is never written)
        if path and Path(path).exists():
            for line in open(path, encoding="utf-8"):
                try:
                    r = json.loads(line)
                except ValueError:   # a half-written last line (power cut): the rest still counts
                    continue
                if isinstance(r, dict) and isinstance(r.get("t"), (int, float)):
                    self.rows.append(r)

    def state(self):
        """what the answer cache keys on: it moves with every recorded line, and with every forget or trim (after a trim
        the line count returns to a number seen before with other lines behind it)"""
        return (len(self.rows), self.epoch)

    def record(self, kind, q, path, mtime=None, ext=None, from_versions=False, now=None):
        """one opened file. The question gives only its purpose kind and the path only its folder: neither is kept"""
        if kind not in KINDS or not path:
            return None
        t = _secs(now)
        key = unicodedata.normalize("NFC", str(path)).replace("\\", "/")
        with self._lock:
            if t - self._once.get(key, -ONCE_SECS) < ONCE_SECS:
                return None
            if len(self._once) > 5000:
                self._once.clear()
            self._once[key] = t
        r = {"t": int(t), "kind": kind, "intent": intent(q), "group": group_of(ext if ext else path), "folder": folder_of(path),
             "age_days": None if mtime is None else round(max(0.0, (t - float(mtime)) / 86400), 1), "from_versions": bool(from_versions)}
        with self._lock:
            self.rows.append(r); self._tally = None
            if self.path:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fo:
                    fo.write(json.dumps(r, ensure_ascii=False) + "\n")
                if os.path.getsize(self.path) > MAX_BYTES:   # years of clicks: the recent half stays (the old half had decayed)
                    self.rows = self.rows[len(self.rows) // 2:]; self.epoch += 1
                    self._write()
        return r

    def _write(self):
        with open(self.path, "w", encoding="utf-8") as fo:
            for r in self.rows:
                fo.write(json.dumps(r, ensure_ascii=False) + "\n")

    def forget(self):
        """forget every habit: the file on this PC is emptied"""
        with self._lock:
            self.rows = []; self.epoch += 1; self._tally = None; self._once = {}
            if self.path:
                self._write()

    def tally(self, now=None):
        """decayed counts as of the start of 'today': formats per purpose kind, folders, recent share, versions"""
        day = day_start(now); key = (self.state(), day)
        tl = self._tally
        if tl is not None and tl["key"] == key:
            return tl
        tl = {"key": key, "fmt": {}, "folders": {}, "n": 0.0, "aged": 0.0, "recent": 0.0, "versions": 0.0, "rows": len(self.rows)}
        for r in list(self.rows):
            # an observation made later 'today' (or after a pinned evaluation day) counts whole, never more than whole
            w = 0.5 ** (max(0.0, (day - r["t"]) / 86400) / HALF_LIFE)
            tl["n"] += w
            if r.get("intent"):
                d = tl["fmt"].setdefault(r["intent"], {}); d[r.get("group") or "other"] = d.get(r.get("group") or "other", 0.0) + w
            if r.get("folder"):
                tl["folders"][r["folder"]] = tl["folders"].get(r["folder"], 0.0) + w
            if r.get("age_days") is not None:
                tl["aged"] += w; tl["recent"] += w * (r["age_days"] <= RECENT_DAYS)
            tl["versions"] += w * bool(r.get("from_versions"))   # kept and counted, not used for the order yet
        self._tally = tl
        return tl

    def fit(self, kind, now=None):
        """how well each format group fits a purpose kind (0-1): the day-one values, and from MIN_OBS opened files on,
        mixed with the share of each group among them. 6 PDFs opened for '발표': pdf (6 + 4*0.5)/10 = 0.8, slides 4/10"""
        base = DEFAULTS.get(kind) or {}
        seen = self.tally(now)["fmt"].get(kind) or {}
        n = sum(seen.values())
        if n < MIN_OBS:
            return dict(base)
        return {g: (seen.get(g, 0.0) + PRIOR * base.get(g, 0.0)) / (n + PRIOR) for g in set(seen) | set(base)}

    def recent_share(self, now=None):
        """share of opened files that were at most RECENT_DAYS old when opened, or None before RECENT_MIN of them"""
        tl = self.tally(now)
        return tl["recent"] / tl["aged"] if tl["aged"] >= RECENT_MIN else None

    def bonuses(self, q, rs, now=None):
        """for each result its parts {'format', 'folder', 'recent'}; each within its own cap"""
        tl = self.tally(now); day = tl["key"][1]
        kind = intent(q); fit = self.fit(kind, now) if kind else {}
        top = max(tl["folders"].values()) if tl["folders"] and tl["n"] >= FOLDER_MIN else 0.0
        p = self.recent_share(now)
        like = min(1.0, (p - RECENT_FROM) / (1 - RECENT_FROM)) if p is not None and p >= RECENT_FROM else 0.0
        out = []
        for c in rs:
            path = c.get("path") or ""
            b = {"format": W_FORMAT * min(1.0, fit.get(group_of(c.get("name") or path), 0.0)),
                 "folder": W_FOLDER * tl["folders"].get(folder_of(path), 0.0) / top if top else 0.0, "recent": 0.0}
            if like:
                try:
                    age = (day - dt.datetime.strptime(str(c.get("date"))[:16], "%Y-%m-%d %H:%M").timestamp()) / 86400
                    b["recent"] = W_RECENT * like if age <= RECENT_DAYS else 0.0
                except ValueError:   # no date on this result: no part
                    pass
            out.append(b)
        return out

    def apply(self, q, res, now=None):
        """settle near-ties in res['results'] by the bonus (<= CAP), before the odds are computed.
        A result passes the one above it only when that one leads by score and the bonus lead is larger than the score lead.
        It never passes: a result without a score (a photo, a name match), a result an earlier stage put above a higher
        score (a rule decided there), or a #1 the remembered history confirmed. With no bonus, no change.
        A result that moved up carries 'habit': one line saying why"""
        rs = res.get("results") or []
        if len(rs) < 2:
            return res
        s = [c.get(SCORE) for c in rs]
        parts = self.bonuses(q, rs, now)
        b = [min(CAP, sum(x.values())) for x in parts]
        if not any(b):
            return res
        held = bool(res.get("episodic_ok"))
        order = []
        for i in range(len(rs)):
            j = len(order)
            while j > 0 and s[i] is not None:
                a = order[j - 1]
                if s[a] is None or s[a] < s[i] or (a == 0 and held) or b[i] - b[a] <= s[a] - s[i]:
                    break
                j -= 1
            order.insert(j, i)
        if order == list(range(len(rs))):
            return res
        S_ = sys.modules.get("search")   # the engine's sentence helper, when this runs inside the engine
        for new, i in enumerate(order):
            if new < i:   # moved up: say why, by the part that gave most
                ko, en = WHY[max(parts[i], key=parts[i].get)]
                rs[i]["habit"] = S_.M(ko, en) if S_ is not None and hasattr(S_, "M") else ko
                rs[i].setdefault("why", []).append(rs[i]["habit"])
        res["results"] = [rs[i] for i in order]
        return res

    def summary(self, now=None, top=5):
        """for the 'what findr has learned' page: formats per purpose kind and folders (most opened first), recent share"""
        tl = self.tally(now)
        return {"n": len(self.rows),
                "formats": {k: sorted(((g, round(v, 1)) for g, v in d.items()), key=lambda x: -x[1])[:3] for k, d in tl["fmt"].items()},
                "folders": sorted(((f, round(v, 1)) for f, v in tl["folders"].items()), key=lambda x: -x[1])[:top],
                "recent": self.recent_share(now), "aged": round(tl["aged"], 1), "versions": round(tl["versions"], 1)}


_STORE = None


def store():
    """the habits of this PC's person (FINDR_DATA or ~/.findr)"""
    global _STORE
    p = str(Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "habits.jsonl")
    if _STORE is None or _STORE.path != p:
        _STORE = Habits(p)
    return _STORE


def apply(q, res, now=None):
    return store().apply(q, res, now)
