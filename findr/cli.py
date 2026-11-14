"""findr - 기억나는 대로 물으면, 내 PC가 찾아 줍니다.
    findr setup              이 PC에 맞는 모델 받기 (처음 한 번)
    findr index [폴더 ...]   색인: 폴더를 주지 않으면 PC 전체의 내 파일 (시스템·프로그램 폴더 제외)
    findr serve              검색 화면 열기 (http://127.0.0.1:8096)
    findr ask "질문"          터미널에서 바로 찾기
    findr doctor             설치 상태 점검
    findr uninstall          이 PC에서 findr 데이터 모두 지우기 (내 파일은 그대로)

findr - ask the way you remember it, and your PC finds the file.
    findr setup              download the models that fit this PC (once)
    findr index [folder ...] index: without folders, your own files on the whole PC (system and program folders left out)
    findr serve              open the search page (http://127.0.0.1:8096)
    findr ask "question"     search from the terminal
    findr doctor             check the installation
    findr uninstall          remove all findr data from this PC (your own files stay)

Messages come in Korean or English: FINDR_LANG=ko|en, else the system's language (findr/lang.py)."""
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
from .lang import L, lang

PORTS = dict(zip(("qu", "checker", "photo"), map(int, os.environ.get("FINDR_PORTS", "8093,8095,8099").split(","))))   # helper servers (local only)


def _engine_env(photo_url=""):
    """the settings the engine was validated with (the same as on the development PC)"""
    m = paths.MODELS
    env = {"CE_DIR": str(m / "rank"), "NONE_MODEL": str(m / "gate" / "gateCeT2.json"), "NONE_T": "0.9", "CE_NOSUMM": "1", "CE_NONE": "-1",
           "CALIB_P": str(m / "gate" / "calibP.json"), "PERSONAL": "1", "QU_CONTENT": "rules", "FINDR_PROFILE": "pc", "PHOTO_URL": photo_url,
           "QU_URL": f"http://127.0.0.1:{PORTS['qu']}/v1/chat/completions", "EXPLORE_URL": f"http://127.0.0.1:{PORTS['checker']}/v1/chat/completions"}
    for k, v in env.items():
        os.environ.setdefault(k, v)
    os.environ.setdefault("LANE2", "1" if _lane2() else "0")   # decided here once: the engine itself only reads the switch


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
    print(L(f"findr {__version__} 설치 | 장치: {dev}{f' ({gb:.0f}GB)' if gb else ''} | 데이터 폴더: {paths.HOME}", f"findr {__version__} setup | device: {dev}{f' ({gb:.0f}GB)' if gb else ''} | data folder: {paths.HOME}"))
    print(L("  받을 모델:", "  Models to download:"), ", ".join(want))
    if _disk_short(want, a.token):
        return 5   # not enough room: nothing downloaded (1 is a failed download, 3/4 belong to the upgrade)
    try:
        models.pull(want, token=a.token)
    except Exception as e:
        msg = str(e)
        if "401" in msg or "403" in msg or "Repository Not Found" in msg or "gated" in msg.lower():
            print(L("\n  모델을 받지 못했습니다(권한 오류). 인터넷 연결을 확인하고 다시 실행하세요:  findr setup", "\n  Could not download the models (access denied). Check your internet connection and run again:  findr setup"))
            print(L("  회사 망 등에서 막히면 허깅페이스 로그인 후 다시:  hf auth login", "  If a company network blocks the download, log in to Hugging Face and run it again:  hf auth login"))
            return 1
        raise
    if not a.no_llm and models.qu_ok(dev):   # without a GPU neither small language model runs: no llama.cpp download
        models.pull_llama_server(dev)
    # the components must really load: a missing Windows runtime (#3) used to pass as "done"
    _check_reranker()
    err = _check_runtime()
    if err:
        print(L(f"\n  준비가 끝나지 않았습니다: {err}", f"\n  Setup did not finish: {err}"))
        if os.name == "nt":
            print(L("  Windows 구성 요소(Visual C++ 런타임)가 오래되었을 수 있습니다: winget install Microsoft.VCRedist.2015+.x64", "  A Windows component (the Visual C++ runtime) may be out of date: winget install Microsoft.VCRedist.2015+.x64"))
        return 1
    try:   # the text reader for scans and screenshots: the system's own (macOS, Windows with Korean OCR) or EasyOCR (about 100 MB, once)
        import index
        if index.ocr_engine() == "easyocr":
            print(L("  글자 인식 모델 받는 중 ...", "  Downloading the text recognition model ..."), flush=True); index.ocr_reader()
        else:
            print(L(f"  글자 인식: 운영체제 기본 기능 ({index.ocr_engine()})", f"  Text recognition: built into the operating system ({index.ocr_engine()})"), flush=True)
    except Exception as e:
        print(L(f"  글자 인식 모델을 받지 못했습니다 ({type(e).__name__}): 스캔 문서는 이름으로만 찾습니다.", f"  Could not download the text recognition model ({type(e).__name__}): scanned documents are found by name only."))
    print(L("\n완료. 다음:  findr index   (PC 전체의 내 파일)   →   findr serve", "\nDone. Next:  findr index   (your files on the whole PC)   →   findr serve"))
    return 0


