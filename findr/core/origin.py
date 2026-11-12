"""Where a file came from, as the PC itself recorded it when the file was saved: no language model, nothing leaves the PC.
  macOS    com.apple.metadata:kMDItemWhereFroms  the addresses a browser (or Mail) saved the file from
           com.apple.quarantine                  the app that wrote the file, and when
  Windows  the Zone.Identifier stream            HostUrl / ReferrerUrl written by the browser
  Linux    user.xdg.origin.url / referrer.url    written by Chromium-based browsers and others
A file is recorded only when it has a source address or was written by a mail or messenger app. Files unpacked from a
downloaded archive carry the download mark without an address (52,841 of 57,222 files on a test PC), and would make nearly
every file 'downloaded'.
Rows go to events: type saved_from_mail / saved_from_messenger / downloaded, source 'pc:<site or app>', no person (a
Mail.app record names the sender: it is not kept). These records are partial (a desktop mail app often leaves no address),
so search uses them to lift files that match, never to say a file is not there (episodic.py).
Safety: an online-only cloud file (OneDrive and similar on Windows) is never opened, since opening it can download it; each
run reads for at most FINDR_ORIGIN_SECS seconds (newest files first) and goes on where it stopped next time.
    python origin.py DB"""
import datetime as dt
import os
import re
import sqlite3
import sys
import time
from urllib.parse import urlparse

PC = "pc:"   # source prefix of the rows this module writes (other rows come from fuller records, e.g. a synthetic PC)
RECV = ("saved_from_mail", "saved_from_messenger", "downloaded")
# web mail: exact services, and the common 'mail.<domain>' / 'webmail.<domain>' hosts of company and school mail
MAIL_HOSTS = ("mail.google.com", "mail-attachment.googleusercontent.com", "outlook.live.com", "outlook.office.com",
              "outlook.office365.com", "attachments.office.net", "outlook.cloud.microsoft", "mail.yahoo.com",
              "mail.yahoo.co.jp", "mail.aol.com", "mail.proton.me", "mail.zoho.com", "mail.yandex.ru", "mail.yandex.com",
              "mail.qq.com", "exmail.qq.com", "mail.163.com", "mail.126.com", "mail.naver.com", "mail.daum.net",
              "mail.kakao.com", "mail.nate.com", "mail.worksmobile.com", "mail.hiworks.com", "fastmail.com", "gmx.net",
              "web.de", "mail.ru", "e.mail.ru")
MAIL_LABELS = {"mail", "webmail", "email", "owa", "outlook"}   # first label of a host: mail.example.org, webmail.example.ac.kr
MAIL_PATHS = (("icloud.com", "/mail"), ("www.icloud.com", "/mail"))   # a site whose mail lives under a path
MSG_HOSTS = ("slack.com", "slack-edge.com", "slack-files.com", "telegram.org", "t.me", "whatsapp.com", "whatsapp.net",
             "discord.com", "discordapp.com", "discordapp.net", "teams.microsoft.com", "teams.live.com", "line.me",
             "line-scdn.net", "jandi.com", "dooray.com", "talk.kakao.com", "wechat.com", "weixin.qq.com", "signal.org",
             "messenger.com", "zalo.me", "viber.com")
MAIL_APPS = {"mail", "com.apple.mail", "microsoft outlook", "outlook", "spark", "spark desktop", "thunderbird", "airmail",
             "mailspring", "canary mail", "mimestream", "postbox", "em client", "mailbird"}
MSG_APPS = {"kakaotalk", "카카오톡", "slack", "telegram", "telegram desktop", "whatsapp", "discord", "line", "microsoft teams",
            "teams", "jandi", "nateon", "dooray", "naver works", "signal", "wechat", "weixin", "viber", "zalo", "messenger",
            "skype", "zoom", "zoom.us"}
_ESC = re.compile(r"\\x([0-9a-fA-F]{2})")


def _app(agent):
    """the writing app's name as macOS stores it ('Microsoft\\x20Outlook' -> 'Microsoft Outlook')"""
    return _ESC.sub(lambda m: chr(int(m.group(1), 16)), agent or "").strip()


