"""findr index (2026-11-03): natural-language search over every file under a root, all local.

Tiers (by what a person looks for):
  1 full text  : the user's own documents and code (pdf, md, txt, html, py, json, logs ...) and application code
  2 head only  : bundled libraries (site-packages, python_embeded ...): first ~1500 chars, one chunk
  3 name only  : media, models, binaries, archives: path, name, size, date
Secrets (.env, keys, credentials) are listed by name only when allowed, never read.
    python index.py --root FOLDER --db INDEX.db
Re-running only re-reads files whose size or mtime changed.
"""
import argparse, datetime, unicodedata
import json
import os
import re
import sqlite3
import stat as _stat
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

if os.environ.get("FINDR_VERBOSE") != "1":   # the indexer is its own process: quiet as the CLI is ('Redirects are currently not supported', #67)
    import logging
    logging.getLogger("torch.distributed.elastic").setLevel(logging.ERROR)

TEXT = {".md", ".txt", ".py", ".pyi", ".js", ".ts", ".jsx", ".tsx", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".csv", ".tsv", ".log", ".html", ".htm", ".css", ".sh", ".ps1", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp",
        ".cu", ".cuh", ".java", ".go", ".rs", ".sql", ".xml", ".rst", ".ipynb", ".jinja", ".tex", ".r", ".m", ".lua", ""}
DOC = {".pdf", ".docx", ".xlsx", ".pptx", ".hwpx", ".hwp", ".doc", ".ppt", ".xls", ".key",
       ".xlsb", ".ods", ".rtf", ".eml", ".pages", ".numbers"}   # moretext.py (sheets, RTF, e-mail), keynote_text (iWork)
AUD = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".wma", ".ogg"}   # transcribed locally by stt_cache.py (faster-whisper)
OLD = {".hwp", ".doc", ".ppt", ".xls"}   # old binary formats: converted locally by LibreOffice (+ H2Orestart for hwp), cached
SECRET = re.compile(r"^(\.env(\..*)?|\.netrc|\.pgpass|\.git-credentials|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|credentials(\.json|\.ya?ml|\.txt)?"
                    r"|secrets?\.(json|ya?ml|toml|txt|env)|.*\.(pem|pfx|p12|kdbx|keystore|jks)"
                    r"|\.(bash|zsh|sh|python|psql|mysql|node_repl|lesshst)_?history|\.history|ConsoleHost_history\.txt)$", re.I)   # shell histories hold typed tokens
# '.key' is both a cryptographic key and an Apple Keynote deck: Keynote is a ZIP, a key file is not (see is_secret)
# values that look like keys are masked inside indexed text (a key pasted into a note or log never reaches the index)
KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}"
                     r"|xox[abpr]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
                     r"|(?i:(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?)[^\s'\"]{12,})")
# a fixed piece of text every KEYLIKE match contains: SQLite LIKE (ASCII case-insensitive, '_' any one character) keeps only the
# chunks that may hold one, so the regex runs on a few rows instead of all of them (a run with no change scanned 920k chunks
# in Python for 58 s, #67). Keep in step with KEYLIKE: tests/test_mask_prefilter.py checks every alternative is covered
KEYLIKE_HINTS = ("sk-", "akia", "gh_", "hf_", "xox", "aiza", "private key", "api", "secret", "password", "passwd", "token")
LIB = re.compile(r"/(site-packages|python_embeded|node_modules|__pycache__|\.git|dist-packages|\.venv|venv)(/|$)")
# folders of keys and sign-ins, never read, whatever folder was chosen (`findr index ~` read .kube/config, .docker/config.json
# and shell histories, whose tokens the key masking does not all catch, and served them to AI tools)
SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", ".1password", "Keychains"}
SKIP_DIR = {".git", "__pycache__", "node_modules"} | SECRET_DIRS
# 2026-11-05 (user's decision): not the user's files -> not indexed by default (--with-libs keeps the old behaviour)
LIB_DIRS = {"site-packages", "python_embeded", "node_modules", "dist-packages", ".venv", "venv", "__pycache__", ".git",
            "dist", ".cache", ".next", ".pytest_cache", ".mypy_cache", ".tox"}
APP_MARK = {"python_embeded", "python_embedded"}
JUNK = re.compile(r"^(vocab\.json|merges\.txt|tokenizer(_config)?\.json|special_tokens_map\.json|vocab\.txt)$|(\.map|\.pyc|\.pyo|\.min\.js|\.min\.css)$|-[A-Za-z0-9_]{8}\.(js|css)$|^~\$|^(Thumbs\.db|\.DS_Store|desktop\.ini)$", re.I)
# backups keep the user's history ("bos 넣기 전 학습 스크립트"): read them as the kind of file they back up
BAK = re.compile(r"(\.(bak\w*|old|orig|backup|swp|tmp)|~)$", re.I)
MAXREAD = 2_000_000   # bytes of text read per tier-1 file
GIST = int(os.environ.get("FINDR_GIST") or 12_000)
RICH = {".pdf", ".docx", ".xlsx", ".pptx", ".hwpx", ".key", ".doc", ".hwp", ".ppt", ".xls"}   # read through a parser: output length tells


def _size(s):
    try:
        return os.path.getsize(s)
    except OSError:
        return 0   # first pass: the opening of each document (its first pieces), the rest later
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")   # Apple GPU: operations it lacks run on the CPU instead of failing
FILE_SECONDS = float(os.environ.get("FINDR_FILE_SECONDS") or 60)   # time budget per file; what was read by then is kept
DATA = {".json", ".jsonl", ".csv", ".tsv", ".log"}   # machine-written results: the head says what they are
DATA_READ = 30_000
CHUNK, OVER = 900, 150


def real_ext(p: Path):
    """the extension that says how to read the file: train.py.bak_pack2 -> .py, 견적서.xlsx.old -> .xlsx"""
    name = p.name
    while BAK.search(name):
        name = BAK.sub("", name)
    return Path(name).suffix.lower() if name != p.name else p.suffix.lower()


def is_secret(p: Path):
    """key and credential files are never read; a '.key' file is a secret unless it is a Keynote deck (a ZIP)"""
    if SECRET.search(p.name) or SECRET.search(BAK.sub("", p.name)):
        return True
    if real_ext(p) == ".key":
        try:
            with open(p, "rb") as f:
                return f.read(4) != b"PK\x03\x04"
        except OSError:
            return True
    return False


def tier(p: Path):
    s = p.as_posix(); ext = real_ext(p)
    if is_secret(p):
        return 0
    if ext in AUD:
        return 1 if transcript(p) is not None else 3
    if ext not in TEXT and ext not in DOC:
        return 3
    if LIB.search(s):
        return 2
    return 1


try:   # old office formats: content sniffing + Word 97 piece table before LibreOffice (orders of magnitude faster)
    from .officetext import sniff_text, doc_text, hwp_text
    from .moretext import sheet_text, rtf_text, eml_text
except ImportError:
    from officetext import sniff_text, doc_text, hwp_text
    from moretext import sheet_text, rtf_text, eml_text


def _long(s: str) -> str:
    """Windows: the \\\\?\\ form of an absolute path, which reaches past 260 characters without the LongPathsEnabled setting
    (off by default: files deeper than that were left out silently, #60). Elsewhere, and for relative paths: unchanged"""
    if os.name != "nt" or s.startswith("\\\\?\\") or not os.path.isabs(s):
        return s
    s = os.path.normpath(s)
    return "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s


def _short(s: str) -> str:
    """back from the \\\\?\\ form: paths are stored, shown and opened in their usual form"""
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    return s[4:] if s.startswith("\\\\?\\") else s


def extract(p: Path, limit):
    ext = real_ext(p)
    if os.name == "nt" and len(str(p)) >= 248:   # past MAX_PATH: readers open it in the \\?\ form
        p = Path(_long(str(p)))
    try:
        if ext in (".xls", ".xlsb", ".ods"):   # sheets: python-calamine first, LibreOffice only when that fails
            t = sheet_text(p, limit)
            if t and t.strip():
                return t
            if ext != ".xls":
                return ""
        if ext == ".rtf":
            return rtf_text(p, limit) or ""
        if ext == ".eml":
            return eml_text(p, limit) or ""
        if ext in (".pages", ".numbers"):   # iWork shares Keynote's container: snappy-framed protobuf in Index/*.iwa
            return keynote_text(p, limit)
        if ext in OLD:
            t = sniff_text(p, limit)   # web-exported HTML/MHT/text saved under an office extension
            if t is None and ext == ".doc":
                t = doc_text(p, limit)   # real Word 97-2003: piece table, no LibreOffice
            if t is None and ext == ".hwp":
                t = hwp_text(p, limit)   # HWP 5: compressed body records, no LibreOffice
            return t if t and t.strip() else convert_old(p)[:limit]
        if ext in AUD:
            return (transcript(p) or "")[:limit]
        if ext == ".pdf":
            return pdf_text(p, limit)
        if ext == ".key":
            return keynote_text(p, limit)
        if ext in (".docx", ".xlsx", ".pptx", ".hwpx"):
            return ooxml_text(p, limit)
        raw = open(p, "rb").read(limit)
        if b"\x00" in raw[:4096] and raw[:2] not in (b"\xff\xfe", b"\xfe\xff"):   # binary, unless UTF-16 text with its BOM
            return ""
        t = _decode_text(raw)   # Korean Notepad files are often 'ANSI' (cp949) or '유니코드' (UTF-16)
        if ext in (".html", ".htm"):
            t = re.sub(r"(?s)<(script|style).*?</\1>", " ", t); t = re.sub(r"<[^>]+>", " ", t)
        if ext == ".ipynb":
            try:
                t = "\n".join("".join(c.get("source", "")) for c in json.loads(t).get("cells", []))
            except Exception:
                pass
        return t
    except (PermissionError, BlockingIOError):
        raise   # open in another program right now: _read_one keeps it pending for the next run
    except OSError as e:
        if getattr(e, "winerror", None) in (32, 33):   # Windows sharing / lock violation (a document open in Office)
            raise
        print("  extract fail", p, type(e).__name__, file=sys.stderr)
        return ""
    except Exception as e:
        print("  extract fail", p, type(e).__name__, file=sys.stderr)
        return ""


def transcript(p: Path):
    """the cached local transcript of an audio file, or None when it has not been transcribed yet"""
    import hashlib
    try:
        st = p.stat()
    except OSError:
        return None
    c = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "conv" / (hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest() + ".txt")
    return c.read_text(encoding="utf-8", errors="ignore") if c.exists() else None


# What the readers can read changes between versions. An unchanged file (same size, mtime and tier) is never read again,
# so files these readers newly handle are queued once when the reader version goes up.
#   2: .xls/.xlsb/.ods without LibreOffice (#29), distribution(배포용) .hwp (#28)
READERS = 2
REREAD_EXT = {2: (".xls", ".xlsb", ".ods", ".hwp")}