def _disk_short(want, token=None):
    """before any download: how much is new, how much room the drive has; True (and what to do) when it would not fit.
    Nothing new to fetch (an update): no check, so a nearly full drive never blocks an update"""
    plan_ = models.download_plan(want, token=token)
    have_ = [f for f in want if f not in {f_ for f_, _, _ in plan_}]
    need = sum(b for _, b, _ in plan_)
    if have_:
        print(L("  이미 있음:", "  Already here:"), ", ".join(have_))
    if not plan_:
        return False
    est = "" if all(x for _, _, x in plan_) else L(" (일부는 추정치)", " (partly estimated)")
    size = lambda b: f"{b / 1e9:.1f}GB" if b >= 1e8 else f"{max(b, 1e6) / 1e6:.0f}MB"
    print(L(f"  새로 받을 용량: 약 {need / 1e9:.1f}GB{est} — ", f"  To download: about {need / 1e9:.1f}GB{est} — ") + ", ".join(f"{f} {size(b)}" for f, b, _ in plan_))
    print(L(f"  디스크 여유: {models.disk_free() / 1e9:.1f}GB (모델 폴더: {paths.MODELS})", f"  Free disk space: {models.disk_free() / 1e9:.1f}GB (models folder: {paths.MODELS})")
          + ("" if models.disk_free(paths.HOME) == models.disk_free() else
             L(f", {models.disk_free(paths.HOME) / 1e9:.1f}GB (데이터 폴더: {paths.HOME})", f", {models.disk_free(paths.HOME) / 1e9:.1f}GB (data folder: {paths.HOME})")))
    short = models.room_check(need)   # the download on the models' drive, a first index on the data folder's drive
    if not short or os.environ.get("FINDR_SKIP_DISK_CHECK") == "1":
        return False
    print()
    for what, p, free, need_ in short:
        print(L(f"  디스크 공간이 모자랍니다 ({what}: {p}): 여유 {free / 1e9:.1f}GB, 필요 약 {need_ / 1e9:.1f}GB", f"  Not enough disk space ({_WHAT_EN.get(what, what)}: {p}): {free / 1e9:.1f}GB free, about {need_ / 1e9:.1f}GB needed"))
    print(L("  공간을 비우거나, 아래 중 하나로 다시 실행해 주세요.", "  Free up some space, or run again with one of these."))
    whats = {w[0] for w in short}
    if whats & {"모델", "모델과 색인"}:   # the models do not fit: fewer of them, or another drive for them
        small = models.plan(photo=False, lite=True)[2]
        if [f for f in want if f not in small]:   # only where --lite fetches less (a Mac or a CPU PC already gets the small set)
            print(L(f"  - 작은 모델만:  findr setup --lite   (빠지는 것: {', '.join(f for f in want if f not in small)})", f"  - Small models only:  findr setup --lite   (left out: {', '.join(f for f in want if f not in small)})"))
        print(L("  - 모델을 다른 드라이브에:  FINDR_MODELS=<여유 있는 폴더> findr setup", "  - Models on another drive:  FINDR_MODELS=<folder with room> findr setup"))
    if whats & {"색인", "모델과 색인"}:   # the index will not fit where the data folder is: the data folder elsewhere
        print(L("  - 데이터 폴더(색인)를 다른 드라이브에:  FINDR_HOME=<여유 있는 폴더> findr setup", "  - Data folder (index) on another drive:  FINDR_HOME=<folder with room> findr setup"))
    w = short[0]
    print(L(f"  디스크 공간이 모자라 받지 않았습니다 ({w[0]} 드라이브 여유 {w[2] / 1e9:.1f}GB, 필요 약 {w[3] / 1e9:.1f}GB).", f"  Nothing was downloaded: not enough disk space ({_WHAT_EN.get(w[0], w[0])} drive: {w[2] / 1e9:.1f}GB free, about {w[3] / 1e9:.1f}GB needed)."))
    return True


_WHAT_EN = {"모델": "models", "색인": "index", "모델과 색인": "models and index"}   # models.room_check()'s names, said in English


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
            print(L(f"  가벼운 재정렬 모델을 받지 못했습니다 ({type(e).__name__}): 전체 모델로 계속합니다", f"  Could not download the light reranker ({type(e).__name__}): continuing with the full model"), flush=True)
            return "fp32"
    try:
        import onnxruntime as ort
        ort.InferenceSession(str(ox), providers=["CPUExecutionProvider"])
        return "int8"
    except Exception as e:
        print(L(f"  가벼운 재정렬 모델을 쓸 수 없어 전체 모델을 받습니다 ({type(e).__name__})", f"  The light reranker cannot run here: downloading the full model ({type(e).__name__})"), flush=True)
        if not (paths.MODELS / "rank" / "model.safetensors").exists():
            models.pull_rank_fp32()
        return "fp32"


def _check_runtime():
    """'' when the compute library and the meaning model load and run here, else what failed"""
    try:
        import torch  # noqa: F401
    except Exception as e:
        return L(f"연산 라이브러리(torch)를 불러오지 못함 ({type(e).__name__}: {str(e)[:160]})", f"could not load the compute library (torch) ({type(e).__name__}: {str(e)[:160]})")
    try:
        import enc_eg2
        enc_eg2.make_encoder(_encoder()).emb(["준비 확인"])
    except Exception as e:
        return L(f"의미 모델을 실행하지 못함 ({type(e).__name__}: {str(e)[:160]})", f"could not run the meaning model ({type(e).__name__}: {str(e)[:160]})")
    return ""


