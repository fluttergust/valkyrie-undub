"""Voice mapping CLI.

  python -m vpundub.mapping <US VALKYRIE.BIN> <JP VALKYRIE.BIN> -o build/map_disc1.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..archive.archive import Archive
from .voicemap import build_plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vpundub.mapping")
    ap.add_argument("us")
    ap.add_argument("jp")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    plan = build_plan(Archive.from_file(a.us), Archive.from_file(a.jp)).to_json()
    print(json.dumps(plan["summary"], indent=1))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(plan, indent=1))
        print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
