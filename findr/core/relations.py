"""Relations between files (#203): what a file is about, which file it is, and which other files are the same thing.

  terms(con, fid)        the file's characteristic words: top terms by tf-idf over the text the index already holds
  file_id(con, fid)      a stable id for the file (survives re-index, edits in place, moves and renames), see RULES
  family_id(con, fid, members)  an id shared by the files of one version family
  copies(con, fid, keep)  other indexed files with the same bytes (sha1), found by size first so few files are read; `keep`
                         (a path -> bool) leaves files out before they are read

Stdlib only; the tables are created when first used (an index made before this opens and migrates in place, nothing is
re-read). Reading never fails on a read-only connection: the functions then return what they can without writing.

RULES for the stable file id (file_uid.uid, "f_" + 16 hex):
  1. one index row keeps its id: re-indexing an unchanged file, and a moved or renamed file whose row the indexer kept
     (index.reuse_moved), change nothing
  2. a file changed in place gets a new index row; the old row leaves a short note (file_uid_gone) when it is dropped, and the
     new row at the same path takes the id back
  3. a file that moved and was not kept by the indexer takes the id of a dropped file with the same volume:inode, size and
     modified time; failing that, of the only dropped file with the same content (sha1) and size
  4. a copy never takes the id of a file that is still there: it is a new file (same content, see copies())
  Notes are forgotten after 60 days. An id is never reused for another file.
The version-family id (file_uid.fam, "v_" + 16 hex) is given to a file and to the members its caller names: the same-content
copies and the other versions in the same folder (never the same name in another folder: every project has its README.md).
A file keeps the family it has; a member that has another family keeps that one too: two families are never merged, so one
wrong pairing cannot join two unrelated families for good. A file without a family takes its own members' (the most common,
then the smallest), else a new id.
Hashing is bounded: files above HASH_MAX bytes are not hashed, and one copies() call reads at most COPY_BYTES."""
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
import uuid

