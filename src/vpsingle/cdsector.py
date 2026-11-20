"""CD-ROM Mode 2 Form 1 sector encoder (ECMA-130): sync, header, subheader, EDC, ECC P/Q.

Raw sector layout (2352 bytes):
  0..11   sync (00 FF*10 00)
  12..15  header: minute, second, frame (BCD), mode (2)
  16..23  subheader (4 bytes, repeated twice)
  24..2071  user data (2048)
  2072..2075 EDC over bytes 16..2071
  2076..2247 ECC P (172 bytes), 2248..2351 ECC Q (104 bytes); computed with the header zeroed (Mode 2).
"""
from __future__ import annotations

SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
SECTOR = 2352


def _tables():
    f_lut = [0] * 256
    b_lut = [0] * 256
    edc = [0] * 256
    for i in range(256):
        j = (i << 1) ^ (0x11D if i & 0x80 else 0)
        f_lut[i] = j & 0xFF
        b_lut[i ^ j & 0xFF] = i
        e = i
        for _ in range(8):
            e = (e >> 1) ^ (0xD8018001 if e & 1 else 0)
        edc[i] = e
    return f_lut, b_lut, edc


F_LUT, B_LUT, EDC_LUT = _tables()


def edc(data: bytes) -> int:
    e = 0
    for b in data:
        e = (e >> 8) ^ EDC_LUT[(e ^ b) & 0xFF]
    return e


def _ecc_pq(buf: bytearray, major_count: int, minor_count: int, major_mult: int, minor_inc: int,
            dst: int) -> None:
    # buf is indexed from the header (byte 12 of the raw sector)
    size = major_count * minor_count
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        a = b = 0
        for _ in range(minor_count):
            t = buf[index]
            index += minor_inc
            if index >= size:
                index -= size
            a ^= t
            b ^= t
            a = F_LUT[a]
        a = B_LUT[F_LUT[a] ^ b]
        buf[dst + major] = a
        buf[dst + major + major_count] = a ^ b


def bcd(v: int) -> int:
    return (v // 10) << 4 | (v % 10)


def lba_header(lba: int, mode: int = 2) -> bytes:
    a = lba + 150
    return bytes((bcd(a // 4500), bcd((a // 75) % 60), bcd(a % 75), mode))


def encode_form1(lba: int, subheader: bytes, data: bytes) -> bytes:
    """Build a raw Mode 2 Form 1 sector. subheader is 4 bytes (file, channel, submode, coding)."""
    assert len(subheader) == 4 and len(data) == 2048
    s = bytearray(SECTOR)
    s[0:12] = SYNC
    s[16:20] = subheader
    s[20:24] = subheader
    s[24:2072] = data
    s[2072:2076] = edc(bytes(s[16:2072])).to_bytes(4, "little")
    # ECC with zeroed header
    body = bytearray(s[12:])
    body[0:4] = b"\0\0\0\0"
    _ecc_pq(body, 86, 24, 2, 86, 2076 - 12)
    _ecc_pq(body, 52, 43, 86, 88, 2248 - 12)
    s[12:] = body
    s[12:16] = lba_header(lba)
    return bytes(s)


def reencode(raw: bytes, lba: int, data: bytes | None = None) -> bytes:
    """Re-encode a raw Form 1 sector at a new LBA (keeping its subheader), optionally with new user data."""
    return encode_form1(lba, raw[16:20], raw[24:2072] if data is None else data)
