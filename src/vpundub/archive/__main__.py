"""Archive CLI.

  python -m vpundub.archive list    <VALKYRIE.BIN>
  python -m vpundub.archive extract <VALKYRIE.BIN> <outdir> [--decompress]
  python -m vpundub.archive verify  <VALKYRIE.BIN>   # TOC + SLZ + rebuild round trip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import slz
from .archive import Archive

STR_MAGIC = b"\x60\x01\x01\x80"


def kind(data: bytes) -> str:
    if slz.is_slz(data):
        return f"slz{data[3]}"
    if data[:4] == STR_MAGIC:
        return "str"
    return "raw"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vpundub.archive")
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("list"); a.add_argument("archive")
    a = sp.add_parser("extract"); a.add_argument("archive"); a.add_argument("outdir")
    a.add_argument("--decompress", action="store_true")
    a = sp.add_parser("verify"); a.add_argument("archive")
    args = ap.parse_args(argv)

    arc = Archive.from_file(args.archive)
    if args.cmd == "list":
        for s in arc.order:
            e = arc.entry(s)
            d = arc[s]
            extra = ""
            if slz.is_slz(d):
                extra = f" blocks={len(slz.blocks(d))}"
            print(f"{s:5d} lba={e.lba:7d} sectors={e.sectors:6d} {kind(d):5s}{extra}")
        print(f"# {len(arc.order)} entries, tail {len(arc.tail) // 2048} sectors")
    elif args.cmd == "extract":
        out = Path(args.outdir)
        out.mkdir(parents=True, exist_ok=True)
        index = []
        for s in arc.order:
            d = arc[s]
            k = kind(d)
            (out / f"{s:04d}.{k}").write_bytes(d)
            rec = {"slot": s, "lba": arc.entry(s).lba, "sectors": arc.entry(s).sectors,
                   "kind": k, "sha1": hashlib.sha1(d).hexdigest()}
            if args.decompress and k.startswith("slz"):
                for i, off in enumerate(slz.blocks(d)):
                    (out / f"{s:04d}.{i}.dec").write_bytes(slz.decompress_block(d, off)[0])
                rec["blocks"] = len(slz.blocks(d))
            index.append(rec)
        (out / "index.json").write_text(json.dumps(index, indent=1))
        print(f"extracted {len(index)} entries to {out}")
    elif args.cmd == "verify":
        blob = Path(args.archive).read_bytes()
        rebuilt = arc.build()
        ok = rebuilt == blob
        bad = []
        for s in arc.order:
            d = arc[s]
            if slz.is_slz(d):
                try:
                    for off in slz.blocks(d):
                        slz.decompress_block(d, off)
                except slz.SlzError as ex:
                    bad.append((s, str(ex)))
        print(f"entries={len(arc.order)} rebuild_identical={ok} slz_errors={len(bad)}")
        return 0 if ok and not bad else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