def cmd_index(a):
    enc = _encoder()
    env = {**os.environ}
    if not a.deep:
        env.setdefault("FINDR_FIRST_PIECES", str(FIRST_PIECES))
    core = paths.CORE
    if not a.paths:   # the whole PC, the person's own files only, in the order people look for them
        cmd = [sys.executable, str(core / "index_pc.py"), "--db", str(a.db), "--model", enc, *(["--gone-only"] if getattr(a, "gone_only", False) else [])]
        print(L(f"PC 전체의 내 파일을 색인합니다 (의미 모델: {enc}). 바탕화면·다운로드부터 — 진행 중에도 검색할 수 있습니다.", f"Indexing your files on the whole PC (meaning model: {enc}). Desktop and Downloads first — you can search while it runs."))
        return subprocess.call(cmd, env=env, cwd=str(core))
    print(L(f"색인: 폴더 {len(a.paths)}개 (이름 → 내용 → 의미 순서, 모든 폴더 함께)", f"Index: {len(a.paths)} folder{'' if len(a.paths) == 1 else 's'} (names → content → meaning, all folders together)"), flush=True)
    # absolute: the indexer runs in findr/core, so `findr index .` indexed findr itself and the next run crashed on its paths
    return subprocess.call([sys.executable, str(core / "index.py"), "--root", *[os.path.abspath(Path(p).expanduser()) for p in a.paths],
                            "--db", str(a.db), "--model", enc, *(["--names-only"] if a.names_only else []),
                            *(["--gone-only"] if getattr(a, "gone_only", False) else [])], env=env, cwd=str(core))


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


LANE2_RAM_GB = 11.5   # a PC sold as 12 GB reports a little less
LANE2_FREE_GB = 5.0   # free when the engine starts: the engine (2.7 GB) and the lane (1.2 GB) still leave the person room


def _lane2(free=None):
    """the second meaning lane (first pieces by the larger model are kept, filled on slow PCs too, and read by the engine).
    LANE2=1 on, LANE2=0 off; not set: on where the PC has the memory for a second encoder. The engine holds 1.2 GB more
    on a CPU, and an 8 GB laptop had 1.5 GB free with the engine running, so: 12 GB or more, and free now. A 16 GB laptop
    with a video and a browser open froze while findr searched and indexed (field, 11-13): what the PC has is not what is
    left for findr. The engine also lets the lane go when memory turns critical (search.py, governor act)"""
    v = os.environ.get("LANE2", "")
    if v in ("0", "1"):
        return v == "1"
    if _ram_gb() < LANE2_RAM_GB:
        return False
    if free is None:
        try:
            if str(paths.CORE) not in sys.path:
                sys.path.insert(0, str(paths.CORE))
            from governor import mem_available_gb
            free = mem_available_gb()
        except Exception:   # noqa: BLE001
            free = None
    return free is None or free >= LANE2_FREE_GB


def _too_slow(why):
    """the upgrade is off because the larger model is slow here (not the person's choice, memory or disk)"""
    return why.startswith("이 PC에서") or why.startswith("이 기기에서")


def _why_say(why):
    """the reason for no upgrade as the person reads it. The reason itself stays Korean: it is tested by its opening words
    (_too_slow, cmd_upgrade's exit code) and kept in the data folder's upgrade_off"""
    if lang() != "en":
        return why
    import re
    fixed = {"꺼짐": "off", "작은 모델을 직접 고름 (setup --lite)": "small model chosen (setup --lite)", "색인 없음": "no index yet",
             "이미 큰 모델": "already on the larger model", "해당 없음": "not needed here", "디스크 여유 3GB 미만": "less than 3GB of free disk space",
             "이 기기에서 큰 모델이 느림": "the larger model is slow on this device"}
    if why in fixed:
        return fixed[why]
    for pat, en in ((r"메모리 (\S+)GB \(8GB 이상에서만\)", r"\1GB of memory (8GB or more needed)"),
                    (r"이 기기에서 큰 모델이 느림 \(초당 (\S+)조각\)", r"the larger model is slow on this device (\1 pieces/s)"),
                    (r"이 PC에서 (\S+)시간 걸림: 작은 모델 유지", r"would take \1 hours on this PC: staying on the small model")):
        if re.fullmatch(pat, why):
            return re.sub(pat, en, why)
    return why


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
        # or the second meaning lane is on and the engine reads them)
        first = True; why = _upgrade_why_not(a.db, first_only=True)
    if why:
        print(L(f"큰 모델로 바꾸기: 하지 않음 ({why})", f"Switch to the larger model: skipped ({_why_say(why)})"))
        # 3: not for this PC (the app stops asking); 4: not now (no index yet, disk full: asked again later)
        return 4 if why == "색인 없음" or why.startswith("디스크") else 3
    if not models.have(UPGRADE_TO):
        print(L("  더 정확한 의미 모델 받는 중 (한 번, 약 1.2GB) ...", "  Downloading the more accurate meaning model (once, about 1.2GB) ..."), flush=True)
        models.pull([UPGRADE_TO])
    if not first and models.gpu()[0] == "mps" and not models.big_ok():   # measured now, with the model here: slow on this Mac, small stays
        why = _upgrade_why_not(a.db) or "이 기기에서 큰 모델이 느림"
        (paths.HOME / "upgrade_off").write_text(why, encoding="utf-8")
        print(L(f"큰 모델로 바꾸기: 하지 않음 ({why})", f"Switch to the larger model: skipped ({_why_say(why)})"))
        if not _lane2():
            return 3
        first = True   # second lane on: this same run goes on with the first pieces
    env = {**os.environ}
    if first:   # 6: every first piece is there; 2: more runs needed. Nothing is switched and upgrade.json is not touched
        print(L("파일 첫 부분만 큰 모델로 읽기 (작은 모델은 그대로)", "Reading only the first part of each file with the larger model (the small model stays)"), flush=True)
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
            # second lane on: the first pieces' vectors stay (1.5 KB a file) for the engine
            c = sqlite3.connect(str(a.db), timeout=60); c.execute("UPDATE chunks SET emb2=NULL" + (" WHERE n!=0" if _lane2() else "")); c.commit()
            _vacuum(c, a.db)
            print(L(f"큰 모델로 바꾸기: 그만둠 (남은 시간 {h:.0f}시간 > {UPGRADE_MAX_HOURS}시간)", f"Switch to the larger model: given up ({h:.0f} hours left > {UPGRADE_MAX_HOURS} hours)"))
    return r


