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
    lite = paths.HOME / "lite"   # the person chose the small model: no idle-time upgrade either
    lite.write_text("1") if a.lite else lite.unlink(missing_ok=True)
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
    if not a.no_llm and models.qu_ok(dev):   # without a GPU neither small language model runs: no llama.cpp download
        models.pull_llama_server(dev)
    # the components must really load: a missing Windows runtime (#3) used to pass as "done"
    _check_reranker()
    err = _check_runtime()
    if err:
        print(f"\n  준비가 끝나지 않았습니다: {err}")
        if os.name == "nt":
            print("  Windows 구성 요소(Visual C++ 런타임)가 오래되었을 수 있습니다: winget install Microsoft.VCRedist.2015+.x64")
        return 1
    try:   # the text reader for scans and screenshots: the system's own (macOS, Windows with Korean OCR) or EasyOCR (about 100 MB, once)
        import index
        if index.ocr_engine() == "easyocr":
            print("  글자 인식 모델 받는 중 ...", flush=True); index.ocr_reader()
        else:
            print(f"  글자 인식: 운영체제 기본 기능 ({index.ocr_engine()})", flush=True)
    except Exception as e:
        print(f"  글자 인식 모델을 받지 못했습니다 ({type(e).__name__}): 스캔 문서는 이름으로만 찾습니다.")
    print("\n완료. 다음:  findr index   (PC 전체의 내 파일)   →   findr serve")
    return 0


FIRST_PIECES = 8   # first pass: meaning vectors for each file's opening pieces; the rest are found by words until a deep run


def _check_reranker():
    """on a CPU-only PC the int8 reranker must load; if it cannot (onnxruntime missing or broken), fetch the fp32 weights"""
    if models.gpu()[0] != "cpu":
        return ""
    ox = paths.MODELS / "rank" / "model_int8.onnx"
    if not ox.exists():   # set up before the int8 model existed: an update fetches just that file
        try:
            models.pull_rank_int8()
        except Exception as e:
            print(f"  가벼운 재정렬 모델을 받지 못했습니다 ({type(e).__name__}): 전체 모델로 계속합니다", flush=True)
            return "fp32"
    try:
        import onnxruntime as ort
        ort.InferenceSession(str(ox), providers=["CPUExecutionProvider"])
        return "int8"
    except Exception as e:
        print(f"  가벼운 재정렬 모델을 쓸 수 없어 전체 모델을 받습니다 ({type(e).__name__})", flush=True)
        if not (paths.MODELS / "rank" / "model.safetensors").exists():
            models.pull_rank_fp32()
        return "fp32"


def _check_runtime():
    """'' when the compute library and the meaning model load and run here, else what failed"""
    try:
        import torch  # noqa: F401
    except Exception as e:
        return f"연산 라이브러리(torch)를 불러오지 못함 ({type(e).__name__}: {str(e)[:160]})"
    try:
        import enc_eg2
        enc_eg2.make_encoder(_encoder()).emb(["준비 확인"])
    except Exception as e:
        return f"의미 모델을 실행하지 못함 ({type(e).__name__}: {str(e)[:160]})"
    return ""


def cmd_index(a):
    enc = _encoder()
    env = {**os.environ}
    if not a.deep:
        env.setdefault("FINDR_FIRST_PIECES", str(FIRST_PIECES))
    core = paths.CORE
    if not a.paths:   # the whole PC, the person's own files only, in the order people look for them
        cmd = [sys.executable, str(core / "index_pc.py"), "--db", str(a.db), "--model", enc]
        print(f"PC 전체의 내 파일을 색인합니다 (의미 모델: {enc}). 바탕화면·다운로드부터 — 진행 중에도 검색할 수 있습니다.")
        return subprocess.call(cmd, env=env, cwd=str(core))
    print(f"색인: 폴더 {len(a.paths)}개 (이름 → 내용 → 의미 순서, 모든 폴더 함께)", flush=True)
    return subprocess.call([sys.executable, str(core / "index.py"), "--root", *[str(Path(p).expanduser()) for p in a.paths],
                            "--db", str(a.db), "--model", enc], env=env, cwd=str(core))


UPGRADE_TO = "embeddinggemma-2"
UPGRADE_MAX_HOURS = 240   # longer than this in idle time on this PC: not worth the download and the electricity


def _ram_gb():
    return models.ram_gb()


