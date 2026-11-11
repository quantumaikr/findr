"""findr - 기억나는 대로 물으면, 내 PC가 찾아 줍니다.
    findr setup              이 PC에 맞는 모델 받기 (처음 한 번)
    findr index [폴더 ...]   색인: 폴더를 주지 않으면 PC 전체의 내 파일 (시스템·프로그램 폴더 제외)
    findr serve              검색 화면 열기 (http://127.0.0.1:8096)
    findr ask "질문"          터미널에서 바로 찾기
    findr doctor             설치 상태 점검
    findr uninstall          이 PC에서 findr 데이터 모두 지우기 (내 파일은 그대로)"""
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
    if a.lite:   # kept across plain setups: the app runs `findr setup` on every update, which used to undo the choice
        lite.write_text("1")
    elif getattr(a, "full", False):
        lite.unlink(missing_ok=True)
    print(f"findr {__version__} 설치 | 장치: {dev}{f' ({gb:.0f}GB)' if gb else ''} | 데이터 폴더: {paths.HOME}")
    print("  받을 모델:", ", ".join(want))
    if _disk_short(want, a.token):
        return 5   # not enough room: nothing downloaded (1 is a failed download, 3/4 belong to the upgrade)
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


def _disk_short(want, token=None):
    """before any download: how much is new, how much room the drive has; True (and what to do) when it would not fit.
    Nothing new to fetch (an update): no check, so a nearly full drive never blocks an update"""
    plan_ = models.download_plan(want, token=token)
    have_ = [f for f in want if f not in {f_ for f_, _, _ in plan_}]
    need = sum(b for _, b, _ in plan_)
    if have_:
        print("  이미 있음:", ", ".join(have_))
    if not plan_:
        return False
    est = "" if all(x for _, _, x in plan_) else " (일부는 추정치)"
    size = lambda b: f"{b / 1e9:.1f}GB" if b >= 1e8 else f"{max(b, 1e6) / 1e6:.0f}MB"
    print(f"  새로 받을 용량: 약 {need / 1e9:.1f}GB{est} — " + ", ".join(f"{f} {size(b)}" for f, b, _ in plan_))
    print(f"  디스크 여유: {models.disk_free() / 1e9:.1f}GB (모델 폴더: {paths.MODELS})"
          + ("" if models.disk_free(paths.HOME) == models.disk_free() else f", {models.disk_free(paths.HOME) / 1e9:.1f}GB (데이터 폴더: {paths.HOME})"))
    short = models.room_check(need)   # the download on the models' drive, a first index on the data folder's drive
    if not short or os.environ.get("FINDR_SKIP_DISK_CHECK") == "1":
        return False
    print()
    for what, p, free, need_ in short:
        print(f"  디스크 공간이 모자랍니다 ({what}: {p}): 여유 {free / 1e9:.1f}GB, 필요 약 {need_ / 1e9:.1f}GB")
    print("  공간을 비우거나, 아래 중 하나로 다시 실행해 주세요.")
    whats = {w[0] for w in short}
    if whats & {"모델", "모델과 색인"}:   # the models do not fit: fewer of them, or another drive for them
        small = models.plan(photo=False, lite=True)[2]
        if [f for f in want if f not in small]:   # only where --lite fetches less (a Mac or a CPU PC already gets the small set)
            print(f"  - 작은 모델만:  findr setup --lite   (빠지는 것: {', '.join(f for f in want if f not in small)})")
        print("  - 모델을 다른 드라이브에:  FINDR_MODELS=<여유 있는 폴더> findr setup")
    if whats & {"색인", "모델과 색인"}:   # the index will not fit where the data folder is: the data folder elsewhere
        print("  - 데이터 폴더(색인)를 다른 드라이브에:  FINDR_HOME=<여유 있는 폴더> findr setup")
    w = short[0]
    print(f"  디스크 공간이 모자라 받지 않았습니다 ({w[0]} 드라이브 여유 {w[2] / 1e9:.1f}GB, 필요 약 {w[3] / 1e9:.1f}GB).")
    return True


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
    # absolute: the indexer runs in findr/core, so `findr index .` indexed findr itself and the next run crashed on its paths
    return subprocess.call([sys.executable, str(core / "index.py"), "--root", *[os.path.abspath(Path(p).expanduser()) for p in a.paths],
                            "--db", str(a.db), "--model", enc, *(["--names-only"] if a.names_only else [])], env=env, cwd=str(core))


