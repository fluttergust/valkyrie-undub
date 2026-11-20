"""Sound objects (voice clips, voice banks, battle sound banks) and packs.

Every piece of voice audio found in VALKYRIE.BIN is stored in one container
shape, called a *sound object* here::

    0x00  u32  H                 offset of the data-size field
    0x04  ...  script            either a raw sound script beginning with
                                 u32 0x80000004 (single voice clip), a script
                                 table (scene voice banks), or an SLZ block
                                 (battle sound banks)
    H     u32  size              bytes of attribute table + ADPCM that follow
    H+4   attribute table:
            u16  table size (incl. this header and the terminator)
            u8   region size (0x14)
            u8   flags
            u32  extra (0 for clips)
            sample entries, until the terminator ``ff ff ff ff 00 00 00 00``:
                u16 sample id, u16 p1, u16 p2, u8 0, u8 k (region count)
                k x 0x14-byte region: [lo, hi, ?, vol] u32 adpcm start,
                                      u32 adpcm end, u16 pitch, u16, u32
    H+4+table  SPU-ADPCM data (offsets above are relative to this point)

Single clips also carry [line id u16, group u8, flag u8] at 0x0C.

Packs (generic resource containers)::

    0x00  u32 count, 0x04 u32 unknown, 0x08 count x (u32 tag, u32 size), items...

A pack may hold SLZ blocks, MIPS code, sound objects and other data. Only
sound-object items may be taken from the Japanese version.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from ..archive import slz

TERMINATOR = b"\xff\xff\xff\xff\x00\x00\x00\x00"
CLIP_MAGIC1 = 0x80000004


@dataclass(frozen=True)
class Region:
    adpcm_offset: int
    adpcm_length: int
    pitch: int


@dataclass(frozen=True)
class Sample:
    sample_id: int
    regions: tuple[Region, ...]


@dataclass(frozen=True)
class SoundObject:
    offset: int                  # within the parent buffer
    length: int                  # H + 4 + size
    script_end: int              # H
    size: int
    table_size: int
    flags: int
    extra: int
    samples: tuple[Sample, ...]
    script_kind: str             # "clip", "slz", "table" or "bank" (no script, H = 0)
    line: int | None = None      # clips only
    group: int | None = None

    @property
    def sample_ids(self) -> tuple[int, ...]:
        return tuple(s.sample_id for s in self.samples)

    @property
    def adpcm_size(self) -> int:
        return self.size - self.table_size

    @property
    def key(self) -> tuple[int, ...]:
        return self.sample_ids


def parse_sound(buf: bytes, off: int = 0, limit: int | None = None) -> SoundObject | None:
    end = len(buf) if limit is None else min(len(buf), limit)
    if off + 0x30 > end:
        return None
    H = struct.unpack_from("<I", buf, off)[0]
    # Script-less banks (e.g. the resident system bank, slot 4) start directly
    # with the size field: treat them as H = 0.
    if buf[off + 6] == 0x14 and buf[off + 7] in (0, 1) and struct.unpack_from("<H", buf, off + 4)[0] >= 0x18:
        alt = parse_sound_at(buf, off, 0, end)
        if alt is not None:
            return alt
    if H < 8 or H > 0x10000 or H % 4 or off + H + 12 > end:
        return None
    return parse_sound_at(buf, off, H, end)


def parse_sound_at(buf: bytes, off: int, H: int, end: int) -> SoundObject | None:
    """Parse a sound object whose size field is at ``off + H``."""
    if off + H + 12 > end:
        return None
    size = struct.unpack_from("<I", buf, off + H)[0]
    t = off + H + 4
    # The size field may overshoot the buffer by a few bytes (seen at a sector
    # boundary); the hard requirement is that the table and ADPCM regions fit.
    if size < 0x18 or t + size > end + 0x10:
        return None
    tsize, rsize, flags, extra = struct.unpack_from("<HBBI", buf, t)
    if rsize != 0x14 or tsize < 0x18 or tsize > size:
        return None
    term = t + tsize - 8
    if buf[term:term + 8] != TERMINATOR:
        return None
    samples = []
    p = t + 8
    while p < term:
        if p + 8 > term:
            return None
        sid, _p1, _p2, _z, k = struct.unpack_from("<HHHBB", buf, p)
        p += 8
        if k == 0 or p + k * 0x14 > term:
            return None
        regs = []
        for _ in range(k):
            _a, astart, aend, pitch = struct.unpack_from("<IIIH", buf, p)
            if aend < astart:
                return None
            regs.append(Region(astart, aend - astart, pitch))
            p += 0x14
        samples.append(Sample(sid, tuple(regs)))
    if p != term or not samples:
        return None
    adpcm = min(size, end - t) - tsize
    for s in samples:
        for r in s.regions:
            if r.adpcm_offset + r.adpcm_length > adpcm:
                return None
    m1 = struct.unpack_from("<I", buf, off + 4)[0]
    if H == 0:
        kind, line, group = "bank", None, None
    elif m1 == CLIP_MAGIC1:
        kind = "clip"
        line, group = struct.unpack_from("<HB", buf, off + 12)
    elif buf[off + 4:off + 7] == b"SLZ":
        kind, line, group = "slz", None, None
    else:
        kind, line, group = "table", None, None
    return SoundObject(off, min(H + 4 + size, end - off), H, size, tsize, flags, extra,
                       tuple(samples), kind, line, group)


@dataclass(frozen=True)
class PackItem:
    tag: int
    offset: int
    size: int


def parse_pack(buf: bytes) -> list[PackItem] | None:
    """Parse a generic pack; None unless the layout fits exactly."""
    if len(buf) < 16:
        return None
    n = struct.unpack_from("<I", buf, 0)[0]
    if not 1 <= n <= 0x400:
        return None
    hdr = 8 + 8 * n
    if hdr > len(buf):
        return None
    items = []
    o = hdr
    for i in range(n):
        tag, size = struct.unpack_from("<II", buf, 8 + 8 * i)
        if o + size > len(buf):
            return None
        items.append(PackItem(tag, o, size))
        o += size
    if len(buf) - o >= 2048 or any(buf[o:]):
        return None
    return items


def build_pack(template: bytes, items: list[bytes]) -> bytes:
    """Rebuild a pack keeping the template's count, unknown word and tags."""
    n, unk = struct.unpack_from("<II", template, 0)
    if n != len(items):
        raise ValueError("item count mismatch")
    out = bytearray(struct.pack("<II", n, unk))
    for i, it in enumerate(items):
        tag = struct.unpack_from("<I", template, 8 + 8 * i)[0]
        out += struct.pack("<II", tag, len(it))
    for it in items:
        out += it
    return bytes(out)