def reread_for_new_readers(con):
    """files whose stored text is empty and whose kind a newer reader handles go back to 'pending' (read in the content wave)"""
    have = int((con.execute("SELECT value FROM meta WHERE key='readers'").fetchone() or ["1"])[0])
    if have >= READERS:
        return 0
    exts = sorted({e for v, es in REREAD_EXT.items() if v > have for e in es})
    n = con.execute(f"""UPDATE files SET pending=1 WHERE tier=1 AND ext IN ({','.join('?' * len(exts))})
                        AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.file=files.id AND length(c.text) > 0)""", exts).rowcount
    con.execute("INSERT OR REPLACE INTO meta VALUES('readers', ?)", (str(READERS),)); con.commit()
    if n:
        print(f"FINDR_REREAD {n} files a newer reader can read ({', '.join(exts)})", flush=True)
    return n


def _hangul_like(t: str) -> bool:
    """bytes read as cp949 really were Korean: most non-ASCII characters are Hangul syllables (Latin-1 bytes read as cp949
    come out as Hanja and symbols)"""
    na = [c for c in t if ord(c) > 127]
    return bool(na) and sum("가" <= c <= "힣" for c in na) >= 0.6 * len(na)


def _decode_text(b: bytes) -> str:
    """text of unknown encoding, as Notepad and converters write it: UTF-16 with a BOM ('유니코드'), UTF-8 (with or without
    BOM), else this PC's code page ('ANSI'), else Korean cp949 when it reads as Korean, else UTF-8 dropping bad bytes.
    b may be cut at a read limit: a character cut in half at the end does not count as a wrong guess"""
    import codecs
    import locale
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", "ignore")
    if b[:3] == codecs.BOM_UTF8:
        b = b[3:]
    sys_enc = (locale.getpreferredencoding(False) or "utf-8").lower()
    cands = [("utf-8", 3, None)]
    if codecs.lookup(sys_enc).name != "utf-8":
        cands.append((sys_enc, 1, _hangul_like if codecs.lookup(sys_enc).name in ("cp949", "euc_kr") else None))
    if codecs.lookup("cp949").name not in [codecs.lookup(e).name for e, _, _ in cands]:
        cands.append(("cp949", 1, _hangul_like))
    for enc, cut, ok in cands:
        try:
            t = b.decode(enc)
        except UnicodeDecodeError as e:
            if e.start < len(b) - cut:
                continue
            t = b[:e.start].decode(enc, "ignore")
        if ok is None or ok(t):
            return t
    return b.decode("utf-8", "ignore")


def convert_old(p: Path):
    """hwp/doc/ppt/xls -> text with a local LibreOffice; cached by path, size and mtime (re-runs cost nothing)"""
    import hashlib
    import shutil
    import tempfile
    st = p.stat()
    key = hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()
    cache = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "conv" / (key + ".txt")
    done = cache.with_suffix(".ok")   # LibreOffice ran and the file really holds no text
    fail = cache.with_suffix(".fail")   # LibreOffice ran and could not convert it: not tried again with the same LibreOffice
    so = shutil.which("soffice")
    try:
        so_id = f"{so}|{Path(so).resolve().stat().st_mtime}" if so else ""
    except OSError:
        so_id = so or ""
    if cache.exists() and (cache.stat().st_size or done.exists() or not so
                           or (fail.exists() and fail.read_text(errors="ignore") == so_id)):   # a failed (empty) result is retried
        return cache.read_text(encoding="utf-8", errors="ignore")
    if not so:
        return ""   # nothing to convert with: no cache entry, so installing LibreOffice later reads the file
    cache.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower(); txt = ""; converted = False
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / ("in" + ext); shutil.copyfile(p, src)
        # plain 'txt:Text' is written in the system code page (cp949 on Korean Windows): read as UTF-8, every Korean
        # character was dropped. Ask for UTF-8, and still read an older LibreOffice's output in the local code page
        target = "pdf" if ext in (".ppt", ".xls") else "txt:Text (encoded):UTF8"
        try:
            subprocess.run(["soffice", "--headless", "--convert-to", target, "--outdir", td, str(src)], capture_output=True, timeout=120)
            converted = (Path(td) / ("in.pdf" if target == "pdf" else "in.txt")).exists()
            if target == "pdf" and converted:
                txt = subprocess.run(["pdftotext", "-enc", "UTF-8", "-layout", str(Path(td) / "in.pdf"), "-"],
                                     capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")
            elif converted:
                txt = _decode_text((Path(td) / "in.txt").read_bytes())
        except Exception as e:
            print("  convert fail", p.name, type(e).__name__, file=sys.stderr)
    cache.write_text(txt, encoding="utf-8")
    if converted and not txt.strip():
        done.touch()   # really empty: not converted again on every run
    elif not converted:
        fail.write_text(so_id, encoding="utf-8")   # up to 120 s each time otherwise; a new LibreOffice gets another try
    return txt


def chunks(text):
    text = text.encode("utf-8", "replace").decode("utf-8")   # a lone surrogate from any reader would stop the whole index
    text = KEYLIKE.sub("[가림]", text)
    text = re.sub(r"[ \t]{3,}", "  ", text).strip()
    if not text:
        return []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK]); i += CHUNK - OVER
    return out


def nfc(s):
    """one form for Korean (macOS writes names decomposed, people type them composed): what search compares is NFC"""
    return unicodedata.normalize("NFC", s) if s else s


