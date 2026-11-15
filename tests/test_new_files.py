"""#221: a file made or renamed while findr runs is found by its name within seconds. The engine's watch sees the folder
change and asks the app for a names-only run (names_now); after that run the engine's names are fresh at the next
question, without waiting for the vectors to be read again"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import index
import newfiles
import search

IDX = Path(index.__file__)
ROOT = Path(__file__).resolve().parents[1]


def run_index(root, db, home, *extra):
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home), "PYTHONIOENCODING": "utf-8",
           "FINDR_ORIGIN": "0"}
    r = subprocess.run([sys.executable, str(IDX), "--root", str(root), "--db", str(db), "--no-embed", *extra],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr


def app_turn(home, root, db):
    """what the app's refresher does when it sees names_now: a names-only run, then the time of the last reflection"""
    if not (home / "names_now").exists():
        return False
    (home / "names_now").unlink()
    run_index(root, db, home, "--names-only")
    (home / "last_refresh").write_text(str(time.time()))
    return True


def finder(db):
    f = search.Finder.__new__(search.Finder)
    f.con = sqlite3.connect(db, check_same_thread=False)
    f._dv = f.con.execute("PRAGMA data_version").fetchone()[0]
    f._seen_at = time.time()   # the vectors were just read: their reload waits its minute
    f.ids, f.encoder, f._top = [], "e5-small", 0
    return f


def names(f):
    f.load_scan()
    return {nm for _, nm, _ in f.name_rows}


def test_new_and_renamed_files_found_by_name_within_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "NAMES_EVERY", 0)   # (a question 2 s after the last read of the names: here at once)
    a = tmp_path / "docs"; a.mkdir(); (a / "old_notes.txt").write_text("x", encoding="utf-8")
    home = tmp_path / "home"; home.mkdir()
    (home / "folders.json").write_text(json.dumps([str(a)]), encoding="utf-8")
    db = tmp_path / "i.db"
    run_index(a, db, home)
    f = finder(db)
    assert names(f) == {"old_notes.txt"}
    w = newfiles.Watch(db, home, every=0.1)
    assert w.step() == 0 and not (home / "names_now").exists()   # the first look only learns the folders' times

    t0 = time.time()
    time.sleep(0.01)
    (a / "Quarterly_Budget_2027.xlsx").write_bytes(b"x" * 10)
    (a / "sub").mkdir(); (a / "sub" / "회의록_11월.txt").write_text("y", encoding="utf-8")
    (a / "~$lock.docx").write_bytes(b"z")   # an office lock file: never asks for a run
    assert w.step() == 2 and (home / "names_now").exists()
    assert app_turn(home, a, db)
    f.refresh()   # the next question: within the vectors' minute, the names are read again
    assert {"quarterly_budget_2027.xlsx", "회의록_11월.txt"} <= names(f)
    assert w.step() == 0
    assert time.time() - t0 < 15, "a new file must be found by name within seconds"

    # a folder that holds only folders: its new file is seen as well
    (a / "proj" / "specs").mkdir(parents=True); (a / "proj" / "specs" / "s.txt").write_text("s", encoding="utf-8")
    w.step(); assert app_turn(home, a, db); assert w.step() == 0
    w2 = newfiles.Watch(db, home, every=0.1)   # the engine started again: it knows proj/ only from the index
    w2.step()
    time.sleep(0.01)
    (a / "proj" / "Roadmap_2027.pptx").write_bytes(b"r")
    assert w2.step() == 1 and app_turn(home, a, db)
    w.step()
    f.refresh()
    assert "roadmap_2027.pptx" in names(f)

    # renamed: the old row takes the new name (reuse_moved), and it is found by it as well
    time.sleep(0.01)
    (a / "old_notes.txt").rename(a / "Kickoff_mtg_agenda.txt")
    assert w.step() == 1
    assert app_turn(home, a, db)
    f.refresh()
    assert "kickoff_mtg_agenda.txt" in names(f) and "old_notes.txt" not in names(f)
    assert w.step() == 0 and not (home / "names_now").exists()   # no second run for it


def test_a_file_the_index_leaves_out_stops_asking(tmp_path):
    a = tmp_path / "docs"; a.mkdir(); (a / "a.txt").write_text("x", encoding="utf-8")
    home = tmp_path / "home"; home.mkdir()
    (home / "folders.json").write_text(json.dumps([str(a)]), encoding="utf-8")
    db = tmp_path / "i.db"
    run_index(a, db, home)
    w = newfiles.Watch(db, home, every=0.1)
    w.step()
    time.sleep(0.01)
    (a / "vocab.json").write_text("{}", encoding="utf-8")   # the index's JUNK: never taken in
    asks = 0
    for _ in range(4):
        if w.step():
            asks += app_turn(home, a, db)
    assert asks == 2 and w.waiting() == 0 and not (home / "names_now").exists()


def test_names_fresh_without_a_vector_reload(tmp_path):
    """the names were read again only with the vectors: at most once a minute, and never while memory was too short for
    a second set of vectors (an 8 GB Mac with a large index kept the names of its start)"""
    db = tmp_path / "i.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);")
    con.execute("INSERT INTO files VALUES(1,'/d/a.txt','a.txt','.txt',1,1,1)"); con.execute("INSERT INTO chunks VALUES(1,1,0,'',NULL)")
    con.commit()
    f = finder(db)
    assert names(f) == {"a.txt"}
    con.execute("INSERT INTO files VALUES(2,'/d/new.txt','new.txt','.txt',1,1,1)"); con.execute("INSERT INTO chunks VALUES(2,2,0,'',NULL)")
    con.commit()
    f._names_at = 0   # (names last read long enough ago)
    f.refresh()
    assert names(f) == {"a.txt", "new.txt"}


def test_app_and_page_take_part():
    rs = (ROOT / "app" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    body = rs[rs.index("fn start_refresher"):rs.index("fn mark_refreshed")]
    assert 'home().join("names_now")' in body
    assert re.search(r"names_only = !deep && \(\(battery && !now_asked\) \|\| names_turn\)", body)   # names only, any power state
    assert "deep && (names_turn ||" in body   # a names turn starts no deep run
    page = (ROOT / "findr" / "core" / "page.html").read_text(encoding="utf-8")
    assert "new_waiting" in page and "반영 중" in page
    assert '"new_waiting"' in (ROOT / "findr" / "core" / "search.py").read_text(encoding="utf-8")