UPGRADE_TO = "embeddinggemma-2"
UPGRADE_MAX_HOURS = 240   # longer than this in idle time on this PC: not worth the download and the electricity


def _ram_gb():
    return models.ram_gb()


def _index_encoder(db=None):
    """the meaning model the index was built with ('' without an index); read-only, the engine may be writing"""
    import sqlite3
    db = Path(db or paths.DB)
    if not db.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        r = con.execute("SELECT value FROM meta WHERE key='encoder'").fetchone()
        con.close()
        return r[0] if r else ""
    except sqlite3.Error:
        return ""


def _lane2():
    """LANE2=1 (default 0, as in the engine): first pieces by the larger model are kept and filled on slow PCs too"""
    return os.environ.get("LANE2", "0") == "1"


def _too_slow(why):
    """the upgrade is off because the larger model is slow here (not the person's choice, memory or disk)"""
    return why.startswith("이 PC에서") or why.startswith("이 기기에서")


def _upgrade_why_not(db, first_only=False):
    """'' when this index should move to the larger meaning model in idle time, else why not.
    first_only: only each file's first piece is read by the larger model (minutes, not days), so 'too slow' does not count"""
    import shutil
    import sqlite3
    off = paths.HOME / "upgrade_off"
    if off.exists() and not (first_only and _too_slow(off.read_text(encoding="utf-8").strip())):
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
    measured = (paths.HOME / "device_check.json").exists() or models.have(UPGRADE_TO)
    # an Apple GPU is measured after the download (cmd_upgrade): asking before it judged a missing model as slow for good
    if models.gpu()[0] == "mps" and measured and not models.big_ok() and not first_only:   # the self-check chose the small model here (#27)
        try:
            rate = json.loads((paths.HOME / "device_check.json").read_text()).get("pieces_per_s")
        except (OSError, ValueError):
            rate = None
        return f"이 기기에서 큰 모델이 느림 (초당 {rate}조각)" if rate is not None else "이 기기에서 큰 모델이 느림"
    return ""


