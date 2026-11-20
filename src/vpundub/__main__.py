"""vpundub CLI.

  python -m vpundub build --disc 1 [--us US.cue] [--jp JP.cue] [--out build/out]
                          [--no-verify-input] [--no-patch] [--no-code-patches]
                          [--no-subtitles] [--driver wait|overlap] [--no-fit-clips] [--whole-sectors N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .build import BuildError, build_disc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vpundub")
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build", help="build an undubbed disc image")
    b.add_argument("--disc", type=int, choices=(1, 2), required=True, action="append",
                   help="disc number (repeatable)")
    b.add_argument("--us", type=Path, help="US .cue/.bin (single disc only)")
    b.add_argument("--jp", type=Path, help="JP .cue/.bin (single disc only)")
    b.add_argument("--out", type=Path, default=Path("build/out"))
    b.add_argument("--no-verify-input", action="store_true")
    b.add_argument("--no-patch", action="store_true")
    b.add_argument("--no-code-patches", action="store_true",
                   help="experiment: leave the boot exe, battle and field engines unpatched")
    b.add_argument("--no-subtitles", action="store_true",
                   help="leave out the Spiritual Concentration subtitles")
    b.add_argument("--driver", choices=("wait", "overlap"), default="wait",
                   help="sound driver patch: wait (release: the next line waits for a voice stream) or "
                        "overlap (trial: no wait, but a whole clip can cut the stream, see TECHNICAL)")
    b.add_argument("--whole-sectors", type=int, default=15,
                   help="trial: battle voice clips of up to N sectors play whole instead of streamed "
                        "(default 15 = original engine limit)")
    b.add_argument("--no-fit-clips", action="store_true",
                   help="keep every JP clip as is (no resampling of FIT_CLIPS such as slot 1630)")
    a = ap.parse_args(argv)
    if (a.us or a.jp) and len(a.disc) > 1:
        ap.error("--us/--jp can only be used with a single --disc")
    try:
        for d in a.disc:
            build_disc(d, a.us, a.jp, a.out, not a.no_verify_input, not a.no_patch,
                       log=lambda m, d=d: print(f"[disc {d}] {m}", flush=True),
                       code_patches=not a.no_code_patches, subtitles=not a.no_subtitles,
                       driver=a.driver, fit_clips=not a.no_fit_clips,
                       whole_sectors=a.whole_sectors)
    except BuildError as e:
        print(f"BUILD FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
