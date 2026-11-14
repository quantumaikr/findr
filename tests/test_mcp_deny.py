"""#200: the MCP tools never name, list or read anything under findr's data folder or a FINDR_MCP_DENY folder, even when it
is in the index, and answer for such a path as for one that is not in the index. Invented names only"""
import importlib
import io
import json
import os
import sqlite3
import sys

import pytest

NOT_INDEXED = {"error": "not a file in findr's index; use a path returned by search_files"}


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """findr's data folder holds an embedding app's own files, and they got into the index; a private folder and a link
    to the data folder too"""
    data = tmp_path / "data"
    (data / "app" / "notes").mkdir(parents=True)
    (tmp_path / "docs").mkdir(); (tmp_path / "private").mkdir()
    monkeypatch.setenv("FINDR_HOME", str(data)); monkeypatch.setenv("FINDR_APP_PORT", "9")   # no engine on port 9
    monkeypatch.setenv("FINDR_MCP_DENY", os.pathsep.join([str(tmp_path / "private"), str(tmp_path / "nowhere")]))
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    link = tmp_path / "shortcut"
    try:
        link.symlink_to(data, target_is_directory=True)
    except (OSError, NotImplementedError):   # Windows without the right to make links
        link = None
    d, docs, priv = data.as_posix(), (tmp_path / "docs").as_posix(), (tmp_path / "private").as_posix()
    rows = [(1, f"{docs}/plan_final.docx", "the final plan of the sensor project"),
            (2, f"{d}/app/notes/plan_final.docx", "the final plan of the sensor project"),   # an exact copy in the data folder
            (3, f"{d}/app/notes/plan_v2.docx", "app private sensor note"),
            (4, f"{priv}/plan_secret.docx", "private sensor note"),
            (5, f"{docs}/../data/app/plan_dotdot.docx", "dotdot sensor note"),
            (6, f"{docs}/sensor_budget.xlsx", "sensor budget numbers")]
    if link is not None:
        rows.append((7, f"{link.as_posix()}/app/plan_linked.docx", "linked sensor note"))
    con = sqlite3.connect(str(data / "index.db"))
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);
        CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    for i, p, t in rows:
        name = p.rsplit("/", 1)[1]
        con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,1)", (i, p, name, "." + name.rsplit(".", 1)[1], 100, 1700000000 + i))
        con.execute("INSERT INTO chunks VALUES(?,?,0,?)", (i, i, t))
        con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (i, name, p, t))
    con.commit(); con.close()
    m.tmp, m.data, m.docs, m.priv, m.link = tmp_path, d, docs, priv, link
    return m


def _paths(r):
    return [x["path"] for x in r.get("results", [])]


def test_search_leaves_out_the_data_folder_and_deny_folders(pc):
    for q in ("sensor", "plan", "plan_final"):
        got = _paths(pc.search_files(q, limit=30))
        assert got and all(p.startswith(pc.docs + "/") and "/../" not in p for p in got), (q, got)
    assert _paths(pc.search_files("plan", kind="word", folder=pc.data)) == []   # a filter pointing inside it: nothing
    assert _paths(pc.search_files("plan", folder=pc.data + "/app")) == []


def test_read_file_answers_as_for_a_file_not_in_the_index(pc):
    assert pc.read_file(pc.docs + "/plan_final.docx")["text"].startswith("the final plan")
    for p in (pc.data + "/app/notes/plan_v2.docx", pc.priv + "/plan_secret.docx", pc.docs + "/../data/app/plan_dotdot.docx",
              pc.data + "/index.db", pc.docs + "/../data/app/notes/plan_v2.docx"):
        assert pc.read_file(p) == NOT_INDEXED, p
    if pc.link is not None:
        assert pc.read_file(pc.link.as_posix() + "/app/plan_linked.docx") == NOT_INDEXED   # a link into the data folder


def test_list_folder_answers_as_for_an_empty_folder(pc):
    empty = pc.list_folder(pc.tmp.as_posix() + "/nothing-here")
    for p in (pc.data, pc.data + "/app", pc.priv, pc.docs + "/../data"):
        r = pc.list_folder(p)
        assert r["files"] == [] and r["folders"] == [] and r["note"] == empty["note"], (p, r)
    r = pc.list_folder(pc.tmp.as_posix())   # the folder above: the data folder and the private folder are not among its folders
    assert {f["name"] for f in r["folders"]} == {"docs"}, r
    assert pc.list_folder(pc.data + "/app/notes/plan_v2.docx")["files"] == []   # a file in it: as a folder not indexed


