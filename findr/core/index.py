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
            return ooxml_text(p, limit)
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
    a = ap.parse_args()
    roots = [Path(r) for r in a.root]; Path(a.db).expanduser().parent.mkdir(parents=True, exist_ok=True)
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
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    if not con.execute("SELECT 1 FROM meta WHERE key='nfc'").fetchone():   # an index made before NFC: moved once (#12)
        moved = 0
        for fid, name, path in con.execute("SELECT id, name, path FROM files").fetchall():
            if nfc(name) == name and nfc(path) == path:
                continue
            con.execute("UPDATE files SET name=? WHERE id=?", (nfc(name), fid))
            for cid, text in con.execute("SELECT id, text FROM chunks WHERE file=?", (fid,)).fetchall():
                con.execute("DELETE FROM fts WHERE rowid=?", (cid,))
                con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(name), nfc(path), nfc(text or "")))
            con.execute("UPDATE chunks SET emb=NULL WHERE file=? AND n < 8", (fid,))   # the name is part of the meaning vector
            moved += 1
        con.execute("INSERT OR REPLACE INTO meta VALUES('nfc', '1')"); con.commit()
        if moved:
            print(f"FINDR_NFC {moved} files with decomposed (NFD) names moved to NFC", flush=True)
    if a.model in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") and not con.execute("SELECT 1 FROM chunks WHERE emb IS NOT NULL LIMIT 1").fetchone():
        con.execute("INSERT OR REPLACE INTO meta VALUES('encoder', ?)", (a.model,))   # known from the start: search works while indexing (#9)
    excl = {nfc(Path(x).as_posix()).rstrip("/") for x in os.environ.get("FINDR_EXCLUDE", "").split(os.pathsep) if x}
    if excl:   # folders this PC chose not to search (exclude.txt): files already indexed under them leave the index
        ids = [i for i, raw in con.execute("SELECT id, path FROM files") if (s := nfc(raw)) in excl or any(s.startswith(x + "/") for x in excl)]
        for fid in ids:
            drop(con, fid)
        if ids:
            print(f"FINDR_EXCLUDE removed {len(ids)} files", flush=True)
    con.commit()
    progress("names", 0, len(roots))
    for k, root in enumerate(roots):   # stage 1: names and folders of every chosen folder first (seconds)
        print("FINDR_ROOT " + json.dumps({"i": k, "n": len(roots), "path": str(root)}, ensure_ascii=False), flush=True)
        seen, t0, new = set(), time.time(), 0
        pre = root.as_posix().rstrip("/") + "/"   # several roots share one db: only this root's files can be "gone"
        known = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute("SELECT path,size,mtime,id,tier FROM files WHERE path LIKE ?", (pre + "%",))}
        for dp, dns, fns in os.walk(root):
            if not a.with_libs and APP_MARK & set(dns):
                dns[:] = []; continue   # an installed application bundle (ships its own Python): not the user's files
            dns[:] = [d for d in dns if d not in (SKIP_DIR if a.with_libs else SKIP_DIR | LIB_DIRS)]
            if os.environ.get("FINDR_SCOPE") == "user":   # whole-PC mode: no system, program, cache or library folders
                from scope import skip_dir, app_profile
                if app_profile(fns):
                    dns[:] = []; continue   # an app's embedded-browser profile (CapCut's CEF folder in ~/Movies)
                rel = "/" if Path(dp) == root else ""
                dns[:] = [d for d in dns if not skip_dir(rel, d)]
            if excl:
                dns[:] = [d for d in dns if nfc((Path(dp) / d).as_posix()) not in excl]
            for fn in fns:
                if not a.with_libs and JUNK.search(fn):
                    continue
                p = Path(dp) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not _stat.S_ISREG(st.st_mode):
                    continue   # sockets, pipes, devices: nothing to read (CEF's SingletonSocket failed here)
                s = p.as_posix(); seen.add(s)
                if s in known and known[s][0] == st.st_size and abs(known[s][1] - st.st_mtime) < 1 and known[s][3] == tier(p):
                    continue   # unchanged (a changed tier rule re-reads the file)
                if s in known:   # changed: drop old rows
                    fid = known[s][2]
                    drop(con, fid)
                tr = tier(p)
                if tr in (1, 2) and cloud_only(st):
                    tr = 3   # OneDrive 'online-only': reading it would download it; found by name until it is on the PC
                cur = con.execute("INSERT INTO files(path,name,ext,size,mtime,tier,pending) VALUES(?,?,?,?,?,?,?)",
                                  (s, nfc(p.name), real_ext(p), st.st_size, st.st_mtime, tr, 1 if tr in (1, 2) else 0))
                fid = cur.lastrowid   # name and folder searchable at once; content comes in its wave
                cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, 0, "")).lastrowid
                con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(p.name), nfc(s[len(root.as_posix()):]), ""))
                new += 1
                if new % 5000 == 0:
                    con.commit(); print(f"  {new} files {time.time() - t0:.0f}s", flush=True)
        gone = [k for k in known if k not in seen]
        for s in gone:
            drop(con, known[s][2])
        con.commit()
        print(f"FINDR_SCAN new/changed {new} removed {len(gone)} in {time.time() - t0:.0f}s")
        progress("names", k + 1, len(roots))
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


