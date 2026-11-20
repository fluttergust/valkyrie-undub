"""The Spiritual Concentration vision tables of the world-map module (ported from vpsub ``tools/scvisions.py``).

The world-map module (archive slot 4796, SLZ, identical on both discs) holds

* an index of 26 records ``u32 count, u32 ptr`` (vision id = index + 1; ``count == 0`` means unused),
  preceded in memory by
* clip lists, ``count`` x ``u16 slot, u8 delay, u8 pan``.

The player waits ``delay`` ticks, starts the clip, waits until it has stopped, next clip. The clip lengths
come from the clip objects on the disc, so the undub's (JP) lengths time the subtitles.
"""
from __future__ import annotations

import struct

from ..archive import slz
from ..assets import adpcm, voice

N_IDS = 26
RAM_LO, RAM_HI = 0x80010000, 0x80200000


def find_index(d: bytes) -> tuple[int, int] | None:
    """Find the vision index in a decompressed module.

    Returns (file offset of the index, load base) or None. Signature: 26
    records ``(count, ptr)`` where non-empty records point at consecutive
    clip lists (``ptr[k+1] == ptr[k] + 4 * count[k]``) that end right where
    the index begins.
    """
    for o in range(0, len(d) - 8 * N_IDS, 4):
        n0, p0 = struct.unpack_from("<II", d, o)
        if not (0 < n0 < 64 and RAM_LO <= p0 < RAM_HI):
            continue
        recs = [struct.unpack_from("<II", d, o + 8 * i) for i in range(N_IDS)]
        live = [(n, p) for n, p in recs if n]
        if len(live) < 4 or any(not (0 < n < 64 and RAM_LO <= p < RAM_HI) for n, p in live):
            continue
        if any(b[1] != a[1] + 4 * a[0] for a, b in zip(live, live[1:])):
            continue
        n_last, p_last = live[-1]
        # the clip lists sit directly before the index
        base = p0 - (o - 4 * sum(n for n, _ in live))
        if p_last + 4 * n_last - base != o:
            continue
        return o, base
    return None


def clip_frames(buf: bytes) -> int | None:
    """Duration in sound-driver ticks from a single clip's script (sum of note lengths)."""
    if len(buf) < 0x14 or struct.unpack_from("<I", buf, 4)[0] != voice.CLIP_MAGIC1:
        return None
    H = struct.unpack_from("<I", buf, 0)[0]
    p, end = 0x10, H
    total, rep_start, rep_sum = 0, None, 0
    while p < end:
        op = buf[p]
        if op == 0x80:
            break
        if op == 0x83:
            p += 4
        elif op == 0x90:
            p += 3
        elif op == 0x8D:
            rep_start, rep_sum = p, 0
            p += 1
        elif op == 0x8E:
            n = buf[p + 1]
            total += rep_sum * (n - 1)
            rep_start = None
            p += 2
        elif op == 0x3C:
            ln = buf[p + 1]
            total += ln
            if rep_start is not None:
                rep_sum += ln
            p += 4
        else:
            return None
    return total


def clip_seconds(buf: bytes) -> float | None:
    so = voice.parse_sound(buf)
    if so is None or not so.samples:
        return None
    best = 0.0
    for s in so.samples:
        for r in s.regions:
            best = max(best, r.adpcm_length / 16 * 28 / adpcm.rate_of(r.pitch))
    return round(best, 3)


def scan(archive, slot: int) -> dict:
    """``{"module_slot", "index_offset", "load_base", "visions": [{"id", "clips": [...], "total", "on_disc"}]}``
    from the module in ``slot`` of ``archive`` (clip lengths from the same archive)."""
    d = slz.decompress(archive[slot])
    hit = find_index(d)
    if not hit:
        raise ValueError(f"slot {slot}: vision index not found")
    idx, base = hit
    visions = []
    for i in range(N_IDS):
        n, p = struct.unpack_from("<II", d, idx + 8 * i)
        if not n:
            continue
        clips, t = [], 0
        for k in range(n):
            cs, delay, pan = struct.unpack_from("<HBB", d, p - base + 4 * k)
            buf = archive[cs] if cs in archive else b""
            fr = clip_frames(buf)
            t += delay
            clips.append({"slot": cs, "delay": delay, "pan": pan, "start": t,
                          "frames": fr, "seconds": clip_seconds(buf) if buf else None,
                          "present": cs in archive})
            t += fr or 0
        visions.append({"id": i + 1, "clips": clips, "total": t,
                        "on_disc": all(c["present"] for c in clips)})
    return {"module_slot": slot, "index_offset": idx, "load_base": base, "visions": visions}
