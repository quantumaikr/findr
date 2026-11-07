"""More formats without LibreOffice: old Excel / OpenDocument sheets (python-calamine), RTF, e-mail (.eml).
Each returns text or None; None means 'could not read', so the caller can fall back (LibreOffice for .xls)."""
import re


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


def eml_text(path, limit=2_000_000):
    """e-mail file: subject, sender, recipients, date, body (plain, or HTML without tags) and attachment names"""
    import email
    from email import policy
    try:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
    except Exception:
        return None
    head = [f"{k}: {msg[k]}" for k in ("Subject", "From", "To", "Cc", "Date") if msg[k]]
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = part.get_content()
            if part.get_content_subtype() == "html":
                body = re.sub(r"(?s)<(script|style).*?</\1>", " ", body)
                body = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", body, flags=re.I)
                import html as _html
                body = _html.unescape(re.sub(r"<[^>]+>", " ", body))
    except Exception:
        body = ""
    files = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    t = "\n".join(head + ([""] + [body.strip()] if body.strip() else []) + (["", "첨부: " + ", ".join(files)] if files else []))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n\s*\n+", "\n\n", t))[:limit]
