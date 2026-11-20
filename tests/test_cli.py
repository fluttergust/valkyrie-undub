"""CLI option wiring (no ROMs needed)."""
import unittest
from unittest import mock

from vpundub import __main__ as cli


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        with mock.patch.object(cli, "build_disc") as b:
            self.assertEqual(cli.main(["build", "--disc", "1", *args]), 0)
        return b.call_args.kwargs

    def test_code_patches_on_by_default(self):
        self.assertTrue(self.run_cli()["code_patches"])

    def test_no_code_patches(self):
        self.assertFalse(self.run_cli("--no-code-patches")["code_patches"])

    def test_subtitles_on_by_default(self):
        self.assertTrue(self.run_cli()["subtitles"])
        self.assertFalse(self.run_cli("--no-subtitles")["subtitles"])


if __name__ == "__main__":
    unittest.main()
