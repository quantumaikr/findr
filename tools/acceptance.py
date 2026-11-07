"""Acceptance check against a running `findr serve`: questions with known answers -> rank, pass, latency.
    python tools/acceptance.py [--questions FILE] [--port 8096] [--rounds 1] [--slow SECONDS] [--k 10]
Questions hold personal file names, so they live outside the repository (default: <data folder>/acceptance.json):
    [{"q": "작년에 받은 견적서", "expect": ["견적서"]}, {"q": "없는 파일 질문", "expect": []}, ...]
`expect` lists substrings of the right file's name (NFC/NFD both match); an empty list means "should find nothing".
Pass = a right file within the top 3, or for an empty `expect`, a 'not found' note or nothing above 30 %.
--slow N: on macOS, a request slower than N s gets `sample` of the server written to acceptance_slow/ (look before guessing)."""
import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

N = lambda s: unicodedata.normalize("NFC", s or "")


def data_dir():
    h = os.environ.get("FINDR_HOME")
    if h:
        return Path(h).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "findr"
    return Path.home() / ".findr"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(data_dir() / "acceptance.json"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("FINDR_APP_PORT", 8096)))
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--slow", type=float, default=0, help="sample the server when a request takes longer (macOS)")
    ap.add_argument("--k", type=int, default=10)
    a = ap.parse_args()
    cases = json.loads(Path(a.questions).read_text(encoding="utf-8"))
    pw = (data_dir() / ".password").read_text().strip()
    hdr = {"Authorization": "Basic " + base64.b64encode(f"acceptance:{pw}".encode()).decode(), "X-Findr": "1"}
    srv = None
    if a.slow and sys.platform == "darwin":
        out = subprocess.run(["lsof", f"-tiTCP:{a.port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
        srv = out[0] if out else None
        Path("acceptance_slow").mkdir(exist_ok=True)

    rows = []
    for rnd in range(a.rounds):
        for i, c in enumerate(cases, 1):
            res, done = {}, threading.Event()

            def ask():
                url = f"http://127.0.0.1:{a.port}/api?" + urllib.parse.urlencode({"q": c["q"], "mode": "auto", "k": a.k})
                t = time.time()
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=600) as r:
                        res["body"] = json.loads(r.read().decode())
                except urllib.error.HTTPError as e:
                    res["body"] = {"error": f"HTTP {e.code}"}
                except Exception as e:   # noqa: BLE001  (report, keep going)
                    res["body"] = {"error": repr(e)}
                res["dt"] = time.time() - t
                done.set()

            threading.Thread(target=ask, daemon=True).start()
            sampled = ""
            if srv and not done.wait(a.slow):
                f = Path("acceptance_slow") / f"r{rnd}_q{i}.txt"
                subprocess.run(["sample", srv, "4", "-mayDie", "-file", str(f)], capture_output=True)
                sampled = str(f)
            done.wait()
            body, exp = res["body"], c.get("expect", [])
            found = body.get("results") or []
            rank = next((j for j, r in enumerate(found, 1) if exp and any(N(e) in N(r.get("name")) for e in exp)), None)
            p1 = found[0].get("prob") if found else None
            ok = (rank is not None and rank <= 3) if exp else (not found or bool(body.get("note")) or (p1 is not None and p1 < 0.3))
            rows.append({"round": rnd, "i": i, "q": c["q"], "ok": ok, "rank": rank, "sec": round(res["dt"], 1),
                         "top1": N(found[0]["name"]) if found else "", "p1": p1, "error": body.get("error"), "sample": sampled})
            print(f"r{rnd} {i:2d} {'PASS' if ok else 'FAIL'} rank={rank} {res['dt']:5.1f}s  {c['q'][:30]}"
                  + (f"  ERROR {body['error']}" if body.get("error") else "") + (f"  -> {sampled}" if sampled else ""), flush=True)

    secs = sorted(r["sec"] for r in rows)
    with_answer = [r for r in rows if cases[r["i"] - 1].get("expect")]
    print(f"\nPASS {sum(r['ok'] for r in rows)}/{len(rows)} | rank 1 {sum(r['rank'] == 1 for r in with_answer)}/{len(with_answer)}"
          f" | median {secs[len(secs) // 2]} s, max {secs[-1]} s" + (f" | >{a.slow:g} s: {sum(s > a.slow for s in secs)}" if a.slow else ""))
    Path("acceptance_result.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
