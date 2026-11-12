"""Where a file came from, as the PC itself recorded it when the file was saved: no language model, nothing leaves the PC.
  macOS    com.apple.metadata:kMDItemWhereFroms  the addresses a browser (or Mail) saved the file from
           com.apple.quarantine                  the app that wrote the file, and when
  Windows  the Zone.Identifier stream            HostUrl / ReferrerUrl written by the browser
A file is recorded only when it has a source address or was written by a mail or messenger app. Files unpacked from a
downloaded archive carry the download mark too (most files on a test PC did), and would make nearly every file 'downloaded'.
Rows go to events (type saved_from_mail / saved_from_messenger / downloaded, source 'pc:<site or app>', no person).
These records are partial (mail saved from a desktop mail app often leaves no address), so search uses them to lift files
that match, never to say a file is not there (episodic.py).   python origin.py DB"""
import datetime as dt
import os
import sqlite3
import sys
from urllib.parse import urlparse

PC = "pc:"   # source prefix of rows this module writes (other rows come from fuller records, e.g. a synthetic PC)
MAIL_HOSTS = ("mail.google.com", "mail-attachment.googleusercontent.com", "outlook.live.com", "outlook.office.com",
              "outlook.office365.com", "attachments.office.net", "mail.naver.com", "mail.daum.net", "mail.nate.com",
              "mail.yahoo.com", "mail.zoho.com", "mail.worksmobile.com", "mail.hiworks.com", "mail.kakao.com")
MAIL_APPS = {"mail", "com.apple.mail", "microsoft outlook", "outlook", "spark", "thunderbird", "airmail", "mailspring"}
MSG_HOSTS = ("kakao", "slack", "telegram", "whatsapp", "discord", "line.me", "teams.microsoft.com", "jandi", "dooray")
MSG_APPS = {"kakaotalk", "카카오톡", "slack", "telegram", "whatsapp", "discord", "line", "microsoft teams", "teams", "jandi",
            "nateon", "dooray", "naver works", "signal", "wechat"}


def _host_is(h, names):
    return any(h == n or h.endswith("." + n) for n in names)


def classify(urls, agent=None):
    """(event type, label) from the recorded addresses and the writing app, or None when nothing says where it came from"""
    urls = [u for u in (urls or []) if isinstance(u, str)]
    a = (agent or "").replace("\\x20", " ").strip().lower()
    if any(u.startswith("message:") for u in urls) or a in MAIL_APPS:
        return "saved_from_mail", "Mail" if any(u.startswith("message:") for u in urls) else agent.replace("\\x20", " ")
    hosts = [(urlparse(u).hostname or "").lower() for u in urls if u.startswith(("http://", "https://"))]
    for h in hosts:
        if _host_is(h, MAIL_HOSTS):
            return "saved_from_mail", h
    for h in hosts:
        if any(m in h for m in MSG_HOSTS):
            return "saved_from_messenger", h
    if a in MSG_APPS:
        return "saved_from_messenger", agent.replace("\\x20", " ")
    if hosts and hosts[0]:
        return "downloaded", hosts[0]
    return None


if sys.platform == "darwin":
    import ctypes, ctypes.util
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    _libc.getxattr.restype = ctypes.c_ssize_t

    def _xattr(path, name):
        b = os.fsencode(path); n = _libc.getxattr(b, name, None, 0, 0, 0)
        if n <= 0:
            return None
        buf = ctypes.create_string_buffer(n); n = _libc.getxattr(b, name, buf, n, 0, 0)
        return buf.raw[:n] if n > 0 else None
else:
    def _xattr(path, name):
        return None


def _when(qt, path):
    """the time the file was saved here: the quarantine stamp, else the file's birth or last-saved time"""
    try:
        if qt:
            return dt.datetime.fromtimestamp(int(qt, 16)).isoformat(timespec="minutes")
    except (ValueError, OverflowError, OSError):
        pass
    try:
        st = os.stat(path)
        return dt.datetime.fromtimestamp(getattr(st, "st_birthtime", None) or st.st_mtime).isoformat(timespec="minutes")
    except OSError:
        return None