def _vacuum(c, db):
    """give the space of dropped vectors back to the disk, when there is room for it: VACUUM writes a copy of the index and,
    with the engine reading, as much again into the write-ahead file. On a nearly full drive it failed half way"""
    import shutil
    import sqlite3
    try:
        size = Path(db).stat().st_size
        free = shutil.disk_usage(Path(db).parent).free
    except OSError:
        return False
    if free < 2 * size + 2 ** 30:
        print(L(f"  색인 정리(VACUUM)는 건너뜀: 디스크 여유 {free / 2 ** 30:.1f}GB", f"  Index compaction (VACUUM) skipped: {free / 2 ** 30:.1f} GB free"), flush=True)
        return False
    try:
        c.execute("PRAGMA journal_size_limit=67108864")
        c.execute("VACUUM")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.Error as e:
        print(f"  VACUUM failed ({type(e).__name__}): the index is unchanged", flush=True)
        return False


_LABEL_EN = {"바탕화면": "Desktop", "다운로드": "Downloads", "문서": "Documents", "클라우드": "Cloud", "사진": "Pictures", "동영상": "Videos",
             "음악": "Music", "홈 폴더 기타": "Other home folders"}


def _label_say(label):
    """scope.py's name for a kind of folder, as the person reads it (the app's folder list only shows it)"""
    if lang() != "en":
        return label
    if label.endswith(": 기타 폴더"):   # 'D: 기타 폴더'
        return label[:-len("기타 폴더")] + "other folders"
    return _LABEL_EN.get(label, label)


def cmd_roots(a):
    """the folders a whole-PC index would cover, in the order people look in them (for the app's folder list)"""
    import scope
    out = []
    for order, label, p in scope.user_roots():
        out.append({"order": order, "label": _label_say(label), "path": str(p)})
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _start(name, cmd, log, env=None):
    f = open(paths.LOGS / log, "w", encoding="utf-8")
    env = {**(os.environ if env is None else env), "PYTHONIOENCODING": "utf-8:replace"}   # a log is read as UTF-8
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
        print(L("아직 색인이 없습니다. 먼저:  findr index", "No index yet. First:  findr index")); return 1
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
            print(L(f"  사진 서버가 시작하지 못했습니다 (사진은 이름으로 찾음): {paths.LOGS / 'photo_server.log'}", f"  The photo server did not start (photos are found by name): {paths.LOGS / 'photo_server.log'}"), flush=True)
    _engine_env(photo_url)
    os.environ.setdefault("FINDR_ENCODER", _encoder())   # the planned encoder, for an index still being built (#9)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    import search
    f = search.Finder(str(a.db))
    if photo_url:   # photos get their meaning vectors in the background, newest first; search works meanwhile
        def photos():
            import sqlite3
            import index
            try:   # the index stayed locked past every retry: one line, and photos are found by name until the next start
                index.embed_photos(sqlite3.connect(str(a.db), timeout=60), photo_url)
            except Exception as e:   # noqa: BLE001
                print(f"FINDR_PHOTO stopped ({type(e).__name__}): photos are found by name for now", flush=True)
        threading.Thread(target=photos, daemon=True).start()
    pw = paths.HOME / ".password"
    search.read_or_make_password(pw)   # missing or empty: made anew, whole (an empty file locked every request out)
    url = f"http://127.0.0.1:{a.port}"
    if a.app:
        os.environ["FINDR_APP"] = "1"   # the page shows "지금 반영" only when the app's refresher is there to answer it
    if a.app:   # the desktop app reads the key from the data folder itself; it only needs to know the page is up.
        # said by the engine once its socket accepts connections (said earlier, a slow PC showed an empty page)
        os.environ["FINDR_READY_LINE"] = "FINDR_APP_READY " + json.dumps({"port": a.port, "features": side}, ensure_ascii=False)
    else:
        print(L(f"\nfindr 검색 화면: {url}\n  로그인: 사용자 이름은 아무거나, 비밀번호는 {pw.read_text().strip()}   (비밀번호 파일: {pw})", f"\nfindr search page: {url}\n  Log in: any user name, password {pw.read_text().strip()}   (password file: {pw})"))
        side_en = [{"질문 이해": "question understanding", "나란히 비교": "side-by-side comparison", "사진 내용": "photo content"}.get(x, x) for x in side]
        print(L(f"  함께 켜진 기능: {', '.join(side) or '기본 검색만'}   |  끝내려면 Ctrl+C", f"  Also running: {', '.join(side_en) or 'basic search only'}   |  Ctrl+C to quit"))
    if not a.no_browser and not a.app:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    search.serve(f, a.port)
    return 0


