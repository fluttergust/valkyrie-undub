"""tri-Ace SLZ compression (as used in Valkyrie Profile PSX).

Reverse-engineered from SLUS_011.56: dispatcher @ 0x800122c0, type 1 @ 0x800123cc,
type 2 @ 0x80012874.

Block header (16 bytes, little endian)::

    0x0  "SLZ"
    0x3  u8   type   (0 = stored, 1 = LZSS, 2 = LZSS + RLE)
    0x4  u32  compressed size (payload bytes after the header)
    0x8  u32  uncompressed size
    0xC  u32  offset to the next block (relative to this block; 0 = last)

The game's loader can select block *n* of a chain by following the ``next`` offsets.

Payload: flag bytes, consumed LSB first. A 1 bit means "copy one literal byte".
A 0 bit means a 2-byte token ``b0 b1``::

    dist = b0 | (b1 & 0x0F) << 8      # distance back from the output cursor
    n    = b1 >> 4

* Type 1: copy ``n + 3`` bytes from ``out[-dist]`` (overlap allowed). ``dist == 0``
  terminates the stream.
* Type 2: if ``n < 15``, copy as in type 1. If ``n == 15`` it is a run (RLE):
    - ``dist < 0x100``: an extra byte ``v`` follows; write ``v`` ``dist + 19`` times.
    - else: write ``b0`` ``(b1 & 0x0F) + 3`` times.
  The stream ends when ``uncompressed size`` bytes have been produced.
"""
from __future__ import annotations

import struct

MAGIC = b"SLZ"
HEADER = 16


class SlzError(ValueError):
    pass


def is_slz(data: bytes) -> bool:
    return len(data) >= HEADER and data[:3] == MAGIC and data[3] in (0, 1, 2)


def header(data: bytes, off: int = 0) -> tuple[int, int, int, int]:
    """Return (type, csize, usize, next)."""
    if data[off:off + 3] != MAGIC:
        raise SlzError(f"no SLZ magic at {off:#x}")
    return (data[off + 3],) + struct.unpack_from("<III", data, off + 4)


def _lz(src: bytes, pos: int, usize: int, rle: bool) -> tuple[bytes, int]:
    out = bytearray()
    flags = 0
    nbits = 0
    n_src = len(src)
    while True:
        if rle and len(out) >= usize:
            break
        if nbits == 0:
            if pos >= n_src:
                raise SlzError("truncated SLZ stream (flags)")
            flags = src[pos]
            pos += 1
            nbits = 8
        bit = flags & 1
        flags >>= 1
        nbits -= 1
        if bit:
            out.append(src[pos])
            pos += 1
            continue
        if pos + 1 >= n_src:
            raise SlzError("truncated SLZ stream (token)")
        b0, b1 = src[pos], src[pos + 1]
        pos += 2
        dist = b0 | (b1 & 0x0F) << 8
        n = b1 >> 4
        if not rle and dist == 0:
            break
        if rle and n == 15:
            if dist < 0x100:
                v = src[pos]
                pos += 1
                out += bytes([v]) * (dist + 19)
            else:
                out += bytes([b0]) * ((b1 & 0x0F) + 3)
            continue
        length = n + 3
        start = len(out) - dist
        if start < 0 or dist == 0:
            raise SlzError(f"bad back-reference dist={dist} at out={len(out)}")
        if dist >= length:
            out += out[start:start + length]
        else:
            for i in range(length):
                out.append(out[start + i])
    return bytes(out), pos


def decompress_block(data: bytes, off: int = 0) -> tuple[bytes, int]:
    """Decompress one block at ``off``. Returns (data, payload bytes consumed)."""
    typ, csize, usize, _nxt = header(data, off)
    p = off + HEADER
    if typ == 0:
        out = bytes(data[p:p + usize])
        used = usize
    elif typ in (1, 2):
        out, end = _lz(data, p, usize, rle=(typ == 2))
        used = end - p
    else:
        raise SlzError(f"unknown SLZ type {typ}")
    if len(out) != usize:
        raise SlzError(f"size mismatch: got {len(out)}, header says {usize}")
    return out, used


def blocks(data: bytes) -> list[int]:
    """Offsets of all blocks in a chain."""
    offs = [0]
    while True:
        _t, _c, _u, nxt = header(data, offs[-1])
        if not nxt:
            return offs
        offs.append(offs[-1] + nxt)


def decompress(data: bytes, block: int | None = None) -> bytes:
    """Decompress block ``block`` (or the concatenation of all blocks)."""
    offs = blocks(data)
    if block is not None:
        return decompress_block(data, offs[block])[0]
    return b"".join(decompress_block(data, o)[0] for o in offs)


# --- Compression -------------------------------------------------------------

def _find(data: bytes, i: int, max_len: int, window: int, table: dict) -> tuple[int, int]:
    """Greedy longest match via a 3-byte hash chain (most recent candidates first)."""
    best_len, best_dist = 0, 0
    if i + 3 > len(data):
        return 0, 0
    key = data[i:i + 3]
    cands = table.get(key)
    if not cands:
        return 0, 0
    lim = min(max_len, len(data) - i)
    checked = 0
    for j in reversed(cands):
        d = i - j
        if d > window:
            break
        l = 3
        while l < lim and data[j + l] == data[i + l]:
            l += 1
        if l > best_len:
            best_len, best_dist = l, d
            if l == lim:
                break
        checked += 1
        if checked >= 64:
            break
    return best_len, best_dist


