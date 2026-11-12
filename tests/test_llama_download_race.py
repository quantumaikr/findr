"""two setups at once (the app started twice) both fetched llama.cpp into one fixed file name: the first to finish
unpacked and deleted it, and the other then failed to open it (FileNotFoundError on a Mac, 0.2.21). Each run now
downloads to a name of its own."""
import io
import json
import tarfile
import threading

from findr import models, paths

NAME = "llama-b0-bin-macos-arm64.tar.gz"


def _archive():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("llama-b0/llama-server"); info.size = len(data); info.mode = 0o755
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fake_net(monkeypatch, gate):
    blob = _archive()
    rel = {"assets": [{"name": NAME, "browser_download_url": "https://example.invalid/" + NAME}]}
    monkeypatch.setattr(models.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(rel).encode()))

    def retrieve(url, path):
        with open(path, "wb") as f:
            f.write(blob)
        gate.wait(5)   # both runs hold a downloaded archive before either unpacks
        return str(path), None
    monkeypatch.setattr(models.urllib.request, "urlretrieve", retrieve)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "BIN", tmp_path / "bin")
    monkeypatch.setattr(models.shutil, "which", lambda name: None)
    monkeypatch.setattr(models.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(models.platform, "machine", lambda: "arm64")


def test_two_setups_at_once_both_end_with_the_server(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    gate = threading.Barrier(2)
    _fake_net(monkeypatch, gate)
    out, errs = [], []

    def run():
        try:
            out.append(models.pull_llama_server("mps"))
        except Exception as e:   # the old code: FileNotFoundError in one of the two
            errs.append(e)
    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]; [t.join(20) for t in ts]
    assert errs == []
    assert len(out) == 2 and all(o and o.endswith("llama-server") for o in out)


def test_no_archive_is_left_behind(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    gate = threading.Barrier(1)
    _fake_net(monkeypatch, gate)
    assert models.pull_llama_server("mps").endswith("llama-server")
    left = [p.name for p in (tmp_path / "bin").iterdir() if p.name != "llama"]   # no archive, no temporary folder
    assert left == [], left
