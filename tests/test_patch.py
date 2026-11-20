"""make_patch: xdelta3 patch whose source is several files joined (US + JP image)."""
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from vpundub.build import make_patch


@unittest.skipIf(shutil.which("xdelta3") is None, "xdelta3 not installed")
class MakePatchTest(unittest.TestCase):
    def test_joined_source(self):
        rnd = random.Random(1)
        us = bytes(rnd.getrandbits(8) for _ in range(300_000))
        jp = bytes(rnd.getrandbits(8) for _ in range(300_000))
        # target: US with a block of JP data in the middle (what the undub does with voices)
        dst = us[:100_000] + jp[50_000:150_000] + us[200_000:]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "us.bin").write_bytes(us)
            (d / "jp.bin").write_bytes(jp)
            (d / "out.bin").write_bytes(dst)
            patch = d / "p.xdelta"
            sha = make_patch([d / "us.bin", d / "jp.bin"], d / "out.bin", patch, log=lambda *_: None)
            self.assertIsNotNone(sha)
            self.assertLess(patch.stat().st_size, 5_000)           # the JP block is copied, not stored
            self.assertEqual(sorted(p.name for p in d.iterdir()), ["jp.bin", "out.bin", "p.xdelta", "us.bin"])
            # apply as a user would: join the images, then decode
            (d / "src.bin").write_bytes(us + jp)
            subprocess.run(["xdelta3", "-d", "-f", "-s", str(d / "src.bin"), str(patch), str(d / "new.bin")],
                           check=True)
            self.assertEqual((d / "new.bin").read_bytes(), dst)
            # the US image alone is not enough: xdelta3 fails (window checksum)
            r = subprocess.run(["xdelta3", "-d", "-f", "-s", str(d / "us.bin"), str(patch), str(d / "bad.bin")],
                               capture_output=True)
            self.assertNotEqual(r.returncode, 0)
            # nor the images joined in the wrong order
            (d / "rev.bin").write_bytes(jp + us)
            r = subprocess.run(["xdelta3", "-d", "-f", "-s", str(d / "rev.bin"), str(patch), str(d / "bad.bin")],
                               capture_output=True)
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
