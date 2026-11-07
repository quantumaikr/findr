"""findr - 기억나는 대로 물으면, 내 PC가 찾아 줍니다.
    findr setup              이 PC에 맞는 모델 받기 (처음 한 번)
    findr index [폴더 ...]   색인: 폴더를 주지 않으면 PC 전체의 내 파일 (시스템·프로그램 폴더 제외)
    findr serve              검색 화면 열기 (http://127.0.0.1:8096)
    findr ask "질문"          터미널에서 바로 찾기
    findr doctor             설치 상태 점검"""
import argparse
import atexit
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__, models, paths

PORTS = dict(zip(("qu", "checker", "photo"), map(int, os.environ.get("FINDR_PORTS", "8093,8095,8099").split(","))))   # helper servers (local only)


def _engine_env(photo_url=""):
    """the settings the engine was validated with (the same as on the development PC)"""
    m = paths.MODELS
    env = {"CE_DIR": str(m / "rank"), "NONE_MODEL": str(m / "gate" / "gateCeT2.json"), "NONE_T": "0.9", "CE_NOSUMM": "1", "CE_NONE": "-1",
           "CALIB_P": str(m / "gate" / "calibP.json"), "PERSONAL": "1", "QU_CONTENT": "rules", "FINDR_PROFILE": "pc", "PHOTO_URL": photo_url,
           "QU_URL": f"http://127.0.0.1:{PORTS['qu']}/v1/chat/completions", "EXPLORE_URL": f"http://127.0.0.1:{PORTS['checker']}/v1/chat/completions"}
    for k, v in env.items():
        os.environ.setdefault(k, v)


def _encoder():
    dev, _, want = models.plan()
    if "embeddinggemma-2" in want and models.have("embeddinggemma-2") and models.big_ok():
        return "embeddinggemma-2"
    if not models.have("multilingual-e5-small"):   # the fallback model, fetched once
        models.pull(["multilingual-e5-small"])
    return "e5-small"


def cmd_setup(a):
    dev, gb, want = models.plan(photo=False if a.no_photo else (True if a.photo else None), lite=a.lite)
    print(f"findr {__version__} 설치 | 장치: {dev}{f' ({gb:.0f}GB)' if gb else ''} | 데이터 폴더: {paths.HOME}")
    print("  받을 모델:", ", ".join(want))
    try:
        models.pull(want, token=a.token)
    except Exception as e:
        msg = str(e)
        if "401" in msg or "403" in msg or "Repository Not Found" in msg or "gated" in msg.lower():
            print("\n  모델을 받지 못했습니다(권한 오류). 인터넷 연결을 확인하고 다시 실행하세요:  findr setup")
            print("  회사 망 등에서 막히면 허깅페이스 로그인 후 다시:  hf auth login")
            return 1
        raise
    if not a.no_llm:
        models.pull_llama_server(dev)
    print("\n완료. 다음:  findr index   (PC 전체의 내 파일)   →   findr serve")
    return 0


def cmd_index(a):
    enc = _encoder()
    env = {**os.environ}
    core = paths.CORE
    if not a.paths:   # the whole PC, the person's own files only, in the order people look for them
        cmd = [sys.executable, str(core / "index_pc.py"), "--db", str(a.db), "--model", enc]
        print(f"PC 전체의 내 파일을 색인합니다 (의미 모델: {enc}). 바탕화면·다운로드부터 — 진행 중에도 검색할 수 있습니다.")
        return subprocess.call(cmd, env=env, cwd=str(core))
    rc = 0
    for i, p in enumerate(a.paths):   # in the order given (the app passes the folders people look in first)
        print("FINDR_ROOT " + json.dumps({"i": i, "n": len(a.paths), "path": p}, ensure_ascii=False), flush=True)
        rc |= subprocess.call([sys.executable, str(core / "index.py"), "--root", str(Path(p).expanduser()), "--db", str(a.db), "--model", enc],
                              env=env, cwd=str(core))
        print("FINDR_ROOT_DONE " + json.dumps({"i": i, "n": len(a.paths)}), flush=True)
    return rc


def cmd_roots(a):
    """the folders a whole-PC index would cover, in the order people look in them (for the app's folder list)"""
    import scope
    out = []
    for order, label, p in scope.user_roots():
        out.append({"order": order, "label": label, "path": str(p)})
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _start(name, cmd, log):
    f = open(paths.LOGS / log, "w")
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    atexit.register(lambda: p.poll() is None and p.terminate())
    return p


def _wait(port, path="/health", secs=120):
    import urllib.request
    for _ in range(secs):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2); return True
        except Exception:
            time.sleep(1)
    return False


