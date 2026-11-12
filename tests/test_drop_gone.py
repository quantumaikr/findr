"""files deleted or moved since they were indexed leave the answer at once, instead of being listed until the next index
run (opening one said 'moved or deleted'). A gone line gives its place to a copy or a version still there."""
import sqlite3

import search as S


def _db(rows):
    con = sqlite3.connect(":memory:")
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
                         CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);""")
    for p in rows:
        fid = con.execute("INSERT INTO files(path,name,size,mtime) VALUES(?,?,?,?)", (str(p), p.name, 3, 1700000000.0)).lastrowid
        con.execute("INSERT INTO chunks(file,n,text) VALUES(?,0,?)", (fid, "text of " + p.name))
    return con


def _line(p, **kw):
    return {"path": str(p), "name": p.name, "fid": -1, "snippet": "old", "date": "2020-01-01 00:00", **kw}


def test_a_deleted_file_leaves_the_answer(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_text("x"); b.write_text("x")
    con = _db([a, b])
    b.unlink()
    res = S.drop_gone(con, {"results": [_line(b), _line(a)]})
    assert [x["path"] for x in res["results"]] == [str(a)]
    assert res["gone"] == 1


def test_a_present_answer_is_untouched(tmp_path):
    a = tmp_path / "a.pdf"; a.write_text("x")
    res0 = {"results": [_line(a)]}
    res = S.drop_gone(_db([a]), res0)
    assert res["results"] == res0["results"] and "gone" not in res


def test_a_copy_takes_the_line_with_its_own_details(tmp_path):
    a = tmp_path / "a.pdf"; c = tmp_path / "sub" / "a (1).pdf"
    c.parent.mkdir(); a.write_text("x"); c.write_text("x")
    con = _db([a, c])
    a.unlink()
    res = S.drop_gone(con, {"results": [_line(a, copies=[str(c)])]})
    x = res["results"][0]
    assert x["path"] == str(c) and x["name"] == "a (1).pdf" and x["snippet"] == "text of a (1).pdf"
    assert x["fid"] == con.execute("SELECT id FROM files WHERE path=?", (str(c),)).fetchone()[0]
    assert "copies" not in x


def test_a_version_takes_the_line_when_no_copy_is_left(tmp_path):
    a = tmp_path / "plan_final.hwp"; v1 = tmp_path / "plan_v1.hwp"; v2 = tmp_path / "plan_v2.hwp"
    for p in (a, v1, v2):
        p.write_text("x")
    con = _db([a, v1, v2])
    a.unlink(); v1.unlink()
    line = _line(a, versions=[{"path": str(v1)}, {"path": str(v2)}])
    res = S.drop_gone(con, {"results": [line]})
    assert [x["path"] for x in res["results"]] == [str(v2)]
    assert "versions" not in res["results"][0]
    assert res["gone"] == 2


def test_gone_copies_under_a_present_line_are_dropped(tmp_path):
    a = tmp_path / "a.pdf"; c = tmp_path / "a (1).pdf"
    a.write_text("x"); c.write_text("x")
    con = _db([a, c])
    c.unlink()
    res = S.drop_gone(con, {"results": [_line(a, copies=[str(c)])]})
    assert res["results"][0]["path"] == str(a) and "copies" not in res["results"][0]


def test_a_file_on_a_missing_drive_is_kept(tmp_path):
    off = tmp_path / "unplugged" / "doc.pdf"   # its folder is not there either: an offline drive, not a deletion
    res = S.drop_gone(_db([]), {"results": [_line(off)]})
    assert [x["path"] for x in res["results"]] == [str(off)] and "gone" not in res


def test_only_the_top_lines_are_checked(tmp_path):
    files = [tmp_path / f"f{i}.pdf" for i in range(25)]
    for p in files:
        p.write_text("x")
    con = _db(files)
    files[22].unlink()   # below the lines a person reads: left to the next index run
    res = S.drop_gone(con, {"results": [_line(p) for p in files]}, top=20)
    assert len(res["results"]) == 25


def test_a_windows_drive_path_seen_from_wsl_is_read_as_mnt():
    if S.os.name == "nt":
        return
    assert S._local("C:\\Users\\me\\a.pdf") == "/mnt/c/Users/me/a.pdf"
    assert S._local("/home/me/a.pdf") == "/home/me/a.pdf"