def _host_is(h, names):
    return any(h == n or h.endswith("." + n) for n in names)


def _is_mail(host, path):
    if _host_is(host, MAIL_HOSTS) or any(host == h and path.lower().startswith(p) for h, p in MAIL_PATHS):
        return True
    labels = host.split(".")
    return len(labels) >= 3 and labels[0] in MAIL_LABELS   # 'mail.example.com', not 'mail.com'-like product names


def classify(urls, agent=None):
    """(event type, label) from the recorded addresses and the writing app, or None when nothing says where it came from"""
    urls = [u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()]
    app = _app(agent); a = app.lower()
    if any(u.lower().startswith("message:") for u in urls):   # Mail.app: sender, subject and a message: link
        return "saved_from_mail", app or "Mail"
    if a in MAIL_APPS:
        return "saved_from_mail", app
    web = []
    for u in urls:
        try:
            p = urlparse(u)
        except ValueError:
            continue
        if p.scheme in ("http", "https") and p.hostname:
            web.append((p.hostname.lower().rstrip("."), p.path or ""))
    for h, path in web:
        if _is_mail(h, path):
            return "saved_from_mail", h
    for h, _ in web:
        if _host_is(h, MSG_HOSTS):
            return "saved_from_messenger", h
    if a in MSG_APPS:
        return "saved_from_messenger", app
    if web:
        return "downloaded", web[0][0]
    return None


if sys.platform == "darwin":
    import ctypes, ctypes.util
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    _libc.getxattr.restype = ctypes.c_ssize_t
    _NOFOLLOW = 0x0001   # XATTR_NOFOLLOW: a link's own record, never the target's

    def _xattr(path, name, limit=1 << 16):
        b = os.fsencode(path); n = _libc.getxattr(b, name, None, 0, 0, _NOFOLLOW)
        if n <= 0 or n > limit:   # none, or unreasonably large: not a source record
            return None
        buf = ctypes.create_string_buffer(n); n = _libc.getxattr(b, name, buf, n, 0, _NOFOLLOW)
        return buf.raw[:n] if n > 0 else None
elif hasattr(os, "getxattr"):
    def _xattr(path, name, limit=1 << 16):
        try:
            v = os.getxattr(path, name, follow_symlinks=False)
        except OSError:   # none, not supported by this file system, or gone
            return None
        return v if len(v) <= limit else None
else:
    def _xattr(path, name, limit=1 << 16):
        return None


def _long(path):
    """Windows: the \\\\?\\ form, so a stream of a file deeper than 260 characters can be opened (as index.py reads files)"""
    if path.startswith("\\\\?\\") or not os.path.isabs(path):
        return path
    p = os.path.normpath(path)
    return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p


_CLOUD = 0x00400000 | 0x00040000 | 0x00001000   # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE (index.cloud_only)


def _zone(path):
    """the text of a file's Zone.Identifier stream (Windows), '' when it has none; never opens an online-only cloud file"""
    p = _long(path)
    try:
        if getattr(os.stat(p), "st_file_attributes", 0) & _CLOUD:
            return ""
        with open(p + ":Zone.Identifier", "rb") as f:
            raw = f.read(8192)
    except (OSError, ValueError):
        return ""
    return _text(raw)


def _text(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):   # some tools write the stream as UTF-16
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8-sig", "replace")


def zone_urls(text):
    """HostUrl / ReferrerUrl from a Windows Zone.Identifier stream ([ZoneTransfer] ZoneId=3 ...): the page first, then the
    file's own address (the page names the service: mail.google.com rather than its attachment server)"""
    got = {}
    for line in (text or "").splitlines():
        k, _, v = line.partition("=")
        if k.strip().lower() in ("hosturl", "referrerurl") and v.strip():
            got[k.strip().lower()] = v.strip()
    return [got[k] for k in ("referrerurl", "hosturl") if k in got]


