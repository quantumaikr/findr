"""#204: files findr cannot read say why; scans are read past 3 pages when one file is asked for; HEIC; docx pages"""
import struct
import zipfile

import olefile  # noqa: F401
import pypdfium2 as pdfium
import pytest

import deepread
import index


def make_ole(path, streams):
    """a small OLE2 compound file written by hand (olefile only reads): up to 3 streams, each padded to 4096 bytes so none
    lives in the mini stream"""
    END, FREE, FATSECT, NO = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF
    fat, data, entries, nxt = [FATSECT, END], b"", [], 2
    for i, (name, blob) in enumerate(streams.items()):
        blob = blob.ljust(max(4096, -(-len(blob) // 512) * 512), b"\0")
        n = len(blob) // 512
        fat += list(range(nxt + 1, nxt + n)) + [END]
        entries.append((name, 2, nxt, len(blob), i + 1))
        data += blob; nxt += n
    ents = [("Root Entry", 5, END, 0, 1 if streams else NO)]
    ents += entries
    d = b""
    for i, (name, typ, start, size, ref) in enumerate(ents):
        nm = (name + "\0").encode("utf-16-le")
        right = NO if typ == 5 or i == len(ents) - 1 else i + 1
        child = ref if typ == 5 else NO
        d += nm.ljust(64, b"\0") + struct.pack("<HBBIII", len(nm), typ, 1, NO, right, child) + b"\0" * 16 + b"\0" * 4 + b"\0" * 16 \
            + struct.pack("<IQ", start, size)
    d = d.ljust(512, b"\0")
    fat = (fat + [FREE] * 128)[:128]
    hdr = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6 \
        + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, END, 0, END, 0) + struct.pack("<I", 0) + struct.pack("<I", FREE) * 108
    open(path, "wb").write(hdr + struct.pack("<128I", *fat) + d + data)
    return path


def header(flags):
    return b"HWP Document File" + b"\0" * 15 + struct.pack("<II", 0x05000300, flags)


def test_hwp5_password_and_plain(tmp_path):
    p = make_ole(tmp_path / "pw.hwp", {"FileHeader": header(0x2)})
    assert "password-protected HWP" in deepread.unreadable_reason(p)
    assert deepread.deep_read(p)["unreadable"].startswith("cannot read")
    q = make_ole(tmp_path / "ok.hwp", {"FileHeader": header(0x1)})
    assert deepread.unreadable_reason(q) is None
    r = make_ole(tmp_path / "dist.hwp", {"FileHeader": header(0x4)})   # a distribution document is not a password
    assert deepread.unreadable_reason(r) is None


def test_hwp3(tmp_path):
    p = tmp_path / "old.hwp"; p.write_bytes(deepread.HWP3_SIGNATURE + b"\0" * 200)
    assert "HWP 3.x" in deepread.unreadable_reason(p)
    assert deepread.deep_read(p)["unreadable"].startswith("cannot read")


def test_plain_and_missing(tmp_path):
    p = tmp_path / "a.txt"; p.write_text("hello")
    assert deepread.unreadable_reason(p) is None
    assert deepread.unreadable_reason(tmp_path / "none.hwp") is None


def test_pdf_password(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    w = pypdf.PdfWriter(); w.add_blank_page(100, 100); w.encrypt("pw")
    p = tmp_path / "e.pdf"; w.write(str(p))
    assert "password-protected PDF" in deepread.unreadable_reason(p)


def pdf(path, pages):
    d = pdfium.PdfDocument.new()
    for _ in range(pages):
        d.new_page(300, 400)
    d.save(str(path)); d.close()
    return path


def test_scan_pages_configurable(tmp_path, monkeypatch):
    p = pdf(tmp_path / "scan.pdf", 7)
    monkeypatch.setattr(index, "ocr_engine", lambda: "vision")
    monkeypatch.setattr(index, "ocr_lines", lambda img: ["견적 스마트센서 납품 단가"])
    r = deepread.deep_read(p, 4000)
    assert r["ocr"] == {"pages_read": 7, "pages_total": 7}       # more than the 3 pages of a first pass
    assert r["text"].count("견적") == 7
    monkeypatch.setenv("FINDR_DEEP_READ_PAGES", "4")
    r = deepread.deep_read(p, 4000)
    assert r["ocr"] == {"pages_read": 4, "pages_total": 7} and "4 of 7" in r["note"]


def test_scan_without_recognizer(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "ocr_engine", lambda: "none")
    r = deepread.deep_read(pdf(tmp_path / "s.pdf", 2))
    assert not r["text"].strip() and "no text recognizer" in r["note"]


def test_index_deep_pages_env(monkeypatch):
    import inspect
    assert inspect.signature(index.ocr_wave).parameters["deep_pages"].default is None


def docx(path, app=None, text="word " * 10):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
        if app is not None:
            z.writestr("docProps/app.xml", app)
    return path


def test_docx_pages(tmp_path):
    assert deepread.docx_pages(docx(tmp_path / "a.docx", "<Properties><Pages>12</Pages></Properties>")) == (12, False)
    assert deepread.docx_pages(docx(tmp_path / "b.docx", "<Properties><Pages>0</Pages></Properties>", "가" * 3100)) == (3, True)
    assert deepread.docx_pages(docx(tmp_path / "c.docx", text="x" * 10)) == (1, True)
    r = deepread.deep_read(docx(tmp_path / "d.docx", "<Pages>5</Pages>"))
    assert r["pages"] == 5 and r["pages_estimated"] is False


def test_office_password(tmp_path):
    p = make_ole(tmp_path / "enc.docx", {"EncryptionInfo": b"x", "EncryptedPackage": b"y"})
    assert "password-protected docx" in deepread.unreadable_reason(p)
    assert deepread.unreadable_reason(docx(tmp_path / "plain.docx")) is None


def test_heic_fallback(tmp_path, monkeypatch):
    p = tmp_path / "p.heic"; p.write_bytes(b"not really heic")
    monkeypatch.setattr(index, "ocr_engine", lambda: "vision")
    monkeypatch.setattr(deepread, "_heic_to_png", lambda path: None)
    r = deepread.deep_read(p)
    assert "pillow-heif" in r["unreadable"]


def test_heic_read(tmp_path, monkeypatch):
    pytest.importorskip("pillow_heif")
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    p = tmp_path / "p.heic"; Image.new("RGB", (64, 64), "white").save(p, format="HEIF")
    monkeypatch.setattr(index, "ocr_engine", lambda: "vision")
    monkeypatch.setattr(index, "ocr_lines", lambda img: ["영수증 합계"])
    r = deepread.deep_read(p)
    assert r["text"] == "영수증 합계" and r["ocr"]["pages_total"] == 1
