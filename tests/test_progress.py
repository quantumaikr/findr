"""what the app and the search page show while indexing: every stage's state, the file count, and a pause the person controls"""
import json
import os
import threading
import time

import index


def read(tmp_path):
    return json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))


def test_stages_move_forward(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); index._P.update(stages={}, files=None, paused=False)
    index.progress("names", 0, 1)
    assert read(tmp_path)["stages"] == {"names": "now"}
    index._P["files"] = 1234
    index.progress("content", 5, 10, minutes_left=3)
    p = read(tmp_path)
    assert p["stages"] == {"names": "done", "namevec": "done", "content": "now"} and p["files"] == 1234 and p["minutes_left"] == 3
    index._P["stages"]["ocr"] = "later"   # scans left for idle time on a CPU
    index.progress("ocr", 0, 0); index.progress("done", 1, 1)
    assert read(tmp_path)["stages"] == {"names": "done", "namevec": "done", "content": "done", "meaning": "done", "ocr": "later"}


def test_pause_holds_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); index._P.update(stages={}, paused=False)
    index.progress("content", 1, 2)
    (tmp_path / "pause").write_text("1")
    t = threading.Thread(target=index.pause_wait); t.start()
    time.sleep(1.5)
    assert t.is_alive() and read(tmp_path)["paused"] is True   # held, and the page can say so
    (tmp_path / "pause").unlink()
    t.join(5)
    assert not t.is_alive() and read(tmp_path)["paused"] is False and read(tmp_path)["stage"] == "content"


def test_forgotten_pause_lapses(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); index._P.update(stages={}, paused=False)
    f = tmp_path / "pause"; f.write_text("1"); old = time.time() - index.PAUSE_MAX - 10; os.utime(f, (old, old))
    t0 = time.time(); index.pause_wait()
    assert time.time() - t0 < 2 and not f.exists()


def test_progress_moves_by_time_with_rate_and_time_left(tmp_path, monkeypatch):
    """the app reads the file every 0.7 s: a stage writes by time, with its rate and minutes left (11/10: the first
    index looked frozen during the name scan of one big folder and between batches of meaning on a slow PC)"""
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); index._P.update(stages={}, files=None, paused=False); index._TICK.clear()
    clock = [1000.0]
    monkeypatch.setattr(index.time, "time", lambda: clock[0])
    index.tick("content", 0, 100)
    clock[0] += 0.2; index.tick("content", 5, 100)
    assert read(tmp_path)["done"] == 0                     # too soon: not written again
    clock[0] += 4.8; index.tick("content", 50, 100)
    p = read(tmp_path)
    assert p["done"] == 50 and p["rate"] == 10.0 and p["minutes_left"] == round(50 / 10 / 60, 1)
    clock[0] += 0.1; index.tick("content", 100, 100)
    assert read(tmp_path)["done"] == 100                   # the last one is always written


def test_name_scan_reports_files_looked_at(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_DATA", str(tmp_path)); index._P.update(stages={}, files=None, paused=False); index._TICK.clear()
    index.tick("names", 0, 1, scanned=4321, folder="Documents/2025")
    p = read(tmp_path)
    assert p["stage"] == "names" and p["scanned"] == 4321 and p["folder"] == "Documents/2025"