TERMS_K = 10            # terms returned
TEXT_CAP = 60000        # characters of a file read for its terms
SAMPLE = 3000           # chunks sampled for document frequencies (all of them in a small index)
GONE_DAYS = 60
GONE_MAX = 20000
COPY_MAX = 200          # same-size files hashed at most per lookup
COPY_BYTES = 2 << 30    # bytes read at most per lookup (200 same-size files of 200 MB would be 40 GB)
HASH_MAX = 512 << 20    # a file larger than this is not hashed (no content id, no copies by content)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_uid(file INTEGER PRIMARY KEY, uid TEXT UNIQUE, fam TEXT, path TEXT, ino TEXT, sig TEXT, size INT, mtime REAL);
CREATE INDEX IF NOT EXISTS file_uid_fam ON file_uid(fam);
CREATE INDEX IF NOT EXISTS file_uid_sig ON file_uid(sig);
CREATE TABLE IF NOT EXISTS file_uid_gone(uid TEXT, fam TEXT, path TEXT, ino TEXT, sig TEXT, size INT, mtime REAL, ts REAL);
CREATE TABLE IF NOT EXISTS file_terms(file INTEGER PRIMARY KEY, stamp TEXT, terms TEXT);"""


def _schema(con):
    try:
        con.executescript(_SCHEMA)
        return True
    except sqlite3.OperationalError:   # read-only connection, or the index is being written
        return False


def _has(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


# ---------------------------------------------------------------- terms
_JOSA = re.compile(r"(으로|에서|에게|이랑|하고|까지|부터|이나|은|는|이|가|을|를|의|에|로|와|과|도|만|랑)$")   # as search._XL_JOSA
_WORD = re.compile(r"[가-힣]{2,}|[a-z][a-z0-9]{2,}")
_STOP = set("the and for with that this are was were from have has not but you your our can will all any its into out "
            "than then them they their there been being also more most other such only over some what when which who "
            "하는 있는 하고 그리고 대한 통해 위한 따라 경우 이후 이전 또는 및 등 것 수 있다 한다 합니다 입니다".split())
_cache = {}


def tokens(text):
    """the words of a text as the index sees them: Korean words without their particle, other words lower case"""
    out = []
    for w in _WORD.findall(unicodedata.normalize("NFC", text or "").lower()):
        if "가" <= w[0] <= "힣":
            s = _JOSA.sub("", w)
            w = s if len(s) >= 2 else w
        if w not in _STOP:
            out.append(w)
    return out


def _df(con):
    """(number of chunks sampled, {term: chunks holding it}) over an even sample of the index, kept until it grows"""
    top = con.execute("SELECT max(id) FROM chunks").fetchone()[0] or 0
    db = (con.execute("PRAGMA database_list").fetchone() or [0, 0, ""])[2]
    key = (db, top // 50)
    if key in _cache:
        return _cache[key]
    step = max(1, top // SAMPLE)
    n, df = 0, {}
    for (t,) in con.execute("SELECT text FROM chunks WHERE n>=0 AND text<>'' AND id % ? = 0 LIMIT ?", (step, SAMPLE)):
        n += 1
        for w in set(tokens(t[:3000])):
            df[w] = df.get(w, 0) + 1
    _cache.clear()
    _cache[key] = (n, df)
    return n, df


def terms(con, fid, k=TERMS_K):
    """the file's characteristic words, best first: tf (log) x idf, the idf from a sample of the whole index. Computed from
    the chunk text findr already stores and kept in file_terms until the file changes. [] for a file with no text yet"""
    row = con.execute("SELECT size, mtime FROM files WHERE id=?", (fid,)).fetchone()
    if not row:
        return []
    stamp = f"{row[0]}|{row[1]}|{k}"
    w = _schema(con)
    if w:
        c = con.execute("SELECT stamp, terms FROM file_terms WHERE file=?", (fid,)).fetchone()
        if c and c[0] == stamp:
            return json.loads(c[1])
    text, n = [], 0
    for (t,) in con.execute("SELECT text FROM chunks WHERE file=? AND n>=0 ORDER BY n", (fid,)):
        if t:
            text.append(t); n += len(t)
        if n >= TEXT_CAP:
            break
    tf = {}
    for x in tokens("\n".join(text)[:TEXT_CAP]):
        tf[x] = tf.get(x, 0) + 1
    out = []
    if tf:
        N, df = _df(con)
        long_ = sum(tf.values()) > 60   # a long text: a word seen once is noise
        sc = {x: (1 + math.log(c)) * (math.log((N + 1) / (df.get(x, 0) + 1)) + 1) for x, c in tf.items() if c > 1 or not long_}
        out = [x for x, _ in sorted(sc.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
    if w and text:   # a file whose text is not read yet is looked at again later
        try:
            con.execute("INSERT OR REPLACE INTO file_terms VALUES(?,?,?)", (fid, stamp, json.dumps(out, ensure_ascii=False))); con.commit()
        except sqlite3.OperationalError:
            pass
    return out


# ---------------------------------------------------------------- ids
def sha1(path):
    """sha1 of the whole file, as index._file_sig; None for a file larger than HASH_MAX"""
    if os.path.getsize(path) > HASH_MAX:
        return None
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _stat(path):
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime, (f"{st.st_dev}:{st.st_ino}" if st.st_ino else None)
    except OSError:
        return None


def tombstone(con, fid):
    """the index row `fid` is about to be dropped: leave a note of its id so the file can take it back where it turns up
    (called by index.drop; a no-op when the file never had an id)"""
    if not _has(con, "file_uid"):
        return
    r = con.execute("SELECT uid, fam, path, ino, sig, size, mtime FROM file_uid WHERE file=?", (fid,)).fetchone()
    if r:
        now = time.time()
        con.execute("INSERT INTO file_uid_gone VALUES(?,?,?,?,?,?,?,?)", (*r, now))
        con.execute("DELETE FROM file_uid_gone WHERE ts<?", (now - GONE_DAYS * 86400,))
        con.execute("DELETE FROM file_uid_gone WHERE rowid IN (SELECT rowid FROM file_uid_gone ORDER BY ts DESC LIMIT -1 OFFSET ?)", (GONE_MAX,))


def _inherit(con, path, size, mtime, ino, sig):
    """the note of a dropped file this one is (rules 2 and 3), or None"""
    live = {u for (u,) in con.execute("SELECT uid FROM file_uid")}   # rule 4: an id still in use is not taken
    rows = [r for r in con.execute("SELECT uid, fam, path, ino, sig, size, mtime FROM file_uid_gone ORDER BY ts DESC") if r[0] not in live]
    for r in rows:
        if r[2] == path:
            return r
    if ino:
        for r in rows:
            if r[3] == ino and r[5] == size and r[6] is not None and abs(r[6] - mtime) < 1:
                return r
    if sig:
        same = {r[0]: r for r in rows if r[4] == sig and r[5] == size}
        if len(same) == 1:
            return next(iter(same.values()))
    return None


def _row(con, fid):
    """{uid, family, sig, ...} of one index row, giving it an id on first sight; None when the file is not in the index
    or the index cannot be written"""
    f = con.execute("SELECT path, size, mtime FROM files WHERE id=?", (fid,)).fetchone()
    if not f or not _schema(con):
        return None
    path, size, mtime = f
    st = _stat(path)
    r = con.execute("SELECT uid, fam, ino, sig, size, mtime, path FROM file_uid WHERE file=?", (fid,)).fetchone()
    try:
        if r:
            sig = r[3]
            if st:
                same = st[0] == r[4] and abs(st[1] - (r[5] or 0)) < 1
                sig = sig if same else None   # the bytes changed: what was known of them is stale
                if not same or st[2] != r[2] or path != r[6]:
                    con.execute("UPDATE file_uid SET path=?, ino=?, sig=?, size=?, mtime=? WHERE file=?", (path, st[2], sig, st[0], st[1], fid))
                    con.commit()
            return {"uid": r[0], "fam": r[1], "sig": sig, "path": path}
        size, mtime, ino = st if st else (size, mtime, None)
        sig = None
        if st and con.execute("SELECT 1 FROM file_uid_gone WHERE size=? AND sig IS NOT NULL LIMIT 1", (size,)).fetchone():
            try:
                sig = sha1(path)   # only when a dropped file of this size has a known content: rule 3
            except OSError:
                pass
        g = _inherit(con, path, size, mtime, ino, sig)
        uid, fam = (g[0], g[1]) if g else ("f_" + uuid.uuid4().hex[:16], None)
        if g:
            con.execute("DELETE FROM file_uid_gone WHERE uid=?", (uid,))
        con.execute("INSERT INTO file_uid VALUES(?,?,?,?,?,?,?,?)", (fid, uid, fam, path, ino, sig, size, mtime))
        con.commit()
        return {"uid": uid, "fam": fam, "sig": sig, "path": path}
    except sqlite3.OperationalError:   # read-only, or the indexer holds the write lock
        return None


def file_id(con, fid):
    """the stable id of an index row (rules in the module doc); None when it cannot be written down"""
    r = _row(con, fid)
    return r["uid"] if r else None


def content_sig(con, fid):
    """sha1 of the file's bytes, read once and kept while its size and modified time stay; None when it cannot be read"""
    r = _row(con, fid)
    if not r:
        return None
    if r["sig"]:
        return r["sig"]
    try:
        s = sha1(r["path"])
    except OSError:
        return None
    if not s:
        return None
    try:
        con.execute("UPDATE file_uid SET sig=? WHERE file=?", (s, fid)); con.commit()
    except sqlite3.OperationalError:
        pass
    return s


