"""Which models this PC gets, and downloading them (once).
findr's own models come from one Hugging Face repository; public models come from their original publishers.
The choice follows the PC: with a GPU the larger meaning model, side-by-side checker and photo model; without one the small model."""
import json
import os
import platform
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from . import paths

OWN = os.environ.get("FINDR_MODEL_REPO", "quantumaikr/findr-models")   # rank/, gate/, qu/ (private while in testing)
PUBLIC = {   # folder -> (repository, files or None for all, what it is for)
    "embeddinggemma-2": ("google/embeddinggemma-2", None, "더 정확한 의미 검색 (큰 모델)"),
    "multilingual-e5-small": ("intfloat/multilingual-e5-small", ["*.json", "model.safetensors", "sentencepiece.bpe.model", "1_Pooling/*"],
                              "의미 검색 (작은 모델)"),
    "checker": ("Qwen/Qwen3-1.7B-GGUF", ["Qwen3-1.7B-Q8_0.gguf"], "후보 나란히 비교 (GPU)"),
    "kanana-2-v-decide-3b-2611": ("kakaocorp/kanana-2-v-decide-3b-2611", None, "사진 내용 검색 (GPU 8GB 이상)"),
}


def gpu():
    """('cuda', GB) / ('mps', 0) / ('cpu', 0)"""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", torch.cuda.get_device_properties(0).total_memory / 2 ** 30
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps", 0.0
    except Exception:
        pass
    return "cpu", 0.0


def ram_gb():
    """this PC's memory in GB (0 when it cannot be read)"""
    try:
        if os.name == "nt":
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("pt", ctypes.c_ulonglong), ("pa", ctypes.c_ulonglong), ("vt", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("x", ctypes.c_ulonglong)]
            m = M(); m.l = ctypes.sizeof(M); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.total / 2 ** 30
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except Exception:
        return 0.0


def checker_ok(dev, gb):
    """the side-by-side checker (1.7B, ~2 GB with its cache): NVIDIA with 6 GB+, or an Apple GPU with 24 GB+ of shared memory
    (on 16 GB it pushed a Mac into swap next to search and indexing, #20)"""
    return dev == "cuda" and gb >= 6 or dev == "mps" and ram_gb() >= 24


def qu_ok(dev):
    """the question-reading model (0.6B) on a GPU only. On 4 CPU cores one reading took 3.1 s, past the 0.6 s the engine
    waits, so its answer was never used, and it held the cores the reranker needed: rules alone answered in 2.2 s instead
    of 3.5 s, with the same accuracy (real PC, 90 questions)"""
    return dev in ("cuda", "mps")


def plan(photo=None, lite=False):
    """the folders this PC should have"""
    dev, gb = gpu()
    # GPU memory decides how much fits: under 4 GB the small meaning model, under 6 GB no side-by-side checker.
    # An Apple GPU starts on the small model too: the large one ran 12-13.5x slower on both field Macs (M3 Air 5.2 vs 62.5,
    # M1 Pro 8.8 vs 118.7 pieces/s), so every Mac downloaded 1.2 GB, measured it for minutes and fell back. The idle-time
    # upgrade fetches and measures it later, and switches only where it is fast (fast first, smarter with use).
    big = dev == "cuda" and gb >= 4 and not lite
    want = ["rank", "gate"] + (["qu"] if qu_ok(dev) else []) + (["embeddinggemma-2"] if big else ["multilingual-e5-small"])
    if not lite and checker_ok(dev, gb):
        want.append("checker")
    if photo is None:
        photo = dev == "cuda" and gb >= 10   # the photo model needs about 7.5 GB of GPU memory next to the rest
    if photo:
        want.append("kanana-2-v-decide-3b-2611")
    return dev, gb, want


def big_ok():
    """the large meaning model on this device. On an Apple GPU it is measured once on real-length pieces (after the first,
    slow, kernel-compiling batch); under FINDR_MIN_RATE pieces/s (default 30) the small model is used. Kept in the data folder."""
    dev, _ = gpu()
    if dev != "mps":
        return True
    mark = paths.HOME / "device_check.json"
    if mark.exists():
        old = json.loads(mark.read_text())
        if old.get("v") == 2:
            return old.get("big_ok", False)
    ok, rate = False, 0.0
    try:
        import time
        import enc_eg2
        print("  Apple GPU에서 큰 모델 속도를 재는 중입니다 (처음 한 번, 몇 분 걸릴 수 있음) ...", flush=True)
        piece = ("회의 전에 고친 견적서 | 영업/2025/고객사\n" + "스마트센서 게이트웨이 납품 일정과 단가 조건을 정리했습니다. " * 30)[:1200]
        e = enc_eg2.EG2("mps"); e.emb([piece] * e.bs, query=False)   # compiles the GPU kernels
        n = e.bs * 4; t = time.time(); e.emb([piece] * n, query=False); rate = n / max(time.time() - t, 1e-6)
        ok = rate >= float(os.environ.get("FINDR_MIN_RATE") or 30)
    except Exception:
        ok = False
    mark.write_text(json.dumps({"v": 2, "device": dev, "big_ok": ok, "pieces_per_s": round(rate, 1)}))
    print(f"  큰 모델 속도: 초당 {rate:.0f}조각 → {'큰 모델 사용' if ok else '작은 모델로 전환 (더 빠름)'}", flush=True)
    return ok


