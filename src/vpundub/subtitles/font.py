"""The game's proportional dialogue font, read from VALKYRIE.BIN (nothing from the game is stored in this repository).

The font is a pack member with tag 7 (SLZ-compressed inside the pack). On both US discs it is archive slot 6
(the resident system pack); 2175-2185 hold copies with extra kanji. Layout (little endian)::

    u32 count | u32 12 (rows) | u8 width[count] | count x 24-byte glyph

Glyph = 12 rows of u16, bit 15 = leftmost pixel, top row first (rows 4-9 = x-height, row 11 = descender
line). Glyph ``i`` is ASCII ``0x20 + i`` for 0x20..0x7E; extras: 95 small dot, 100 ``・`` (6 px), 103/104 tall
brackets. The pen advances by ``width[i]`` exactly (checked against the WLM vision text on screen: "Valkyrie"
at x 85,93,99,102,108,114,118,121). A few glyphs fill their whole advance (I R T V W X Y ? ``(``), so the next
glyph's ink can touch theirs: "I..." then reads "I.." (the first dot joins the I's serif). ``layout`` adds
1 px only where two neighbours' ink would touch in the same row (``TOUCH_GAP``). Field scenes copy a per-scene subset of these glyphs into their event
block (pack tag 4) with the rows rotated (row 11 first, see ``fieldfont.py``).

On screen each glyph is white with a 1-pixel dark outline (4-neighbour), the field glyph cache style.
"""
from __future__ import annotations

import struct

from ..archive import slz
from ..assets.voice import parse_pack

GLYPH = 24
ROWS = 12
FONT_SLOT = 6
EXTRA = {"・": 100}
TOUCH_GAP = 1   # extra px between neighbours whose ink would touch (subtitles only; the game has no kerning)


class Font:
    def __init__(self, data: bytes):
        n, rows = struct.unpack_from("<II", data, 0)
        if rows != ROWS or 8 + n + GLYPH * n != len(data):
            raise ValueError("not a field font")
        self.widths = data[8:8 + n]
        self.glyphs = [data[8 + n + GLYPH * i:8 + n + GLYPH * (i + 1)] for i in range(n)]

    def index(self, ch: str) -> int:
        if ch in EXTRA:
            return EXTRA[ch]
        i = ord(ch) - 0x20
        if not 0 <= i < 0x5F:
            raise KeyError(f"no glyph for {ch!r}")
        return i

    def rows(self, i: int) -> list[int]:
        g = self.glyphs[i]
        return [int.from_bytes(g[2 * j:2 * j + 2], "little") for j in range(ROWS)]

    def width(self, text: str) -> int:
        lay = self.layout(text)
        return lay[-1][1] + self.widths[lay[-1][0]] if lay else 0

    def touches(self, a: int, b: int) -> bool:
        """Glyph ``b`` placed ``width[a]`` px after glyph ``a`` would have ink on or right next to ``a``'s ink
        in some row (bit 15 = column 0, so ``>> n`` moves n columns right)."""
        w = self.widths[a]
        return any((ra | ra >> 1) & (rb >> w) for ra, rb in zip(self.rows(a), self.rows(b)))

    def layout(self, text: str) -> list[tuple[int, int]]:
        """(glyph index, pen x) per character; ``TOUCH_GAP`` px more where neighbours' ink would touch."""
        out, x, prev = [], 0, None
        for c in text:
            i = self.index(c)
            if prev is not None and self.touches(prev, i):
                x += TOUCH_GAP
            out.append((i, x))
            x += self.widths[i]
            prev = i
        return out

    def raster(self, text: str) -> list[list[int]]:
        """4bpp-style raster of one line: 0 clear, 1 outline, 2 glyph (size width+2 x 14)."""
        w = self.width(text) + 2 + 4
        img = [[0] * w for _ in range(ROWS + 2)]
        for i, x in self.layout(text):
            for j, r in enumerate(self.rows(i)):
                for q in range(16):
                    if r >> (15 - q) & 1:
                        img[1 + j][1 + x + q] = 2
        for y in range(len(img)):
            for x in range(w):
                if img[y][x] == 0 and any(0 <= y + dy < len(img) and 0 <= x + dx < w and img[y + dy][x + dx] == 2
                                          for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                    img[y][x] = 1
        return img


def find_font(archive) -> bytes:
    """Font bytes from an ``vpundub`` Archive: the first tag-7 pack member with the font layout,
    trying slot 6 first."""
    for s in [FONT_SLOT] + [s for s in archive.slots if s != FONT_SLOT]:
        raw = archive[s]
        for it in parse_pack(raw) or []:
            if it.tag != 7:
                continue
            d = raw[it.offset:it.offset + it.size]
            if slz.is_slz(d):
                d = slz.decompress(d)
            try:
                Font(d)
            except ValueError:
                continue
            return d
    raise ValueError("font not found")
