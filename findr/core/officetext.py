"""Word 97-2003 (.doc) text without LibreOffice: read the piece table (CLX) and pull each piece from the WordDocument stream.
Pieces are UTF-16LE, or 8-bit cp1252 when 'compressed'. Field codes ({HYPERLINK ...}) are dropped, their results kept.
Returns None when the file is not a readable Word 97+ document (encrypted, Word 6/95, damaged) -> caller falls back.
Pure Python + olefile: the same on Windows, macOS and Linux."""
import html as _html
import re
import struct

_SCRIPT = re.compile(r"(?is)<(script|style)\b.*?</\1>")


# --- HTML as a reader sees it (red-team 0.2.26): a tag is not a space ('Pass<b>word</b>' is one word), entities are
# decoded ('password&#58; x'), and a table is rows of tab-separated cells, so the credential-table rule sees its header
_HTML_TOK = re.compile(r"<!--.*?(?:-->|$)|<(/?)([A-Za-z][A-Za-z0-9:\-]*)\b[^>]*>|<[!?/][^>]*>|([^<]+)|<", re.S)
_HTML_BLOCK = {"p", "div", "li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "table", "caption", "section", "article", "header", "footer", "nav", "aside", "main", "blockquote", "pre",
               "hr", "form", "fieldset", "legend", "title", "address", "figure", "figcaption", "details", "summary", "body",
               "html", "head", "option", "select", "textarea", "center"}


def html_text(t):
    """HTML -> text: script / style / comments dropped; inline tags joined without a space (as a browser shows them);
    block tags and <br> end a line; a table row is one line of tab-separated cells; entities decoded after the tags are
    gone; runs of spaces in the source collapsed except inside <pre> / <textarea>"""
    t = _SCRIPT.sub(" ", t or "")
    out, pre = [], 0
    for m in _HTML_TOK.finditer(t):
        close, name, text = m.group(1), m.group(2), m.group(3)
        if text is not None:
            s = _html.unescape(text)
            out.append(s if pre else re.sub(r"\s+", " ", s))
            continue
        if not name:
            if m.group(0) == "<":
                out.append("<")
            continue
        n = name.lower()
        if n in ("pre", "textarea"):
            pre = max(0, pre + (-1 if close else 1))
        if n == "br":
            out.append("\n")
        elif n in ("td", "th"):
            if close:
                out.append("\t")
        elif n == "tr":   # one line a row: no blank line between rows (a blank line ends a table)
            _row_end(out) if close else _line(out)
        elif n in _HTML_BLOCK:
            _line(out)
    return tidy_rows("".join(out).replace("\r", ""))


def _line(out):
    """a line break unless the text already ends one (spaces after it aside)"""
    while out and out[-1] in (" ", ""):
        out.pop()
    if out and not out[-1].endswith("\n"):
        out.append("\n")


def _row_end(out):
    """a table row's end: the tab after its last cell becomes the line break"""
    while out and out[-1] in (" ", ""):
        out.pop()
    if out and out[-1] == "\t":
        out[-1] = "\n"
    else:
        _line(out)