def cmd_serve(a):
    if not Path(a.db).exists():
        print("아직 색인이 없습니다. 먼저:  findr index"); return 1
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # a polite stop still ends the helper servers (atexit)
    dev, gb, _ = models.plan()
    ls = models.llama_server()
    side = []
    qu = sorted((paths.MODELS / "qu").glob("*.gguf"))
    if ls and qu and not a.no_llm:
        _start("qu", [ls, "-m", str(qu[0]), "--port", str(PORTS["qu"]), "--host", "127.0.0.1", "-ngl", "99", "-np", "4", "-c", "4096",
                      "--temp", "0", "--cache-ram", "512"], "qu_server.log"); side.append("질문 이해")
    ck = sorted((paths.MODELS / "checker").glob("*.gguf"))
    if ls and ck and dev != "cpu" and not a.no_llm:
        _start("checker", [ls, "-m", str(ck[0]), "--jinja", "-ngl", "99", "-c", "16384", "-np", "4", "--port", str(PORTS["checker"]),
                           "--host", "127.0.0.1", "--no-webui"], "checker.log"); side.append("나란히 비교")
    photo_url = ""
    if models.have("kanana-2-v-decide-3b-2611") and dev == "cuda" and not a.no_photo:
        _start("photo", [sys.executable, str(paths.CORE / "photo_srv.py"), "--port", str(PORTS["photo"])], "photo_server.log")
        if _wait(PORTS["photo"], "/", secs=300):
            photo_url = f"http://127.0.0.1:{PORTS['photo']}"; side.append("사진 내용")
    _engine_env(photo_url)
    import search
    f = search.Finder(str(a.db))
    if photo_url:   # photos get their meaning vectors in the background, newest first; search works meanwhile
        def photos():
            import sqlite3
            import index
            index.embed_photos(sqlite3.connect(str(a.db), timeout=60), photo_url)
        threading.Thread(target=photos, daemon=True).start()
    pw = paths.HOME / ".password"
    if not pw.exists():
        import secrets
        pw.write_text(secrets.token_urlsafe(12))
        try:
            pw.chmod(0o600)
        except OSError:
            pass
    url = f"http://127.0.0.1:{a.port}"
    if a.app:   # the desktop app reads the key from the data folder itself; it only needs to know the page is up
        print("FINDR_APP_READY " + json.dumps({"port": a.port, "features": side}, ensure_ascii=False), flush=True)
    else:
        print(f"\nfindr 검색 화면: {url}\n  로그인: 사용자 이름은 아무거나, 비밀번호는 {pw.read_text().strip()}   (비밀번호 파일: {pw})")
        print(f"  함께 켜진 기능: {', '.join(side) or '기본 검색만'}   |  끝내려면 Ctrl+C")
    if not a.no_browser and not a.app:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    search.serve(f, a.port)
    return 0


def cmd_ask(a):
    _engine_env()
    import search
    f = search.Finder(str(a.db))
    r = search.add_probs(f.search(" ".join(a.query), mode="xce", k=a.k))
    if r.get("note"):
        print("ⓘ", r["note"])
    for i, x in enumerate(r.get("results", []), 1):
        pr = f"{round(100 * x['prob'])}%" if x.get("prob") is not None else ""
        print(f"{i:2d}. {pr:>4}  {x['name']}  ({x['date']})\n      {x['path']}")
    return 0


def cmd_doctor(a):
    dev, gb, want = models.plan()
    print(f"findr {__version__} | 파이썬 {sys.version.split()[0]} | 장치 {dev}{f' {gb:.0f}GB' if gb else ''} | 데이터 {paths.HOME}")
    for w in want:
        print(f"  {'✓' if models.have(w) else '✗'} 모델 {w}")
    print(f"  {'✓' if models.llama_server() else '✗'} llama-server (없으면 질문을 규칙으로 읽음)")
    if Path(a.db).exists():
        import sqlite3
        c = sqlite3.connect(str(a.db))
        n = c.execute("SELECT count(*) FROM files").fetchone()[0]
        v = c.execute("SELECT count(*) FROM chunks WHERE emb IS NOT NULL").fetchone()[0]
        try:
            ph = c.execute("SELECT count(*) FROM photo_emb").fetchone()[0]
        except Exception:
            ph = 0
        print(f"  ✓ 색인: 파일 {n:,}개, 의미 벡터 {v:,}개, 사진 벡터 {ph:,}개")
    else:
        print("  ✗ 색인 없음 (findr index)")
    return 0


def main(argv=None):
    paths.setup_env()
    ap = argparse.ArgumentParser(prog="findr", description="기억나는 대로 물으면, 내 PC가 찾아 줍니다.")
    ap.add_argument("--db", default=str(paths.DB), help="색인 파일 (기본: 데이터 폴더의 index.db)")
    ap.add_argument("--version", action="version", version=f"findr {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="이 PC에 맞는 모델 받기")
    s.add_argument("--lite", action="store_true", help="GPU가 있어도 작은 모델만"); s.add_argument("--photo", action="store_true", help="사진 내용 검색 모델도")
    s.add_argument("--no-photo", action="store_true"); s.add_argument("--no-llm", action="store_true", help="llama-server 받지 않기")
    s.add_argument("--token", default=None, help="허깅페이스 토큰 (비공개 모델용)")
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("index", help="색인 (폴더를 주지 않으면 PC 전체의 내 파일)"); s.add_argument("paths", nargs="*"); s.set_defaults(fn=cmd_index)
    s = sub.add_parser("serve", help="검색 화면 열기"); s.add_argument("--port", type=int, default=8096)
    s.add_argument("--no-browser", action="store_true"); s.add_argument("--no-photo", action="store_true"); s.add_argument("--no-llm", action="store_true")
    s.add_argument("--app", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("roots", help="PC 전체 색인 때 볼 폴더 목록 (JSON)"); s.set_defaults(fn=cmd_roots)
    s = sub.add_parser("ask", help="터미널에서 찾기"); s.add_argument("query", nargs="+"); s.add_argument("-k", type=int, default=5); s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("doctor", help="설치 상태 점검"); s.set_defaults(fn=cmd_doctor)
    a = ap.parse_args(argv)
    a.db = Path(a.db).expanduser()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
