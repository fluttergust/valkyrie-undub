"""Ending-FMV subtitles (ported from vpsub ``src/vpsub/fmvsub.py`` + ``tools/mkpatch.py``, vpsub 9c46426).

The ending FMV (slot 4864, disc 2; the JP voices are the separate clips 4977-4980 the undub swapped in) is a
24-bit MDEC movie, so GPU text cannot be drawn on it. ``fmvsub.S`` (hand-written MIPS I, header comment) is
called from the FMV player's frame swap and composites one outlined line per cue into the finished frame on
the CPU. It is appended to the FMV player item of pack 4616 (item 3, tag 0x16; copied by field module 2292 to
0x800BEA00 in a 0x13880-byte heap block, whose last 0x400 bytes are unused), within the slot's sector
slack, so the disc layout stays the same. The player's ``jal DrawSync; move a0,zero`` at item offset 0xC8C
becomes ``jal hook; move a0,s0``. See TECHNICAL.md "Ending FMV subtitles".

Like the vision overlay, the build needs no compiler: the assembled code is kept in ``fmvsub.bin`` (``hook``
up to ``cols``), with the data addresses it was assembled against (``LAYOUT``; the code reaches the data as
``0x800D0000 + %lo`` and holds the strip width ``WT`` as an immediate). The data (glyph strip from the disc's
font, column table, pages) is built at build time; ``image`` checks it lands where the code expects.
``python3 tools/subtitle_overlay.py`` reassembles both routines (``compile``).

Cues are FMV decoded-frame numbers (0x800C0C40, 15 fps), measured by vpsub on the ref2 recording. A page shows
while ``start <= frame < end``. Each cue is one centred line in the game's dialogue font, wrapped like the
vision subtitles (296 px). Only the glyphs the text uses are stored, in one 12-row strip, plus a blank 1 px
glyph for the touch gap. ``colour_map`` decodes the data the way the routine does; the tests compare it with
``Font.raster``.

Pack 4616 is the same on both US discs and untouched by the voice swap; both discs get the patch (as vpsub).
"""
from __future__ import annotations

import struct
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent

SCREEN_W = 320
ROWS = 12
BOTTOM = 209            # top ink row (pages.BOTTOM)
COLX, MW = 10, 300      # map: screen x COLX .. COLX + MW - 1 (fmvsub.S)
INK, DARK = 248, 16
GAP = "gap"

# (start, end, text) in FMV decoded frames; voice clips (US/JP) in TECHNICAL "Ending FMV subtitles"
CUES = [
    (44, 145, "Loki, I never understood what was inside your heart."),     # 4860
    (187, 265, "I wonder if I'll ever be able to understand you."),        # 4861
    (280, 303, "I think you already do."),                                 # 4861
    (458, 505, "Lucian, shall we go?"),                                    # 4863
]

SLOT, ITEM, TAG = 4616, 3, 0x16                 # FMV player code item (copied to BASE by module 2292)
BASE, SIZE = 0x800BEA00, 0x133DC
SLOT_LEN, DATA_END = 0x4B800, 0x4B45C           # slot bytes (sector padded), end of its pack data
SITE = 0xC8C                                    # item offset of `jal DrawSync; move a0,zero` (0x800BF68C)
ORIG = (0x0C00836B, 0x00002021)
MOVE_A0_S0 = 0x02002021
HOOK = BASE + SIZE                              # 0x800D1DDC
# fmvsub.bin was assembled with the data at these addresses (and strip width WT); tools/subtitle_overlay.py
LAYOUT = {"WT": 135, "cols": 0x800D1FDC, "bits": 0x800D1FF9, "pages": 0x800D20C4, "fmvend": 0x800D2170}


class FmvError(RuntimeError):
    pass


def jal(target: int) -> int:
    return 0x0C000000 | (target >> 2) & 0x03FFFFFF


# --- data ---------------------------------------------------------------------------------------------------
def line_glyphs(font, text: str) -> tuple[int, list]:
    """(screen x of the pen, [font glyph index or GAP]) for a centred line, ``Font.layout`` spacing."""
    x0 = (SCREEN_W - font.width(text)) // 2
    out, pen = [], 0
    for i, x in font.layout(text):
        out += [GAP] * (x - pen) + [i]
        pen = x + font.widths[i]
    return x0, out


