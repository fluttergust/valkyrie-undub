"""End-to-end build check (needs the two disc images in ./roms; about 20 s).

Other inputs (e.g. an undub pair) can be checked with VP_DISC1=<disc1.bin> VP_DISC2=<disc2.bin>, plus
VP_OVERRIDE=INDEX=FILE[,INDEX=FILE...] for entries that differ between the discs; the reference-image checks
are skipped for them. TestOverride checks overrides on the reference discs (one smaller, one bigger than
the entry it replaces; the entries after them move)."""
import hashlib
import os
import shutil
import struct
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src", "vpsingle"))
import builder  # noqa: E402
import cdsector  # noqa: E402
import patches  # noqa: E402
import slz  # noqa: E402
import vptoc  # noqa: E402

D1 = os.environ.get("VP_DISC1") or os.path.join(ROOT, "roms", "us", "Valkyrie Profile (USA) (Disc 1)", "Valkyrie Profile (USA) (Disc 1).bin")
D2 = os.environ.get("VP_DISC2") or os.path.join(ROOT, "roms", "us", "Valkyrie Profile (USA) (Disc 2)", "Valkyrie Profile (USA) (Disc 2).bin")
HAVE_ROMS = os.path.exists(D1) and os.path.exists(D2)
OVERRIDES = {}
for _o in filter(None, os.environ.get("VP_OVERRIDE", "").split(",")):
    _i, _, _p = _o.partition("=")
    with open(_p, "rb") as _f:
        OVERRIDES[int(_i)] = _f.read()
MODULE_ENTRIES = {mp.entry for mp in patches.MODULE_PATCHES} - set(OVERRIDES)
OUTDIR = os.path.join(ROOT, "build", "test")
OUT = os.path.join(OUTDIR, "unified.bin")

# Reference inputs (Redump-style US images) and the expected output for them.
D1_SHA1 = "fa13cbde448a47fa87b6c7829df1c5e6215f5f66"
D2_SHA1 = "7cc65766b46273b4ee166269170ecbaa433908fd"
OUT_SHA1 = "da54b9839116ee6bc4bf610231a2785ff1e4c12d"


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def assert_contiguous(tc, toc, archive_end):
    es = sorted((e for e in toc.entries if e.lba is not None), key=lambda e: e.lba)
    tc.assertEqual(es[0].lba, vptoc.ARCHIVE_LBA + vptoc.HEADER_SECTORS)
    for a, b in zip(es, es[1:]):
        tc.assertEqual(a.lba + a.size, b.lba, (a, b))
    tc.assertEqual(es[-1].lba + es[-1].size, archive_end)


