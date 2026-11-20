import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src", "vpsingle"))
import cdsector  # noqa: E402

D1 = os.path.join(ROOT, "roms", "us", "Valkyrie Profile (USA) (Disc 1)", "Valkyrie Profile (USA) (Disc 1).bin")


class TestSynthetic(unittest.TestCase):
    def test_header(self):
        self.assertEqual(cdsector.lba_header(0), bytes((0x00, 0x02, 0x00, 2)))
        self.assertEqual(cdsector.lba_header(296896 - 150), bytes((0x65, 0x58, 0x46, 2)))

    def test_encode_reencode_stable(self):
        data = bytes((i * 13) & 0xFF for i in range(2048))
        s = cdsector.encode_form1(1000, bytes((0, 0, 8, 0)), data)
        self.assertEqual(len(s), cdsector.SECTOR)
        self.assertEqual(s[:12], cdsector.SYNC)
        self.assertEqual(s[24:2072], data)
        self.assertEqual(cdsector.reencode(s, 1000), s)

    def test_data_change_changes_edc(self):
        data = bytes(2048)
        a = cdsector.encode_form1(5, bytes((0, 0, 8, 0)), data)
        b = cdsector.encode_form1(5, bytes((0, 0, 8, 0)), b"\x01" + data[1:])
        self.assertNotEqual(a[2072:2076], b[2072:2076])


@unittest.skipUnless(os.path.exists(D1), "ROMs not present")
class TestDisc(unittest.TestCase):
    def test_reencode_matches_original(self):
        # re-encoding a mastered Form 1 sector must reproduce it bit for bit (EDC + P/Q ECC)
        with open(D1, "rb") as f:
            for lba in (16, 22, 24, 150, 151, 1000, 200000):
                f.seek(lba * cdsector.SECTOR)
                raw = f.read(cdsector.SECTOR)
                self.assertEqual(cdsector.reencode(raw, lba), raw, lba)


if __name__ == "__main__":
    unittest.main()