def label(p: Path):   # what the encoder sees besides the text: names carry meaning ("LoRA_실험_요약.pdf")
    return nfc(f"{p.name} | {'/'.join(p.parts[-4:-1])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, nargs="+", help="one or more folders, in the order people look in them")
    ap.add_argument("--db", required=True)
    ap.add_argument("--model", default="embeddinggemma-2"); ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--with-libs", action="store_true", help="also index bundled libraries and build output (head only)")
    # 2026-11-05: newest and most useful first. The scan registers every file by name at once; content is read in waves.
    ap.add_argument("--max-age-days", type=float, default=0, help="this wave reads content only of files changed within N days (0 = all)")
    ap.add_argument("--limit", type=int, default=0, help="read content of at most N pending files in this run (0 = no limit)")
    ap.add_argument("--upgrade", default="", help="move the meaning vectors to this encoder in idle time (no scan), then switch")
    ap.add_argument("--minutes", type=float, default=50, help="--upgrade: stop after N minutes (the next idle run continues)")
    ap.add_argument("--upgrade-first", action="store_true", help="--upgrade: only each file's first piece, and no switch (exit 6 when all are there)")
    ap.add_argument("--names-only", action="store_true", help="register new and changed files by name only (on battery); content waits for the next full run")
    a = ap.parse_args()
    roots = [Path(r) for r in a.root]; Path(a.db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    if not one_indexer(a.db):
        print("FINDR_BUSY 다른 색인 작업이 이 색인을 쓰고 있습니다. 끝난 뒤에 다시 실행해 주세요.", flush=True)
        sys.exit(3)
    miss = missing_parts()
    if miss:   # once per run, so a quiet gap has a reason and a fix
        print("FINDR_WARN missing: " + ", ".join(miss) + " -> findr doctor (설치 명령을 알려 줍니다)", flush=True)
    con = sqlite3.connect(Path(a.db).expanduser(), timeout=60)
    con.execute("PRAGMA journal_mode=WAL")   # the search server keeps reading while the index is updated
    con.executescript("""
      CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
      CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
      CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');
      CREATE INDEX IF NOT EXISTS ch_file ON chunks(file);
      CREATE INDEX IF NOT EXISTS ch_n ON chunks(n, file);
      CREATE INDEX IF NOT EXISTS ch_noemb ON chunks(n, file) WHERE emb IS NULL;   -- what embed() looks for: no full pass over vectors
      CREATE TABLE IF NOT EXISTS cards(file INTEGER PRIMARY KEY, outline TEXT, summary TEXT, done INT DEFAULT 0);""")
    if "pending" not in [r[1] for r in con.execute("PRAGMA table_info(files)")]:
        con.execute("ALTER TABLE files ADD COLUMN pending INT DEFAULT 0")
    if "emb2" not in [r[1] for r in con.execute("PRAGMA table_info(chunks)")]:   # the larger model's vectors, filled in idle time
        con.execute("ALTER TABLE chunks ADD COLUMN emb2 BLOB")
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    if not con.execute("SELECT 1 FROM meta WHERE key='nfc'").fetchone():   # an index made before NFC: moved once (#12)
        start = int((con.execute("SELECT value FROM meta WHERE key='nfc_upto'").fetchone() or ["0"])[0])   # an interrupted move goes on from here
        rows = con.execute("SELECT id, name, path FROM files WHERE id > ? ORDER BY id", (start,)).fetchall()
        moved, t0 = 0, time.time()
        for i, (fid, name, path) in enumerate(rows, 1):
            if nfc(name) != name or nfc(path) != path:
                con.execute("UPDATE files SET name=? WHERE id=?", (nfc(name), fid))
                for cid, text in con.execute("SELECT id, text FROM chunks WHERE file=?", (fid,)).fetchall():
                    con.execute("DELETE FROM fts WHERE rowid=?", (cid,))
                    con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(name), nfc(path), nfc(text or "")))
                con.execute("UPDATE chunks SET emb=NULL WHERE file=? AND n < 8", (fid,))   # the name is part of the meaning vector
                moved += 1
            if i % 2000 == 0 or i == len(rows):   # minutes on a large Mac index: show it moving, and keep what is done if it is stopped
                con.execute("INSERT OR REPLACE INTO meta VALUES('nfc_upto', ?)", (str(fid),)); con.commit()
                progress("nfc", i, len(rows))
                print(f"FINDR_NFC_PROGRESS {i}/{len(rows)} {time.time() - t0:.0f}s", flush=True)
        con.execute("INSERT OR REPLACE INTO meta VALUES('nfc', '1')"); con.execute("DELETE FROM meta WHERE key='nfc_upto'"); con.commit()
        if moved:
            print(f"FINDR_NFC {moved} files with decomposed (NFD) names moved to NFC", flush=True)
    reread_for_new_readers(con)
    if a.model in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") and not con.execute("SELECT 1 FROM chunks WHERE emb IS NOT NULL LIMIT 1").fetchone():
        con.execute("INSERT OR REPLACE INTO meta VALUES('encoder', ?)", (a.model,))   # known from the start: search works while indexing (#9)
    key = (lambda x: nfc(x).casefold()) if os.name == "nt" else nfc   # Windows folder names ignore case
    excl = {key(Path(x).as_posix()).rstrip("/") for x in os.environ.get("FINDR_EXCLUDE", "").split(os.pathsep) if x}
    try:   # findr's own checkout and data folder inside a chosen folder: not the person's files (#66); a chosen root itself stays
        from scope import own_dirs
    except ImportError:
        from .scope import own_dirs
    try:
        from scope import is_findr_checkout
    except ImportError:
        from .scope import is_findr_checkout
    # other copies of findr already in the index (a second clone, an older install, a worktree: #119), found by their
    # pyproject.toml rows; like the running checkout, they leave unless a chosen root is one of them or lies inside one
    copies = {Path(raw).parent for (raw,) in con.execute("SELECT path FROM files WHERE name='pyproject.toml'") if is_findr_checkout(Path(raw).parent)}
    in_root = lambda k: any(key(Path(r).resolve().as_posix()).rstrip("/") in (k,) or key(Path(r).resolve().as_posix()).startswith(k + "/") for r in roots)
    for d in [*own_dirs(), *copies]:
        k = key(d.as_posix()).rstrip("/")
        if not in_root(k):
            excl.add(k)
    if excl:   # folders this PC chose not to search (exclude.txt): files already indexed under them leave the index
        ids = [i for i, raw in con.execute("SELECT id, path FROM files") if (s := key(raw)) in excl or any(s.startswith(x + "/") for x in excl)]
        for fid in ids:
            drop(con, fid)
        if ids:
            print(f"FINDR_EXCLUDE removed {len(ids)} files", flush=True)
    if os.environ.get("FINDR_SCOPE") == "user" and not a.upgrade:
        pres = [key(r.as_posix()).rstrip("/") + "/" for r in roots]
        rows = con.execute("SELECT id, path FROM files").fetchall()
        # a file whose folder is not there right now (a drive or share offline) is kept: only folders no longer chosen leave
        out = [i for i, raw in rows if not any(key(raw).startswith(x) for x in pres) and os.path.isdir(_long(str(Path(raw).parent)))]
        if out and len(out) <= len(rows) // 2:   # more than half would be a missing drive or a bad root list: kept
            for fid in out:
                drop(con, fid)
            print(f"FINDR_OUTSIDE removed {len(out)} files under folders no longer searched", flush=True)
        elif out:
            print(f"FINDR_OUTSIDE kept {len(out)} files outside the current folders (too many to be a scope change)", flush=True)
    con.commit()
    if a.upgrade:
        return upgrade(con, a.upgrade, a.minutes, first_only=a.upgrade_first)
    progress("names", 0, len(roots))
    scanned = 0   # files looked at in the folders before this one
    for k, root in enumerate(roots):   # stage 1: names and folders of every chosen folder first (seconds)
        print("FINDR_ROOT " + json.dumps({"i": k, "n": len(roots), "path": str(root)}, ensure_ascii=False), flush=True)
        if not os.path.isdir(_long(str(root))):   # unplugged disk, network drive offline: its files stay as they were
            print(f"FINDR_ROOT_MISSING {root} (kept as indexed)", flush=True)
            progress("names", k + 1, len(roots)); continue
        seen, t0, new = set(), time.time(), 0
        pre = root.as_posix().rstrip("/") + "/"   # several roots share one db: only this root's files can be "gone"
        known = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute("SELECT path,size,mtime,id,tier FROM files WHERE path LIKE ? ESCAPE '\\'", (like_pre(pre),))}
        unread = []   # folders the walk could not open: counted, never silent
        for dpl, dns, fns in os.walk(_long(str(root)), onerror=unread.append):
            dp = _short(dpl)
            if not a.with_libs and APP_MARK & set(dns):
                dns[:] = []; continue   # an installed application bundle (ships its own Python): not the user's files
            dns[:] = [d for d in dns if d not in (SKIP_DIR if a.with_libs else SKIP_DIR | LIB_DIRS)]
            if os.name == "nt":   # os.walk skips symlinks but follows junctions ('Documents/My Music' -> Music, a second scan)
                from scope import is_link
                dns[:] = [d for d in dns if not is_link(os.path.join(dpl, d))]
            if os.environ.get("FINDR_SCOPE") == "user":   # whole-PC mode: no system, program, cache or library folders
                from scope import skip_dir, app_profile
                if app_profile(fns):
                    dns[:] = []; continue   # an app's embedded-browser profile (CapCut's CEF folder in ~/Movies)
                # the parent's path from the root: system names (windows, users, drivers, samsung ...) are skipped only at the
                # top. rel was "" below the top, which skip_dir took as the top too: a person's own 'users' or 'drivers'
                # folder anywhere in a project left the index (Mac Air, 11/9: 300 files of a web app's users/ code)
                try:
                    rel = "/" if Path(dp) == root else (Path(dp).relative_to(root).as_posix() or "?")
                except ValueError:
                    rel = "?"   # not under the root as written (a different spelling): below the top all the same
                dns[:] = [d for d in dns if not skip_dir(rel, d)]
            if excl:
                dns[:] = [d for d in dns if key((Path(dp) / d).as_posix()) not in excl]
            # a copy of findr met during the walk (#119): only folders holding a pyproject.toml are looked at, which is rare
            dns[:] = [d for d in dns if not (os.path.isfile(os.path.join(dpl, d, "pyproject.toml")) and is_findr_checkout(Path(dp) / d))]
            for fn in fns:
                if not a.with_libs and JUNK.search(fn):
                    continue
                p = Path(dp) / fn
                try:
                    st = os.stat(os.path.join(dpl, fn))
                except OSError:
                    continue
                if not _stat.S_ISREG(st.st_mode):
                    continue   # sockets, pipes, devices: nothing to read (CEF's SingletonSocket failed here)
                s = p.as_posix()
                try:
                    s.encode("utf-8")
                except UnicodeEncodeError:   # a name that is not valid text (Linux bytes): skipped, it crashed every run
                    continue
                seen.add(s)
                tick("names", k, len(roots), scanned=scanned + len(seen), folder=_short(dpl)[len(str(root)):].lstrip("/\\")[:80] or root.name)
                tr = tier(p)
                if tr in (1, 2) and cloud_only(st):
                    tr = 3   # OneDrive 'online-only': reading it would download it; found by name until it is on the PC
                if s in known and known[s][0] == st.st_size and abs(known[s][1] - st.st_mtime) < 1 and known[s][3] == tr:
                    continue   # unchanged (a changed tier rule re-reads the file; compared with the stored tier, so an
                    # online-only file is no longer dropped and added again on every run)
                if s in known:   # changed: drop old rows
                    fid = known[s][2]
                    drop(con, fid)
                cur = con.execute("INSERT INTO files(path,name,ext,size,mtime,tier,pending) VALUES(?,?,?,?,?,?,?)",
                                  (s, nfc(p.name), real_ext(p), st.st_size, st.st_mtime, tr, 1 if tr in (1, 2) else 0))
                fid = cur.lastrowid   # name and folder searchable at once; content comes in its wave
                cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, 0, "")).lastrowid
                con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(p.name), nfc(s[len(root.as_posix()):]), ""))
                new += 1
                if new % 500 == 0:
                    pause_wait()
                if new % 5000 == 0:
                    con.commit(); print(f"  {new} files {time.time() - t0:.0f}s", flush=True)
        # folders the walk could not open keep their files (a share that dropped mid-walk is not a deletion)
        cut = [Path(_short(getattr(e, "filename", "") or "")).as_posix().rstrip("/") + "/" for e in unread if getattr(e, "filename", None)]
        gone = [k for k in known if k not in seen and not any(k.startswith(c) for c in cut)]
        for s in gone:
            drop(con, known[s][2])
        con.commit()
        print(f"FINDR_SCAN new/changed {new} removed {len(gone)} in {time.time() - t0:.0f}s")
        if unread:
            print(f"FINDR_SKIPPED {len(unread)} folders could not be read ({type(unread[0]).__name__})", flush=True)
        scanned += len(seen)
        progress("names", k + 1, len(roots), scanned=scanned)
    _P["files"] = con.execute("SELECT count(*) FROM files WHERE tier>0").fetchone()[0]
    if not a.no_embed:   # stage 1b: a meaning vector from each file's name and folders (natural-language search on names)
        print("FINDR_STAGE namevec", flush=True); embed(con, a.model, names_only=True)
    if a.names_only:   # 2026-11-08 (#106): on battery the app still shows new files by name; their content stays pending
        progress("done", 1, 1); print("FINDR_STAGE done (names only)", flush=True); return
    print("FINDR_STAGE content", flush=True)
    for k, root in enumerate(roots):   # stage 2: content, the folders people look in first
        progress("content", k, len(roots)); read_wave(con, root, a)
    remask(con)
    if not a.no_embed:   # stage 3: meaning (each file's opening pieces first)
        print("FINDR_STAGE meaning", flush=True); embed(con, a.model)
    if os.environ.get("FINDR_OCR", "1") != "0":   # stage 4: text in scans and screenshots
        print("FINDR_STAGE ocr", flush=True)
        for k, root in enumerate(roots):
            progress("ocr", k, len(roots)); ocr_wave(con, root)
        if not a.no_embed:
            embed(con, a.model)
    progress("done", 1, 1); print("FINDR_STAGE done", flush=True)
    if os.environ.get("PHOTO_URL"):   # photos: one meaning vector each from the local photo model (no request seen)
        embed_photos(con, os.environ["PHOTO_URL"])
    print("FINDR_STATS", dict(con.execute("SELECT tier,count(*) FROM files GROUP BY tier").fetchall()),
          "chunks", con.execute("SELECT count(*) FROM chunks").fetchone()[0])


STAGES = ("nfc", "names", "namevec", "content", "meaning", "ocr")
_P = {"stages": {}, "started": time.time()}   # this run's view of every stage, for the app and the search page


def progress(stage, done, total, **kw):
    """where the index stands, for the app and the search page; written to the data folder (atomically)
    stages: done / now / later (left for idle time) / (absent: waiting). files: how many files this PC has."""
    d = os.environ.get("FINDR_DATA")
    if not d:
        return
    st = _P["stages"]
    if stage == "done":
        for k in STAGES:
            if st.get(k) == "now":
                st[k] = "done"
    elif stage in STAGES:
        for k in STAGES[:STAGES.index(stage)]:   # every earlier stage is finished (or was left for later)
            if st.get(k) in (None, "now") and k != "nfc":
                st[k] = "done"
        if st.get(stage) != "later":
            st[stage] = "now"
    p = {"stage": stage, "done": done, "total": total, "t": time.time(), "pid": os.getpid(), "started": _P["started"],
         "stages": st, "files": _P.get("files"), "paused": _P.get("paused", False), **kw}
    try:
        f = Path(d, "progress.json"); tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(p), encoding="utf-8"); os.replace(tmp, f)   # a reader never sees half a file
    except OSError:
        pass


