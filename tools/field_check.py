"""Field check against a running `findr serve`: the same numbers from every PC, with no file names in them.
    python tools/field_check.py [--port 8096] [--rounds 3] [--ready-wait 300] [--ask] [--db FILE] [--out FILE]
What it measures (acceptance.py measures ranks against known answers; this one needs no answer list):
  - the PC: OS, CPU cores, memory, findr version, index size (files, pieces)
  - the engine: /healthz, seconds until /ready, indexing still running
  - common office questions: seconds per question, queue wait, route, result counts at k=10 and k=30
  - with --ask: seconds for `findr ask` through the running engine
The result holds counts and times only, so it can be pasted into an issue as it is: <data folder>/field_check.json"""
import argparse
import base64
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# what office workers ask; no personal names, so every PC gets the same list
QUESTIONS = ["보고서", "견적서", "회의록", "계약서", "지난달에 만든 엑셀", "발표 자료", "작년 예산", "사진",
             "영수증 pdf", "프로젝트 일정표"]
NONE_Q = "2019년 남극 펭귄 사육 일지"   # should come back with a 'not found' note or low confidence
SLOW_P90 = 3.0   # seconds: above this on the slowest tenth, the check says so


def data_dir():
    h = os.environ.get("FINDR_HOME")
    if h:
        return Path(h).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "findr"
    return Path.home() / ".findr"


def mem_gb():
    try:
        if sys.platform == "darwin":
            return round(int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30, 1)
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong),
                            ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            m = MS(); m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.total / 2**30, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except Exception:   # noqa: BLE001  (a number we could not read is left out, not guessed)
        return None


def index_size(db):
    if not db.exists():
        return {}
    try:   # read-only: the engine may be writing
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        out = {"files": con.execute("SELECT count(*) FROM files").fetchone()[0],
               "pieces": con.execute("SELECT count(*) FROM chunks").fetchone()[0]}
        con.close()
        return out
    except sqlite3.Error as e:
        return {"error": str(e)}


class Engine:
    def __init__(self, port, d):
        self.base = f"http://127.0.0.1:{port}"
        pw = (d / ".password").read_text().strip() if (d / ".password").exists() else ""
        self.hdr = {"Authorization": "Basic " + base64.b64encode(f"field_check:{pw}".encode()).decode(), "X-Findr": "1"}

    def get(self, path, timeout=600):
        t = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=self.hdr), timeout=timeout) as r:
                raw = r.read().decode()
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"text": raw}
        except urllib.error.HTTPError as e:
            body = {"error": f"HTTP {e.code}"}
        except Exception as e:   # noqa: BLE001  (report, keep going)
            body = {"error": type(e).__name__}
        return body, time.time() - t


