"""Is this path inside that folder, however it is spelled? (red-team pass, 11/14)

Comparing path text misses spellings the OS takes for the same folder: macOS firmlinks (/System/Volumes/Data/Users/x is
/Users/x, and realpath does not say so), letter case on macOS and Windows, hard-linked or bind-mounted folders, Windows
short names (PROGRA~1), \\\\localhost\\C$\\..., \\\\?\\UNC\\..., and trailing dots or spaces (Windows drops them). The
folder itself answers: two spellings of one folder have the same (st_dev, st_ino). These helpers compare by that, and
give one text form for what can still only be compared as text (an index row from another PC)."""
import functools
import os
import re
import sys

FIRMLINK = "/System/Volumes/Data"


def unfirm(s):
    """macOS: '/System/Volumes/Data/<x>' -> '/<x>' when that is the same place (a firmlink). Unchanged elsewhere"""
    s = str(s)
    if sys.platform != "darwin" or not (s == FIRMLINK or s.startswith(FIRMLINK + "/")):
        return s
    rest = s[len(FIRMLINK):] or "/"
    try:
        if os.path.exists(rest) and (not os.path.exists(s) or os.path.samefile(rest, s)):
            return rest
    except OSError:
        pass
    return s


_LOCAL_SHARE = re.compile(r"^[\\/]{2}(?:localhost|127\.0\.0\.1|\[?::1\]?|\.)[\\/]([A-Za-z])\$(?=[\\/]|$)", re.I)


def win_text(s):
    """a Windows path's usual form: \\\\?\\C:\\ and \\\\?\\UNC\\ prefixes dropped, \\\\localhost\\C$\\x as C:\\x, and each
    part without the trailing dots and spaces Windows ignores ('Secret.\\' is 'Secret\\')"""
    s = str(s)
    if s[:8].upper() in ("\\\\?\\UNC\\", "//?/UNC/"):
        s = "\\\\" + s[8:]
    elif s[:4] in ("\\\\?\\", "//?/", "\\\\.\\", "//./") and re.match(r"^[A-Za-z]:", s[4:]):
        s = s[4:]
    m = _LOCAL_SHARE.match(s)
    if m:
        s = m.group(1).upper() + ":" + s[m.end():]
    lead = re.match(r"^([\\/]{2}|[A-Za-z]:[\\/]?|[\\/])?", s).group(0)
    parts = [p.rstrip(" .") if p not in (".", "..") else p for p in re.split(r"[\\/]+", s[len(lead):])]
    return lead.replace("/", "\\") + "\\".join(parts)


def long_name(s):
    """Windows: a short 8.3 name (PROGRA~1) as its long name, when the path exists; unchanged elsewhere"""
    if os.name != "nt" or "~" not in str(s):
        return s
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(str(s), buf, 32768)
        return buf.value if 0 < n < 32768 else s
    except Exception:   # noqa: BLE001
        return s


@functools.lru_cache(maxsize=65536)
def ident(p):
    """(st_dev, st_ino) of a folder or file (links followed), or None when it is not there or the file system has no ids"""
    try:
        st = os.stat(p)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def clear():
    ident.cache_clear()


def ancestors(p):
    """p (absolute) and each folder above it, nearest first"""
    try:
        a = os.path.abspath(str(p))
    except (OSError, ValueError):
        return []
    out = [a]
    while True:
        up = os.path.dirname(a)
        if not up or up == a:
            return out
        out.append(up); a = up


def ids_of(folders):
    """{(st_dev, st_ino): folder} for the folders that are there"""
    out = {}
    for d in folders:
        i = ident(str(d))
        if i is not None:
            out.setdefault(i, d)
    return out


def under(p, ids, follow=True):
    """the folder of `ids` that p is (or is inside), compared by identity, or None. With follow, where p really leads
    (links, junctions) is looked at too. Each folder above p is looked up once per clear()"""
    if not ids or not p:
        return None
    cands = ancestors(p)
    if follow:
        try:
            r = os.path.realpath(str(p))
            if os.path.normcase(r) != os.path.normcase(cands[0] if cands else ""):
                cands += ancestors(r)
        except (OSError, ValueError):
            pass
    for a in cands:
        i = ident(a)
        if i is not None and i in ids:
            return ids[i]
    return None


def variant(folder, root):
    """`folder` spelled the way `root` is when root is above it under another spelling (firmlink, case, link):
    root / (folder below the ancestor that is root), or None"""
    ri = ident(str(root))
    if ri is None:
        return None
    f = os.path.abspath(str(folder))
    for a in ancestors(f):
        if ident(a) == ri:
            rel = os.path.relpath(f, a)
            return os.path.join(str(root), rel) if rel != "." else str(root)
    return None


def split_list(s):
    """a list of folders as one setting (FINDR_MCP_DENY): ';' always, and os.pathsep; on macOS / Linux a Windows path
    (C:\\x) is not cut at its colon"""
    out = []
    for piece in re.split(r";", s or ""):
        if os.pathsep == ";":
            out.append(piece); continue
        bits = piece.split(os.pathsep)
        i = 0
        while i < len(bits):
            b = bits[i]
            if re.fullmatch(r"[A-Za-z]", b) and i + 1 < len(bits) and bits[i + 1][:1] in ("\\", "/"):
                b = b + ":" + bits[i + 1]; i += 1
            out.append(b); i += 1
    return [x.strip() for x in out if x.strip()]
