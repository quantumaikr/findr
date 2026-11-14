"""Reading one file now, for an assistant that asked for it (MCP read_file): what the index has not got.

  unreadable_reason(path)   why findr cannot read this file at all (HWP 3.x, a password, an encrypted Office file), or None.
                            The reason is returned to the caller instead of an empty text that looks like an empty file.
  docx_pages(path)          (pages, estimated): docProps/app.xml <Pages> when the saving program wrote it, else an estimate
  deep_read(path, max_chars) the text of the file read now; a scanned PDF is read by OCR for up to FINDR_DEEP_READ_PAGES pages
                            (200) and FINDR_DEEP_READ_SECONDS (180 s), a photo (HEIC too) by OCR, and the answer says how many
                            pages were read of how many. Indexing keeps its own, smaller limits (FINDR_OCR_FIRST_PAGES,
                            FINDR_OCR_DEEP_PAGES): a whole index must stay bounded in time and memory, one file asked for need not.
HEIC needs the optional pillow-heif (a dependency of findr; `pip install pillow-heif`); on macOS sips is the fallback."""
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HWP3_SIGNATURE = b"HWP Document File V3.00"
OOXML = (".docx", ".xlsx", ".pptx")
IMAGES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff", ".gif")
CHARS_PER_PAGE = 1500   # non-space characters on an A4 page of ordinary text: only for the estimate


def _env_num(name, default, cast=int):
    try:
        v = cast(os.environ.get(name) or default)
        return v if v > 0 else cast(default)
    except ValueError:
        return cast(default)


def _ext(path):
    return Path(str(path)).suffix.lower()


def unreadable_reason(path):
    """the reason findr cannot read this file's text (a sentence for the caller), or None when it can try"""
    ext = _ext(path)
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            f.seek(0)
            if ext in (".hwp", ".hwpx") or head.startswith(b"HWP Doc"):
                if f.read(len(HWP3_SIGNATURE)) == HWP3_SIGNATURE:
                    return "cannot read: HWP 3.x file (the old 1990s format; open it in Hangul and save as HWP 5 or PDF)"
            if ext == ".hwp" and head[:4] == b"\xd0\xcf\x11\xe0":
                import olefile
                ole = olefile.OleFileIO(str(path))
                try:
                    if ole.exists("FileHeader"):
                        fh = ole.openstream("FileHeader").read()
                        if fh.startswith(b"HWP Document File") and len(fh) >= 40 and struct.unpack_from("<I", fh, 36)[0] & 0x2:
                            return "cannot read: password-protected HWP file (remove the password in Hangul first)"
                finally:
                    ole.close()
            if ext in OOXML + (".doc", ".xls", ".ppt") and head[:4] == b"\xd0\xcf\x11\xe0":
                import olefile
                ole = olefile.OleFileIO(str(path))
                try:
                    if ole.exists("EncryptedPackage"):   # an OOXML file with a password is an OLE container, not a zip
                        return f"cannot read: password-protected {ext[1:]} file (remove the password first)"
                    if ext == ".doc" and ole.exists("WordDocument"):
                        wd = ole.openstream("WordDocument").read(16)
                        if len(wd) >= 12 and struct.unpack_from("<H", wd, 0x0A)[0] & 0x0100:
                            return "cannot read: password-protected doc file (remove the password first)"
                finally:
                    ole.close()
            if ext == ".pdf":
                try:
                    import pypdfium2 as pdfium
                    pdfium.PdfDocument(str(path)).close()
                except ImportError:
                    pass
                except Exception as e:
                    if "password" in str(e).lower():
                        return "cannot read: password-protected PDF (remove the password first)"
    except OSError:
        return None
    except Exception:
        return None
    return None


def _xml_text_chars(z, name):
    try:
        xml = z.read(name).decode("utf-8", "ignore")
    except KeyError:
        return 0
    return sum(len(re.sub(r"\s", "", t)) for t in re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", xml))


def docx_pages(path):
    """(pages, estimated) for a .docx, or None. Word writes <Pages> into docProps/app.xml when it saves; a file from another
    program may carry 0 or nothing: then the pages are estimated from the amount of text and marked as such"""
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return None
    try:
        try:
            m = re.search(r"<Pages>\s*(\d+)\s*</Pages>", z.read("docProps/app.xml").decode("utf-8", "ignore"))
            if m and int(m.group(1)) > 0:
                return int(m.group(1)), False
        except KeyError:
            pass
        chars = _xml_text_chars(z, "word/document.xml")
        if not chars:
            return None
        return max(1, math.ceil(chars / CHARS_PER_PAGE)), True
    finally:
        z.close()


def _index():
    try:
        from . import index
    except ImportError:
        import index
    return index


def _heic_to_png(path):
    """macOS: sips converts HEIC when pillow-heif is not installed. A temp PNG path, or None"""
    if sys.platform != "darwin":
        return None
    out = os.path.join(tempfile.mkdtemp(prefix="findr_heic_"), "p.png")
    try:
        subprocess.run(["sips", "-s", "format", "png", str(path), "--out", out], capture_output=True, timeout=60, check=True)
        return out if os.path.exists(out) else None
    except Exception:
        return None


def _ocr_image(path, ix, side):
    """(lines, reason): a photo or screenshot read by OCR; HEIC through pillow-heif, else sips, else a reason"""
    import numpy as np
    from PIL import Image
    ix.heif_on()
    tmp = None
    try:
        try:
            im = Image.open(path)
            im.load()
        except Exception:
            if _ext(path) not in (".heic", ".heif"):
                return [], "cannot read: not an image this PC can open"
            tmp = _heic_to_png(path)
            if not tmp:
                return [], "cannot read: HEIC needs the optional pillow-heif (pip install pillow-heif)"
            im = Image.open(tmp)
        im = im.convert("RGB"); im.thumbnail((side, side))
        return ix.ocr_lines(np.array(im)), None
    finally:
        if tmp:
            try:
                os.remove(tmp); os.rmdir(os.path.dirname(tmp))
            except OSError:
                pass


def _ocr_pdf(path, ix, side, max_pages, seconds, end=None):
    """(lines, pages_read, pages_total); `end` (time.time()) or now + seconds: checked before every page"""
    import numpy as np
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(path)); total = len(doc); lines, done = [], 0
    end = min(end or float("inf"), time.time() + seconds)
    try:
        for i in range(min(total, max_pages)):
            if time.time() > end:
                break
            pg = doc[i]
            lines += ix.ocr_lines(np.array(pg.render(scale=ix._page_scale(pg, side)).to_pil().convert("RGB")))
            done += 1
    finally:
        doc.close()
    return lines, done, total