def have(folder):
    """the model is there: files besides huggingface_hub's own .cache, and no download left half done. A dropped connection
    used to leave .cache (or a few json files) behind, and every 다시 시도 then skipped the model and failed the check"""
    d = paths.MODELS / folder
    if not d.is_dir() or not any(p.name != ".cache" for p in d.iterdir()):
        return False
    if any((d / ".cache").rglob("*.incomplete")):
        return False
    if folder == "rank":   # our reranker: the weights themselves, not only its json files
        return any(d.glob("*.safetensors")) or (d / "model_int8.onnx").exists()
    return True


# about how much each folder takes once downloaded (MB), for when the repository cannot be asked (offline, a company proxy):
# the repositories' own file sizes on 2026-11-08 (rank without the int8 file, which is 543 MB on its own)
APPROX_MB = {"rank": 1153, "rank-int8": 560, "gate": 1, "qu": 639, "embeddinggemma-2": 1526, "multilingual-e5-small": 510,
             "checker": 1834, "kanana-2-v-decide-3b-2611": 7341}
INDEX_ROOM_GB = 2.0   # room left for the first index to start (it grows to 1-6 GB on a whole PC, #67), besides the models


def _patterns(folder):
    """(repository, files to fetch, files to skip, size key) for one folder, as pull() fetches it"""
    if folder in ("rank", "gate", "qu"):
        cpu = gpu()[0] == "cpu"
        if folder == "rank" and cpu:
            return OWN, [f"{folder}/*.json", f"{folder}/model_int8.onnx"], None, "rank-int8"
        return OWN, [f"{folder}/*"], None if cpu else ["*.onnx"], folder
    repo, files, _ = PUBLIC[folder]
    return repo, files, None, folder


def _repo_bytes(repo, allow, ignore, token=None):
    """bytes of the files snapshot_download would fetch with these patterns, from the repository's own file list"""
    from fnmatch import fnmatch
    from huggingface_hub import HfApi
    info = HfApi().model_info(repo, files_metadata=True, token=token, timeout=10)
    return sum(s.size or 0 for s in info.siblings
               if (not allow or any(fnmatch(s.rfilename, p) for p in allow)) and not (ignore and any(fnmatch(s.rfilename, p) for p in ignore)))


def download_plan(folders, token=None):
    """[(folder, bytes, exact)] for the folders this PC does not have yet (have(): a half-done download counts as missing).
    exact is False when the repository could not be asked and the size is the APPROX_MB estimate"""
    out, sizes = [], {}
    for f in folders:
        if have(f) or (f not in PUBLIC and f not in ("rank", "gate", "qu")):
            continue
        repo, allow, ignore, key = _patterns(f)
        try:
            if (repo, tuple(allow or ()), tuple(ignore or ())) not in sizes:
                sizes[(repo, tuple(allow or ()), tuple(ignore or ()))] = _repo_bytes(repo, allow, ignore, token)
            out.append((f, sizes[(repo, tuple(allow or ()), tuple(ignore or ()))], True))
        except Exception:   # offline, a proxy, a private repository without a token: the estimate keeps the check useful
            out.append((f, APPROX_MB.get(key, 0) * 10 ** 6, False))
    return out


def disk_free(where=None):
    """free bytes on the drive that holds the models folder (or its nearest existing parent: before the first setup)"""
    p = Path(where or paths.MODELS)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def pull(folders, token=None):
    from huggingface_hub import snapshot_download
    own = [f for f in folders if f in ("rank", "gate", "qu") and not have(f)]
    if own:
        print(f"  findr 모델 받는 중 ({', '.join(own)}) ...", flush=True)
        cpu = gpu()[0] == "cpu"
        pats = [p for f in own for p in ([f"{f}/*.json", f"{f}/model_int8.onnx"] if f == "rank" and cpu else [f"{f}/*"])]
        # a CPU-only PC ranks with the int8 model (543 MB); a GPU PC never uses it
        snapshot_download(OWN, local_dir=paths.MODELS, allow_patterns=pats, ignore_patterns=None if cpu else ["*.onnx"], token=token)
    for f in folders:   # the public models (meaning search, photos): without them the engine would fetch them at search time
        if f in PUBLIC and not have(f):
            repo, files, why = PUBLIC[f]
            print(f"  {why}: {repo} 받는 중 ...", flush=True)
            snapshot_download(repo, local_dir=paths.MODELS / f, allow_patterns=files, token=token)


