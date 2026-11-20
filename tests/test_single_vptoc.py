import os
import struct
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src", "vpsingle"))
import vptoc  # noqa: E402

D1 = os.path.join(ROOT, "roms", "us", "Valkyrie Profile (USA) (Disc 1)", "Valkyrie Profile (USA) (Disc 1).bin")
D2 = os.path.join(ROOT, "roms", "us", "Valkyrie Profile (USA) (Disc 2)", "Valkyrie Profile (USA) (Disc 2).bin")
HAVE_ROMS = os.path.exists(D1) and os.path.exists(D2)


class TestCodec(unittest.TestCase):
    def test_lba_msf_roundtrip(self):
        for lba in (0, 13, 163, 296896, 359999):
            self.assertEqual(vptoc.msf_to_lba(*vptoc.lba_to_msf(lba)), lba)

    def test_entry_encode_decode(self):
        e = vptoc.Entry(5, 12165, 0x1234)
        w, lo = e.encode()
        self.assertEqual(w, bytes.fromhex("02441512"))       # MSF 02:44:15 (LBA 12165 + 150), size hi 0x12
        self.assertEqual(lo, 0x34)
        self.assertEqual(vptoc.decode_entry(5, w, lo), e)
        self.assertEqual(vptoc.decode_entry(7, bytes(4), 0), vptoc.Entry(7, None, 0))

    def test_keystream_generator(self):
        ks = vptoc.KEYSTREAM
        self.assertEqual(len(ks), vptoc.HEADER_BYTES)
        self.assertEqual(struct.unpack_from("<I", ks, 0)[0], vptoc.SEED)
        k = vptoc.SEED
        k2 = (k ^ (k << 1)) & 0xFFFFFFFF
        self.assertEqual(ks[vptoc.TOC_BYTES], k2 & 0xFF)
        self.assertEqual(struct.unpack_from("<I", ks, 4)[0], (~k2 ^ vptoc.SEED) & 0xFFFFFFFF)

    def test_crypt_is_involution(self):
        data = bytes((i * 7) & 0xFF for i in range(vptoc.HEADER_BYTES))
        self.assertEqual(vptoc.crypt(vptoc.crypt(data)), data)

    def test_build_parse_roundtrip(self):
        ents = [vptoc.Entry(i, None, 0) for i in range(1, vptoc.N_WORDS)]
        ents[0] = vptoc.Entry(1, 163, 300)
        ents[2] = vptoc.Entry(3, 463, 1)
        t = vptoc.Toc(ents)
        self.assertEqual(vptoc.Toc.parse(t.build()).entries, ents)


@unittest.skipUnless(HAVE_ROMS, "ROMs not present")
class TestDiscs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t1, cls.c1 = vptoc.load(D1)
        cls.t2, cls.c2 = vptoc.load(D2)

    def test_roundtrip_bit_exact(self):
        self.assertEqual(vptoc.crypt(self.t1.build()), self.c1)
        self.assertEqual(vptoc.crypt(self.t2.build()), self.c2)

    def test_entry_counts(self):
        self.assertEqual(sum(e.lba is not None for e in self.t1.entries), 4472)
        self.assertEqual(sum(e.lba is not None for e in self.t2.entries), 4451)

    def test_disc_markers(self):
        # entry 2 exists only on disc 1, entry 3 only on disc 2 (checked by the disc-change routine)
        self.assertIsNotNone(self.t1.get(2).lba)
        self.assertIsNone(self.t1.get(3).lba)
        self.assertIsNone(self.t2.get(2).lba)
        self.assertIsNotNone(self.t2.get(3).lba)

    def test_entries_inside_archive(self):
        for t, path in ((self.t1, D1), (self.t2, D2)):
            end = os.path.getsize(path) // vptoc.SECTOR_RAW
            for e in t.entries:
                if e.lba is not None:
                    self.assertTrue(vptoc.ARCHIVE_LBA + vptoc.HEADER_SECTORS <= e.lba < end, e)

    def test_shared_entry_points_at_same_data(self):
        a, b = self.t1.get(1825), self.t2.get(1825)
        self.assertNotEqual(a.lba, b.lba)
        self.assertEqual(vptoc.read_user(D1, a.lba), vptoc.read_user(D2, b.lba))


if __name__ == "__main__":
    unittest.main()
