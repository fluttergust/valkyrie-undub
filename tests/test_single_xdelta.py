"""tools/make_xdelta.sh: the joined-source patch round-trips and stays small (needs ./roms and xdelta3; ~40 s)."""
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src", "vpsingle"))
import builder  # noqa: E402

D1 = os.path.join(ROOT, "roms", "us", "Valkyrie Profile (USA) (Disc 1)", "Valkyrie Profile (USA) (Disc 1).bin")
D2 = os.path.join(ROOT, "roms", "us", "Valkyrie Profile (USA) (Disc 2)", "Valkyrie Profile (USA) (Disc 2).bin")
OUTDIR = os.path.join(ROOT, "build", "test_xdelta")


@unittest.skipUnless(os.path.exists(D1) and os.path.exists(D2) and shutil.which("xdelta3"),
                     "ROMs or xdelta3 not present")
class TestXdelta(unittest.TestCase):
    def test_roundtrip(self):
        os.makedirs(OUTDIR, exist_ok=True)
        try:
            img = os.path.join(OUTDIR, "single.bin")
            patch = os.path.join(OUTDIR, "single.xdelta")
            builder.build(D1, D2, img)
            r = subprocess.run([os.path.join(ROOT, "tools", "make_xdelta.sh"), img, patch, D1, D2],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)        # the script decodes and compares SHA-1s
            self.assertIn("(verified)", r.stdout)
            self.assertLess(os.path.getsize(patch), 1 << 20)  # no game data inside
        finally:
            shutil.rmtree(OUTDIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
