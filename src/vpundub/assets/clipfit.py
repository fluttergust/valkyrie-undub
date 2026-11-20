"""Fit a JP voice clip under the game's whole-upload size limit.

The battle engine (slot 1490) plays a standalone voice clip as a whole SPU
upload when its archive entry is at most 0x7fff bytes, measured as the TOC
sector count * 2048 (so at most 15 sectors); bigger clips are streamed from the
CD through a small ring buffer. While a stream plays the driver refuses every
other upload, so a voice line that the US game loads during the clip waits
until the stream ends (or, without the slot-1490 retry patch, is dropped). A JP
clip can cross the limit where the shorter US clip did not (disc 2 slot 1630,
Brahms's opening line in the Hrist battle scene: JP 3.1 s, 20 sectors; US 12 sectors).

``fit_clip`` keeps such clips whole: the ADPCM is decoded, resampled down by
whole semitones and re-encoded, and the region's pitch byte is lowered by the
same number of semitones, so the line plays at the same speed and length.

Layout: the ADPCM starts at ``H + 4 + table_size - 4`` from the object start;
its first 4 bytes double as the zero half of the attribute table terminator, so
the re-encoded data starts with one silent block (28 samples, ~1.7 ms).
"""
from __future__ import annotations

import math
import struct

from . import adpcm
from .voice import find_sounds

WHOLE_LIMIT = 15 * 2048      # entry bytes; 15 sectors whole, 16 sectors streamed (measured)
MAX_SEMITONES = 15           # 22050 Hz -> 9269 Hz at most; beyond that the clip is left as is


def fit_clip(slot: int, entry: bytes, limit: int = WHOLE_LIMIT,
             max_semitones: int = MAX_SEMITONES) -> tuple[bytes, int]:
    """Return (new entry, semitones lowered). Entry is unchanged if it already fits.

    Raises ``ValueError`` when the clip is not a single-region standalone clip
    or would need more than ``max_semitones``.
    """
    refs = find_sounds(slot, entry)
    if len(refs) != 1 or refs[0].item is not None or refs[0].obj.script_kind != "clip":
        raise ValueError(f"slot {slot}: not a standalone clip")
    o = refs[0].obj
    if o.length <= limit:
        return entry, 0
    if len(o.samples) != 1 or len(o.samples[0].regions) != 1:
        raise ValueError(f"slot {slot}: clip with several regions")
    t = o.offset + o.script_end + 4           # attribute table
    data = t - 4 + o.table_size               # ADPCM base (table_size counts its own size word)
    reg = o.samples[0].regions[0]
    rpos = t + 16                             # first region entry
    _a, start, end, pitch = struct.unpack_from("<IIIH", entry, rpos)
    body = entry[data + start:data + end]
    tail = entry[data + end:o.offset + o.length]
    head = entry[o.offset:data + start]
    first_flags, last_flags = body[1], body[-15]
    pcm = adpcm.decode(body)
    src = adpcm.rate_of(pitch)
    budget = limit - len(head) - len(tail)
    k = max(1, math.ceil(12 * math.log2(len(body) / budget)))
    if k > max_semitones:
        raise ValueError(f"slot {slot}: needs {k} semitones (max {max_semitones})")
    while True:
        # A silent first block: the first 4 ADPCM bytes are also the zero half
        # of the attribute table's terminator, so they must stay zero.
        new = bytearray(bytes([0, first_flags]) + bytes(14))
        new += adpcm.encode(adpcm.resample(pcm, src, src * 2 ** (-k / 12)), last_flags)
        if len(new) <= budget:
            break
        k += 1
        if k > max_semitones:
            raise ValueError(f"slot {slot}: needs more than {max_semitones} semitones")
    hi = (pitch >> 8) - k
    if hi < 0x80:                              # stay a negative semitone offset
        raise ValueError(f"slot {slot}: needs {k} semitones, below 1.4 kHz")
    out = bytearray(head + bytes(new) + tail)
    base = o.offset
    struct.pack_into("<I", out, rpos - base + 8, start + len(new))
    struct.pack_into("<H", out, rpos - base + 12, (hi << 8) | (pitch & 0xFF))
    struct.pack_into("<I", out, o.script_end, len(out) - o.script_end - 4)  # object size after the size word
    _ = reg
    # drop the old sector padding after the object; the archive pads again
    return bytes(entry[:base]) + bytes(out), k
