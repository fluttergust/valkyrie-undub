"""Phase 5 tests: end-to-end build, determinism and output verification.

    PYTHONPATH=src python3 -m unittest tests.test_build -v
"""
from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_disc import need, sha1_file  # noqa: E402
from vpundub.build import BuildError, build_disc, verify_output  # noqa: E402
from vpundub.disc.image import DiscImage  # noqa: E402

# Expected output of `python -m vpundub build --disc N` (regression: any change
# to the build must be deliberate and update these values).
EXPECTED_SHA1 = {
    1: "fb2874c3f0c6a3aec7b930a19ece7f7c7be9617f",
    2: "0ac85d3fd1427ec26c6cfd6c6174fa48c1f1bec1",
}
TMP = ROOT / "build" / "test_build"


class TestBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TMP.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    @need("us1")
    @need("jp1")
    def test_disc1_deterministic_and_verified(self):
        res = build_disc(1, out_dir=TMP, patch=False, log=lambda m: None)
        self.assertEqual(sha1_file(res.bin_path), EXPECTED_SHA1[1])
        self.assertEqual(res.manifest["output"]["sectors"], 310565)
        self.assertGreater(res.manifest["filler_tail_sectors_left"], 0)
        self.assertGreater(res.manifest["subtitles"]["pages"], 50)

        # verify_output must catch a modified non-voice byte (slot 1 = main program).
        import vpundub.build as B
        img = DiscImage(res.bin_path)
        us, base = B._archive(B._open(B.DEFAULT_CUE[("us", 1)]))
        jp, _ = B._archive(B._open(B.DEFAULT_CUE[("jp", 1)]))
        plan = B.build_plan(us, jp)
        new = B.apply_plan(us, jp, plan)
        tampered = dict(new)
        tampered[1] = us[1][:-1] + b"\x01"
        with self.assertRaises(BuildError):
            verify_output(img, us, jp, plan, tampered, log=lambda m: None)

    def test_rejects_wrong_input(self):
        bogus = TMP / "bogus.bin"
        TMP.mkdir(parents=True, exist_ok=True)
        bogus.write_bytes(bytes(2352 * 32))
        with self.assertRaises(BuildError):
            build_disc(1, us_path=bogus, jp_path=bogus, out_dir=TMP, patch=False, log=lambda m: None)


if __name__ == "__main__":
    unittest.main()