_TICK = {}


def tick(stage, done, total, every=0.7, **kw):
    """progress at most every `every` seconds, with the rate and the minutes left of this stage. The app reads the file
    every 0.7 s: counts written per 200 files, per 256 pieces or per folder left the first index looking frozen for a
    minute or more on slow PCs (11/10: Windows and Mac, the name scan of a whole home folder is one folder)"""
    now = time.time()
    t = _TICK.get(stage)
    if t is None or done < t["done0"]:
        t = _TICK[stage] = {"t0": now, "done0": done, "last": 0.0}
    if now - t["last"] < every and done != total:
        return
    t["last"] = now
    rate = (done - t["done0"]) / max(now - t["t0"], 1e-6)
    if total and rate > 0 and now - t["t0"] >= 3:   # a few seconds of work before guessing the rest
        kw.setdefault("minutes_left", round((total - done) / rate / 60, 1))
    progress(stage, done, total, rate=round(rate, 1), **kw)


PAUSE_MAX = 3600   # a pause the person forgot about lapses after an hour


def pause_wait():
    """hold while the person paused indexing (the app or the search page writes 'pause' in the data folder)"""
    d = os.environ.get("FINDR_DATA")
    if not d:
        return
    f = Path(d, "pause")
    held = False
    while f.exists():
        try:
            if time.time() - f.stat().st_mtime > PAUSE_MAX:
                f.unlink(); break
        except OSError:
            break
        if not held:
            held = True; _P["paused"] = True
            print("FINDR_PAUSED", flush=True); _repeat_progress()
        time.sleep(1)
    if held:
        _P["paused"] = False; print("FINDR_RESUMED", flush=True); _repeat_progress()


def _repeat_progress():
    d = os.environ.get("FINDR_DATA")
    try:
        last = json.loads(Path(d, "progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        last = {"stage": "names", "done": 0, "total": 0}
    kw = {k: v for k, v in last.items() if k not in ("stage", "done", "total", "t", "pid", "started", "stages", "files", "paused")}
    progress(last.get("stage", "names"), last.get("done", 0), last.get("total", 0), **kw)


def priority(path, ext, mtime, now):
    """higher = read sooner: recent first; documents and the user's document folders ahead; machine logs and bulk data behind"""
    age = (now - mtime) / 86400
    bonus = 0
    if ext in DOC or ext in (".hwp", ".doc", ".ppt", ".xls", ".md", ".txt", ".html"):
        bonus += 60
    if "/Documents/" in path or "/GoogleDrive/" in path:
        bonus += 30
    if ext in DATA:
        bonus -= 60
    return bonus - age


def cloud_only(st):
    """a cloud placeholder (Windows: OneDrive 'online-only' and similar): its content is not on this PC"""
    a = getattr(st, "st_file_attributes", 0)
    return bool(a & (0x00400000 | 0x00040000 | 0x00001000))   # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE


def cloud_only_path(s):
    try:
        return cloud_only(os.stat(_long(s)))
    except OSError:
        return True   # cannot even stat it: do not try to read it


def like_pre(pre):
    """a folder prefix for LIKE with its own % and _ taken literally (ESCAPE '\\'): '~/my_docs' claimed '~/my-docs'"""
    return pre.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


_LOCK = None


def one_indexer(db):
    """one indexer per index: a second `findr index` failed on the unique path or on a locked database. An OS lock on a file
    next to the index, released by the system when the process ends, so a killed run never blocks the next one."""
    global _LOCK
    path = str(Path(db).expanduser()) + ".lock"
    if _LOCK is not None and not _LOCK.closed and _LOCK.name == path:
        return True   # this process already holds it (main() run twice in one process)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _LOCK = f
    return True


def _read_one(job):
    """text of one file; None when it could not be opened now (locked by another program): tried again next time"""
    s, lim = job
    try:
        return extract(Path(s), lim) or ""
    except (PermissionError, BlockingIOError):
        return None
    except OSError as e:
        return None if getattr(e, "winerror", None) in (32, 33) else ""   # sharing / lock violation on Windows
    except Exception:
        return ""


def read_all(jobs):
    """text of each (path, limit) in order; several worker processes when there is enough to read (FINDR_READ_WORKERS)"""
    n = int(os.environ.get("FINDR_READ_WORKERS") or max(1, min(8, (os.cpu_count() or 2) - 1)))
    if n <= 1 or len(jobs) < 16:
        yield from map(_read_one, jobs); return
    from concurrent.futures import ProcessPoolExecutor
    done = 0
    try:
        with ProcessPoolExecutor(max_workers=n) as ex:
            for r in ex.map(_read_one, jobs, chunksize=4):   # results come back in the given (priority) order
                done += 1
                yield r
    except Exception:   # a worker died (out of memory, a reader crashed), or no worker processes on this platform
        # the chunk that was being read stays pending (None) for the next run: it may be what crashed the worker;
        # the rest is read here, each text still with its own file
        skip = min(len(jobs), done + 4) if done else 0
        yield from (None for _ in range(done, skip))
        yield from map(_read_one, jobs[skip:])


def read_wave(con, root, a):
    """read the content of pending files under this root, best first, within this wave's age and size limits"""
    now = time.time(); pre = root.as_posix().rstrip("/") + "/"
    first = int(os.environ.get("FINDR_FIRST_PIECES") or 0) > 0
    # first pass: pending=1 (never read); the deep run also finishes the files read only in part (pending=2)
    rows = con.execute(f"SELECT id, path, ext, mtime, tier FROM files WHERE pending IN ({'1' if first else '1,2'}) AND path LIKE ? ESCAPE '\\'",
                       (like_pre(pre),)).fetchall()
    if a.max_age_days:
        rows = [r for r in rows if now - r[3] <= a.max_age_days * 86400]
    rows.sort(key=lambda r: -priority(r[1], r[2], r[3], now))
    if a.limit:
        rows = rows[: a.limit]
    t0 = time.time()
    full = lambda ext, tr: 1500 if tr == 2 else DATA_READ if ext in DATA else MAXREAD
    jobs = [(s, min(GIST, full(ext, tr)) if first else full(ext, tr)) for _, s, ext, _, tr in rows]
    locked = 0
    for i, ((fid, s, ext, mtime, tr), txt) in enumerate(zip(rows, read_all(jobs))):
        pause_wait()
        if txt is None:   # open in another program right now: stays pending, read on the next run
            locked += 1
            continue
        p = Path(s)
        cs = chunks(txt)[: (1 if tr == 2 else 2000)] or [""]
        con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file=? AND n>=0)", (fid,))
        con.execute("DELETE FROM chunks WHERE file=? AND n>=0", (fid,))
        rel = s[len(root.as_posix()):]
        for n, c in enumerate(cs):
            cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, n, c)).lastrowid
            con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(p.name), nfc(rel), nfc(c)))
        part = first and tr == 1 and ext not in DATA and (len(txt) >= GIST - 64 or (ext not in RICH and _size(s) > GIST))   # cut at the gist
        # (plain text is read by bytes: Korean takes 3 per character, so the file size tells): the deep run reads the rest
        con.execute("UPDATE files SET pending=? WHERE id=?", (2 if part else 0, fid))
        if (i + 1) % 200 == 0:
            con.commit(); print(f"  read {i + 1}/{len(rows)} {time.time() - t0:.0f}s", flush=True)
        tick("content", i + 1, len(rows), folder=root.name)
    con.commit()
    left = con.execute("SELECT count(*) FROM files WHERE pending IN (1,2) AND path LIKE ? ESCAPE '\\'", (like_pre(pre),)).fetchone()[0]
    print(f"FINDR_READ {len(rows)} files in {time.time() - t0:.0f}s, still pending {left}" + (f" (in use by another program: {locked})" if locked else ""))


SHOTLIKE = re.compile(r"screenshot|screen ?shot|스크린샷|캡처|캡쳐|화면 ?캡|capture|snip|clip|scan|스캔|영수증|receipt"
                      r"|등록증|증명|확인서|신분증|명함|계약서|통장|사본|진단서|청구서|여권|면허|certificate|license|passport|contract|invoice", re.I)
# (images named like documents: a photographed certificate or contract is a document; its text is read on the first pass)
_OCR = None


OCR_SIDE = int(os.environ.get("FINDR_OCR_SIDE") or 2400)   # longest side of a page image given to OCR (pixels)


_ENGINE = None


def ocr_engine():
    """the text recognizer for this system: 'vision' (macOS built-in, Korean in accurate mode), 'windows' (Windows' own OCR,
    when its Korean recognizer is installed), else 'easyocr' (NVIDIA or Apple GPU, or the CPU). FINDR_OCR_ENGINE overrides.
    On an M3, 6 Korean slide pages: Vision 0.6 s/page, EasyOCR on the Apple GPU 4.3 s, on the CPU 22.8 s; Vision also misread less.
    On 12 synthetic Korean/English A4 pages, 4 CPU cores: EasyOCR 96% of characters at 14.6 s/page."""
    global _ENGINE
    if _ENGINE:
        return _ENGINE
    want = (os.environ.get("FINDR_OCR_ENGINE") or "").lower()
    if want in ("vision", "windows", "easyocr"):
        _ENGINE = want; return want
    _ENGINE = "easyocr"
    if sys.platform == "darwin":
        try:
            import Vision  # noqa: F401  (pyobjc-framework-Vision)
            _ENGINE = "vision"
        except ImportError:
            pass
    elif sys.platform == "win32" and _windows_ocr() is not None:
        _ENGINE = "windows"
    if _ENGINE == "easyocr":
        import importlib.util
        if importlib.util.find_spec("easyocr") is None:
            _ENGINE = "none"   # no recognizer installed here (#31): scans stay findable by name
    return _ENGINE


def missing_parts():
    """readers this run will go without, each with what it costs (#31: after a git pull nothing said so)"""
    import importlib.util
    out = []
    if ocr_engine() == "none":
        out.append("easyocr (스캔·스크린샷 글자)")
    for mod, what in (("pypdfium2", "빠른 PDF 읽기"), ("pillow_heif", "아이폰 사진 HEIC"), ("python_calamine", "xls·ods 표")):
        if importlib.util.find_spec(mod) is None:
            out.append(f"{mod} ({what})")
    return out


_WIN = []


def _windows_ocr():
    """Windows' own recognizer for Korean (Windows 10/11 with the Korean OCR feature; Korean Windows has it), or None.
    Without it: Settings > Time & language > Language > Korean > Optional features, or
    Add-WindowsCapability -Online -Name "Language.OCR~~~ko-KR~0.0.1.0" (administrator)."""
    if not _WIN:
        eng = None
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine
            eng = OcrEngine.try_create_from_language(Language("ko")) if OcrEngine.is_language_supported(Language("ko")) else None
        except Exception:
            eng = None
        _WIN.append(eng)
    return _WIN[0]


