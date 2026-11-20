"""tri-Ace SLZ streams (Valkyrie Profile archive entries): decode to tokens, and patch in place.

Format (as documented by the user's ../vptrans/src/vpundub/archive/slz.py, which reverse-engineered the decoder
in SLUS_011.56; this is an independent implementation of the same format):

    16-byte header: "SLZ", u8 type (0 stored, 1 LZSS, 2 LZSS+RLE), u32 csize, u32 usize, u32 next (0 = last)
    payload: flag bytes read LSB first, 1 = literal byte, 0 = 2-byte token b0 b1,
             dist = b0 | (b1 & 0xF) << 8, n = b1 >> 4
    type 1: copy n + 3 bytes from dist back; dist 0 ends the stream
    type 2: n < 15 copies as type 1; n == 15 is a run: dist < 0x100 -> next byte v repeated dist + 19 times,
            else b0 repeated (b1 & 0xF) + 3 times; the stream ends after usize bytes

`patch()` changes bytes of the decompressed data without recompressing: every token that writes a changed byte,
or copies from one, becomes literals, and the rest of the stream is kept token for token. The result grows by
a few bytes and decodes to exactly the patched data (checked).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"SLZ"
HDR = 16


class SlzError(ValueError):
    pass


@dataclass(frozen=True)
class Tok:
    kind: str       # "lit", "copy", "run" or "end" (type 1 terminator)
    raw: bytes      # encoded bytes (without the flag bit)
    out: int        # output offset of the first byte produced
    n: int          # bytes produced
    src: int = -1   # copy: output offset copied from


def header(data: bytes) -> tuple[int, int, int, int]:
    if data[:3] != MAGIC or data[3] not in (0, 1, 2):
        raise SlzError("not an SLZ block")
    return (data[3],) + struct.unpack_from("<III", data, 4)


def tokens(data: bytes) -> tuple[int, list[Tok]]:
    """Parse a single-block LZ stream (type 1 or 2) into tokens. Returns (type, tokens)."""
    typ, csize, usize, nxt = header(data)
    if typ == 0 or nxt:
        raise SlzError(f"only single-block LZ streams are supported (type {typ}, next {nxt})")
    p, end, o = HDR, HDR + csize, 0
    toks: list[Tok] = []
    flags = nbits = 0
    while not (typ == 2 and o >= usize):
        if nbits == 0:
            if p >= end:
                raise SlzError("truncated (flags)")
            flags, nbits, p = data[p], 8, p + 1
        bit, flags, nbits = flags & 1, flags >> 1, nbits - 1
        if bit:
            toks.append(Tok("lit", data[p:p + 1], o, 1))
            p, o = p + 1, o + 1
            continue
        if p + 2 > end:
            raise SlzError("truncated (token)")
        b0, b1 = data[p], data[p + 1]
        dist, n = b0 | (b1 & 0x0F) << 8, b1 >> 4
        if typ == 1 and dist == 0:
            toks.append(Tok("end", data[p:p + 2], o, 0))
            p += 2
            break
        if typ == 2 and n == 15:
            if dist < 0x100:
                toks.append(Tok("run", data[p:p + 3], o, dist + 19))
                p, o = p + 3, o + dist + 19
            else:
                toks.append(Tok("run", data[p:p + 2], o, (b1 & 0x0F) + 3))
                p, o = p + 2, o + (b1 & 0x0F) + 3
            continue
        if dist == 0 or dist > o:
            raise SlzError(f"bad back-reference dist={dist} at output {o:#x}")
        toks.append(Tok("copy", data[p:p + 2], o, n + 3, o - dist))
        p, o = p + 2, o + n + 3
    if o != usize or p > end:
        raise SlzError(f"size mismatch: produced {o:#x}, header {usize:#x}")
    return typ, toks


def decompress(data: bytes) -> bytes:
    typ, csize, usize, _ = header(data)
    if typ == 0:
        return bytes(data[HDR:HDR + usize])
    out = bytearray()
    for t in tokens(data)[1]:
        if t.kind == "lit":
            out += t.raw
        elif t.kind == "run":
            out += (t.raw[2:3] if len(t.raw) == 3 else t.raw[:1]) * t.n
        elif t.kind == "copy":
            for i in range(t.n):
                out.append(out[t.src + i])
    return bytes(out)


def encode(typ: int, usize: int, toks: list[Tok]) -> bytes:
    body = bytearray()
    for g in range(0, len(toks), 8):
        group = toks[g:g + 8]
        body.append(sum(1 << i for i, t in enumerate(group) if t.kind == "lit"))
        for t in group:
            body += t.raw
    return MAGIC + bytes([typ]) + struct.pack("<III", len(body), usize, 0) + bytes(body)


def patch(data: bytes, changes: dict[int, bytes]) -> bytes:
    """Return the SLZ block `data` with decompressed bytes replaced ({offset: new bytes}).

    The returned block is padded with zeros to the length of `data` when it fits (whole sectors stay whole)."""
    typ, _, usize, _ = header(data)
    plain = bytearray(decompress(data))
    for off, new in changes.items():
        if off < 0 or off + len(new) > usize:
            raise SlzError(f"patch at {off:#x} outside the data")
        plain[off:off + len(new)] = new
    if typ == 0:
        out = data[:HDR] + bytes(plain)
    else:
        hot = [(off, off + len(new)) for off, new in changes.items()]

        def touches(a: int, b: int) -> bool:
            return any(a < e and s < b for s, e in hot)
        toks: list[Tok] = []
        for t in tokens(data)[1]:
            if t.n and (touches(t.out, t.out + t.n) or (t.kind == "copy" and touches(t.src, t.src + t.n))):
                toks += [Tok("lit", bytes([plain[t.out + i]]), t.out + i, 1) for i in range(t.n)]
            else:
                toks.append(t)
        out = encode(typ, usize, toks)
    if decompress(out) != plain:
        raise SlzError("patched stream does not decode to the patched data")
    if len(out) <= len(data):
        out += bytes(len(data) - len(out))
    return out