def cmd_mcp(a):
    """AI tools on this PC find the person's files through findr (search_files, read_file). Speaks MCP on stdin/stdout."""
    from .mcp_server import main as serve_mcp
    serve_mcp()
    return 0


def cmd_relations(a):
    """what ties one indexed file to others, as JSON: characteristic words, stable file id, version-family id, same-content copies"""
    from .mcp_server import file_relations
    r = file_relations(os.path.abspath(os.path.expanduser(a.path)) if os.path.exists(os.path.expanduser(a.path)) else a.path, a.terms)
    print(json.dumps(r, ensure_ascii=False))
    return 1 if "error" in r else 0


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
    req = urllib.request.Request(f"{mcp_server.ENGINE}/api?" + urllib.parse.urlencode({"q": q, "k": k, "mode": "auto", **({"lang": "en"} if lang() == "en" else {})}),
                                 headers={"Cookie": f"findr_t={key}", "Authorization": "Basic " + base64.b64encode(f"findr:{key}".encode()).decode()})
    try:
        return json.loads(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=120).read())
    except Exception:
        return None


def cmd_ask(a):
    q = " ".join(a.query)
    if not Path(a.db).exists():   # Finder would create an empty index.db, and serve and doctor then failed on it
        print(L("아직 색인이 없습니다. 먼저:  findr index", "No index yet. First:  findr index")); return 1
    t = time.time()
    r = None if (a.local or Path(a.db).resolve() != paths.DB.resolve()) else _ask_engine(q, a.k)
    how = L("실행 중인 검색 엔진", "the running search engine")
    if r is None:   # no engine up: load the models here (slower: every ask loads them again)
        how = L("모델을 직접 불러옴", "models loaded here")
        _engine_env()
        os.environ.setdefault("FINDR_ENCODER", _encoder())
        import search
        f = search.Finder(str(a.db))
        if lang() == "en" and hasattr(search, "set_lang"):   # the engine's own sentences (the note) in the same language
            search.set_lang("en")
        r = search.add_probs(f.search(q, mode="xce", k=a.k))
    if r.get("note"):
        print("ⓘ", r["note"])
    for i, x in enumerate(r.get("results", []), 1):
        pr = f"{round(100 * x['prob'])}%" if x.get("prob") is not None else ""
        print(f"{i:2d}. {pr:>4}  {x['name']}  ({x.get('date', '')})\n      {x['path']}")
    print(L(f"  ({time.time() - t:.1f}초, {how})", f"  ({time.time() - t:.1f}s, {how})"))
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
    return f'uv pip install --python "{sys.executable}" -e "{root}"   ({L("uv가 없으면", "without uv")}: "{sys.executable}" -m pip install -e "{root}")'