def compress(data: bytes, typ: int = 2) -> bytes:
    """Compress ``data`` into a single SLZ block of type 0, 1 or 2.

    The output is valid for the game's decoder but is not guaranteed to be
    byte-identical to tri-Ace's original compressor.
    """
    if typ == 0:
        return MAGIC + bytes([0]) + struct.pack("<III", len(data), len(data), 0) + data
    if typ not in (1, 2):
        raise ValueError("type must be 0, 1 or 2")
    rle = typ == 2
    out = bytearray()
    group: list[bytes] = []
    flags = 0
    nflag = 0

    def emit(tok: bytes, literal: bool):
        nonlocal flags, nflag
        if literal:
            flags |= 1 << nflag
        group.append(tok)
        nflag += 1
        if nflag == 8:
            flush()

    def flush():
        nonlocal flags, nflag
        if nflag:
            out.append(flags)
            for t in group:
                out.extend(t)
            group.clear()
            flags = 0
            nflag = 0

    table: dict[bytes, list[int]] = {}

    def index(pos: int):
        if pos + 3 <= len(data):
            table.setdefault(data[pos:pos + 3], []).append(pos)

    i = 0
    n = len(data)
    while i < n:
        if rle:
            # run length at i
            r = 1
            while i + r < n and r < 274 and data[i + r] == data[i]:
                r += 1
            if r >= 19:
                emit(bytes([(r - 19) & 0xFF, 0xF0 | ((r - 19) >> 8)]) + bytes([data[i]]), False)
                for k in range(r):
                    index(i + k)
                i += r
                continue
        max_len = 17 if rle else 18
        length, dist = _find(data, i, max_len, 0xFFF, table)
        if rle and length < 4:
            r = 1
            while i + r < n and r < 18 and data[i + r] == data[i]:
                r += 1
            if r >= 4:
                # short run needs dist >= 0x100: encoded as b0=value, b1=0xF0|(r-3)
                emit(bytes([data[i], 0xF0 | (r - 3)]), False)
                for k in range(r):
                    index(i + k)
                i += r
                continue
        if length >= 3:
            emit(bytes([dist & 0xFF, ((length - 3) << 4) | (dist >> 8)]), False)
            for k in range(length):
                index(i + k)
            i += length
        else:
            emit(bytes([data[i]]), True)
            index(i)
            i += 1
    if not rle:
        emit(b"\x00\x00", False)  # terminator: dist == 0
    flush()
    return MAGIC + bytes([typ]) + struct.pack("<III", len(out), n, 0) + bytes(out)


# --- Patching -----------------------------------------------------------------

def _tokens(data: bytes) -> list[tuple[bool, bytes, int, int]]:
    """Token list of a single type-1 or type-2 block.

    Each token is ``(literal, raw, n, dist)``: its flag bit, its encoded bytes, the
    number of output bytes and the back-reference distance (0 for literals and
    type-2 runs, which read no earlier output)."""
    typ, _c, usize, nxt = header(data)
    if typ not in (1, 2) or nxt:
        raise SlzError("patch_block supports single type-1/type-2 blocks only")
    rle = typ == 2
    toks: list[tuple[bool, bytes, int, int]] = []
    pos, nbits, flags, produced = HEADER, 0, 0, 0
    while not (rle and produced >= usize):
        if nbits == 0:
            flags, pos, nbits = data[pos], pos + 1, 8
        bit, flags, nbits = flags & 1, flags >> 1, nbits - 1
        if bit:
            toks.append((True, data[pos:pos + 1], 1, 0))
            pos += 1
            produced += 1
            continue
        b0, b1 = data[pos], data[pos + 1]
        dist = b0 | (b1 & 0x0F) << 8
        if not rle and dist == 0:
            break
        if rle and b1 >> 4 == 15:
            if dist < 0x100:
                toks.append((False, data[pos:pos + 3], dist + 19, 0))
                pos += 3
            else:
                toks.append((False, data[pos:pos + 2], (b1 & 0x0F) + 3, 0))
                pos += 2
        else:
            toks.append((False, data[pos:pos + 2], (b1 >> 4) + 3, dist))
            pos += 2
        produced += toks[-1][2]
    if produced != usize:
        raise SlzError("token stream size mismatch")
    return toks


def patch_block(block: bytes, new: bytes) -> bytes:
    """Re-encode a type-1 or type-2 SLZ block so it decompresses to ``new`` (same
    length as the original data), keeping every original token whose output and
    source bytes are unchanged; only tokens that touch changed bytes become literals.

    This keeps the block (nearly) as small as tri-Ace's original, unlike a full
    re-compression with ``compress``.
    """
    typ = header(block)[0]
    old = decompress(block)
    if len(old) != len(new):
        raise SlzError("patch_block needs data of the same length")
    changed = bytearray(len(old))
    for i, (a, b) in enumerate(zip(old, new)):
        if a != b:
            changed[i] = 1
    out_toks: list[tuple[bool, bytes]] = []
    o = 0
    for lit, raw, n, dist in _tokens(block):
        src_dirty = dist and any(changed[o - dist:o - dist + n])
        if any(changed[o:o + n]) or src_dirty:
            out_toks += [(True, new[o + k:o + k + 1]) for k in range(n)]
        else:
            out_toks.append((lit, raw))
        o += n
    body = bytearray()
    for g in range(0, len(out_toks), 8):
        grp = out_toks[g:g + 8]
        body.append(sum(1 << k for k, (lit, _r) in enumerate(grp) if lit))
        for _lit, raw in grp:
            body += raw
    if typ == 1:
        # terminator token (flag bit 0, dist 0) in a group of its own
        body += b"\x00\x00\x00"
    res = MAGIC + bytes([typ]) + struct.pack("<III", len(body), len(new), 0) + bytes(body)
    if decompress(res) != new:
        raise SlzError("patch_block round trip failed")
    return res
