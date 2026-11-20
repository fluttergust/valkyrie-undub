#!/usr/bin/env python3
"""Compile the subtitle routines: overlay.c -> overlay.bin (visions), fmvsub.S -> fmvsub.bin (ending FMV).

The build uses the committed .bin files, so it needs no compiler. Run this after changing overlay.c/.ld,
fmvsub.S/.ld or the FMV cues (needs clang, ld.lld, llvm-objcopy, llvm-nm, llvm-objdump with the mipsel
target; the FMV data is assembled with the font of the US disc 1 image, ./roms):

    python3 tools/subtitle_overlay.py            # rewrite overlay.bin and fmvsub.bin
    python3 tools/subtitle_overlay.py --check    # exit 1 if either .bin is not what its source compiles to

``hook`` must stay at subtitles.patch.HOOK (update the constant if overlay.c moves it). For fmvsub.bin the
data addresses are printed as ``LAYOUT``; fmvsub.LAYOUT must match (``--check`` compares them).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vpundub.subtitles import patch  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as t:
        code, syms = patch.compile_overlay(Path(t))
    if syms["hook"] != patch.HOOK:
        print(f"hook at {syms['hook']:#x}, expected {patch.HOOK:#x}")
        return 1
    target = patch.HERE / "overlay.bin"
    rc = 0
    if a.check:
        same = target.read_bytes() == code
        print(f"overlay.bin {'matches' if same else 'differs from'} overlay.c ({len(code)} bytes)")
        rc |= not same
    else:
        target.write_bytes(code)
        print(f"{target}: {len(code)} bytes, hook {syms['hook']:#x}, subdata {syms['subdata']:#x}")
    return rc | fmv(a.check)


def fmv(check: bool) -> int:
    from vpundub.build import DEFAULT_CUE, _archive, _open
    from vpundub.subtitles import fmvsub
    from vpundub.subtitles.font import Font, find_font
    us, _ = _archive(_open(Path(DEFAULT_CUE[("us", 1)])))
    with tempfile.TemporaryDirectory() as t:
        img, syms = fmvsub.compile(Path(t), Font(find_font(us)))
    lay = {k: syms[k] for k in fmvsub.LAYOUT}
    code = img[:syms["cols"] - fmvsub.HOOK]
    target = fmvsub.HERE / "fmvsub.bin"
    text = "LAYOUT = {" + ", ".join(f'"{k}": {v}' if k == "WT" else f'"{k}": {v:#010X}'.replace("0X", "0x")
                                    for k, v in lay.items()) + "}"
    if check:
        same = target.read_bytes() == code and lay == fmvsub.LAYOUT
        print(f"fmvsub.bin {'matches' if same else 'differs from'} fmvsub.S ({len(code)} B code, {len(img)} B "
              f"with data); {text}")
        return 0 if same else 1
    target.write_bytes(code)
    print(f"{target}: {len(code)} B code, {len(img)} B with data, "
          f"{fmvsub.SLOT_LEN - fmvsub.DATA_END - len(img)} B of slot {fmvsub.SLOT} left")
    if lay != fmvsub.LAYOUT:
        print(f"update fmvsub.py: {text}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
