"""src/slz.py: token parse/encode round trip and in-place patching (synthetic streams; the real overlays are
covered by tests/test_builder.py and, when the ROMs are present, below)."""
import os
import struct
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src", "vpsingle"))
import patches  # noqa: E402
import slz  # noqa: E402
import vptoc  # noqa: E402

D1 = os.environ.get("VP_DISC1") or os.path.join(ROOT, "roms", "us", "Valkyrie Profile (USA) (Disc 1)", "Valkyrie Profile (USA) (Disc 1).bin")


def block(typ: int, usize: int, body: bytes) -> bytes:
    return b"SLZ" + bytes([typ]) + struct.pack("<III", len(body), usize, 0) + body


# type 2: "abcd" literals, copy 8 from dist 4 (overlapping), short run of 'Z' x5, long run of 'q' x(19+1), "!"
#   flags group 1: L L L L C R R L -> bits 0b10001111; group 2: L
T2 = block(2, 4 + 8 + 5 + 20 + 1 + 1,
           bytes([0b10001111]) + b"abcd" + bytes([4, 5 << 4]) + bytes([0x5A, 0xF2])
           + bytes([1, 0xF0, ord("q")]) + b"!" + bytes([1]) + b".")
T2_PLAIN = b"abcd" + b"abcdabcd" + b"Z" * 5 + b"q" * 20 + b"!" + b"."
# type 1: "xyz", copy 6 from dist 3, end token
T1 = block(1, 9, bytes([0b0111]) + b"xyz" + bytes([3, 3 << 4]) + bytes([0, 0]))


class TestSlz(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(slz.decompress(T2), T2_PLAIN)
        self.assertEqual(slz.decompress(T1), b"xyz" * 3)

    def test_encode_round_trip(self):
        for data in (T1, T2):
            typ, toks = slz.tokens(data)
            self.assertEqual(slz.encode(typ, len(slz.decompress(data)), toks), data)

    def test_patch_literal_copy_run(self):
        for off, new in ((1, b"B"), (6, b"!!"), (13, b"zz"), (30, b"Q"), (0, b"ABCDEFGHIJKL")):
            want = bytearray(T2_PLAIN)
            want[off:off + len(new)] = new
            got = slz.patch(T2, {off: new})
            self.assertEqual(slz.decompress(got), bytes(want), (off, new))

    def test_patch_fixes_later_copies(self):
        # the copy at 4..11 reads bytes 0..7: changing byte 2 must not leak into the copy's output
        got = slz.decompress(slz.patch(T2, {2: b"#"}))
        self.assertEqual(got[:12], b"ab#dabcdabcd")
        got = slz.decompress(slz.patch(T1, {0: b"X"}))
        self.assertEqual(got, b"Xyzxyzxyz")

    def test_patch_keeps_length_when_it_fits(self):
        padded = T2 + bytes(64)
        self.assertEqual(len(slz.patch(padded, {1: b"B"})), len(padded))

    def test_bad_streams(self):
        with self.assertRaises(slz.SlzError):
            slz.tokens(b"XYZ" + bytes(13))
        with self.assertRaises(slz.SlzError):
            slz.decompress(block(1, 3, bytes([0]) + bytes([5, 0])))        # copy before any output
        with self.assertRaises(slz.SlzError):
            slz.patch(T1, {8: b"ab"})                                       # past the end


class TestModulePatches(unittest.TestCase):
    def test_definitions(self):
        self.assertEqual(sorted(mp.entry for mp in patches.MODULE_PATCHES), [1, 2174, 4806])
        for mp in patches.MODULE_PATCHES:
            self.assertEqual(len(mp.orig), len(mp.new))
            self.assertEqual((mp.vaddr - mp.base) % 4, 0)
            self.assertEqual(sum(a != b for a, b in zip(mp.orig, mp.new)), 1)      # only li a0, 1 -> 0

    @unittest.skipUnless(os.path.exists(D1), "ROMs not present")
    def test_real_overlays(self):
        t1, _ = vptoc.load(D1)
        for mp in patches.MODULE_PATCHES:
            e = t1.get(mp.entry)
            data = vptoc.read_user(D1, e.lba, e.size)
            new = patches.apply_module(data, [mp])
            self.assertEqual(len(new), len(data))                        # still the same sectors
            self.assertLessEqual(slz.header(new)[1] - slz.header(data)[1], 64)
            plain = slz.decompress(new)
            o = mp.vaddr - mp.base
            self.assertEqual(plain[o:o + len(mp.new)], mp.new)
            self.assertEqual(patches.apply_module(new, [mp]), new)        # idempotent
