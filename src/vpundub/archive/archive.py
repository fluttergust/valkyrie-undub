"""VALKYRIE.BIN container: parse, extract and rebuild.

Layout (all sizes in 2048-byte sectors)::

    [TOC: 13 sectors][entry data, contiguous, in disc order][filler tail]

* Entries are addressed by **slot number** (1..0x13FF). The game code (executable
  and every overlay) reads files only through the executable's slot-based API
  (seek(slot[, sector offset]), read, exec). Slot numbers must therefore be kept;
  disc positions may change freely as long as the TOC is rewritten.
* Entry positions in the TOC are absolute disc LBAs, so ``base_lba`` (the LBA of
  VALKYRIE.BIN itself, 150 on every disc) is needed to convert positions.
* The filler tail (13500 sectors on US1/US2/JP1, none on JP2) is not referenced
  by the TOC. It is zero sectors with an identical 6-byte marker sector every 50
  sectors. It is kept verbatim, or truncated to absorb growth so the overall
  file size stays the same.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .toc import TOC_BYTES, TOC_SECTORS, Entry, Toc

SECTOR = 2048
BASE_LBA = 150


def _pad(data: bytes) -> bytes:
    r = len(data) % SECTOR
    return data if r == 0 and data else data + b"\x00" * ((SECTOR - r) if data else SECTOR)


@dataclass
class Archive:
    toc: Toc
    data: dict[int, bytes]          # slot -> raw entry bytes (whole sectors)
    order: list[int]                # slots in on-disc order
    tail: bytes = b""               # filler after the last entry
    base_lba: int = BASE_LBA
    extra: dict = field(default_factory=dict)

    # --- parsing -----------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | os.PathLike, base_lba: int = BASE_LBA) -> "Archive":
        with open(path, "rb") as f:
            return cls.from_bytes(f.read(), base_lba)

    @classmethod
    def from_bytes(cls, blob: bytes, base_lba: int = BASE_LBA) -> "Archive":
        toc = Toc.decode(blob[:TOC_BYTES])
        if any(blob[0x6400:TOC_BYTES]):
            raise ValueError("unexpected data in TOC padding")
        order = [e.index for e in toc.sorted()]
        data = {}
        cur = base_lba + TOC_SECTORS
        for e in toc.sorted():
            if e.lba != cur:
                raise ValueError(f"slot {e.index}: expected LBA {cur}, got {e.lba} (gap/overlap)")
            off = (e.lba - base_lba) * SECTOR
            data[e.index] = blob[off:off + e.sectors * SECTOR]
            cur = e.end
        tail = blob[(cur - base_lba) * SECTOR:]
        return cls(toc, data, order, tail, base_lba)

    # --- access ------------------------------------------------------------
    def entry(self, slot: int) -> Entry:
        return self.toc.entries[slot]

    def __contains__(self, slot: int) -> bool:
        return slot in self.data

    def __getitem__(self, slot: int) -> bytes:
        return self.data[slot]

    @property
    def slots(self) -> list[int]:
        return list(self.order)

    # --- building ----------------------------------------------------------
    def build(self, replacements: dict[int, bytes] | None = None,
              keep_size: bool = True) -> bytes:
        """Serialise the archive with some slots' contents replaced.

        * Entries keep their on-disc order. Replaced entries are sector-padded.
        * ``keep_size``: truncate (or extend with zero) the filler tail so the
          total file size matches the original whenever possible. If the data
          grows past the original size, the tail is dropped and the file grows.
        """
        replacements = dict(replacements or {})
        unknown = set(replacements) - set(self.data)
        if unknown:
            raise KeyError(f"unknown slots {sorted(unknown)} (adding slots is not supported)")
        chunks: list[bytes] = []
        entries: dict[int, Entry] = {}
        cur = self.base_lba + TOC_SECTORS
        for slot in self.order:
            d = replacements.get(slot, self.data[slot])
            d = _pad(d) if slot in replacements else d
            n = len(d) // SECTOR
            if n > 0xFFFF:
                raise ValueError(f"slot {slot}: {n} sectors exceed the 16-bit size field")
            entries[slot] = Entry(slot, cur, n)
            chunks.append(d)
            cur += n
        body_sectors = cur - self.base_lba
        orig_sectors = self.size_sectors
        tail = self.tail
        if keep_size:
            room = (orig_sectors - body_sectors) * SECTOR
            tail = tail[:room] if room >= 0 else b""
            if room > len(tail):
                tail = tail + b"\x00" * (room - len(tail))
        toc = Toc(entries)
        return toc.encode() + b"".join(chunks) + tail

    @property
    def size_sectors(self) -> int:
        return TOC_SECTORS + sum(len(d) // SECTOR for d in self.data.values()) + len(self.tail) // SECTOR
