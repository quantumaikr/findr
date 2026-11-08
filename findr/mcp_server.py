"""findr for AI tools: an MCP server (stdio) so assistants on this PC (Claude Desktop, Claude Code and others) can find the
person's files through findr.

Tools
  search_files(query, limit)   the files most likely meant, with the odds, where they are and a short excerpt
  read_file(path, max_chars)   the text findr already read from one indexed file

It asks the running search engine when it is up (full quality: meaning, reranking, odds). Without it, it searches the
index directly by names and words. read_file returns only files in the index, from the text findr stored; it never opens
other paths. What the tools return goes to the assistant that called them, so the person's assistant settings decide
whether it leaves the PC."""
import json
import os
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


def _index_search(query, limit):
    """names and words straight from the index (the engine is not running)"""
    con = _db()
    if con is None:
        return {"results": [], "note": "findr has not indexed this PC yet. Open the findr app, or run: findr index", "engine": "none"}
    words = [w for w in unicodedata.normalize("NFC", query).split() if len(w) >= 2][:6]
    if not words:
        return {"results": [], "note": "", "engine": "index"}
    seen, out = set(), []
    sql = "SELECT id, path, name, mtime FROM files WHERE tier IN (1,3) AND " + " AND ".join(["lower(nfc(name)) LIKE ?"] * len(words)) + " ORDER BY mtime DESC LIMIT ?"
    rows = con.execute(sql, [f"%{w.lower()}%" for w in words] + [limit]).fetchall()
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


def search_files(query: str, limit: int = 8) -> dict:
    """Find files on this PC from a vague description, the way a person remembers them: what the file was about, roughly
    when, who sent it, which version ('the draft before the final'). Korean and English. Returns the likely files, best
    first, with 'chance' (0-1) that each is the one meant, where it is, when it was modified, and a short excerpt.
    A note says when nothing is a sure match. Ask the person before opening or using a file they did not mean."""
    limit = max(1, min(int(limit or 8), 30))
    return _engine_search(query, limit) or _index_search(query, limit)


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
                                       "Use search_files first; read_file only for a path it returned.")
    srv.tool()(search_files)
    srv.tool()(read_file)
    srv.run()
