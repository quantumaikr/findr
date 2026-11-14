"""More formats without LibreOffice: old Excel / OpenDocument sheets (python-calamine), RTF, e-mail (.eml, .emlx, .mbox).
Each returns text or None; None means 'could not read', so the caller can fall back (LibreOffice for .xls)."""
import re
from pathlib import Path


def sheet_text(path, limit=2_000_000):
    """.xls / .xlsb / .ods: every sheet's cells, one row per line (python-calamine, a Rust reader; no LibreOffice)"""
    try:
        from python_calamine import CalamineWorkbook
    except ImportError:
        return None
    try:
        wb = CalamineWorkbook.from_path(str(path))
        out, size = [], 0
        for name in wb.sheet_names:
            out.append(f"[{name}]")
            for row in wb.get_sheet_by_name(name).to_python(skip_empty_area=True):
                cells = [str(c).strip() for c in row if c not in ("", None)]
                if cells:
                    line = "\t".join(cells); out.append(line); size += len(line)
                    if size >= limit:
                        return "\n".join(out)[:limit]
        return "\n".join(out)
    except Exception:
        return None


# RTF: groups whose content is not document text
_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "themedata", "colorschememapping", "datastore",
             "latentstyles", "listtable", "listoverridetable", "rsidtbl", "generator", "xmlnstbl", "mmathPr", "pgdsctbl",
             "filetbl", "revtbl", "header", "footer", "headerl", "headerr", "footerl", "footerr", "fldinst", "bkmkstart", "bkmkend"}
_RTF_TOKEN = re.compile(rb"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+", re.S)


def rtf_text(path, limit=2_000_000):
    """RTF -> plain text: \\ansicpgN code page for \\'hh bytes (cp949 for Korean), \\uN with \\ucN fallbacks, paragraphs and cells"""
    try:
        raw = open(path, "rb").read(20_000_000)
    except OSError:
        return None
    if not raw.lstrip().startswith(b"{\\rtf"):
        return None
    m = re.search(rb"\\ansicpg(\d+)", raw[:4096])
    cp = f"cp{int(m.group(1))}" if m else "cp1252"
    try:
        "".encode(cp)
    except LookupError:
        cp = "cp1252"
    out, pend = [], bytearray()            # pend: \'hh bytes waiting to be decoded together (a Korean letter is two)
    stack, skip, uc, drop = [], False, 1, 0   # drop: fallback characters to swallow after \uN
    size = [0]                             # running length: summing out on every token made big files quadratic

    def put(t):
        out.append(t); size[0] += len(t)

    def flush():
        if pend:
            put(pend.decode(cp, "replace")); pend.clear()

    for word, num, hexb, sym, brace, text in (m.groups() for m in _RTF_TOKEN.finditer(raw)):
        if brace == b"{":
            flush(); stack.append((skip, uc, drop)); continue
        if brace == b"}":
            flush()
            if stack:
                skip, uc, drop = stack.pop()   # fallbacks left unswallowed inside a group do not eat the next group's text
            continue
        if hexb is not None:
            if drop:
                drop -= 1; continue
            if not skip:
                pend.append(int(hexb, 16))
            continue
        if word is not None:
            flush(); w = word.decode()
            if w in _RTF_SKIP:
                skip = True
            elif skip:
                pass
            elif w == "uc" and num is not None:
                uc = int(num)
            elif w == "u" and num is not None:
                n = int(num); put(chr(n + 65536 if n < 0 else n)); drop = uc
            elif w in ("par", "line", "row", "sect", "page"):
                put("\n")
            elif w in ("tab", "cell"):
                put("\t")
            continue
        if sym is not None:
            flush()
            if sym == b"*":
                skip = True            # {\* ...}: an optional destination this reader does not know
            elif not skip and sym in (b"\\", b"{", b"}"):
                put(sym.decode())
            elif not skip and sym == b"~":
                put(" ")
            continue
        if text is not None and not skip:
            flush()
            t = text.decode(cp, "replace")
            if drop:
                k = min(drop, len(t)); t = t[k:]; drop -= k
            put(t)
        if size[0] > limit * 2:
            break
    flush()
    # \uN gives UTF-16 units: join surrogate pairs (emoji, rare Hanja) instead of keeping lone halves
    t = "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()[:limit]


