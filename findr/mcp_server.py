"""findr for AI tools: an MCP server (stdio) so assistants on this PC (Claude Desktop, Claude Code and others) can find the
person's files through findr.

Tools
  search_files(query, limit, kind, since, until, folder)   the files most likely meant, with the odds, where they are and
                               a short excerpt; optionally only one kind of file, a period, or one folder
  read_file(path, max_chars)   the text findr already read from one indexed file; when the index has none, the file read now
                               (scan OCR with pages read of total, HEIC), why it cannot be read (HWP 3.x, password), docx pages
  list_folder(path, limit)     what one folder holds, newest first, and its subfolders: to look around the way a person
                               does once a search shows roughly where something is (a photo with no text, a project folder)
  versions(path)               the same document's other files: exact copies anywhere, other versions in its folder, the
                               same name in other folders, with dates, so the latest or the original can be picked
  file_relations(path, terms)  what ties one file to others: its characteristic words, a stable file id, the id of its version
                               family and the files with exactly the same content (#203)

The last two and the filters came from real-PC runs (11/9): agents with search_files alone needed 2-4 calls, and said a
kind filter, a folder view and a version view would have ended most of them in one.

It asks the running search engine when it is up (full quality: meaning, reranking, odds). Without it, it searches the
index directly by names and words. read_file returns only files in the index, from the text findr stored; it never opens
other paths. What the tools return goes to the assistant that called them, so the person's assistant settings decide
whether it leaves the PC.

Nothing under findr's data folder (paths.HOME), or under the folders in FINDR_MCP_DENY (separated by os.pathsep), is ever
named, listed or read by these tools, even when it is in the index: an app that embeds findr may keep its own files there.
Nor is a file the secret rules mark (keys, certificates, password exports: index.is_secret), checked at each answer, so a
rule added since the last index run holds at once. The tools answer for such a path exactly as for one that is not in the
index (#200)."""
import functools
import json
import os
import posixpath
import re
import sqlite3
import unicodedata
import urllib.parse
import urllib.request

from . import paths
from .lang import L, lang

ENGINE = f"http://127.0.0.1:{os.environ.get('FINDR_APP_PORT') or os.environ.get('FINDR_PORT') or 8096}"


def _key():
    try:
        return (paths.HOME / ".password").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_DOWN = [0.0]   # until when the engine is taken as not running


LOADING_RETRY = 5   # seconds before an engine that is still loading is asked again


def _engine_up():
    """a quick look before asking: on Windows a refused localhost connection takes ~2 s (it retries), so with the app
    closed every search_files call waited 2 s before the index search. Not up: look again after 15 s. An engine whose
    port is open but which is still loading its index and models (#198 opens the port first) is taken as down for
    LOADING_RETRY s: its /api would hold the call until it is loaded (minutes on a slow PC). /healthz?json=1 says which"""
    import socket
    import time
    if time.time() < _DOWN[0]:
        return False
    try:
        socket.create_connection(("127.0.0.1", int(ENGINE.rsplit(":", 1)[1])), timeout=0.3).close()
    except OSError:
        _DOWN[0] = time.time() + 15
        return False
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        raw = op.open(f"{ENGINE}/healthz?json=1", timeout=2).read()
    except Exception:   # noqa: BLE001  (open but not answering: busy or hung)
        _DOWN[0] = time.time() + LOADING_RETRY
        return False
    try:
        h = json.loads(raw)
    except ValueError:
        return True   # an older engine answers 'ok' only: it loads before it opens the port
    if isinstance(h, dict) and h.get("loaded") is False:
        _DOWN[0] = time.time() + LOADING_RETRY
        return False
    return True


def _engine_search(query, limit, ask=None):
    """results from the running engine, or None when it is not up. Results in the data folder or a FINDR_MCP_DENY folder
    are left out; when some were, the engine is asked once more for a few more, so the answer still holds `limit`"""
    if not _engine_up():
        return None
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    import base64
    k = _key()   # the page's password: as the app's cookie and as HTTP Basic (any user name), whichever the engine takes
    req = urllib.request.Request(f"{ENGINE}/api?" + urllib.parse.urlencode({"q": query, "k": ask or limit, **({"lang": "en"} if lang() == "en" else {})}),
                                 headers={"Cookie": f"findr_t={k}", "Authorization": "Basic " + base64.b64encode(f"findr:{k}".encode()).decode()})
    try:
        r = json.loads(op.open(req, timeout=60).read())
    except Exception:
        return None
    got, roots = r.get("results") or [], _deny_roots()
    kept = [x for x in got if not _denied(x.get("path"), roots)]
    if len(kept) < len(got) and len(kept) < limit and ask is None and len(got) >= limit:
        more = _engine_search(query, limit, ask=min(50, limit + 2 * (len(got) - len(kept)) + 5))
        if more is not None:
            return more
    out = []
    con = _db() if any(x.get("chunk_id") is not None for x in kept[:limit]) else None
    for x in kept[:limit]:
        out.append({"name": x.get("name"), "path": x.get("path"), "modified": x.get("date"),
                    "chance": round(x["prob"], 2) if x.get("prob") is not None else None,
                    "excerpt": _mask(x.get("snippet") or "")[:300], "excerpt_start": x.get("snippet_start"),
                    **_excerpt_place(con, x.get("chunk_id"), x.get("snippet_start"))})
    if con is not None:
        con.close()
    return {"results": out, "note": r.get("note") or "", "engine": "full"}


