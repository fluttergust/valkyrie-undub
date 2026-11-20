"""Decode the English dialogue text of every field event block in VALKYRIE.BIN (no game data in the repo).

Field scenes keep their text and a per-scene glyph subset in one pack member with tag 4 (SLZ inside the
pack; archive slots ~3610-4727 on both discs). The scene font is the last run of 24-byte glyphs starting with
the digit signature (``fieldfont.SIG``); text byte ``c`` (< 0x80) draws scene glyph ``c - 0x25``. Each scene
glyph is identified by comparing it with the global font (``discfont``) with rows rotated; glyphs 0-9 are the
digits, a few are scene-only symbols (``SPECIAL``).

Control codes (all ``xx 80 ...``)::

    93 80 pp 00     message start (pp = portrait / speaker slot); the speaker name follows, ended by 80 80
    80 80           line break
    83 80 nn        inline delay (nn frames; ff = wait)
    8c 80 nn 0b     end of page (wait for button); text may continue without a new 93
    91 80 a b [00]  4 bytes + an optional 00 (timing / window)
    84 80 n         3 bytes; 86 80 a b / 87 80 a b: 4 bytes
    other xx 80     2 bytes (81, 85, 92 ...)
    (lengths measured: the byte after the code is text or another code in >75% of uses)
    03..0c          digits 0-9 (single bytes below the glyph range); 0d = "!"
    00              padding, skipped (00 00 = end of the text)
    other < 25      not text: ends the message (binary data after the last one)
"""
from __future__ import annotations

import re

from .font import Font

GLYPH = 24
CODE_BASE = 0x25
# digits "0" and "1" (12 rows each, u16 LE): the scene font starts with them (last match in the block)
SIG = bytes.fromhex("000000000038004400820082008200820082004400380000"
                    "000000000008007800080008000800080008000800080000")

NAMES = {95: ".", 98: ":", 100: "・", 103: "(", 104: ")", 106: "+", 107: "?", 108: "-"}
# scene glyphs 10-12 and these indices are fixed in every block; 27/30/34 are scene-only shapes
SPECIAL = {11: "!", 27: "▼", 30: "“", 34: "”"}
# other scene-only shapes, by raw (stored) glyph bytes
SHAPES = {
    "000000780040004000400040004000400040004000000000": "「",
    "000000000000000000000000000000000000006000600000": ".",     # big ellipsis dot
    "008000000000000000000000000000000000006000600000": ".",     # same dot, stray bit in stored row 0
    "000800000000000000000000000000000000006000600000": ".",
    "00000000000080200011000a0004000a0011802000000000": "×",
    "000000000081004200240018001800180024004200810000": "×",
    "000000000004000a000a00110011802040404040e0ff0000": "△",
    "00000000c07f4040404040404040404040404040c07f0000": "□",
    "0000000e803140404040208020802080404040408031000e": "○",
    "0000000400040004000400040004000400040015000e0004": "↓",
}
# single bytes below the glyph range that are text: 03-0c digits ("The 4th and 14th Runes"), 0d "!"
LOW = {**{0x03 + i: str(i) for i in range(10)}, 0x0D: "!"}
CTRL_LEN = {0x80: 2, 0x83: 3, 0x84: 3, 0x86: 4, 0x87: 4, 0x8C: 4, 0x91: 4, 0x93: 4}
MAX_HAM = 2


def _ham(x: bytes, y: bytes) -> int:
    return sum(bin(p ^ q).count("1") for p, q in zip(x, y))


class GlyphNamer:
    """Names scene glyphs by matching them against the global font (rows rotated)."""

    def __init__(self, font: Font):
        self.names = [chr(0x20 + i) if i < 0x5F else NAMES.get(i, "¤") for i in range(len(font.glyphs))]
        self.rot = [g[22:] + g[:22] for g in font.glyphs]
        self.exact: dict[bytes, int] = {}
        for i, r in enumerate(self.rot):
            self.exact.setdefault(r, i)          # lowest index wins (blank = space)

    def name(self, k: int, g: bytes, used: set[int] = frozenset()) -> str:
        """``used``: font indices already matched exactly by other scene glyphs (ties go elsewhere,
        e.g. a near-match "j" is not taken for the "i" already present)."""
        if k < 10:
            return str(k)
        if g in self.exact:
            return self.names[self.exact[g]]
        if g.hex() in SHAPES:
            return SHAPES[g.hex()]
        h, _, i = min((_ham(g, r), i in used, i) for i, r in enumerate(self.rot))
        return self.names[i] if h <= MAX_HAM else SPECIAL.get(k, "¤")


