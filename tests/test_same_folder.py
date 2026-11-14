"""red-team pass (11/14): findr's data folder and the FINDR_MCP_DENY folders under other spellings of the same folder
(macOS firmlinks /System/Volumes/Data/..., letter case, a linked folder, Windows \\\\?\\, \\\\localhost\\C$\\, trailing
dots), and links with harmless names that lead to a secret, into the data folder or out of the chosen folders.
Invented names and contents only"""
import importlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import index
import samepath

DARWIN = sys.platform == "darwin"


def _link(link, target, is_dir=False):
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("this PC cannot make links (Windows without the right)")


# ---------------------------------------------------------------- text forms
@pytest.mark.parametrize("given,want", [
    ("\\\\?\\C:\\Users\\me\\Secret\\a.txt", "C:\\Users\\me\\Secret\\a.txt"),
    ("\\\\?\\UNC\\server\\share\\x", "\\\\server\\share\\x"),
    ("\\\\localhost\\C$\\Users\\me\\Secret\\a.txt", "C:\\Users\\me\\Secret\\a.txt"),
    ("\\\\127.0.0.1\\d$\\data", "D:\\data"),
    ("C:\\Users\\me\\Secret.\\a.txt", "C:\\Users\\me\\Secret\\a.txt"),
    ("C:\\Users\\me\\Secret \\a.txt ", "C:\\Users\\me\\Secret\\a.txt"),
    ("C:/Users/me/x", "C:\\Users\\me\\x"),
])
def test_windows_forms(given, want):
    assert samepath.win_text(given) == want


def test_deny_list_keeps_windows_paths_whole():
    sep = os.pathsep
    got = samepath.split_list(f"/a/b{sep}C:\\Secret{sep}D:/x;E:\\y")
    if os.name == "nt":
        assert got == ["/a/b", "C:\\Secret", "D:/x", "E:\\y"]
    else:
        assert got == ["/a/b", "C:\\Secret", "D:/x", "E:\\y"], got


@pytest.mark.skipif(not DARWIN, reason="macOS firmlinks")
def test_unfirm(tmp_path):
    real = os.path.realpath(tmp_path)
    assert samepath.unfirm("/System/Volumes/Data" + real) == real
    assert samepath.unfirm("/System/Volumes/Data/no/such/place") == "/System/Volumes/Data/no/such/place"


# ---------------------------------------------------------------- the indexer
def _tree(base):
    """a root with a document, findr's data folder inside it, a secret file, and links"""
    root = base / "redteam"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "notes.txt").write_text("alpha project plan harborlight", encoding="utf-8")
    home = root / "home"; home.mkdir()
    (home / "appmemo.txt").write_text("data folder secret note zebracorn", encoding="utf-8")
    return root, home


