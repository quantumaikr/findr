"""findr index (2026-11-03): natural-language search over every file under a root, all local.

Tiers (by what a person looks for):
  1 full text  : the user's own documents and code (pdf, md, txt, html, py, json, logs ...) and application code
  2 head only  : bundled libraries (site-packages, python_embeded ...): first ~1500 chars, one chunk
  3 name only  : media, models, binaries, archives: path, name, size, date
Secrets (.env, keys, credentials) are listed by name only when allowed, never read.
    python index.py --root FOLDER --db INDEX.db
Re-running only re-reads files whose size or mtime changed.
"""
import argparse, datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

TEXT = {".md", ".txt", ".py", ".pyi", ".js", ".ts", ".jsx", ".tsx", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".csv", ".tsv", ".log", ".html", ".htm", ".css", ".sh", ".ps1", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp",
        ".cu", ".cuh", ".java", ".go", ".rs", ".sql", ".xml", ".rst", ".ipynb", ".jinja", ".tex", ".r", ".m", ".lua", ""}
DOC = {".pdf", ".docx", ".xlsx", ".pptx", ".hwpx", ".hwp", ".doc", ".ppt", ".xls", ".key"}
AUD = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".wma", ".ogg"}   # transcribed locally by stt_cache.py (faster-whisper)
OLD = {".hwp", ".doc", ".ppt", ".xls"}   # old binary formats: converted locally by LibreOffice (+ H2Orestart for hwp), cached
SECRET = re.compile(r"^(\.env(\..*)?|\.netrc|\.pgpass|\.git-credentials|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|credentials(\.json|\.ya?ml|\.txt)?"
                    r"|secrets?\.(json|ya?ml|toml|txt|env)|.*\.(pem|pfx|p12|kdbx|keystore|jks))$", re.I)
# '.key' is both a cryptographic key and an Apple Keynote deck: Keynote is a ZIP, a key file is not (see is_secret)
# values that look like keys are masked inside indexed text (a key pasted into a note or log never reaches the index)
KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}"
                     r"|xox[abpr]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
                     r"|(?i:(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?)[^\s'\"]{12,})")
LIB = re.compile(r"/(site-packages|python_embeded|node_modules|__pycache__|\.git|dist-packages|\.venv|venv)(/|$)")
SKIP_DIR = {".git", "__pycache__", "node_modules"}
# 2026-11-05 (user's decision): not the user's files -> not indexed by default (--with-libs keeps the old behaviour)
LIB_DIRS = {"site-packages", "python_embeded", "node_modules", "dist-packages", ".venv", "venv", "__pycache__", ".git",
            "dist", ".cache", ".next", ".pytest_cache", ".mypy_cache", ".tox"}
APP_MARK = {"python_embeded", "python_embedded"}
JUNK = re.compile(r"^(vocab\.json|merges\.txt|tokenizer(_config)?\.json|special_tokens_map\.json|vocab\.txt)$|(\.map|\.pyc|\.pyo|\.min\.js|\.min\.css)$|-[A-Za-z0-9_]{8}\.(js|css)$|^~\$|^(Thumbs\.db|\.DS_Store|desktop\.ini)$", re.I)
# backups keep the user's history ("bos 넣기 전 학습 스크립트"): read them as the kind of file they back up
BAK = re.compile(r"(\.(bak\w*|old|orig|backup|swp|tmp)|~)$", re.I)
MAXREAD = 2_000_000   # bytes of text read per tier-1 file
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
except ImportError:
    from officetext import sniff_text, doc_text, hwp_text


def extract(p: Path, limit):
    ext = real_ext(p)
    try:
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
            import zipfile
            z = zipfile.ZipFile(p); parts = []
            for n in z.namelist():
                if n.endswith(".xml") and any(k in n for k in ("word/document", "sharedStrings", "worksheets/", "slides/slide", "Contents/section")):
                    parts.append(re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "ignore")))
            return re.sub(r"\s+", " ", " ".join(parts))[:limit]
        raw = open(p, "rb").read(limit)
        if b"\x00" in raw[:4096]:
            return ""
        t = raw.decode("utf-8", "ignore")
        if ext in (".html", ".htm"):
            t = re.sub(r"(?s)<(script|style).*?</\1>", " ", t); t = re.sub(r"<[^>]+>", " ", t)
        if ext == ".ipynb":
            try:
                t = "\n".join("".join(c.get("source", "")) for c in json.loads(t).get("cells", []))
            except Exception:
                pass
        return t
    except Exception as e:
        print("  extract fail", p, type(e).__name__, file=sys.stderr)
        return ""


