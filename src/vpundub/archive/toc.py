"""VALKYRIE.BIN table of contents (TOC): decryption, parsing and encoding.

Reverse-engineered from the loader executable (SLUS_011.56 @ 0x80011d38, identical
code in SLPM_863.79 and the Disc 2 executables):

* The TOC occupies the first 13 sectors (0x6800 bytes) of VALKYRIE.BIN. The game
  reads all 13 and copies the first 0x6400 bytes. The remaining 0x400 bytes are zero.
* Decryption, key ``k = 0x64283921``, for ``i in 0..0x13FF``::

      word[i]         ^= k                       (32-bit LE words, bytes 0..0x4FFF)
      k2               = k ^ (k << 1)
      byte[0x5000+i]  ^= k2 & 0xFF               (bytes 0x5000..0x63FF)
      k                = ~k2 ^ 0x64283921

  The operation is a pure XOR stream, so encryption is the same function.
* ``word[0] == 0x19563824`` is the magic, checked at 0x80011e50.
* For entry ``n >= 1``, ``word[n]`` holds the bytes ``[MM, SS, FF, size_hi]``: the
  absolute disc address as BCD minute/second/frame, plus the high byte of the
  size in sectors. ``byte[0x5000+n]`` is the low byte of the size (accessor at
  0x80011ca0: ``((hi16 & 0xff00) | lo) << 11`` = size in bytes).
  An all-zero entry is unused.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

TOC_SECTORS = 13
TOC_BYTES = 0x6800
TOC_USED = 0x6400
N_SLOTS = 0x1400           # slot 0 is the magic
MAGIC = 0x19563824
KEY = 0x64283921
PREGAP = 150
_M32 = 0xFFFFFFFF


def crypt(data: bytes) -> bytes:
    """Encrypt or decrypt (XOR stream, self-inverse) the first 0x6400 TOC bytes."""
    if len(data) < TOC_USED:
        raise ValueError("TOC too short")
    t = bytearray(data[:TOC_USED])
    words = list(struct.unpack_from(f"<{N_SLOTS}I", t))
    k = KEY
    for i in range(N_SLOTS):
        words[i] ^= k
        k2 = (k ^ (k << 1)) & _M32
        t[0x5000 + i] ^= k2 & 0xFF
        k = (~k2 & _M32) ^ KEY
    struct.pack_into(f"<{N_SLOTS}I", t, 0, *words)
    return bytes(t) + bytes(data[TOC_USED:])


def _bcd2i(b: int) -> int:
    hi, lo = b >> 4, b & 0xF
    if hi > 9 or lo > 9:
        raise ValueError(f"bad BCD byte {b:#04x}")
    return hi * 10 + lo


def _i2bcd(v: int) -> int:
    if not 0 <= v <= 99:
        raise ValueError(f"BCD out of range: {v}")
    return ((v // 10) << 4) | (v % 10)


def msf_to_lba(m: int, s: int, f: int) -> int:
    return (m * 60 + s) * 75 + f - PREGAP


def lba_to_msf(lba: int) -> tuple[int, int, int]:
    a = lba + PREGAP
    return a // 4500, (a // 75) % 60, a % 75


@dataclass(frozen=True)
class Entry:
    index: int     # slot number (1..0x13FF)
    lba: int       # absolute disc LBA
    sectors: int   # size in 2048-byte sectors

    @property
    def end(self) -> int:
        return self.lba + self.sectors


@dataclass
class Toc:
    entries: dict[int, Entry]      # non-empty slots only
    n_slots: int = N_SLOTS

    @classmethod
    def decode(cls, raw: bytes) -> "Toc":
        """Parse an encrypted TOC (the first 0x6400+ bytes of VALKYRIE.BIN)."""
        t = crypt(raw[:TOC_USED])
        words = struct.unpack_from(f"<{N_SLOTS}I", t)
        if words[0] != MAGIC:
            raise ValueError(f"bad TOC magic {words[0]:#010x}")
        entries = {}
        for n in range(1, N_SLOTS):
            w = words[n]
            lo = t[0x5000 + n]
            if w == 0 and lo == 0:
                continue
            m, s, f, hi = w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, w >> 24
            entries[n] = Entry(n, msf_to_lba(_bcd2i(m), _bcd2i(s), _bcd2i(f)), (hi << 8) | lo)
        return cls(entries)

    def encode(self) -> bytes:
        """Build the encrypted 0x6800-byte TOC block."""
        t = bytearray(TOC_USED)
        words = [0] * N_SLOTS
        words[0] = MAGIC
        for n, e in self.entries.items():
            if not 1 <= n < N_SLOTS:
                raise ValueError(f"slot {n} out of range")
            if not 0 <= e.sectors <= 0xFFFF:
                raise ValueError(f"slot {n}: size {e.sectors} sectors does not fit 16 bits")
            m, s, f = lba_to_msf(e.lba)
            words[n] = _i2bcd(m) | _i2bcd(s) << 8 | _i2bcd(f) << 16 | (e.sectors >> 8) << 24
            t[0x5000 + n] = e.sectors & 0xFF
        struct.pack_into(f"<{N_SLOTS}I", t, 0, *words)
        return crypt(bytes(t)) + bytes(TOC_BYTES - TOC_USED)

    def sorted(self) -> list[Entry]:
        return sorted(self.entries.values(), key=lambda e: (e.lba, e.index))
