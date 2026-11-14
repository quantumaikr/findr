"""relations between files (#203): characteristic words, stable file ids, version-family ids, same-content copies.
All files are made here from synthetic text."""
import importlib
import json
import os
import sqlite3

import pytest

import index
import relations

BODY_A = "낙엽 수거 계획서 낙엽 수거 일정과 낙엽 처리 비용을 정리한다 수거함 위치 수거함 개수 " * 4
BODY_B = "quarterly telescope budget review: telescope mirror polishing, mirror coating and telescope scheduling. " * 4
OTHER = "회의 참석자 명단과 안건 결정 사항 후속 조치 일정 담당자 연락처 " * 3


def make(tmp_path, files):
    """files: {relative path: (text, mtime)} -> an index (old schema: no relation tables) over real files in tmp"""
    db = tmp_path / "index.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
        CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');
        CREATE TABLE cards(file INTEGER PRIMARY KEY, outline TEXT, summary TEXT, done INT DEFAULT 0);""")
    for i, (rel, (text, mt)) in enumerate(files.items(), 1):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        os.utime(p, (mt, mt))
        con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,1)", (i, p.as_posix(), p.name, p.suffix, p.stat().st_size, mt))
        con.execute("INSERT INTO chunks VALUES(?,?,0,?,NULL)", (i, i, text))
    con.commit()
    return con


def test_terms_are_the_files_own_words_without_particles(tmp_path):
    con = make(tmp_path, {"a/계획.txt": (BODY_A, 1.7e9), "b/memo.txt": (OTHER, 1.7e9), "c/budget.txt": (BODY_B, 1.7e9)})
    t = relations.terms(con, 1)
    assert t[:2] == ["낙엽", "수거"] or {"낙엽", "수거"} <= set(t[:3])
    assert "수거함" in t and "회의" not in t
    assert relations.terms(con, 3)[0] == "telescope"
    assert relations.tokens("낙엽을 수거 계획서의 the Telescope") == ["낙엽", "수거", "계획서", "telescope"]   # the same particle strip as the search's
    again = relations.terms(con, 1)   # kept in file_terms, not computed again
    assert again == t and con.execute("SELECT count(*) FROM file_terms").fetchone()[0] == 2
    assert relations.terms(con, 99) == []


def test_file_id_is_kept_when_the_row_stays_and_when_the_file_is_edited_or_moved(tmp_path):
    con = make(tmp_path, {"a/보고서.txt": (BODY_A, 1.7e9), "b/other.txt": (OTHER, 1.7e9)})
    u1, u2 = relations.file_id(con, 1), relations.file_id(con, 2)
    assert u1 and u2 and u1 != u2 and relations.file_id(con, 1) == u1
    # edited in place: the indexer drops the row and adds a new one at the same path
    path = con.execute("SELECT path FROM files WHERE id=1").fetchone()[0]
    index.drop(con, 1)
    open(path, "a", encoding="utf-8").write("추가 문장")
    st = os.stat(path)
    con.execute("INSERT INTO files VALUES(10,?,?,?,?,?,1)", (path, "보고서.txt", ".txt", st.st_size, st.st_mtime))
    assert relations.file_id(con, 10) == u1
    # moved with its bytes and time: the same volume and inode
    old = con.execute("SELECT path FROM files WHERE id=2").fetchone()[0]
    index.drop(con, 2)
    new = str(tmp_path / "moved" / "renamed.txt")
    os.makedirs(os.path.dirname(new))
    os.rename(old, new)
    st = os.stat(new)
    con.execute("INSERT INTO files VALUES(11,?,?,?,?,?,1)", (new, "renamed.txt", ".txt", st.st_size, st.st_mtime))
    assert relations.file_id(con, 11) == u2
    assert con.execute("SELECT count(*) FROM file_uid_gone").fetchone()[0] == 0   # the notes were used


def test_moved_by_copy_takes_the_id_by_content_and_a_copy_is_a_new_file(tmp_path):
    con = make(tmp_path, {"a/x.txt": (BODY_B, 1.7e9)})
    u = relations.file_id(con, 1)
    assert relations.content_sig(con, 1)   # content known: a move by copy-and-delete can be told
    old = con.execute("SELECT path FROM files WHERE id=1").fetchone()[0]
    index.drop(con, 1)
    new = tmp_path / "z" / "x2.txt"
    new.parent.mkdir()
    new.write_text(BODY_B, encoding="utf-8")   # another inode, another time, the same bytes
    os.remove(old)
    con.execute("INSERT INTO files VALUES(5,?,?,?,?,?,1)", (new.as_posix(), "x2.txt", ".txt", new.stat().st_size, new.stat().st_mtime))
    assert relations.file_id(con, 5) == u
    cp = tmp_path / "z" / "copy.txt"   # a copy while the file is still there: its own id
    cp.write_text(BODY_B, encoding="utf-8")
    con.execute("INSERT INTO files VALUES(6,?,?,?,?,?,1)", (cp.as_posix(), "copy.txt", ".txt", cp.stat().st_size, cp.stat().st_mtime))
    assert relations.file_id(con, 6) not in (None, u)


def test_dropping_a_row_removes_its_relation_rows(tmp_path):
    con = make(tmp_path, {"a/x.txt": (BODY_A, 1.7e9)})
    relations.terms(con, 1); relations.file_id(con, 1)
    index.drop(con, 1)
    assert con.execute("SELECT count(*) FROM file_uid").fetchone()[0] == 0 and con.execute("SELECT count(*) FROM file_terms").fetchone()[0] == 0


def test_same_content_copies_by_bytes_not_by_name_or_size(tmp_path):
    con = make(tmp_path, {"a/계약.txt": (BODY_A, 1.7e9), "b/이름이 다른 사본.txt": (BODY_A, 1.6e9),
                          "c/same size.txt": (BODY_A.replace("낙", "남"), 1.7e9), "d/else.txt": (OTHER, 1.7e9)})
    c = relations.copies(con, 1)
    assert [x["name"] for x in c] == ["이름이 다른 사본.txt"] and c[0]["id"] and c[0]["fid"] == 2
    assert relations.copies(con, 4) == []
    os.remove(con.execute("SELECT path FROM files WHERE id=2").fetchone()[0])   # a copy no longer on disk is not offered
    assert relations.copies(con, 1) == []


def test_family_id_is_shared_and_merges_to_the_smallest(tmp_path):
    con = make(tmp_path, {"a/p.txt": (BODY_A, 1.7e9), "a/p v2.txt": (BODY_A + "x", 1.7e9), "b/q.txt": (OTHER, 1.7e9), "b/q 최종.txt": (OTHER + "y", 1.7e9)})
    f1 = relations.family_id(con, 1, [2])
    f2 = relations.family_id(con, 3, [4])
    assert f1 and f2 and f1 != f2 and relations.family_id(con, 2, [1]) == f1
    merged = relations.family_id(con, 1, [3])   # found to be one family after all: the smaller id wins, all four follow
    assert merged == min(f1, f2)
    assert {r[0] for r in con.execute("SELECT fam FROM file_uid")} == {merged}


def test_read_only_index_answers_without_ids(tmp_path):
    con = make(tmp_path, {"a/x.txt": (BODY_A, 1.7e9)}); con.close()
    ro = sqlite3.connect(f"file:{tmp_path / 'index.db'}?mode=ro", uri=True)
    assert relations.file_id(ro, 1) is None and relations.family_id(ro, 1, []) is None
    assert relations.terms(ro, 1)   # terms are still computed, only not kept


@pytest.fixture
def mcp_pc(tmp_path, monkeypatch):
    monkeypatch.setenv("FINDR_HOME", str(tmp_path / "home")); monkeypatch.setenv("FINDR_APP_PORT", "9")
    (tmp_path / "home").mkdir()
    import findr.paths
    importlib.reload(findr.paths)
    m = importlib.reload(importlib.import_module("findr.mcp_server"))
    con = make(tmp_path, {"p/제안서.txt": (BODY_A, 1.7e9), "p/제안서_최종.txt": (BODY_A + "끝", 1.7e9), "q/사본.txt": (BODY_A, 1.6e9),
                          "q/else.txt": (OTHER, 1.7e9)})
    con.close()
    os.replace(tmp_path / "index.db", tmp_path / "home" / "index.db")
    # the files table holds paths of the moved db's old place: they are real files, so nothing else changes
    return m, tmp_path


def test_file_relations_tool_and_versions_additive_fields(mcp_pc):
    m, tmp = mcp_pc
    p = (tmp / "p" / "제안서.txt").as_posix()
    r = m.file_relations(p)
    assert r["file"]["file_id"].startswith("f_") and r["file"]["family_id"].startswith("v_") and r["file"]["content_sha1"]
    assert [x["name"] for x in r["same_content"]] == ["사본.txt"] and r["same_content"][0]["file_id"]
    assert r["terms"][0] in ("낙엽", "수거")
    assert {x["name"] for x in r["family"]} >= {"제안서_최종.txt", "사본.txt"}
    v = m.versions(p)   # the same ids, the old fields untouched
    assert v["file"]["family_id"] == r["file"]["family_id"] and v["file"]["file_id"] == r["file"]["file_id"]
    assert {"name", "path", "modified"} <= set(v["file"]) and all({"relation", "name", "path", "modified"} <= set(o) for o in v["others"])
    assert m.file_relations(p)["file"]["file_id"] == r["file"]["file_id"]   # stable between calls
    assert "error" in m.file_relations(str(tmp / "nope.txt"))


def test_old_index_migrates_in_place(mcp_pc):
    m, tmp = mcp_pc
    db = tmp / "home" / "index.db"
    before = sqlite3.connect(db).execute("SELECT count(*) FROM sqlite_master WHERE name IN ('file_uid','file_uid_gone','file_terms')").fetchone()[0]
    assert before == 0
    m.file_relations((tmp / "q" / "else.txt").as_posix())
    con = sqlite3.connect(db)
    assert {"file_uid", "file_uid_gone", "file_terms"} <= {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 4   # nothing was re-read or changed


def test_cli_relations_prints_json(mcp_pc, capsys, monkeypatch):
    m, tmp = mcp_pc
    from findr import cli
    importlib.reload(cli)
    assert cli.main(["relations", str(tmp / "p" / "제안서.txt")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["file"]["family_id"].startswith("v_") and out["same_content"]