def scene_table(block: bytes, namer: GlyphNamer) -> tuple[int, dict[int, str]]:
    fb = block.rfind(SIG)
    if fb < 0:
        raise ValueError("no scene font")
    gs = [block[fb + k * GLYPH:fb + (k + 1) * GLYPH] for k in range(0x80 - CODE_BASE)]
    gs = [g for g in gs if len(g) == GLYPH]
    used = {namer.exact[g] for g in gs[10:] if g in namer.exact}
    return fb, {k: namer.name(k, g, used) for k, g in enumerate(gs)}


def _speaker_at(block: bytes, i: int, end: int, longest: int = 24) -> bool:
    """A speaker name (text bytes) followed by ``80 80`` starts at ``i``."""
    j = i
    while j < min(end, i + longest) and CODE_BASE <= block[j] < 0x80:
        j += 1
    return j > i and block[j:j + 2] == b"\x80\x80"


def messages(block: bytes, namer: GlyphNamer) -> list[dict]:
    """[{"offset", "speaker", "text"}]: one per ``93 80`` message; lines joined by spaces."""
    end, tab = scene_table(block, namer)
    out, cur, line, i = [], None, [], 0

    def flush_line():
        if cur is not None and line:
            cur["lines"].append("".join(line))
        line.clear()

    while i < end:
        b = block[i]
        if b >= 0x80 and i + 1 < end and block[i + 1] == 0x80:
            if b == 0x93:
                flush_line()
                cur = {"offset": i, "lines": []}
                out.append(cur)
            elif b != 0x83:              # all but inline delays end a line (8c = page end; the message
                flush_line()             # may go on without a new speaker)
            i += CTRL_LEN.get(b, 2)
            if b == 0x91 and i < end and block[i] == 0:
                i += 1                   # 91 80 a b [00]
            continue
        if cur is not None:
            c = b - CODE_BASE
            if 0 <= c < len(tab):
                line.append(tab[c])
            elif b in LOW:
                line.append(LOW[b])
            elif b == 0 and i + 2 < end and block[i + 1] >= 0x80 and block[i + 2] == 0x80:
                pass                     # padding before a code ("Wha...?" 00 85 80 "In war...")
            elif b == 0 and _speaker_at(block, i + 1, end):
                flush_line()             # 00 name 80 80: next message, same scene, no 93 header
                cur = {"offset": i, "lines": []}
                out.append(cur)
            else:                        # 00 before other text ends the message ("...at all!" 00 "This is
                flush_line()             # transfer equipment."), as does any other low byte
                cur = None
        i += 1
    flush_line()
    res = []
    for m in out:
        if not m["lines"]:
            continue
        speaker, *body = m["lines"]
        res.append({"offset": m["offset"], "speaker": speaker.strip(),
                    "text": " ".join(s.strip() for s in body).replace("「", "").strip()})
    return res


# from vpsub tools/matchsubs.py: a cue refers to a token range of an on-disc message
def token_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of the tokens of ``text``: whitespace-separated, punctuation attached; an ellipsis
    ends a token ("I...you" -> "I...", "you")."""
    t = text.replace("・", ".").replace("「", " ")          # same length: spans index the original text
    out = []
    for m in re.finditer(r"\S+", t):
        a = m.start()
        for e in re.finditer(r"\.{2,}", m.group(0)):
            b = m.start() + e.end()
            if b < m.end():
                out.append((a, b))
                a = b
        out.append((a, m.end()))
    return out


def cut(msg_text: str, r: tuple[int, int]) -> str:
    """The text of tokens ``r[0]..r[1]-1`` of ``msg_text`` (see ``tokens``), spacing as on disc."""
    sp = token_spans(msg_text)[r[0]:r[1]]
    return msg_text[sp[0][0]:sp[-1][1]].replace("・", ".") if sp else ""
