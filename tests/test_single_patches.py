import os
import struct
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src", "vpsingle"))
import patches  # noqa: E402


def load_delay_hazards(code: bytes) -> list[int]:
    """Indices of instructions that read the register loaded by the instruction just before them
    (the R3000 returns the old value there). Covers lw/lh/lb(u) followed by jr/jalr or any rs/rt use."""
    ws = struct.unpack(f"<{len(code) // 4}I", code)
    bad = []
    for i in range(1, len(ws)):
        p, w = ws[i - 1], ws[i]
        if (p >> 26) in (0x20, 0x21, 0x23, 0x24, 0x25):
            rt = (p >> 16) & 31
            if rt and rt in ((w >> 21) & 31, (w >> 16) & 31):
                bad.append(i)
    return bad


class TestPatches(unittest.TestCase):
    def test_lengths_and_alignment(self):
        for p in patches.PATCHES:
            self.assertEqual(len(p.orig), len(p.new))
            self.assertEqual(p.offset % 4, 0)

    def test_no_load_delay_hazard(self):
        for p in patches.PATCHES:
            self.assertEqual(load_delay_hazards(p.new), [], p.name)

    def test_hazard_detector(self):
        bad = patches.words(0x8FBF0018, 0x03E00008)        # lw ra,24(sp); jr ra
        self.assertEqual(load_delay_hazards(bad), [1])

    def test_lid_wait_calls(self):
        ws = struct.unpack("<15I", patches.LID_WAIT.new)
        self.assertIn(patches.jal(patches.CD_SYNC), ws)
        self.assertIn(patches.jal(patches.CD_CONTROL_B), ws)
        self.assertEqual(patches.LID_WAIT.offset, 0x273C)

    def test_apply_verifies_and_is_idempotent(self):
        p = patches.LID_WAIT
        buf = bytearray(b"\xAA" * 16 + p.orig + b"\xBB" * 16)
        base = p.offset - 16
        patches.apply(buf, p, base)
        self.assertEqual(bytes(buf[16:16 + len(p.new)]), p.new)
        before = bytes(buf)
        patches.apply(buf, p, base)
        self.assertEqual(bytes(buf), before)
        with self.assertRaises(ValueError):
            patches.apply(bytearray(len(buf)), p, base)


if __name__ == "__main__":
    unittest.main()