def build(font, cues=CUES) -> tuple[int, bytes, bytes, bytes]:
    """(strip width WT, cols, bits, pages)."""
    lines = [line_glyphs(font, t) for _, _, t in cues]
    used = list(dict.fromkeys(g for _, gl in lines for g in gl))
    widths = [1 if g == GAP else font.widths[g] for g in used]
    cols = [0]
    for w in widths:
        cols.append(cols[-1] + w)
    wt = cols[-1]
    if wt > 255 or len(used) > 255:
        raise FmvError("glyph strip too wide")
    bits = []
    for j in range(ROWS):
        for g, w in zip(used, widths):
            r = 0 if g == GAP else font.rows(g)[j]
            bits += [r >> (15 - k) & 1 for k in range(w)]
    strip = bytes(sum(b << (7 - k) for k, b in enumerate(bits[n:n + 8])) for n in range(0, len(bits), 8))
    idx = {g: n for n, g in enumerate(used)}
    pages = bytearray()
    prev = 0
    for (s, e, t), (x0, gl) in zip(cues, lines):
        x = x0 - COLX
        if not prev <= s < e < 0x10000 or s == 0:
            raise FmvError(f"cue {t!r}: frames must be ascending and non-zero")
        if x < 1 or x0 + font.width(t) > COLX + MW - 1 or len(gl) > 255:
            raise FmvError(f"line too wide: {t!r}")
        pages += struct.pack("<HHBB", s, e, len(gl), x) + bytes(idx[g] for g in gl)
        pages += bytes(len(pages) & 1)
        prev = e
    pages += bytes(2)
    return wt, bytes(cols), strip, bytes(pages)


def asm(font, cues=CUES) -> str:
    """fmvdata.inc for fmvsub.S."""
    wt, cols, bits, pages = build(font, cues)

    def rows(name, data):
        out = [f"{name}:"]
        for n in range(0, len(data), 16):
            out.append("    .byte " + ", ".join(str(b) for b in data[n:n + 16]))
        return out
    return "\n".join([f"    .equ WT, {wt}", '    .section .rodata.fmv,"a",@progbits',
                      *rows("cols", cols), *rows("bits", bits), "    .balign 2", *rows("pages", pages),
                      "    .text", ""])


def page_at(pages: bytes, frame: int) -> int | None:
    """Offset of the page record shown at ``frame`` (fmvsub.S ``page``), None if none."""
    q = 0
    while True:
        if struct.unpack_from("<H", pages, q)[0] == 0:
            return None
        s, e, n = struct.unpack_from("<HHB", pages, q)
        if s <= frame < e:
            return q
        q = (q + 7 + n) & ~1


def colour_map(wt: int, cols: bytes, bits: bytes, pages: bytes, q: int) -> list[list[int]]:
    """14 x MW map (0, DARK, INK) of the page at ``pages[q]``, built like fmvsub.S ``found``/``char``."""
    m = [[0] * MW for _ in range(ROWS + 2)]
    n, x = pages[q + 4], pages[q + 5]
    for g in pages[q + 6:q + 6 + n]:
        c0, w = cols[g], cols[g + 1] - cols[g]
        for j in range(ROWS):
            for k in range(w):
                b = j * wt + c0 + k
                if bits[b >> 3] << (b & 7) & 0x80:
                    px = x + k
                    for dy, dx in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                        m[j + 1 + dy][px + dx] |= DARK
                    m[j + 1][px] = INK
        x += w
    return m


# --- code ---------------------------------------------------------------------------------------------------
def code() -> bytes:
    """The assembled routine, ``hook`` up to ``cols``."""
    return (HERE / "fmvsub.bin").read_bytes()


def layout(font, cues=CUES) -> tuple[dict, bytes]:
    """(label addresses and WT, data from ``cols`` on, padded to 4) for the committed code's size."""
    wt, cols, bits, pages = build(font, cues)
    c = HOOK + len(code())
    b = c + len(cols)
    p = b + len(bits) + (b + len(bits)) % 2
    data = cols + bits + bytes(p - b - len(bits)) + pages
    return {"WT": wt, "cols": c, "bits": b, "pages": p, "fmvend": p + len(pages)}, data + bytes(-(c + len(data)) % 4)


def image(font, cues=CUES) -> bytes:
    """Routine + data, placed at HOOK; raises FmvError if the data does not match ``fmvsub.bin``."""
    lay, data = layout(font, cues)
    if lay != LAYOUT:
        raise FmvError(f"FMV subtitle data layout {lay} differs from fmvsub.bin's {LAYOUT}: "
                       "run tools/subtitle_overlay.py")
    img = code() + data
    if len(img) > SLOT_LEN - DATA_END:
        raise FmvError(f"FMV subtitles: {len(img)} B, the slot has {SLOT_LEN - DATA_END} B free")
    return img