def progress(stage, done, total, **kw):
    """where the index stands, for the search page ('색인 진행 중 …'); written to the data folder"""
    d = os.environ.get("FINDR_DATA")
    if not d:
        return
    try:
        Path(d, "progress.json").write_text(json.dumps({"stage": stage, "done": done, "total": total, "t": time.time(), **kw}), encoding="utf-8")
    except OSError:
        pass


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
    locked = 0
    for i, ((fid, s, ext, mtime, tr), txt) in enumerate(zip(rows, read_all(jobs))):
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
        con.execute("UPDATE files SET pending=0 WHERE id=?", (fid,))
        if (i + 1) % 200 == 0:
            con.commit(); print(f"  read {i + 1}/{len(rows)} {time.time() - t0:.0f}s", flush=True)
            progress("content", i + 1, len(rows), folder=root.name)
    con.commit()
    left = con.execute("SELECT count(*) FROM files WHERE pending=1 AND path LIKE ?", (pre + "%",)).fetchone()[0]
    print(f"FINDR_READ {len(rows)} files in {time.time() - t0:.0f}s, still pending {left}" + (f" (in use by another program: {locked})" if locked else ""))


SHOTLIKE = re.compile(r"screenshot|screen ?shot|스크린샷|캡처|캡쳐|화면 ?캡|capture|snip|clip|scan|스캔|영수증|receipt", re.I)
_OCR = None


OCR_SIDE = int(os.environ.get("FINDR_OCR_SIDE") or 2400)   # longest side of a page image given to OCR (pixels)


def ocr_engine():
    """'vision' (macOS built-in, Korean in accurate mode) or 'easyocr'. FINDR_OCR_ENGINE overrides.
    On an M3, 6 Korean slide pages: Vision 0.6 s/page, EasyOCR on the Apple GPU 4.3 s, on the CPU 22.8 s; Vision also misread less."""
    want = (os.environ.get("FINDR_OCR_ENGINE") or "").lower()
    if want in ("vision", "easyocr"):
        return want
    if sys.platform == "darwin":
        try:
            import Vision  # noqa: F401  (pyobjc-framework-Vision)
            return "vision"
        except ImportError:
            pass
    return "easyocr"


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
    req.setRecognitionLanguages_(["ko-KR", "en-US"]); req.setUsesLanguageCorrection_(True)
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
    except Exception:
        pass   # Vision failed on this image: EasyOCR below
    try:
        return [t for t in ocr_reader().readtext(img, detail=0, paragraph=True) if t.strip()]
    except Exception:
        return []


