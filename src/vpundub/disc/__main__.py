"""Disc layer CLI.

  python -m vpundub.disc info    <bin|cue>
  python -m vpundub.disc extract <bin|cue> <outdir>
  python -m vpundub.disc rebuild <template bin|cue> <out.bin> [NAME=path ...]
  python -m vpundub.disc verify  <bin|cue>     # check every sector's EDC/ECC + header
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from .edc_ecc import regenerate_batch
from .image import DiscImage, _msf_headers, write_cue
from .iso9660 import parse, rebuild
from .sector import SYNC


def _open(p: str) -> DiscImage:
    return DiscImage.from_cue(p) if p.lower().endswith(".cue") else DiscImage(p)


def verify_image(img: DiscImage, batch: int = 8192) -> list[int]:
    """Return LBAs whose sync/header/EDC/ECC do not match a regeneration."""
    bad: list[int] = []
    sync = np.frombuffer(SYNC, dtype=np.uint8)
    for s in range(0, len(img), batch):
        blk = np.array(img.raw[s:s + batch])
        g = regenerate_batch(blk)
        g[:, 0:12] = sync
        g[:, 12:16] = _msf_headers(np.arange(s, s + len(blk), dtype=np.int64))
        bad.extend(int(s + i) for i in np.nonzero((g != blk).any(axis=1))[0])
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vpundub.disc")
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("info"); a.add_argument("image")
    a = sp.add_parser("extract"); a.add_argument("image"); a.add_argument("outdir")
    a = sp.add_parser("rebuild"); a.add_argument("template"); a.add_argument("out")
    a.add_argument("replace", nargs="*", help="NAME=path")
    a = sp.add_parser("verify"); a.add_argument("image")
    args = ap.parse_args(argv)

    if args.cmd == "info":
        img = _open(args.image)
        vol = parse(img)
        print(f"sectors={len(img)} volume_size={vol.volume_size} postgap={vol.postgap()}")
        for e in sorted(vol.entries, key=lambda e: e.lba):
            print(f"{e.lba:8d} {e.size:11d} {'D' if e.is_dir else 'F'} {e.path}")
    elif args.cmd == "extract":
        vol = parse(_open(args.image))
        out = Path(args.outdir)
        for e in vol.files:
            dest = out / e.path.lstrip("/").split(";")[0]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(vol.image.read_form1(e.lba, e.size))
            print(f"{dest} ({e.size} bytes)")
    elif args.cmd == "rebuild":
        vol = parse(_open(args.template))
        repl = {}
        for r in args.replace:
            name, _, path = r.partition("=")
            repl[name] = Path(path).read_bytes()
        out = Path(args.out)
        new = rebuild(vol, str(out), repl)
        write_cue(out, out.with_suffix(".cue"))
        print(f"wrote {out} ({len(new.image)} sectors)")
    elif args.cmd == "verify":
        bad = verify_image(_open(args.image))
        print(f"{len(bad)} bad sectors" + (f": first {bad[:10]}" if bad else ""))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