def compile(work: Path, font, cues=CUES) -> tuple[bytes, dict[str, int]]:
    """fmvsub.S + its data assembled at HOOK with clang/ld.lld (vpsub mkpatch.compile_fmv):
    (image padded to 4, symbols)."""
    from .patch import CFLAGS, check_hazards
    work.mkdir(parents=True, exist_ok=True)
    (work / "fmvdata.inc").write_text(asm(font, cues))
    obj, elf, binf = work / "fmvsub.o", work / "fmvsub.elf", work / "fmvsub.bin"
    subprocess.run(["clang", *CFLAGS, "-I", str(work), "-c", str(HERE / "fmvsub.S"), "-o", str(obj)], check=True)
    subprocess.run(["ld.lld", "-T", str(HERE / "fmvsub.ld"), str(obj), "-o", str(elf)], check=True)
    subprocess.run(["llvm-objcopy", "-O", "binary", "--only-section=.text", "--only-section=.rodata",
                    str(elf), str(binf)], check=True)
    syms = {}
    for line in subprocess.run(["llvm-nm", str(elf)], check=True, capture_output=True, text=True).stdout.split("\n"):
        f = line.split()
        if len(f) == 3:
            syms[f[2]] = int(f[0], 16)
    img = binf.read_bytes()
    if syms["hook"] != HOOK or len(img) != syms["fmvend"] - HOOK:
        raise FmvError(f"fmvsub: hook {syms['hook']:#x}, expected {HOOK:#x}")
    for k in ("cols", "bits", "pages", "fmvend"):
        if syms[k] >> 15 != 0x800D0000 >> 15:       # the code reaches them as 0x800D0000 + %lo
            raise FmvError(f"fmvsub: {k} at {syms[k]:#x} outside 0x800D0000..0x800D7FFF")
    try:
        check_hazards(img, HOOK, syms["cols"])
    except Exception as e:
        raise FmvError(f"fmvsub: {e}") from e
    syms["WT"] = build(font, cues)[0]
    return img + bytes(-len(img) % 4), syms


# --- pack patch ---------------------------------------------------------------------------------------------
def _check_pack(slot: bytes):
    from ..assets.voice import parse_pack
    items = parse_pack(slot)
    it = items[ITEM] if items and len(items) > ITEM else None
    if len(slot) != SLOT_LEN or not it or it.tag != TAG or it.size != SIZE \
            or items[-1].offset + items[-1].size != DATA_END or any(slot[DATA_END:]):
        raise FmvError(f"slot {SLOT}: not the expected FMV player pack")
    return items, it


def patch_slot(slot: bytes, overlay: bytes) -> bytes:
    """Pack 4616 with ``overlay`` appended to the player item and the swap's DrawSync call sent to HOOK."""
    from ..assets.voice import build_pack
    items, it = _check_pack(slot)
    item = bytearray(slot[it.offset:it.offset + it.size])
    if struct.unpack_from("<2I", item, SITE) != ORIG:
        raise FmvError(f"slot {SLOT}: hook site changed (already patched?)")
    struct.pack_into("<2I", item, SITE, jal(HOOK), MOVE_A0_S0)
    new = build_pack(slot, [bytes(item) + overlay if n == ITEM else slot[i.offset:i.offset + i.size]
                            for n, i in enumerate(items)])
    if len(new) > SLOT_LEN:
        raise FmvError("FMV pack grew past its slot")
    return new + bytes(SLOT_LEN - len(new))


def fmv_slot(text_archive) -> tuple[bytes, dict]:
    """(new slot 4616, summary) with the font read from ``text_archive`` (the US archive)."""
    from .font import Font, find_font
    font = Font(find_font(text_archive))
    img = image(font)
    return patch_slot(text_archive[SLOT], img), {
        "cues": len(CUES), "code_bytes": len(code()), "data_bytes": len(img) - len(code()),
        "slot_bytes_left": SLOT_LEN - DATA_END - len(img)}


def verify_slot(us_data: bytes, out_data: bytes) -> None:
    """The output pack: the US pack with only the hook pair changed and the routine appended to item 3."""
    from ..assets.voice import parse_pack
    items, it = _check_pack(us_data)
    new = parse_pack(out_data)
    if not new or [i.tag for i in new] != [i.tag for i in items] or len(out_data) != SLOT_LEN:
        raise FmvError(f"slot {SLOT}: pack layout changed")
    for n, (a, b) in enumerate(zip(items, new)):
        u, o = us_data[a.offset:a.offset + a.size], out_data[b.offset:b.offset + b.size]
        if n != ITEM:
            if u != o:
                raise FmvError(f"slot {SLOT} item {n}: modified")
            continue
        want = bytearray(u)
        struct.pack_into("<2I", want, SITE, jal(HOOK), MOVE_A0_S0)
        if o[:SIZE] != want or not o[SIZE:].startswith(code()) or HOOK + len(o) - SIZE > LAYOUT["fmvend"] + 4:
            raise FmvError(f"slot {SLOT}: player item not patched as expected")
