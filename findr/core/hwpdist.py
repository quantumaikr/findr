"""HWP 5 배포용(distribution) 문서의 본문 복호화.
본 제품은 한글과컴퓨터의 한글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
(한글 문서 파일 구조 - 배포용 문서, revision 1.2:20141009)

ViewText/SectionN = [HWPTAG_DISTRIBUTE_DOC_DATA record: 4-byte header + 256 bytes] + AES-128-ECB(rest)
  seed = first 4 bytes of the 256; MSVC srand(seed)/rand() fills a 256-byte pattern (odd call: value & 0xFF,
  even call: (& 0x0F) + 1 repeats); XOR with the 256 bytes; key = 16 bytes at offset (seed & 0x0F) + 4."""
import struct

# ---- AES-128 decryption (FIPS-197), pure Python: a few hundred KB of body text per document at most ----
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b27509832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cfd0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdbe0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9ee1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
_INV = bytes(_SBOX.index(i) for i in range(256))
_RCON = (1, 2, 4, 8, 16, 32, 64, 128, 27, 54)


def _xt(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a); b >>= 1
    return r


_M9, _M11, _M13, _M14 = ([_mul(i, k) for i in range(256)] for k in (9, 11, 13, 14))


def _round_keys(key):
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]; t = [_SBOX[b] for b in t]; t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


def aes128_ecb_decrypt(key, data):
    rk = _round_keys(key); out = bytearray()
    for off in range(0, len(data) - len(data) % 16, 16):
        s = [b ^ k for b, k in zip(data[off:off + 16], rk[10])]
        for rnd in range(9, -1, -1):
            s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]       # inverse shift rows (column-major state)
            s = [_INV[b] for b in s]
            s = [b ^ k for b, k in zip(s, rk[rnd])]
            if rnd:
                n = []
                for c in range(4):                                     # inverse mix columns
                    a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                    n += [_M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3], _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
                          _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3], _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]]
                s = n
        out += bytes(s)
    return bytes(out)


def aes128_ecb_encrypt(key, data):
    """the forward cipher, for tests that build a distribution section with a known key (findr itself only decrypts)"""
    rk = _round_keys(key); out = bytearray()
    for off in range(0, len(data) - len(data) % 16, 16):
        s = [b ^ k for b, k in zip(data[off:off + 16], rk[0])]
        for rnd in range(1, 11):
            s = [_SBOX[b] for b in s]
            s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]       # shift rows
            if rnd < 10:
                n = []
                for c in range(4):                                     # mix columns
                    a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                    n += [_xt(a0) ^ _xt(a1) ^ a1 ^ a2 ^ a3, a0 ^ _xt(a1) ^ _xt(a2) ^ a2 ^ a3,
                          a0 ^ a1 ^ _xt(a2) ^ _xt(a3) ^ a3, _xt(a0) ^ a0 ^ a1 ^ a2 ^ _xt(a3)]
                s = n
            s = [b ^ k for b, k in zip(s, rk[rnd])]
        out += bytes(s)
    return bytes(out)


# ---- 배포용 문서 데이터 -> key ----
HWPTAG_DISTRIBUTE_DOC_DATA = 16 + 12


def _msvc_rand(seed):
    state = seed & 0xFFFFFFFF
    while True:
        state = (state * 214013 + 2531011) & 0xFFFFFFFF
        yield (state >> 16) & 0x7FFF


def distribute_key(doc_data):
    """16-byte AES key from the 256-byte HWPTAG_DISTRIBUTE_DOC_DATA payload"""
    seed = struct.unpack_from("<I", doc_data, 0)[0]
    r, pat = _msvc_rand(seed), bytearray()
    while len(pat) < 256:
        a = next(r) & 0xFF; n = (next(r) & 0x0F) + 1
        pat += bytes([a]) * n
    dec = bytes(x ^ y for x, y in zip(doc_data, pat[:256]))
    off = (seed & 0x0F) + 4
    return dec[off:off + 16]


def decrypt_viewtext(stream):
    """ViewText/SectionN stream -> the section's record bytes (still zlib-compressed when the document is), or None"""
    if len(stream) < 4 + 256:
        return None
    h = struct.unpack_from("<I", stream, 0)[0]
    if h & 0x3FF != HWPTAG_DISTRIBUTE_DOC_DATA or (h >> 20) & 0xFFF != 256:
        return None
    key = distribute_key(stream[4:260])
    return aes128_ecb_decrypt(key, stream[260:])