def copies(con, fid, limit=50, keep=None):
    """other indexed files with the same bytes: [{path, name, mtime (epoch), size, fid (index row), id (stable file id)}], newest first. Files of another size
    cannot be copies, so only same-size files are read (at most COPY_MAX and COPY_BYTES per call; the hashes are kept).
    keep(path) -> False leaves a file out before it is read (the MCP tools' data folder and deny list)"""
    f = con.execute("SELECT size, ext FROM files WHERE id=?", (fid,)).fetchone()
    if not f or not f[0] or f[0] > HASH_MAX:
        return []
    mine = content_sig(con, fid)
    if not mine:
        return []
    out = []
    cand = con.execute("SELECT id, path, name, mtime FROM files WHERE size=? AND id<>? AND tier IN (1,3) ORDER BY mtime DESC LIMIT ?",
                       (f[0], fid, max(1, min(COPY_MAX, COPY_BYTES // f[0])))).fetchall()
    for fid2, p2, n2, mt in cand:
        if keep is not None and not keep(p2):
            continue
        if os.path.exists(p2) and content_sig(con, fid2) == mine:
            out.append({"path": p2, "name": n2, "mtime": mt, "size": f[0], "fid": fid2, "id": file_id(con, fid2)})
    return out[:limit]


def family_id(con, fid, member_fids):
    """the version-family id of a file whose family is `member_fids` (index rows: same-content copies and versions in the
    same folder; itself not required). The file's own family if it has one, else its members' (most common, then smallest),
    else a new one. Members without a family take it; a member in another family stays there (no merge). None when ids
    cannot be written (stops at the first row that cannot: the indexer holds the index)"""
    rows = []
    for m in dict.fromkeys([fid, *member_fids]):
        r = _row(con, m)
        if r is None:
            return None
        rows.append((m, r))
    fam = rows[0][1]["fam"]
    if not fam:
        have = [r["fam"] for _, r in rows if r["fam"]]
        fam = min(set(have), key=lambda x: (-have.count(x), x)) if have else "v_" + uuid.uuid4().hex[:16]
    try:
        free = [(fam, m) for m, r in rows if not r["fam"]]
        if free:
            con.executemany("UPDATE file_uid SET fam=? WHERE file=?", free)
            con.commit()
    except sqlite3.OperationalError:
        return None
    return fam