def deep_read(path, max_chars=4000, seconds=None):
    """the file read now. Keys: text; unreadable (a reason, no text) ; pages / pages_estimated (docx); ocr {pages_read,
    pages_total} for a scan; note (what the caller should know when the text is short, partial or missing).
    Only what the indexer reads: never a secret (index.is_secret), never an online-only cloud file, never a kind of file
    findr reads by name only (its raw bytes may be a key: a VPN profile). Values that look like keys are masked as in the
    index. seconds: the time budget from now (FINDR_DEEP_READ_SECONDS, 180, when not given), checked before each page"""
    t_end = time.time() + (seconds if seconds else _env_num("FINDR_DEEP_READ_SECONDS", 180, float))
    out = {"text": ""}
    ix = _index()
    ext = ix.real_ext(Path(path))
    if ix.is_secret(Path(path)):
        out["note"] = "findr never reads key, certificate or password files"
        return out
    if ext not in ix.TEXT and ext not in ix.DOC and ext not in IMAGES:
        out["note"] = "findr does not read the contents of this kind of file (only its name)"
        return out
    try:
        if ix.cloud_only_path(str(path)):
            out["note"] = "an online-only cloud file: its content is not on this PC (reading it would download it)"
            return out
    except Exception:   # noqa: BLE001
        pass
    why = unreadable_reason(path)
    if why:
        out["unreadable"] = why
        out["note"] = why
        return out
    if ext == ".docx":
        pg = docx_pages(path)
        if pg:
            out["pages"], out["pages_estimated"] = pg
    text = ""
    if ext not in IMAGES:
        try:
            text = ix.extract(Path(path), max(max_chars, 20000)) or ""
        except Exception:
            text = ""
    if ext == ".pdf" and len(text.strip()) < 200:
        if ix.ocr_engine() == "none":
            out["note"] = "scanned PDF: no text recognizer installed (easyocr), so only the file's own text is returned"
        else:
            try:
                lines, done, total = _ocr_pdf(path, ix, ix.OCR_SIDE, _env_num("FINDR_DEEP_READ_PAGES", 200),
                                              max(0.0, t_end - time.time()), end=t_end)
                text = "\n".join(lines) or text
                out["ocr"] = {"pages_read": done, "pages_total": total}
                if done < total:
                    out["note"] = (f"scanned PDF: OCR read {done} of {total} pages (limits FINDR_DEEP_READ_PAGES, "
                                   "FINDR_DEEP_READ_SECONDS / FINDR_MCP_DEEP_READ_SECONDS)")
                else:
                    out["note"] = f"scanned PDF: OCR read all {total} pages"
            except Exception:
                out["note"] = "scanned PDF: OCR failed"
    elif ext in IMAGES:
        if ix.ocr_engine() == "none":
            out["note"] = "no text recognizer installed (easyocr)"
        else:
            lines, why = _ocr_image(path, ix, ix.OCR_SIDE)
            if why:
                out["unreadable"] = why; out["note"] = why
            else:
                text = "\n".join(lines)
                out["ocr"] = {"pages_read": 1, "pages_total": 1}
    text = ix.mask(text)   # as index.chunks masks what it stores
    out["text"] = text[:max_chars]
    out["truncated"] = len(text) > max_chars
    return out


def main(argv=None):
    """python -m findr.core.deepread PATH [MAX_CHARS] [SECONDS]: deep_read in a process of its own (the MCP server's OCR,
    so the recognizer leaves with it); one line of JSON on standard output"""
    import json
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        got = deep_read(a[0], int(a[1]) if len(a) > 1 else 4000, float(a[2]) if len(a) > 2 else None)
    except Exception as e:   # noqa: BLE001
        got = {"text": "", "note": f"the file could not be read now ({type(e).__name__})"}
    sys.stdout.write("\n" + json.dumps(got, ensure_ascii=True) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
