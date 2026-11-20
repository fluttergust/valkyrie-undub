"""Spiritual Concentration subtitles (src/vpundub/subtitles, ported from ../vpsub with its tests).

    PYTHONPATH=src python3 -m unittest tests.test_subtitles -v

Synthetic fonts/modules need no disc; the disc tests skip without the US ROMs, the overlay
recompile test without the LLVM toolchain.
"""
from __future__ import annotations

import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_disc import need  # noqa: E402
from vpundub.archive import slz  # noqa: E402
from vpundub.subtitles import cues, fieldtext, patch, visions  # noqa: E402
from vpundub.subtitles import pages as P  # noqa: E402
from vpundub.subtitles.font import Font  # noqa: E402


def synth(n=101):
    widths = bytes((3 + i % 5) for i in range(n))
    glyphs = b""
    for i in range(n):
        rows = [0] * 12
        rows[5] = 0x8000          # one pixel at column 0, row 5
        glyphs += struct.pack("<12H", *rows)
    return struct.pack("<II", n, 12) + widths + glyphs


def mono_font(w=6, n=109):
    return Font(struct.pack("<II", n, 12) + bytes([w] * n) + b"\0" * 24 * n)


class TestFont(unittest.TestCase):
    def test_parse_and_layout(self):
        f = Font(synth())
        self.assertEqual(len(f.glyphs), 101)
        ia, ib = ord("a") - 0x20, ord("b") - 0x20
        self.assertEqual(f.layout("ab"), [(ia, 0), (ib, f.widths[ia])])
        self.assertEqual(f.width("ab"), f.widths[ia] + f.widths[ib])
        self.assertEqual(f.index("・"), 100)

    def test_bad_layout_rejected(self):
        with self.assertRaises(ValueError):
            Font(synth()[:-1])

    def test_raster_outline(self):
        img = Font(synth()).raster("!")
        self.assertEqual(img[6][1], 2)                                   # glyph pixel (row 5 -> +1 border)
        self.assertTrue(img[5][1] == img[7][1] == img[6][2] == img[6][0] == 1)   # 4-neighbour outline
        self.assertEqual(img[5][0], 0)                                   # no diagonal

    def test_unknown_char(self):
        with self.assertRaises(KeyError):
            Font(synth()).index("é")

    def test_touch_gap(self):
        """'I' fills its 3 px advance; a '.' right after it would join its serif, so it gets 1 px more."""
        n = 101
        widths, rows = bytearray([4] * n), [[0] * 12 for _ in range(n)]
        ii, dot, i_l = ord("I") - 0x20, ord(".") - 0x20, ord("l") - 0x20
        widths[ii], rows[ii][9] = 3, 0xE000
        widths[dot], rows[dot][9] = 2, 0x8000
        widths[i_l], rows[i_l][2] = 2, 0x8000
        font = Font(struct.pack("<II", n, 12) + bytes(widths) + b"".join(struct.pack("<12H", *r) for r in rows))
        self.assertTrue(font.touches(ii, dot) and not font.touches(dot, dot) and not font.touches(ii, i_l))
        self.assertEqual(font.layout("I.."), [(ii, 0), (dot, 4), (dot, 6)])
        self.assertEqual((font.width("I.."), font.width("Il")), (8, 5))