def _page_scale(page, side=None):
    """2x for normal pages, less for big ones so the image's longest side stays near OCR_SIDE (a 3670-pt slide was 7339 px)"""
    w, h = page.get_size()
    return max(0.5, min(2.0, (side or OCR_SIDE) / max(w, h, 1)))


def ocr_wave(con, root, first_pages=10, deep_pages=50):
    """scanned PDFs and screenshots now; every photo on a deep run. Each file is read once (files.ocr)."""
    if ocr_engine() != "vision":
        try:
            import easyocr  # noqa: F401
        except ImportError:
            return
    if "ocr" not in [r[1] for r in con.execute("PRAGMA table_info(files)")]:
        con.execute("ALTER TABLE files ADD COLUMN ocr INT DEFAULT 0")
    deep = int(os.environ.get("FINDR_FIRST_PIECES") or 0) == 0
    try:   # without a GPU, reading a scan is slow: the first pass takes the first pages, a deep run the rest
        import torch
        if not (torch.cuda.is_available() or (getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available())):
            first_pages = min(first_pages, 3)
    except Exception:
        pass
    pre = root.as_posix().rstrip("/") + "/"
    pdfs = [r for r in con.execute("""SELECT f.id, f.path FROM files f WHERE f.ext='.pdf' AND f.tier=1 AND COALESCE(f.ocr,0)=0
              AND f.path LIKE ? AND (SELECT COALESCE(sum(length(c.text)),0) FROM chunks c WHERE c.file=f.id) < 200
              ORDER BY f.mtime DESC""", (pre + "%",))]
    imgs = [(i, s) for i, s, e in con.execute("SELECT id, path, ext FROM files WHERE tier=3 AND COALESCE(ocr,0)=0 AND path LIKE ? AND size < 30000000 "
                                              "ORDER BY mtime DESC", (pre + "%",)) if (e or "").lower() in PHOTO_EXT and (deep or SHOTLIKE.search(s))]
    if not pdfs and not imgs:
        return
    t0, got, chars = time.time(), 0, 0
    heif_on()
    for fid, s in pdfs + imgs:
        lines = []
        if s.lower().endswith(".pdf"):
            try:
                import numpy as _np, pypdfium2 as pdfium
                doc = pdfium.PdfDocument(s); end = time.time() + 3 * FILE_SECONDS   # reading a scan is slower than reading text
                for i in range(min(len(doc), deep_pages if deep else first_pages)):
                    if time.time() > end:
                        break
                    pg = doc[i]
                    lines += ocr_lines(_np.array(pg.render(scale=_page_scale(pg)).to_pil().convert("RGB")))
                doc.close()
            except Exception:
                pass
        else:
            try:
                from PIL import Image
                import numpy as _np
                im = Image.open(s).convert("RGB"); im.thumbnail((2400, 2400))
                lines = ocr_lines(_np.array(im))
            except Exception:
                lines = []
        txt = "\n".join(lines)
        if len(re.sub(r"\s", "", txt)) >= 8:   # real text, not a stray mark
            p_ = Path(s); rel = s[len(root.as_posix()):]
            con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file=? AND n>=0)", (fid,))
            con.execute("DELETE FROM chunks WHERE file=? AND n>=0", (fid,))
            for n, c in enumerate(chunks(txt)[:2000] or [""]):
                cid = con.execute("INSERT INTO chunks(file,n,text) VALUES(?,?,?)", (fid, n, c)).lastrowid
                con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(p_.name), nfc(rel), nfc(c)))
            got += 1; chars += len(txt)
        con.execute("UPDATE files SET ocr=1 WHERE id=?", (fid,))
        con.commit()   # each file: progress is visible and an interrupted run keeps what it read
    con.commit()
    print(f"FINDR_OCR {len(pdfs)} scanned PDFs + {len(imgs)} images read, text found in {got} ({chars:,} chars) {time.time() - t0:.0f}s", flush=True)