def ask_once(eng, q, k):
    body, dt = eng.get("/api?" + urllib.parse.urlencode({"q": q, "mode": "auto", "k": k}))
    res = body.get("results") or []
    return {"sec": round(dt, 2), "n": len(res), "wait_ms": body.get("wait_ms", 0), "route": body.get("route"),
            "note": bool(body.get("note")), "p1": res[0].get("prob") if res else None, "error": body.get("error")}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("FINDR_APP_PORT", 8096)))
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--ready-wait", type=float, default=300, help="seconds to wait for /ready")
    ap.add_argument("--ask", action="store_true", help="also time `findr ask` through the running engine")
    ap.add_argument("--db", default="", help="index file, if not <data folder>/index.db")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    d = data_dir()
    eng = Engine(a.port, d)
    rep = {"when": time.strftime("%Y-%m-%d %H:%M"), "pc": {
        "os": f"{platform.system()} {platform.release()}", "machine": platform.machine(), "cores": os.cpu_count(),
        "mem_gb": mem_gb(), "python": platform.python_version()}}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import findr
        rep["pc"]["findr"] = findr.__version__
    except ImportError:
        pass
    rep["index"] = index_size(Path(a.db) if a.db else d / "index.db")

    h, dt = eng.get("/healthz", timeout=10)
    rep["engine"] = {"health": h.get("text") == "ok", "health_sec": round(dt, 2)}
    if not rep["engine"]["health"]:
        print(f"엔진이 응답하지 않습니다 (포트 {a.port}). `findr serve`를 먼저 켜 주세요.")
        return finish(rep, a, d)
    t0 = time.time()
    while True:
        r, _ = eng.get("/ready", timeout=10)
        if r.get("ready") or time.time() - t0 > a.ready_wait:
            break
        time.sleep(1)
    rep["engine"]["ready"] = bool(r.get("ready"))
    rep["engine"]["ready_wait_sec"] = round(time.time() - t0, 1)
    p, _ = eng.get("/progress", timeout=10)
    rep["engine"]["indexing"] = bool(p) and "error" not in p and "text" not in p

    rows = []
    for rnd in range(a.rounds):
        for q in QUESTIONS:
            r10 = ask_once(eng, q, 10)
            rows.append({"round": rnd, "q": q, **r10})
            print(f"r{rnd} {r10['sec']:5.2f}s  {r10['n']:2d}개  {r10['route'] or '-':4s}  {q}"
                  + (f"  ERROR {r10['error']}" if r10["error"] else ""), flush=True)
    rep["questions"] = rows
    secs = [r["sec"] for r in rows if not r["error"]]
    firsts = [r["sec"] for r in rows if r["round"] == 0 and not r["error"]]
    nfiles = rep["index"].get("files") or 0
    # a cut list is the CPU-default bug (#83); an empty list with a note ('지난달 엑셀이 없습니다') is an answer
    short = [r["q"] for r in rows if not r["error"] and not r["note"] and r["n"] < min(10, nfiles)]
    r30 = ask_once(eng, QUESTIONS[0], 30)
    none = ask_once(eng, NONE_Q, 10)
    rep["summary"] = {
        "asked": len(rows), "errors": sum(bool(r["error"]) for r in rows),
        "median_sec": statistics.median(secs) if secs else None, "p90_sec": pct(secs, 0.9), "max_sec": max(secs) if secs else None,
        "first_round_median_sec": statistics.median(firsts) if firsts else None,
        "wait_ms_max": max((r["wait_ms"] or 0 for r in rows), default=0),
        "short_lists": len(short), "k30_count": r30["n"], "k30_sec": r30["sec"],
        "none_note": none["note"], "none_p1": none["p1"], "none_n": none["n"]}

    if a.ask:
        exe = [sys.executable, "-m", "findr", "ask", QUESTIONS[0]]
        t = time.time()
        pr = subprocess.run(exe, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            cwd=str(Path(__file__).resolve().parents[1]))
        rep["summary"]["ask_sec"] = round(time.time() - t, 1)
        rep["summary"]["ask_ok"] = pr.returncode == 0

    s = rep["summary"]
    checks = {"엔진 준비": rep["engine"]["ready"], "오류 없음": s["errors"] == 0,
              f"느린 10% ≤ {SLOW_P90:g}초": s["p90_sec"] is not None and s["p90_sec"] <= SLOW_P90,
              "결과 개수(k=10)": s["short_lists"] == 0, "결과 개수(k=30)": s["k30_count"] >= min(30, nfiles),
              "없는 파일 질문": s["none_note"] or s["none_n"] == 0 or (s["none_p1"] or 0) < 0.3}
    if a.ask:
        checks["findr ask"] = s["ask_ok"]
    rep["checks"] = checks
    print(f"\n{rep['pc']['os']} · 코어 {rep['pc']['cores']} · 메모리 {rep['pc']['mem_gb']} GB · findr {rep['pc'].get('findr')}"
          f" · 파일 {nfiles:,} · 준비까지 {rep['engine']['ready_wait_sec']} s" + (" · 색인 중" if rep["engine"]["indexing"] else ""))
    print(f"질문 {s['asked']}개: 중앙값 {s['median_sec']} s, 느린 10% {s['p90_sec']} s, 최대 {s['max_sec']} s, 대기 최대 {s['wait_ms_max']} ms"
          + (f", findr ask {s['ask_sec']} s" if a.ask else ""))
    for k, v in checks.items():
        print(f"  {'통과' if v else '확인 필요'}  {k}")
    return finish(rep, a, d)


def finish(rep, a, d):
    out = Path(a.out) if a.out else d / "field_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"결과: {out}")
    return 0 if rep.get("checks") and all(rep["checks"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
