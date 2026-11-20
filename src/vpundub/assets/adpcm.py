"""PS1 SPU-ADPCM decode / encode and resampling.

A block is 16 bytes: header (shift | filter << 4), flags, then 14 bytes holding
28 signed 4-bit samples (low nibble first). Decoding::

    s = (nibble << 12) >> shift
    s += (s1 * K0[f] + s2 * K1[f] + 32) >> 6

The encoder is a deterministic brute-force search per block over the 5 filters
and the two best shifts, simulating the decoder exactly (so its output decodes
bit-exactly to what the SPU plays).
"""
from __future__ import annotations

import numpy as np

K0 = (0, 60, 115, 98, 122)
K1 = (0, 0, -52, -55, -60)
FLAG_END = 0x01
FLAG_REPEAT = 0x02


def decode(data: bytes) -> np.ndarray:
    out = np.empty(len(data) // 16 * 28, dtype=np.int16)
    s1 = s2 = 0
    n = 0
    for p in range(0, len(data) - 15, 16):
        shift = data[p] & 0x0F
        f = min(data[p] >> 4, 4)
        k0, k1 = K0[f], K1[f]
        for b in data[p + 2:p + 16]:
            for nib in (b & 0x0F, b >> 4):
                v = (nib - 16 if nib >= 8 else nib) << 12 >> shift
                v += (s1 * k0 + s2 * k1 + 32) >> 6
                v = -32768 if v < -32768 else 32767 if v > 32767 else v
                out[n] = v
                n += 1
                s2, s1 = s1, v
    return out[:n]


def _try(block, s1, s2, f, shift):
    k0, k1 = K0[f], K1[f]
    nibs = []
    err = 0
    for x in block:
        pred = (s1 * k0 + s2 * k1 + 32) >> 6
        r = x - pred
        q = (r * (1 << shift) + (1 << 11)) >> 12 if shift < 12 else r
        q = -8 if q < -8 else 7 if q > 7 else q
        v = (q << 12 >> shift) + pred
        v = -32768 if v < -32768 else 32767 if v > 32767 else v
        err += (x - v) * (x - v)
        nibs.append(q & 0x0F)
        s2, s1 = s1, v
    return err, nibs, s1, s2


def encode(pcm: np.ndarray, flags_last: int = FLAG_END) -> bytes:
    """Encode int16 PCM (padded to 28-sample blocks) to SPU-ADPCM.

    The first block gets no flags; the last block gets ``flags_last``.
    """
    x = [int(v) for v in pcm]
    x += [0] * (-len(x) % 28)
    out = bytearray()
    s1 = s2 = 0
    for b in range(0, len(x), 28):
        block = x[b:b + 28]
        best = None
        for f in range(5):
            # residual range under this filter (using ideal history) → shift
            k0, k1 = K0[f], K1[f]
            p1, p2, peak = s1, s2, 0
            for v in block:
                r = v - ((p1 * k0 + p2 * k1 + 32) >> 6)
                peak = max(peak, abs(r))
                p2, p1 = p1, v
            sh = 12
            while sh > 0 and (7 << 12 >> sh) < peak:
                sh -= 1
            for shift in {sh, min(12, sh + 1), max(0, sh - 1)}:
                cand = _try(block, s1, s2, f, shift)
                if best is None or cand[0] < best[0][0]:
                    best = (cand, f, shift)
        (err, nibs, s1, s2), f, shift = best
        out.append(shift | f << 4)
        out.append(flags_last if b + 28 >= len(x) else 0)
        out += bytes(nibs[i] | nibs[i + 1] << 4 for i in range(0, 28, 2))
    return bytes(out)


def resample(pcm: np.ndarray, src_rate: float, dst_rate: float, taps: int = 32) -> np.ndarray:
    """Windowed-sinc resampling (deterministic, numpy only)."""
    x = pcm.astype(np.float64)
    n_out = int(len(x) * dst_rate / src_rate)
    ratio = dst_rate / src_rate
    cutoff = min(1.0, ratio) * 0.95
    t = np.arange(n_out) / ratio
    i0 = np.floor(t).astype(np.int64)
    out = np.zeros(n_out)
    for k in range(-taps + 1, taps + 1):
        idx = i0 + k
        d = t - idx
        w = cutoff * np.sinc(cutoff * d) * (0.5 + 0.5 * np.cos(np.pi * d / taps))
        valid = (idx >= 0) & (idx < len(x))
        out[valid] += x[idx[valid]] * w[valid]
    return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def rate_of(pitch: int) -> float:
    """Playback rate of a region pitch field (high byte = semitones from 44100 Hz)."""
    semis = (pitch >> 8) - 256 if pitch >> 8 >= 128 else pitch >> 8
    return 44100 * 2 ** (semis / 12)
