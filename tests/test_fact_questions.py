"""questions made from file facts (tools/fact_cards.py, tools/gen_questions.py): every question has exactly one right file,
version and person cues say what the index holds, and what is printed carries no names. All files are synthetic."""
import datetime as dt
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import traces

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


FC, GQ = _load("fact_cards"), _load("gen_questions")
NOW = dt.date(2026, 11, 20)
QUOTE = "견적 품목 노트북 거치대 모니터암 단가 수량 납품 일정 견적 품목 거치대 " * 4
MEET = "주간 회의 안건 결정 사항 후속 조치 담당자 일정 회의 안건 결정 " * 4
PLAN = "사업 계획 시장 분석 목표 매출 투자 계획 사업 시장 목표 분석 " * 4
T = lambda s: dt.datetime.fromisoformat(s).timestamp()


def make(tmp_path, files):
    """files: {relative path: (text, modified time)} -> an index over real files in tmp (schema as index.py makes it)"""
    con = sqlite3.connect(tmp_path / "index.db")
    con.executescript("""CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
        CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');
        CREATE TABLE cards(file INTEGER PRIMARY KEY, outline TEXT, summary TEXT, done INT DEFAULT 0);""")
    ids = {}
    for i, (rel, (text, mt)) in enumerate(files.items(), 1):
        p = tmp_path / "pc" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        os.utime(p, (mt, mt))
        con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,1)", (i, p.as_posix(), p.name, p.suffix, p.stat().st_size, mt))
        con.execute("INSERT INTO chunks VALUES(?,?,0,?,NULL)", (i, i, text))
        ids[rel] = i
    traces.ensure(con)
    con.commit()
    return con, ids


def office(tmp_path):
    con, ids = make(tmp_path, {
        "영업/견적서_v1.txt": (QUOTE, T("2026-09-01T10:00")),
        "영업/견적서_v2.txt": (QUOTE + "할인 ", T("2026-10-05T10:00")),
        "영업/견적서_최종.txt": (QUOTE + "할인 확정 ", T("2026-11-03T10:00")),
        "회의/주간회의록.txt": (MEET, T("2026-10-15T09:00")),
        "회의/주간회의록2.txt": (MEET + "참고 ", T("2026-10-16T09:00")),   # the same words, also from 김민수 by mail
        "기획/사업계획서.txt": (PLAN, T("2026-11-10T15:00")),
    })
    ev = [("회의/주간회의록.txt", "saved_from_mail", "2026-10-15T09:00", "김민수", "mail"),
          ("회의/주간회의록2.txt", "saved_from_mail", "2026-10-16T09:00", "김민수", "mail"),
          ("기획/사업계획서.txt", "printed", "2026-11-10T16:00", None, None),
          ("영업/견적서_최종.txt", "sent", "2026-11-03T11:00", "박영업", None)]
    for rel, typ, t, person, source in ev:
        con.execute("INSERT INTO events(file, type, time, person, source) VALUES(?,?,?,?,?)", (ids[rel], typ, t, person, source))
    con.execute("INSERT INTO docmeta VALUES(?,?,?,?)", (ids["기획/사업계획서.txt"], "이영희", "이영희", 3))
    con.commit()
    return con, ids


def _facts_file(tmp_path, con):
    out = tmp_path / "facts.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for x in FC.facts(con):
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    return out


def test_facts_read_what_the_index_holds(tmp_path):
    con, ids = office(tmp_path)
    fs = FC.facts(con)
    by = lambda rel_path, rel: [x["value"] for x in fs if x["file"] == ids[rel_path] and x["rel"] == rel]
    assert by("회의/주간회의록.txt", "ev:saved_from_mail") == ["김민수"]
    assert by("기획/사업계획서.txt", "author") == ["이영희"] and by("기획/사업계획서.txt", "last_by") == []   # same person once
    assert by("기획/사업계획서.txt", "ev:printed") == [""]
    assert by("영업/견적서_최종.txt", "version") == ["latest"]          # by modified time, not by the name
    assert by("영업/견적서_v2.txt", "version") == ["before_latest"]
    assert by("영업/견적서_v1.txt", "version") == ["first"]
    assert by("기획/사업계획서.txt", "version") == []                    # one file is no family
    terms = [x for x in fs if x["file"] == ids["영업/견적서_v1.txt"] and x["rel"] == "term"]
    assert [x["rank"] for x in terms] == list(range(len(terms))) and "견적" in [x["value"] for x in terms[:3]]
    rep = json.dumps(FC.counts(fs), ensure_ascii=False)
    assert "김민수" not in rep and "/" not in rep and "견적" not in rep


