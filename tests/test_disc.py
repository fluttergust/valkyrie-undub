"""Disc layer tests (EDC/ECC, sectors, ISO9660 read/rebuild).

ROM-dependent tests skip when ./roms is missing. Run from the repo root:
    PYTHONPATH=src python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from vpundub.disc.__main__ import verify_image  # noqa: E402
from vpundub.disc.edc_ecc import edc, edc_batch, regenerate  # noqa: E402
from vpundub.disc.image import DiscImage  # noqa: E402
from vpundub.disc.iso9660 import parse, rebuild  # noqa: E402
from vpundub.disc.sector import SubHeader, build_sector, lba_to_msf_header  # noqa: E402

ROMS = {
    "us1": ROOT / "roms/us/Valkyrie Profile (USA) (Disc 1)/Valkyrie Profile (USA) (Disc 1).bin",
    "us2": ROOT / "roms/us/Valkyrie Profile (USA) (Disc 2)/Valkyrie Profile (USA) (Disc 2).bin",
    "jp1": ROOT / "roms/japan/Valkyrie Profile (Japan) (Disc 1)/Valkyrie Profile (Japan) (Disc 1).bin",
    "jp2": ROOT / "roms/japan/Valkyrie Profile (Japan) (Disc 2)/Valkyrie Profile (Japan) (Disc 2).bin",
}
SHA1 = {
    "us1": "fa13cbde448a47fa87b6c7829df1c5e6215f5f66",
    "us2": "7cc65766b46273b4ee166269170ecbaa433908fd",
    "jp1": "463af3fc81befbbf92c89c47b16dac72fc85f30d",
    "jp2": "b992244720af08b97e37c1fba8dc6e86790c7fbc",
}
TMP = ROOT / "build" / "test_tmp"


def sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def need(key: str):
    return unittest.skipUnless(ROMS[key].exists(), f"ROM {key} not present")


class TestSector(unittest.TestCase):
    def test_msf(self):
        self.assertEqual(lba_to_msf_header(0), bytes([0x00, 0x02, 0x00, 2]))
        self.assertEqual(lba_to_msf_header(16), bytes([0x00, 0x02, 0x16, 2]))
        # 310564 + 150 = 310714 = 69:02:64
        self.assertEqual(lba_to_msf_header(310564), bytes([0x69, 0x02, 0x64, 2]))

    def test_edc_scalar_matches_batch(self):
        rng = np.random.default_rng(1234)
        blk = rng.integers(0, 256, size=(5, 2056), dtype=np.uint8)
        got = edc_batch(blk)
        for i in range(5):
            self.assertEqual(int(got[i]), edc(blk[i].tobytes()))

    def test_build_sector_deterministic(self):
        s = build_sector(1000, SubHeader(0, 0, 0x08, 0), b"VALKYRIE" * 256)
        self.assertEqual(len(s), 2352)
        # Regenerating an already-correct sector must be a no-op.
        self.assertEqual(regenerate(s), s)
        # Regression vector (algorithm validated against 1.24M real sectors).
        self.assertEqual(hashlib.sha1(s).hexdigest(), SYNTH_SHA1)

    def test_form2_edc_flag(self):
        sub = SubHeader(0, 0, 0x20, 0)
        z = build_sector(5, sub, b"", form2_edc=False)
        n = build_sector(5, sub, b"", form2_edc=True)
        self.assertEqual(z[0x92C:0x930], b"\0\0\0\0")
        self.assertNotEqual(n[0x92C:0x930], b"\0\0\0\0")


SYNTH_SHA1 = "873e676223261f78e471a5c62b9bd28cf7dd2834"


class _RomTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TMP.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)


class TestOriginals(_RomTest):
    def _check(self, key):
        img = DiscImage(ROMS[key])
        self.assertEqual(verify_image(img), [], "original sectors must regenerate exactly")
        vol = parse(img)
        self.assertEqual(vol.volume_size, len(img))
        names = sorted(e.name for e in vol.files)
        self.assertIn("VALKYRIE.BIN;1", names)
        self.assertIn("SYSTEM.CNF;1", names)

    @need("us1")
    def test_us1(self): self._check("us1")

    @need("jp1")
    def test_jp1(self): self._check("jp1")


class TestRoundTrip(_RomTest):
    """No-change rebuild must be bit-identical to the original pressing."""

    def _rt(self, key):
        out = TMP / f"rt_{key}.bin"
        rebuild(parse(DiscImage(ROMS[key])), str(out))
        self.assertEqual(sha1_file(out), SHA1[key])
        out.unlink()

    @need("us1")
    def test_us1(self): self._rt("us1")

    @need("us2")
    def test_us2(self): self._rt("us2")

    @need("jp1")
    def test_jp1(self): self._rt("jp1")

    @need("jp2")
    def test_jp2(self): self._rt("jp2")


class TestModifiedRebuild(_RomTest):
    @need("us1")
    def test_shrink_and_relocate(self):
        vol = parse(DiscImage(ROMS["us1"]))
        small = bytes(range(256)) * 40000 + b"tail"          # ~10 MB, odd size
        cnf = vol.read("SYSTEM.CNF") + b"\r\n" + b"X" * 3000  # 2 sectors -> must move
        out = TMP / "mod_us1.bin"
        new = rebuild(vol, str(out), {"VALKYRIE.BIN": small, "SYSTEM.CNF": cnf})

        self.assertEqual(new.read("VALKYRIE.BIN"), small)
        self.assertEqual(new.read("SYSTEM.CNF"), cnf)
        self.assertEqual(new.read("SLUS_011.56"), vol.read("SLUS_011.56"))
        self.assertEqual(new.find("SLUS_011.56").lba, 24)
        self.assertEqual(new.find("VALKYRIE.BIN").lba, 150)
        self.assertGreater(new.find("SYSTEM.CNF").lba, 150)   # relocated to the end
        self.assertEqual(new.volume_size, len(new.image))
        self.assertEqual(new.postgap(), 150)
        self.assertEqual(verify_image(new.image), [])
        # Sectors before the first file are untouched apart from the patched records.
        for lba in (0, 4, 12, 17, 18, 21):
            self.assertEqual(new.image.sector(lba), vol.image.sector(lba))
        # File subheaders: data on every sector, EOF|EOR on the last.
        e = new.find("VALKYRIE.BIN")
        self.assertEqual(new.image.subheader(e.lba).submode, 0x08)
        self.assertEqual(new.image.subheader(e.lba + e.sectors - 1).submode, 0x89)
        out.unlink()


if __name__ == "__main__":
    unittest.main()
