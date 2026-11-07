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
    "embeddinggemma-2": ("google/embeddinggemma-2", None, "의미 검색 (GPU)"),
    "multilingual-e5-small": ("intfloat/multilingual-e5-small", ["*.json", "model.safetensors", "sentencepiece.bpe.model", "1_Pooling/*"],
                              "의미 검색 (GPU 없는 PC)"),
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


def plan(photo=None, lite=False):
    """the folders this PC should have"""
    dev, gb = gpu()
    big = dev in ("cuda", "mps") and not lite
    want = ["rank", "gate", "qu"] + (["embeddinggemma-2", "checker"] if big else ["multilingual-e5-small"])
    if photo is None:
        photo = dev == "cuda" and gb >= 10   # the photo model needs about 7.5 GB of GPU memory next to the rest
    if photo:
        want.append("kanana-2-v-decide-3b-2611")
    return dev, gb, want


def have(folder):
    d = paths.MODELS / folder
    return d.is_dir() and any(d.iterdir())


def pull(folders, token=None):
    from huggingface_hub import snapshot_download
    own = [f for f in folders if f in ("rank", "gate", "qu") and not have(f)]
    if own:
        print(f"  findr 모델 받는 중 ({', '.join(own)}) ...", flush=True)
        snapshot_download(OWN, local_dir=paths.MODELS, allow_patterns=[f"{f}/*" for f in own], token=token)
    for f in folders:
        if f in PUBLIC and not have(f):
            repo, files, why = PUBLIC[f]
            print(f"  {why}: {repo} 받는 중 ...", flush=True)
            snapshot_download(repo, local_dir=paths.MODELS / f, allow_patterns=files, token=token)


def llama_server():
    """path of llama-server (the small local language models run in it), or None"""
    for c in (shutil.which("llama-server"), *paths.BIN.glob("llama/**/llama-server.exe"), *paths.BIN.glob("llama/**/llama-server")):
        if c and Path(c).is_file():
            return str(c)
    return None


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