# --- e-mail: .eml, Apple Mail .emlx, and .mbox (Apple Mail export "X.mbox/mbox", Thunderbird) ---------------------------------
MAIL_MAX_BYTES = 100_000_000     # read from one .eml / .emlx; one message inside an .mbox
MAIL_MAX_DEPTH = 12              # multipart nesting followed (real mail is 3-5 deep; a hostile 1,200-deep one is not parsed)
MAIL_MAX_PARTS = 300             # parts looked at in one message
MAIL_MAX_ATTACH = 10             # attachments whose text is read, per message
MAIL_ATTACH_BYTES = 30_000_000   # an attachment larger than this is named only
MAIL_ATTACH_CHARS = 100_000      # text kept per attachment
MBOX_MAX_MESSAGES = 5000
MBOX_MAX_BYTES = 512_000_000     # read from one .mbox file
MBOX_MESSAGE_CHARS = 60_000      # text kept per message in an .mbox, so one long message does not use up the file's share
MBOX_SECONDS = 45
# Korean senders label cp949 text (which has the 8,822 syllables KS X 1001 / euc-kr lacks, e.g. 똠) as euc-kr, ks_c_5601-1987 ...
_KO_CHARSET = re.compile(rb"(?i)(=\?|charset\s*=\s*[\"']?)(euc-?kr|ks_c_5601(?:-1987)?|x-windows-949|ks_c_5601_1987)(?=[?\"'; \r\n]|$)")
_ATT_EXT = {".pdf", ".docx", ".xlsx", ".pptx", ".hwpx", ".txt", ".md", ".csv", ".tsv", ".log", ".html", ".htm", ".xml", ".json",
            ".hwp", ".doc", ".xls", ".xlsb", ".ods", ".rtf"}   # nothing nested (.eml, .zip) and no LibreOffice conversion (.ppt)


def _parse_mail(raw):
    """bytes of one RFC 822 message -> email.message.EmailMessage, or None (hostile nesting, undecodable)"""
    import email
    from email import policy
    if len(raw) > MAIL_MAX_BYTES:
        raw = raw[:MAIL_MAX_BYTES]
    if len(re.findall(rb"(?im)^content-type:\s*multipart/", raw)) > MAIL_MAX_PARTS:   # cheap check before the parser goes deep
        return None
    raw = _KO_CHARSET.sub(lambda m: m.group(1) + b"cp949", raw)
    try:
        return email.message_from_bytes(raw, policy=policy.default)
    except Exception:   # includes RecursionError
        return None


def _hdr(msg, k):
    try:
        v = msg[k]
        return re.sub(r"\s+", " ", str(v)).strip() if v else ""
    except Exception:
        return ""


def _html_text(body):
    import html as _html
    body = re.sub(r"(?s)<(script|style).*?</\1>", " ", body)
    body = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", body, flags=re.I)
    return _html.unescape(re.sub(r"<[^>]+>", " ", body))


def _body_text(part):
    try:
        return part.get_content()
    except Exception:
        try:
            b = part.get_payload(decode=True) or b""
            return b.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            return ""


def _walk(msg):
    """(body part, attachments) of a message, without recursion: depth and part counts are capped"""
    plain = html = None
    atts = []
    stack, seen = [(msg, 0)], 0
    while stack and seen < MAIL_MAX_PARTS:
        part, depth = stack.pop()
        seen += 1
        try:
            if part.is_multipart():
                if depth < MAIL_MAX_DEPTH:
                    stack.extend((c, depth + 1) for c in reversed(part.get_payload()))
                continue
            ctype = part.get_content_type()
            fn = part.get_filename()
            if fn or part.get_content_disposition() == "attachment":
                atts.append((fn or "", part))
            elif ctype == "text/plain" and plain is None:
                plain = part
            elif ctype == "text/html" and html is None:
                html = part
        except Exception:
            continue
    return plain or html, atts


def _attachment_text(name, part, deadline=None):
    """text inside an attachment, read by the same readers as a file of that kind; "" when unsupported, too big or unreadable"""
    import tempfile
    ext = Path(name).suffix.lower()
    if ext not in _ATT_EXT:
        return ""
    try:
        data = part.get_payload(decode=True)
        if not data or len(data) > MAIL_ATTACH_BYTES:
            return ""
        with tempfile.TemporaryDirectory(prefix="findr-att-") as d:
            f = Path(d) / ("att" + ext)
            f.write_bytes(data)
            if ext in (".hwp", ".doc"):
                try:
                    from .officetext import hwp_text, doc_text
                except ImportError:
                    from officetext import hwp_text, doc_text
                fn = hwp_text if ext == ".hwp" else doc_text
            elif ext in (".xls", ".xlsb", ".ods"):
                fn = sheet_text
            elif ext == ".rtf":
                fn = rtf_text
            else:
                try:
                    from .index import extract as fn
                except ImportError:
                    from index import extract as fn
            return (fn(f, MAIL_ATTACH_CHARS) or "").strip()[:MAIL_ATTACH_CHARS]
    except Exception:
        return ""


