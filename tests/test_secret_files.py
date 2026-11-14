"""#200: certificate, key and password files are never read (tier 0), files already indexed become tier 0 on the next run,
and a chosen folder inside findr's data folder no longer lets the rest of the data folder in. Invented names only"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import index

SECRET_NAMES = ["signCert.der", "SIGNCERT.DER", "kmCert.der", "signPri.key", "kmPri.key", "kmpri.KEY",
                "회사.pfx", "backup.P12", "server.ppk", "vault.kdb",
                "passwords.csv", "Passwords.csv", "passwords_2024.csv", "Chrome Passwords.csv", "Microsoft Edge Passwords.csv",
                "Chrome 비밀번호.csv", "logins.csv", "Logins (1).csv", "logins.json", "key4.db", "Login Data",
                "bitwarden_export_20250101123456.json", "bitwarden_encrypted_export_20250101.json", "lastpass_export.csv",
                "lastpass_vault_export.csv", "1PasswordExport-ABCDEF-20250101-120000.csv", "1PasswordExport-ABCDEF.1pux",
                "dashlane-credential-export.csv", "nordpass_export.csv", "KeePass backup.xml", "Proton Pass_export_2025.zip",
                "passwords.csv.bak", "passwords.csv~"]
PLAIN_NAMES = ["견적서.csv", "매출_2025.csv", "login_guide.txt", "사용법.txt", "keynote_backup.txt", "password_policy.docx",
               "bitwarden 설치 안내.txt", "회의록.hwp", "data.der", "zookeeper_backup.json"]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_secret_names_are_tier_0(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"name,url,username,password\nexample,https://example.invalid,me,not-real\n")
    assert index.tier(p) == 0, name


@pytest.mark.parametrize("name", PLAIN_NAMES)
def test_ordinary_names_are_still_read(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"ordinary text")
    assert index.tier(p) != 0, name


@pytest.mark.parametrize("path", [
    "/home/me/NPKI/yessign/USER/cn=demo/notes.txt",
    "/Users/me/Library/Preferences/NPKI/readme.txt",
    "/home/me/backup/gpki/photo.png",
    "/home/me/Epki/x.pdf",
    "C:\\Users\\me\\AppData\\LocalLow\\NPKI\\KICA\\USER\\list.txt",
    "C:/Program Files/GPKI/Certificate/class2/info.txt",
    "D:\\자료\\공동인증서 백업\\설명.txt",
    "/home/me/문서/공인인증서/안내.hwp",
    "/home/me/공동인증서_2025.zip",
])
def test_certificate_folders_and_named_paths(path):
    assert index.secret_path(path), path
    assert index.is_secret(Path(path)), path   # no file needed: the path alone decides


def test_certificate_names_in_decomposed_korean():
    """macOS may hand the name decomposed (NFD); it is the same name"""
    import unicodedata
    assert index.secret_path(unicodedata.normalize("NFD", "/Users/me/공동인증서/a.txt"))


@pytest.mark.parametrize("path", ["/home/me/npki_report.txt", "/home/me/문서/인증서 발급 안내.txt", "/home/me/NPKI",
                                  "C:\\Users\\me\\Documents\\GPKI안내.hwp"])
def test_not_a_certificate_folder(path):
    assert not index.secret_path(path), path   # the folder name alone, as a whole folder; a file named NPKI is a file


def _index(roots, db, home):
    env = {**os.environ, "FINDR_OCR": "0", "FINDR_ORIGIN": "0", "FINDR_HOME": str(home), "FINDR_DATA": str(home)}
    r = subprocess.run([sys.executable, str(Path(index.__file__)), "--root", *map(str, roots), "--db", str(db), "--no-embed"],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0, r.stderr[-800:]
    return sqlite3.connect(db)


def test_secret_files_are_kept_unread_and_already_read_ones_lose_their_text(tmp_path):
    root = tmp_path / "home"
    (root / "문서").mkdir(parents=True)
    (root / "문서" / "견적서.txt").write_text("스마트센서 견적", encoding="utf-8")
    (root / "문서" / "passwords.csv").write_text("name,password\nsample,not-a-real-one\n", encoding="utf-8")
    npki = root / "AppData" / "LocalLow" / "NPKI" / "DEMO" / "USER" / "cn=demo"
    npki.mkdir(parents=True)
    (npki / "signCert.der").write_bytes(b"\x30\x82fake")
    (npki / "memo.txt").write_text("not a real certificate", encoding="utf-8")
    con = _index([root], tmp_path / "i.db", tmp_path / "data")
    tiers = dict(con.execute("SELECT name, tier FROM files"))
    assert tiers["견적서.txt"] == 1
    assert tiers["passwords.csv"] == 0 and tiers["signCert.der"] == 0 and tiers["memo.txt"] == 0, tiers
    text = " ".join(t or "" for (t,) in con.execute("SELECT text FROM chunks"))
    assert "not-a-real-one" not in text and "not a real certificate" not in text
    # an index made before this rule: the file was read (tier 1) and its text stored
    fid = con.execute("SELECT id FROM files WHERE name='passwords.csv'").fetchone()[0]
    con.execute("UPDATE files SET tier=1 WHERE id=?", (fid,))
    con.execute("UPDATE chunks SET text='name,password sample,not-a-real-one' WHERE file=?", (fid,))
    con.commit(); con.close()
    con = _index([root], tmp_path / "i.db", tmp_path / "data")
    assert con.execute("SELECT tier FROM files WHERE name='passwords.csv'").fetchone()[0] == 0
    assert not con.execute("SELECT 1 FROM chunks WHERE text LIKE '%not-a-real-one%'").fetchone()


def _data_folder(home):
    data = home / "findr-data"
    (data / "logs").mkdir(parents=True); (data / "logs" / "engine.log").write_text("engine log line", encoding="utf-8")
    (data / "settings.json").write_text('{"lang": "ko"}', encoding="utf-8")
    (data / "shared" / "notes").mkdir(parents=True)
    (data / "shared" / "notes" / "메모.txt").write_text("공유 메모", encoding="utf-8")
    (data / "embedded-app" / "cache").mkdir(parents=True)
    (data / "embedded-app" / "cache" / "state.txt").write_text("app state", encoding="utf-8")
    return data


def test_a_chosen_folder_inside_the_data_folder_keeps_the_rest_out(tmp_path):
    home = tmp_path / "home"; (home / "문서").mkdir(parents=True)
    (home / "문서" / "회의록.txt").write_text("주간 회의록", encoding="utf-8")
    data = _data_folder(home)
    con = _index([home, data / "shared"], tmp_path / "i.db", data)
    names = {n for (n,) in con.execute("SELECT name FROM files")}
    assert names == {"회의록.txt", "메모.txt"}, names


def test_only_the_chosen_folder_inside_the_data_folder(tmp_path):
    data = _data_folder(tmp_path / "home")
    con = _index([data / "shared"], tmp_path / "i.db", data)
    assert {n for (n,) in con.execute("SELECT name FROM files")} == {"메모.txt"}


def test_data_folder_files_already_indexed_leave(tmp_path):
    """an index made while the chosen folder inside the data folder let all of it in"""
    home = tmp_path / "home"; (home / "문서").mkdir(parents=True)
    (home / "문서" / "회의록.txt").write_text("주간 회의록", encoding="utf-8")
    data = _data_folder(home)
    con = _index([home], tmp_path / "i.db", tmp_path / "elsewhere")   # the data folder was not findr's then: all indexed
    assert {"engine.log", "settings.json", "state.txt", "메모.txt"} <= {n for (n,) in con.execute("SELECT name FROM files")}
    con.close()
    con = _index([home, data / "shared"], tmp_path / "i.db", data)
    assert {n for (n,) in con.execute("SELECT name FROM files")} == {"회의록.txt", "메모.txt"}


def test_the_data_folder_chosen_itself_stays(tmp_path):
    """as before: choosing the data folder itself (a developer) keeps all of it"""
    data = _data_folder(tmp_path / "home")
    con = _index([data], tmp_path / "i.db", data)
    assert {"engine.log", "settings.json", "메모.txt"} <= {n for (n,) in con.execute("SELECT name FROM files")}
