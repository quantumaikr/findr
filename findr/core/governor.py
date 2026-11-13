"""Resource manager: what this PC is (profile) and how it is doing right now (signals), so findr does what fits comfortably.

Comfort before capability: a feature that makes the PC slow is worse than a lighter one that keeps it pleasant. It reads
the signals every few seconds, judges the pressure with hysteresis, and records what it would do (FINDR_GOV lines and
/status). One action is taken: when memory turns critical, `act("memory")` lets the engine drop what it can rebuild (the
second meaning lane's model). A 16 GB laptop with a video and a browser open froze while findr indexed and searched (field,
11-13); the indexer waits for memory on its own (index.memory_wait).

    profile()  -> {"tier", "reasons", "ram_gb", "cores", "gpu", "gpu_gb"}          fixed for this run
    Governor() -> thread; .state() -> {"profile", "signals", "pressure", "would", "since"}"""
import os
import sys
import threading
import time

TIERS = ("full", "standard", "light", "minimal")   # 쾌적+ / 표준 / 가벼움 / 최소
TIER_KO = {"full": "쾌적+", "standard": "표준", "light": "가벼움", "minimal": "최소"}


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:   # a minimal environment: the signals that need it are left out
        return None


def tier_of(dev, gpu_gb, ram_gb):
    """the comfort tier from what the PC has, with the reasons in plain words"""
    if ram_gb and ram_gb < 7.5:
        return "minimal", [f"메모리 {ram_gb:.0f}GB"]
    if dev == "cuda" and gpu_gb >= 10 and ram_gb >= 15:
        return "full", [f"NVIDIA GPU {gpu_gb:.0f}GB", f"메모리 {ram_gb:.0f}GB"]
    if dev == "cuda" and gpu_gb >= 4:
        return "standard", [f"NVIDIA GPU {gpu_gb:.0f}GB"]
    if dev == "mps" and ram_gb >= 15:
        return "standard", ["Apple GPU", f"메모리 {ram_gb:.0f}GB"]
    return "light", (["GPU 없음"] if dev == "cpu" else ["Apple GPU" if dev == "mps" else f"GPU {gpu_gb:.0f}GB"]) + \
        ([f"메모리 {ram_gb:.0f}GB"] if ram_gb else [])


def profile(dev=None, gpu_gb=None):
    """this PC as findr sees it at start: device, memory, cores and the comfort tier"""
    ps = _psutil()
    ram = ps.virtual_memory().total / 2**30 if ps else 0.0
    cores = (ps.cpu_count(logical=False) if ps else None) or os.cpu_count() or 0
    if dev is None:
        dev = os.environ.get("FINDR_DEVICE") or "cpu"
    if gpu_gb is None:
        gpu_gb = 0.0
        try:
            if dev == "cuda" and "torch" in sys.modules and sys.modules["torch"].cuda.is_available():
                gpu_gb = sys.modules["torch"].cuda.get_device_properties(0).total_memory / 2**30
        except Exception:   # noqa: BLE001
            pass
    t, why = tier_of(dev, gpu_gb, ram)
    return {"tier": t, "tier_ko": TIER_KO[t], "reasons": why, "ram_gb": round(ram, 1), "cores": cores, "gpu": dev,
            "gpu_gb": round(gpu_gb, 1)}


def mem_available_gb():
    """memory that can still be had now, inside a container's limit as well (WSL services, Docker, the low-memory test)"""
    ps = _psutil()
    avail = ps.virtual_memory().available / 2**30 if ps else None
    if sys.platform.startswith("linux"):
        try:
            rel = open("/proc/self/cgroup").read().strip().split(":")[-1]
            base = "/sys/fs/cgroup" + rel
            mx = open(base + "/memory.max").read().strip()
            if mx != "max":
                # memory.current counts the file cache too, which the kernel takes back before it stops anything: without
                # it a limit full of cache read as critical with room to spare (the same reckoning container tools use)
                used = int(open(base + "/memory.current").read())
                try:
                    for ln in open(base + "/memory.stat"):
                        if ln.startswith("inactive_file "):
                            used -= int(ln.split()[1]); break
                except OSError:
                    pass
                left = (int(mx) - used) / 2**30
                avail = left if avail is None else min(avail, left)
        except (OSError, ValueError):
            pass
    return avail


