"""stage 5 (instant layer): a search engine that gets to know its person — from the person's final choices, on this PC only.
Memory: (query, chosen file, rejected files). A new query that resembles a past one lifts the file chosen then (and lowers
files rejected then). Similarity: character bigrams of the request with particles and filler removed (no model, no GPU).
    M = Memory(path); M.add(q, chosen_fid, rejected_fids); res = M.apply(F, q, res)"""
import json, os, re, sys, threading
from pathlib import Path

FILL = re.compile(r"(찾아\s*줘|찾아줘|어디\s*있지|어디\s*있더라|어딨지|어디|파일|문서|거|것|좀|그|관련|했던|하던|있던|지난번|최신|최근|예전|요즘|이번)")   # what the file is, not when or which version


def grams(q):
    s = re.sub(r"\s+", "", FILL.sub(" ", q.lower()))
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


KIND = re.compile(r"결과보고|보고서|보고|예산표|예산|견적서|견적|회의록|일정표|일정|기획안|기획서|체크리스트|보도자료|요구사항|설계서|테스트결과|"
                  r"장애보고서|회고|계약서|제안서|정산서|정산|명단|메모|안내문|공지|목록|표|초안|최종")


def kinds(q):
    return set(KIND.findall(q.replace(" ", "")))


def sim(a, b, qa=None, qb=None):
    """v1: character bigrams. v2 (PM_SIM=v2): a request is 'which work' + 'which document' — a different document kind
    for the same work is a different memory"""
    s = len(a & b) / max(1, len(a | b))
    if os.environ.get("PM_SIM", "v2") == "v2" and qa is not None:
        ka, kb = kinds(qa), kinds(qb)
        if ka and kb and not (ka & kb):
            s *= 0.3
        ga = {w for w in FILL.sub(" ", qa).split() if w not in ka}; gb = {w for w in FILL.sub(" ", qb).split() if w not in kb}
        s = 0.5 * s + 0.5 * (len(ga & gb) / max(1, len(ga | gb)))
    return s


def max_rows():
    """the most choices kept (PM_MAX): the oldest go first. apply() reads every row on each question"""
    try:
        return max(10, int(os.environ.get("PM_MAX") or 2000))
    except ValueError:
        return 2000


def _row(r):
    """a stored line as a row, or None when it is not one (a half-written line, a hand edit)"""
    if not isinstance(r, dict) or not isinstance(r.get("q"), str):
        return None
    ch, rj = r.get("chosen"), r.get("rejected") or []
    if not (ch is None or isinstance(ch, int)) or not isinstance(rj, list) or not all(isinstance(x, int) for x in rj):
        return None
    return {"q": r["q"], "chosen": ch, "rejected": rj}


class Memory:
    def __init__(self, path=None, tau=0.25, lift=2.0):   # set on the nickname simulation (big_mkt, tuning), 11-07
        self.path, self.tau, self.lift, self.rows = path, tau, lift, []
        self._lock = threading.Lock()   # the page answers on several threads: a click and a 'forget' at once
        dirty = False
        if path and Path(path).exists():
            # a line cut by a power loss or a frozen PC must not stop every search (it raised on every question, #3):
            # it is left out, and the file is written again without it
            raw = Path(path).read_bytes()
            dirty = bool(raw) and not raw.endswith(b"\n")
            for line in raw.decode("utf-8", "replace").splitlines():
                if not line.strip():
                    continue
                try:
                    r = _row(json.loads(line))
                except ValueError:
                    r = None
                if r is None:
                    dirty = True
                else:
                    self.rows.append(r)
        if len(self.rows) > max_rows():
            self.rows = self.rows[-max_rows():]; dirty = True
        for r in self.rows:
            r["_g"] = grams(r["q"])
        if dirty and path:
            try:
                self._write()
            except OSError:
                pass

    def _write(self):
        """the whole file anew, all or nothing: a temporary file beside it, then put in its place"""
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fo:
                for r in self.rows:
                    fo.write(json.dumps({k: v for k, v in r.items() if k != "_g"}, ensure_ascii=False) + "\n")
                fo.flush()
                os.fsync(fo.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def add(self, q, chosen=None, rejected=()):
        r = {"q": q, "chosen": chosen, "rejected": list(rejected)}
        with self._lock:
            r["_g"] = grams(q); self.rows.append(r)
            cap = max_rows()
            if len(self.rows) > cap:   # trimmed to nine tenths, so the file is not rewritten on every later click
                self.rows = self.rows[-(cap * 9 // 10):]
                if self.path:
                    self._write()
            elif self.path:
                with open(self.path, "a", encoding="utf-8") as fo:
                    fo.write(json.dumps({k: v for k, v in r.items() if k != "_g"}, ensure_ascii=False) + "\n")

    def forget(self, i=None):
        """forget one learned link (index) or everything (None); the file on this PC is rewritten"""
        with self._lock:
            self.rows = [] if i is None else [r for j, r in enumerate(self.rows) if j != i]
            if self.path:
                self._write()

    def apply(self, F, q, res):
        """re-order res['results'] by remembered choices; a remembered file that is missing from the list joins it"""
        g = grams(q); bonus = {}
        with self._lock:
            rows = list(self.rows)
        for r in rows:
            s = sim(g, r["_g"], q, r["q"])
            if s < self.tau:
                continue
            if r["chosen"] is not None:
                bonus[r["chosen"]] = max(bonus.get(r["chosen"], 0), s)
            for f in r["rejected"]:
                bonus[f] = min(bonus.get(f, 0), -s)
        if not bonus:
            return res
        rs = res.get("results") or []
        have = {c["fid"] for c in rs}
        rs = rs + F.files_out([f for f, b in bonus.items() if b > 0 and f not in have])
        base = {c["fid"]: (c.get("prob") if c.get("prob") is not None else max(0.0, 1 - 0.08 * i)) for i, c in enumerate(rs)}
        # like a good assistant: follow a clear request as said; bring memory in when the engine is unsure,
        # or when the person repeats (nearly) the same request
        top = rs[0] if rs else {}
        conf = top.get("prob") if top.get("prob") is not None else max(0.0, min(1.0, top.get("ce") or 0.0))
        gate = os.environ.get("PM_GATE", "1") == "1"
        w = {f: (self.lift if (not gate or b >= 0.7 or b < 0) else self.lift * (1 - conf)) for f, b in bonus.items()}
        rs.sort(key=lambda c: -(base.get(c["fid"], 0) + w.get(c["fid"], 0) * bonus.get(c["fid"], 0)))
        for c in rs:
            if bonus.get(c["fid"], 0) > 0:
                S_ = sys.modules.get("search")   # the engine's sentence helper, when this runs inside the engine
                c.setdefault("why", []).append(S_.M("예전에 비슷하게 찾았을 때 고른 파일", "The file you chose when you asked something similar")
                                               if S_ is not None and hasattr(S_, "M") else "예전에 비슷하게 찾았을 때 고른 파일")
        res["results"] = rs
        return res
