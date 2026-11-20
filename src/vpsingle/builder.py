#!/usr/bin/env python3
"""Build a single-disc Valkyrie Profile (USA) image from disc 1 + disc 2.

Layout of the output image:
  LBA 0 .. D1_END-1      disc 1, verbatim (except the archive header, two ISO9660 sectors and the
                         code-patched SLUS_011.56 sector, see patches.py; with overrides, the entries from the
                         first overridden one on are rewritten/moved, see below)
  LBA D1_END ..          disc 2 files that are not on disc 1 (in disc 2 order, re-encoded)
  + 150 postgap sectors  (as on the originals)
D1_END is the end of disc 1's last file, so the appended data overwrites disc 1's end-of-archive filler.

Header modes (what goes into the archive header at LBA 150):
  unified  one TOC with every index: disc 1 entries + disc-2-only entries pointing at the appended data.
           (All 4,057 indices present on both discs hold identical files.)
  disc2    disc 2's TOC, remapped: shared files -> disc 1 copy, others -> appended copy.
           Used to test the relocated disc 2 data with a normal disc swap.

Overrides (--override INDEX=FILE): replacement data for an archive entry. An entry present on both discs with
different contents ("changed", e.g. the undub's world-map module 4796, see tools/undub_subtitles.py) needs
one, since one disc can hold only one copy. The data replaces the entry where it is; if its sector count differs,
the disc 1 entries after it move by the difference, so the archive stays contiguous (no gaps).

usage: builder.py <disc1.bin> <disc2.bin> [-o OUT.bin] [--header unified|disc2] [--no-patch]
                  [--override INDEX=FILE ...]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cdsector  # noqa: E402
import patches  # noqa: E402
import slz  # noqa: E402
import vptoc  # noqa: E402
import file_diff  # noqa: E402

SECTOR = cdsector.SECTOR
POSTGAP = 150
PVD_LBA = 16
EOF_SUBHEADER = bytes((0, 0, 0x89, 0))
DATA_SUBHEADER = bytes((0, 0, 0x08, 0))      # what every archive file sector carries (form 1 data)


class Image:
    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "rb")
        self.nsect = os.path.getsize(path) // SECTOR

    def raw(self, lba: int, n: int = 1) -> bytes:
        self.f.seek(lba * SECTOR)
        return self.f.read(n * SECTOR)


def sectors(nbytes: int) -> int:
    return -(-nbytes // vptoc.USER_SIZE)


def plan(d1: str, d2: str, overrides: dict[int, bytes] | None = None):
    """Compute the merged layout. Returns a dict with both TOC variants, the disc 1 entries that are rewritten
    or moved (relayout), and the appended disc 2 files.

    An override replaces its entry where it is. If its size in sectors differs, the disc 1 entries after it
    move by the difference (they are plain data files, read through the TOC), so the archive stays contiguous
    like the original discs."""
    overrides = overrides or {}
    t1, _ = vptoc.load(d1)
    t2, _ = vptoc.load(d2)
    h1, h2 = file_diff.file_hashes(d1, t1), file_diff.file_hashes(d2, t2)
    cls = file_diff.classify(h1, h2)
    d1_end = file_diff.data_end(t1)
    missing = sorted(i for i, (c, _) in cls.items() if c == "changed" and i not in overrides)
    if missing:
        raise ValueError(f"entries {missing} differ between the discs; give replacement data for them "
                         "(--override INDEX=FILE, see tools/undub_subtitles.py for the undub's entry 4796)")
    for idx, data in overrides.items():
        if not data or (t1.get(idx).lba is None and t2.get(idx).lba is None):
            raise ValueError(f"override {idx}: empty data or no such entry on either disc")

    # disc 1 entries in disc order; from the first overridden one on, each is rewritten at a running position
    loc = {}                # index -> (lba, sectors) wherever that differs from disc 1's entry
    relayout = []           # (index, old_lba, old_sectors, new_lba, new_sectors, override data or None)
    on1 = sorted((e for e in t1.entries if e.lba is not None), key=lambda e: e.lba)
    first = next((k for k, e in enumerate(on1) if e.index in overrides), len(on1))
    copy_end = on1[first].lba if first < len(on1) else d1_end
    pos = copy_end
    for e in on1[first:]:
        data = overrides.get(e.index)
        n = sectors(len(data)) if data is not None else e.size
        relayout.append((e.index, e.lba, e.size, pos, n, data))
        if (pos, n) != (e.lba, e.size):
            loc[e.index] = (pos, n)
        pos += n
    assert pos >= copy_end and (first == len(on1) or on1[-1].lba + on1[-1].size == d1_end)
    new_d1_end = pos

    appended = []           # (index, d2_lba, d2_sectors, new_lba, override data or None), in disc 2 order
    for idx in sorted((i for i, (c, _) in cls.items() if c in ("new", "changed") and t1.get(i).lba is None),
                      key=lambda i: h2[i][0]):
        lba, n, _ = h2[idx]
        data = overrides.get(idx)
        m = sectors(len(data)) if data is not None else n
        appended.append((idx, lba, n, pos, data))
        loc[idx] = (pos, m)
        pos += m

    def unified_entry(e1, e2):
        if e2.index in loc:
            return vptoc.Entry(e2.index, *loc[e2.index])
        return e1

    def disc2_entry(e):
        if e.lba is None:
            return e
        if e.index in loc:
            return vptoc.Entry(e.index, *loc[e.index])
        c, ref = cls[e.index]
        assert c in ("same", "moved")
        r = t1.get(ref)
        return vptoc.Entry(e.index, *loc.get(ref, (r.lba, e.size)))

    unified = vptoc.Toc([unified_entry(a, b) for a, b in zip(t1.entries, t2.entries)], t1.pad)
    disc2 = vptoc.Toc([disc2_entry(e) for e in t2.entries], t2.pad)
    return dict(d1_end=d1_end, copy_end=copy_end, new_d1_end=new_d1_end, archive_end=pos, relayout=relayout,
                appended=appended, unified=unified, disc2=disc2)


def data_sectors(lba: int, data: bytes, eof_at: int) -> list[bytes]:
    """Raw form 1 sectors holding `data` (zero-padded) from `lba`; the sector at `eof_at` gets the EOF subheader."""
    out = []
    for k in range(sectors(len(data))):
        chunk = data[k * vptoc.USER_SIZE:(k + 1) * vptoc.USER_SIZE].ljust(vptoc.USER_SIZE, b"\0")
        out.append(cdsector.encode_form1(lba + k, EOF_SUBHEADER if lba + k == eof_at else DATA_SUBHEADER, chunk))
    return out


def patch_iso(sec16: bytes, sec22: bytes, total: int, archive_bytes: int) -> tuple[bytes, bytes]:
    pvd = bytearray(sec16[24:2072])
    assert pvd[1:6] == b"CD001"
    pvd[80:88] = total.to_bytes(4, "little") + total.to_bytes(4, "big")
    root = bytearray(sec22[24:2072])
    name = b"VALKYRIE.BIN;1"
    p = root.find(name)
    assert p > 33, "VALKYRIE.BIN record not found in root directory"
    rec = p - 33
    root[rec + 10:rec + 18] = archive_bytes.to_bytes(4, "little") + archive_bytes.to_bytes(4, "big")
    return (cdsector.reencode(sec16, PVD_LBA, bytes(pvd)), cdsector.reencode(sec22, 22, bytes(root)))


def root_dir_lba(img: Image) -> int:
    pvd = img.raw(PVD_LBA)[24:2072]
    return int.from_bytes(pvd[156 + 2:156 + 6], "little")


def find_file(img: Image, name: str) -> tuple[int, int]:
    """(lba, size in bytes) of a file in the root directory (single-sector root, as on these discs)."""
    root = img.raw(root_dir_lba(img))[24:2072]
    p = 0
    while p < len(root) and root[p]:
        n = root[p]
        ident = root[p + 33:p + 33 + root[p + 32]].decode("ascii", "replace")
        if ident.split(";")[0] == name:
            return int.from_bytes(root[p + 2:p + 6], "little"), int.from_bytes(root[p + 10:p + 14], "little")
        p += n
    raise KeyError(name)


def patch_sectors(img: Image, plist: list) -> dict[int, bytes]:
    """Apply code patches; returns {lba: re-encoded raw sector}."""
    out: dict[int, bytes] = {}
    for pt in plist:
        lba0, size = find_file(img, pt.file)
        assert pt.offset + len(pt.new) <= size
        first, last = pt.offset // vptoc.USER_SIZE, (pt.offset + len(pt.new) - 1) // vptoc.USER_SIZE
        buf = bytearray()
        for k in range(first, last + 1):
            raw = out.get(lba0 + k, img.raw(lba0 + k))
            buf += raw[24:2072]
        patches.apply(buf, pt, first * vptoc.USER_SIZE)
        for i, k in enumerate(range(first, last + 1)):
            lba = lba0 + k
            raw = out.get(lba, img.raw(lba))
            out[lba] = cdsector.reencode(raw, lba, bytes(buf[i * 2048:(i + 1) * 2048]))
    return out


def module_patches(d1: str, d2: str, overrides: dict[int, bytes]) -> tuple[dict, dict[int, bytes]]:
    """Apply patches.MODULE_PATCHES. Returns (layout plan, {lba: raw sector}) after patching each entry where it
    is (same LBA and sector count). An entry that is overridden, moved by the relayout or grows becomes an
    override instead (`overrides` is updated)."""
    t1, _ = vptoc.load(d1)
    i1 = Image(d1)
    entries = sorted({mp.entry for mp in patches.MODULE_PATCHES})
    for idx in entries:
        if t1.get(idx).lba is None:
            raise ValueError(f"module patch entry {idx} is not on disc 1")
    patched = {idx: patches.apply_module(overrides.get(idx) or vptoc.read_user(d1, t1.get(idx).lba, t1.get(idx).size),
                                         [mp for mp in patches.MODULE_PATCHES if mp.entry == idx])
               for idx in entries}
    for idx in entries:
        if idx in overrides or sectors(slz.HDR + slz.header(patched[idx])[1]) > t1.get(idx).size:
            overrides[idx] = patched[idx]
    while True:                     # overriding one entry can move the others into the relayout
        p = plan(d1, d2, overrides)
        late = [i for i in entries if i not in overrides and t1.get(i).lba + t1.get(i).size > p["copy_end"]]
        if not late:
            break
        for i in late:
            overrides[i] = patched[i]
    special: dict[int, bytes] = {}
    for idx in entries:
        if idx in overrides:
            continue
        e = t1.get(idx)
        new = patched[idx][:e.size * vptoc.USER_SIZE]
        new += bytes(e.size * vptoc.USER_SIZE - len(new))
        for k in range(e.size):
            special[e.lba + k] = cdsector.reencode(i1.raw(e.lba + k), e.lba + k,
                                                   new[k * vptoc.USER_SIZE:(k + 1) * vptoc.USER_SIZE])
    return p, special


def build(d1: str, d2: str, out: str, header: str = "unified", code_patches: bool = True,
          overrides: dict[int, bytes] | None = None) -> dict:
    overrides = dict(overrides or {})
    user_overrides = sorted(overrides)
    special = {}
    if code_patches:
        p, special = module_patches(d1, d2, overrides)
    else:
        p = plan(d1, d2, overrides)
    i1, i2 = Image(d1), Image(d2)
    assert root_dir_lba(i1) == 22
    total = p["archive_end"] + POSTGAP
    archive_bytes = (p["archive_end"] - vptoc.ARCHIVE_LBA) * vptoc.USER_SIZE

    toc = p[header]
    # keep the unused tail of header sector 12 from the source disc
    tail = vptoc.read_user(d2 if header == "disc2" else d1, vptoc.ARCHIVE_LBA, vptoc.HEADER_SECTORS)
    hdr_full = vptoc.crypt(toc.build()) + tail[vptoc.HEADER_BYTES:]

    s16, s22 = patch_iso(i1.raw(PVD_LBA), i1.raw(22), total, archive_bytes)
    special.update({PVD_LBA: s16, 22: s22})
    if code_patches:
        special.update(patch_sectors(i1, patches.PATCHES))
    assert all(lba < p["copy_end"] for lba in special) and vptoc.ARCHIVE_LBA + vptoc.HEADER_SECTORS <= p["copy_end"]
    sha = hashlib.sha1()
    tmp = out + ".part"
    with open(tmp, "wb") as f:
        def w(b: bytes):
            f.write(b)
            sha.update(b)
        # disc 1 part
        chunk = 4096
        lba = 0
        while lba < p["copy_end"]:
            n = min(chunk, p["copy_end"] - lba)
            buf = bytearray(i1.raw(lba, n))
            for sl, data in special.items():
                if lba <= sl < lba + n:
                    buf[(sl - lba) * SECTOR:(sl - lba + 1) * SECTOR] = data
            for k in range(vptoc.HEADER_SECTORS):
                s = vptoc.ARCHIVE_LBA + k
                if lba <= s < lba + n:
                    off = (s - lba) * SECTOR
                    raw = bytes(buf[off:off + SECTOR])
                    buf[off:off + SECTOR] = cdsector.reencode(raw, s, hdr_full[k * 2048:(k + 1) * 2048])
            w(bytes(buf))
            lba += n
        last = p["archive_end"] - 1          # the archive's last sector carries the EOF subheader

        def copy(img: Image, src: int, n: int, dst: int):
            for k in range(n):
                raw = img.raw(src + k)
                if dst + k == last:
                    w(cdsector.encode_form1(dst + k, EOF_SUBHEADER, raw[24:2072]))
                else:
                    w(cdsector.reencode(raw, dst + k))
        # disc 1 entries from the first override on (overridden or moved)
        for idx, src, n, dst, m, data in p["relayout"]:
            if data is not None:
                for raw in data_sectors(dst, data, last):
                    w(raw)
            else:
                copy(i1, src, n, dst)
        # appended disc 2 files
        for idx, src, n, dst, data in p["appended"]:
            if data is not None:
                for raw in data_sectors(dst, data, last):
                    w(raw)
            else:
                copy(i2, src, n, dst)
        # postgap: copy disc 1's trailing sectors, new MSF header only
        for k in range(POSTGAP):
            raw = bytearray(i1.raw(i1.nsect - POSTGAP + k))
            raw[12:16] = cdsector.lba_header(p["archive_end"] + k)
            w(bytes(raw))
    os.replace(tmp, out)
    cue = os.path.splitext(out)[0] + ".cue"
    with open(cue, "w", newline="\r\n") as f:
        f.write(f'FILE "{os.path.basename(out)}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
    moved = sum(1 for r in p["relayout"] if r[5] is None and r[1] != r[3])
    return dict(total=total, d1_end=p["new_d1_end"], archive_end=p["archive_end"],
                appended_files=len(p["appended"]), overridden=user_overrides, moved=moved, sha1=sha.hexdigest())


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("disc1")
    ap.add_argument("disc2")
    ap.add_argument("-o", "--out", default=os.path.join("build", "Valkyrie Profile (USA) (Single Disc).bin"))
    ap.add_argument("--header", choices=("unified", "disc2"), default="unified")
    ap.add_argument("--no-patch", action="store_true", help="do not apply code patches (src/patches.py)")
    ap.add_argument("--override", action="append", default=[], metavar="INDEX=FILE",
                    help="replacement data for archive entry INDEX (repeatable)")
    a = ap.parse_args(argv[1:])
    overrides = {}
    for o in a.override:
        idx, _, path = o.partition("=")
        if not idx.isdigit() or not path:
            ap.error(f"--override {o!r}: expected INDEX=FILE")
        with open(path, "rb") as f:
            overrides[int(idx)] = f.read()
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    try:
        r = build(a.disc1, a.disc2, a.out, a.header, not a.no_patch, overrides)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    ov = (f", overrides {r['overridden']} ({r['moved']} disc 1 files moved)" if overrides else "")
    print(f"{a.out}: {r['total']} sectors ({r['total'] / 4500:.2f} min), disc 1 data to LBA {r['d1_end']}, "
          f"{r['appended_files']} disc 2 files appended, archive end {r['archive_end']}{ov}, sha1 {r['sha1']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
