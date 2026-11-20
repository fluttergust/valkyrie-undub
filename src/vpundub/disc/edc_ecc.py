"""CD-ROM EDC / ECC (Mode 2) generation.

The algorithms follow ECMA-130 Annex A/B, as implemented in Neill Corlett's ECM
tools and documented in psx-spx. Each function has a numpy-vectorised batch form,
so a whole 700 MB image can be checked or regenerated in seconds instead of
minutes.

Raw sector layout (2352 bytes):
  0x000  12  sync
  0x00C   4  header (MSF BCD + mode)
  0x010   8  subheader (copied twice)
  Form 1: 0x018 2048 user data, 0x818 EDC, 0x81C P-parity (172), 0x8C8 Q-parity (104)
  Form 2: 0x018 2324 user data, 0x92C EDC (may legally be 0)
"""
from __future__ import annotations

import numpy as np

SECTOR_SIZE = 2352
FORM1_DATA = 2048
FORM2_DATA = 2324

# --- EDC (CRC-32, poly 0x8001801B, reflected) --------------------------------

def _make_edc_table() -> np.ndarray:
    t = np.zeros(256, dtype=np.uint32)
    for i in range(256):
        e = i
        for _ in range(8):
            e = (e >> 1) ^ (0xD8018001 if e & 1 else 0)
        t[i] = e
    return t


EDC_TABLE = _make_edc_table()
_EDC_LIST = EDC_TABLE.tolist()


def edc(data: bytes | bytearray | memoryview) -> int:
    """EDC of a single byte string."""
    crc = 0
    t = _EDC_LIST
    for b in bytes(data):
        crc = (crc >> 8) ^ t[(crc ^ b) & 0xFF]
    return crc


def edc_batch(block: np.ndarray) -> np.ndarray:
    """EDC for each row of a (N, L) uint8 array. Returns uint32[N]."""
    crc = np.zeros(block.shape[0], dtype=np.uint32)
    cols = np.ascontiguousarray(block.T)  # (L, N): contiguous per column
    for col in cols:
        crc = (crc >> np.uint32(8)) ^ EDC_TABLE[(crc ^ col) & np.uint32(0xFF)]
    return crc


# --- ECC (Reed-Solomon product code over GF(2^8)) ---------------------------

def _make_ecc_luts() -> tuple[np.ndarray, np.ndarray]:
    f = np.zeros(256, dtype=np.uint8)
    b = np.zeros(256, dtype=np.uint8)
    for i in range(256):
        j = (i << 1) ^ (0x11D if i & 0x80 else 0)
        f[i] = j & 0xFF
        b[i ^ j] = i
    return f, b


ECC_F, ECC_B = _make_ecc_luts()


def _ecc_indices(major_count: int, minor_count: int, major_mult: int,
                 minor_inc: int) -> np.ndarray:
    size = major_count * minor_count
    idx = np.zeros((major_count, minor_count), dtype=np.intp)
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        for minor in range(minor_count):
            idx[major, minor] = index
            index += minor_inc
            if index >= size:
                index -= size
    return idx


# Offsets are relative to sector byte 0x0C.
_P_IDX = _ecc_indices(86, 24, 2, 86)
_Q_IDX = _ecc_indices(52, 43, 86, 88)


def _ecc_block(src: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """src: (N, L) uint8. Returns (N, 2*major) uint8 parity."""
    gathered = src[:, idx]  # (N, major, minor)
    n, major, minor = gathered.shape
    a = np.zeros((n, major), dtype=np.uint8)
    for m in range(minor):
        a = ECC_F[a ^ gathered[:, :, m]]
    bsum = np.bitwise_xor.reduce(gathered, axis=2)
    a = ECC_B[ECC_F[a] ^ bsum]
    return np.concatenate([a, a ^ bsum], axis=1)


def ecc_batch(sectors: np.ndarray) -> np.ndarray:
    """Compute P+Q parity (276 bytes) for (N, 2352) Mode 2 Form 1 sectors.

    For Mode 2 the 4 header bytes are treated as zero.
    """
    work = sectors[:, 0x0C:0x8C8].copy()
    work[:, 0:4] = 0
    p = _ecc_block(work[:, :2064], _P_IDX)          # 172 bytes
    work2 = np.concatenate([work[:, :2064], p], axis=1)  # 2236 bytes
    q = _ecc_block(work2, _Q_IDX)                   # 104 bytes
    return np.concatenate([p, q], axis=1)


def regenerate_batch(sectors: np.ndarray) -> np.ndarray:
    """Return a copy of (N, 2352) Mode 2 sectors with EDC/ECC regenerated.

    The form is chosen per sector from the subheader submode bit 5. Form 2 EDC
    is computed only where the original EDC was non-zero, because a zero Form 2
    EDC means "not present" (common in padding sectors).
    """
    out = sectors.copy()
    form2 = (out[:, 0x12] & 0x20) != 0
    f1 = np.nonzero(~form2)[0]
    f2 = np.nonzero(form2)[0]
    if f1.size:
        s = out[f1]
        e = edc_batch(s[:, 0x10:0x818])
        s[:, 0x818:0x81C] = e.astype('<u4').view(np.uint8).reshape(-1, 4)
        s[:, 0x81C:0x930] = ecc_batch(s)
        out[f1] = s
    if f2.size:
        s = out[f2]
        had = s[:, 0x92C:0x930].any(axis=1)
        e = edc_batch(s[:, 0x10:0x92C])
        e = np.where(had, e, 0).astype('<u4')
        s[:, 0x92C:0x930] = e.view(np.uint8).reshape(-1, 4)
        out[f2] = s
    return out


def regenerate(sector: bytes | bytearray, form2_edc: bool = True) -> bytes:
    """Regenerate EDC/ECC for a single raw sector.

    For Form 2, ``form2_edc`` chooses whether to write the EDC (True) or zero
    (False). This lets new padding sectors match the original convention.
    """
    arr = np.frombuffer(bytes(sector), dtype=np.uint8).reshape(1, SECTOR_SIZE).copy()
    if arr[0, 0x12] & 0x20:
        arr[0, 0x92C:0x930] = 1 if form2_edc else 0  # marker so batch keeps/zeroes it
    return regenerate_batch(arr)[0].tobytes()