def _excerpt_place(con, cid, start):
    """{'excerpt_chunk': n, 'excerpt_offset': o}: excerpt_start counts from the start of one stored piece of the file (chunk
    n); excerpt_offset is the same place in read_file's text (the pieces in order, joined by a line break), so
    read_file(path, max_chars > excerpt_offset + len(excerpt)) holds it. {} when the engine did not say which piece"""
    if con is None or cid is None or start is None:
        return {}
    try:
        r = con.execute("SELECT file, n FROM chunks WHERE id=?", (int(cid),)).fetchone()
        if not r or r[1] < 0:
            return {}
        before = con.execute("SELECT coalesce(sum(length(text) + 1), 0) FROM chunks WHERE file=? AND n>=0 AND n<? AND text<>''",
                             (r[0], r[1])).fetchone()[0]
        return {"excerpt_chunk": r[1], "excerpt_offset": before + int(start)}
    except (sqlite3.Error, ValueError, TypeError):
        return {}


@functools.lru_cache(maxsize=65536)
def _real_dir(d):
    """a folder as the disk has it: links, junctions and Windows short names resolved. Kept per folder (a folder of a
    hundred files is resolved once), emptied at each tool call so a link changed meanwhile is seen"""
    try:
        return os.path.realpath(d)
    except (OSError, ValueError):
        return os.path.abspath(d)


def _canon(p):
    """one spelling for comparing paths: absolute, links resolved, '..' folded, NFC, '/' and case folded, macOS firmlinks
    (/System/Volumes/Data/...) taken off, Windows forms (\\\\?\\, \\\\localhost\\C$\\, trailing dots and spaces, short
    names) made the usual one. Case is folded on every OS: Windows and macOS ignore it, and on Linux the cost is at most a
    path left out that need not be. Spellings text cannot tell (hard links, bind mounts) are compared by identity in _denied"""
    from .core import samepath
    s = unicodedata.normalize("NFC", os.path.expanduser(str(p or "")))
    winlike = bool(re.match(r"^[A-Za-z]:[\\/]|^[\\/]{2}[^\\/]", s))
    if winlike or os.name == "nt":
        s = samepath.win_text(s)
    if winlike and os.name != "nt":
        s = s.replace("\\", "/")   # a Windows path seen on another OS (an index copied over): compared as written
    elif os.path.isabs(s):
        s = samepath.long_name(os.path.normpath(s))
        d, name = os.path.split(s)
        full = os.path.join(_real_dir(d), name) if name else _real_dir(s)
        if name and os.path.islink(full):
            full = _real_dir(full)   # a file that is a link: where it points
        s = samepath.unfirm(full)
    s = s.replace("\\", "/")
    if s.startswith("//?/UNC/"):
        s = "//" + s[8:]
    elif s.startswith("//?/"):
        s = s[4:]
    lead = "//" if s.startswith("//") else ""
    return (lead + posixpath.normpath(s).lstrip("/") if lead else posixpath.normpath(s)).rstrip("/").casefold() or "/"


class _Roots(set):
    """the deny folders' canonical texts, and .ids: the same folders by (st_dev, st_ino)"""
    ids = {}


def _deny_roots():
    """findr's data folder and the folders in FINDR_MCP_DENY (';' or os.pathsep between them; C:\\x is one folder on any
    OS), each in every form the index may hold it (C:\\ and /mnt/c/), and by identity"""
    from .core import samepath
    out, dirs = _Roots(), []
    for d in [str(paths.HOME), *samepath.split_list(os.environ.get("FINDR_MCP_DENY", ""))]:
        if d.strip():
            forms = _path_forms(d.strip())
            out |= {_canon(f) for f in forms}
            dirs += [os.path.expanduser(f) for f in forms if os.path.isabs(os.path.expanduser(f))]
    out.ids = samepath.ids_of(dirs)
    return out


def _secret(p):
    """the indexer's secret rules that need only the path (index.secret_name) on a path as stored: a rule added since the
    file was indexed hides it now, not at the next index run. The rules that open the file (a '.key' that is not a Keynote
    deck, a CSV's header) were applied by the indexer (tier 0). A Windows path is taken apart as one on any OS"""
    from pathlib import PurePosixPath, PureWindowsPath
    try:
        from .core import index
        s = str(p)
        q = PureWindowsPath(s) if (os.name == "nt" or "\\" in s) else PurePosixPath(s)
        if index.secret_name(q):
            return True
        if os.path.isabs(s) and os.path.islink(s):   # a link with a harmless name: where it leads decides too
            t = index.link_target(s)
            return bool(t) and index.secret_name(PurePosixPath(t) if os.name != "nt" else PureWindowsPath(t))
        return False
    except Exception:   # noqa: BLE001
        return False