def cmd_doctor(a):
    dev, gb, want = models.plan()
    print(L(f"findr {__version__} | 파이썬 {sys.version.split()[0]} | 장치 {dev}{f' {gb:.0f}GB' if gb else ''} | 데이터 {paths.HOME}", f"findr {__version__} | Python {sys.version.split()[0]} | device {dev}{f' {gb:.0f}GB' if gb else ''} | data {paths.HOME}"))
    try:
        import torch
        print(L(f"  ✓ 연산 라이브러리 torch {torch.__version__}", f"  ✓ compute library torch {torch.__version__}"))
    except Exception as e:
        print(L(f"  ✗ 연산 라이브러리를 불러오지 못함: {type(e).__name__}: {str(e)[:160]}", f"  ✗ could not load the compute library: {type(e).__name__}: {str(e)[:160]}"))
        if os.name == "nt":
            print(L("    → Visual C++ 런타임 업데이트: winget install Microsoft.VCRedist.2015+.x64", "    → update the Visual C++ runtime: winget install Microsoft.VCRedist.2015+.x64"))
    try:   # the comfort tier the resource manager starts from (what this PC has)
        sys.path.insert(0, str(paths.CORE)); import governor
        pr = governor.profile(dev, gb)
        why_en = ", ".join(x.replace("메모리 ", "memory ").replace("GPU 없음", "no GPU") for x in pr["reasons"])
        tier_en = {"full": "comfortable+", "standard": "standard", "light": "light", "minimal": "minimal"}.get(pr["tier"], pr["tier"])
        print(L(f"  ✓ 이 PC에 맞춘 등급: {pr['tier_ko']} ({', '.join(pr['reasons'])})", f"  ✓ tier for this PC: {tier_en} ({why_en})"))
    except Exception:   # noqa: BLE001
        pass
    for w in want:
        print(L(f"  {'✓' if models.have(w) else '✗'} 모델 {w}", f"  {'✓' if models.have(w) else '✗'} model {w}"))
    used = _index_encoder()   # the list above is what this PC may have; the index says which meaning model it really uses
    if used:
        why = ""
        try:
            chk = json.loads((paths.HOME / "device_check.json").read_text())
            if not chk.get("big_ok"):
                why = L(f" (Apple GPU 자가 시험: 큰 모델 초당 {chk.get('pieces_per_s')}조각 → 작은 모델)", f" (Apple GPU self-test: larger model at {chk.get('pieces_per_s')} pieces/s → small model)")
        except (OSError, ValueError):
            pass
        print(L(f"  ✓ 색인의 의미 모델: {used}{why}", f"  ✓ meaning model of the index: {used}{why}"))
    if dev == "cpu":
        ox = (paths.MODELS / "rank" / "model_int8.onnx").exists()
        print(L(f"  {'✓' if ox or (paths.MODELS / 'rank' / 'model.safetensors').exists() else '✗'} 재정렬: {'가벼운 8비트 (CPU용)' if ox else '전체 정밀도'}", f"  {'✓' if ox or (paths.MODELS / 'rank' / 'model.safetensors').exists() else '✗'} reranker: {'light 8-bit (for CPU)' if ox else 'full precision'}"))
        try:
            if str(paths.CORE) not in sys.path:
                sys.path.insert(0, str(paths.CORE))
            import gpu_dml
            g = gpu_dml.describe()
            if g:   # Windows: an AMD or Intel GPU runs the reranker where it was measured faster
                print(L(f"    재정렬 장치: {g[0]}", f"    reranker device: {g[1]}"))
        except Exception:   # noqa: BLE001
            pass
    if models.qu_ok(dev):
        print(L(f"  {'✓' if models.llama_server() else '✗'} llama-server (없으면 질문을 규칙으로 읽음)", f"  {'✓' if models.llama_server() else '✗'} llama-server (without it, questions are read by rules)"))
    else:
        print(L("  ✓ 질문 읽기: 규칙 (GPU가 없으면 이쪽이 빠르고 정확도는 같음)", "  ✓ question reading: rules (without a GPU this is faster and just as accurate)"))
    if dev == "cuda" and models.llama_server():   # a CUDA build that finds no GPU still runs, ~5x slower, and says nothing
        try:
            out = subprocess.run([models.llama_server(), "--list-devices"], capture_output=True, text=True, timeout=60, env=models.llama_env()).stdout
        except Exception:
            out = ""
        gpu = "CUDA" in out
        print(L(f"  {'✓' if gpu else '✗'} llama-server 장치: {'GPU' if gpu else 'CPU (GPU를 못 찾음: 질문 이해가 느려짐)'}", f"  {'✓' if gpu else '✗'} llama-server device: {'GPU' if gpu else 'CPU (no GPU found: question understanding is slower)'}"))
    miss = _missing_deps()
    if miss:   # #31: an update by `git pull` does not install what was added since
        print(L(f"  ✗ 빠진 구성 요소 {len(miss)}개: {', '.join(miss)}", f"  ✗ {len(miss)} missing component{'' if len(miss) == 1 else 's'}: {', '.join(miss)}"))
        print(L(f"    → 설치:  {_reinstall_cmd()}", f"    → install:  {_reinstall_cmd()}"))
    else:
        print(L("  ✓ 구성 요소 모두 설치됨", "  ✓ all components installed"))
    try:
        import index
        e = index.ocr_engine()
        if e == "none":
            raise ImportError("easyocr")
        print(L(f"  ✓ 글자 인식: {dict(vision='macOS 기본', windows='Windows 기본 (한국어)', easyocr='EasyOCR').get(e, e)}", f"  ✓ text recognition: {dict(vision='built into macOS', windows='built into Windows (Korean)', easyocr='EasyOCR').get(e, e)}"))
        if e == "easyocr" and os.name == "nt":
            print(L("    → 더 빠르게: Windows 한국어 글자 인식 설치 (관리자 PowerShell)  Add-WindowsCapability -Online -Name \"Language.OCR~~~ko-KR~0.0.1.0\"", "    → faster: install Windows text recognition for Korean (PowerShell as administrator)  Add-WindowsCapability -Online -Name \"Language.OCR~~~ko-KR~0.0.1.0\""))
    except ImportError:
        print(L(f"  ✗ 글자 인식 없음 (스캔·스크린샷은 이름으로만 찾음) → 설치:  {_reinstall_cmd()}", f"  ✗ no text recognition (scans and screenshots are found by name only) → install:  {_reinstall_cmd()}"))
    except Exception as e:
        print(L(f"  ✗ 글자 인식 확인 실패: {type(e).__name__}", f"  ✗ text recognition check failed: {type(e).__name__}"))
    if Path(a.db).exists():
        import sqlite3
        c = sqlite3.connect(str(a.db))
        n = c.execute("SELECT count(*) FROM files").fetchone()[0]
        v = c.execute("SELECT count(*) FROM chunks WHERE emb IS NOT NULL").fetchone()[0]
        try:
            ph = c.execute("SELECT count(*) FROM photo_emb").fetchone()[0]
        except Exception:
            ph = 0
        print(L(f"  ✓ 색인: 파일 {n:,}개, 의미 벡터 {v:,}개, 사진 벡터 {ph:,}개", f"  ✓ index: {n:,} files, {v:,} meaning vectors, {ph:,} photo vectors"))
        # #66-10: what a deep run would still do, and when it helps
        # (the pieces a first run leaves on purpose, past each file's opening FIRST_PIECES, and the unread rest of long files)
        try:
            sys.path.insert(0, str(paths.CORE)); import index as _ix
            later = _ix.deep_left(c, FIRST_PIECES)
        except Exception:   # noqa: BLE001  (an index from an older version: no pending column)
            later = (0, 0)
        if later[0]:
            print(L(f"  ○ 깊은 계산 대기: 긴 문서 {later[1]:,}개의 뒷부분 약 {later[0]:,}조각은 아직 단어로만 찾습니다",
                    f"  ○ waiting for the deep run: about {later[0]:,} later piece{'' if later[0] == 1 else 's'} of {later[1]:,} long "
                    f"document{'' if later[1] == 1 else 's'} found by words only"))
            print(L("    → 문서 뒷부분에만 있는 내용을 뜻으로 찾을 때 도움이 됩니다. 앱은 PC를 쓰지 않을 때 조금씩 하고, 지금 하려면  findr index --deep",
                    "    → helps when what you remember is only deep inside a document. The app does it bit by bit while the PC is idle; to run it now:  findr index --deep"))
    else:
        print(L("  ○ 아직 색인 전입니다 → findr index", "  ○ not indexed yet → findr index"))
    return 0