def _upgrade_why_not(db):
    """'' when this index should move to the larger meaning model in idle time, else why not"""
    import shutil
    import sqlite3
    off = paths.HOME / "upgrade_off"
    if off.exists():
        return off.read_text(encoding="utf-8").strip() or "꺼짐"
    if (paths.HOME / "lite").exists():
        return "작은 모델을 직접 고름 (setup --lite)"
    if not Path(db).exists():
        return "색인 없음"
    c = sqlite3.connect(str(db), timeout=60)
    r = c.execute("SELECT value FROM meta WHERE key='encoder'").fetchone()
    if not r or r[0] != "e5-small":
        return "이미 큰 모델" if r and r[0] == UPGRADE_TO else "해당 없음"
    if _ram_gb() and _ram_gb() < 7.5:
        return f"메모리 {_ram_gb():.0f}GB (8GB 이상에서만)"
    if shutil.disk_usage(paths.HOME).free < 3 * 2 ** 30:
        return "디스크 여유 3GB 미만"
    if models.gpu()[0] == "mps" and not models.big_ok():   # the Apple GPU self-check already chose the small model here (#27)
        try:
            rate = json.loads((paths.HOME / "device_check.json").read_text()).get("pieces_per_s")
        except (OSError, ValueError):
            rate = None
        return f"이 기기에서 큰 모델이 느림 (초당 {rate}조각)" if rate is not None else "이 기기에서 큰 모델이 느림"
    return ""


def cmd_upgrade(a):
    """idle time on a PC that started on the small meaning model: compute the larger model's vectors bit by bit
    (at most --minutes per run), then switch at once. Search keeps working with the small model until the switch."""
    why = _upgrade_why_not(a.db)
    if why:
        print(f"큰 모델로 바꾸기: 하지 않음 ({why})"); return 1
    if not models.have(UPGRADE_TO):
        print("  더 정확한 의미 모델 받는 중 (한 번, 약 1.2GB) ...", flush=True)
        models.pull([UPGRADE_TO])
    env = {**os.environ}
    r = subprocess.call([sys.executable, str(paths.CORE / "index.py"), "--root", str(paths.HOME), "--db", str(a.db),
                         "--upgrade", UPGRADE_TO, "--minutes", str(a.minutes)], env=env, cwd=str(paths.CORE))
    st = paths.HOME / "upgrade.json"
    if r == 2 and st.exists():   # paused: give up when the rest would take far too long on this PC
        h = json.loads(st.read_text(encoding="utf-8")).get("hours_left") or 0
        if h > UPGRADE_MAX_HOURS:
            (paths.HOME / "upgrade_off").write_text(f"이 PC에서 {h:.0f}시간 걸림: 작은 모델 유지", encoding="utf-8")
            import sqlite3
            c = sqlite3.connect(str(a.db), timeout=60); c.execute("UPDATE chunks SET emb2=NULL"); c.commit(); c.execute("VACUUM")
            print(f"큰 모델로 바꾸기: 그만둠 (남은 시간 {h:.0f}시간 > {UPGRADE_MAX_HOURS}시간)")
    return r


def cmd_roots(a):
    """the folders a whole-PC index would cover, in the order people look in them (for the app's folder list)"""
    import scope
    out = []
    for order, label, p in scope.user_roots():
        out.append({"order": order, "label": label, "path": str(p)})
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _start(name, cmd, log, env=None):
    f = open(paths.LOGS / log, "w")
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
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
    if not models.qu_ok(dev):   # a CPU reads questions by rules: faster there, same accuracy
        os.environ.setdefault("QU_URL", "off")
    elif ls and qu and not a.no_llm:
        _start("qu", [ls, "-m", str(qu[0]), "--port", str(PORTS["qu"]), "--host", "127.0.0.1", "-ngl", "99", "-np", "4", "-c", "4096",
                      "--temp", "0", "--cache-ram", "512"], "qu_server.log", models.llama_env()); side.append("질문 이해")
    ck = sorted((paths.MODELS / "checker").glob("*.gguf"))
    if ls and ck and models.checker_ok(dev, gb) and not a.no_llm:
        _start("checker", [ls, "-m", str(ck[0]), "--jinja", "-ngl", "99", "-c", "16384", "-np", "4", "--port", str(PORTS["checker"]),
                           "--host", "127.0.0.1", "--no-webui"], "checker.log", models.llama_env()); side.append("나란히 비교")
    else:   # no checker on this PC (or downloaded earlier on a 16 GB Mac): the page skips its second pass
        os.environ.setdefault("FINDR_REFINE", "off")
    photo_url = ""
    if models.have("kanana-2-v-decide-3b-2611") and dev == "cuda" and not a.no_photo:
        _start("photo", [sys.executable, str(paths.CORE / "photo_srv.py"), "--port", str(PORTS["photo"])], "photo_server.log")
        if _wait(PORTS["photo"], "/", secs=300):
            photo_url = f"http://127.0.0.1:{PORTS['photo']}"; side.append("사진 내용")
    _engine_env(photo_url)
    os.environ.setdefault("FINDR_ENCODER", _encoder())   # the planned encoder, for an index still being built (#9)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
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
    if a.app:   # the desktop app reads the key from the data folder itself; it only needs to know the page is up.
        # said by the engine once its socket accepts connections (said earlier, a slow PC showed an empty page)
        os.environ["FINDR_READY_LINE"] = "FINDR_APP_READY " + json.dumps({"port": a.port, "features": side}, ensure_ascii=False)
    else:
        print(f"\nfindr 검색 화면: {url}\n  로그인: 사용자 이름은 아무거나, 비밀번호는 {pw.read_text().strip()}   (비밀번호 파일: {pw})")
        print(f"  함께 켜진 기능: {', '.join(side) or '기본 검색만'}   |  끝내려면 Ctrl+C")
    if not a.no_browser and not a.app:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    search.serve(f, a.port)
    return 0


