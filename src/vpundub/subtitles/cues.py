"""Subtitle cues of one disc (``data/discN.json``) resolved against the disc and laid out with the game font.

Ported from vpsub ``tools/subs.py``. Each cue's text is either vpsub's transcription (``text``) or a
reference into the on-disc script (``ref``: field event block slot/member, message offset, token range),
decoded from ``VALKYRIE.BIN`` here, so the official text is never stored. The text is mapped to the font's
character set and word-wrapped with the font's advance widths (``MAX_W`` px, ``MAX_LINES`` lines). A cue too
long for one screen is split at sentence (else word) boundaries into consecutive cues, its time shared by
length. Problems (missing glyphs, unresolvable refs, empty cues) are returned; the build refuses any.
"""
from __future__ import annotations

import re

from . import fieldtext
from .font import Font, find_font
from ..archive import slz
from ..assets.voice import parse_pack

MAX_W = 296          # px per line (screen 320 minus margins)
MAX_LINES = 2
CHARMAP = {"“": '"', "”": '"', "‘": "'", "’": "'", "…": "...", "—": "-", "–": "-"}


def block(archive, slot: int, member: int) -> bytes:
    raw = archive[slot]
    it = parse_pack(raw)[member]
    d = raw[it.offset:it.offset + it.size]
    return slz.decompress(d) if slz.is_slz(d) else d


class Resolver:
    def __init__(self, archive):
        self.archive = archive
        self.namer = fieldtext.GlyphNamer(Font(find_font(archive)))
        self.cache: dict[tuple[int, int], dict[int, dict]] = {}

    def message(self, slot: int, member: int, offset: int) -> dict:
        k = (slot, member)
        if k not in self.cache:
            self.cache[k] = {m["offset"]: m for m in fieldtext.messages(block(self.archive, slot, member), self.namer)}
        return self.cache[k][offset]

    def text(self, ref: dict) -> str:
        m = self.message(ref["slot"], ref["member"], ref["offset"])
        return fieldtext.cut(m["text"], tuple(ref["words"]))


def to_font(text: str, font: Font) -> tuple[str, set[str]]:
    """Text in the font's character set and the characters it lacks."""
    out = "".join(CHARMAP.get(c, c) for c in text)
    out = re.sub(r"\s+", " ", out).strip()
    bad = set()
    for c in out:
        try:
            font.index(c)
        except KeyError:
            bad.add(c)
    return out, bad


def wrap(text: str, font: Font, max_w: int = MAX_W) -> list[str]:
    """Greedy word wrap by pixel width (a word wider than a line stays whole)."""
    lines, cur = [], ""
    for w in text.split(" "):
        t = f"{cur} {w}" if cur else w
        if cur and font.width(t) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def balance(lines: list[str], font: Font, max_w: int = MAX_W) -> list[str]:
    """Two wrapped lines re-broken where the wider line is narrowest (no "... vortex / of time.")."""
    if len(lines) != 2:
        return lines
    ws = " ".join(lines).split(" ")
    best = min(range(1, len(ws)), key=lambda k: (max(font.width(" ".join(ws[:k])), font.width(" ".join(ws[k:]))),
                                               -k))
    a, b = " ".join(ws[:best]), " ".join(ws[best:])
    return [a, b] if max(font.width(a), font.width(b)) <= max_w else lines


def pages(text: str, font: Font, max_w: int = MAX_W, max_lines: int = MAX_LINES) -> list[list[str]]:
    """Split ``text`` into screens of at most ``max_lines`` wrapped lines, preferring sentence breaks."""
    if len(wrap(text, font, max_w)) <= max_lines:
        return [balance(wrap(text, font, max_w), font, max_w)]
    sents = re.findall(r".+?(?:[.!?]+[\"']?(?=\s)|$)", text)
    sents = [s.strip() for s in sents if s.strip()]
    out, cur = [], ""
    for s in sents:
        t = f"{cur} {s}" if cur else s
        if cur and len(wrap(t, font, max_w)) > max_lines:
            out.append(cur)
            cur = s
        else:
            cur = t
    out.append(cur)
    res = []
    for p in out:                         # a single sentence still too long: split by lines
        ls = wrap(p, font, max_w)
        res += [balance(ls[i:i + max_lines], font, max_w) for i in range(0, len(ls), max_lines)]
    return res


def split_times(start: float, end: float, sizes: list[int], pauses: list[float],
                snap: float = 1.0) -> list[tuple[float, float]]:
    """Share [start, end] between pages by text length; each boundary moves to the nearest pause in the
    voice within ``snap`` s (each pause used once, boundaries kept in order)."""
    total, t, cuts = sum(sizes), start, []
    for s in sizes[:-1]:
        t += (end - start) * s / total
        near = [p for p in pauses if abs(p - t) <= snap and (not cuts or p > cuts[-1]) and start < p < end]
        cuts.append(min(near, key=lambda p: abs(p - t)) if near else t)
    edges = [start] + cuts + [end]
    return [(round(edges[i], 2), round(edges[i + 1], 2)) for i in range(len(sizes))]


def resolve(doc: dict, archive) -> tuple[dict, list[str]]:
    """Resolved cues per vision/clip: ``{"disc", "visions": [{"id", "clips": [{"slot", "cues": [...]}]}]}``."""
    r = Resolver(archive)
    font = Font(find_font(archive))
    problems, out = [], {"disc": doc["disc"], "visions": []}
    for v in doc["visions"]:
        clips = []
        for c in v["clips"]:
            if "drop" in c:
                continue
            where = f"vision {v['id']} clip {c['slot']}"
            if "ref" in c:
                ref = c["ref"]
                if ref["disc"] != doc["disc"]:
                    problems.append(f"{where}: ref into disc {ref['disc']}")
                    continue
                try:
                    text = r.text(ref)
                except (KeyError, IndexError, ValueError) as e:
                    problems.append(f"{where}: unresolved ref {ref} ({e})")
                    continue
            else:
                text = c["text"]
            text, bad = to_font(text, font)
            if bad:
                problems.append(f"{where}: no glyph for {''.join(sorted(bad))!r} in {text!r}")
                continue
            if not text:
                problems.append(f"{where}: empty")
                continue
            ps = pages(text, font)
            bounds = split_times(c["start"], c["end"], [len(" ".join(p)) for p in ps], c.get("pauses", []))
            cues = [{"start": a, "end": b, "lines": p, "width": max(font.width(s) for s in p)}
                    for (a, b), p in zip(bounds, ps)]
            clips.append({"slot": c["slot"], "cues": cues})
        out["visions"].append({"id": v["id"], "clips": clips})
    return out, problems
