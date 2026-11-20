import struct
import unittest

import numpy as np

from vpundub.assets import adpcm
from vpundub.assets.clipfit import fit_clip
from vpundub.assets.voice import find_sounds, TERMINATOR


def tone(n, rate=22050, hz=440):
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * hz * t) * 12000).astype(np.int16)


def make_clip(adpcm_bytes: bytes, pitch=0xF47F) -> bytes:
    """Standalone clip object laid out like the game's (e.g. disc 2 slot 1630).

    H=28, script, size word, attribute table with one region. The ADPCM starts
    4 bytes before the end of the table: its first block's first 4 bytes are
    the zero half of the terminator. A 4-byte tail follows the ADPCM.
    """
    H = 28
    assert adpcm_bytes[:4] == bytes(4)
    script = struct.pack("<I", 0x80000004) + bytes(H - 8)
    table = struct.pack("<HBBI", 8 + 8 + 0x14 + 8, 0x14, 0, 0)
    table += struct.pack("<HHHBB", 0x2900, 0, 0, 0, 1)
    table += struct.pack("<IIIHHI", 0x7F00007F, 0, len(adpcm_bytes), pitch, 0, 0)
    table += TERMINATOR
    body = table[:-4] + adpcm_bytes + bytes(4)
    return struct.pack("<I", H) + script + struct.pack("<I", len(body)) + body


def clip_adpcm(n_samples: int) -> bytes:
    """Tone ADPCM led by a silent block, as the game's clips are."""
    return bytes(16) + adpcm.encode(tone(n_samples))


class TestAdpcm(unittest.TestCase):
    def test_roundtrip_exact(self):
        enc = adpcm.encode(tone(28 * 50))
        self.assertEqual(adpcm.encode(adpcm.decode(enc)), enc)

    def test_encode_quality(self):
        x = tone(28 * 200)
        d = adpcm.decode(adpcm.encode(x)).astype(float)
        snr = 10 * np.log10((x.astype(float) ** 2).sum() / ((x - d) ** 2).sum())
        self.assertGreater(snr, 25)

    def test_end_flag(self):
        enc = adpcm.encode(tone(100))
        self.assertEqual(len(enc) % 16, 0)
        self.assertEqual(enc[-15], adpcm.FLAG_END)
        self.assertEqual(enc[1], 0)

    def test_rate_of(self):
        self.assertAlmostEqual(adpcm.rate_of(0xF47F), 22050)
        self.assertEqual(round(adpcm.rate_of(0xF07F)), 17501)

    def test_resample_length(self):
        self.assertEqual(len(adpcm.resample(tone(22050), 22050, 11025)), 11025)


class TestClipFit(unittest.TestCase):
    def test_fit_large_clip(self):
        clip = make_clip(clip_adpcm(28 * 2600))                  # ~41.6 KB
        self.assertGreater(len(clip), 30720)
        self.assertIsNotNone(find_sounds(1, clip))
        new, k = fit_clip(1, clip)
        self.assertGreater(k, 0)
        self.assertLessEqual(len(new), 30720)
        o = find_sounds(1, new)[0].obj
        t = o.offset + o.script_end + 4
        self.assertEqual(new[t + o.table_size - 8:t + o.table_size], TERMINATOR)
        self.assertEqual(struct.unpack_from("<I", new, o.script_end)[0], len(new) - o.script_end - 4)
        reg = o.samples[0].regions[0]
        self.assertEqual(reg.pitch >> 8, 0xF4 - k)
        self.assertEqual(reg.pitch & 0xFF, 0x7F)
        # same play time
        old_s = 28 * 2601 / 22050
        new_s = reg.adpcm_length // 16 * 28 / adpcm.rate_of(reg.pitch)
        self.assertAlmostEqual(new_s, old_s, delta=0.01)

    def test_small_clip_untouched(self):
        clip = make_clip(clip_adpcm(28 * 100))
        self.assertEqual(fit_clip(1, clip), (clip, 0))

    def test_fitted_audio_matches(self):
        clip = make_clip(clip_adpcm(28 * 2600))
        new, k = fit_clip(1, clip)
        o = find_sounds(1, new)[0].obj
        base = o.offset + o.script_end + o.table_size
        reg = o.samples[0].regions[0]
        got = adpcm.decode(new[base:base + reg.adpcm_length]).astype(float)[28:]
        src = np.concatenate([np.zeros(28, np.int16), tone(28 * 2600)])  # with the silent lead block
        ref = adpcm.resample(src, 22050, adpcm.rate_of(reg.pitch)).astype(float)
        n = min(len(got), len(ref))
        snr = 10 * np.log10((ref[:n] ** 2).sum() / ((ref[:n] - got[:n]) ** 2).sum())
        self.assertGreater(snr, 20)


if __name__ == "__main__":
    unittest.main()
