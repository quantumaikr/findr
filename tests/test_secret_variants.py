"""red-team pass (11/14): secret files under the names people's systems really give them. Copies ('logins 2.csv',
'Copy of logins.csv'), a second extension ('passwords.csv.txt'), decomposed Korean, trailing spaces and dots ('.env ',
'id_rsa.'), more key files (.envrc, .npmrc, kubeconfig), a password export under any name (its first line), and the same
rules for mail attachments, with a mail inside a mail kept to its name. Invented names and values only"""
import os
import sqlite3
import subprocess
import sys
import unicodedata
from email.message import EmailMessage
from pathlib import Path

import pytest

import index
import moretext

CRED = "url,username,password\nhttps://bank.example,jdoe_fake,PwFakeVariant1\n"

NAMES = ["logins 2.csv", "Copy of logins.csv", "copy of Copy of logins.csv", "logins - Copy (2).csv", "logins copy.csv",
         "passwords.csv.txt", "passwords.csv.bak", "비밀번호 2.csv", "비밀번호 - 복사본.csv", "비밀번호 (1).csv",
         unicodedata.normalize("NFD", "비밀번호.csv"), unicodedata.normalize("NFD", "Chrome 비밀번호 2.csv"),
         ".env ", ".env.", ".env.production ", "id_rsa.", "id_ed25519 ", ".envrc", ".npmrc", ".pypirc", ".netrc", "_netrc",
         ".pgpass", "credentials", ".git-credentials", "kubeconfig", "kubeconfig.yaml", "Logins.CSV"]


@pytest.mark.parametrize("name", NAMES)
def test_name_variants_are_secret(tmp_path, name):
    if os.name == "nt" and name != name.rstrip(" ."):
        pytest.skip("Windows drops trailing spaces and dots from names")
    p = tmp_path / name
    p.write_text(CRED.replace(",", ";") if name.startswith(".") else "x=1\n", encoding="utf-8")
    assert index.is_secret(p) and index.tier(p) == 0, name


def test_name_variants_without_the_file():
    for name in NAMES:
        assert index.secret_name(Path("/home/me/docs") / name), name


@pytest.mark.parametrize("path", ["/home/me/.docker/config.json", "/home/me/.kube/config", "/home/me/.aws/credentials",
                                  "/home/me/.ssh/config", "C:\\Users\\me\\.ssh\\known_hosts", "/home/me/.SSH /config"])
def test_files_in_key_folders(path):
    assert index.secret_name(Path(path)), path


@pytest.mark.parametrize("name", ["report 2.pdf", "Copy of 회의록.hwp", "견적서 (1).csv", "notes.txt", "login_guide.txt",
                                  "environment.md", "id_card_scan.png", "credential_policy.docx", "tokenizer.json"])
def test_ordinary_names(tmp_path, name):
    p = tmp_path / name
    p.write_text("ordinary text", encoding="utf-8")
    assert not index.is_secret(p) and index.tier(p) != 0, name


@pytest.mark.parametrize("name,head", [
    ("export.csv", "url,username,password\n"),
    ("내보내기.csv", "사이트,아이디,비번\n"),
    ("내보내기.tsv", "주소\t사용자\t암호\n"),
    ("accounts.txt", "origin,login,pwd\n"),
    ("data.csv", '"Title","User Name","Password (encrypted)"\n'),
    ("목록.csv", "사이트,사용자 이름,패스워드\n"),
])
def test_any_name_with_a_credential_header(tmp_path, name, head):
    p = tmp_path / name
    p.write_text(head + "a,b,c\n", encoding="utf-8")
    assert index.is_secret(p), name
    assert index.is_secret(Path(name), head=head.encode("utf-8"))


@pytest.mark.parametrize("head", ["name,date,amount\n", "My password and username notes for the bank\n", "policy,owner,status\n",
                                  "항목,담당,금액\n", "id,title,date\n"])
def test_ordinary_headers(tmp_path, head):
    p = tmp_path / "data.csv"
    p.write_text(head, encoding="utf-8")
    assert not index.is_secret(p), head


def _index(roots, db, home):
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_ORIGIN": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home)}
    r = subprocess.run([sys.executable, str(Path(index.__file__)), "--root", *map(str, roots), "--db", str(db), "--no-embed"],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0, r.stderr[-800:]
    return sqlite3.connect(db)


def test_a_password_export_under_any_name_is_not_stored(tmp_path):
    """the scan of names does not open every text file; the content stage finds the header and keeps the name only,
    and the next run keeps it so without reading it again"""
    root = tmp_path / "home"; root.mkdir()
    (root / "export_2025.csv").write_text(CRED, encoding="utf-8")
    (root / "메모.txt").write_text("사이트,아이디,비번\nbank.example,jdoe_fake,PwFakeKorean9\n", encoding="utf-8")
    (root / "견적서.csv").write_text("품목,수량,단가\n센서,2,520000\n", encoding="utf-8")
    for _ in range(2):
        con = _index([root], tmp_path / "i.db", tmp_path / "data")
        tiers = dict(con.execute("SELECT name, tier FROM files"))
        assert tiers == {"export_2025.csv": 0, "메모.txt": 0, "견적서.csv": 1}, tiers
        text = " ".join(t or "" for (t,) in con.execute("SELECT text FROM chunks"))
        assert "PwFake" not in text and "520000" in text
        con.close()


def _mail(atts, inner=None):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = "alpha attachments", "a@example.com", "b@example.com"
    m.set_content("see attached")
    for name, data in atts:
        m.add_attachment(data.encode("utf-8"), maintype="text", subtype="csv", filename=name)
    if inner is not None:
        m.add_attachment(inner)
    return m


@pytest.mark.parametrize("name", ["logins 2.csv", "Copy of logins.csv", "passwords.csv.txt", "vault_export.csv",
                                  unicodedata.normalize("NFD", "비밀번호 2.csv"), "export.csv"])
def test_mail_attachments_follow_the_same_rules(tmp_path, name):
    p = tmp_path / "a.eml"
    p.write_bytes(_mail([(name, CRED)]).as_bytes())
    t = moretext.eml_text(p)
    assert "alpha attachments" in t and "PwFakeVariant1" not in t, t


def test_a_mail_inside_a_mail_is_its_name_only(tmp_path):
    inner = _mail([("notes.txt", "inner secret plan zebracorn\n")])
    inner.replace_header("Subject", "inner fwd")
    p = tmp_path / "a.eml"
    p.write_bytes(_mail([("report.csv", "품목,수량\n센서,2\n")], inner=inner).as_bytes())
    t = moretext.eml_text(p)
    assert "센서" in t and "zebracorn" not in t and "inner fwd" not in t, t