@unittest.skipUnless(HAVE_ROMS, "ROMs not present")
class TestBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(OUTDIR, exist_ok=True)
        cls.r = builder.build(D1, D2, OUT, overrides=OVERRIDES)
        cls.t1, _ = vptoc.load(D1)
        cls.t2, _ = vptoc.load(D2)
        cls.tu, _ = vptoc.load(OUT)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(OUTDIR, ignore_errors=True)

    def raw(self, path, lba):
        with open(path, "rb") as f:
            f.seek(lba * cdsector.SECTOR)
            return f.read(cdsector.SECTOR)

    def test_layout(self):
        self.assertEqual(self.r["total"], self.r["archive_end"] + builder.POSTGAP)
        self.assertLessEqual(self.r["total"], 360000)              # fits an 80-minute CD-R
        self.assertEqual(os.path.getsize(OUT), self.r["total"] * cdsector.SECTOR)
        self.assertEqual(self.r["sha1"], sha1(OUT))

    def test_reference_output(self):
        if OVERRIDES or (sha1(D1), sha1(D2)) != (D1_SHA1, D2_SHA1):
            self.skipTest("inputs differ from the reference images")
        self.assertEqual(self.r["total"], 336718)
        self.assertEqual(self.r["sha1"], OUT_SHA1)

    def test_unified_toc(self):
        n = sum(e.lba is not None for e in self.tu.entries)
        n1 = sum(e.lba is not None for e in self.t1.entries)
        self.assertEqual(n, n1 + self.r["appended_files"])
        self.assertIsNotNone(self.tu.get(2).lba)        # both disc markers
        self.assertIsNotNone(self.tu.get(3).lba)

    def test_archive_contiguous(self):
        # like the original discs: entries back to back from the header on, the last one ending the archive
        assert_contiguous(self, self.tu, self.r["archive_end"])

    def test_every_file_matches_its_source(self):
        # first and last sector of every file: disc 1 copy if present there, else disc 2
        for eu in self.tu.entries:
            if eu.lba is None:
                continue
            if eu.index in OVERRIDES:
                data = OVERRIDES[eu.index]
                self.assertEqual(eu.size, builder.sectors(len(data)))
                got = vptoc.read_user(OUT, eu.lba, eu.size)
                self.assertEqual(got[:len(data)], data)
                self.assertFalse(got[len(data):].strip(b"\0"))
                continue
            e1 = self.t1.get(eu.index)
            src, e = (D1, e1) if e1.lba is not None else (D2, self.t2.get(eu.index))
            self.assertEqual(eu.size, e.size)
            if eu.index in MODULE_ENTRIES:
                continue                                    # test_module_patches
            for k in (0, eu.size - 1):
                self.assertEqual(vptoc.read_user(OUT, eu.lba + k), vptoc.read_user(src, e.lba + k), eu)

    def test_iso_and_patch(self):
        img = builder.Image(OUT)
        self.assertEqual(builder.find_file(img, "VALKYRIE.BIN")[1],
                         (self.r["archive_end"] - vptoc.ARCHIVE_LBA) * 2048)
        lba, _ = builder.find_file(img, patches.EXE)
        p = patches.LID_WAIT
        exe = vptoc.read_user(OUT, lba, 64)
        self.assertEqual(exe[p.offset:p.offset + len(p.new)], p.new)
        self.assertEqual(struct.unpack_from("<I", self.raw(OUT, 16), 24 + 80)[0], self.r["total"])

    def test_module_patches(self):
        # the patched overlays decode to the source overlay with only the patched words changed
        for idx in sorted(MODULE_ENTRIES):
            mps = [mp for mp in patches.MODULE_PATCHES if mp.entry == idx]
            eu, e1 = self.tu.get(idx), self.t1.get(idx)
            self.assertEqual(eu.size, e1.size)
            got = slz.decompress(vptoc.read_user(OUT, eu.lba, eu.size))
            want = bytearray(slz.decompress(vptoc.read_user(D1, e1.lba, e1.size)))
            for mp in mps:
                o = mp.vaddr - mp.base
                self.assertEqual(want[o:o + len(mp.orig)], mp.orig, mp.name)
                want[o:o + len(mp.new)] = mp.new
            self.assertEqual(got, bytes(want), idx)
            for k in range(eu.size):
                raw = self.raw(OUT, eu.lba + k)
                self.assertEqual(cdsector.reencode(raw, eu.lba + k), raw)
                self.assertEqual(raw[16:24], self.raw(D1, e1.lba + k)[16:24])      # subheader kept

    def test_sectors_valid(self):
        # all rewritten sectors carry a correct header, EDC and ECC
        for lba in [16, 22, 24 + patches.LID_WAIT.offset // 2048] + list(range(150, 163)) + \
                   [self.r["d1_end"], self.r["archive_end"] - 1]:
            raw = self.raw(OUT, lba)
            self.assertEqual(raw[12:16], cdsector.lba_header(lba), lba)
            self.assertEqual(cdsector.reencode(raw, lba), raw, lba)


@unittest.skipUnless(HAVE_ROMS and not OVERRIDES, "ROMs not present")
class TestOverride(unittest.TestCase):
    """Two overrides on shared entries of the reference discs: 4795 grows by one sector, 4796 shrinks to two;
    both stay in place and the entries after them move."""
    SMALL, BIG = 4796, 4795

    @classmethod
    def setUpClass(cls):
        t1, _ = vptoc.load(D1)
        cls.e_small, cls.e_big = t1.get(cls.SMALL), t1.get(cls.BIG)
        cls.ov = {cls.SMALL: bytes(range(256)) * 9 + b"tail",                    # 2 sectors
                  cls.BIG: b"\x5a" * ((cls.e_big.size + 1) * 2048 - 7)}
        os.makedirs(OUTDIR, exist_ok=True)
        cls.out = os.path.join(OUTDIR, "override.bin")
        cls.r = builder.build(D1, D2, cls.out, overrides=cls.ov)
        cls.tu, _ = vptoc.load(cls.out)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(OUTDIR, ignore_errors=True)

    def test_placement(self):
        self.assertEqual(self.r["overridden"], [self.BIG, self.SMALL])
        es, eb = self.tu.get(self.SMALL), self.tu.get(self.BIG)
        self.assertEqual((eb.lba, eb.size), (self.e_big.lba, self.e_big.size + 1))
        self.assertEqual((es.lba, es.size), (self.e_big.lba + self.e_big.size + 1, 2))
        delta = 1 + 2 - self.e_small.size
        self.assertEqual(self.r["total"], builder.plan(D1, D2)["archive_end"] + builder.POSTGAP + delta)
        assert_contiguous(self, self.tu, self.r["archive_end"])
        t1, _ = vptoc.load(D1)
        e1, eu = t1.get(self.SMALL + 1), self.tu.get(self.SMALL + 1)       # a moved disc 1 file
        self.assertEqual(eu.lba, e1.lba + delta)
        self.assertEqual(vptoc.read_user(self.out, eu.lba, eu.size), vptoc.read_user(D1, e1.lba, e1.size))

    def test_data_and_sectors(self):
        for idx, data in self.ov.items():
            e = self.tu.get(idx)
            got = vptoc.read_user(self.out, e.lba, e.size)
            self.assertEqual(got[:len(data)], data)
            self.assertFalse(got[len(data):].strip(b"\0"))
            with open(self.out, "rb") as f:
                for k in range(e.size):
                    f.seek((e.lba + k) * cdsector.SECTOR)
                    raw = f.read(cdsector.SECTOR)
                    self.assertEqual(cdsector.reencode(raw, e.lba + k), raw)
                    last = e.lba + k == self.r["archive_end"] - 1
                    self.assertEqual(raw[16:20], builder.EOF_SUBHEADER if last else builder.DATA_SUBHEADER)


if __name__ == "__main__":
    unittest.main()
