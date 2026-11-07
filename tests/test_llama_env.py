"""llama-server finds the CUDA runtime that came in its own archive (Linux): without it the build runs on the CPU"""
import os
import sys

import pytest

from findr import models, paths


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the library path matters on Linux only")
def test_cuda_runtime_folder_on_library_path(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    exe, rt = b / "llama" / "llama-b1", b / "llama" / "cudart-llama-b1-bin-ubuntu-cuda-12.8-x64"
    exe.mkdir(parents=True); rt.mkdir(parents=True)
    (exe / "llama-server").write_text(""); (exe / "libggml-cuda.so").write_text("")
    (rt / "libcudart.so.12").write_text(""); (rt / "libcublas.so.12").write_text("")
    monkeypatch.setattr(paths, "BIN", b)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/lib/wsl/lib")
    got = models.llama_env()["LD_LIBRARY_PATH"].split(os.pathsep)
    assert str(rt) in got and str(exe) in got
    assert got[-1] == "/usr/lib/wsl/lib", "what was on the path stays, after the runtime"


def test_no_llama_keeps_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "BIN", tmp_path / "bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/x")
    assert models.llama_env().get("LD_LIBRARY_PATH") == "/x"
