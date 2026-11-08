"""findr for AI tools: an MCP server (stdio) so assistants on this PC (Claude Desktop, Claude Code and others) can find the
person's files through findr.

Tools
  search_files(query, limit, kind, since, until, folder)   the files most likely meant, with the odds, where they are and
                               a short excerpt; optionally only one kind of file, a period, or one folder
  read_file(path, max_chars)   the text findr already read from one indexed file
  list_folder(path, limit)     what one folder holds, newest first, and its subfolders: to look around the way a person
                               does once a search shows roughly where something is (a photo with no text, a project folder)
  versions(path)               the same document's other files: exact copies anywhere, other versions in its folder, the
                               same name in other folders, with dates, so the latest or the original can be picked

The last two and the filters came from real-PC runs (11/9): agents with search_files alone needed 2-4 calls, and said a
kind filter, a folder view and a version view would have ended most of them in one.

It asks the running search engine when it is up (full quality: meaning, reranking, odds). Without it, it searches the
index directly by names and words. read_file returns only files in the index, from the text findr stored; it never opens
other paths. What the tools return goes to the assistant that called them, so the person's assistant settings decide
whether it leaves the PC."""
import json
import os
import re
import sqlite3
import unicodedata
import urllib.parse
import urllib.request

from . import paths

ENGINE = f"http://127.0.0.1:{os.environ.get('FINDR_APP_PORT') or os.environ.get('FINDR_PORT') or 8096}"


def _key():
    try:
        return (paths.HOME / ".password").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_DOWN = [0.0]   # until when the engine is taken as not running


def _engine_up():
    """a quick look before asking: on Windows a refused localhost connection takes ~2 s (it retries), so with the app
    closed every search_files call waited 2 s before the index search. Not up: look again after 15 s"""
    import socket
    import time
    if time.time() < _DOWN[0]:
        return False
    try:
        socket.create_connection(("127.0.0.1", int(ENGINE.rsplit(":", 1)[1])), timeout=0.3).close()
        return True
    except OSError:
        _DOWN[0] = time.time() + 15
        return False


def _engine_search(query, limit):
    """results from the running engine, or None when it is not up"""
    if not _engine_up():
        return None
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    import base64
    k = _key()   # the page's password: as the app's cookie and as HTTP Basic (any user name), whichever the engine takes
    req = urllib.request.Request(f"{ENGINE}/api?" + urllib.parse.urlencode({"q": query, "k": limit}),
                                 headers={"Cookie": f"findr_t={k}", "Authorization": "Basic " + base64.b64encode(f"findr:{k}".encode()).decode()})
    try:
        r = json.loads(op.open(req, timeout=60).read())
    except Exception:
        return None
    out = []
    for x in (r.get("results") or [])[:limit]:
        out.append({"name": x.get("name"), "path": x.get("path"), "modified": x.get("date"),
                    "chance": round(x["prob"], 2) if x.get("prob") is not None else None,
                    "excerpt": (x.get("snippet") or "")[:300]})
    return {"results": out, "note": r.get("note") or "", "engine": "full"}


def _db():
    if not paths.DB.exists():
        return None
    con = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True, timeout=10)
    con.create_function("nfc", 1, lambda s: unicodedata.normalize("NFC", s) if s else s, deterministic=True)
    return con


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
    sql = f"SELECT id, path, name, mtime FROM files WHERE tier IN (1,3) AND {namec}{extra} ORDER BY mtime DESC LIMIT ?"
    rows = con.execute(sql, [f"%{w.lower()}%" for w in words] + xargs + [limit]).fetchall()
    try:   # words inside documents (3-letter index)
        fq = " OR ".join('"' + w.replace('"', "") + '"' for w in words if len(w) >= 3)
        if fq:
            rows += con.execute("""SELECT f.id, f.path, f.name, f.mtime FROM fts JOIN chunks c ON c.id = fts.rowid JOIN files f ON f.id = c.file
                                   WHERE fts MATCH ? AND f.tier IN (1,3) ORDER BY rank LIMIT ?""", (fq, limit * 3)).fetchall()   # as the name search: no library or secret files
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


