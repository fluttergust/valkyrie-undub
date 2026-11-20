"""Raw MODE2/2352 BIN image access, CUE handling and batched sector writing."""
from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np

from .edc_ecc import FORM1_DATA, FORM2_DATA, SECTOR_SIZE, regenerate_batch
from .sector import PREGAP, SYNC, SubHeader

_BATCH = 4096


class DiscImage:
    """Read-only, memory-mapped view of a single-track MODE2/2352 BIN."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        size = self.path.stat().st_size
        if size % SECTOR_SIZE:
            raise ValueError(f"{path}: size {size} is not a multiple of {SECTOR_SIZE}")
        self.raw = np.memmap(self.path, dtype=np.uint8, mode="r").reshape(-1, SECTOR_SIZE)

    @classmethod
    def from_cue(cls, cue: str | os.PathLike) -> "DiscImage":
        return cls(parse_cue(cue))

    def __len__(self) -> int:
        return self.raw.shape[0]

    def sector(self, lba: int) -> bytes:
        return self.raw[lba].tobytes()

    def subheader(self, lba: int) -> SubHeader:
        return SubHeader.from_bytes(self.raw[lba, 0x10:0x14].tobytes())

    def user(self, lba: int) -> bytes:
        r = self.raw[lba]
        n = FORM2_DATA if r[0x12] & 0x20 else FORM1_DATA
        return r[0x18:0x18 + n].tobytes()

    def read_form1(self, lba: int, size: int) -> bytes:
        """Read ``size`` bytes of Form 1 user data starting at ``lba``."""
        count = (size + FORM1_DATA - 1) // FORM1_DATA
        data = self.raw[lba:lba + count, 0x18:0x18 + FORM1_DATA]
        return data.tobytes()[:size]


def parse_cue(cue: str | os.PathLike) -> Path:
    """Return the BIN path referenced by a single-file CUE sheet."""
    cue = Path(cue)
    text = cue.read_text(errors="replace")
    m = re.search(r'FILE\s+"([^"]+)"\s+BINARY', text)
    if not m:
        raise ValueError(f"{cue}: no BINARY FILE entry")
    return cue.parent / m.group(1)


def write_cue(bin_path: str | os.PathLike, cue_path: str | os.PathLike) -> None:
    bin_name = Path(bin_path).name
    Path(cue_path).write_text(
        f'FILE "{bin_name}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
        newline="\n",
    )


def _msf_headers(lbas: np.ndarray) -> np.ndarray:
    a = lbas + PREGAP
    m = a // 4500
    s = (a // 75) % 60
    f = a % 75
    bcd = lambda v: ((v // 10) << 4) | (v % 10)  # noqa: E731
    out = np.empty((len(lbas), 4), dtype=np.uint8)
    out[:, 0] = bcd(m)
    out[:, 1] = bcd(s)
    out[:, 2] = bcd(f)
    out[:, 3] = 2
    return out


class SectorWriter:
    """Sequential writer that batches sectors and regenerates EDC/ECC.

    Every sector gets a correct sync and MSF header for its LBA. Callers supply
    either a subheader plus payload (``add``) or a template raw sector whose
    subheader/payload are kept (``add_raw``). A template's Form 2 EDC presence
    (zero / non-zero) is preserved.
    """

    def __init__(self, path: str | os.PathLike):
        self.f = open(path, "wb")
        self.lba = 0
        self._buf: list[bytes] = []

    def add(self, sub: SubHeader, data: bytes, form2_edc: bool = False) -> None:
        size = FORM2_DATA if sub.form2 else FORM1_DATA
        if len(data) > size:
            raise ValueError(f"payload {len(data)} > {size}")
        body = sub.to_bytes() + data.ljust(size, b"\x00")
        raw = bytearray(SYNC + b"\0\0\0\0" + body)
        raw.extend(b"\x00" * (SECTOR_SIZE - len(raw)))
        if sub.form2 and form2_edc:
            raw[0x92C] = 1  # marker: regenerate keeps a non-zero EDC
        self._push(bytes(raw))

    def add_raw(self, raw: bytes) -> None:
        if len(raw) != SECTOR_SIZE:
            raise ValueError("raw sector must be 2352 bytes")
        r = bytearray(raw)
        if r[0x12] & 0x20 and any(r[0x92C:0x930]):
            r[0x92C] = r[0x92C] or 1
        self._push(bytes(r))

    def add_file(self, data: bytes, sub_mid: SubHeader, sub_last: SubHeader) -> int:
        """Write Form 1 file data; returns the starting LBA."""
        start = self.lba
        n = max(1, (len(data) + FORM1_DATA - 1) // FORM1_DATA)
        for i in range(n):
            chunk = data[i * FORM1_DATA:(i + 1) * FORM1_DATA]
            self.add(sub_last if i == n - 1 else sub_mid, chunk)
        return start

    def _push(self, raw: bytes) -> None:
        self._buf.append(raw)
        self.lba += 1
        if len(self._buf) >= _BATCH:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        n = len(self._buf)
        arr = np.frombuffer(b"".join(self._buf), dtype=np.uint8).reshape(n, SECTOR_SIZE).copy()
        first = self.lba - n
        arr[:, 0x0C:0x10] = _msf_headers(np.arange(first, first + n, dtype=np.int64))
        self.f.write(regenerate_batch(arr).tobytes())
        self._buf.clear()

    def close(self) -> None:
        self.flush()
        self.f.close()

    def __enter__(self) -> "SectorWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
