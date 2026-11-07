"""Memory traces of files: what the PC already records about how a file was made and handled, read without any language model.
  docmeta   author, last editor, revision count from document properties (Office Open XML core.xml, old Office / HWP summary
            information, PDF info dictionary)
  events    created / modified / opened / downloaded / saved_from_mail / saved_from_messenger / sent / printed / renamed / copied,
            with time, person and source (filled by collectors on a real PC, or loaded for a synthetic PC)
  calendar  meetings (title, start, end, attendees), when the person allows it
Everything stays in the local index.   python traces.py DB ROOT [--synthetic DIR]"""
import json, re, sqlite3, struct, sys, zipfile
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS docmeta(file INTEGER PRIMARY KEY, author TEXT, last_by TEXT, revision INT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, file INT, type TEXT, time TEXT, person TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS ev_file ON events(file);
CREATE TABLE IF NOT EXISTS calendar(id INTEGER PRIMARY KEY, title TEXT, start TEXT, end TEXT, attendees TEXT);
"""
_TAG = lambda name: re.compile(rf"<(?:\w+:)?{name}[^>]*>([^<]*)</(?:\w+:)?{name}>")
_CREATOR, _LASTBY, _REV = _TAG("creator"), _TAG("lastModifiedBy"), _TAG("revision")


def doc_properties(path):
    """(author, last editor, revision) from the file's own properties, or None"""
    p = Path(path); ext = p.suffix.lower()
    try:
        if ext in (".docx", ".xlsx", ".pptx", ".hwpx"):
            with zipfile.ZipFile(p) as z:
                core = next((n for n in z.namelist() if n.lower().endswith("core.xml")), None)
                if not core:
                    return None
                x = z.read(core).decode("utf-8", "ignore")
            g = lambda rx: (rx.search(x).group(1).strip() if rx.search(x) else None)
            rev = g(_REV)
            return g(_CREATOR), g(_LASTBY), int(rev) if rev and rev.isdigit() else None
        if ext in (".doc", ".xls", ".ppt", ".hwp"):
            import olefile
            if not olefile.isOleFile(str(p)):
                return None
            ole = olefile.OleFileIO(str(p))
            for stream in ("\x05HwpSummaryInformation", "\x05SummaryInformation"):
                if ole.exists(stream):
                    pr = ole.getproperties(stream, convert_time=False)
                    dec = lambda v: v.decode("utf-8", "ignore") if isinstance(v, bytes) else v
                    rev = dec(pr.get(9))
                    return dec(pr.get(4)), dec(pr.get(8)), int(rev) if isinstance(rev, str) and rev.isdigit() else None
            return None
        if ext == ".pdf":
            from pypdf import PdfReader
            info = PdfReader(str(p)).metadata or {}
            a = info.get("/Author")
            return (str(a) if a else None), None, None
    except Exception:
        return None
    return None


def ensure(con):
    con.executescript(SCHEMA)


def harvest_docmeta(con, root):
    """read document properties of indexed files under root; returns how many files had any author/editor"""
    ensure(con); pre = str(root).rstrip("/") + "/"; n = 0
    for fid, path in con.execute("SELECT id, path FROM files WHERE path LIKE ? AND tier IN (1,3)", (pre + "%",)).fetchall():
        r = doc_properties(path)
        if r and (r[0] or r[1]):
            con.execute("INSERT OR REPLACE INTO docmeta VALUES(?,?,?,?)", (fid, r[0], r[1], r[2])); n += 1
    con.commit()
    return n


def load_synthetic(con, root, d):
    """a synthetic PC's recorded traces (docmeta.json, events.jsonl, calendar.jsonl) into the index"""
    ensure(con); d = Path(d)
    fid = {p: i for i, p in con.execute("SELECT id, path FROM files")}
    key = lambda rel: fid.get(str(Path(root) / rel.split("/", 1)[1]))
    for m in json.load(open(d / "docmeta.json", encoding="utf-8")) if (d / "docmeta.json").exists() else []:
        if key(m["path"]):
            con.execute("INSERT OR REPLACE INTO docmeta VALUES(?,?,?,?)", (key(m["path"]), m.get("author"), m.get("last_modified_by"), m.get("revision")))
    for e in (json.loads(l) for l in open(d / "events.jsonl", encoding="utf-8") if l.strip()) if (d / "events.jsonl").exists() else []:
        if key(e["path"]):
            con.execute("INSERT INTO events(file, type, time, person, source) VALUES(?,?,?,?,?)",
                        (key(e["path"]), e["type"], e["time"], e.get("person"), e.get("source")))
    for c in (json.loads(l) for l in open(d / "calendar.jsonl", encoding="utf-8") if l.strip()) if (d / "calendar.jsonl").exists() else []:
        con.execute("INSERT INTO calendar(title, start, end, attendees) VALUES(?,?,?,?)",
                    (c["title"], c["start"], c.get("end"), json.dumps(c.get("attendees", []), ensure_ascii=False)))
    con.commit()


if __name__ == "__main__":
    db, root = sys.argv[1], sys.argv[2]
    con = sqlite3.connect(db)
    if "--synthetic" in sys.argv:
        load_synthetic(con, root, sys.argv[sys.argv.index("--synthetic") + 1])
    else:
        print("docmeta files", harvest_docmeta(con, root))
