"""Phase 3 tests: sound objects, packs, and the US->JP voice plan.

    PYTHONPATH=src python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_archive import load_archive  # noqa: E402
from test_disc import need  # noqa: E402
from vpundub.archive import slz  # noqa: E402
from vpundub.archive.archive import Archive  # noqa: E402
from vpundub.assets.voice import (build_pack, find_sounds, pad_pack_item,  # noqa: E402
                                  parse_pack, parse_sound, sound_bytes)
from vpundub.mapping.voicemap import apply_plan, build_plan, jp_object, jp_whole_clip  # noqa: E402


def make_clip(line: int, group: int, sample: int, adpcm: bytes) -> bytes:
    """Build a single voice clip exactly like the ones on disc."""
    script = struct.pack("<I", 0x80000004) + struct.pack("<I", 0x14)
    script += struct.pack("<HBB", line, group, 0x40)
    script += bytes([0x83, 0x00]) + struct.pack("<H", sample)
    script += bytes([0x3C, 0x1C, 0x1B, 0x7F, 0x80, 0, 0, 0])
    H = 4 + len(script)
    table = struct.pack("<HBBI", 0x2C, 0x14, 0, 0)
    table += struct.pack("<HHHBB", sample, 0x053F, 0x5FC3, 0, 1)
    table += struct.pack("<IIIHHI", 0x7F007F00, 0, len(adpcm), 0xEE7F, 0, 0x4000)
    table += b"\xff\xff\xff\xff\x00\x00\x00\x00"
    data = table + adpcm
    return struct.pack("<I", H) + script + struct.pack("<I", len(data)) + data


class TestSoundObject(unittest.TestCase):
    def test_clip_parse(self):
        adpcm = bytes(range(16)) * 20
        c = make_clip(0x16, 1, 0x111B, adpcm)
        so = parse_sound(c)
        self.assertIsNotNone(so)
        self.assertEqual(so.script_kind, "clip")
        self.assertEqual((so.line, so.group), (0x16, 1))
        self.assertEqual(so.sample_ids, (0x111B,))
        self.assertEqual(so.length, len(c))
        self.assertEqual(so.adpcm_size, len(adpcm))

    def test_scriptless_bank(self):
        # Resident bank layout (slot 4): size field directly followed by the table.
        clip = make_clip(1, 1, 0x40, bytes(96))
        so = parse_sound(clip)
        bank = clip[so.script_end:]
        b = parse_sound(bank)
        self.assertIsNotNone(b)
        self.assertEqual((b.script_kind, b.script_end, b.sample_ids), ("bank", 0, (0x40,)))

    def test_reject_garbage(self):
        self.assertIsNone(parse_sound(bytes(256)))
        c = bytearray(make_clip(1, 1, 2, bytes(64)))
        c[-len(bytes(64)) - 8] ^= 1  # break the terminator
        self.assertIsNone(parse_sound(bytes(c)))

    def test_pack_roundtrip_and_find(self):
        c1 = make_clip(1, 1, 0x10, bytes(160))
        so1 = parse_sound(c1)
        raw = pad_pack_item(c1, so1)
        self.assertEqual((len(raw) - so1.script_end - so1.table_size) % 128, 0)
        c2 = make_clip(2, 1, 0x11, bytes(320))
        comp = slz.compress(c2, 2)
        code = b"\xe8\xff\xbd\x27" * 8
        tmpl = struct.pack("<II", 3, 0) + struct.pack("<IIIIII", 0x270F, 1, 0x701800, 1, 0x12, 1)
        pack = build_pack(tmpl + b"\0\0\0", [raw, comp, code])
        items = parse_pack(pack)
        self.assertEqual([i.tag for i in items], [0x270F, 0x701800, 0x12])
        refs = find_sounds(7, pack)
        self.assertEqual([(r.item, r.compressed) for r in refs], [(0, False), (1, True)])
        self.assertEqual(sound_bytes(pack, refs[0]), c1)
        self.assertEqual(sound_bytes(pack, refs[1]), c2)


class TestVoicePlanRom(unittest.TestCase):
    @need("us1")
    @need("jp1")
    def test_disc1_plan_and_apply(self):
        _, us = load_archive("us1")
        _, jp = load_archive("jp1")
        plan = build_plan(us, jp)
        s = plan.to_json()["summary"]
        # Regression values established in Phase 3.
        self.assertEqual(s["replacements"], 1976)
        self.assertEqual(s["standalone"], 1859)
        self.assertEqual(s["pack_items"], 117)
        self.assertEqual(s["sequence_packs"], 277)
        self.assertEqual(s["identical_to_jp"], 182)
        self.assertEqual(s["unmatched"], 10)
        # Sequence packs keep the US number of sync points; slot 4795 (ending pack)
        # passes the size guard only through SIZE_GUARD_OK, at exactly 231 sectors.
        for sp in plan.sequence_packs:
            self.assertEqual(sp.us_tags.count(0x270F), sp.jp_tags.count(0x270F))
        self.assertEqual([(r.item, r.jp_slot, r.jp_item) for r in plan.replacements if r.slot == 4795],
                         [(227, 4912, 275)])
        self.assertFalse(any(x.get("slot") == 4795 for x in plan.review))

        new = apply_plan(us, jp, plan)
        blob = us.build(new)
        self.assertEqual(len(blob), us.size_sectors * 2048)   # tail absorbs growth
        arc = Archive.from_bytes(blob)
        self.assertEqual(arc.order, us.order)
        self.assertEqual(-(-len(arc[4795]) // 2048), 231)
        for r in plan.replacements:
            refs = [x for x in find_sounds(r.slot, arc[r.slot]) if x.item == r.item]
            self.assertEqual(len(refs), 1, (r.slot, r.item))
            # the JP object, resampled for the FIT_CLIPS entries (slot 1630, both discs)
            self.assertEqual(sound_bytes(arc[r.slot], refs[0]), jp_whole_clip(jp, r)[0])
        # mugi pitfalls (Phase 4) as invariants:
        #  - a slot never changes kind (mugi turned scene packs 1021/1099 into clips),
        #  - a replacement carries the same line/group/samples (mugi 852/853/1531/1532/2050),
        #  - no SLZ/STR (code, video) entry is touched.
        for r in plan.replacements:
            self.assertFalse(slz.is_slz(us[r.slot]), r.slot)
            self.assertNotEqual(us[r.slot][:4], b"\x60\x01\x01\x80", r.slot)
            a = [x for x in find_sounds(r.slot, us[r.slot]) if x.item == r.item][0].obj
            b = [x for x in find_sounds(r.slot, arc[r.slot]) if x.item == r.item][0].obj
            self.assertEqual((a.sample_ids, a.line, a.group, a.script_kind),
                             (b.sample_ids, b.line, b.group, b.script_kind))
        seq = {sp.slot: sp for sp in plan.sequence_packs}
        for slot, sp in seq.items():
            self.assertEqual(arc[slot].rstrip(b"\0"), jp[sp.jp_slot].rstrip(b"\0"))
            self.assertEqual(len(find_sounds(slot, arc[slot])), len(parse_pack(arc[slot])))
        for slot in new:
            self.assertEqual(parse_sound(us[slot]) is None, parse_sound(arc[slot]) is None, slot)
            self.assertEqual(parse_pack(us[slot]) is None, parse_pack(arc[slot]) is None, slot)
            if parse_pack(us[slot]) and slot not in seq:
                self.assertEqual([i.tag for i in parse_pack(us[slot])],
                                 [i.tag for i in parse_pack(arc[slot])], slot)
        # Untouched slots are byte-identical; in packs only voice items change.
        for slot in us.order:
            if slot not in new:
                self.assertEqual(arc[slot], us[slot])
                continue
            items = parse_pack(us[slot])
            if not items or slot in seq:
                continue
            voice = {x.item for x in find_sounds(slot, us[slot])}
            for i, (a, b) in enumerate(zip(items, parse_pack(arc[slot]))):
                if i not in voice:
                    self.assertEqual(us[slot][a.offset:a.offset + a.size],
                                     arc[slot][b.offset:b.offset + b.size], (slot, i))


    @need("us2")
    @need("jp2")
    def test_disc2_slot1536_full_jp(self):
        """Slot 1536 now plays the full JP line (scene retimed by scenefix, Phase 6)."""
        _, us = load_archive("us2")
        _, jp = load_archive("jp2")
        plan = build_plan(us, jp)
        r = next(r for r in plan.replacements if r.slot == 1536)
        self.assertEqual(apply_plan(us, jp, plan)[1536].rstrip(b"\0"),
                         jp_object(jp, r).rstrip(b"\0"))


if __name__ == "__main__":
    unittest.main()
