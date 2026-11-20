"""Tests for the undub's sound code patches (src/vpundub/codepatch.py): boot executable
sound driver and battle engine slot 1490. The portrait fix is in tests/test_portrait.py.

    PYTHONPATH=src python3 -m unittest tests.test_codepatch -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_disc import need  # noqa: E402
from vpundub.build import DEFAULT_CUE, _open  # noqa: E402
from vpundub.archive import slz  # noqa: E402
from vpundub.build import _archive  # noqa: E402
from vpundub.codepatch import (EXE_NAME, PATCHES, CodePatchError, _beqz, _j,  # noqa: E402
                               apply_slot_patches, inject_records, patch_exe, patched_slot_data,
                               slot_patches)
from vpundub.disc.iso9660 import parse  # noqa: E402


def fake_exe() -> bytearray:
    exe = bytearray(b"PS-X EXE" + bytes(0x20000 - 8))
    for p in PATCHES:
        exe[p.offset:p.offset + len(p.old)] = p.old
    return exe


class TestCodePatch(unittest.TestCase):
    def test_jump_encoding(self):
        self.assertEqual(_j(0x80019B3C), 0x080066CF)

    def test_patch_applies_and_checks(self):
        out = patch_exe(bytes(fake_exe()))
        for p in PATCHES:
            self.assertEqual(out[p.offset:p.offset + len(p.new)], p.new)
        with self.assertRaises(CodePatchError):
            patch_exe(out)                     # already patched: original bytes gone
        with self.assertRaises(CodePatchError):
            patch_exe(b"\0" * 0x20000)         # not an executable

    def test_offsets(self):
        self.assertEqual(PATCHES[0].offset, 0x9980 + 0x800)

    @need("us1")
    @need("us2")
    def test_us_executables(self):
        exes = [parse(_open(DEFAULT_CUE[("us", d)])).read(EXE_NAME[d]) for d in (1, 2)]
        self.assertEqual(exes[0], exes[1])
        out = patch_exe(exes[0])
        diff = [i for i, (a, b) in enumerate(zip(exes[0], out)) if a != b]
        self.assertTrue(diff and min(diff) >= PATCHES[0].offset and max(diff) < PATCHES[0].offset + 8)

    @need("us1")
    def test_driver_overlap(self):
        from vpundub.codepatch import DRIVER_OVERLAP, DRIVERS
        exe = parse(_open(DEFAULT_CUE[("us", 1)])).read(EXE_NAME[1])
        p = DRIVER_OVERLAP
        self.assertEqual(exe[p.offset:p.offset + len(p.old)], p.old)
        out = patch_exe(exe, DRIVERS["overlap"])
        self.assertEqual(out[p.offset:p.offset + 32], p.new)
        self.assertEqual(int.from_bytes(p.new[16:20], "little"), _beqz(2, 0x80019980, 0x800199DC))
        diff = [i for i, (x, y) in enumerate(zip(exe, out)) if x != y]
        self.assertTrue(min(diff) >= p.offset and max(diff) < p.offset + 32)
        with self.assertRaises(CodePatchError):        # the two driver patches overlap
            patch_exe(out, DRIVERS["wait"])

    def test_beqz_encoding(self):
        self.assertEqual(_beqz(3, 0x80052D04, 0x80052E34), 0x1060004B)

    def test_patch_block_keeps_size(self):
        data = bytes(range(256)) * 64
        blk = slz.compress(data, 1)
        new = bytearray(data)
        new[1000:1008] = b"PATCHED!"
        out = slz.patch_block(blk, bytes(new))
        self.assertEqual(slz.decompress(out), bytes(new))
        self.assertLess(slz.header(out)[1], slz.header(blk)[1] + 64)

    @need("us1")
    @need("us2")
    def test_battle_engine(self):
        ps = slot_patches()[1490]
        for d in (1, 2):
            arc, _ = _archive(_open(DEFAULT_CUE[("us", d)]))
            us = arc[1490]
            new = apply_slot_patches(lambda s: arc[s], {})[1490]
            want = patched_slot_data(us, ps)
            self.assertEqual(slz.decompress(new), want)
            old = slz.decompress(us)
            diff = [i for i, (a, b) in enumerate(zip(old, want)) if a != b]
            self.assertTrue(diff and all(any(p.offset <= i < p.offset + len(p.new) for p in ps) for i in diff))
            self.assertLessEqual(len(new), len(us))   # same sector count as the original

    def test_whole_limit_encoding(self):
        from vpundub.codepatch import whole_limit_patches
        self.assertEqual(whole_limit_patches(15), ())
        self.assertEqual((15 + 1) * 2048 - 1, 0x7FFF)                 # the original limit
        ps = whole_limit_patches(20)
        self.assertEqual([p.addr for p in ps], [0x80052CD0, 0x80053078])
        self.assertTrue(all(p.new == (0x3402A7FF).to_bytes(4, "little") for p in ps))   # ori v0, zero, 0xa7ff
        with self.assertRaises(CodePatchError):
            whole_limit_patches(40)

    @need("us1")
    def test_whole_limit_sites(self):
        from vpundub.codepatch import SLOT_PATCHES, whole_limit_patches
        arc, _ = _archive(_open(DEFAULT_CUE[("us", 1)]))
        old = slz.decompress(arc[1490])
        for p in whole_limit_patches(20):
            self.assertEqual(old[p.offset:p.offset + 4], p.old)
        new = apply_slot_patches(lambda s: arc[s], {}, SLOT_PATCHES + whole_limit_patches(20))[1490]
        self.assertEqual(slz.decompress(new), patched_slot_data(arc[1490], slot_patches(
            SLOT_PATCHES + whole_limit_patches(20))[1490]))

    def test_inject_records(self):
        import struct
        old = bytes(range(64))
        new = bytearray(old); new[3] = 0xFF; new[10] = 0xFE; new[40:42] = b"\xAA\xBB"
        rec = inject_records(0x80010000, old, bytes(new), gap=16)
        out, pos = [], 0
        while pos < len(rec):
            a, n = struct.unpack_from("<II", rec, pos)
            out.append((a, rec[pos + 8:pos + 8 + n], rec[pos + 8 + n:pos + 8 + 2 * n]))
            pos += 8 + 2 * n
        # 3 and 10 are joined (gap < 16), 40-41 is its own run
        self.assertEqual(out, [(0x80010003, old[3:11], bytes(new[3:11])), (0x80010028, old[40:42], b"\xAA\xBB")])
        self.assertEqual(inject_records(0, old, old), b"")
        with self.assertRaises(CodePatchError):
            inject_records(0, old, old[:-1])

    @need("us1")
    def test_inject_records_cover_slot_patches(self):
        # applying the records to the US slot gives the patched slot (what the emulator tests write)
        import struct
        for slot, ps in slot_patches().items():
            arc, _ = _archive(_open(DEFAULT_CUE[("us", 1)]))
            old = slz.decompress(arc[slot]); want = patched_slot_data(arc[slot], ps)
            rec, ram, pos, base = inject_records(ps[0].base, old, want), bytearray(old), 0, ps[0].base
            while pos < len(rec):
                a, n = struct.unpack_from("<II", rec, pos)
                self.assertEqual(bytes(ram[a - base:a - base + n]), rec[pos + 8:pos + 8 + n])
                ram[a - base:a - base + n] = rec[pos + 8 + n:pos + 8 + 2 * n]
                pos += 8 + 2 * n
            self.assertEqual(bytes(ram), want)


if __name__ == "__main__":
    unittest.main()