def cmd_uninstall(a):
    """remove everything findr keeps on this PC: one data folder (index, models, engine runtime, settings, logs). The person's
    own files are never touched. A running engine or indexer must be closed first: their files would come back"""
    import shutil
    h = paths.HOME
    if not h.is_dir():
        print(L(f"지울 데이터가 없습니다: {h}", f"No data to remove: {h}")); return 0
    # a folder set by FINDR_HOME counts only when it holds findr's own things: FINDR_HOME=~ must never empty a home folder
    # (an empty models folder is no sign: every other command creates one at start)
    marks = [m for m in ("index.db", "runtime", "progress.json", "folders.json") if (h / m).exists()]
    if (h / "models").is_dir() and any((h / "models").iterdir()):
        marks.append("models")
    if not (h.name in ("findr", ".findr") and marks or os.environ.get("FINDR_HOME") and marks):
        print(L(f"findr의 데이터 폴더가 아니라서 지우지 않습니다: {h}", f"Not a findr data folder, so nothing was removed: {h}")); return 1
    busy = []
    try:
        pid = json.loads((h / "progress.json").read_text(encoding="utf-8")).get("pid")
        if pid and pid != os.getpid():
            os.kill(int(pid), 0); busy.append(L(f"색인 작업(pid {pid})", f"the indexer (pid {pid})"))
    except (OSError, ValueError, TypeError):
        pass
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:8096/healthz", timeout=1); busy.append(L("검색 엔진(8096)", "the search engine (8096)"))
    except Exception:
        pass
    if busy:
        print(L("먼저 닫아 주세요: " + ", ".join(busy) + ". 앱을 쓰고 있다면 앱을 닫거나 앱의 설정 → findr 지우기를 쓰세요.",
                "Close these first: " + ", ".join(busy) + ". If you are using the app, close it, or use Settings → Remove findr in the app.")); return 2
    size = sum(f.stat().st_size for f in h.rglob("*") if f.is_file()) / 2**30
    print(L(f"지울 폴더: {h} (약 {size:.1f}GB: 색인, 모델, 엔진, 설정, 기록). 내 파일은 지우지 않습니다.", f"Folder to remove: {h} (about {size:.1f}GB: index, models, engine, settings, logs). Your own files are not touched."))
    if not a.yes and input(L("정말 지울까요? [y/N] ", "Really remove it? [y/N] ")).strip().lower() not in ("y", "yes", "예", "ㅇ"):
        print(L("지우지 않았습니다.", "Nothing was removed.")); return 0
    # the Windows app is installed into this same folder: its files stay for the system's uninstaller (removing them here
    # left 'findr' in Settings > Apps with no uninstaller to run)
    app = [n for n in ("findr-desktop.exe", "uninstall.exe") if (h / n).exists()]
    if app:
        keep = {"findr-desktop.exe", "uninstall.exe", "uv.exe", "engine"}
        for e in list(h.iterdir()):
            if e.name.lower() not in keep:
                shutil.rmtree(e, ignore_errors=True) if e.is_dir() else e.unlink(missing_ok=True)
        left = [e.name for e in h.iterdir() if e.name.lower() not in keep]
        print(L("데이터를 지웠습니다.", "Data removed.") if not left else
              L(f"일부를 지우지 못했습니다: {', '.join(left[:5])}", f"Some items could not be removed: {', '.join(left[:5])}"))
        print(L("앱도 지우려면 Windows 설정 → 앱 → 설치된 앱에서 findr를 제거하세요(앱 파일은 그때 지워집니다).", "To remove the app too, uninstall findr in Windows Settings → Apps → Installed apps (the app's files are removed then)."))
        return 0
    shutil.rmtree(h, ignore_errors=True)
    print(L("지웠습니다.", "Removed.") if not h.exists() else
          L(f"일부를 지우지 못했습니다. 이 폴더를 직접 지워 주세요: {h}", f"Some items could not be removed. Please delete this folder yourself: {h}"))
    print(L("남은 것: 앱이라면 Windows 설정 → 앱 / macOS 응용 프로그램 폴더에서 지우고, 명령줄로 설치했다면 저장소 폴더를 지우면 끝입니다.", "What is left: the app, if you use it (remove it in Windows Settings → Apps or the macOS Applications folder), or the repository folder if you installed from the command line (just delete it)."))
    print(L("설치에 쓴 uv 캐시(여러 프로그램이 함께 씀)는 그대로 둡니다. 필요하면 `uv cache clean`으로 비울 수 있습니다.", "The uv cache used for the installation (shared with other programs) is left alone. Clear it with `uv cache clean` if you like."))
    return 0