def _denied(p, roots=None):
    """True for a path in or under the data folder or a FINDR_MCP_DENY folder ('..' cannot step out of the check), or a file
    the secret rules mark"""
    if not p:
        return False
    roots = _deny_roots() if roots is None else roots
    for f in _path_forms(str(p)):
        c = _canon(f)
        if any(c == r or c.startswith(r.rstrip("/") + "/") for r in roots):
            return True
    from .core import samepath
    if getattr(roots, "ids", None) and os.path.isabs(str(p)) and samepath.under(str(p), roots.ids) is not None:
        return True   # the same folder under a spelling text cannot tell (a hard link, a bind mount, a firmlink, case)
    return _secret(p)


def _db():
    if not paths.DB.exists():
        return None
    con = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True, timeout=10)
    con.create_function("nfc", 1, lambda s: unicodedata.normalize("NFC", s) if s else s, deterministic=True)
    _real_dir.cache_clear()
    from .core import samepath
    samepath.clear()
    roots = _deny_roots()
    con.create_function("denied", 1, lambda s: 1 if _denied(s, roots) else 0, deterministic=True)
    return con


RW_WAIT = 0.2   # seconds a tool waits for the index's write lock: the indexer holds it for long stretches (140 s seen)


def _rw():
    """the index open for writing, for the ids and terms kept in it (made when first asked, #203); None when it cannot be
    written now (the indexer holds it, a read-only folder): the tools then answer without those. The write lock is tried
    once, for RW_WAIT s, before anything else; each later write waits at most RW_WAIT s too"""
    if not paths.DB.exists():
        return None
    con = None
    try:
        con = sqlite3.connect(str(paths.DB), timeout=RW_WAIT)
        con.create_function("nfc", 1, lambda s: unicodedata.normalize("NFC", s) if s else s, deterministic=True)
        con.execute("SELECT 1 FROM files LIMIT 1")
        con.execute("BEGIN IMMEDIATE")   # free now? (the indexer writes in long transactions)
        con.rollback()
        return con
    except sqlite3.Error:
        if con is not None:
            con.close()
        return None


def _rel_ids(fid, family, named=(), con=None):
    """{'file_id', 'family_id', 'members': {path: file id}} for an index row. `family`: the paths of its family (same-content
    copies and versions in its folder); `named`: other paths whose file ids are wanted too. Nothing is hashed here (#203
    review: versions() read every same-size file at each call). {} when the ids cannot be written now"""
    from .core import relations
    own = con is None
    con = _rw() if own else con
    if con is None:
        return {}
    try:
        fids = {}
        for p2 in dict.fromkeys([*family, *named]):
            r = con.execute("SELECT id FROM files WHERE path=?", (p2,)).fetchone()
            if r:
                fids[p2] = r[0]
        uid = relations.file_id(con, fid)
        if uid is None:   # the indexer took the lock meanwhile
            return {}
        fam = relations.family_id(con, fid, [fids[p2] for p2 in family if p2 in fids])
        members = {}
        for p2, f2 in fids.items():
            u = relations.file_id(con, f2)
            if u is None:
                break
            members[p2] = u
        return {"file_id": uid, "family_id": fam, "members": members}
    except sqlite3.Error:
        return {}
    finally:
        if own:
            con.close()