class TestPages(unittest.TestCase):
    def setUp(self):
        self.font = Font(synth())

    def test_layout_centred_bottom(self):
        g = P.layout(self.font, ["ab", "c d"])
        self.assertEqual([x.order for x in g], [0, 1, 2, 4])             # the space takes reveal slot 3
        self.assertEqual({x.y for x in g}, {P.BOTTOM - 1 - P.PITCH, P.BOTTOM - 1})
        self.assertEqual(g[0].x, (P.SCREEN_W - self.font.width("ab")) // 2 - 1)
        self.assertEqual(g[0].w, self.font.widths[g[0].cell] + 4)

    def test_clip_pages_timing(self):
        cs = [{"start": 0.0, "end": 2.0, "lines": ["ab"]}, {"start": 4.0, "end": 9.0, "lines": ["cd"]}]
        a, b = P.clip_pages(self.font, cs, limit=round(9.5 * P.FPS))
        self.assertEqual((a.start, a.end), (0, round(2.0 * P.FPS)))
        self.assertLessEqual(a.stop, b.start)                           # one page at a time
        self.assertTrue(a.fade == P.FADE_OUT and a.stop == a.end + 64)
        self.assertTrue(b.end == round(9.0 * P.FPS) and b.stop <= round(9.5 * P.FPS))
        self.assertGreater(b.fade, P.FADE_OUT)                          # squeezed to be clear by the limit
        self.assertLessEqual(P.modulation(b, b.glyphs[0], b.stop - 1), b.fade)

    def test_modulation_reveal_and_fade(self):
        p = P.clip_pages(self.font, [{"start": 1.0, "end": 3.0, "lines": ["abc"]}])[0]
        g0, g1 = p.glyphs[:2]
        self.assertEqual(P.modulation(p, g0, p.start - 1), 0)
        self.assertEqual(P.modulation(p, g0, p.start), P.FADE_IN)
        self.assertEqual(P.modulation(p, g1, p.start + p.speed), P.FADE_IN)
        self.assertEqual(P.modulation(p, g0, p.start + 100), P.FULL)
        self.assertEqual(P.modulation(p, g0, p.end), P.FULL - p.fade)
        self.assertEqual(P.modulation(p, g0, p.stop), 0)
        self.assertIsNone(P.active([p], p.stop))
        self.assertIs(P.active([p], p.start), p)

    def test_speed_for(self):
        self.assertEqual(P.speed_for(10, 600), P.CHAR_FRAMES)
        self.assertEqual(P.speed_for(100, 100), 1)

    def test_cell_words_nibbles(self):
        bm = [[0] * P.CELL_W for _ in range(P.CELL_H)]
        bm[0][0], bm[0][1] = 1, 2
        w = P.cell_words(bm)
        self.assertEqual((len(w), w[0]), (P.CELL_H * P.CELL_W // 2, 0x21))

    def test_pack_layout(self):
        pg = P.clip_pages(self.font, [{"start": 0.5, "end": 2.0, "lines": ["ab", "ba"]}])
        blob = P.pack(self.font, {3: [None, pg]})
        vis = struct.unpack_from(f"<{P.NVIS}I", blob)
        cells_off, ncells = struct.unpack_from("<II", blob, 4 * P.NVIS)
        self.assertEqual((ncells, len(blob)), (2, cells_off + ncells * P.CELL_H * 8))
        self.assertEqual([i for i, o in enumerate(vis) if o], [3])
        nclips, c0, c1 = struct.unpack_from("<3I", blob, vis[3])
        self.assertTrue(nclips == 2 and c0 == 0 and c1 % 4 == 0)
        p = pg[0]
        self.assertEqual(struct.unpack_from("<IHHHBBBBH", blob, c1),
                         (1, p.start, p.end, p.stop, p.speed, p.fade, 4, 2, 0))
        g = p.glyphs[0]
        self.assertEqual(struct.unpack_from("<HBBBB", blob, c1 + 4 + 12 + 2), (g.x, g.y, 0, g.order, g.w))

    def test_pack_rejects_too_many_cells(self):
        p = P.Page(0, 10, 20, 1, 2, [P.Glyph(0, 0, i % 101, i, 8) for i in range(101)])
        p.glyphs += [P.Glyph(0, 0, 0, 0, 8)] * 300
        with self.assertRaises(ValueError):
            P.pack(self.font, {1: [[p]]})

    def test_page_keeps_a_fade_before_limit(self):
        p = P.clip_pages(self.font, [{"start": 0.0, "end": 3.0, "lines": ["ab"]}], limit=round(3.0 * P.FPS))[0]
        self.assertTrue(p.stop <= round(3.0 * P.FPS) and p.stop - p.end >= P.MIN_FADE and p.fade <= 8)

    def test_game_subtitled_vision_left_out(self):
        vis = {v: {"clips": [{"slot": 1, "seconds": 3.0}]} for v in (3, 26)}
        subs = {v: {"clips": [{"slot": 1, "cues": [{"start": 0.0, "end": 1.0, "lines": ["ab"]}]}]} for v in (3, 26)}
        self.assertEqual(set(P.build_visions(self.font, subs, vis)), {3})

    def test_merge_discs_takes_complete_disc(self):
        """Vision 16 style: partial on disc 1, complete on disc 2 -> both discs use disc 2's data."""
        d1 = ({16: _vis(16, [True, False]), 2: _vis(2, [True, True])},
              {16: {"id": 16, "clips": [{"slot": 100, "cues": []}]}, 2: {"id": 2, "clips": ["d1"]}})
        d2 = ({16: _vis(16, [True, True]), 2: _vis(2, [True, False])},
              {16: {"id": 16, "clips": ["full"]}, 2: {"id": 2, "clips": ["partial"]}})
        vis, subs = P.merge_discs([d1, d2])
        self.assertTrue(vis[16] == d2[0][16] and subs[16] == d2[1][16])
        self.assertTrue(vis[2] == d1[0][2] and subs[2] == d1[1][2])
        self.assertEqual(P.merge_discs([d2, d1]), (vis, subs))   # same result whichever disc is built

    def test_merge_discs_rejects_conflicts(self):
        a = ({1: _vis(1, [True])}, {1: {"id": 1, "clips": ["a"]}})
        b = ({1: _vis(1, [True])}, {1: {"id": 1, "clips": ["b"]}})
        with self.assertRaises(ValueError):
            P.merge_discs([a, b])
        with self.assertRaises(ValueError):
            P.merge_discs([({1: _vis(1, [False])}, {})])

    def test_short_page_min_show(self):
        p = P.clip_pages(self.font, [{"start": 0.0, "end": 0.5, "lines": ["ab"]}])[0]
        self.assertEqual(p.end, P.MIN_SHOW)
        q = P.clip_pages(self.font, [{"start": 0.0, "end": 0.5, "lines": ["ab"]}], limit=60)[0]
        self.assertTrue(q.stop <= 60 and q.stop - q.end >= P.MIN_FADE)  # no room: still clear by the limit

    def test_end_limits(self):
        lim = P.end_limits([{"seconds": 2.0, "delay": 0}, {"seconds": 1.0, "delay": 5},
                            {"seconds": 3.0, "delay": 4}])
        self.assertEqual(lim[2], round(3.0 * P.FPS) + P.AFTER_END)
        self.assertEqual(lim[1], round(1.0 * P.FPS) + round(3.0 * P.FPS) + 8 + P.AFTER_END)
        self.assertEqual(lim[0], 120 + 60 + 180 + 10 + 8 + P.AFTER_END)
        self.assertIsNone(P.end_limits([{"seconds": None}, {"seconds": 1.0}])[0])

    def test_last_page_full_fade_after_vision_end(self):
        vis = {3: {"clips": [{"slot": 1, "seconds": 3.0}]}}
        subs = {3: {"clips": [{"slot": 1, "cues": [{"start": 0.0, "end": 3.0, "lines": ["ab"]}]}]}}
        p = P.build_visions(self.font, subs, vis)[3][0][-1]
        self.assertTrue(p.fade == P.FADE_OUT and p.end == round(3.0 * P.FPS) and p.stop > round(3.0 * P.FPS))


def _vis(vid, present):
    return {"id": vid, "on_disc": all(present),
            "clips": [{"slot": 100 + k, "delay": 0, "seconds": 1.0 if p else None, "present": p}
                      for k, p in enumerate(present)]}


def _page(start, end, stop):
    return P.Page(start, end, stop, 1, 2, [P.Glyph(0, 0, 1, 0, 8)])


class TestRuntime(unittest.TestCase):
    """pages.Runtime: the Python model of overlay.c's page clock."""

    def test_carries_into_clip_without_subtitles(self):
        a = _page(0, 100, 164)
        rt = P.Runtime([[a], None])
        self.assertIs(rt.step(1000, 0)[0], a)
        self.assertEqual(rt.step(1050)[:2], (a, 50))
        self.assertEqual(rt.step(1120, 1), (a, 120, P.FULL))           # next clip has no page: fades as planned
        self.assertEqual(rt.step(1163)[:2], (a, 163))
        self.assertIsNone(rt.step(1164))

    def test_cuts_carry_for_new_page(self):
        a, b = _page(0, 100, 164), _page(0, 50, 114)
        rt = P.Runtime([[a], [b]])
        rt.step(1000, 0)
        pg, t, cap = rt.step(1110, 1)                                   # b wants the screen: a fades fast
        self.assertTrue(pg is a and cap == P.FULL - P.CUT_FADE)
        n = P.FULL // P.CUT_FADE - 1
        self.assertEqual(rt.step(1110 + n - 1)[:3:2], (a, P.CUT_FADE))
        self.assertEqual(rt.step(1110 + n)[:2], (b, n))                 # then b, on its own clock

    def test_page_outlives_vision_end(self):
        a = _page(0, 100, 164)
        rt = P.Runtime([[a]])
        rt.step(1000, 0)
        self.assertEqual(rt.step(1110, ended=True), (a, 110, P.FULL))
        self.assertEqual(rt.step(1163, ended=True)[:2], (a, 163))
        self.assertIsNone(rt.step(1164, ended=True))
        self.assertIsNone(rt.step(1200, ended=True))


class TestCues(unittest.TestCase):
    def test_to_font_maps_and_flags(self):
        f = mono_font()
        self.assertEqual(cues.to_font("“special”  place…", f), ('"special" place...', set()))
        self.assertEqual(cues.to_font("é!", f)[1], {"é"})

    def test_wrap_balance_pages(self):
        f = mono_font()                                                 # 6 px per char
        text = "I am he who hath entrusted his soul to the eternal vortex of time."
        lines = cues.pages(text, f)[0]
        self.assertEqual(len(lines), 2)
        self.assertEqual(" ".join(lines), text)
        self.assertLess(abs(len(lines[0]) - len(lines[1])), 12)        # balanced
        long = " ".join(["One sentence here."] * 8)
        ps = cues.pages(long, f)
        self.assertTrue(all(len(p) <= 2 for p in ps) and len(ps) > 1)
        self.assertEqual(" ".join(" ".join(p) for p in ps), long)
        self.assertTrue(all(f.width(s) <= cues.MAX_W for p in ps for s in p))

    def test_split_times_snaps_to_pauses(self):
        self.assertEqual(cues.split_times(0.0, 10.0, [1, 1], []), [(0.0, 5.0), (5.0, 10.0)])
        self.assertEqual(cues.split_times(0.0, 10.0, [1, 1], [4.4, 8.0]), [(0.0, 4.4), (4.4, 10.0)])
        self.assertEqual(cues.split_times(0.0, 10.0, [1, 1], [2.0]), [(0.0, 5.0), (5.0, 10.0)])


def synth_field_font(n=109):
    """Every glyph distinct: row 3 holds the index as a bit pattern."""
    glyphs = b""
    for i in range(n):
        rows = [0] * 12
        rows[3] = (i + 1) << 4
        rows[5] = 0xF000
        glyphs += struct.pack("<12H", *rows)
    return Font(struct.pack("<II", n, 12) + bytes([6] * n) + glyphs)


def rot(g):
    return g[22:] + g[:22]


class TestFieldText(unittest.TestCase):
    def test_messages(self):
        font = synth_field_font()
        chars = "LucianwhtsByeVlH "
        names = {c: i for i, c in enumerate(chars, start=10)}

        def enc(s):
            return bytes(names[c] + 0x25 for c in s)
        text = (b"\x93\x80\x01\x00" + enc("Lucian") + b"\x80\x80" + enc("what is") + b"\x83\x80\x0c"
                + enc(" this") + b"\x0d" + b"\x8c\x80\x01\x0b" + enc("Bye") + b"\x00"
                + b"\x93\x80\x02\x00" + enc("Val") + b"\x80\x80" + enc("Hi") + b"\x8c\x80\x02\x0b")
        glyphs = fieldtext.SIG + b"\0" * 24 * 8 + b"".join(rot(font.glyphs[font.index(c)]) for c in chars)
        ms = fieldtext.messages(text + b"\0" * 8 + glyphs, fieldtext.GlyphNamer(font))
        self.assertEqual([(m["speaker"], m["text"]) for m in ms], [("Lucian", "what is this! Bye"), ("Val", "Hi")])

    def test_message_ends_and_headerless_speaker(self):
        font = synth_field_font()
        chars = "ValkyrieHiSoOhNtx!. "
        names = {c: i for i, c in enumerate(chars, start=10)}

        def enc(t):
            return bytes(names[c] + 0x25 for c in t)
        text = (b"\x93\x80\x01\x00" + enc("Val") + b"\x80\x80" + enc("Hi.") + b"\x00\x85\x80" + enc("So")
                + b"\x00" + enc("Valkyrie") + b"\x80\x80" + enc("Oh") + b"\x8c\x80\x01\x0b"
                + b"\x93\x80\x02\x00" + enc("Val") + b"\x80\x80" + enc("Hi") + b"\x00" + enc("Next text."))
        glyphs = fieldtext.SIG + b"\0" * 24 * 8 + b"".join(rot(font.glyphs[font.index(c)]) for c in chars)
        ms = fieldtext.messages(text + b"\0" * 8 + glyphs, fieldtext.GlyphNamer(font))
        self.assertEqual([(m["speaker"], m["text"]) for m in ms],
                         [("Val", "Hi. So"), ("Valkyrie", "Oh"), ("Val", "Hi")])

    def test_namer_near_match_and_digits(self):
        font = synth_field_font()
        nm = fieldtext.GlyphNamer(font)
        g = bytearray(rot(font.glyphs[font.index("a")]))
        g[0] ^= 1                                                       # one stray bit
        self.assertEqual(nm.name(20, bytes(g)), "a")
        self.assertEqual(nm.name(3, b"\0" * 24), "3")

    def test_cut(self):
        msg = "I'm going to send you one of these earrings and I want you to take it."
        self.assertEqual(fieldtext.cut(msg, (0, 9)), "I'm going to send you one of these earrings")
        self.assertEqual(fieldtext.cut("I..., I...you", (0, 3)), "I..., I...")


BASE = 0x80030000


def make_module(counts: list[int], pad: int = 0x40) -> bytes:
    """Padding, then clip lists, then the 26-record index (as in slot 4796)."""
    lists, recs, p = b"", [], BASE + pad
    for i, n in enumerate(counts + [0] * (visions.N_IDS - len(counts))):
        if n:
            recs.append((n, p))
            lists += b"".join(struct.pack("<HBB", 2000 + 10 * i + k, 5, 0x40) for k in range(n))
            p += 4 * n
        else:
            recs.append((0, 0))
    return b"\xff" * pad + lists + b"".join(struct.pack("<II", n, q) for n, q in recs) + b"\0" * 32


def clip(script: bytes) -> bytes:
    return struct.pack("<IIII", 0x10 + len(script), 0x80000004, 0, 0) + script


class TestVisions(unittest.TestCase):
    def test_find_index_locates_table_and_base(self):
        self.assertEqual(visions.find_index(make_module([3, 0, 2, 4, 5])), (0x40 + 4 * (3 + 2 + 4 + 5), BASE))

    def test_find_index_rejects_too_few_records(self):
        self.assertIsNone(visions.find_index(make_module([3, 2])))

    def test_clip_frames_sums_notes_and_repeats(self):
        s = b"\x83\0\0\0" + b"\x3c\x0a\x7f\0" + b"\x8d" + b"\x3c\x05\x7f\0" + b"\x8e\x03" + b"\x80"
        self.assertEqual(visions.clip_frames(clip(s)), 10 + 5 * 3)

    def test_clip_frames_unknown_or_not_a_clip(self):
        self.assertIsNone(visions.clip_frames(clip(b"\x42\x80")))
        self.assertIsNone(visions.clip_frames(b"\0" * 32))


class TestPatch(unittest.TestCase):
    def test_hazard_check(self):
        lw_v0, use_v0, nop = 0x8C820000, 0x00401821, 0                  # lw v0,0(a0); addu v1,v0,zero
        patch.check_hazards(struct.pack("<3I", lw_v0, nop, use_v0), 0, 12)
        with self.assertRaises(patch.SubtitleError):
            patch.check_hazards(struct.pack("<2I", lw_v0, use_v0), 0, 8)
        patch.check_hazards(struct.pack("<2I", 0x88820003, 0x98820000), 0, 8)   # lwl/lwr pair allowed

    def test_patch_module_and_inject(self):
        mod = bytearray(patch.MODULE_LEN)
        off = patch.HOOK_SITE - patch.MODULE_BASE
        with self.assertRaises(patch.SubtitleError):
            patch.patch_module(bytes(mod), b"")                         # hook site is not the vision step call
        struct.pack_into("<I", mod, off, patch.jal(patch.STEP))
        out = patch.patch_module(bytes(mod), b"\1\2\3\4")
        self.assertEqual(struct.unpack_from("<I", out, off)[0], 0x0C000000 | patch.HOOK >> 2 & 0x03FFFFFF)
        self.assertEqual(out[patch.MODULE_LEN:], b"\1\2\3\4")
        with self.assertRaises(patch.SubtitleError):
            patch.patch_module(bytes(mod), bytes(patch.SAVE_BUF - patch.OVERLAY_BASE + 1))
        rec = patch.inject_records(out)
        self.assertEqual(rec, struct.pack("<II", patch.OVERLAY_BASE, 4) + b"\1\2\3\4"
                         + struct.pack("<II", patch.HOOK_SITE, 4) + out[off:off + 4])
        self.assertEqual(patch.inject_records(bytes(mod)), b"")

    def test_overlay_bin_layout(self):
        code = patch.overlay_code()
        self.assertEqual(len(code) % 4, 0)
        self.assertLess(patch.HOOK - patch.OVERLAY_BASE, len(code))
        self.assertEqual(patch.jal(0x80033FE8), 0x0C00CFFA)            # the hook-site word retime.lua checks

    @unittest.skipUnless(all(shutil.which(t) for t in ("clang", "ld.lld", "llvm-objcopy", "llvm-nm", "llvm-objdump")),
                         "LLVM toolchain not installed")
    def test_overlay_bin_is_overlay_c(self):
        with tempfile.TemporaryDirectory() as t:
            code, syms = patch.compile_overlay(Path(t))
        self.assertEqual(syms["hook"], patch.HOOK)
        self.assertEqual(code, patch.overlay_code(), "overlay.bin is stale: run tools/subtitle_overlay.py")


def _us(disc):
    from vpundub.build import DEFAULT_CUE, _archive, _open
    return _archive(_open(ROOT / DEFAULT_CUE[("us", disc)]))[0]


class TestDisc(unittest.TestCase):
    @need("us1")
    def test_disc1_vision_inventory(self):
        r = visions.scan(_us(1), patch.MODULE_SLOT)
        self.assertEqual(r["load_base"], patch.MODULE_BASE)
        self.assertEqual([v["id"] for v in r["visions"] if v["on_disc"]], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14, 15])
        v2 = next(v for v in r["visions"] if v["id"] == 2)
        self.assertEqual([c["slot"] for c in v2["clips"]], list(range(2486, 2492)))

    def _resolves(self, disc):
        us = _us(disc)
        res, problems = cues.resolve(patch.load_cues(disc), us)
        self.assertEqual(problems, [])
        self.assertGreater(sum(len(c["cues"]) for v in res["visions"] for c in v["clips"]), 60)

    @need("us1")
    def test_disc1_cues_resolve(self):
        self._resolves(1)

    @need("us2")
    def test_disc2_cues_resolve(self):
        self._resolves(2)

    @need("us1")
    @need("us2")
    @need("jp1")
    @need("jp2")
    def test_both_discs_same_subtitles(self):
        """Each vision comes from the disc holding all its clips: both discs get the same pages, and
        vision 16 (only 1 of its 7 lines on disc 1 alone) is complete on disc 1 too."""
        from vpundub.build import _other_disc
        d1, d2 = _other_disc(2, False, print), _other_disc(1, False, print)
        p1, _ = patch.subtitle_pages(1, d1[1], d1[2], [d2])
        p2, _ = patch.subtitle_pages(2, d2[1], d2[2], [d1])
        font = Font(patch.find_font(d1[1]))
        self.assertEqual(P.pack(font, p1), P.pack(font, p2))
        self.assertEqual(sum(1 for c in p1[16] if c), 7)      # 9 clips, 7 with a line
        single, _ = patch.subtitle_pages(1, d1[1], d1[2])
        self.assertEqual(sum(1 for c in single.get(16, []) if c), 1)

    @need("us1")
    def test_module_slot_and_verify(self):
        """module_slot on the US archive (US clip timing): only the hook word changes, the overlay is
        appended, and build.verify_subtitle_slot accepts it but rejects any other change."""
        from vpundub.build import BuildError, verify_subtitle_slot
        us = _us(1)
        comp, info = patch.module_slot(1, us, us)
        self.assertGreater(info["pages"], 50)
        verify_subtitle_slot(us[patch.MODULE_SLOT], comp)
        mod = bytearray(slz.decompress(comp))
        mod[0x100] ^= 1
        with self.assertRaises(BuildError):
            verify_subtitle_slot(us[patch.MODULE_SLOT], slz.compress(bytes(mod), 1))
        with self.assertRaises(BuildError):
            verify_subtitle_slot(us[patch.MODULE_SLOT], us[patch.MODULE_SLOT])   # not patched at all


if __name__ == "__main__":
    unittest.main()