def cmd_mcp(a):
    """AI tools on this PC find the person's files through findr (search_files, read_file). Speaks MCP on stdin/stdout."""
    from .mcp_server import main as serve_mcp
    serve_mcp()
    return 0


def cmd_ask(a):
    _engine_env()
    os.environ.setdefault("FINDR_ENCODER", _encoder())
    import search
    f = search.Finder(str(a.db))
    r = search.add_probs(f.search(" ".join(a.query), mode="xce", k=a.k))
    if r.get("note"):
        print("ⓘ", r["note"])
    for i, x in enumerate(r.get("results", []), 1):
        pr = f"{round(100 * x['prob'])}%" if x.get("prob") is not None else ""
        print(f"{i:2d}. {pr:>4}  {x['name']}  ({x['date']})\n      {x['path']}")
    return 0


def _missing_deps():
    """pyproject.toml's dependencies that this environment lacks (platform markers honoured)"""
    import re as _re
    from importlib import metadata
    pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pp.exists():
        return []
    try:
        from packaging.requirements import Requirement
    except ImportError:
        return []
    s = pp.read_text(encoding="utf-8")
    block = _re.search(r"dependencies\s*=\s*\[(.*?)\n\]", s, _re.S)
    out = []
    for line in (block.group(1).splitlines() if block else []):
        m = _re.match(r'\s*"([^"]+)"', line)
        if not m:
            continue
        try:
            req = Requirement(m.group(1).split(" @ ")[0] if " @ " in m.group(1) else m.group(1))
        except Exception:
            continue
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            metadata.version(req.name)
        except metadata.PackageNotFoundError:
            out.append(req.name)
    return out


def _reinstall_cmd():
    root = Path(__file__).resolve().parent.parent
    return f'uv pip install --python "{sys.executable}" -e "{root}"   (uv가 없으면: "{sys.executable}" -m pip install -e "{root}")'


