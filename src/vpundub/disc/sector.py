"""Raw Mode 2 sector construction and parsing."""
from __future__ import annotations

from dataclasses import dataclass

from .edc_ecc import FORM1_DATA, FORM2_DATA, SECTOR_SIZE, regenerate

SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
PREGAP = 150  # LBA 0 == MSF 00:02:00

# Subheader submode bits
SM_EOR = 0x01
SM_VIDEO = 0x02
SM_AUDIO = 0x04
SM_DATA = 0x08
SM_TRIGGER = 0x10
SM_FORM2 = 0x20
SM_RT = 0x40
SM_EOF = 0x80


def _bcd(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


def lba_to_msf_header(lba: int, mode: int = 2) -> bytes:
    a = lba + PREGAP
    m, rem = divmod(a, 75 * 60)
    s, f = divmod(rem, 75)
    return bytes([_bcd(m), _bcd(s), _bcd(f), mode])


@dataclass(frozen=True)
class SubHeader:
    file: int = 0
    channel: int = 0
    submode: int = SM_DATA
    coding: int = 0

    def to_bytes(self) -> bytes:
        b = bytes([self.file, self.channel, self.submode, self.coding])
        return b + b

    @classmethod
    def from_bytes(cls, b: bytes) -> "SubHeader":
        return cls(b[0], b[1], b[2], b[3])

    @property
    def form2(self) -> bool:
        return bool(self.submode & SM_FORM2)


def build_sector(lba: int, sub: SubHeader, data: bytes,
                 form2_edc: bool = True) -> bytes:
    """Build a complete raw 2352-byte Mode 2 sector with EDC/ECC."""
    size = FORM2_DATA if sub.form2 else FORM1_DATA
    if len(data) > size:
        raise ValueError(f"sector payload too large: {len(data)} > {size}")
    data = data.ljust(size, b"\x00")
    raw = SYNC + lba_to_msf_header(lba) + sub.to_bytes() + data
    raw = raw.ljust(SECTOR_SIZE, b"\x00")
    return regenerate(raw, form2_edc=form2_edc)


def user_data(raw: bytes) -> bytes:
    """Extract the user data of a raw sector (2048 or 2324 bytes)."""
    if raw[0x12] & SM_FORM2:
        return raw[0x18:0x18 + FORM2_DATA]
    return raw[0x18:0x18 + FORM1_DATA]