def main(argv=None):
    if (argv if argv is not None else sys.argv[1:])[:1] != ["mcp"]:   # MCP's channel is UTF-8 by its own library
        from . import textout
        textout.fit()   # Windows: piped output in the encoding its reader expects (#66-10)
    if (argv if argv is not None else sys.argv[1:])[:1] == ["uninstall"]:   # creates nothing in the folder it is about to judge
        ap = argparse.ArgumentParser(prog="findr uninstall"); ap.add_argument("cmd"); ap.add_argument("--yes", action="store_true")
        return cmd_uninstall(ap.parse_args(argv))
    paths.setup_env()
    try:   # company networks that inspect TLS: model downloads trust the system's certificates
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="findr", description=L("기억나는 대로 물으면, 내 PC가 찾아 줍니다.", "Ask the way you remember it, and your PC finds the file."))
    ap.add_argument("--db", default=str(paths.DB), help=L("색인 파일 (기본: 데이터 폴더의 index.db)", "index file (default: index.db in the data folder)"))
    ap.add_argument("--version", action="version", version=f"findr {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help=L("이 PC에 맞는 모델 받기", "download the models that fit this PC"))
    s.add_argument("--lite", action="store_true", help=L("GPU가 있어도 작은 모델만", "small models only, even with a GPU")); s.add_argument("--full", action="store_true", help=L("--lite 선택을 되돌림", "undo --lite")); s.add_argument("--photo", action="store_true", help=L("사진 내용 검색 모델도", "also the model that finds photos by what is in them"))
    s.add_argument("--no-photo", action="store_true"); s.add_argument("--no-llm", action="store_true", help=L("llama-server 받지 않기", "do not download llama-server"))
    s.add_argument("--token", default=None, help=L("허깅페이스 토큰 (비공개 모델용)", "Hugging Face token (for private models)"))
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("index", help=L("색인 (폴더를 주지 않으면 PC 전체의 내 파일)", "index (without folders: your files on the whole PC)")); s.add_argument("paths", nargs="*")
    s.add_argument("--deep", action="store_true", help=L("긴 문서의 뒷부분까지 의미 계산 (오래 걸림, 쉬는 시간에 권장)", "meaning vectors for the whole of long documents (slow: best run while the PC is idle)"))
    s.add_argument("--names-only", action="store_true", help=L("새 파일을 이름으로만 반영 (내용은 다음 색인 때, 배터리일 때 앱이 씀)", "add new files by name only (content at the next index; the app uses this on battery)"))
    s.add_argument("--gone-only", action="store_true", help=L("지운 파일만 빠르게 정리 (바뀐 폴더만 확인, 내용은 읽지 않음)", "only forget deleted files, quickly (looks at changed folders only, reads nothing)"))
    s.set_defaults(fn=cmd_index)
    s = sub.add_parser("serve", help=L("검색 화면 열기", "open the search page")); s.add_argument("--port", type=int, default=8096)
    s.add_argument("--no-browser", action="store_true"); s.add_argument("--no-photo", action="store_true"); s.add_argument("--no-llm", action="store_true")
    s.add_argument("--app", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("roots", help=L("PC 전체 색인 때 볼 폴더 목록 (JSON)", "the folders a whole-PC index covers (JSON)")); s.set_defaults(fn=cmd_roots)
    s = sub.add_parser("mcp", help=L("AI 도구(Claude 등)에 findr 연결: MCP 서버 (stdio)", "connect findr to AI tools (Claude and others): MCP server (stdio)")); s.set_defaults(fn=cmd_mcp)
    s = sub.add_parser("relations", help=L("파일 하나의 관계 (JSON): 특징 낱말, 안정적인 파일 id, 버전 묶음 id, 같은 내용 사본",
                                           "how one file relates to others (JSON): characteristic words, stable file id, version-family id, same-content copies"))
    s.add_argument("path"); s.add_argument("--terms", type=int, default=10); s.set_defaults(fn=cmd_relations)
    s = sub.add_parser("ask", help=L("터미널에서 찾기 (검색 화면이 켜져 있으면 그 엔진에 물음)", "search from the terminal (asks the running engine when the search page is open)")); s.add_argument("query", nargs="+")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--local", action="store_true", help=L("켜진 엔진을 쓰지 않고 모델을 직접 불러오기", "load the models here instead of asking the running engine")); s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("doctor", help=L("설치 상태 점검", "check the installation")); s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("uninstall", help=L("이 PC에서 findr 데이터(색인, 모델, 엔진, 설정) 모두 지우기", "remove all findr data (index, models, engine, settings) from this PC"))
    s.add_argument("--yes", action="store_true", help=L("묻지 않고 지우기", "remove without asking")); s.set_defaults(fn=cmd_uninstall)
    s = sub.add_parser("upgrade", help=L("쉬는 시간에 더 정확한 의미 모델로 조금씩 바꾸기 (GPU 없는 PC)", "move to the more accurate meaning model bit by bit in idle time (PCs without a GPU)"))
    s.add_argument("--minutes", type=float, default=50, help=L("이번에 쓸 최대 시간 (다음 번에 이어서)", "most minutes to spend this time (the next run continues)")); s.set_defaults(fn=cmd_upgrade)
    s.add_argument("--first-only", action="store_true", help=L("파일마다 첫 부분만 큰 모델로 읽기 (전체 바꾸기가 너무 느린 PC에서도, 바꾸지는 않음)", "read only the first part of each file with the larger model (also on PCs too slow for the full move; nothing is switched)"))
    a = ap.parse_args(argv)
    a.db = Path(a.db).expanduser()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
