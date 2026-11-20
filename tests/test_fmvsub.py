"""Ending-FMV subtitles (src/vpundub/subtitles/fmvsub.py, ported from vpsub tests/test_fmvsub.py).

    PYTHONPATH=src python3 -m unittest tests.test_fmvsub -v

Synthetic fonts need no disc; the disc tests skip without the US ROMs, the reassembly test without the
LLVM toolchain.
"""
from __future__ import annotations

import random
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_disc import need  # noqa: E402
from vpundub.subtitles import fmvsub  # noqa: E402
from vpundub.subtitles.font import Font, find_font  # noqa: E402

TOOLS = all(shutil.which(t) for t in ("clang", "ld.lld", "llvm-objcopy", "llvm-nm"))


def inked(n=101, seed=1):
    """Synthetic font whose glyphs have random ink inside their width (like the disc font)."""
    rnd = random.Random(seed)
    widths = bytes(3 + i % 6 for i in range(n))
    glyphs = b""
    for i in range(n):
        mask = (0xFFFF << (16 - widths[i])) & 0xFFFF
        glyphs += struct.pack("<12H", *[rnd.getrandbits(16) & mask & rnd.choice((0, 0xFFFF)) for _ in range(12)])
    return Font(struct.pack("<II", n, 12) + widths + glyphs)


def _us(disc):
    from vpundub.build import DEFAULT_CUE, _archive, _open
    return _archive(_open(ROOT / DEFAULT_CUE[("us", disc)]))[0]


class TestData(unittest.TestCase):
    def test_map_matches_raster(self):
        font = inked()
        wt, cols, bits, pages = fmvsub.build(font)
        want = {2: fmvsub.INK, 1: fmvsub.DARK, 0: 0}
        for start, end, text in fmvsub.CUES:
            q = fmvsub.page_at(pages, start)
            self.assertIsNotNone(q)
            self.assertEqual(fmvsub.page_at(pages, end - 1), q)
            m = fmvsub.colour_map(wt, cols, bits, pages, q)
            ras, x = font.raster(text), pages[q + 5]
            self.assertEqual(x + fmvsub.COLX, (fmvsub.SCREEN_W - font.width(text)) // 2)   # centred
            for j in range(fmvsub.ROWS + 2):
                for i in range(fmvsub.MW):
                    r = i - x + 1
                    v = want[ras[j][r]] if 0 <= r < len(ras[j]) else 0
                    self.assertEqual(m[j][i], v, (text, j, i))

    def test_no_page_between_cues(self):
        pages = fmvsub.build(inked())[3]
        shown = {f for f in range(600) if fmvsub.page_at(pages, f) is not None}
        self.assertEqual(shown, {f for s, e, _ in fmvsub.CUES for f in range(s, e)})

    def test_bad_cues_rejected(self):
        with self.assertRaises(fmvsub.FmvError):
            fmvsub.build(inked(), [(10, 5, "ab")])

    def test_wrong_pack_rejected(self):
        with self.assertRaises(fmvsub.FmvError):
            fmvsub.patch_slot(bytes(fmvsub.SLOT_LEN), b"")


class TestDisc(unittest.TestCase):
    @need("us1")
    @need("us2")
    def test_slot_patch_and_verify(self):
        """Both discs: committed code + disc-font data at the assembled addresses, fits the slot slack,
        only the hook pair and the appended routine differ, and verify_slot rejects other changes."""
        from vpundub.assets.voice import parse_pack
        for disc in (1, 2):
            us = _us(disc)
            new, info = fmvsub.fmv_slot(us)
            self.assertEqual(len(new), fmvsub.SLOT_LEN)
            self.assertEqual((info["code_bytes"], info["data_bytes"], info["slot_bytes_left"]), (512, 404, 16))
            fmvsub.verify_slot(us[fmvsub.SLOT], new)
            it = parse_pack(new)[fmvsub.ITEM]
            self.assertEqual(struct.unpack_from("<2I", new, it.offset + fmvsub.SITE),
                             (fmvsub.jal(fmvsub.HOOK), fmvsub.MOVE_A0_S0))
            bad = bytearray(new)
            bad[parse_pack(new)[0].offset] ^= 1
            with self.assertRaises(fmvsub.FmvError):
                fmvsub.verify_slot(us[fmvsub.SLOT], bytes(bad))
            with self.assertRaises(fmvsub.FmvError):
                fmvsub.verify_slot(us[fmvsub.SLOT], us[fmvsub.SLOT])           # not patched
            with self.assertRaises(fmvsub.FmvError):
                fmvsub.patch_slot(new, b"")                                     # already patched

    @need("us1")
    @unittest.skipUnless(TOOLS, "LLVM toolchain missing")
    def test_fmvsub_bin_current(self):
        """fmvsub.bin + LAYOUT are what fmvsub.S assembles to with the disc font (tools/subtitle_overlay.py)."""
        font = Font(find_font(_us(1)))
        with tempfile.TemporaryDirectory() as t:
            img, syms = fmvsub.compile(Path(t), font)
        self.assertEqual({k: syms[k] for k in fmvsub.LAYOUT}, fmvsub.LAYOUT)
        self.assertEqual(img, fmvsub.image(font), "fmvsub.bin is stale: run tools/subtitle_overlay.py")


if __name__ == "__main__":
    unittest.main()
