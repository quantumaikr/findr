"""red-team pass on 0.2.26 (11/15): leaks closed after it, by general rules rather than one more literal each. Every
name, value and key here is invented, and values are built from pieces so that no whole one sits in the source"""
import importlib
import os
import sqlite3
import subprocess
import sys
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

import index
import moretext

V = "Quo" + "kkaR4m"   # the invented secret value every case hides


# ---------------------------------------------------------------- 6. labels spelled to pass, HTML
@pytest.mark.parametrize("label", ["pаssword", "ｐａｓｓｗｏｒｄ", "PASS​WORD",
                                   "pass­word", "pаss‍wоrd", "\U0001d429\U0001d41a\U0001d42c\U0001d42c\U0001d430\U0001d428\U0001d42b\U0001d41d",
                                   "api⁠_key", "аpi_key", "тоkеn", "비밀​번호", "password&#58;", "password&colon;"])
def test_look_alike_labels(label):
    sep = "" if label.endswith(";") else ": "
    text = "intro line\n" + label + sep + " " + V + "\nкириллица остаётся\n"
    out = index.mask(text)
    assert V not in out, out
    assert out.startswith("intro line\n" + label) and out.endswith("\nкириллица остаётся\n"), out   # the text keeps its letters


def test_fullwidth_separator_and_header():
    assert V not in index.mask("password：" + V)
    assert index.cred_header("Sіte,Usеr,Pаssword\n")
    assert V not in index.mask("Site，User，Password\nbank，kim，" + V + "\n")


def test_russian_text_is_left_as_it_is():
    t = "Все хорошо, отчёт готов.\nсекрет не здесь\nα β γ\n"
    assert index.mask(t) == t
    assert index.match_copy("ok")[1] is None


def test_mapping_back_keeps_other_text():
    text = "앞 글​ 그대로 pаssword: " + V + " 끝\n다음 줄 &amp; 그대로\n"
    assert index.mask(text) == "앞 글​ 그대로 pаssword: [가림]\n다음 줄 &amp; 그대로\n"


def test_html_file_entities_and_tables(tmp_path):
    p = tmp_path / "page.html"
    p.write_text("<html><body><table><tr><th>Site</th><th>Login</th><th>Password</th></tr>\n<tbody><tr><td>bank</td><td>kim</td>"
                 f"<td>{V}1</td></tr></tbody></table><p>pass<b>word</b>&#58; {V}2</p><p>api&#x5F;key&#x3A; {V}3</p></body></html>",
                 encoding="utf-8")
    t = index.extract(p, 10000)
    assert "Site\tLogin\tPassword\nbank\tkim\t" + V + "1" in t, repr(t)
    out = index.mask(t)
    assert V not in out, out


def test_html_mail_table(tmp_path):
    m = EmailMessage(); m["From"] = "a@example.invalid"; m["To"] = "b@example.invalid"; m["Subject"] = "list"
    m.set_content("<table><tr><td>Site</td><td>User</td><td>Password</td></tr><tr><td>x</td><td>y</td><td>" + V + "</td></tr></table>",
                  subtype="html")
    p = tmp_path / "m.eml"
    p.write_bytes(m.as_bytes())
    t = moretext.eml_text(p)
    assert "Site\tUser\tPassword" in t and V not in index.mask(t), t


def test_plain_mail_keeps_its_tabs(tmp_path):
    m = EmailMessage(); m["From"] = "a@example.invalid"; m["To"] = "b@example.invalid"; m["Subject"] = "list"
    m.set_content("site\tuser\tpassword\nbank\tkim\t" + V + "\n")
    p = tmp_path / "m.eml"
    p.write_bytes(m.as_bytes())
    assert V not in index.mask(moretext.eml_text(p))


def test_remask_looks_at_text_the_hints_cannot_see(capsys):
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);"
                      "CREATE TABLE chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);"
                      "CREATE VIRTUAL TABLE fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');"
                      "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);")
    con.execute("INSERT INTO files VALUES(1, '/d/n.txt', 'n.txt', '.txt', 1, 0, 1)")
    con.execute("INSERT INTO chunks VALUES(1, 1, 0, ?, NULL)", ("pаsswоrd: " + V,))
    con.execute("INSERT INTO chunks VALUES(2, 1, 1, ?, NULL)", ("ｔｏｋｅｎ＝" + V,))
    index.remask(con)
    assert V not in "".join(t for (t,) in con.execute("SELECT text FROM chunks"))