def _index(roots, db, home):
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_ORIGIN": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home)}
    r = subprocess.run([sys.executable, str(Path(index.__file__)), "--root", *map(str, roots), "--db", str(db), "--no-embed"],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0, r.stderr[-800:]
    con = sqlite3.connect(db)
    names = {n for (n,) in con.execute("SELECT name FROM files")}
    text = " ".join(t or "" for (t,) in con.execute("SELECT text FROM chunks"))
    tiers = dict(con.execute("SELECT name, tier FROM files"))
    con.close()
    return names, text, tiers


def _spellings(root):
    """other spellings of `root` this PC takes for the same folder"""
    out = []
    real = os.path.realpath(root)
    if DARWIN and os.path.isdir("/System/Volumes/Data" + real):
        out.append(("firmlink", Path("/System/Volumes/Data" + real)))
    up = Path(str(root)[:-len(root.name)] + root.name.upper())
    if os.path.isdir(up) and os.path.samefile(up, root):
        out.append(("case", up))
    return out


@pytest.mark.parametrize("how", ["firmlink", "case", "link"])
def test_data_folder_left_out_under_another_spelling(tmp_path, how):
    root, home = _tree(tmp_path)
    if how == "link":
        alias = tmp_path / "alias"; _link(alias, root, True)
    else:
        alias = dict(_spellings(root)).get(how)
        if alias is None:
            pytest.skip(f"no {how} spelling on this PC")
    names, text, _ = _index([alias], tmp_path / "i.db", home)
    assert "notes.txt" in names and "harborlight" in text
    assert "appmemo.txt" not in names and "zebracorn" not in text, names


def test_rows_from_another_spelling_leave(tmp_path):
    """an index made while the data folder was not findr's, under the other spelling: its rows leave on the next run"""
    root, home = _tree(tmp_path)
    alias = dict(_spellings(root)).get("firmlink") or dict(_spellings(root)).get("case")
    if alias is None:
        alias = tmp_path / "alias"; _link(alias, root, True)
    names, _, _ = _index([alias], tmp_path / "i.db", tmp_path / "elsewhere")
    assert "appmemo.txt" in names
    names, text, _ = _index([alias], tmp_path / "i.db", home)
    assert "appmemo.txt" not in names and "zebracorn" not in text, names


def test_links_with_harmless_names(tmp_path):
    root, home = _tree(tmp_path)
    outside = tmp_path / "outside"; (outside / ".ssh").mkdir(parents=True)
    (outside / ".ssh" / "config").write_text("Host fake\n  IdentityFile ~/.ssh/id_fake\n", encoding="utf-8")
    (outside / "elsewhere.txt").write_text("outside words quillfeather", encoding="utf-8")
    (root / "docs" / "Chrome Passwords.csv").write_text("url,username,password\nhttps://bank.example,jdoe_fake,PwFakeLink1\n", encoding="utf-8")
    _link(root / "docs" / "shortcut_notes.txt", root / "docs" / "Chrome Passwords.csv")
    _link(root / "docs" / "ssh_config_link", outside / ".ssh" / "config")
    _link(root / "docs" / "outside_link.txt", outside / "elsewhere.txt")
    _link(root / "docs" / "memo_link.txt", home / "appmemo.txt")
    names, text, tiers = _index([root], tmp_path / "i.db", home)
    assert tiers["shortcut_notes.txt"] == 0 and tiers["ssh_config_link"] == 0, tiers
    assert tiers["outside_link.txt"] == 3, tiers   # out of the chosen folders: its name only
    assert "memo_link.txt" not in names
    assert not any(w in text for w in ("PwFakeLink1", "IdentityFile", "quillfeather", "zebracorn")), text


def test_is_secret_follows_links(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "config").write_text("Host fake\n", encoding="utf-8")
    (tmp_path / "logins.csv").write_text("url,username,password\n", encoding="utf-8")
    _link(tmp_path / "sshcfg", tmp_path / ".ssh", True)
    _link(tmp_path / "notes.txt", tmp_path / "logins.csv")
    assert index.is_secret(tmp_path / "sshcfg" / "config")
    assert index.is_secret(tmp_path / "notes.txt") and index.tier(tmp_path / "notes.txt") == 0


# ---------------------------------------------------------------- the MCP tools
NOT_INDEXED = {"error": "not a file in findr's index; use a path returned by search_files"}


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    root, home = _tree(tmp_path)
    (root / "docs" / "denyme").mkdir()
    (root / "docs" / "denyme" / "deny_plan.txt").write_text("deny plan words", encoding="utf-8")
    monkeypatch.setenv("FINDR_HOME", str(home)); monkeypatch.setenv("FINDR_APP_PORT", "9")
    monkeypatch.setenv("FINDR_MCP_DENY", str(root / "docs" / "denyme") + os.pathsep + "C:\\Users\\me\\Secret")
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    con = sqlite3.connect(str(home / "index.db"))
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT);
        CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');""")
    m.rows = []
    def add(p, text, tier=1):
        i = len(m.rows) + 1; name = str(p).replace("\\", "/").rsplit("/", 1)[1]
        con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)", (i, str(p), name, os.path.splitext(name)[1], 10, 1700000000 + i, tier))
        con.execute("INSERT INTO chunks VALUES(?,?,0,?)", (i, i, text))
        con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (i, name, str(p), text)); m.rows.append(str(p))
    for alias in [root] + [a for _, a in _spellings(root)]:
        add(alias / "home" / "appmemo.txt", "data folder note zebracorn")
        add(alias / "docs" / "denyme" / "deny_plan.txt", "deny plan zebracorn")
    add(root / "docs" / "notes.txt", "alpha project plan zebracorn")
    for w in ("C:\\Users\\me\\Secret\\plan.txt", "C:\\Users\\me\\Secret.\\plan2.txt", "\\\\localhost\\C$\\Users\\me\\Secret\\plan3.txt",
              "\\\\?\\C:\\Users\\me\\Secret\\plan4.txt"):
        add(w, "windows deny zebracorn")
    con.commit(); con.close()
    m.root, m.home = root, home
    return m


def test_mcp_deny_under_every_spelling(mcp):
    got = [x["path"] for x in mcp.search_files("zebracorn", limit=30)["results"]]
    assert got == [str(mcp.root / "docs" / "notes.txt")], got
    for p in mcp.rows:
        if p != str(mcp.root / "docs" / "notes.txt"):
            assert mcp.read_file(p) == NOT_INDEXED, p
            assert mcp.versions(p).get("error") and mcp.file_relations(p).get("error"), p
    assert mcp.list_folder(str(mcp.root / "home"))["files"] == []


def test_mcp_secret_link_is_not_read(mcp, tmp_path):
    (tmp_path / "logins.csv").write_text("url,username,password\nhttps://bank.example,jdoe_fake,PwFakeMcp1\n", encoding="utf-8")
    lk = mcp.root / "docs" / "harmless.txt"
    _link(lk, tmp_path / "logins.csv")
    assert mcp._denied(str(lk))
    out = tmp_path / "outside.txt"; out.write_text("outside words", encoding="utf-8")
    lk2 = mcp.root / "docs" / "outside_link.txt"; _link(lk2, out)
    con = sqlite3.connect(str(mcp.home / "index.db"))
    con.execute("INSERT INTO files VALUES(99,?,?,'.txt',10,1700000099,1)", (str(lk2), "outside_link.txt"))
    con.execute("INSERT INTO chunks VALUES(99,99,0,'')"); con.commit(); con.close()
    r = mcp.read_file(str(lk2))
    assert "outside words" not in r.get("text", ""), r   # a link out of the chosen folders is not followed by a read now
