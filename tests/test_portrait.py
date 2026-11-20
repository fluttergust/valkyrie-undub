"""Tests for the mirrored-portrait fix (field engine slot 2292, src/vpundub/codepatch.py,
src/vpundub/asm/portrait_tile.s; TECHNICAL.md "Portrait seam"). Kept apart from the
undub (voice) tests.

    PYTHONPATH=src python3 -m unittest tests.test_portrait -v

The in-game check (packets and screenshots) is tools/portrait_compare.py on two
prologue runs; it needs the emulator, so it is not a unit test.
"""
from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_disc import need  # noqa: E402
from vpundub.archive import slz  # noqa: E402
from vpundub.build import DEFAULT_CUE, _archive, _open  # noqa: E402
from vpundub.codepatch import (FIELD_SLOT, SLOT_PATCHES, _PORTRAIT_TILE_CODE,  # noqa: E402
                               apply_slot_patches, patched_slot_data, slot_patches)


class TestPortraitPatch(unittest.TestCase):
    def test_patch_block_type2(self):
        # field engine slot 2292 is type 2 (RLE tokens as well as back-references)
        rnd = random.Random(2)
        data = b"".join(bytes(rnd.getrandbits(3) for _ in range(200)) + bytes(rnd.randrange(4, 300))
                        for _ in range(60))
        blk = slz.compress(data, 2)
        self.assertEqual(slz.header(blk)[0], 2)
        for at in (0, len(data) // 2, len(data) - 8):
            new = bytearray(data)
            new[at:at + 8] = b"PATCHED!"
            out = slz.patch_block(blk, bytes(new))
            self.assertEqual(slz.header(out)[0], 2)
            self.assertEqual(slz.decompress(out), bytes(new))
            self.assertLess(slz.header(out)[1], slz.header(blk)[1] + 64)

    @need("us1")
    @need("us2")
    def test_field_engine_on_us_discs(self):
        self.assertEqual(sorted(slot_patches()), [1490, FIELD_SLOT])
        ps = slot_patches()[FIELD_SLOT]
        for d in (1, 2):
            arc, _ = _archive(_open(DEFAULT_CUE[("us", d)]))
            new = apply_slot_patches(lambda s: arc[s], {}, ps)
            for slot in (FIELD_SLOT,):
                us = arc[slot]
                want = patched_slot_data(us, ps)
                self.assertEqual(slz.decompress(new[slot]), want)
                old = slz.decompress(us)
                diff = [i for i, (a, b) in enumerate(zip(old, want)) if a != b]
                self.assertTrue(diff)
                self.assertTrue(all(any(p.offset <= i < p.offset + len(p.new) for p in ps) for i in diff))
                self.assertLessEqual(len(new[slot]), len(us))   # same sector count as the original

    @unittest.skipIf(shutil.which("llvm-mc") is None or shutil.which("llvm-objcopy") is None,
                     "llvm-mc / llvm-objcopy not installed")
    def test_asm_source_matches(self):
        src = ROOT / "src/vpundub/asm/portrait_tile.s"
        with tempfile.TemporaryDirectory() as d:
            obj, binf = Path(d) / "t.o", Path(d) / "t.bin"
            subprocess.run(["llvm-mc", "-triple=mipsel", "-mcpu=mips1", "-filetype=obj", str(src), "-o", str(obj)],
                           check=True)
            subprocess.run(["llvm-objcopy", "-O", "binary", "-j", ".text", str(obj), str(binf)], check=True)
            self.assertEqual(binf.read_bytes(), _PORTRAIT_TILE_CODE)

    def test_portrait_patch_fits(self):
        p = [p for p in SLOT_PATCHES if p.slot == FIELD_SLOT]
        self.assertEqual(len(p), 1)
        self.assertEqual(len(p[0].old), 0x8004E550 - 0x8004E454)
        self.assertEqual(len(p[0].new), len(p[0].old))
        self.assertEqual(p[0].new[len(_PORTRAIT_TILE_CODE):], bytes(len(p[0].old) - len(_PORTRAIT_TILE_CODE)))


if __name__ == "__main__":
    unittest.main()