def pull_rank_int8(token=None):
    """the int8 reranker for a CPU-only PC that was set up before it existed (an update finds the rank folder already there)"""
    from huggingface_hub import hf_hub_download
    print("  가벼운 재정렬 모델 받는 중 (543MB, 한 번) ...", flush=True)
    hf_hub_download(OWN, "rank/model_int8.onnx", local_dir=paths.MODELS, token=token)


def pull_rank_fp32(token=None):
    """the full-precision reranker weights, when the int8 model cannot run on this PC"""
    from huggingface_hub import snapshot_download
    print("  재정렬 모델(전체 정밀도) 받는 중 ...", flush=True)
    snapshot_download(OWN, local_dir=paths.MODELS, allow_patterns=["rank/*"], ignore_patterns=["*.onnx"], token=token)


def llama_server():
    """path of llama-server (the small local language models run in it), or None"""
    for c in (shutil.which("llama-server"), *paths.BIN.glob("llama/**/llama-server.exe"), *paths.BIN.glob("llama/**/llama-server")):
        if c and Path(c).is_file():
            return str(c)
    return None


def llama_env():
    """environment for llama-server. The Linux CUDA build comes as two archives, and the runtime (libcudart, libcublas)
    lands in its own folder: without it on the library path the build finds no GPU and runs the models on the CPU"""
    env = dict(os.environ)
    if sys.platform.startswith("linux"):
        libs = sorted({str(p.parent) for p in paths.BIN.glob("llama/**/lib*.so*")})
        if libs:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(libs + [x for x in env.get("LD_LIBRARY_PATH", "").split(os.pathsep) if x])
    return env


def pull_llama_server(dev):
    """the official llama.cpp build for this system (GitHub releases); without it findr reads questions by rules"""
    if llama_server():
        return llama_server()
    import tarfile
    sysname, arch = platform.system(), platform.machine().lower()
    x64 = arch in ("x86_64", "amd64")
    if sysname == "Windows":   # CUDA 12.4 builds run on older NVIDIA drivers too
        key, rt = ("bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip") if dev == "cuda" else ("bin-win-cpu-x64.zip", None)
    elif sysname == "Darwin":
        key, rt = ("bin-macos-arm64.tar.gz" if not x64 else "bin-macos-x64.tar.gz"), None
    else:
        key, rt = (("bin-ubuntu-cuda-12.8-x64.tar.gz", "bin-ubuntu-cuda-12.8-x64.tar.gz") if dev == "cuda" and x64 else
                   ("bin-ubuntu-x64.tar.gz" if x64 else "bin-ubuntu-arm64.tar.gz", None))
    tag = os.environ.get("FINDR_LLAMA_TAG", "b12182")   # a tested build; builds are published under b<number> tags
    try:
        rel = json.load(urllib.request.urlopen(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{tag}", timeout=30))
    except Exception as e:
        print(f"  llama.cpp 정보를 못 받았습니다 ({type(e).__name__}): 질문은 규칙으로 읽습니다.")
        return None
    assets = rel.get("assets", [])
    pick = [a for a in assets if a["name"].startswith("llama-") and a["name"].endswith(key)]
    if rt:   # the CUDA runtime the build needs
        pick += [a for a in assets if a["name"].startswith("cudart-") and a["name"].endswith(rt)][:1]
    if not pick:
        print("  이 시스템용 llama.cpp 빌드를 찾지 못했습니다: 질문은 규칙으로 읽습니다."); return None
    dst = paths.BIN / "llama"; dst.mkdir(parents=True, exist_ok=True)
    for a in pick[:2]:
        z = paths.BIN / a["name"]
        print(f"  llama.cpp {tag} 받는 중: {a['name']}", flush=True)
        urllib.request.urlretrieve(a["browser_download_url"], z)
        if z.name.endswith(".zip"):
            with zipfile.ZipFile(z) as f:
                f.extractall(dst)
        else:
            with tarfile.open(z) as f:
                f.extractall(dst, filter="data")
        z.unlink()
    for p in dst.rglob("llama-server*"):
        try:
            p.chmod(0o755)
        except OSError:
            pass
    return llama_server()