def remask(con):
    """every run: any stored chunk that still holds a key-like value is masked, re-indexed and re-embedded"""
    hit = 0
    for cid, fid, txt in con.execute("SELECT id, file, text FROM chunks").fetchall():
        if txt and KEYLIKE.search(txt):
            t = KEYLIKE.sub("[가림]", txt); name, path = con.execute("SELECT name, path FROM files WHERE id=?", (fid,)).fetchone()
            con.execute("DELETE FROM fts WHERE rowid=?", (cid,))
            con.execute("INSERT INTO fts(rowid,name,path,text) VALUES(?,?,?,?)", (cid, nfc(name), nfc(path), t))
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


def ooxml_text(p, limit):
    """Word, Excel, PowerPoint, HWPX: text parts read in pieces, stopping once `limit` characters are in.
    A large workbook can expand to gigabytes of XML; reading it whole used to take all the memory and time."""
    import zipfile
    keys = ("word/document", "sharedStrings", "worksheets/", "slides/slide", "Contents/section")
    def order(n):   # body first; slides and sections in their own order (slide2 before slide10)
        m = re.search(r"(\d+)\.xml$", n)
        return (0 if ("document" in n or "sharedStrings" in n) else 1, n.rsplit("/", 1)[0], int(m.group(1)) if m else 0)
    z = zipfile.ZipFile(p); out, have = [], 0; end = time.time() + FILE_SECONDS
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
        for nm in sorted(names, key=lambda x: (not x.startswith("Index/Slide"), x)):   # slides first
            if time.time() > end:
                break
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
        return subprocess.run(["pdftotext", "-layout", "-l", "500", str(p), "-"], capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")[:limit]
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
            return "\n".join(out)[:limit]
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


def embed(con, model):
    Enc = None
    from enc_eg2 import EG2, SmallEnc, encoder_of
    import torch
    from enc_eg2 import make_encoder
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    if model in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") and not con.execute("SELECT 1 FROM chunks WHERE emb IS NOT NULL LIMIT 1").fetchone():
        con.execute("INSERT OR REPLACE INTO meta VALUES('encoder', ?)", (model,)); con.commit()   # a new index records its encoder
    enc = make_encoder(encoder_of(con)) if encoder_of(con) in ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta") else Enc(model)   # same encoder as the vectors already stored
    if not isinstance(enc, EG2):
        enc.tok.model_max_length = 512
    if os.environ.get("FINDR_THREADS"):   # background indexing: leave CPU cores for the person's own work
        torch.set_num_threads(max(1, int(os.environ["FINDR_THREADS"])))
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
    t0 = last = time.time(); warned = False
    for i in range(0, len(uniq), 256):
        b = uniq[i:i + 256]
        with torch.no_grad():
            v = emb512(enc, [k for k, _ in b]).cpu().numpy().astype(np.float16)
        con.executemany("UPDATE chunks SET emb=? WHERE id=?", [(x.tobytes(), cid) for x, (_, cids) in zip(v, b) for cid in cids])
        done_ = i + len(b)
        if done_ % 2560 < 256 or time.time() - last > 30 or done_ == len(uniq):   # saved and reported often: progress is visible
            con.commit(); last = time.time()
            rate = done_ / max(last - t0, 1e-6); left = (len(uniq) - done_) / max(rate, 1e-6)
            print(f"  embedded {done_}/{len(uniq)} {last - t0:.0f}s | {rate:.0f} pieces/s | about {left / 60:.0f} min left", flush=True)
            progress("meaning", done_, len(uniq), minutes_left=round(left / 60))
            if not warned and done_ >= 1024 and left > 6 * 3600 and isinstance(enc, EG2) and not isinstance(enc, SmallEnc):
                warned = True
                print("FINDR_SLOW 이 PC에서는 큰 모델이 느립니다. 작은 모델로 바꾸면 훨씬 빨라집니다:  findr setup --lite", flush=True)
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