def _windows_lines(img):
    """Windows OCR (Korean recognizer; it also reads Latin letters and digits), lines top to bottom"""
    import asyncio
    import numpy as np
    from PIL import Image
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    eng = _windows_ocr()
    im = Image.fromarray(img) if not isinstance(img, (str, Path)) else Image.open(img)
    im = im.convert("RGB")
    side = max(1, int(getattr(eng, "max_image_dimension", 0) or 2600))   # larger images are refused
    if max(im.size) > side:
        im.thumbnail((side, side))
    a = np.asarray(im.convert("RGBA"))[..., [2, 1, 0, 3]].copy()   # BGRA, as the recognizer takes it
    w = DataWriter(); w.write_bytes(a.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(w.detach_buffer(), BitmapPixelFormat.BGRA8, im.width, im.height)

    async def go():
        return await eng.recognize_async(bmp)
    res = asyncio.run(go())
    return [ln.text for ln in res.lines if ln.text.strip()]


def ocr_reader():
    """EasyOCR (Korean + English), loaded once; its models live in findr's model folder. Uses NVIDIA or the Apple GPU when present."""
    global _OCR
    if _OCR is None:
        import easyocr, torch
        d = Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "ocr"
        d.mkdir(parents=True, exist_ok=True)
        mps = getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available()
        _OCR = easyocr.Reader(["ko", "en"], gpu=torch.cuda.is_available() or mps, model_storage_directory=str(d), verbose=False)   # Apple GPU too (#8)
    return _OCR


def _vision_lines(img):
    """macOS Vision text recognition (accurate, Korean + English), lines top to bottom"""
    import io
    import Vision
    from Foundation import NSData
    from PIL import Image
    im = Image.fromarray(img) if not isinstance(img, (str, Path)) else Image.open(img).convert("RGB")
    buf = io.BytesIO(); im.save(buf, format="JPEG", quality=92); b = buf.getvalue()
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)   # accurate: the level that has Korean
    # language correction roughly doubles the time (M3: 0.45 -> 0.23 s/page without it) and fixes Korean words
    # (장연 -> 장면, 명렁 -> 명령); kept on unless FINDR_OCR_FAST=1
    req.setRecognitionLanguages_(["ko-KR", "en-US"]); req.setUsesLanguageCorrection_(os.environ.get("FINDR_OCR_FAST") != "1")
    h = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(b, len(b)), None)
    ok, err = h.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(str(err))
    obs = []
    for o in req.results() or []:
        c = o.topCandidates_(1)
        if c and c[0].string().strip():
            bb = o.boundingBox()   # normalised, origin bottom-left
            obs.append((-round(bb.origin.y + bb.size.height, 2), bb.origin.x, c[0].string()))
    return [t for _, _, t in sorted(obs)]


def ocr_lines(img):
    """text lines of one image (a path or an RGB array), top to bottom"""
    try:
        if ocr_engine() == "vision":
            return _vision_lines(img)
        if ocr_engine() == "windows":
            return _windows_lines(img)
    except Exception:
        pass   # the system recognizer failed on this image: EasyOCR below
    try:
        return [t for t in ocr_reader().readtext(img, detail=0, paragraph=True) if t.strip()]
    except Exception:
        return []


def _page_scale(page, side=None):
    """2x for normal pages, less for big ones so the image's longest side stays near OCR_SIDE (a 3670-pt slide was 7339 px)"""
    w, h = page.get_size()
    return max(0.5, min(2.0, (side or OCR_SIDE) / max(w, h, 1)))


OCR_FIRST_PAGES = int(os.environ.get("FINDR_OCR_FIRST_PAGES") or 3)


_SAID = []


def _gpu_present():
    try:
        import torch
        return torch.cuda.is_available() or (getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available())
    except Exception:
        return False
OCR_PARTIAL = 3   # files.ocr: 1 = read, 3 = only the first pages read (a deep run reads the rest)


def _file_sig(path):
    """content fingerprint for finding copies ('견적서 (1).pdf' next to '견적서.pdf'): sha1 of the whole file"""
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _ocr_copy_of(con, fid, path, size, deep):
    """an identical file that was already OCR'd (same size, same bytes): (its chunk texts, its ocr state), or None"""
    want = (1,) if deep else (1, OCR_PARTIAL)
    rows = con.execute(f"SELECT id, path, ocr FROM files WHERE size=? AND id<>? AND ocr IN ({','.join('?' * len(want))})",
                       (size, fid, *want)).fetchall()
    if not rows:
        return None
    try:
        sig = _file_sig(path)
    except OSError:
        return None
    for oid, opath, ost in rows:
        try:
            if _file_sig(opath) != sig:
                continue
        except OSError:
            continue
        texts = [t for (t,) in con.execute("SELECT text FROM chunks WHERE file=? AND n>=0 ORDER BY n", (oid,))]
        return texts, ost
    return None


def photos_to_read(rows, deep, engine):
    """which photos the text recognizer reads now. rows: (id, path), newest first. A deep run reads them all. A first pass
    reads screenshot-like names, and with the system's own recognizer (macOS Vision, Windows OCR) also the newest
    FINDR_OCR_PHOTOS photos (300): on a Mac Air (11/9) half of 50 random photos in the person's own folders carried text worth
    finding (screenshots, product pages, slides, posters, notes) and Vision read one in 0.17 s, yet 1 of 1,443 had any text
    in the index, because the deep run that reads the rest had not come"""
    if deep:
        return list(rows)
    first = [r for r in rows if SHOTLIKE.search(r[1])]
    if engine in ("vision", "windows"):
        n = int(os.environ.get("FINDR_OCR_PHOTOS") or 300)
        have = {r[0] for r in first}
        first += [r for r in rows if r[0] not in have][:max(0, n)]
    return first


def ocr_wave(con, root, first_pages=None, deep_pages=50):
    """scanned PDFs and screenshots now; every photo on a deep run. Each file is read once (files.ocr).
    First pass: the first OCR_FIRST_PAGES pages of a PDF (files.ocr = 3 when pages are left); a deep run reads the rest.
    A copy of a file already read gets that file's text instead of a second OCR."""
    deep = int(os.environ.get("FINDR_FIRST_PIECES") or 0) == 0
    first_pages, side = first_pages or OCR_FIRST_PAGES, OCR_SIDE
    if ocr_engine() == "none":
        return   # nothing to read with (FINDR_WARN at the start of the run says what to install)
    if ocr_engine() == "easyocr" and not _gpu_present() and not deep and os.environ.get("FINDR_OCR_FIRST") != "1":
        if not _SAID:
            _SAID.append(1); print("FINDR_OCR later: scans are read during idle time on this PC (no GPU or system text recognizer)", flush=True)
            _P["stages"]["ocr"] = "later"
        return   # first index: speed first; the deep run reads them
    if ocr_engine() == "easyocr" and not _gpu_present():
        # EasyOCR on a CPU, for search (words that find the file, not a faithful copy). 12 synthetic A4 pages, 4 cores,
        # share of words found / s per page:  2400 px 89-91% 9-15 s,  1600 px 90% 4.1-4.4 s,  1200 px 85-88% 2.6-2.9 s,
        # 1000 px 72-87% 2 s (small print falls off). First pass: page 1 at 1200 px; the deep run: the rest at 1600 px.
        first_pages = int(os.environ.get("FINDR_OCR_FIRST_PAGES") or 1)
        side = int(os.environ.get("FINDR_OCR_SIDE") or (1600 if deep else 1200))
    if ocr_engine() == "easyocr":
        try:
            import easyocr  # noqa: F401
        except ImportError:
            return
    if "ocr" not in [r[1] for r in con.execute("PRAGMA table_info(files)")]:
        con.execute("ALTER TABLE files ADD COLUMN ocr INT DEFAULT 0")
    pre = root.as_posix().rstrip("/") + "/"
    pdfs = [r for r in con.execute("""SELECT f.id, f.path FROM files f WHERE f.ext='.pdf' AND f.tier=1 AND f.path LIKE ? ESCAPE '\\'
              AND ((COALESCE(f.ocr,0)=0 AND (SELECT COALESCE(sum(length(c.text)),0) FROM chunks c WHERE c.file=f.id) < 200)
                   OR (? AND f.ocr=?))
              ORDER BY f.mtime DESC""", (like_pre(pre), 1 if deep else 0, OCR_PARTIAL))]
    rows = [(i, s) for i, s, e, z in con.execute("SELECT id, path, ext, size FROM files WHERE tier=3 AND COALESCE(ocr,0)=0 AND path LIKE ? ESCAPE '\\' AND size < 30000000 "
                                                 "ORDER BY mtime DESC", (like_pre(pre),)) if (e or "").lower() in PHOTO_EXT
            and not cloud_only_path(s) and not asset_like(s, z)]   # an online-only photo is not downloaded for OCR (a deep run fetched whole photo libraries)
    imgs = photos_to_read(rows, deep, ocr_engine())
    if not pdfs and not imgs:
        return
    t0, got, chars, copied = time.time(), 0, 0, 0
    heif_on()
    items = pdfs + imgs
    for k, (fid, s) in enumerate(items):
        pause_wait()
        lines, state, texts = [], 1, None
        tick("ocr", k, len(items), folder=root.name)
        try:
            size = os.path.getsize(s)
        except OSError:
            size = -1
        same = _ocr_copy_of(con, fid, s, size, deep) if size > 0 else None
        if same:
            texts, state = same; copied += 1
        elif s.lower().endswith(".pdf"):
            try:
                import numpy as _np, pypdfium2 as pdfium
                doc = pdfium.PdfDocument(s); end = time.time() + 3 * FILE_SECONDS   # reading a scan is slower than reading text
                upto = min(len(doc), deep_pages if deep else first_pages)
                if not deep and len(doc) > upto:
                    state = OCR_PARTIAL   # pages left for the deep run
                for i in range(upto):
                    if time.time() > end:
                        break
                    pg = doc[i]
                    lines += ocr_lines(_np.array(pg.render(scale=_page_scale(pg, side)).to_pil().convert("RGB")))
                doc.close()
            except Exception:
                pass
        else:
            try:
                from PIL import Image
                import numpy as _np
                im = Image.open(s).convert("RGB"); im.thumbnail((side, side))
                lines = ocr_lines(_np.array(im))
            except Exception:
                lines = []
        txt = "\n".join(texts if texts is not None else lines)
        if len(re.sub(r"\s", "", txt)) >= 8:   # real text, not a stray mark
            p_ = Path(s); rel = s[len(root.as_posix()):]
            con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file=? AND n>=0)", (fid,))
            con.execute("DELETE FROM chunks WHERE file=? AND n>=0", (fid,))
            for n, c in enumerate((texts if texts is not None else chunks(txt))[:2000] or [""]):
                cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, n, c)).lastrowid
                con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(p_.name), nfc(rel), nfc(c)))
            got += 1; chars += len(txt)
        con.execute("UPDATE files SET ocr=? WHERE id=?", (state, fid))
        con.commit()   # each file: progress is visible and an interrupted run keeps what it read
    con.commit()
    print(f"FINDR_OCR {len(pdfs)} scanned PDFs + {len(imgs)} images read, text found in {got} ({chars:,} chars), "
          f"{copied} copies of files already read, {time.time() - t0:.0f}s", flush=True)


