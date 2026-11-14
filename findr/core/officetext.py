"""Word 97-2003 (.doc) text without LibreOffice: read the piece table (CLX) and pull each piece from the WordDocument stream.
Pieces are UTF-16LE, or 8-bit cp1252 when 'compressed'. Field codes ({HYPERLINK ...}) are dropped, their results kept.
Returns None when the file is not a readable Word 97+ document (encrypted, Word 6/95, damaged) -> caller falls back.
Pure Python + olefile: the same on Windows, macOS and Linux."""
import html as _html
import re
import struct

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"(?is)<(script|style)\b.*?</\1>")


def _strip_html(t):
    t = _SCRIPT.sub(" ", t)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>|</h\d>", "\n", t)
    t = _html.unescape(_TAG.sub(" ", t))
    return re.sub(r"[ \t ]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def sniff_text(path, limit=2_000_000):
    """files whose extension says Word/Excel/PowerPoint but whose bytes are HTML, MHT or plain text (common for documents
    exported by web systems). Returns text, or None when the bytes are a real binary office file."""
    try:
        raw = open(path, "rb").read(limit * 2)
    except OSError:
        return None
    if raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):   # UTF-32 (its BOM starts like UTF-16's)
        t = raw.decode("utf-32", "ignore")
    elif raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        t = raw.decode("utf-16", "ignore")
    elif raw[:4] == b"\xd0\xcf\x11\xe0" or b"\x00" in raw[:4096]:
        return None                                        # OLE container or other binary
    else:
        body = raw[3:] if raw[:3] == b"\xef\xbb\xbf" else raw
        try:
            t = body.decode("utf-8")
        except UnicodeDecodeError:
            t = body.decode("cp949", "ignore")
    head = t.lstrip()[:400].lower()
    if head.startswith("mime-version") or "content-type: multipart" in head:
        import email
        from email import policy
        msg = email.message_from_bytes(raw, policy=policy.default)
        parts = [p.get_content() for p in msg.walk() if p.get_content_type() in ("text/html", "text/plain")]
        t = "\n".join(_strip_html(x) if "<" in x else x for x in parts if isinstance(x, str))
    elif head.startswith(("<html", "<!doctype", "<?xml", "<meta", "<head")) or "<html" in head:
        t = _strip_html(t)
    return t[:limit]


_HWP_WIDE = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}   # controls that span 8 WCHARs


# 본 제품은 한글과컴퓨터의 한글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
def _hwp_para_text(data, out, limit, total=0):
    """PARA_TEXT records of one section's record stream -> out; returns the running length"""
    i = 0
    while i + 4 <= len(data):
        h = struct.unpack_from("<I", data, i)[0]; i += 4
        tag, size = h & 0x3FF, (h >> 20) & 0xFFF
        if size == 0xFFF:
            if i + 4 > len(data):
                break                    # a cut-off tail (a decrypted section ends in AES padding): keep what was read
            size = struct.unpack_from("<I", data, i)[0]; i += 4
        if tag == 67:                    # HWPTAG_PARA_TEXT
            rec = data[i:i + size]; j = 0; buf = []
            while j + 2 <= len(rec):
                c = struct.unpack_from("<H", rec, j)[0]
                if c < 32:
                    if c in _HWP_WIDE:
                        if c == 9:
                            buf.append("\t")
                        j += 16; continue
                    if c in (10, 13):
                        buf.append("\n")
                    j += 2; continue
                buf.append(chr(c)); j += 2
            s = "".join(buf).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")   # join surrogate pairs (emoji etc.)
            out.append(s); total += len(s)
            if total >= limit:
                break
        i += size
    return total