def transcript(p: Path):
    """the cached local transcript of an audio file, or None when it has not been transcribed yet"""
    import hashlib
    st = p.stat()
    c = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "conv" / (hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest() + ".txt")
    return c.read_text(encoding="utf-8", errors="ignore") if c.exists() else None


def convert_old(p: Path):
    """hwp/doc/ppt/xls -> text with a local LibreOffice; cached by path, size and mtime (re-runs cost nothing)"""
    import hashlib
    import shutil
    import tempfile
    st = p.stat()
    key = hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()
    cache = Path(os.environ.get("FINDR_DATA") or (Path.home() / ".findr")) / "conv" / (key + ".txt")
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="ignore")
    cache.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower(); txt = ""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / ("in" + ext); shutil.copyfile(p, src)
        target = "pdf" if ext in (".ppt", ".xls") else "txt:Text"
        try:
            subprocess.run(["soffice", "--headless", "--convert-to", target, "--outdir", td, str(src)], capture_output=True, timeout=120)
            if target == "pdf" and (Path(td) / "in.pdf").exists():
                txt = subprocess.run(["pdftotext", "-layout", str(Path(td) / "in.pdf"), "-"], capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")
            elif (Path(td) / "in.txt").exists():
                txt = (Path(td) / "in.txt").read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print("  convert fail", p.name, type(e).__name__, file=sys.stderr)
    cache.write_text(txt, encoding="utf-8")
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


def label(p: Path):   # what the encoder sees besides the text: names carry meaning ("LoRA_실험_요약.pdf")
    return f"{p.name} | {'/'.join(p.parts[-4:-1])}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True); ap.add_argument("--db", required=True)
    ap.add_argument("--model", default="embeddinggemma-2"); ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--with-libs", action="store_true", help="also index bundled libraries and build output (head only)")
    # 2026-11-05: newest and most useful first. The scan registers every file by name at once; content is read in waves.
    ap.add_argument("--max-age-days", type=float, default=0, help="this wave reads content only of files changed within N days (0 = all)")
    ap.add_argument("--limit", type=int, default=0, help="read content of at most N pending files in this run (0 = no limit)")
    a = ap.parse_args()
    root = Path(a.root); Path(a.db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(Path(a.db).expanduser(), timeout=60)
    con.execute("PRAGMA journal_mode=WAL")   # the search server keeps reading while the index is updated
    con.executescript("""
      CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, ext TEXT, size INT, mtime REAL, tier INT);
      CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, file INT, n INT, text TEXT, emb BLOB);
      CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, path, text, content='', contentless_delete=1, tokenize='trigram');
      CREATE INDEX IF NOT EXISTS ch_file ON chunks(file);
      CREATE TABLE IF NOT EXISTS cards(file INTEGER PRIMARY KEY, outline TEXT, summary TEXT, done INT DEFAULT 0);""")
    if "pending" not in [r[1] for r in con.execute("PRAGMA table_info(files)")]:
        con.execute("ALTER TABLE files ADD COLUMN pending INT DEFAULT 0")
    seen, t0, new = set(), time.time(), 0
    pre = root.as_posix().rstrip("/") + "/"   # several roots share one db: only this root's files can be "gone"
    known = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute("SELECT path,size,mtime,id,tier FROM files WHERE path LIKE ?", (pre + "%",))}
    for dp, dns, fns in os.walk(root):
        if not a.with_libs and APP_MARK & set(dns):
            dns[:] = []; continue   # an installed application bundle (ships its own Python): not the user's files
        dns[:] = [d for d in dns if d not in (SKIP_DIR if a.with_libs else SKIP_DIR | LIB_DIRS)]
        if os.environ.get("FINDR_SCOPE") == "user":   # whole-PC mode: no system, program, cache or library folders
            from scope import skip_dir
            rel = "/" if Path(dp) == root else ""
            dns[:] = [d for d in dns if not skip_dir(rel, d)]
        for fn in fns:
            if not a.with_libs and JUNK.search(fn):
                continue
            p = Path(dp) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            s = p.as_posix(); seen.add(s)
            if s in known and known[s][0] == st.st_size and abs(known[s][1] - st.st_mtime) < 1 and known[s][3] == tier(p):
                continue   # unchanged (a changed tier rule re-reads the file)
            if s in known:   # changed: drop old rows
                fid = known[s][2]
                drop(con, fid)
            tr = tier(p)
            cur = con.execute("INSERT INTO files(path,name,ext,size,mtime,tier,pending) VALUES(?,?,?,?,?,?,?)",
                              (s, p.name, real_ext(p), st.st_size, st.st_mtime, tr, 1 if tr in (1, 2) else 0))
            fid = cur.lastrowid   # name and folder searchable at once; content comes in its wave
            cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, 0, "")).lastrowid
            con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, p.name, s[len(root.as_posix()):], ""))
            new += 1
            if new % 5000 == 0:
                con.commit(); print(f"  {new} files {time.time() - t0:.0f}s", flush=True)
    gone = [k for k in known if k not in seen]
    for s in gone:
        drop(con, known[s][2])
    con.commit()
    print(f"FINDR_SCAN new/changed {new} removed {len(gone)} in {time.time() - t0:.0f}s")
    read_wave(con, root, a)
    remask(con)
    if not a.no_embed:
        embed(con, a.model)
    if os.environ.get("PHOTO_URL"):   # photos: one meaning vector each from the local photo model (no request seen)
        embed_photos(con, os.environ["PHOTO_URL"])
    print("FINDR_STATS", dict(con.execute("SELECT tier,count(*) FROM files GROUP BY tier").fetchall()),
          "chunks", con.execute("SELECT count(*) FROM chunks").fetchone()[0])


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