def remask(con):
    """every run: any stored chunk that still holds a key-like value is masked, re-indexed and re-embedded.
    Triggers note every chunk whose text is written (inserted, or filled in place by a later stage); only those are looked at,
    after one full look when the triggers are new or KEYLIKE changed. A run with no change scanned 920k chunks for 58 s (#67)"""
    import hashlib
    hit = 0
    con.execute("CREATE TABLE IF NOT EXISTS mask_todo(id INTEGER PRIMARY KEY)")
    con.execute("CREATE TRIGGER IF NOT EXISTS mask_ins AFTER INSERT ON chunks BEGIN INSERT OR IGNORE INTO mask_todo VALUES(NEW.id); END")
    con.execute("CREATE TRIGGER IF NOT EXISTS mask_upd AFTER UPDATE OF text ON chunks BEGIN INSERT OR IGNORE INTO mask_todo VALUES(NEW.id); END")
    pat = hashlib.sha1((KEYLIKE.pattern + "|" + "|".join(KEYLIKE_HINTS)).encode()).hexdigest()[:16]
    seen = con.execute("SELECT value FROM meta WHERE key='mask_pattern'").fetchone()
    w = " OR ".join(["c.text LIKE ?"] * len(KEYLIKE_HINTS))
    # CROSS JOIN keeps mask_todo outer: with a plain JOIN SQLite scanned every chunk (2.6 GB, ~5 s) to find an empty todo list
    src = "chunks c" if not seen or seen[0] != pat else "mask_todo t CROSS JOIN chunks c ON c.id = t.id"
    rows = con.execute(f"SELECT c.id, c.file, c.text FROM {src} WHERE {w}", [f"%{h}%" for h in KEYLIKE_HINTS]).fetchall()
    for cid, fid, txt in rows:
        if txt and KEYLIKE.search(txt):
            t = KEYLIKE.sub("[가림]", txt); name, path = con.execute("SELECT name, path FROM files WHERE id=?", (fid,)).fetchone()
            con.execute("DELETE FROM fts WHERE rowid=?", (cid,))
            con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(name), nfc(path), t))
            con.execute("UPDATE chunks SET text=?, emb=NULL WHERE id=?", (t, cid)); hit += 1
    con.execute("DELETE FROM mask_todo")   # (the masking above re-noted its own rows: already clean)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('mask_pattern', ?)", (pat,))
    con.commit(); print(f"FINDR_MASK {hit} chunks masked ({'all' if src == 'chunks c' else 'new or changed'} chunks looked at)")


PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}


def asset_like(path, size):
    """an image that is not a photo: macOS '._' metadata files, and icons, sprites and logos (short side under 200 px).
    On a developer's Mac (11/9) 73 of 100 image files were such assets and a photo model read them as cards and logos;
    reading or embedding them only costs time. Only small files are opened (header only)"""
    name = os.path.basename(path or "")
    if name.startswith("._") or (size or 0) < 2048:
        return True
    if (size or 0) >= 150_000:
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            return min(im.size) < 200
    except Exception:
        return False


def heif_on():
    """iPhone photos (HEIC/HEIF) open like any other image once pillow-heif is installed"""
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass


def taken_at(path):
    """when the photo was taken (EXIF DateTimeOriginal / DateTime), or None: the file time stays the fallback"""
    try:
        from PIL import Image
        heif_on()
        ex = Image.open(path).getexif()
        v = ex.get_ifd(0x8769).get(36867) or ex.get(306)
        return datetime.datetime.strptime(str(v).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp() if v else None
    except Exception:
        return None


def embed_photos(con, url, bs=16):
    """photo vectors for photos that have none yet, newest first; a photo the model cannot read stays searchable by name"""
    import base64, json as _j, urllib.request
    con.execute("CREATE TABLE IF NOT EXISTS photo_emb(file INTEGER PRIMARY KEY, emb BLOB, taken REAL)")
    if "taken" not in [r[1] for r in con.execute("PRAGMA table_info(photo_emb)")]:
        con.execute("ALTER TABLE photo_emb ADD COLUMN taken REAL")
    rows = [(i, p) for i, p, e, z in con.execute("SELECT id, path, ext, size FROM files WHERE id NOT IN (SELECT file FROM photo_emb) ORDER BY mtime DESC")
            if (e or "").lower() in PHOTO_EXT and not asset_like(p, z)]   # icons and '._' files: no photo vector (found by name)
    t0, done = time.time(), 0
    for k in range(0, len(rows), bs):
        b = rows[k:k + bs]
        try:
            req = urllib.request.Request(url.rstrip("/") + "/photo", _j.dumps({"paths": [p for _, p in b]}).encode(), {"Content-Type": "application/json"})
            vecs = _j.loads(urllib.request.urlopen(req, timeout=600).read())["vecs"]
        except Exception as e:
            print(f"FINDR_PHOTO stopped: photo model not reachable ({type(e).__name__})"); break
        con.executemany("INSERT OR REPLACE INTO photo_emb(file, emb, taken) VALUES(?,?,?)",
                        [(i, base64.b64decode(v), taken_at(p)) for (i, p), v in zip(b, vecs) if v])
        con.commit(); done += sum(1 for v in vecs if v)
    print(f"FINDR_PHOTO {done}/{len(rows)} photos {time.time() - t0:.0f}s")


def ooxml_text(p, limit):
    """Word, Excel, PowerPoint, HWPX: text parts read in pieces, stopping once `limit` characters are in.
    A large workbook can expand to gigabytes of XML; reading it whole used to take all the memory and time."""
    import zipfile
    keys = ("word/document", "sharedStrings", "worksheets/", "slides/slide", "Contents/section")
    def order(n):   # body first; slides and sections in their own order (slide2 before slide10)
        m = re.search(r"(\d+)\.xml$", n)
        return (0 if ("document" in n or "sharedStrings" in n) else 1, n.rsplit("/", 1)[0], int(m.group(1)) if m else 0)
    z = zipfile.ZipFile(p); out, have = [], 0; end = time.time() + FILE_SECONDS
    try:   # the properties the saving program wrote (title, subject, keywords, author): first, they say what the file is
        core = z.read("docProps/core.xml").decode("utf-8", "ignore")
        tag = lambda t: (re.search(rf"<(?:\w+:)?{t}[^>]*>([^<]*)</", core) or [None, None])[1]
        props = props_line(tag("title"), tag("subject"), tag("keywords"), tag("creator"))
        if props:
            out.append(props); have += len(props)
    except KeyError:
        pass
    for n in sorted((n for n in z.namelist() if n.endswith(".xml") and any(k in n for k in keys)), key=order):
        with z.open(n) as f:
            tail = ""
            while have < limit and time.time() < end:
                b = f.read(1 << 20)
                if not b:
                    break
                txt = tail + b.decode("utf-8", "ignore")
                cut = txt.rfind(">") + 1   # a tag may continue in the next piece
                tail = txt[cut:]
                t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt[:cut]))
                if t.strip():
                    out.append(t); have += len(t)
        if have >= limit:
            break
    return re.sub(r"\s+", " ", " ".join(out)).strip()[:limit]


def _snappy(b):
    """raw snappy block (what Keynote's .iwa chunks hold)"""
    i = n = sh = 0
    while True:
        c = b[i]; i += 1; n |= (c & 0x7F) << sh; sh += 7
        if c < 0x80:
            break
    out = bytearray()
    while i < len(b):
        tag = b[i]; i += 1; t = tag & 3
        if t == 0:
            ln = tag >> 2
            if ln >= 60:
                k = ln - 59; ln = int.from_bytes(b[i:i + k], "little"); i += k
            ln += 1; out += b[i:i + ln]; i += ln
            continue
        if t == 1:
            ln = ((tag >> 2) & 7) + 4; off = ((tag >> 5) << 8) | b[i]; i += 1
        elif t == 2:
            ln = (tag >> 2) + 1; off = int.from_bytes(b[i:i + 2], "little"); i += 2
        else:
            ln = (tag >> 2) + 1; off = int.from_bytes(b[i:i + 4], "little"); i += 4
        if off >= ln:
            out += out[-off:len(out) - off + ln]
        else:   # overlapping copy
            for _ in range(ln):
                out.append(out[-off])
    return bytes(out)


def _iwa(data):
    """an .iwa file: chunks of [0x00][3-byte length][snappy block]"""
    out, i = [], 0
    while i + 4 <= len(data):
        ln = int.from_bytes(data[i + 1:i + 4], "little"); i += 4
        try:
            out.append(_snappy(data[i:i + ln]))
        except Exception:
            break
        i += ln
    return b"".join(out)


_WORDY = re.compile(r"[가-힣A-Za-z]")
_STYLEID = re.compile(r"(paragraph|character|list|shape|cell|table|chart|slide|text|caption|connection|drawable)s?style|^#?[a-z]+-\d+(-|$)|^!", re.I)


def _pb_strings(buf):
    """text in protobuf bytes: every length-delimited field whose bytes read as text (Keynote's slide text, notes, titles)"""
    out, i, n = [], 0, len(buf)
    while i < n - 2:
        if buf[i] & 7 == 2 and buf[i] >> 3:
            j, ln, sh = i + 1, 0, 0
            while j < n and sh < 35:
                c = buf[j]; j += 1; ln |= (c & 0x7F) << sh; sh += 7
                if c < 0x80:
                    break
            if 2 <= ln <= 20000 and j + ln <= n:
                try:
                    t = buf[j:j + ln].decode("utf-8")
                except UnicodeDecodeError:
                    t = None
                if t and _WORDY.search(t) and sum(ch.isprintable() or ch in "\n\t" for ch in t) >= 0.95 * len(t) \
                        and not re.fullmatch(r"[A-Za-z0-9_.\-/]+", t):   # identifiers, style and class names are not text
                    out.append(t); i = j + ln; continue
        i += 1
    return out


