"""HWP distribution(배포용) documents: the published decryption steps, checked piece by piece"""
import struct

import hwpdist


def test_aes128_fips197_vector():
    key = bytes(range(16))
    assert hwpdist.aes128_ecb_decrypt(key, bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")).hex() == "00112233445566778899aabbccddeeff"


def test_msvc_rand_sequence():
    r = hwpdist._msvc_rand(1)   # MSVC srand(1): rand() = 41, 18467, 6334, 26500
    assert [next(r) for _ in range(4)] == [41, 18467, 6334, 26500]


def test_key_from_distribute_doc_data_round_trip():
    seed, key = 0x1234ABC7, bytes(range(100, 116))
    plain = bytearray(256); plain[0:4] = struct.pack("<I", seed)
    off = (seed & 0x0F) + 4
    plain[off:off + 16] = key
    r, pat = hwpdist._msvc_rand(seed), bytearray()
    while len(pat) < 256:
        a = next(r) & 0xFF; pat += bytes([a]) * ((next(r) & 0x0F) + 1)
    enc = bytes(x ^ y for x, y in zip(plain, pat[:256]))
    enc = struct.pack("<I", seed) + enc[4:]   # the seed itself is stored as is
    assert hwpdist.distribute_key(enc) == key


def test_not_a_distribution_section():
    assert hwpdist.decrypt_viewtext(b"\x00" * 300) is None


def test_aes128_encrypt_fips197_vector():
    assert hwpdist.aes128_ecb_encrypt(bytes(range(16)), bytes.fromhex("00112233445566778899aabbccddeeff")).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"


def _record(tag, payload):
    return struct.pack("<I", tag | (len(payload) << 20)) + payload


def test_distribution_section_end_to_end():
    """a ViewText section built the way the spec describes: decrypt -> inflate -> PARA_TEXT, the text comes back"""
    import zlib
    import officetext
    seed, key = 0x0BADF00D, bytes(range(200, 216))
    data = bytearray(256); data[0:4] = struct.pack("<I", seed); off = (seed & 0x0F) + 4; data[off:off + 16] = key
    r, pat = hwpdist._msvc_rand(seed), bytearray()
    while len(pat) < 256:
        a = next(r) & 0xFF; pat += bytes([a]) * ((next(r) & 0x0F) + 1)
    enc = struct.pack("<I", seed) + bytes(x ^ y for x, y in zip(data, pat[:256]))[4:]
    body = _record(66, b"\0" * 24) + _record(67, "배포용 본문 시험 문장".encode("utf-16-le"))
    co = zlib.compressobj(9, zlib.DEFLATED, -15); z = co.compress(body) + co.flush()
    z += b"\0" * (-len(z) % 16)                                     # padded to the AES block
    section = _record(hwpdist.HWPTAG_DISTRIBUTE_DOC_DATA, enc) + hwpdist.aes128_ecb_encrypt(key, z)
    plain = hwpdist.decrypt_viewtext(section)
    out = []
    officetext._hwp_para_text(zlib.decompress(plain, -15), out, 10_000)
    assert "".join(out) == "배포용 본문 시험 문장"


def test_cut_off_tail_keeps_text():
    import officetext
    out = []
    data = _record(67, "앞 문단".encode("utf-16-le")) + struct.pack("<I", 67 | (0xFFF << 20)) + b"\x01"   # extended size cut off
    officetext._hwp_para_text(data, out, 10_000)
    assert "".join(out) == "앞 문단"