def cmd_doctor(a):
    dev, gb, want = models.plan()
    print(f"findr {__version__} | 파이썬 {sys.version.split()[0]} | 장치 {dev}{f' {gb:.0f}GB' if gb else ''} | 데이터 {paths.HOME}")
    try:
        import torch
        print(f"  ✓ 연산 라이브러리 torch {torch.__version__}")
    except Exception as e:
        print(f"  ✗ 연산 라이브러리를 불러오지 못함: {type(e).__name__}: {str(e)[:160]}")
        if os.name == "nt":
            print("    → Visual C++ 런타임 업데이트: winget install Microsoft.VCRedist.2015+.x64")
    for w in want:
        print(f"  {'✓' if models.have(w) else '✗'} 모델 {w}")
    if dev == "cpu":
        ox = (paths.MODELS / "rank" / "model_int8.onnx").exists()
        print(f"  {'✓' if ox or (paths.MODELS / 'rank' / 'model.safetensors').exists() else '✗'} 재정렬: {'가벼운 8비트 (CPU용)' if ox else '전체 정밀도'}")
    if models.qu_ok(dev):
        print(f"  {'✓' if models.llama_server() else '✗'} llama-server (없으면 질문을 규칙으로 읽음)")
    else:
        print("  ✓ 질문 읽기: 규칙 (GPU가 없으면 이쪽이 빠르고 정확도는 같음)")
    if dev == "cuda" and models.llama_server():   # a CUDA build that finds no GPU still runs, ~5x slower, and says nothing
        try:
            out = subprocess.run([models.llama_server(), "--list-devices"], capture_output=True, text=True, timeout=60, env=models.llama_env()).stdout
        except Exception:
            out = ""
        gpu = "CUDA" in out
        print(f"  {'✓' if gpu else '✗'} llama-server 장치: {'GPU' if gpu else 'CPU (GPU를 못 찾음: 질문 이해가 느려짐)'}")
    miss = _missing_deps()
    if miss:   # #31: an update by `git pull` does not install what was added since
        print(f"  ✗ 빠진 구성 요소 {len(miss)}개: {', '.join(miss)}")
        print(f"    → 설치:  {_reinstall_cmd()}")
    else:
        print("  ✓ 구성 요소 모두 설치됨")
    try:
        import index
        e = index.ocr_engine()
        if e == "none":
            raise ImportError("easyocr")
        print(f"  ✓ 글자 인식: {dict(vision='macOS 기본', windows='Windows 기본 (한국어)', easyocr='EasyOCR').get(e, e)}")
        if e == "easyocr" and os.name == "nt":
            print("    → 더 빠르게: Windows 한국어 글자 인식 설치 (관리자 PowerShell)  Add-WindowsCapability -Online -Name \"Language.OCR~~~ko-KR~0.0.1.0\"")
    except ImportError:
        print(f"  ✗ 글자 인식 없음 (스캔·스크린샷은 이름으로만 찾음) → 설치:  {_reinstall_cmd()}")
    except Exception as e:
        print(f"  ✗ 글자 인식 확인 실패: {type(e).__name__}")
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
        print("  ○ 아직 색인 전입니다 → findr index")
    return 0


def main(argv=None):
    paths.setup_env()
    try:   # company networks that inspect TLS: model downloads trust the system's certificates
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="findr", description="기억나는 대로 물으면, 내 PC가 찾아 줍니다.")
    ap.add_argument("--db", default=str(paths.DB), help="색인 파일 (기본: 데이터 폴더의 index.db)")
    ap.add_argument("--version", action="version", version=f"findr {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="이 PC에 맞는 모델 받기")
    s.add_argument("--lite", action="store_true", help="GPU가 있어도 작은 모델만"); s.add_argument("--photo", action="store_true", help="사진 내용 검색 모델도")
    s.add_argument("--no-photo", action="store_true"); s.add_argument("--no-llm", action="store_true", help="llama-server 받지 않기")
    s.add_argument("--token", default=None, help="허깅페이스 토큰 (비공개 모델용)")
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("index", help="색인 (폴더를 주지 않으면 PC 전체의 내 파일)"); s.add_argument("paths", nargs="*")
    s.add_argument("--deep", action="store_true", help="긴 문서의 뒷부분까지 의미 계산 (오래 걸림, 쉬는 시간에 권장)")
    s.set_defaults(fn=cmd_index)
    s = sub.add_parser("serve", help="검색 화면 열기"); s.add_argument("--port", type=int, default=8096)
    s.add_argument("--no-browser", action="store_true"); s.add_argument("--no-photo", action="store_true"); s.add_argument("--no-llm", action="store_true")
    s.add_argument("--app", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("roots", help="PC 전체 색인 때 볼 폴더 목록 (JSON)"); s.set_defaults(fn=cmd_roots)
    s = sub.add_parser("mcp", help="AI 도구(Claude 등)에 findr 연결: MCP 서버 (stdio)"); s.set_defaults(fn=cmd_mcp)
    s = sub.add_parser("ask", help="터미널에서 찾기"); s.add_argument("query", nargs="+"); s.add_argument("-k", type=int, default=5); s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("doctor", help="설치 상태 점검"); s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("upgrade", help="쉬는 시간에 더 정확한 의미 모델로 조금씩 바꾸기 (GPU 없는 PC)")
    s.add_argument("--minutes", type=float, default=50, help="이번에 쓸 최대 시간 (다음 번에 이어서)"); s.set_defaults(fn=cmd_upgrade)
    a = ap.parse_args(argv)
    a.db = Path(a.db).expanduser()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
