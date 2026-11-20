"""Archive layer tests: TOC crypto/codec, SLZ codec, VALKYRIE.BIN round trip.

    PYTHONPATH=src python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import random
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_disc import ROMS, need  # noqa: E402
from vpundub.archive import slz  # noqa: E402
from vpundub.archive.archive import Archive  # noqa: E402
from vpundub.archive.toc import (MAGIC, TOC_BYTES, Entry, Toc, crypt,  # noqa: E402
                                 lba_to_msf, msf_to_lba)
from vpundub.disc.image import DiscImage  # noqa: E402
from vpundub.disc.iso9660 import parse  # noqa: E402


class TestToc(unittest.TestCase):
    def test_crypt_involution(self):
        rng = random.Random(1)
        blob = bytes(rng.randrange(256) for _ in range(0x6400))
        self.assertEqual(crypt(crypt(blob)), blob)

    def test_known_key_stream(self):
        # The first encrypted word of a zero table is the key itself.
        enc = crypt(bytes(0x6400))
        self.assertEqual(struct.unpack_from("<I", enc, 0)[0], 0x64283921)

    def test_msf(self):
        self.assertEqual(msf_to_lba(0, 4, 13), 163)
        self.assertEqual(lba_to_msf(163), (0, 4, 13))
        for lba in (0, 1, 149, 150, 4499, 300000):
            self.assertEqual(msf_to_lba(*lba_to_msf(lba)), lba)

    def test_encode_decode(self):
        ents = {1: Entry(1, 163, 58), 2: Entry(2, 221, 1), 7: Entry(7, 222, 0x2DF0),
                0x13FF: Entry(0x13FF, 300000, 0xFFFF)}
        raw = Toc(ents).encode()
        self.assertEqual(len(raw), TOC_BYTES)
        dec = crypt(raw[:0x6400])
        self.assertEqual(struct.unpack_from("<I", dec, 0)[0], MAGIC)
        self.assertEqual(Toc.decode(raw).entries, ents)

    def test_size_overflow(self):
        with self.assertRaises(ValueError):
            Toc({1: Entry(1, 163, 0x10000)}).encode()


class TestSlz(unittest.TestCase):
    SAMPLES = [
        b"",
        b"A",
        b"abcabcabcabcabcabcabcabc" * 50,
        bytes(5000),                                   # long run
        b"\x01" * 18 + b"\x02" * 4 + b"\x03" * 300,    # short and long runs
        bytes(random.Random(7).randrange(256) for _ in range(20000)),
        b"".join(struct.pack("<I", 0x27BDFFE8 + i % 7) for i in range(4000)),
    ]

    def test_roundtrip_all_types(self):
        for typ in (0, 1, 2):
            for s in self.SAMPLES:
                c = slz.compress(s, typ)
                self.assertTrue(slz.is_slz(c))
                out, used = slz.decompress_block(c)
                self.assertEqual(out, s, f"type {typ}, len {len(s)}")
                self.assertEqual(used, slz.header(c)[1])

    def test_handcrafted_type2_rle(self):
        # flags: token, token, literal, token... (LSB first)
        # tok1: long run, dist=0x01 -> 0x01+19 = 20 copies of 0xAA
        # tok2: short run, b0=0x55, b1=0xF2 -> (2)+3 = 5 copies of 0x55
        # lit : 0x42
        # tok3: copy dist=6, n=0 -> 3 bytes "UUU"? (out[-6:-3])
        payload = bytes([0b0100, 0x01, 0xF0, 0xAA, 0x55, 0xF2, 0x42, 0x06, 0x00])
        expect = b"\xAA" * 20 + b"\x55" * 5 + b"\x42"
        expect += expect[-6:-3]
        blob = b"SLZ\x02" + struct.pack("<III", len(payload), len(expect), 0) + payload
        self.assertEqual(slz.decompress(blob), expect)

    def test_handcrafted_type1_overlap_and_end(self):
        # literal 'x', then copy dist=1 n=2 -> 5 x's (overlapping), then terminator
        payload = bytes([0b001, ord("x"), 0x01, 0x20, 0x00, 0x00])
        blob = b"SLZ\x01" + struct.pack("<III", len(payload), 6, 0) + payload
        self.assertEqual(slz.decompress(blob), b"x" * 6)

    def test_chain(self):
        a, b = slz.compress(b"first" * 10, 2), slz.compress(b"second" * 10, 1)
        a = a[:12] + struct.pack("<I", len(a)) + a[16:]
        chain = a + b
        self.assertEqual(slz.blocks(chain), [0, len(a)])
        self.assertEqual(slz.decompress(chain, 1), b"second" * 10)
        self.assertEqual(slz.decompress(chain), b"first" * 10 + b"second" * 10)


def load_archive(key: str) -> tuple[bytes, Archive]:
    vol = parse(DiscImage(ROMS[key]))
    e = vol.find("VALKYRIE.BIN")
    blob = vol.image.read_form1(e.lba, e.size)
    return blob, Archive.from_bytes(blob, base_lba=e.lba)


class TestArchiveRom(unittest.TestCase):
    def _rt(self, key):
        blob, arc = load_archive(key)
        self.assertEqual(arc.build(), blob)
        # Every SLZ block decodes and consumes exactly its compressed size.
        for s in arc.order:
            d = arc[s]
            if slz.is_slz(d):
                for off in slz.blocks(d):
                    out, used = slz.decompress_block(d, off)
                    self.assertEqual(used, slz.header(d, off)[1], f"slot {s}")

    @need("us1")
    def test_us1(self): self._rt("us1")

    @need("us2")
    def test_us2(self): self._rt("us2")

    @need("jp1")
    def test_jp1(self): self._rt("jp1")

    @need("jp2")
    def test_jp2(self): self._rt("jp2")

    @need("us1")
    def test_replace_grow_and_shrink(self):
        blob, arc = load_archive("us1")
        grow = b"G" * (arc.entry(10).sectors * 2048 + 5000)   # +3 sectors
        shrink = b"S" * 100                                     # 1 sector
        new = arc.build({10: grow, 9: shrink})
        self.assertEqual(len(new), len(blob))                   # tail absorbs growth
        arc2 = Archive.from_bytes(new)
        self.assertEqual(arc2.order, arc.order)
        self.assertEqual(arc2[10][:len(grow)], grow)
        self.assertEqual(arc2[9][:100], shrink)
        self.assertEqual(arc2.entry(9).sectors, 1)
        for s in arc.order:
            if s not in (9, 10):
                self.assertEqual(arc2[s], arc[s])
        # Without keep_size the tail is preserved and the file grows.
        new2 = arc.build({10: grow}, keep_size=False)
        self.assertEqual(len(new2), len(blob) + 3 * 2048)

    @need("us1")
    def test_main_program_is_mips(self):
        _, arc = load_archive("us1")
        prog = slz.decompress(arc[1])
        self.assertEqual(len(prog), 0x431C8)
        # first instruction: addiu $sp, $sp, -0x18
        self.assertEqual(struct.unpack_from("<I", prog, 0)[0], 0x27BDFFE8)


if __name__ == "__main__":
    unittest.main()