def tidy_rows(s):
    """spaces around tabs and line ends dropped, runs of spaces and blank lines made one"""
    s = re.sub(r"[ \f\v ]*\t[ \f\v ]*", "\t", s)
    s = re.sub(r"[ \f\v ]*\n[ \f\v ]*", "\n", s)
    s = re.sub(r"[ \f\v]{2,}", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# --- Office XML (docx, pptx, xlsx, hwpx, odt / odp) read in order (red-team 0.2.26: every tag read as a space split
# 'Pass</w:t><w:t>word' into two words, and a label passed unmasked). Runs inside one paragraph join without a space;
# paragraphs and line breaks end a line; a table is rows of tab-separated cells (so the credential-table rule sees it)
_XTOK = re.compile(r"<(/?)(?:[A-Za-z_][\w.\-]*:)?([A-Za-z_][\w.\-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>|<[^>]*>|([^<]+)")
_X_PARA = {"p", "h"}                                  # w:p, a:p, hp:p, text:p, text:h
_X_CELL = {"tc", "table-cell", "covered-table-cell"}  # w:tc, a:tc, hp:tc, table:table-cell
_X_ROW = {"tr", "table-row"}                          # w:tr, a:tr, hp:tr, table:table-row
_X_BREAK = {"br", "cr", "lineBreak", "line-break"}    # w:br, a:br, hp:lineBreak, text:line-break
_X_TEXT = {"t", "delText"}                            # where the text is (w:t, a:t, hp:t); OpenDocument: the paragraph itself
_X_SKIP = {"tabs", "rPh", "instrText"}                # tab stops (not tab characters), phonetic guides, field codes


class XmlFlow:
    """the text of one Office XML part, fed in pieces that each end at a tag's end; .text() when done. odf: the text is
    the paragraph's own content (OpenDocument) rather than inside <t> elements"""

    def __init__(self, odf=False):
        self.out, self.odf = [], odf
        self.cell = self.intext = self.para = self.skip = self.size = 0

    def feed(self, piece):
        out = self.out
        for m in _XTOK.finditer(piece):
            close, name, attrs, selfc, text = m.groups()
            if text is not None:
                if self.skip or not (self.intext or (self.odf and self.para)):
                    continue   # the indentation of pretty-printed XML is not text
                s = _html.unescape(text)
                s = re.sub(r"[\r\n\t]+", " ", s) if not self.odf else re.sub(r"\s+", " ", s)
                out.append(s); self.size += len(s)
                continue
            if name is None:
                continue
            opening, ending = not close and not selfc, bool(close or selfc)
            if name in _X_SKIP:
                if not selfc:
                    self.skip = max(0, self.skip + (-1 if close else 1))
            elif name in _X_TEXT:
                if not selfc:
                    self.intext = max(0, self.intext + (-1 if close else 1))
            elif name in _X_PARA:
                if not selfc:
                    self.para = max(0, self.para + (-1 if close else 1))
                if ending:
                    out.append(" " if self.cell else "\n")
            elif name in _X_CELL:
                if ending:
                    out.append("\t")
                if not selfc:
                    self.cell = max(0, self.cell + (-1 if close else 1))
            elif name in _X_ROW:   # one line a row: no blank line between rows (a blank line ends a table)
                if close:
                    _row_end(out)
                elif opening:
                    _line(out)
            elif self.skip:
                continue
            elif name in _X_BREAK and (opening or selfc):
                out.append(" " if self.cell else "\n")
            elif name == "tab" and (opening or selfc):
                out.append(" " if self.cell else "\t")
            elif name == "s" and self.odf and (opening or selfc):   # text:s, one space or text:c of them
                c = re.search(r"\bc\s*=\s*[\"'](\d+)", attrs or "")
                out.append(" " * min(int(c.group(1)) if c else 1, 64))

    def text(self):
        return tidy_rows("".join(self.out))


def _col_index(ref):
    """'C12' -> 2 (the column letters as a 0-based index); None when there are none"""
    m = re.match(r"([A-Za-z]{1,3})", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


XLSX_COLS = 512   # columns of one sheet row laid out by position at most (empty cells between them kept as empty columns)


class SheetFlow:
    """an xlsx worksheet's rows as tab-separated lines: shared strings looked up, inline strings and values as they are;
    a formula's text is not a value. Fed in pieces like XmlFlow"""

    def __init__(self, shared):
        self.shared, self.lines, self.size = shared, [], 0
        self.row = self.cell = self.where = None
        self.ctype, self.col = "", None

    def feed(self, piece):
        for m in _XTOK.finditer(piece):
            close, name, attrs, selfc, text = m.groups()
            if text is not None:
                if self.cell is not None and self.where:
                    self.cell.append(_html.unescape(text))
                continue
            if name == "row":
                if not close:
                    self.row = []
                if close or selfc:
                    self._end_row()
            elif name == "c":
                if self.row is None:
                    self.row = []
                if not close:
                    r = re.search(r"\br\s*=\s*[\"']([A-Za-z]+)", attrs or "")
                    t = re.search(r"\bt\s*=\s*[\"'](\w+)", attrs or "")
                    self.ctype = t.group(1) if t else ""
                    self.col = _col_index(r.group(1)) if r else None
                    self.cell = []
                if close or selfc:
                    self._end_cell()
            elif name in ("v", "t") and self.cell is not None and not selfc:
                self.where = None if close else name
            elif name in ("f", "rPh") and not (close or selfc):
                self.where = None

    def _end_cell(self):
        v = "".join(self.cell or [])
        if self.ctype == "s":
            try:
                v = self.shared[int(v.strip())]
            except (ValueError, IndexError):
                v = ""
        v = re.sub(r"\s+", " ", v).strip()
        col = self.col if self.col is not None and len(self.row) <= self.col < XLSX_COLS else len(self.row)
        self.row += [""] * (col - len(self.row)) + [v]
        self.cell = self.where = None

    def _end_row(self):
        while self.row and not self.row[-1]:
            self.row.pop()
        if self.row:
            line = "\t".join(self.row); self.lines.append(line); self.size += len(line) + 1
        self.row = None

    def text(self):
        if self.row:
            self._end_row()
        return "\n".join(self.lines)


class SharedStrings:
    """an xlsx sharedStrings part: one string per <si>, its runs joined without a space, phonetic guides left out"""

    def __init__(self):
        self.items, self.cur, self.where, self.skip, self.size = [], None, False, 0, 0

    def feed(self, piece):
        for m in _XTOK.finditer(piece):
            close, name, attrs, selfc, text = m.groups()
            if text is not None:
                if self.cur is not None and self.where and not self.skip:
                    self.cur.append(_html.unescape(text))
                continue
            if name == "si":
                if not close:
                    self.cur = []
                if close or selfc:
                    s = "".join(self.cur or []); self.items.append(s); self.size += len(s); self.cur = None
            elif name == "t" and not selfc:
                self.where = not close
            elif name == "rPh" and not selfc:
                self.skip = max(0, self.skip + (-1 if close else 1))


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
        t = "\n".join(html_text(x) if "<" in x else x for x in parts if isinstance(x, str))
    elif head.startswith(("<html", "<!doctype", "<?xml", "<meta", "<head")) or "<html" in head:
        t = html_text(t)
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