def _day(mt):
    try:   # a negative or odd mtime raised on Windows and failed the whole tool call
        import datetime
        return datetime.datetime.fromtimestamp(mt or 0).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _index_search(query, limit, exts=None, folder=""):
    """names and words straight from the index (the engine is not running, or inside a filter the engine's list missed)"""
    con = _db()
    if con is None:
        return {"results": [], "note": "findr has not indexed this PC yet. Open the findr app, or run: findr index", "engine": "none"}
    words = [w for w in unicodedata.normalize("NFC", query).split() if len(w) >= 2][:6]
    if not words and not (exts or folder):
        return {"results": [], "note": "", "engine": "index"}
    seen, out = set(), []
    extra, xargs = "", []
    if exts:
        extra += f" AND ext IN ({','.join('?' * len(exts))})"; xargs += sorted(exts)
    if folder:
        extra += " AND replace(nfc(path), '\\', '/') LIKE ? ESCAPE '\\'"
        xargs.append(_norm(folder).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
    # every word in the name; inside a filter, any word will do (the filter already narrows it)
    join = " OR " if (exts or folder) else " AND "
    namec = ("(" + join.join(["lower(nfc(name)) LIKE ?"] * len(words)) + ")") if words else "1"
    sql = f"SELECT id, path, name, mtime FROM files WHERE tier IN (1,3) AND {namec}{extra} AND NOT denied(path) ORDER BY mtime DESC LIMIT ?"
    rows = con.execute(sql, [f"%{w.lower()}%" for w in words] + xargs + [limit]).fetchall()
    try:   # words inside documents (3-letter index)
        fq = " OR ".join('"' + w.replace('"', "") + '"' for w in words if len(w) >= 3)
        if fq:
            rows += con.execute("""SELECT f.id, f.path, f.name, f.mtime FROM fts JOIN chunks c ON c.id = fts.rowid JOIN files f ON f.id = c.file
                                   WHERE fts MATCH ? AND f.tier IN (1,3) AND NOT denied(f.path) ORDER BY rank LIMIT ?""", (fq, limit * 3)).fetchall()   # as the name search: no library or secret files
    except sqlite3.OperationalError:
        pass
    import datetime
    for fid, p, name, mt in rows:
        if fid in seen:
            continue
        seen.add(fid)
        out.append({"name": name, "path": p, "modified": _day(mt), "chance": None, "excerpt": ""})
        if len(out) >= limit:
            break
    return {"results": out, "note": "The findr search engine is not running, so this is a simple name and word search. "
                                    "Open the findr app (or run: findr serve) for better results.", "engine": "index"}


KINDS = {"pdf": {".pdf"}, "word": {".docx", ".doc", ".odt", ".rtf", ".pages"}, "hwp": {".hwp", ".hwpx"},
         "slides": {".pptx", ".ppt", ".key", ".odp"}, "sheet": {".xlsx", ".xls", ".csv", ".numbers", ".ods"},
         "image": {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp", ".tif", ".tiff"},
         "text": {".txt", ".md"}, "audio": {".mp3", ".m4a", ".wav", ".aac", ".ogg"},
         "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"}}


def _exts(kind):
    """'pdf', 'slides', '.key' or 'image,pdf' -> the extensions; None for any"""
    if not kind:
        return None
    out = set()
    for k in str(kind).lower().replace(" ", "").split(","):
        out |= KINDS.get(k, {k if k.startswith(".") else "." + k})
    return out


def _norm(p):
    return unicodedata.normalize("NFC", str(p or "")).replace("\\", "/").rstrip("/")


def _fits(x, exts, since, until, folder):
    """one result against the filters (names, 'modified' dates as YYYY-MM-DD..., folder as a path prefix)"""
    if exts and os.path.splitext(x.get("name") or "")[1].lower() not in exts:
        return False
    day = (x.get("modified") or "")[:10]
    if since and (not day or day < since[:10]):
        return False
    if until and (not day or day > until[:10]):
        return False
    if folder and not _norm(x.get("path")).lower().startswith(_norm(folder).lower() + "/"):
        return False
    return True


def _path_forms(pth):
    """the same file as the index may have stored it: as given, with / for \\, and C:\\... <-> /mnt/c/... (WSL)"""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", pth or "")
    forms = [pth, (pth or "").replace("\\", "/")]
    if m:
        forms.append(f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/"))
    m = re.match(r"^/mnt/([a-z])/(.*)$", pth or "")
    if m:
        forms.append(f"{m.group(1).upper()}:\\" + m.group(2).replace("/", "\\"))
    return list(dict.fromkeys(forms))


MAYBE = 0.15        # a file at least this likely to be the asked kind is never left out
OTHER_SURE = 0.6    # a file is left out only when this sure it is another kind


def search_files(query: str, limit: int = 8, kind: str = "", since: str = "", until: str = "", folder: str = "") -> dict:
    """Find files on this PC from a vague description, the way a person remembers them: what the file was about, roughly
    when, who sent it, which version ('the draft before the final'). Korean and English. Returns the likely files, best
    first, with 'chance' (0-1) that each is the one meant, where it is, when it was modified, and a short excerpt.
    A note says when nothing is a sure match. Ask the person before opening or using a file they did not mean.
    excerpt_start is where the excerpt begins inside one stored piece of the file (excerpt_chunk, counted from 0);
    excerpt_offset is the same place in read_file's text (read_file with max_chars past it shows the passage).
    Optional filters: kind (pdf, word, hwp, slides, sheet, image, text, audio, video, or extensions like '.key';
    several with commas; or a kind of document: 회의록/minutes, 계약서/contract, 견적서/invoice, 보고서/report,
    계획서/proposal, 신청서/application, 공문/notice, 규정/manual, 명단/list, 양식/form, 증명서/certificate,
    강의/lecture, 발표/presentation), since / until (YYYY-MM-DD, by modified date), folder (only inside this folder).
    Use them when the person said the format, the kind of document, the period or the place, or when a first search
    came back with the wrong kind of file."""
    limit = max(1, min(int(limit or 8), 30))
    from .core import doctype
    words = [w for w in re.split(r"[,\s]+", str(kind or "")) if w]
    dk = doctype.match(kind)   # a kind of document (회의록, contract, ...): read from the index's own vectors
    exts = _exts(",".join(w for w in words if not doctype.match(w)))
    if not (exts or dk or since or until or folder):
        return _engine_search(query, limit) or _index_search(query, limit)
    r = _engine_search(query, 50) or _index_search(query, 50, exts=exts, folder=folder)   # wider, then the filters
    kept = [x for x in r["results"] if _fits(x, exts, since, until, folder)]
    if len(kept) < limit or dk:   # the index itself, inside the filters, for what the ranked list did not hold
        seen = {x["path"] for x in kept}
        kept += [x for x in _index_search(query, 300 if dk else 50, exts=exts, folder=folder)["results"]   # a kind is read cheaply: look wider
                 if x["path"] not in seen and _fits(x, exts, since, until, folder)]
    note = ""
    if dk:
        con = _db()
        ids = {}
        if con is not None:
            for x in kept:
                row = None
                for c in _path_forms(x["path"]):   # the engine may name a file the way the OS does, the index another way
                    row = con.execute("SELECT id FROM files WHERE path=?", (c,)).fetchone()
                    if row:
                        break
                if row:
                    ids[x["path"]] = row[0]
        P = doctype.probs(con, list(ids.values())) if con is not None else {}
        if P:
            labels = doctype.heads()["labels"]
            for x in kept:
                p = P.get(ids.get(x["path"]))
                if p is not None:
                    x["doc_kind"], x["doc_kind_chance"] = labels[int(p.argmax())], round(float(p.max()), 2)
                    x["_m"] = doctype.chance_of(p, dk)
            # only a file surely of another kind leaves: the kind model is wrong at times (0.5-0.8 on PCs it never saw)
            # and an uncertain guess must not hide the file meant. The rest keep the search's order; those not read as
            # the asked kind are marked maybe, those with no stored vector unknown
            out, maybe = [], 0
            for x in kept:
                k, c = x.get("doc_kind"), x.get("doc_kind_chance", 0)
                if k is not None and k not in dk and c >= OTHER_SURE and x.get("_m", 0) < MAYBE:
                    continue
                if k is None:
                    x["doc_kind_match"] = "unknown"; maybe += 1
                elif k not in dk:
                    x["doc_kind_match"] = "maybe"; maybe += 1
                out.append(x)
            for x in out:
                x.pop("_m", None)
            kept = out
            if maybe:
                note = f"{maybe} of these may be another kind of document (the kind was uncertain); they are marked."
        else:
            note = "Kinds of documents cannot be told on this index (its encoder has no kind model), so that part was not applied."
    r = dict(r); r["results"] = kept[:limit]
    r["filters"] = {k: v for k, v in (("kind", kind), ("since", since), ("until", until), ("folder", folder)) if v}
    if note:
        r["note"] = note
    elif not r["results"]:
        r["note"] = "Nothing matched inside these filters. Try without one of them, or list_folder to look around."
    return r


def list_folder(path: str, limit: int = 50) -> dict:
    """What one folder on this PC holds (as findr indexed it): its files, newest first, with dates and sizes, and its
    subfolders with how many files each has. Give a folder, or a file to see the folder it is in. Use it to look around
    once a search shows roughly where something is: photos and scans with no text, a project or event folder."""
    con = _db()
    if con is None:
        return {"error": "findr has no index on this PC yet"}
    limit = max(1, min(int(limit or 50), 200))
    p = _norm(path)
    if _denied(path):
        return {"folder": p, "files": [], "folders": [], "note": "findr has nothing indexed in this folder"}
    if con.execute("SELECT 1 FROM files WHERE replace(nfc(path), '\\', '/') = ? AND NOT denied(path)", (p,)).fetchone():
        p = p.rsplit("/", 1)[0]   # a file: its folder
    pre = p + "/"
    esc = pre.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    files, sub, here = [], {}, 0
    for fp, name, size, mt in con.execute("SELECT path, name, size, mtime FROM files WHERE tier IN (1,3) AND "
                                          "replace(nfc(path), '\\', '/') LIKE ? ESCAPE '\\' AND NOT denied(path) ORDER BY mtime DESC", (esc + "%",)):
        rest = _norm(fp)[len(pre):]
        if "/" in rest:
            d = rest.split("/", 1)[0]; sub[d] = sub.get(d, 0) + 1
        else:
            here += 1
            if len(files) < limit:
                files.append({"name": name, "path": fp, "modified": _day(mt), "size": size})
    if not files and not sub:
        return {"folder": p, "files": [], "folders": [], "note": "findr has nothing indexed in this folder"}
    return {"folder": p, "files": files, "more_files": here - len(files),
            "folders": [{"name": d, "path": pre + d, "files": n} for d, n in sorted(sub.items(), key=lambda kv: -kv[1])]}


_VMARK = re.compile(r"([._\- ]?(rev|v|ver)\.?\d+(\.\d+)?|[._\- ]?(old|orig|backup|final|draft|최종|진짜최종|초안|수정본?|백업|"
                     r"복사본|사본|\d+차본?|copy)|\s*\(\d+\)|[._\-]\d{4,8})(?=[._\- ]|$)", re.I)


def _stem(name):
    s = os.path.splitext(unicodedata.normalize("NFC", name or ""))[0]
    for _ in range(3):
        s = _VMARK.sub("", s).strip(" ._-")
    return s.lower()


_GROUP = {e: g for g, es in {"slides": (".pptx", ".ppt", ".key", ".odp"), "doc": (".docx", ".doc", ".hwp", ".hwpx", ".pages", ".odt", ".pdf"),
                              "sheet": (".xlsx", ".xls", ".csv", ".numbers", ".ods")}.items() for e in es}


def _grams(s, digits=True):
    """character trigrams of a name or text, letters (and digits) only (Korean names have few spaces). Names are compared
    without digits: session numbers, years and dates ('08.' / '17.') are what differs between runs of the same course"""
    t = re.sub(r"[\W_]+" if digits else r"[\W_\d]+", "", unicodedata.normalize("NFC", s or "").lower())
    return {t[i:i + 3] for i in range(len(t) - 2)} or ({t} if t else set())


def _overlap(a, b):
    """share of the smaller set found in the other: '08. 강화학습' is all inside '17. 언어모델 실용강의 - 강화학습'"""
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


SIMILAR_MAX, SIBLING_MAX = 10, 3000   # at most 10 similar files; a parent with more files than this: the same folder only


def _similar(con, fid, name, ext, folder, head, mt, seen):
    """files that may be another version without sharing the name (#122 follow-up, field pilot): the same subject in the
    same folder or a sibling folder (a course run again: '1차 … / 08. 강화학습' and '2차 … / 17. … 강화학습'), by name or
    opening text, and another file of the same kind made the same day in the same folder (a contest entry saved under a
    new name). Each with why and how close"""
    parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
    pre = (parent or folder) + "/"
    rows = con.execute("SELECT id, name, ext, path, mtime FROM files WHERE tier IN (1,3) AND replace(nfc(path), '\\', '/') LIKE ? ESCAPE '!' AND NOT denied(path)",
                       (pre.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%",)).fetchall()
    def place(p2):
        d = _norm(p2).rsplit("/", 1)[0]
        return "same" if d == folder else ("sibling" if parent and d.rsplit("/", 1)[0] == parent else None)
    if len(rows) > SIBLING_MAX:   # a parent like the home or Downloads folder: siblings are not one project
        rows = [r for r in rows if place(r[3]) == "same"]
    ng, hg, day, grp = _grams(_stem(name), False), _grams(head[:700]), _day(mt), _GROUP.get((ext or "").lower())
    out = []
    for f2, n2, e2, p2, mt2 in rows:
        where = place(p2)
        if f2 in seen or not where:
            continue
        sim_n = _overlap(ng, _grams(_stem(n2), False))
        why, score = None, 0.0
        if sim_n >= 0.6:
            why, score = L(f"비슷한 이름 {sim_n:.2f}", f"similar name {sim_n:.2f}"), sim_n
        elif where == "same" and day and _day(mt2) == day and grp and _GROUP.get((e2 or "").lower()) == grp:
            why, score = L("같은 날 같은 폴더", "same day, same folder"), 0.55
        if why is None and hg and sim_n >= 0.2:   # some name in common: the opening text decides
            h2 = (con.execute("SELECT text FROM chunks WHERE file=? AND n=0", (f2,)).fetchone() or [""])[0] or ""
            sim_h = _overlap(hg, _grams(h2[:700]))
            if sim_h >= 0.5:
                why, score = L(f"비슷한 앞부분 {sim_h:.2f}", f"similar opening text {sim_h:.2f}"), sim_h
        if why:
            out.append({"relation": "similar", "name": n2, "path": p2, "modified": _day(mt2), "why": why,
                        "folder": L("같은 폴더", "same folder") if where == "same" else L("이웃 폴더", "sibling folder"), "score": round(score, 2)})
    out.sort(key=lambda x: -x["score"])
    return out[:SIMILAR_MAX]


def versions(path: str) -> dict:
    """The same document's other files on this PC, so the right one can be picked: exact copies anywhere (same format,
    size and text), other versions in the same folder (v2, 최종, (1), dates in the name), files with the same name in
    other folders, and up to 10 'similar' files that may be another version under another name: the same subject in this
    folder or a sibling folder (a course given again, by name or opening text) and another file of the same kind made the
    same day in this folder. Each with its folder and modified date ('similar' also with why and how close). Use it when
    a result may be a copy, a draft, last year's or another run of the same course or event."""
    con = _db()
    if con is None:
        return {"error": "findr has no index on this PC yet"}
    p = _norm(path)
    row = None if _denied(path) else con.execute("SELECT id, name, ext, size, path FROM files WHERE replace(nfc(path), '\\', '/') = ? "
                                                 "AND tier > 0 AND NOT denied(path)", (p,)).fetchone()
    if not row:
        return {"error": "not a file in findr's index; use a path returned by search_files"}
    fid, name, ext, size, fp = row
    head = (con.execute("SELECT text FROM chunks WHERE file=? AND n=0", (fid,)).fetchone() or [""])[0] or ""
    st, folder = _stem(name), p.rsplit("/", 1)[0]
    out, seen = [], {fid}
    if size:
        for f2, n2, p2, mt in con.execute("SELECT f.id, f.name, f.path, f.mtime FROM files f WHERE f.ext=? AND f.size=? AND f.id<>? "
                                          "AND f.tier IN (1,3) AND NOT denied(f.path) AND (SELECT text FROM chunks c WHERE c.file=f.id AND c.n=0) IS ?", (ext, size, fid, head or None)):
            out.append({"relation": "copy", "name": n2, "path": p2, "modified": _day(mt)}); seen.add(f2)
    for f2, n2, p2, mt in con.execute("SELECT id, name, path, mtime FROM files WHERE tier IN (1,3) AND nfc(name) LIKE ? AND NOT denied(path)", (f"%{st[:20]}%",)):
        if f2 in seen or _stem(n2) != st:
            continue
        seen.add(f2)
        rel = "version" if _norm(p2).rsplit("/", 1)[0] == folder else "same name elsewhere"
        out.append({"relation": rel, "name": n2, "path": p2, "modified": _day(mt)})
    out.sort(key=lambda x: x["modified"], reverse=True)   # newest first, then grouped: copies, versions, elsewhere
    out.sort(key=lambda x: {"copy": 0, "version": 1}.get(x["relation"], 2))
    me = con.execute("SELECT mtime FROM files WHERE id=?", (fid,)).fetchone()[0]
    sim = _similar(con, fid, name, ext, folder, head, me, seen)   # after the named ones: maybe another version, with why
    out = out[:30] + sim
    # additive fields (#203): the family is the copies and the versions in this folder, not the same name elsewhere
    ids = _rel_ids(fid, [o["path"] for o in out if o["relation"] in ("copy", "version")],
                   [o["path"] for o in out if o["relation"] == "same name elsewhere"])
    for o in out:
        if o["path"] in ids.get("members", {}):
            o["file_id"] = ids["members"][o["path"]]
    return {"file": {"name": name, "path": fp, "modified": _day(me), "file_id": ids.get("file_id"), "family_id": ids.get("family_id")},
            "others": out, "note": "" if out else "No other copy or version of this file is indexed."}


def file_relations(path: str, terms: int = 10) -> dict:
    """What ties one indexed file to others, from findr's index: its characteristic words (what the text is about, best
    first), a stable file id (kept when the file is re-indexed, edited or moved), the id of its version family (the same one
    versions() reports), and the other files with exactly the same content (sha1), wherever they are. Use the ids to
    recognise the same file or the same family of files across calls; use copies to find duplicates of one file."""
    from .core import relations
    ro = _db()
    if ro is None:
        return {"error": "findr has no index on this PC yet"}
    # the same guard as read_file: not the data folder, a deny folder or a secret file (#200), and not a tier-0 row
    row = None if _denied(path) else ro.execute("SELECT id, name, size, mtime, path FROM files WHERE replace(nfc(path), '\\', '/') = ? "
                                                "AND tier > 0 AND NOT denied(path)", (_norm(path),)).fetchone()
    ro.close()
    if not row:
        return {"error": "not a file in findr's index; use a path returned by search_files"}
    fid, name, size, mt, fp = row
    v = versions(fp)
    con = _rw()
    words, same, sig = [], [], None
    f = dict(v.get("file") or {})
    if con is not None:
        try:
            words = relations.terms(con, fid, max(1, min(int(terms or 10), 30)))
            words = [w for w in words if _mask(w) == w]   # a key-like word of an index from before a masking rule
            sig = relations.content_sig(con, fid)
            roots = _deny_roots()
            same = [{"name": c["name"], "path": c["path"], "modified": _day(c["mtime"]), "file_id": c["id"], "_fid": c["fid"]}
                    for c in relations.copies(con, fid, keep=lambda p2: not _denied(p2, roots))]
            if same:   # the very same bytes belong to the family too (members already in another family stay there)
                fam = relations.family_id(con, fid, [c["_fid"] for c in same])
                f["family_id"] = fam or f.get("family_id")
        except sqlite3.Error:
            pass
        finally:
            con.close()
    for c in same:
        c.pop("_fid", None)
    return {"file": {"name": name, "path": fp, "modified": _day(mt), "size": size, "file_id": f.get("file_id"),
                     "family_id": f.get("family_id"), "content_sha1": sig},
            "terms": words, "same_content": same,
            "family": [o for o in v.get("others", []) if o["relation"] != "similar"],
            "note": "" if f.get("file_id") else "Ids and terms could not be stored now (the index is busy or read-only); try again."}


def read_file(path: str, max_chars: int = 4000) -> dict:
    """The text findr already read from one file in its index (a path from search_files). Documents, scans and slides
    come back as plain text. Only indexed files can be read, and only up to max_chars (at most 20000)."""
    con = _db()
    if con is None:
        return {"error": "findr has no index on this PC yet"}
    max_chars = max(200, min(int(max_chars or 4000), 20000))
    row = None if _denied(path) else con.execute("SELECT id, name, tier FROM files WHERE (path = ? OR nfc(path) = ?) AND tier > 0 AND NOT denied(path)",
                                                 (path, unicodedata.normalize("NFC", path))).fetchone()
    if not row:
        return {"error": "not a file in findr's index; use a path returned by search_files"}
    text, n = [], 0
    for (t,) in con.execute("SELECT text FROM chunks WHERE file = ? AND n >= 0 ORDER BY n", (row[0],)):
        if t:
            text.append(t); n += len(t)
        if n >= max_chars:
            break
    body = _mask("\n".join(text))[:max_chars]   # stored text is masked; an index from before a newer rule is masked here
    out = {"name": row[1], "path": path, "text": body, "truncated": n > max_chars,
           "note": "" if body else "findr has only the name of this file so far (its text is read later, or it has none)"}
    try:   # what the index cannot say: why a file is unreadable, a docx's pages, and the file read now when the index has no text
        if os.path.isfile(path) and (not body or path.lower().endswith(".docx")) and _deep_ok(path, row[2], con):
            got = _deep_read(path, max_chars)
            for k in ("unreadable", "pages", "pages_estimated", "ocr"):
                if k in got:
                    out[k] = got[k]
            if not body:
                out["text"], out["truncated"] = _mask(got.get("text") or ""), got.get("truncated", False)
                out["note"] = got.get("note") or ("read from the file now (findr has not stored its text yet)" if out["text"] else out["note"])
    except Exception:   # noqa: BLE001
        pass
    return out


def _mask(text):
    """values that look like keys, masked as the index masks them (index.chunks)"""
    from .core import index
    return index.mask(text)


def _deep_ok(path, tier, con=None):
    """read now only what the indexer would read: a tier-1 file (documents, text) or a photo / scan for OCR. Never a tier-3
    file read as raw bytes (a VPN profile holds a private key), never a secret (the link's target too), never an
    online-only cloud file (reading it would download it). A link is read only when where it leads is itself an indexed,
    readable file outside the deny folders: a link out of the chosen folders is never followed"""
    from pathlib import Path
    from .core import deepread, index
    if index.is_secret(Path(path)) or index.cloud_only_path(path):
        return False
    t = index.link_target(path)
    if t is not None and os.path.islink(path):
        if con is None or _denied(t):
            return False
        r = con.execute("SELECT tier FROM files WHERE path=? OR nfc(path)=?", (t, unicodedata.normalize("NFC", t))).fetchone()
        if not r or r[0] <= 0 or not (r[0] == 1 or index.real_ext(Path(t)) in deepread.IMAGES):
            return False
    return tier == 1 or index.real_ext(Path(path)) in deepread.IMAGES


DEEP_SECONDS = 30   # FINDR_MCP_DEEP_READ_SECONDS: time an assistant's read_file may spend on OCR (pages are read until then)
DEEP_GRACE = 15     # ... and the page in progress when it runs out; then the reader is stopped


def _deep_cmd(path, max_chars, seconds):
    import sys
    return [sys.executable, "-m", "findr.core.deepread", str(path), str(int(max_chars)), str(seconds)]


def _deep_read(path, max_chars):
    """deepread.deep_read: in this process for what needs no recognizer; OCR (a scanned PDF, a photo) in a short-lived child
    process, so the recognizer (EasyOCR / torch, 1-2 GB) is not kept by the MCP server, and a slow scan is cut off at
    FINDR_MCP_DEEP_READ_SECONDS (30) instead of holding the assistant for minutes"""
    from pathlib import Path
    from .core import deepread, index
    ext = index.real_ext(Path(path))
    if ext != ".pdf" and ext not in deepread.IMAGES:
        return deepread.deep_read(path, max_chars)
    why = deepread.unreadable_reason(path)
    if why:
        return {"text": "", "unreadable": why, "note": why}
    seconds = deepread._env_num("FINDR_MCP_DEEP_READ_SECONDS", DEEP_SECONDS, float)
    import subprocess
    try:
        r = subprocess.run(_deep_cmd(path, max_chars, seconds), capture_output=True, timeout=seconds + DEEP_GRACE,
                           stdin=subprocess.DEVNULL, cwd=str(Path(__file__).resolve().parent.parent))
        line = [x for x in r.stdout.decode("utf-8", "replace").splitlines() if x.startswith("{")][-1:]
        return json.loads(line[0]) if line else {"text": "", "note": "the file could not be read now"}
    except subprocess.TimeoutExpired:
        return {"text": "", "note": f"reading this file took longer than {seconds:.0f} s and was stopped "
                                    "(FINDR_MCP_DEEP_READ_SECONDS); its text comes when findr indexes it"}


def main():
    try:   # MCP SDK 2.x
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:   # 1.x
        from mcp.server.fastmcp import FastMCP as Server
    srv = Server("findr", instructions="Search the files on this person's PC by description (findr). "
                                       "Use search_files first (with kind / since / until / folder when the person said the "
                                       "format, period or place); read_file only for a path it returned. To look around "
                                       "where a result is, list_folder; to pick between copies, drafts and versions, versions.")
    srv.tool()(search_files)
    srv.tool()(read_file)
    srv.tool()(list_folder)
    srv.tool()(versions)
    srv.tool()(file_relations)
    srv.run()
