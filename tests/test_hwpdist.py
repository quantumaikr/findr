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