def search_files(query: str, limit: int = 8, kind: str = "", since: str = "", until: str = "", folder: str = "") -> dict:
    """Find files on this PC from a vague description, the way a person remembers them: what the file was about, roughly
    when, who sent it, which version ('the draft before the final'). Korean and English. Returns the likely files, best
    first, with 'chance' (0-1) that each is the one meant, where it is, when it was modified, and a short excerpt.
    A note says when nothing is a sure match. Ask the person before opening or using a file they did not mean.
    Optional filters: kind (pdf, word, hwp, slides, sheet, image, text, audio, video, or extensions like '.key';
    several with commas), since / until (YYYY-MM-DD, by modified date), folder (only inside this folder). Use them when
    the person said the format, the period or the place, or when a first search came back with the wrong kind of file."""
    limit = max(1, min(int(limit or 8), 30))
    exts = _exts(kind)
    if not (exts or since or until or folder):
        return _engine_search(query, limit) or _index_search(query, limit)
    r = _engine_search(query, 50) or _index_search(query, 50, exts=exts, folder=folder)   # wider, then the filters
    kept = [x for x in r["results"] if _fits(x, exts, since, until, folder)]
    if len(kept) < limit:   # the index itself, inside the filters, for what the ranked list did not hold
        seen = {x["path"] for x in kept}
        kept += [x for x in _index_search(query, 50, exts=exts, folder=folder)["results"]
                 if x["path"] not in seen and _fits(x, exts, since, until, folder)]
    r = dict(r); r["results"] = kept[:limit]
    r["filters"] = {k: v for k, v in (("kind", kind), ("since", since), ("until", until), ("folder", folder)) if v}
    if not r["results"]:
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
    if con.execute("SELECT 1 FROM files WHERE replace(nfc(path), '\\', '/') = ?", (p,)).fetchone():
        p = p.rsplit("/", 1)[0]   # a file: its folder
    pre = p + "/"
    esc = pre.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    files, sub, here = [], {}, 0
    for fp, name, size, mt in con.execute("SELECT path, name, size, mtime FROM files WHERE tier IN (1,3) AND "
                                          "replace(nfc(path), '\\', '/') LIKE ? ESCAPE '\\' ORDER BY mtime DESC", (esc + "%",)):
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
    rows = con.execute("SELECT id, name, ext, path, mtime FROM files WHERE tier IN (1,3) AND replace(nfc(path), '\\', '/') LIKE ? ESCAPE '!'",
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
            why, score = f"비슷한 이름 {sim_n:.2f}", sim_n
        elif where == "same" and day and _day(mt2) == day and grp and _GROUP.get((e2 or "").lower()) == grp:
            why, score = "같은 날 같은 폴더", 0.55
        if why is None and hg and sim_n >= 0.2:   # some name in common: the opening text decides
            h2 = (con.execute("SELECT text FROM chunks WHERE file=? AND n=0", (f2,)).fetchone() or [""])[0] or ""
            sim_h = _overlap(hg, _grams(h2[:700]))
            if sim_h >= 0.5:
                why, score = f"비슷한 앞부분 {sim_h:.2f}", sim_h
        if why:
            out.append({"relation": "similar", "name": n2, "path": p2, "modified": _day(mt2), "why": why,
                        "folder": "같은 폴더" if where == "same" else "이웃 폴더", "score": round(score, 2)})
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
    row = con.execute("SELECT id, name, ext, size, path FROM files WHERE replace(nfc(path), '\\', '/') = ?", (p,)).fetchone()
    if not row:
        return {"error": "not a file in findr's index; use a path returned by search_files"}
    fid, name, ext, size, fp = row
    head = (con.execute("SELECT text FROM chunks WHERE file=? AND n=0", (fid,)).fetchone() or [""])[0] or ""
    st, folder = _stem(name), p.rsplit("/", 1)[0]
    out, seen = [], {fid}
    if size:
        for f2, n2, p2, mt in con.execute("SELECT f.id, f.name, f.path, f.mtime FROM files f WHERE f.ext=? AND f.size=? AND f.id<>? "
                                          "AND (SELECT text FROM chunks c WHERE c.file=f.id AND c.n=0) IS ?", (ext, size, fid, head or None)):
            out.append({"relation": "copy", "name": n2, "path": p2, "modified": _day(mt)}); seen.add(f2)
    for f2, n2, p2, mt in con.execute("SELECT id, name, path, mtime FROM files WHERE tier IN (1,3) AND nfc(name) LIKE ?", (f"%{st[:20]}%",)):
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
    return {"file": {"name": name, "path": fp, "modified": _day(me)}, "others": out,
            "note": "" if out else "No other copy or version of this file is indexed."}


def read_file(path: str, max_chars: int = 4000) -> dict:
    """The text findr already read from one file in its index (a path from search_files). Documents, scans and slides
    come back as plain text. Only indexed files can be read, and only up to max_chars (at most 20000)."""
    con = _db()
    if con is None:
        return {"error": "findr has no index on this PC yet"}
    max_chars = max(200, min(int(max_chars or 4000), 20000))
    row = con.execute("SELECT id, name FROM files WHERE path = ? OR nfc(path) = ?", (path, unicodedata.normalize("NFC", path))).fetchone()
    if not row:
        return {"error": "not a file in findr's index; use a path returned by search_files"}
    text, n = [], 0
    for (t,) in con.execute("SELECT text FROM chunks WHERE file = ? AND n >= 0 ORDER BY n", (row[0],)):
        if t:
            text.append(t); n += len(t)
        if n >= max_chars:
            break
    body = "\n".join(text)[:max_chars]
    return {"name": row[1], "path": path, "text": body, "truncated": n > max_chars,
            "note": "" if body else "findr has only the name of this file so far (its text is read later, or it has none)"}


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
    srv.run()