def hwp_text(path, limit=2_000_000):
    """HWP 5 (한글) text without LibreOffice: zlib-compressed BodyText sections, PARA_TEXT records (tables included).
    Distribution(배포용) documents: the ViewText sections are decrypted as the published format describes (hwpdist.py);
    if that fails, the unencrypted preview text (PrvText). Returns None for password-protected documents and anything
    not HWP 5 -> caller falls back."""
    try:
        import olefile
        import zlib
        if not olefile.isOleFile(str(path)):
            return None
        ole = olefile.OleFileIO(str(path))
        if not ole.exists("FileHeader"):
            return None
        fh = ole.openstream("FileHeader").read()
        if not fh.startswith(b"HWP Document File"):
            return None
        props = struct.unpack_from("<I", fh, 36)[0]
        if props & 0x2:                          # password: the text is not ours to read
            return None
        compressed, dist = bool(props & 0x1), bool(props & 0x4)
        storage = "ViewText" if dist else "BodyText"
        secs = sorted((e for e in ole.listdir() if len(e) == 2 and e[0] == storage and e[1].startswith("Section")),
                      key=lambda e: int(e[1][7:] or 0))
        out, total = [], 0
        for e in secs:
            try:
                data = ole.openstream(e).read()
                if dist:
                    from hwpdist import decrypt_viewtext
                    data = decrypt_viewtext(data)
                    if data is None:
                        continue
                if compressed:
                    data = zlib.decompress(data, -15)
                total = _hwp_para_text(data, out, limit, total)
            except Exception:
                if not dist:
                    raise
                continue   # one unreadable distribution section: the others still count
            if total >= limit:
                break
        if dist and not "".join(out).strip() and ole.exists("PrvText"):   # the preview the file keeps unencrypted
            out = [ole.openstream("PrvText").read().decode("utf-16-le", "ignore")]
        t = "".join(out)
    except Exception:
        return None
    return re.sub(r"\n{3,}", "\n\n", t)[:limit]


def doc_text(path, limit=2_000_000):
    try:
        import olefile
        if not olefile.isOleFile(str(path)):
            return None
        ole = olefile.OleFileIO(str(path))
        wd = ole.openstream("WordDocument").read()
        if len(wd) < 0x1AA or struct.unpack_from("<H", wd, 0)[0] not in (0xA5EC, 0xA5DC):
            return None
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        if flags & 0x0100:          # fEncrypted
            return None
        table = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table):
            return None
        tb = ole.openstream(table).read()
        fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
        clx = tb[fc_clx:fc_clx + lcb_clx]
        i = 0
        while i < len(clx) and clx[i] == 0x01:          # skip Prc (property modifiers)
            i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
        if i >= len(clx) or clx[i] != 0x02:
            return None
        lcb = struct.unpack_from("<I", clx, i + 1)[0]; plc = clx[i + 5:i + 5 + lcb]
        n = (lcb - 4) // 12                               # (n+1) CPs of 4 bytes + n PCDs of 8 bytes
        cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
        out = []
        for k in range(n):
            fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)[0]
            cnt = cps[k + 1] - cps[k]
            if fc & 0x40000000:
                off = (fc & 0x3FFFFFFF) // 2; out.append(wd[off:off + cnt].decode("cp1252", "ignore"))
            else:
                out.append(wd[fc:fc + 2 * cnt].decode("utf-16le", "ignore"))
        t = "".join(out)
    except Exception:
        return None
    # fields: \x13 instruction \x14 result \x15 -> result; cell/row marks -> tab/newline; other controls out
    res, depth, keep = [], 0, []
    for ch in t:
        if ch == "\x13":
            depth += 1; keep.append(False); continue
        if ch == "\x14" and depth:
            keep[-1] = True; continue
        if ch == "\x15" and depth:
            depth -= 1; keep.pop(); continue
        if depth and not keep[-1]:
            continue
        res.append(ch)
    t = "".join(res).replace("\r", "\n").replace("\x07", "\t").replace("\x0b", "\n").replace("\x0c", "\n")
    t = "".join(c for c in t if c in "\n\t" or ord(c) >= 32)
    return t[:limit]