# iWork documents carry the locale's calendar and currency names and a style sheet: not what people search for
_IWORK_NOISE = re.compile(r"^(?:[일월화수목금토]요일|오전|오후|AM|PM|(?:Sun|Mon|Tues|Wednes|Thurs|Fri|Satur)day|"
                          r"(?:January|February|March|April|May|June|July|August|September|October|November|December)|"
                          r"\d{1,2}월|[A-Z]{2}[$¥€£₩]|사용자 설정 포맷.*|Office Theme.*)$")
_IWORK_SKIP = ("Stylesheet", "ViewState", "Metadata", "CalculationEngine", "AnnotationAuthorStorage")


def keynote_text(p, limit):
    """Apple Keynote: '09 and older keep XML (index.apxl); 6+ keep snappy-framed protobuf (Index/*.iwa, sometimes in Index.zip)"""
    import gzip, io, zipfile
    try:
        z = zipfile.ZipFile(p)
        names = z.namelist()
        for nm in names:
            if nm.endswith(("index.apxl", "index.apxl.gz")):
                raw = z.read(nm)
                raw = gzip.decompress(raw) if nm.endswith(".gz") else raw
                return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore"))).strip()[:limit]
        if "Index.zip" in names:
            z = zipfile.ZipFile(io.BytesIO(z.read("Index.zip"))); names = z.namelist()
        seen, out, size = set(), [], 0; end = time.time() + FILE_SECONDS
        iwork = Path(str(p)).suffix.lower() in (".pages", ".numbers")
        for nm in sorted(names, key=lambda x: (not x.startswith(("Index/Slide", "Index/Tables/")), x)):   # slides / tables first
            if time.time() > end:
                break
            if not (nm.startswith("Index/") and nm.endswith(".iwa")) or z.getinfo(nm).file_size > 50_000_000:
                continue
            if iwork and any(k in nm for k in _IWORK_SKIP):
                continue
            for t in _pb_strings(_iwa(z.read(nm))):
                t = t.strip()
                if len(_WORDY.findall(t)) < 2 or _STYLEID.search(t):   # style ids, symbols: not what people search for
                    continue
                if iwork and _IWORK_NOISE.match(t):
                    continue
                if t and t not in seen:
                    seen.add(t); out.append(t); size += len(t)
            if size >= limit:
                break
        return "\n".join(out)[:limit]
    except Exception:
        return ""


def pdf_props(p):
    """title, subject and keywords that the saving program wrote into the PDF ('' when none or generic)"""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(p))
        try:
            m = doc.get_metadata_dict(skip_empty=True)
        finally:
            doc.close()
    except Exception:
        return ""
    return props_line(m.get("Title"), m.get("Subject"), m.get("Keywords"), m.get("Author"))


_GENERIC = re.compile(r"^(untitled|제목 ?없음|microsoft (word|powerpoint|excel).*|document\d*|문서\d*|슬라이드 ?\d*|presentation\d*|pdf|unknown|user|admin|owner)$", re.I)


def props_line(title=None, subject=None, keywords=None, author=None):
    """one line of document properties for the index, skipping the empty and the generic ('Microsoft Word - 문서1')"""
    out = []
    for k, v in (("제목", title), ("주제", subject), ("키워드", keywords), ("작성자", author)):
        v = re.sub(r"\s+", " ", str(v or "")).strip()[:200]
        if v and not _GENERIC.match(v) and not re.fullmatch(r"[\W_]*", v):
            out.append(f"{k}: {v}")
    return (" | ".join(out) + "\n") if out else ""


def pdf_text(p, limit):
    """pdftotext when it is installed (Linux; best layout), else pypdf (Windows, macOS: nothing extra to install)"""
    import shutil
    if shutil.which("pdftotext") and os.environ.get("FINDR_PDF") in (None, "", "pdftotext"):
        pages = "500" if limit > 100_000 else "12"   # a gist needs the first pages only
        out = subprocess.run(["pdftotext", "-layout", "-l", pages, str(p), "-"], capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")
        return (pdf_props(p) + out)[:limit]
    if os.environ.get("FINDR_PDF") != "pypdf":
        try:
            import pypdfium2 as pdfium   # Chrome's PDF engine: much faster than pure-Python parsing
            doc = pdfium.PdfDocument(str(p)); out, n = [], 0; end = time.time() + FILE_SECONDS
            try:
                for i in range(len(doc)):
                    if time.time() > end:
                        break
                    pg = doc[i]; tp = pg.get_textpage(); t = tp.get_text_range() or ""; tp.close(); pg.close()
                    out.append(t); n += len(t)
                    if n >= limit:
                        break
            finally:
                doc.close()
            return (pdf_props(p) + "\n".join(out))[:limit]
        except ImportError:
            pass
        except Exception:
            return ""
    try:
        from pypdf import PdfReader
        out, n = [], 0; end = time.time() + FILE_SECONDS
        for pg in PdfReader(str(p)).pages:
            if time.time() > end:
                break
            t = pg.extract_text() or ""; out.append(t); n += len(t)
            if n >= limit:
                break
        return "\n".join(out)[:limit]
    except Exception:
        return ""


def drop(con, fid):
    con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file=?)", (fid,))
    con.execute("DELETE FROM chunks WHERE file=?", (fid,)); con.execute("DELETE FROM files WHERE id=?", (fid,))
    try:
        con.execute("DELETE FROM photo_emb WHERE file=?", (fid,))
    except sqlite3.OperationalError:
        pass


class EngineEncoder:
    """document vectors from the search engine already running on this PC (POST /embed): the model stays in one process.
    The indexer's own copy (torch and the model) was 1.7 of its 1.75 GB, and next to the engine's 2.7 GB it went past what
    an 8 GB laptop leaves: the OS killed the engine (3 GB reproduction). Used when the engine holds the same encoder;
    FINDR_EMBED_VIA=local turns it off, and any failure falls back to the indexer's own copy."""
    STEP = 32

    def __init__(self, url, pw, name):
        self.url, self.name = url, name
        import base64
        self.hdr = {"Authorization": "Basic " + base64.b64encode(f"index:{pw}".encode()).decode(), "X-Findr": "1",
                    "Content-Type": "application/json"}

    @classmethod
    def connect(cls, name):
        if os.environ.get("FINDR_EMBED_VIA", "auto") == "local" or not name:
            return None
        d = os.environ.get("FINDR_DATA")
        try:
            pw = Path(d, ".password").read_text().strip() if d else ""
        except OSError:
            return None
        e = cls(f"http://127.0.0.1:{os.environ.get('FINDR_APP_PORT') or 8096}", pw, name)
        try:
            e.vectors(["준비"])
            return e
        except Exception:   # noqa: BLE001  (no engine, another encoder, an old engine without /embed)
            return None

    def vectors(self, texts):
        import base64
        import urllib.request
        out = []
        for i in range(0, len(texts), self.STEP):
            req = urllib.request.Request(self.url + "/embed", json.dumps({"encoder": self.name, "texts": texts[i:i + self.STEP]}).encode(),
                                         self.hdr)
            r = json.loads(urllib.request.urlopen(req, timeout=300).read())
            out.append(np.frombuffer(base64.b64decode(r["vecs"]), np.float16).reshape(-1, r["dim"]))
        return np.concatenate(out) if out else np.zeros((0, 1), np.float16)


def _meta_encoder(con):
    r = con.execute("SELECT value FROM meta WHERE key='encoder'").fetchone()
    return r[0] if r else None


def embed(con, model, names_only=False):
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    if model in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") and not con.execute("SELECT 1 FROM chunks WHERE emb IS NOT NULL LIMIT 1").fetchone():
        con.execute("INSERT OR REPLACE INTO meta VALUES('encoder', ?)", (model,)); con.commit()   # a new index records its encoder
    cap = int(os.environ.get("FINDR_FIRST_PIECES") or 0)   # first pass on a large PC: each file's opening pieces only
    if names_only:   # every file's name and folders, content or not (a file read later gets new pieces and vectors)
        rows = con.execute("""SELECT c.id, c.text, f.path FROM chunks c JOIN files f ON f.id=c.file
                              WHERE c.emb IS NULL AND f.tier>0 AND c.n=0 AND COALESCE(c.text,'')='' ORDER BY f.mtime DESC""").fetchall()
    else:
        rows = con.execute(f"""SELECT c.id, c.text, f.path FROM chunks c JOIN files f ON f.id=c.file
                          WHERE c.emb IS NULL AND f.tier>0 AND COALESCE(f.pending,0) IN (0,2) {"AND c.n < %d" % cap if cap > 0 else ""}
                          ORDER BY (c.n > 1), f.mtime DESC, c.n""").fetchall()   # every file's opening pieces first, newest first
    later = con.execute("SELECT count(*) FROM chunks c JOIN files f ON f.id=c.file WHERE c.emb IS NULL AND f.tier>0 "
                        "AND COALESCE(f.pending,0) IN (0,2) AND c.n >= ?", (cap,)).fetchone()[0] if cap > 0 and not names_only else 0
    if not rows:   # nothing to embed: no encoder load (three loads cost ~7 s per run with nothing new)
        con.commit(); print(f"FINDR_EMBED 0 chunks 0s (computed 0, identical skipped 0" + (f", later pieces left for a deep run {later})" if later else ")"))
        return
    name = _meta_encoder(con)
    eng = EngineEncoder.connect(name) if name in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") else None
    enc = None

    def local():
        """the indexer's own copy of the encoder (no engine, or the engine went away)"""
        from enc_eg2 import EG2, make_encoder
        import torch
        e = make_encoder(name) if name in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") else None
        if e is None:
            raise RuntimeError(f"no encoder for {name}")
        if not isinstance(e, EG2):
            e.tok.model_max_length = 512
        if os.environ.get("FINDR_THREADS"):   # background indexing: leave CPU cores for the person's own work
            torch.set_num_threads(max(1, int(os.environ["FINDR_THREADS"])))
        return e

    def vecs(texts):
        nonlocal eng, enc
        if eng is not None:
            try:
                return eng.vectors(texts)
            except Exception as e:   # noqa: BLE001  (the engine stopped or was restarted): carry on with our own copy
                print(f"FINDR_EMBED engine gone ({type(e).__name__}): this run computes its own vectors", flush=True); eng = None
        if enc is None:
            enc = local()
        import torch
        with torch.no_grad():
            return emb512(enc, texts).cpu().numpy().astype(np.float16)

    if eng is None:
        enc = local()
    print(f"FINDR_EMBED_VIA {'engine' if eng is not None else 'local'} ({name})", flush=True)
    # identical inputs (a copied file, repeated boilerplate under the same name) are computed once
    groups = {}
    for cid, t, p in rows:
        groups.setdefault(f"{label(Path(p))}\n{t}"[:1200], []).append(cid)
    uniq = list(groups.items())
    t0 = last = time.time(); warned = False
    stage = "namevec" if names_only else "meaning"
    progress(stage, 0, len(uniq))   # the stage shows at once, not after its first batch
    i, bs = 0, 256
    while i < len(uniq):
        pause_wait()
        b = uniq[i:i + bs]
        tb = time.time()
        v = vecs([k for k, _ in b])
        if time.time() - tb > 8 and bs > 32:
            bs //= 2   # a slow PC (a large model on a CPU: 50 s per 256) reports more often
        con.executemany("UPDATE chunks SET emb=?, emb2=NULL WHERE id=?", [(x.tobytes(), cid) for x, (_, cids) in zip(v, b) for cid in cids])   # changed text: its side vector is stale
        done_ = i + len(b); i = done_
        rate = done_ / max(time.time() - t0, 1e-6); left = (len(uniq) - done_) / max(rate, 1e-6)
        tick(stage, done_, len(uniq), minutes_left=round(left / 60, 1))
        if done_ % 2560 < len(b) or time.time() - last > 30 or done_ == len(uniq):   # saved often
            con.commit(); last = time.time()
            print(f"  embedded {done_}/{len(uniq)} {last - t0:.0f}s | {rate:.0f} pieces/s | about {left / 60:.0f} min left", flush=True)
            if not warned and done_ >= 1024 and left > 6 * 3600 and name == "embeddinggemma-2":
                warned = True
                print("FINDR_SLOW 이 PC에서는 큰 모델이 느립니다. 작은 모델로 바꾸면 훨씬 빨라집니다:  findr setup --lite", flush=True)
    con.commit(); print(f"FINDR_EMBED {len(rows)} chunks {time.time() - t0:.0f}s (computed {len(uniq)}, identical skipped {len(rows) - len(uniq)}"
                        + (f", later pieces left for a deep run {later})" if later else ")"))


