"""plain text as Windows Notepad saves it. On Korean Windows 'ANSI' is cp949 and '유니코드' is UTF-16: the first lost every
Korean character (read as UTF-8), the second was dropped as binary (its NUL bytes)"""
import index

KO = "옛날 메모장으로 쓴 견적 내용"


def _ext(tmp_path, name, data, limit=None):
    p = tmp_path / name; p.write_bytes(data)
    return index.extract(p, limit or index.MAXREAD)


def test_ansi_korean_notepad(tmp_path):
    assert KO in _ext(tmp_path, "견적.txt", KO.encode("cp949"))


def test_unicode_notepad_utf16(tmp_path):
    assert KO in _ext(tmp_path, "견적16.txt", "﻿".encode("utf-16-le") + KO.encode("utf-16-le"))
    assert KO in _ext(tmp_path, "견적16be.txt", b"\xfe\xff" + KO.encode("utf-16-be"))


def test_utf8_with_and_without_bom(tmp_path):
    assert _ext(tmp_path, "a.txt", b"\xef\xbb\xbf" + KO.encode()).startswith("옛날")
    assert KO in _ext(tmp_path, "b.txt", KO.encode())


def test_utf8_cut_at_the_read_limit_stays_utf8(tmp_path):
    data = ("가" * 50).encode()                      # 3 bytes each
    t = _ext(tmp_path, "cut.txt", data, limit=100)   # 100 = 33 characters and one byte of the next
    assert t == "가" * 33


def test_latin1_text_does_not_turn_into_hangul(tmp_path):
    t = _ext(tmp_path, "cafe.txt", "café résumé naïve".encode("latin-1"))
    assert not any("가" <= c <= "힣" for c in t), t
    assert "caf" in t and "sum" in t


def test_binary_is_still_skipped(tmp_path):
    assert _ext(tmp_path, "blob.txt", b"\x00\x01\x02binary\x00" * 50) == ""