def cmd_upgrade(a):
    """idle time on a PC that started on the small meaning model: compute the larger model's vectors bit by bit
    (at most --minutes per run), then switch at once. Search keeps working with the small model until the switch."""
    first = bool(getattr(a, "first_only", False))
    why = _upgrade_why_not(a.db)
    if why and _too_slow(why) and (first or _lane2()):
        # the whole upgrade is off on this PC because it would take too long: the first pieces alone still fit (--first-only,
        # or LANE2=1 where the engine reads them as a second meaning lane)
        first = True; why = _upgrade_why_not(a.db, first_only=True)
    if why:
        print(f"큰 모델로 바꾸기: 하지 않음 ({why})")
        # 3: not for this PC (the app stops asking); 4: not now (no index yet, disk full: asked again later)
        return 4 if why == "색인 없음" or why.startswith("디스크") else 3
    if not models.have(UPGRADE_TO):
        print("  더 정확한 의미 모델 받는 중 (한 번, 약 1.2GB) ...", flush=True)
        models.pull([UPGRADE_TO])
    if not first and models.gpu()[0] == "mps" and not models.big_ok():   # measured now, with the model here: slow on this Mac, small stays
        why = _upgrade_why_not(a.db) or "이 기기에서 큰 모델이 느림"
        (paths.HOME / "upgrade_off").write_text(why, encoding="utf-8")
        print(f"큰 모델로 바꾸기: 하지 않음 ({why})")
        if not _lane2():
            return 3
        first = True   # LANE2=1: this same run goes on with the first pieces
    env = {**os.environ}
    if first:   # 6: every first piece is there; 2: more runs needed. Nothing is switched and upgrade.json is not touched
        print("파일 첫 부분만 큰 모델로 읽기 (작은 모델은 그대로)", flush=True)
        return subprocess.call([sys.executable, str(paths.CORE / "index.py"), "--root", str(paths.HOME), "--db", str(a.db),
                                "--upgrade", UPGRADE_TO, "--upgrade-first", "--minutes", str(a.minutes)], env=env, cwd=str(paths.CORE))
    r = subprocess.call([sys.executable, str(paths.CORE / "index.py"), "--root", str(paths.HOME), "--db", str(a.db),
                         "--upgrade", UPGRADE_TO, "--minutes", str(a.minutes)], env=env, cwd=str(paths.CORE))
    st = paths.HOME / "upgrade.json"
    if r == 2 and st.exists():   # paused: give up when the rest would take far too long on this PC
        h = json.loads(st.read_text(encoding="utf-8")).get("hours_left") or 0
        if h > UPGRADE_MAX_HOURS:
            (paths.HOME / "upgrade_off").write_text(f"이 PC에서 {h:.0f}시간 걸림: 작은 모델 유지", encoding="utf-8")
            import sqlite3
            # LANE2=1: the first pieces' vectors stay (1.5 KB a file) for the engine's second meaning lane
            c = sqlite3.connect(str(a.db), timeout=60); c.execute("UPDATE chunks SET emb2=NULL" + (" WHERE n!=0" if _lane2() else "")); c.commit(); c.execute("VACUUM")
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
        # the photo model takes 1-2 min to load. Search does not wait for it (it used to, up to 5 min, also when the server had
        # died): photos are found by name until the server answers, then by what is in them
        ph = _start("photo", [sys.executable, str(paths.CORE / "photo_srv.py"), "--port", str(PORTS["photo"])], "photo_server.log")
        time.sleep(1)
        if ph.poll() is None:
            photo_url = f"http://127.0.0.1:{PORTS['photo']}"; side.append("사진 내용")
        else:
            print(f"  사진 서버가 시작하지 못했습니다 (사진은 이름으로 찾음): {paths.LOGS / 'photo_server.log'}", flush=True)
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
    if a.app:
        os.environ["FINDR_APP"] = "1"   # the page shows "지금 반영" only when the app's refresher is there to answer it
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


def _ask_engine(q, k):
    """the search engine already running for this data folder answers: the same path, numbers and speed as the page, and no
    second copy of the models in memory (#66, #67, #68: ask took 14-29 s and ranked differently). None when it is not up,
    or when the engine on that port belongs to another data folder (its password differs)"""
    import base64
    import urllib.parse
    import urllib.request
    from . import mcp_server
    if not mcp_server._engine_up():
        return None
    key = mcp_server._key()
    req = urllib.request.Request(f"{mcp_server.ENGINE}/api?" + urllib.parse.urlencode({"q": q, "k": k, "mode": "auto"}),
                                 headers={"Cookie": f"findr_t={key}", "Authorization": "Basic " + base64.b64encode(f"findr:{key}".encode()).decode()})
    try:
        return json.loads(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=120).read())
    except Exception:
        return None