def _when(qt, st):
    """when the file was saved here: the quarantine stamp (macOS), else the file's creation time where the OS keeps one, else
    its last-saved time (Linux ctime is a metadata change, not creation)"""
    try:
        if qt:
            t = int(qt, 16)
            if 315532800 < t < time.time() + 86400 * 2:   # 1980 .. now: a damaged stamp is ignored
                return dt.datetime.fromtimestamp(t).isoformat(timespec="minutes")
    except (ValueError, OverflowError, OSError):
        pass
    if st is None:
        return None
    t = getattr(st, "st_birthtime", None) or (st.st_ctime if os.name == "nt" else None) or st.st_mtime
    try:
        return dt.datetime.fromtimestamp(t).isoformat(timespec="minutes")
    except (ValueError, OverflowError, OSError):
        return None


def read(path):
    """(event type, label, time) for one file, or None"""
    urls, agent, qt = [], None, None
    if sys.platform == "darwin":
        w = _xattr(path, b"com.apple.metadata:kMDItemWhereFroms")
        if w:
            try:
                import plistlib
                v = plistlib.loads(w); urls = v if isinstance(v, list) else []
            except Exception:   # a damaged record: as if there were none
                urls = []
        q = _xattr(path, b"com.apple.quarantine", limit=4096)
        if q:
            parts = q.decode("utf-8", "replace").split(";")
            qt = parts[1] if len(parts) > 1 else None
            agent = parts[2] if len(parts) > 2 else None
    elif os.name == "nt":
        urls = zone_urls(_zone(path))
    elif hasattr(os, "getxattr"):   # Linux. (WSL: a Windows drive's streams are not visible through /mnt; unmarked there)
        for name in ("user.xdg.referrer.url", "user.xdg.origin.url"):
            v = _xattr(path, name, limit=8192)
            if v:
                urls.append(v.decode("utf-8", "replace"))
    else:
        return None
    c = classify(urls, agent)
    if not c:
        return None
    try:
        st = os.stat(_long(path) if os.name == "nt" else path)
    except OSError:
        st = None
    return c[0], c[1][:200], _when(qt, st)


def collect(con, budget=None, commit_every=2000):
    """read the source of each file not looked at since it got its path, newest first, for at most `budget` seconds (the rest
    waits for the next run); returns counts: looked, left, and one per event type"""
    budget = float(os.environ.get("FINDR_ORIGIN_SECS", "20")) if budget is None else budget
    con.executescript("""
      CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, file INT, type TEXT, time TEXT, person TEXT, source TEXT);
      CREATE INDEX IF NOT EXISTS ev_file ON events(file);
      CREATE TABLE IF NOT EXISTS origin_seen(file INTEGER PRIMARY KEY, path TEXT);""")
    # files that left the index take their marks along (also where drop() predates the events table)
    con.execute("DELETE FROM origin_seen WHERE file NOT IN (SELECT id FROM files)")
    con.execute("DELETE FROM events WHERE source LIKE 'pc:%' AND file NOT IN (SELECT id FROM files)")
    todo = con.execute("SELECT f.id, f.path, s.path FROM files f LEFT JOIN origin_seen s ON s.file=f.id "
                       "WHERE s.path IS NULL OR s.path<>f.path ORDER BY f.mtime DESC").fetchall()
    out, t0, i = {"looked": 0}, time.monotonic(), 0
    for i, (fid, path, was) in enumerate(todo, 1):
        if was is not None:   # the id now holds another path (moved, or a reused id): its old source no longer holds
            con.execute("DELETE FROM events WHERE file=? AND source LIKE 'pc:%'", (fid,))
        try:
            r = read(path)
        except Exception:   # one strange file never stops the rest
            r = None
        if r:
            con.execute("INSERT INTO events(file, type, time, person, source) VALUES(?,?,?,NULL,?)", (fid, r[0], r[2], PC + r[1]))
            out[r[0]] = out.get(r[0], 0) + 1
        con.execute("INSERT OR REPLACE INTO origin_seen(file, path) VALUES(?,?)", (fid, path))
        out["looked"] = i
        if i % commit_every == 0:
            con.commit()
        if budget and time.monotonic() - t0 > budget:
            break
    con.commit()
    out["left"] = len(todo) - out["looked"]
    return out


if __name__ == "__main__":
    print(collect(sqlite3.connect(sys.argv[1]), budget=0))