def _read_one(job):
    s, lim = job
    try:
        return extract(Path(s), lim) or ""
    except Exception:
        return ""


def read_all(jobs):
    """text of each (path, limit) in order; several worker processes when there is enough to read (FINDR_READ_WORKERS)"""
    n = int(os.environ.get("FINDR_READ_WORKERS") or max(1, min(8, (os.cpu_count() or 2) - 1)))
    if n <= 1 or len(jobs) < 16:
        yield from map(_read_one, jobs); return
    from concurrent.futures import ProcessPoolExecutor
    try:
        with ProcessPoolExecutor(max_workers=n) as ex:
            yield from ex.map(_read_one, jobs, chunksize=4)   # results come back in the given (priority) order
    except Exception:   # a platform without working worker processes: read in this process
        yield from map(_read_one, jobs)


def read_wave(con, root, a):
    """read the content of pending files under this root, best first, within this wave's age and size limits"""
    now = time.time(); pre = root.as_posix().rstrip("/") + "/"
    rows = con.execute("SELECT id, path, ext, mtime, tier FROM files WHERE pending=1 AND path LIKE ?", (pre + "%",)).fetchall()
    if a.max_age_days:
        rows = [r for r in rows if now - r[3] <= a.max_age_days * 86400]
    rows.sort(key=lambda r: -priority(r[1], r[2], r[3], now))
    if a.limit:
        rows = rows[: a.limit]
    t0 = time.time()
    jobs = [(s, 1500 if tr == 2 else DATA_READ if ext in DATA else MAXREAD) for _, s, ext, _, tr in rows]
    for i, ((fid, s, ext, mtime, tr), txt) in enumerate(zip(rows, read_all(jobs))):
        p = Path(s)
        cs = chunks(txt)[: (1 if tr == 2 else 2000)] or [""]
        con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file=? AND n>=0)", (fid,))
        con.execute("DELETE FROM chunks WHERE file=? AND n>=0", (fid,))
        rel = s[len(root.as_posix()):]
        for n, c in enumerate(cs):
            cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, n, c)).lastrowid
            con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, p.name, rel, c))
        con.execute("UPDATE files SET pending=0 WHERE id=?", (fid,))
        if (i + 1) % 500 == 0:
            con.commit(); print(f"  read {i + 1}/{len(rows)} {time.time() - t0:.0f}s", flush=True)
    con.commit()
    left = con.execute("SELECT count(*) FROM files WHERE pending=1 AND path LIKE ?", (pre + "%",)).fetchone()[0]
    print(f"FINDR_READ {len(rows)} files in {time.time() - t0:.0f}s, still pending {left}")


def remask(con):
    """every run: any stored chunk that still holds a key-like value is masked, re-indexed and re-embedded"""
    hit = 0
    for cid, fid, txt in con.execute("SELECT id, file, text FROM chunks").fetchall():
        if txt and KEYLIKE.search(txt):
            t = KEYLIKE.sub("[가림]", txt); name, path = con.execute("SELECT name, path FROM files WHERE id=?", (fid,)).fetchone()
            con.execute("DELETE FROM fts WHERE rowid=?", (cid,))
            con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, name, path, t))
            con.execute("UPDATE chunks SET text=?, emb=NULL WHERE id=?", (t, cid)); hit += 1
    con.commit(); print(f"FINDR_MASK {hit} chunks masked")


PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}


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
    rows = [(i, p) for i, p, e in con.execute("SELECT id, path, ext FROM files WHERE id NOT IN (SELECT file FROM photo_emb) ORDER BY mtime DESC")
            if (e or "").lower() in PHOTO_EXT]
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
        seen, out, size = set(), [], 0
        for nm in sorted(names, key=lambda x: (not x.startswith("Index/Slide"), x)):   # slides first
            if not (nm.startswith("Index/") and nm.endswith(".iwa")) or z.getinfo(nm).file_size > 50_000_000:
                continue
            for t in _pb_strings(_iwa(z.read(nm))):
                t = t.strip()
                if len(_WORDY.findall(t)) < 2 or _STYLEID.search(t):   # style ids, symbols: not what people search for
                    continue
                if t and t not in seen:
                    seen.add(t); out.append(t); size += len(t)
            if size >= limit:
                break
        return "\n".join(out)[:limit]
    except Exception:
        return ""


def pdf_text(p, limit):
    """pdftotext when it is installed (Linux; best layout), else pypdf (Windows, macOS: nothing extra to install)"""
    import shutil
    if shutil.which("pdftotext") and os.environ.get("FINDR_PDF") in (None, "", "pdftotext"):
        return subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")[:limit]
    if os.environ.get("FINDR_PDF") != "pypdf":
        try:
            import pypdfium2 as pdfium   # Chrome's PDF engine: much faster than pure-Python parsing
            doc = pdfium.PdfDocument(str(p)); out, n = [], 0
            try:
                for i in range(len(doc)):
                    pg = doc[i]; tp = pg.get_textpage(); t = tp.get_text_range() or ""; tp.close(); pg.close()
                    out.append(t); n += len(t)
                    if n >= limit:
                        break
            finally:
                doc.close()
            return "\n".join(out)[:limit]
        except ImportError:
            pass
        except Exception:
            return ""
    try:
        from pypdf import PdfReader
        out, n = [], 0
        for pg in PdfReader(str(p)).pages:
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


def embed(con, model):
    Enc = None
    from enc_eg2 import EG2, encoder_of
    import torch
    from enc_eg2 import make_encoder
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    if model in ("embeddinggemma-2", "e5-small") and not con.execute("SELECT 1 FROM chunks WHERE emb IS NOT NULL LIMIT 1").fetchone():
        con.execute("INSERT OR REPLACE INTO meta VALUES('encoder', ?)", (model,)); con.commit()   # a new index records its encoder
    enc = make_encoder(encoder_of(con)) if encoder_of(con) in ("embeddinggemma-2", "e5-small") else Enc(model)   # same encoder as the vectors already stored
    if not isinstance(enc, EG2):
        enc.tok.model_max_length = 512
    cap = int(os.environ.get("FINDR_FIRST_PIECES") or 0)   # first pass on a large PC: each file's opening pieces only
    rows = con.execute(f"""SELECT c.id, c.text, f.path FROM chunks c JOIN files f ON f.id=c.file
                          WHERE c.emb IS NULL AND f.tier>0 AND COALESCE(f.pending,0)=0 {"AND c.n < %d" % cap if cap > 0 else ""}
                          ORDER BY (c.n > 1), f.mtime DESC, c.n""").fetchall()   # every file's opening pieces first, newest first
    later = con.execute("SELECT count(*) FROM chunks c JOIN files f ON f.id=c.file WHERE c.emb IS NULL AND f.tier>0 "
                        "AND COALESCE(f.pending,0)=0 AND c.n >= ?", (cap,)).fetchone()[0] if cap > 0 else 0
    # identical inputs (a copied file, repeated boilerplate under the same name) are computed once
    groups = {}
    for cid, t, p in rows:
        groups.setdefault(f"{label(Path(p))}\n{t}"[:1200], []).append(cid)
    uniq = list(groups.items())
    t0 = time.time()
    for i in range(0, len(uniq), 256):
        b = uniq[i:i + 256]
        with torch.no_grad():
            v = emb512(enc, [k for k, _ in b]).cpu().numpy().astype(np.float16)
        con.executemany("UPDATE chunks SET emb=? WHERE id=?", [(x.tobytes(), cid) for x, (_, cids) in zip(v, b) for cid in cids])
        if i % 25600 == 0:
            con.commit(); print(f"  embedded {i + len(b)}/{len(uniq)} {time.time() - t0:.0f}s", flush=True)
    con.commit(); print(f"FINDR_EMBED {len(rows)} chunks {time.time() - t0:.0f}s (computed {len(uniq)}, identical skipped {len(rows) - len(uniq)}"
                        + (f", later pieces left for a deep run {later})" if later else ")"))


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
    main()
