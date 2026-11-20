"""Subtitle pages as the in-game runtime sees them, the reference model of ``overlay.c`` and the data packer
(from vpsub ``src/vpsub/pages.py``).

A *page* is one cue (1-3 centred lines) of one vision clip, timed in vsyncs (59.94 Hz) from the clip start.
Each glyph fades in by ``FADE_IN`` per vsync, one character every ``speed`` vsyncs (spaces take a slot, as
in the field text); from ``end`` the whole page fades out by ``fade`` per vsync and is gone at ``stop``.
``fade`` is ``FADE_OUT`` unless the next page (or the end of the vision) comes sooner, then the fade is
squeezed so the page is clear by then; ``end`` is pulled back to leave at least ``MIN_FADE`` vsyncs for it
(no page pops off in one frame). A page stays fully shown for at least ``MIN_SHOW`` vsyncs when there is
room. At most one page shows at a time.

Clips start when the game says so, not on a fixed clock (measured starts come up to ~1 s earlier than the
clip lengths and delays predict), so a page may outlive its clip: the runtime keeps drawing it (*carry*)
through the following clips until its ``stop``, unless a new page wants the screen, then the carried page is
cut with a fast fade (``CUT_FADE`` per vsync) and the new one shows once it is gone (``Runtime``). The
vision ends when its last voice is done, but its picture stays up for seconds after, so a page still showing
then finishes its fade-out; each clip's pages must be clear by ``AFTER_END`` vsyncs after the predicted
vision end (``end_limits``).

Font glyphs are 4bpp cells (16 px wide, 14 rows: index 0 clear, 1 outline, 2 ink) uploaded to VRAM by the
runtime; ``pack()`` writes the data layout documented at the top of ``overlay.c``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FPS = 59.94          # NTSC vsyncs per second
SCREEN_W = 320
PITCH = 13           # px between lines (the WLM vision text)
BOTTOM = 209         # y of the last line's top row of ink on the 320x240 screen (222 was cut by overscan)
FULL = 0x80          # sprite modulation at full strength
FADE_IN = 4          # per vsync per character (0 -> 0x80 in 32 vsyncs)
FADE_OUT = 2         # per vsync for the whole page (0x80 -> 0 in 64 vsyncs)
CHAR_FRAMES = 3      # vsyncs between characters (the WLM text's usual 83 80 03)
MIN_FADE = 16        # vsyncs kept for the fade-out before the next page / the vision end (fade <= 8)
MIN_SHOW = 90        # vsyncs a page is at least fully shown (start -> end), room permitting
CUT_FADE = 8         # per vsync: a carried page cut by a new one (0x80 -> 0 in 16 vsyncs)
AFTER_END = 72       # vsyncs a page may outlast the predicted vision end (a full fade-out + 8)
CELL_W, CELL_H = 16, 14
NVIS = 27
MAX_CELLS = 16 * 13   # VRAM cell area (960,256)-(1023,463), below the CLUTs at y 464


@dataclass
class Glyph:
    x: int           # screen position of the cell's top-left (320x240)
    y: int
    cell: int        # font glyph index
    order: int       # reveal slot
    w: int           # sprite width (outline included)


@dataclass
class Page:
    start: int       # vsyncs from clip start
    end: int         # fade-out starts
    stop: int        # first vsync with nothing drawn
    speed: int       # vsyncs per character
    fade: int        # modulation lost per vsync after ``end``
    glyphs: list[Glyph] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def cells(self) -> list[int]:
        """Distinct font glyphs in first-use order (VRAM slot = position)."""
        return list(dict.fromkeys(g.cell for g in self.glyphs))


def layout(font, lines: list[str]) -> list[Glyph]:
    """One cell per non-space character: centred lines, ``PITCH`` apart, last line at ``BOTTOM``."""
    out, order = [], 0
    top = BOTTOM - 1 - PITCH * (len(lines) - 1)
    for n, line in enumerate(lines):
        x0 = (SCREEN_W - font.width(line)) // 2
        for (i, px), ch in zip(font.layout(line), line):
            order += 1
            if ch != " ":
                out.append(Glyph(x0 + px - 1, top + n * PITCH, i, order - 1, font.widths[i] + 4))
    return out


def speed_for(n_chars: int, frames: int) -> int:
    """``CHAR_FRAMES``, faster only when the reveal would not finish in 3/4 of the page."""
    s = CHAR_FRAMES
    while s > 1 and n_chars * s > frames * 3 // 4:
        s -= 1
    return s


def clip_pages(font, cues: list[dict], limit: int | None = None) -> list[Page]:
    """Pages of one clip from resolved cues (``start``/``end`` in seconds, ``lines``). ``limit``: vsync
    the drawing must be over by (the vision closes when its last clip ends)."""
    pages = []
    for q in cues:
        a, b = round(q["start"] * FPS), round(q["end"] * FPS)
        b = max(b, a + MIN_SHOW)
        n = sum(len(s) for s in q["lines"])
        pages.append(Page(a, b, 0, speed_for(n, b - a), FADE_OUT, layout(font, q["lines"]), list(q["lines"])))
    for i, p in enumerate(pages):
        nxt = pages[i + 1].start if i + 1 < len(pages) else limit
        if nxt is not None:
            p.end = max(p.start + 1, min(p.end, nxt - MIN_FADE))
        p.fade = FADE_OUT
        if nxt is not None and p.end + -(-FULL // p.fade) > nxt:
            p.fade = -(-FULL // max(1, nxt - p.end))
        p.stop = p.end + -(-FULL // p.fade)
        if nxt is not None:
            p.stop = min(p.stop, nxt)
    return pages


def modulation(p: Page, g: Glyph, t: int) -> int:
    """Sprite modulation (0..0x80) of glyph ``g`` at ``t`` vsyncs after the clip start (= overlay.c)."""
    m = min(FULL, (t - p.start - g.order * p.speed + 1) * FADE_IN)
    if t >= p.end:
        m = min(m, FULL - (t - p.end + 1) * p.fade)
    return max(0, m)


GAME_SUBTITLED = {26}   # Weeping Lily Meadow: the field scene shows its own text (TECHNICAL "Vision text fade")

def merge_discs(discs: list[tuple[dict, dict]]) -> tuple[dict, dict]:
    """One ``(vis, subs)`` pair for all discs from per-disc ``(vis by id, subs by id)``.

    Both discs index every vision, but each disc only holds the voice clips of some of them (``on_disc``);
    on the other disc the absent clips have no length and no transcription, so its data for that vision is
    partial (vision 16 on disc 1 had one clip of seven). Taking every vision from a disc that holds all its
    clips gives the same subtitle data for both images, so a vision that can play on either disc (Shiho's,
    chapter 5 or 6) is subtitled the same way. Complete copies on two discs must agree."""
    vis, subs = {}, {}
    for vid in sorted({i for v, _ in discs for i in v}):
        full = [(v[vid], s.get(vid)) for v, s in discs if vid in v and v[vid]["on_disc"]]
        if not full:
            raise ValueError(f"vision {vid}: no disc holds all its clips")
        if any(f != full[0] for f in full[1:]):
            raise ValueError(f"vision {vid}: complete on several discs but the data differs")
        vis[vid] = full[0][0]
        if full[0][1] is not None:
            subs[vid] = full[0][1]
    return vis, subs


def build_visions(font, subs: dict, vis: dict) -> dict[int, list[list[Page] | None]]:
    """``{vision id: [pages of clip k or None]}`` from resolved cues (``subs.py --json`` visions by id) and
    the ``scvisions.py`` clip lists (by id). Each clip's pages must be clear before the predicted vision end
    (``end_limits``). Visions the game already subtitles are left out."""
    out = {}
    for vid, v in vis.items():
        if vid in GAME_SUBTITLED:
            continue
        ent = {c["slot"]: c for c in subs.get(vid, {}).get("clips", [])}
        limits = end_limits(v["clips"])
        clips = []
        for k, c in enumerate(v["clips"]):
            e = ent.get(c["slot"])
            clips.append(clip_pages(font, e["cues"], limits[k]) if e and e["cues"] else None)
        out[vid] = clips
    return out


def end_limits(clips: list[dict]) -> list[int | None]:
    """Per clip: vsyncs from its start to the predicted vision end (clip lengths + 2 vsyncs per delay tick)
    plus ``AFTER_END``; None if a length is unknown."""
    out: list[int | None] = []
    for k in range(len(clips)):
        rest = clips[k:]
        if any(not c.get("seconds") for c in rest):
            out.append(None)
            continue
        out.append(sum(round(c["seconds"] * FPS) for c in rest) + sum(2 * c.get("delay", 0) for c in rest[1:])
                   + AFTER_END)
    return out


def active(pages: list[Page], t: int) -> Page | None:
    return next((p for p in pages if p.start <= t < p.stop), None)


class Runtime:
    """Which page the overlay draws each vsync (= ``hook()`` in overlay.c). ``step(now, started, ended)``:
    ``started`` is the clip index whose playback began this vsync (else None), ``ended`` that the vision id
    is 0 (a page still showing finishes its fade-out, nothing new starts); returns
    ``(page, t, cap)`` (t = vsyncs since that page's clip start, cap = modulation ceiling) or None."""

    def __init__(self, clips: list[list[Page] | None]):
        self.clips = clips
        self.clip, self.t0 = -1, 0
        self.last, self.last_t0 = None, 0          # page drawn on the previous vsync
        self.carry, self.carry_t0, self.cut = None, 0, None

    def step(self, now: int, started: int | None = None, ended: bool = False):
        if ended:
            if self.clip != -2:
                if self.carry is None and self.last is not None and now - self.last_t0 < self.last.stop:
                    self.carry, self.carry_t0, self.cut = self.last, self.last_t0, None
                self.last, self.clip = None, -2
            started = None
        if started is not None and started != self.clip:
            if self.carry is None and self.last is not None and now - self.last_t0 < self.last.stop:
                self.carry, self.carry_t0, self.cut = self.last, self.last_t0, None
            self.last = None
            self.clip, self.t0 = started, now
        pages = self.clips[self.clip] if 0 <= self.clip < len(self.clips) else None
        pg = active(pages or [], now - self.t0)
        if self.carry is not None:
            ct = now - self.carry_t0
            if ct < self.carry.stop:
                if pg is not None and self.cut is None:
                    self.cut = now
                cap = FULL if self.cut is None else FULL - (now - self.cut + 1) * CUT_FADE
                if cap > 0:
                    return self.carry, ct, cap
            self.carry = None
        if pg is None:
            return None
        self.last, self.last_t0 = pg, self.t0
        return pg, now - self.t0, FULL


def cell_bitmap(font, i: int) -> list[list[int]]:
    """14 rows x 16 px of glyph ``i``: 0 clear, 1 outline (4-neighbour), 2 ink; ink starts at x 1, y 1."""
    img = [[0] * CELL_W for _ in range(CELL_H)]
    for j, r in enumerate(font.rows(i)):
        for q in range(CELL_W - 1):
            if r >> (15 - q) & 1:
                img[1 + j][1 + q] = 2
    for y in range(CELL_H):
        for x in range(CELL_W):
            if img[y][x] == 0 and any(0 <= y + dy < CELL_H and 0 <= x + dx < CELL_W and img[y + dy][x + dx] == 2
                                      for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                img[y][x] = 1
    return img


def cell_words(bitmap: list[list[int]]) -> bytes:
    """4bpp VRAM bytes of a cell (low nibble = left pixel), 8 bytes per row."""
    out = bytearray()
    for row in bitmap:
        for x in range(0, CELL_W, 2):
            out.append(row[x] | row[x + 1] << 4)
    return bytes(out)


def pack(font, visions: dict[int, list[list[Page] | None]]) -> bytes:
    """Runtime data: ``visions[vid][clip]`` = pages (or None)."""
    bank: dict[int, int] = {}
    for clips in visions.values():
        for pages in clips:
            for p in pages or []:
                for c in p.cells():
                    bank.setdefault(c, len(bank))
    head = 4 * NVIS + 8
    body = bytearray()
    vis_off = [0] * NVIS

    def here() -> int:
        return head + len(body)

    for vid in sorted(visions):
        clips = visions[vid]
        if not any(clips):
            continue
        offs = []
        clip_bytes = bytearray()
        base = here() + 4 + 4 * len(clips)
        for pages in clips:
            if not pages:
                offs.append(0)
                continue
            offs.append(base + len(clip_bytes))
            clip_bytes += struct.pack("<I", len(pages))
            for p in pages:
                cells = p.cells()
                if len(cells) > MAX_CELLS or len(p.glyphs) > 255 or p.stop > 0xFFFF:
                    raise ValueError(f"page too large: {p.lines}")
                slot = {c: k for k, c in enumerate(cells)}
                clip_bytes += struct.pack("<HHHBBBBH", p.start, p.end, p.stop, p.speed, p.fade,
                                          len(p.glyphs), len(cells), 0)
                clip_bytes += bytes(bank[c] for c in cells) + b"\0" * (len(cells) & 1)
                for g in p.glyphs:
                    clip_bytes += struct.pack("<HBBBB", g.x & 0xFFFF, g.y, slot[g.cell], g.order, g.w)
                clip_bytes += b"\0" * (-len(clip_bytes) % 4)
        vis_off[vid] = here()
        body += struct.pack("<I", len(clips)) + b"".join(struct.pack("<I", o) for o in offs) + clip_bytes
    cells_off = here()
    for c in sorted(bank, key=bank.get):
        body += cell_words(cell_bitmap(font, c))
    return struct.pack(f"<{NVIS}I", *vis_off) + struct.pack("<II", cells_off, len(bank)) + bytes(body)
