"""ISO9660 (PlayStation / CD-XA) reading and template-based rebuilding.

Rebuilding is *template-based*: the original disc supplies the system area,
volume descriptors, path tables, directory records (with their dates and XA
attributes) and padding sectors. Only file contents, extent LBAs, sizes and
the volume space size are rewritten. This keeps every byte we don't need to
change identical to the original pressing.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .edc_ecc import FORM1_DATA
from .image import DiscImage, SectorWriter
from .sector import SM_DATA, SM_EOF, SM_EOR, SubHeader

PVD_LBA = 16


@dataclass
class DirRecord:
    path: str            # e.g. "/VALKYRIE.BIN;1"
    lba: int
    size: int
    is_dir: bool
    rec_lba: int         # sector holding this record
    rec_off: int         # byte offset of the record in that sector
    raw: bytes = b""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def sectors(self) -> int:
        return max(1, (self.size + FORM1_DATA - 1) // FORM1_DATA)


@dataclass
class IsoVolume:
    image: DiscImage
    pvd: bytes
    volume_size: int
    root: DirRecord
    entries: list[DirRecord] = field(default_factory=list)

    @property
    def files(self) -> list[DirRecord]:
        return [e for e in self.entries if not e.is_dir]

    @property
    def dirs(self) -> list[DirRecord]:
        return [self.root] + [e for e in self.entries if e.is_dir]

    def find(self, name: str) -> DirRecord:
        """Find a file by full path or bare name (version suffix optional)."""
        want = name.upper().lstrip("/")
        for e in self.entries:
            p = e.path.lstrip("/").upper()
            if want in (p, p.split(";")[0], e.name.upper(), e.name.upper().split(";")[0]):
                return e
        raise KeyError(name)

    def read(self, name: str) -> bytes:
        e = self.find(name)
        return self.image.read_form1(e.lba, e.size)

    def postgap(self) -> int:
        """Number of trailing sectors after the last allocated extent."""
        end = max(e.lba + e.sectors for e in self.entries + [self.root])
        return len(self.image) - end


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def _both32(v: int) -> bytes:
    return struct.pack("<I", v) + struct.pack(">I", v)


def _parse_record(buf: bytes, off: int) -> tuple[int, int, int, bytes]:
    ln = buf[off]
    rec = buf[off:off + ln]
    return _u32(rec, 2), _u32(rec, 10), rec[25], rec


def parse(image: DiscImage) -> IsoVolume:
    pvd = image.user(PVD_LBA)[:2048]
    if pvd[0:6] != b"\x01CD001":
        raise ValueError("no primary volume descriptor at LBA 16")
    vol_size = _u32(pvd, 80)
    rlba, rsize, rflags, rraw = _parse_record(pvd, 156)
    root = DirRecord("/", rlba, rsize, True, PVD_LBA, 156, rraw)
    vol = IsoVolume(image, pvd, vol_size, root)

    def walk(d: DirRecord, prefix: str) -> None:
        for s in range(d.sectors):
            buf = image.user(d.lba + s)[:2048]
            off = 0
            while off < 2048 and buf[off]:
                lba, size, flags, raw = _parse_record(buf, off)
                nl = raw[32]
                name = raw[33:33 + nl]
                if name not in (b"\x00", b"\x01"):
                    sname = name.decode("ascii", "replace")
                    is_dir = bool(flags & 2)
                    rec = DirRecord(prefix + sname, lba, size, is_dir, d.lba + s, off, raw)
                    vol.entries.append(rec)
                    if is_dir:
                        walk(rec, prefix + sname + "/")
                off += buf[off]

    walk(root, "/")
    return vol


def rebuild(vol: IsoVolume, out_path: str, replacements: dict[str, bytes] | None = None,
            sub_mid: SubHeader = SubHeader(0, 0, SM_DATA, 0),
            sub_last: SubHeader = SubHeader(0, 0, SM_EOF | SM_DATA | SM_EOR, 0)) -> IsoVolume:
    """Write a new image from ``vol`` with some files' contents replaced.

    Files keep their original LBA where possible. A file that no longer fits
    before the next extent is moved to the end of the data area. Gap and
    padding sectors are copied from the template. The volume size is updated
    and the original postgap length is kept.

    Returns the parsed volume of the written image.
    """
    replacements = {k.upper(): v for k, v in (replacements or {}).items()}
    img = vol.image

    contents: dict[str, bytes] = {}
    for e in vol.files:
        key = next((k for k in replacements
                    if k.lstrip("/") in (e.path.lstrip("/").upper(), e.name.upper(),
                                         e.name.upper().split(";")[0])), None)
        contents[e.path] = replacements.pop(key) if key else img.read_form1(e.lba, e.size)
    if replacements:
        raise KeyError(f"unknown files: {sorted(replacements)}")

    # Plan the layout: directories/system area stay put.
    fixed_end = max(d.lba + d.sectors for d in vol.dirs)
    order = sorted(vol.files, key=lambda e: e.lba)
    orig_end = max(e.lba + e.sectors for e in vol.entries + [vol.root])
    new_lba: dict[str, int] = {}
    cursor = fixed_end
    deferred = []
    for i, e in enumerate(order):
        n = max(1, (len(contents[e.path]) + FORM1_DATA - 1) // FORM1_DATA)
        limit = order[i + 1].lba if i + 1 < len(order) else None
        start = max(e.lba, cursor)
        if limit is not None and start + n > limit:
            deferred.append((e, n))
            continue
        new_lba[e.path] = start
        cursor = start + n
    tail = max(cursor, orig_end) if deferred else cursor
    for e, n in deferred:
        new_lba[e.path] = tail
        tail += n
    data_end = max([fixed_end] + [new_lba[e.path] + max(1, (len(contents[e.path]) + 2047) // 2048)
                                  for e in vol.files])
    postgap = vol.postgap()
    total = data_end + postgap

    # Patched copies of the PVD and directory sectors.
    patched: dict[int, bytearray] = {}

    def sector_buf(lba: int) -> bytearray:
        if lba not in patched:
            patched[lba] = bytearray(img.user(lba))
        return patched[lba]

    pvd = sector_buf(PVD_LBA)
    pvd[80:88] = _both32(total)
    for e in vol.files:
        b = sector_buf(e.rec_lba)
        b[e.rec_off + 2:e.rec_off + 10] = _both32(new_lba[e.path])
        b[e.rec_off + 10:e.rec_off + 18] = _both32(len(contents[e.path]))

    by_lba = {new_lba[e.path]: e for e in vol.files}
    orig_extents = [(e.lba, e.lba + e.sectors) for e in vol.files]

    def stale(lba: int) -> bool:
        """True if ``lba`` was part of an original file extent (now rewritten or freed)."""
        return any(a <= lba < b for a, b in orig_extents)

    with SectorWriter(out_path) as w:
        lba = 0
        while lba < data_end:
            if lba in by_lba:
                e = by_lba[lba]
                w.add_file(contents[e.path], sub_mid, sub_last)
                lba = w.lba
                continue
            if lba < len(img) and not stale(lba):
                t = img.sector(lba)
                if lba in patched:
                    sub = SubHeader.from_bytes(t[0x10:0x14])
                    w.add(sub, bytes(patched[lba]))
                else:
                    w.add_raw(t)
            else:
                w.add(sub_mid, b"")
            lba += 1
        assert w.lba == data_end, (w.lba, data_end)
        # Postgap: replicate the template's trailing sectors (empty, zero subheader).
        tmpl_tail = len(img) - postgap
        for i in range(postgap):
            w.add_raw(img.sector(tmpl_tail + i))

    return parse(DiscImage(out_path))