def cmd_ask(a):
    q = " ".join(a.query)
    if not Path(a.db).exists():   # Finder would create an empty index.db, and serve and doctor then failed on it
        print("아직 색인이 없습니다. 먼저:  findr index"); return 1
    t = time.time()
    r = None if (a.local or Path(a.db).resolve() != paths.DB.resolve()) else _ask_engine(q, a.k)
    how = "실행 중인 검색 엔진"
    if r is None:   # no engine up: load the models here (slower: every ask loads them again)
        how = "모델을 직접 불러옴"
        _engine_env()
        os.environ.setdefault("FINDR_ENCODER", _encoder())
        import search
        f = search.Finder(str(a.db))
        r = search.add_probs(f.search(q, mode="xce", k=a.k))
    if r.get("note"):
        print("ⓘ", r["note"])
    for i, x in enumerate(r.get("results", []), 1):
        pr = f"{round(100 * x['prob'])}%" if x.get("prob") is not None else ""
        print(f"{i:2d}. {pr:>4}  {x['name']}  ({x.get('date', '')})\n      {x['path']}")
    print(f"  ({time.time() - t:.1f}초, {how})")
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
    try:   # the comfort tier the resource manager starts from (what this PC has)
        sys.path.insert(0, str(paths.CORE)); import governor
        pr = governor.profile(dev, gb)
        print(f"  ✓ 이 PC에 맞춘 등급: {pr['tier_ko']} ({', '.join(pr['reasons'])})")
    except Exception:   # noqa: BLE001
        pass
    for w in want:
        print(f"  {'✓' if models.have(w) else '✗'} 모델 {w}")
    used = _index_encoder()   # the list above is what this PC may have; the index says which meaning model it really uses
    if used:
        why = ""
        try:
            chk = json.loads((paths.HOME / "device_check.json").read_text())
            if not chk.get("big_ok"):
                why = f" (Apple GPU 자가 시험: 큰 모델 초당 {chk.get('pieces_per_s')}조각 → 작은 모델)"
        except (OSError, ValueError):
            pass
        print(f"  ✓ 색인의 의미 모델: {used}{why}")
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


def cmd_uninstall(a):
    """remove everything findr keeps on this PC: one data folder (index, models, engine runtime, settings, logs). The person's
    own files are never touched. A running engine or indexer must be closed first: their files would come back"""
    import shutil
    h = paths.HOME
    if not h.is_dir():
        print(f"지울 데이터가 없습니다: {h}"); return 0
    # a folder set by FINDR_HOME counts only when it holds findr's own things: FINDR_HOME=~ must never empty a home folder
    # (an empty models folder is no sign: every other command creates one at start)
    marks = [m for m in ("index.db", "runtime", "progress.json", "folders.json") if (h / m).exists()]
    if (h / "models").is_dir() and any((h / "models").iterdir()):
        marks.append("models")
    if not (h.name in ("findr", ".findr") and marks or os.environ.get("FINDR_HOME") and marks):
        print(f"findr의 데이터 폴더가 아니라서 지우지 않습니다: {h}"); return 1
    busy = []
    try:
        pid = json.loads((h / "progress.json").read_text(encoding="utf-8")).get("pid")
        if pid and pid != os.getpid():
            os.kill(int(pid), 0); busy.append(f"색인 작업(pid {pid})")
    except (OSError, ValueError, TypeError):
        pass
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:8096/healthz", timeout=1); busy.append("검색 엔진(8096)")
    except Exception:
        pass
    if busy:
        print("먼저 닫아 주세요: " + ", ".join(busy) + ". 앱을 쓰고 있다면 앱을 닫거나 앱의 설정 → findr 지우기를 쓰세요."); return 2
    size = sum(f.stat().st_size for f in h.rglob("*") if f.is_file()) / 2**30
    print(f"지울 폴더: {h} (약 {size:.1f}GB: 색인, 모델, 엔진, 설정, 기록). 내 파일은 지우지 않습니다.")
    if not a.yes and input("정말 지울까요? [y/N] ").strip().lower() not in ("y", "yes", "예", "ㅇ"):
        print("지우지 않았습니다."); return 0
    # the Windows app is installed into this same folder: its files stay for the system's uninstaller (removing them here
    # left 'findr' in Settings > Apps with no uninstaller to run)
    app = [n for n in ("findr-desktop.exe", "uninstall.exe") if (h / n).exists()]
    if app:
        keep = {"findr-desktop.exe", "uninstall.exe", "uv.exe", "engine"}
        for e in list(h.iterdir()):
            if e.name.lower() not in keep:
                shutil.rmtree(e, ignore_errors=True) if e.is_dir() else e.unlink(missing_ok=True)
        left = [e.name for e in h.iterdir() if e.name.lower() not in keep]
        print("데이터를 지웠습니다." if not left else f"일부를 지우지 못했습니다: {', '.join(left[:5])}")
        print("앱도 지우려면 Windows 설정 → 앱 → 설치된 앱에서 findr를 제거하세요(앱 파일은 그때 지워집니다).")
        return 0
    shutil.rmtree(h, ignore_errors=True)
    print("지웠습니다." if not h.exists() else f"일부를 지우지 못했습니다. 이 폴더를 직접 지워 주세요: {h}")
    print("남은 것: 앱이라면 Windows 설정 → 앱 / macOS 응용 프로그램 폴더에서 지우고, 명령줄로 설치했다면 저장소 폴더를 지우면 끝입니다.")
    print("설치에 쓴 uv 캐시(여러 프로그램이 함께 씀)는 그대로 둡니다. 필요하면 `uv cache clean`으로 비울 수 있습니다.")
    return 0