def _message_text(msg, limit, attachments=True):
    head = [f"{k}: {v}" for k in ("Subject", "From", "To", "Cc", "Date") if (v := _hdr(msg, k))]
    # thread headers, compact: lets a mail be tied to the conversation it belongs to
    head += [f"{k}: {v[:1000]}" for k in ("Message-ID", "In-Reply-To", "References") if (v := _hdr(msg, k))]
    body = ""
    part, atts = _walk(msg)
    if part is not None:
        body = _body_text(part)
        if part.get_content_subtype() == "html":
            body = _html_text(body)
    names = [n for n, _ in atts if n]
    blocks = []
    if attachments:
        for n, a in atts[:MAIL_MAX_ATTACH]:
            t = _attachment_text(n, a)
            if t:
                blocks.append(f"[첨부 {n}]\n{t}")
    t = "\n".join(head + ([""] + [body.strip()] if body.strip() else []) + (["", "첨부: " + ", ".join(names)] if names else [])
                  + ([""] + blocks if blocks else []))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n\s*\n+", "\n\n", t))[:limit]


def eml_text(path, limit=2_000_000):
    """e-mail file: subject, sender, recipients, date, thread headers, body (plain, or HTML without tags), attachment names
    and the text inside attachments. None when the file cannot be parsed (the caller then indexes the name only)"""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAIL_MAX_BYTES)
    except OSError:
        return None
    msg = _parse_mail(raw)
    if msg is None:
        return None
    try:
        return _message_text(msg, limit)
    except Exception:
        return None


def emlx_text(path, limit=2_000_000):
    """Apple Mail .emlx: a first line with the byte count, then that many bytes of RFC 822, then an XML plist (flags) that is dropped"""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAIL_MAX_BYTES + 64)
    except OSError:
        return None
    first, _, rest = raw.partition(b"\n")
    try:
        n = int(first.strip())
        rest = rest[:n]
    except ValueError:
        rest = raw   # no count line: a bare RFC 822 message saved as .emlx
    msg = _parse_mail(rest)
    if msg is None:
        return None
    try:
        return _message_text(msg, limit)
    except Exception:
        return None


def _mbox_messages(f):
    """raw messages of an mbox file, streamed: a 'From ' line that follows a blank line (or starts the file) begins the next one"""
    buf, prev_blank, size, total, count = [], True, 0, 0, 0
    for line in f:
        total += len(line)
        if line.startswith(b"From ") and prev_blank:
            if buf:
                yield b"".join(buf); count += 1
            buf, size = [], 0
            if count >= MBOX_MAX_MESSAGES or total > MBOX_MAX_BYTES:
                return
            prev_blank = False
            continue   # the envelope line is not part of the message
        prev_blank = line in (b"\n", b"\r\n")
        if size < MAIL_MAX_BYTES:
            buf.append(line[1:] if line.startswith(b">From ") else line); size += len(line)
    if buf:
        yield b"".join(buf)


def mbox_text(path, limit=2_000_000):
    """an .mbox file (Thunderbird, or Apple Mail's X.mbox/mbox) is ONE index entry: its messages' texts in order, joined by a blank line,
    up to `limit`. The index has one row per file and no sub-entries, so the file is found by any of its messages; each message
    is capped (MBOX_MESSAGE_CHARS) so a long one does not crowd out the rest. Unparsable messages are skipped."""
    import time
    deadline = time.monotonic() + MBOX_SECONDS
    out, n = [], 0
    try:
        with open(path, "rb") as f:
            for raw in _mbox_messages(f):
                msg = _parse_mail(raw)
                if msg is not None:
                    try:
                        t = _message_text(msg, MBOX_MESSAGE_CHARS)
                    except Exception:
                        t = ""
                    if t:
                        out.append(t); n += len(t) + 2
                if n >= limit or time.monotonic() > deadline:
                    break
    except OSError:
        return None
    return "\n\n".join(out)[:limit] if out else None
