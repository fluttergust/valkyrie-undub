"""Scene timing patches (src/vpundub/mapping/scenefix.py)."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_archive import load_archive  # noqa: E402
from test_disc import need  # noqa: E402
from vpundub.archive import slz  # noqa: E402
from vpundub.assets.voice import parse_pack  # noqa: E402
from vpundub.mapping.scenefix import LINE_HOLDS, SCENE_WAITS, LineHold, SceneWait, apply_scene_waits, patch_script  # noqa: E402


def rec(*w):
    return struct.pack("<5H", *w)


class TestPatchScript(unittest.TestCase):
    def test_patch(self):
        data = rec(7, 0x600, 0, 0, 0) + rec(2, 0x8C5, 40, 1, 0) + rec(2, 0x8C6, 220, 1, 0) + rec(0x63, 0, 0, 0, 0)
        out = patch_script(data, [SceneWait(0, 0, 0x8C6, 220, 340, "")])
        self.assertEqual(out, data.replace(rec(2, 0x8C6, 220, 1, 0), rec(2, 0x8C6, 340, 1, 0)))

    def test_unexpected_input_fails(self):
        data = rec(2, 0x8C6, 221, 1, 0)
        with self.assertRaises(ValueError):
            patch_script(data, [SceneWait(0, 0, 0x8C6, 220, 340, "")])
        with self.assertRaises(ValueError):
            patch_script(rec(2, 0x8C6, 220, 1, 0) * 2, [SceneWait(0, 0, 0x8C6, 220, 340, "")])

    def test_line_hold(self):
        data = struct.pack("<6H", 300, 411, 2094, 400, 414, 2094) + struct.pack("<3H", 634, 9999, 9999)
        out = patch_script(data, [LineHold(0, 0, 411, 2094, 300, 814)])
        self.assertEqual(out[:6], struct.pack("<3H", 814, 411, 2094))
        self.assertEqual(out[6:], data[6:])
        with self.assertRaises(ValueError):
            patch_script(data, [LineHold(0, 0, 411, 2094, 301, 814)])


def item0(entry):
    it = parse_pack(entry)[0]
    return slz.decompress(entry[it.offset:it.offset + it.size])


class TestDiscScenes(unittest.TestCase):
    def _check(self, key, jp_key, jp_slot):
        _, us = load_archive(key)
        _, jp = load_archive(jp_key)
        new = apply_scene_waits(us, {})
        self.assertEqual(set(new), {w.slot for w in SCENE_WAITS + LINE_HOLDS})
        u, n, j = item0(us[1988]), item0(new[1988]), item0(jp[jp_slot])
        self.assertEqual(len(u), len(n))
        for w in SCENE_WAITS:
            self.assertIn(rec(2, w.msg, w.us_wait, 1, 0), u)
            self.assertIn(rec(2, w.msg, w.new_wait, 1, 0), n)
            self.assertIn(rec(2, w.msg, w.new_wait, 1, 0), j)   # the JP release uses these waits
        # only the wait words differ
        diff = [i for i in range(len(u)) if u[i] != n[i]]
        self.assertTrue(diff and len(diff) <= 2 * len(SCENE_WAITS))
        # other pack items untouched
        a, b = parse_pack(us[1988]), parse_pack(new[1988])
        for x, y in list(zip(a, b))[1:]:
            self.assertEqual(us[1988][x.offset:x.offset + x.size], new[1988][y.offset:y.offset + y.size])

    def _check_holds(self, key, jp_key):
        _, us = load_archive(key)
        _, jp = load_archive(jp_key)
        new = apply_scene_waits(us, {})
        U, N = parse_pack(us[1810]), parse_pack(new[1810])
        J = parse_pack(jp[1933])
        for item in (0, 1):
            u = slz.decompress(us[1810][U[item].offset:U[item].offset + U[item].size])
            n = slz.decompress(new[1810][N[item].offset:N[item].offset + N[item].size])
            j = slz.decompress(jp[1933][J[item].offset:J[item].offset + J[item].size])
            end = u.index(struct.pack("<2H", 9999, 9999)) - 2
            self.assertEqual(n[end:], u[end:])
            # every record of the table: hold = JP hold, action and US voice slot unchanged
            for o in range(end - 20 * 6, end, 6):
                uh, ua, uv = struct.unpack_from("<3H", u, o)
                nh, na, nv = struct.unpack_from("<3H", n, o)
                jh, ja, jv = struct.unpack_from("<3H", j, o)
                self.assertEqual((na, nv), (ua, uv))
                self.assertEqual((ja, jv), (ua, uv + 123))
                self.assertEqual(nh, jh)
            diff = [i for i in range(len(u)) if u[i] != n[i]]
            self.assertTrue(all(end - 120 <= i < end for i in diff))
            self.assertEqual(len([h for h in LINE_HOLDS if h.item == item]), 17)

    @need("us1")
    @need("jp1")
    def test_disc1(self):
        self._check("us1", "jp1", 2111)
        self._check_holds("us1", "jp1")

    @need("us2")
    @need("jp2")
    def test_disc2_holds(self):
        self._check_holds("us2", "jp2")

    @need("us2")
    @need("jp2")
    def test_disc2(self):
        self._check("us2", "jp2", 2111)


if __name__ == "__main__":
    unittest.main()