def zone_urls(text):
    """HostUrl / ReferrerUrl from a Windows Zone.Identifier stream ([ZoneTransfer] ZoneId=3 ...)"""
    out = []
    for line in (text or "").splitlines():
        k, _, v = line.partition("=")
        if k.strip().lower() in ("hosturl", "referrerurl") and v.strip():
            out.append(v.strip())
    return sorted(out, key=lambda u: not u.lower().startswith("http"))   # addresses first, 'about:internet' last


def _long(path):
    """Windows: the \\\\?\\ form, so a stream of a file deeper than 260 characters can be opened (as index.py reads it)"""
    if path.startswith("\\\\?\\") or not os.path.isabs(path):
        return path
    p = os.path.normpath(path)
    return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p


def _zone(path):
    """the text of a file's Zone.Identifier stream (Windows), or '' when it has none"""
    try:
        with open(_long(path) + ":Zone.Identifier", encoding="utf-8", errors="replace") as f:
            return f.read(4096)
    except (OSError, ValueError):
        return ""


def read(path):
    """(event type, label, time) for one file, or None"""
    urls, agent, qt = [], None, None
    if sys.platform == "darwin":
        w = _xattr(path, b"com.apple.metadata:kMDItemWhereFroms")
        if w:
            try:
                import plistlib
                v = plistlib.loads(w); urls = v if isinstance(v, list) else []
            except Exception:
                urls = []
        q = _xattr(path, b"com.apple.quarantine")
        if q:
            parts = q.decode("utf-8", "replace").split(";")
            qt = parts[1] if len(parts) > 1 else None
            agent = parts[2] if len(parts) > 2 else None
    elif os.name == "nt":
        urls = zone_urls(_zone(path))
    elif hasattr(os, "getxattr"):   # Linux: Chrome/Chromium and others write the XDG source attributes
        for name in ("user.xdg.origin.url", "user.xdg.referrer.url"):
            try:
                urls.append(os.getxattr(path, name).decode("utf-8", "replace"))
            except OSError:
                pass
        # (WSL: a Windows drive's alternate streams are not visible through /mnt; those files stay unmarked)
    else:
        return None
    c = classify(urls, agent)
    return (c[0], c[1], _when(qt, path)) if c else None


def collect(con, commit_every=2000):
    """read the source of every file not looked at since it got its path; returns counts per event type"""
    con.executescript("""
      CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, file INT, type TEXT, time TEXT, person TEXT, source TEXT);
      CREATE INDEX IF NOT EXISTS ev_file ON events(file);
      CREATE TABLE IF NOT EXISTS origin_seen(file INTEGER PRIMARY KEY, path TEXT);""")
    con.execute("DELETE FROM origin_seen WHERE file NOT IN (SELECT id FROM files)")
    todo = con.execute("SELECT f.id, f.path, s.path FROM files f LEFT JOIN origin_seen s ON s.file=f.id "
                       "WHERE s.path IS NULL OR s.path<>f.path").fetchall()
    out = {"looked": len(todo)}
    for i, (fid, path, was) in enumerate(todo, 1):
        if was is not None:   # the id now holds another path (moved, or a reused id): its old source no longer holds
            con.execute("DELETE FROM events WHERE file=? AND source LIKE 'pc:%'", (fid,))
        r = read(path)
        if r:
            con.execute("INSERT INTO events(file, type, time, person, source) VALUES(?,?,?,NULL,?)", (fid, r[0], r[2], PC + r[1]))
            out[r[0]] = out.get(r[0], 0) + 1
        con.execute("INSERT OR REPLACE INTO origin_seen(file, path) VALUES(?,?)", (fid, path))
        if i % commit_every == 0:
            con.commit()
    con.commit()
    return out


if __name__ == "__main__":
    print(collect(sqlite3.connect(sys.argv[1])))