def user_idle_seconds():
    """seconds since the person last used the keyboard or mouse; None when this PC cannot tell"""
    try:
        if os.name == "nt":
            import ctypes

            class LII(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            li = LII(); li.cbSize = ctypes.sizeof(LII)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
                return None
            return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r'"HIDIdleTime" = (\d+)', out)
            return int(m.group(1)) / 1e9 if m else None
        out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) / 1000 if out.isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


LOOK_EVERY = 5   # seconds between looks at the keyboard and mouse during an idle-time run


def person_is_back(within):
    """the app's idle-time runs give the PC back: true when the keyboard or mouse was used in the last `within` seconds"""
    if not within:
        return False
    idle = user_idle_seconds()
    return idle is not None and idle < within


# office documents: their first pieces get the larger model's vector before code, logs and data files
UPGRADE_DOCS = ("pdf", "hwp", "hwpx", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "key", "pages", "numbers", "md", "txt")
FIRST_DONE = 6   # upgrade(first_only=True): every first piece has its side vector (0-2 are the full upgrade's, 3-5 the CLI's)


def upgrade(con, target, minutes=50, bs=None, first_only=False):
    """compute `target` vectors next to the current ones (column emb2), at most `minutes` per run; when all are there,
    switch in one transaction (emb <- emb2, meta encoder <- target). Returns 0 switched / 2 more runs needed / 1 nothing to do.
    Each file's first piece (n=0) comes first, office documents before the rest, newest first: the search engine can use
    those alone as a second meaning lane (LANE2 in search.py) long before the switch. On 4,771 real files the answer was
    among the first 10 candidates for 33 of 45 questions with that lane, 22 without (2,000 virtual questions: 1,850, 1,530).
    first_only: stop there, without switching, for a PC where the whole upgrade is too slow; returns FIRST_DONE (6) when
    every first piece is there, 2 when more runs are needed. Its progress goes to lane2.json: the hours left of a
    first-pieces run must not be read as the whole upgrade's (the CLI turns the upgrade off on that number)."""
    from enc_eg2 import encoder_of, make_encoder
    import torch
    cur = encoder_of(con)
    if cur == target:
        print(f"FINDR_UPGRADE done already ({target})", flush=True); return 1
    if "emb2" not in [r[1] for r in con.execute("PRAGMA table_info(chunks)")]:
        con.execute("ALTER TABLE chunks ADD COLUMN emb2 BLOB")
    mark = con.execute("SELECT value FROM meta WHERE key='upgrade'").fetchone()
    if not mark or mark[0] != target:   # a new target (or none yet): side vectors of another encoder are not reused
        con.execute("UPDATE chunks SET emb2=NULL"); con.execute("INSERT OR REPLACE INTO meta VALUES('upgrade', ?)", (target,))
    con.commit()
    if os.environ.get("FINDR_THREADS"):
        torch.set_num_threads(max(1, int(os.environ["FINDR_THREADS"])))
    enc = make_encoder(target)
    if bs:
        enc.bs = bs
    elif getattr(enc, "dev", "cpu") == "cpu":
        enc.bs = int(os.environ.get("FINDR_BATCH") or 8)   # small batches on the CPU: memory stays low, each run stops on time
    t0 = time.time(); stop = t0 + minutes * 60; did = 0
    scope = " AND n=0" if first_only else ""   # what this run counts and fills
    pfile = "lane2.json" if first_only else "upgrade.json"
    docs = ",".join(f"'.{e}'" for e in UPGRADE_DOCS)
    total = con.execute(f"SELECT count(*) FROM chunks WHERE emb IS NOT NULL{scope}").fetchone()[0]
    # the app's idle-time run stops when the person comes back (FINDR_STOP_ON_INPUT = seconds of recent input that count):
    # on an 8 GB laptop a 10-minute run kept 2.7 GB and half the cores while the person searched. Smaller steps so it notices soon.
    back = float(os.environ.get("FINDR_STOP_ON_INPUT") or 0)
    step = 16 if back else 64
    last = {k: v for k, v in _last_upgrade().items() if k in ("rate", "hours_left")}   # read now: progress below rewrites the file
    looked = 0.0
    while True:
        # first pieces first (documents, then the rest, newest first); after them the order used before (cards and second
        # pieces, then the deeper ones, newest file first)
        rows = con.execute(f"""SELECT c.id, c.text, f.path FROM chunks c JOIN files f ON f.id=c.file
                               WHERE c.emb IS NOT NULL AND c.emb2 IS NULL{' AND c.n=0' if first_only else ''}
                               ORDER BY (c.n != 0), (c.n = 0 AND lower(coalesce(f.ext, '')) NOT IN ({docs})), (c.n > 1), f.mtime DESC, c.n
                               LIMIT ?""", (step,)).fetchall()
        if not rows:
            break
        returned = False
        if back and time.time() - looked >= LOOK_EVERY:   # not every step: on a fast GPU 16 pieces take a fraction of a second
            looked = time.time(); returned = person_is_back(back)
        if time.time() > stop or returned:
            left = con.execute(f"SELECT count(*) FROM chunks WHERE emb IS NOT NULL AND emb2 IS NULL{scope}").fetchone()[0]
            rate = did / max(time.time() - t0, 1e-6)
            if returned:   # a run cut short (often while the model was still loading) says nothing about the speed: keep the
                # last run's estimate. Measured here, 0 pieces would read as 'thousands of hours' and turn the upgrade off for good
                print(f"FINDR_UPGRADE paused {total - left}/{total} | the person is back after {did} pieces", flush=True)
                progress_upgrade(target, total - left, total, file=pfile, back=True, **last); return 2
            print(f"FINDR_UPGRADE paused {total - left}/{total} | {rate:.1f} pieces/s | about {left / max(rate, 1e-6) / 3600:.1f} h of idle time left", flush=True)
            progress_upgrade(target, total - left, total, file=pfile, rate=round(rate, 2), hours_left=round(left / max(rate, 1e-6) / 3600, 1)); return 2
        with torch.no_grad():
            v = emb512(enc, [f"{label(Path(p))}\n{t}"[:1200] for _, t, p in rows]).cpu().numpy().astype(np.float16)
        con.executemany("UPDATE chunks SET emb2=? WHERE id=?", [(x.tobytes(), cid) for x, (cid, _, _) in zip(v, rows)])
        con.commit(); did += len(rows)
        if did % 640 < step:
            progress_upgrade(target, total - con.execute(f"SELECT count(*) FROM chunks WHERE emb IS NOT NULL AND emb2 IS NULL{scope}").fetchone()[0], total, file=pfile, **last)
    if first_only:   # the first pieces are there: the small model keeps answering, the second lane reads these
        progress_upgrade(target, total, total, file=pfile, first_only=True)
        print(f"FINDR_UPGRADE first pieces ready ({target}): {total} files, {did} this run in {time.time() - t0:.0f}s", flush=True)
        return FIRST_DONE
    with con:   # every vector has its counterpart: switch at once (search reloads when the recorded encoder changes)
        con.execute("UPDATE chunks SET emb=emb2 WHERE emb IS NOT NULL")
        con.execute("UPDATE chunks SET emb2=NULL")
        con.execute("INSERT OR REPLACE INTO meta VALUES('encoder', ?)", (target,))
        con.execute("DELETE FROM meta WHERE key='upgrade'")
    progress_upgrade(target, total, total, switched=True)
    print(f"FINDR_UPGRADE switched to {target}: {total} vectors in {time.time() - t0:.0f}s this run", flush=True)
    return 0


def _last_upgrade():
    d = os.environ.get("FINDR_DATA")
    try:
        return json.loads(Path(d, "upgrade.json").read_text(encoding="utf-8")) if d else {}
    except (OSError, ValueError):
        return {}


def progress_upgrade(target, done, total, file="upgrade.json", **kw):
    """the upgrade's own progress file (the indexing banner stays for indexing); a first-pieces run writes lane2.json"""
    d = os.environ.get("FINDR_DATA")
    if d:
        try:
            Path(d, file).write_text(json.dumps({"target": target, "done": done, "total": total, "t": time.time(), **kw}), encoding="utf-8")
        except OSError:
            pass


def emb512(enc, texts):
    if hasattr(enc, "st"):   # EmbeddingGemma 2: document prompt, 512 tokens
        return enc.emb(texts, query=False)
    import torch
    import torch.nn.functional as F
    b = enc.tok([enc.pre + t for t in texts], padding=True, truncation=True, max_length=512, return_tensors="pt").to(enc.dev)
    with torch.autocast(enc.dev, dtype=torch.bfloat16, enabled=enc.dev == "cuda"):
        h = enc.m(**b).last_hidden_state
    if enc.pool == "cls":
        v = h[:, 0]
    else:
        mk = b["attention_mask"].unsqueeze(-1).to(h.dtype); v = (h * mk).sum(1) / mk.sum(1)
    return F.normalize(v.float(), dim=-1)


if __name__ == "__main__":
    sys.exit(main() or 0)