def test_index_is_opened_read_only(tmp_path):
    con, _ = office(tmp_path); con.close()
    ro = FC.connect(tmp_path / "index.db")
    assert FC.facts(ro)   # terms are worked out without being stored
    try:
        ro.execute("CREATE TABLE x(a)")
        assert False, "the index must not be writable through the tool"
    except sqlite3.OperationalError:
        pass


def test_every_question_has_exactly_one_right_file(tmp_path):
    con, ids = office(tmp_path)
    F = GQ.load(_facts_file(tmp_path, con))
    qs, st = GQ.generate(F, NOW, 300, seed=0)
    assert qs and st["questions"] == len(qs)
    path_of = {f["path"]: fid for fid, f in F.items()}
    for q in qs:
        fid = path_of[q["answers"][0]]
        assert GQ.fits(F[fid], q["facts"], NOW)
        assert not [g for g in F if g != fid and GQ.fits(F[g], q["facts"], NOW)], q["q"]
        assert q["answers"][0] not in q["negatives"]
    twins = {ids["회의/주간회의록.txt"], ids["회의/주간회의록2.txt"]}
    for q in qs:   # the two minutes share their words and sender: only a cue that tells them apart may ask for one
        if path_of[q["answers"][0]] in twins and any(p["kind"] == "ev" and p["value"] == "김민수" for p in q["facts"]):
            assert any(p["kind"] == "term" and p["value"] == "참고" for p in q["facts"]) or \
                   any(p.get("period") for p in q["facts"]), q["q"]


def test_version_questions_name_the_version_and_keep_the_others_as_negatives(tmp_path):
    con, ids = office(tmp_path)
    F = GQ.load(_facts_file(tmp_path, con))
    qs, _ = GQ.generate(F, NOW, 300, seed=1)
    prev = [q for q in qs if "version:before_latest" in q["shape"]]
    assert prev, "the family of three should give a 'right before the final' question"
    for q in prev:
        assert q["answers"][0].endswith("견적서_v2.txt")
        assert "직전" in q["q"] or "바로 전" in q["q"]
        assert {Path(p).name for p in q["negatives"]} >= {"견적서_v1.txt", "견적서_최종.txt"}


def test_people_and_periods_are_said_as_people_say_them():
    assert (GQ.ig("김민수"), GQ.ig("박영업"), GQ.ig("이영희 님")) == ("김민수가", "박영업이", "이영희 님이")
    p = lambda d, now=NOW: GQ.period(d, now)
    assert p("2026-11-03")[1][0] == "이번 달" and p("2026-10-15")[1][0] == "지난달"
    assert p("2026-03-01")[1][0] == "올해 3월" and p("2025-05-05")[1][0] == "작년" and p("2023-01-01")[1][0] == "2023년"
    assert p("2026-12-01") is None and p("not a day") is None
    assert p("2025-12-20", dt.date(2026, 1, 10))[1][0] == "지난달"   # last month across the new year
    assert p("2026-10-15")[0] == p("2026-10-01")[0] != p("2026-09-30")[0]


def test_same_seed_same_questions_and_the_report_holds_no_names(tmp_path):
    con, _ = office(tmp_path)
    facts = _facts_file(tmp_path, con)
    a = GQ.main([str(facts), "--out", str(tmp_path / "a.jsonl"), "--now", "2026-11-20", "--n", "50", "--seed", "3"])
    GQ.main([str(facts), "--out", str(tmp_path / "b.jsonl"), "--now", "2026-11-20", "--n", "50", "--seed", "3"])
    assert (tmp_path / "a.jsonl").read_text(encoding="utf-8") == (tmp_path / "b.jsonl").read_text(encoding="utf-8")
    rep = json.dumps(a, ensure_ascii=False)
    assert "김민수" not in rep and "/" not in rep and "견적" not in rep
    assert 0 < a["distinct2"] <= 1 and a["questions"] > 0
    rows = [json.loads(l) for l in open(tmp_path / "a.jsonl", encoding="utf-8")]
    assert all(r["kind"] == "known" and r["answers"] and r["id"].startswith("G") for r in rows)   # real_eval's format