def main(argv=None):
    if (argv if argv is not None else sys.argv[1:])[:1] == ["uninstall"]:   # creates nothing in the folder it is about to judge
        ap = argparse.ArgumentParser(prog="findr uninstall"); ap.add_argument("cmd"); ap.add_argument("--yes", action="store_true")
        return cmd_uninstall(ap.parse_args(argv))
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
    s.add_argument("--lite", action="store_true", help="GPU가 있어도 작은 모델만"); s.add_argument("--full", action="store_true", help="--lite 선택을 되돌림"); s.add_argument("--photo", action="store_true", help="사진 내용 검색 모델도")
    s.add_argument("--no-photo", action="store_true"); s.add_argument("--no-llm", action="store_true", help="llama-server 받지 않기")
    s.add_argument("--token", default=None, help="허깅페이스 토큰 (비공개 모델용)")
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("index", help="색인 (폴더를 주지 않으면 PC 전체의 내 파일)"); s.add_argument("paths", nargs="*")
    s.add_argument("--deep", action="store_true", help="긴 문서의 뒷부분까지 의미 계산 (오래 걸림, 쉬는 시간에 권장)")
    s.add_argument("--names-only", action="store_true", help="새 파일을 이름으로만 반영 (내용은 다음 색인 때, 배터리일 때 앱이 씀)")
    s.set_defaults(fn=cmd_index)
    s = sub.add_parser("serve", help="검색 화면 열기"); s.add_argument("--port", type=int, default=8096)
    s.add_argument("--no-browser", action="store_true"); s.add_argument("--no-photo", action="store_true"); s.add_argument("--no-llm", action="store_true")
    s.add_argument("--app", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("roots", help="PC 전체 색인 때 볼 폴더 목록 (JSON)"); s.set_defaults(fn=cmd_roots)
    s = sub.add_parser("mcp", help="AI 도구(Claude 등)에 findr 연결: MCP 서버 (stdio)"); s.set_defaults(fn=cmd_mcp)
    s = sub.add_parser("ask", help="터미널에서 찾기 (검색 화면이 켜져 있으면 그 엔진에 물음)"); s.add_argument("query", nargs="+")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--local", action="store_true", help="켜진 엔진을 쓰지 않고 모델을 직접 불러오기"); s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("doctor", help="설치 상태 점검"); s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("uninstall", help="이 PC에서 findr 데이터(색인, 모델, 엔진, 설정) 모두 지우기")
    s.add_argument("--yes", action="store_true", help="묻지 않고 지우기"); s.set_defaults(fn=cmd_uninstall)
    s = sub.add_parser("upgrade", help="쉬는 시간에 더 정확한 의미 모델로 조금씩 바꾸기 (GPU 없는 PC)")
    s.add_argument("--minutes", type=float, default=50, help="이번에 쓸 최대 시간 (다음 번에 이어서)"); s.set_defaults(fn=cmd_upgrade)
    s.add_argument("--first-only", action="store_true", help="파일마다 첫 부분만 큰 모델로 읽기 (전체 바꾸기가 너무 느린 PC에서도, 바꾸지는 않음)")
    a = ap.parse_args(argv)
    a.db = Path(a.db).expanduser()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
