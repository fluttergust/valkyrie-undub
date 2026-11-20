#!/usr/bin/env python3
"""File-level diff of VALKYRIE.BIN between disc 1 and disc 2, using the decoded TOCs (exact file sizes).

For each TOC index present on disc 2, classify it as:
  same      identical content to the disc 1 file with the same index
  moved     identical to a disc 1 file with a different index
  changed   index exists on disc 1 but content differs
  new       index absent on disc 1, no identical disc 1 file

usage: file_diff.py <disc1.bin> <disc2.bin> [--list]
"""
from __future__ import annotations

import collections
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vptoc  # noqa: E402


def file_hashes(path: str, toc: vptoc.Toc) -> dict[int, tuple[int, int, str]]:
    """index -> (lba, sectors, sha1 of the file's user data)."""
    out = {}
    with open(path, "rb") as f:
        for e in toc.entries:
            if e.lba is None:
                continue
            h = hashlib.sha1()
            for i in range(e.size):
                f.seek((e.lba + i) * vptoc.SECTOR_RAW + vptoc.USER_OFF)
                h.update(f.read(vptoc.USER_SIZE))
            out[e.index] = (e.lba, e.size, h.hexdigest())
    return out


def classify(h1: dict, h2: dict) -> dict[int, tuple[str, int | None]]:
    by_hash1 = collections.defaultdict(list)
    for idx, (_, _, h) in sorted(h1.items()):
        by_hash1[h].append(idx)
    res = {}
    for idx, (_, _, h) in sorted(h2.items()):
        if idx in h1 and h1[idx][2] == h:
            res[idx] = ("same", idx)
        elif by_hash1.get(h):
            res[idx] = ("moved", by_hash1[h][0])
        elif idx in h1:
            res[idx] = ("changed", idx)
        else:
            res[idx] = ("new", None)
    return res


def data_end(toc: vptoc.Toc) -> int:
    """First LBA after the last byte of file data (end-of-archive filler excluded)."""
    return max(e.lba + e.size for e in toc.entries if e.lba is not None)


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    d1, d2 = args
    t1, _ = vptoc.load(d1)
    t2, _ = vptoc.load(d2)
    h1, h2 = file_hashes(d1, t1), file_hashes(d2, t2)
    res = classify(h1, h2)
    cnt = collections.Counter(c for c, _ in res.values())
    secs = collections.Counter()
    for idx, (c, _) in res.items():
        secs[c] += h2[idx][1]
    print(f"disc1 files {len(h1)}  disc2 files {len(h2)}  disc1-only indices {len(set(h1) - set(h2))}")
    for c in ("same", "moved", "changed", "new"):
        print(f"  {c:8s} {cnt[c]:5d} files {secs[c]:7d} sectors")
    end1 = data_end(t1)
    app = secs["changed"] + secs["new"]
    print(f"disc 1 file data ends at LBA {end1} (image has {os.path.getsize(d1) // vptoc.SECTOR_RAW} sectors)")
    print(f"append {app} sectors -> merged image >= {end1 + app + 150} sectors "
          f"({(end1 + app + 150) / 4500:.1f} min), before inter-file padding")
    if "--list" in argv:
        for idx, (c, ref) in sorted(res.items()):
            if c != "same":
                lba, n, _ = h2[idx]
                print(f"{idx:5d} {c:8s} d2_lba {lba:7d} sectors {n:6d} ref {ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