def on_battery():
    ps = _psutil()
    try:
        b = ps.sensors_battery() if ps else None
        return bool(b and not b.power_plugged)
    except Exception:   # noqa: BLE001  (no battery sensor on a desktop or in WSL)
        return False


_THERM = {"t": 0.0, "v": None}


def os_speed_limit():
    """macOS only: the CPU speed limit the OS applies for heat (pmset -g therm), asked at most once a minute"""
    if sys.platform != "darwin":
        return None
    if time.time() - _THERM["t"] > 60:
        _THERM["t"] = time.time()
        try:
            import re
            import subprocess
            out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
            _THERM["v"] = int(m.group(1)) if m else None
        except Exception:   # noqa: BLE001
            _THERM["v"] = None
    return _THERM["v"]


class Governor(threading.Thread):
    """samples the signals every `every` seconds (smoothed), judges the pressure with hysteresis, and keeps the state.
    Observe only: `would` lists what it would do, so the lines can be set from real signals before anything acts."""

    # thresholds (enter / leave): leave is looser than enter, so a value near a line does not switch back and forth
    MEM_TIGHT = (1.5, 2.0)     # GB available: tight below 1.5, ok again above 2.0
    MEM_CRIT = (0.7, 1.0)
    CPU_BUSY = (70.0, 50.0)    # % of all cores used by other programs
    SLOW = (2.0, 1.4)          # reranker time per token against its best at that size: heat or another program sharing

    def __init__(self, every=5.0, dev=None, gpu_gb=None, log=print, act=None):
        super().__init__(daemon=True, name="findr-governor")
        self.every, self.log, self.act, self.acted = every, log, act, []
        self.prof = profile(dev, gpu_gb)
        self.sig, self.press, self.since = {}, {"memory": "ok", "cpu": "ok", "power": "ac", "heat": "ok"}, time.time()
        self._ema, self._lock = {}, threading.Lock()
        self._rr_best, self._rr_ema = None, None
        self._ps = _psutil()
        self._me = self._ps.Process() if self._ps else None
        if self._ps:
            self._ps.cpu_percent(None)
            self._me.cpu_percent(None)

    @staticmethod
    def _bucket(tokens):
        return 0 if tokens < 2000 else 1 if tokens < 8000 else 2 if tokens < 32000 else 3

    def rerank_time(self, seconds, tokens):
        """the engine reports each reranking batch: its seconds and its size in tokens (padding included). Per candidate it
        read as heat on a cool desktop twice (4.4x, then 4.7x within batch sizes): a GPU has a fixed cost per batch, and the
        candidates' texts differ in length. Per token, against the best seen at that size, it measures the device itself."""
        if tokens <= 0 or self.prof["gpu"] == "cuda":
            # an NVIDIA GPU scores a batch in a few ms, and the ratio still swung past the line on a cool desktop (observed):
            # heat matters on fanless Macs and CPU laptops, where a batch takes long enough to measure
            return
        b, per = self._bucket(tokens), seconds / tokens
        with self._lock:
            best = self._rr_best = self._rr_best or {}
            best[b] = per if b not in best else min(best[b], per)
            r = per / best[b] if best[b] > 0 else 1.0
            self._rr_ema = r if self._rr_ema is None else 0.7 * self._rr_ema + 0.3 * r

    def _smooth(self, k, v, a=0.4):
        if v is None:
            return None
        self._ema[k] = v if k not in self._ema else (1 - a) * self._ema[k] + a * v
        return self._ema[k]

    @staticmethod
    def _level(cur, value, enter, leave, high, low="ok", more_is_bad=True):
        """hysteresis: enter `high` past `enter`, go back to `low` only past `leave`"""
        if value is None:
            return cur
        bad = (lambda x, t: x > t) if more_is_bad else (lambda x, t: x < t)
        if cur != high and bad(value, enter):
            return high
        if cur == high and not bad(value, leave):
            return low
        return cur

    def _mem_level(self, cur, m):
        """ok / tight / critical by available GB, each level left only past its looser line"""
        if m is None:
            return cur
        if m < self.MEM_CRIT[0] or (cur == "critical" and m < self.MEM_CRIT[1]):
            return "critical"
        if m < self.MEM_TIGHT[0] or (cur in ("tight", "critical") and m < self.MEM_TIGHT[1]):
            return "tight"
        return "ok"

    def sample(self):
        ps, sig = self._ps, {}
        sig["mem_avail_gb"] = self._smooth("mem", mem_available_gb())
        if ps:
            total = ps.cpu_percent(None)
            mine = self._me.cpu_percent(None) / max(1, ps.cpu_count() or 1)
            try:
                for c in self._me.children(recursive=True):
                    mine += c.cpu_percent(None) / max(1, ps.cpu_count() or 1)
            except Exception:   # noqa: BLE001
                pass
            sig["cpu_others_pct"] = self._smooth("cpu", max(0.0, total - mine))
            try:
                sig["findr_mem_gb"] = round((self._me.memory_info().rss + sum(c.memory_info().rss for c in self._me.children(recursive=True))) / 2**30, 2)
            except Exception:   # noqa: BLE001
                pass
        sig["on_battery"] = on_battery()
        sig["os_speed_limit"] = os_speed_limit()   # macOS: CPU speed limit % under heat (100 = none)
        with self._lock:
            sig["rerank_slowdown"] = round(self._rr_ema, 2) if self._rr_ema else None
        p = dict(self.press)
        p["memory"] = self._mem_level(self.press["memory"], sig["mem_avail_gb"])
        p["cpu"] = self._level(self.press["cpu"], sig.get("cpu_others_pct"), *self.CPU_BUSY, "busy")
        p["power"] = "battery" if sig["on_battery"] else "ac"
        p["heat"] = self._level(self.press["heat"], sig["rerank_slowdown"], *self.SLOW, "slow")
        if sig["os_speed_limit"] is not None and sig["os_speed_limit"] < 80:   # the OS says it is slowing the CPU down for heat
            p["heat"] = "slow"
        with self._lock:
            entered_critical = p["memory"] == "critical" and self.press["memory"] != "critical"
            changed = p != self.press
            self.sig = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in sig.items()}
            if changed:
                self.press, self.since = p, time.time()
        if changed:
            self.log("FINDR_GOV " + " ".join(f"{k}={v}" for k, v in p.items()) + " | would: " + ("; ".join(self.would()) or "-"))
            # on entering critical memory only: a CPU or power change while memory stays critical is not a new reason
            # (it acted again on a CI runner whose CPU load moved)
            if entered_critical and self.act is not None:
                try:
                    did = self.act("memory")
                    if did:
                        self.acted = (self.acted + [f"{int(time.time())} {did}"])[-10:]
                        self.log(f"FINDR_GOV acted: {did}")
                except Exception as e:   # noqa: BLE001  (the manager never takes the engine down)
                    self.log(f"FINDR_GOV act error {type(e).__name__}")
        return self.state()

    def would(self):
        """what an acting manager would do now (recorded only, nothing changes yet)"""
        p, w = self.press, []
        # memory: holding indexing was tried and measured, and it is not the action. In a 3 GB limit it held 300 s and
        # freed nothing (43 s -> 321 s); in 4 GB with the indexer's own model loaded, 43 s -> 144 s and more time at the
        # limit (a held process keeps what it holds). What frees memory is the indexer letting go of its model, to
        # start again later
        if p["memory"] == "critical":
            w.append("색인기가 모델을 내려놓고 나중에 이어서, 사진 모델 내림 (검색 엔진 보호)")
        elif p["memory"] == "tight":
            w.append("큰 파일 읽기 미룸")
        if p["cpu"] == "busy":
            w.append("백그라운드 작업 쉼, 재정렬 후보 줄임")
        if p["power"] == "battery":
            w.append("깊게 읽기·업그레이드 미룸")
        if p["heat"] == "slow":
            w.append("재정렬 시간 예산 줄임, 백그라운드 간격 늘림")
        return w

    def state(self):
        with self._lock:
            return {"profile": self.prof, "signals": dict(self.sig), "pressure": dict(self.press), "would": self.would(),
                    "since": round(self.since), "observe_only": self.act is None, "acted": list(self.acted)}

    def run(self):
        while True:
            try:
                self.sample()
            except Exception as e:   # noqa: BLE001  (the manager never takes the engine down)
                self.log(f"FINDR_GOV error {type(e).__name__}")
            time.sleep(self.every)