@dataclass(frozen=True)
class SoundRef:
    """Location of a sound object.

    ``item`` is None for a whole entry. ``compressed`` means the pack item is a
    single SLZ block whose *decompressed* content is the sound object; in that
    case ``obj`` offsets refer to the decompressed buffer, ``item_size`` is the
    compressed item size and ``slz_type`` its SLZ type.
    """
    slot: int
    item: int | None
    obj: SoundObject
    compressed: bool = False
    slz_type: int | None = None
    item_size: int | None = None


def sound_bytes(buf: bytes, ref: SoundRef) -> bytes:
    """The (decompressed) bytes of the sound object ``ref`` within entry ``buf``."""
    if ref.compressed:
        it = parse_pack(buf)[ref.item]
        data = slz.decompress(buf[it.offset:it.offset + it.size])
        return data[ref.obj.offset:ref.obj.offset + ref.obj.length]
    return buf[ref.obj.offset:ref.obj.offset + ref.obj.length]


def pad_pack_item(obj: bytes, so: SoundObject) -> bytes:
    """Pad a sound object the way raw pack items are stored on disc.

    Observed on every uncompressed sound item of all four discs:
    ``(item_len - H - table_size) % 128 == 0``, zero-filled.
    """
    n = len(obj)
    rem = (n - so.script_end - so.table_size) % 128
    return obj + b"\x00" * ((128 - rem) % 128)


def item_bytes(buf: bytes, item: int) -> bytes:
    it = parse_pack(buf)[item]
    return buf[it.offset:it.offset + it.size]


def find_sounds(slot: int, buf: bytes) -> list[SoundRef]:
    so = parse_sound(buf, 0)
    if so is not None:
        return [SoundRef(slot, None, so)]
    items = parse_pack(buf)
    if not items:
        return []
    out = []
    for i, it in enumerate(items):
        so = parse_sound(buf, it.offset, it.offset + it.size)
        if so is not None:
            out.append(SoundRef(slot, i, so, item_size=it.size))
            continue
        sub = buf[it.offset:it.offset + it.size]
        if slz.is_slz(sub) and len(slz.blocks(sub)) == 1:
            try:
                dec = slz.decompress(sub)
            except slz.SlzError:
                continue
            so = parse_sound(dec, 0)
            if so is not None:
                # Re-base: offsets are relative to the decompressed buffer.
                out.append(SoundRef(slot, i, so, True, sub[3], it.size))
    return out