def test_versions_answers_as_for_a_file_not_in_the_index(pc):
    r = pc.versions(pc.docs + "/plan_final.docx")
    assert "error" not in r and all(not o["path"].startswith((pc.data, pc.priv)) and "/../" not in o["path"] for o in r["others"]), r
    for p in (pc.data + "/app/notes/plan_final.docx", pc.priv + "/plan_secret.docx", pc.docs + "/../data/app/plan_dotdot.docx"):
        assert pc.versions(p) == NOT_INDEXED, p


def test_case_and_dotdot_cannot_step_around_the_check(pc):
    assert pc._denied(pc.data.upper() + "/APP/x.txt")   # Windows and macOS ignore case: so does the check
    assert pc._denied(pc.docs + "/../data/app/x.txt")
    assert pc._denied(pc.docs + "/./../data")
    assert not pc._denied(pc.docs + "/../docs/plan_final.docx")
    assert not pc._denied(pc.tmp.as_posix() + "/data-other/x.txt")   # a folder whose name only begins the same
    assert pc._denied(pc.tmp.as_posix() + "/nowhere/x.txt")   # a deny folder that does not exist (yet) holds as well
    assert not pc._denied("") and not pc._denied(None)


def test_windows_paths_on_any_pc(pc, monkeypatch):
    monkeypatch.setattr(pc.paths, "HOME", "C:\\Users\\me\\AppData\\Local\\findr")
    # os.pathsep is ':' outside Windows, so a drive letter cannot be written in FINDR_MCP_DENY there: the list is read as given
    monkeypatch.setattr(pc.os, "pathsep", ";")
    monkeypatch.setenv("FINDR_MCP_DENY", "D:\\Private;E:\\Other")
    for p in ("C:\\Users\\me\\AppData\\Local\\findr\\index.db", "c:/users/ME/appdata/local/FINDR/app/x.txt",
              "/mnt/c/Users/me/AppData/Local/findr/app/x.txt", "C:\\Users\\me\\Documents\\..\\AppData\\Local\\findr\\x.txt",
              "d:\\private\\a.txt", "D:/Private", "e:\\other"):
        assert pc._denied(p), p
    for p in ("C:\\Users\\me\\Documents\\a.txt", "C:\\Users\\me\\AppData\\Local\\findr-old\\a.txt", "D:\\Privately\\a.txt"):
        assert not pc._denied(p), p


def test_engine_results_are_filtered_too(pc, monkeypatch):
    """with the engine running: its answer passes through the same check, and a few more are asked when some left"""
    asked = []
    docs, data = pc.docs, pc.data

    class Op:
        def open(self, req, timeout=0):
            k = int(dict(x.split("=", 1) for x in req.full_url.split("?", 1)[1].split("&"))["k"])
            asked.append(k)
            res = [{"name": "a.docx", "path": f"{data}/app/a.docx", "prob": 0.9},
                   {"name": "b.docx", "path": f"{docs}/b.docx", "prob": 0.5},
                   {"name": "c.docx", "path": f"{docs}/../data/c.docx", "prob": 0.4},
                   {"name": "d.docx", "path": f"{docs}/d.docx", "prob": 0.3},
                   {"name": "e.docx", "path": f"{docs}/e.docx", "prob": 0.2}][:k]
            return io.BytesIO(json.dumps({"results": res, "note": ""}).encode())

    monkeypatch.setattr(pc, "_engine_up", lambda: True)
    monkeypatch.setattr(pc.urllib.request, "build_opener", lambda *a: Op())
    r = pc.search_files("plan", limit=3)
    assert r["engine"] == "full" and _paths(r) == [f"{docs}/b.docx", f"{docs}/d.docx", f"{docs}/e.docx"], r
    assert asked[0] == 3 and len(asked) == 2 and asked[1] > 3


def test_deny_list_is_read_at_each_call(pc, monkeypatch):
    assert pc.read_file(pc.docs + "/plan_final.docx").get("text")
    monkeypatch.setenv("FINDR_MCP_DENY", os.pathsep.join([pc.docs, pc.priv]))
    assert pc.read_file(pc.docs + "/plan_final.docx") == NOT_INDEXED
    assert _paths(pc.search_files("sensor", limit=30)) == []
