#!/usr/bin/env bash
# Make an xdelta3 patch that produces the single-disc image, then verify it by decoding it again.
#
# Source = disc 1 and disc 2 joined into one file (default): the patch is small (~150 KB) and holds no game
# data, since every sector of the single disc comes from one of the two discs (only the archive header, two
# ISO9660 sectors, the patched exe sector and the moved sectors' headers/EDC/ECC differ).
# Source = disc 1 only (give no disc 2): applies to disc 1 directly, but the patch carries the disc-2-only
# files (~65 MB).
#
# usage: tools/make_xdelta.sh <single-disc.bin> <out.xdelta> <disc1.bin> [<disc2.bin>]
# apply: cat disc1.bin disc2.bin > src.bin         (Windows: copy /b disc1.bin+disc2.bin src.bin)
#        xdelta3 -d -s src.bin <out.xdelta> <single-disc.bin>
set -euo pipefail
if [ $# -lt 3 ] || [ $# -gt 4 ]; then
  sed -n 2,13p "$0" >&2
  exit 2
fi
tgt=$1 out=$2 d1=$3 d2=${4:-}
src=$d1
if [ -n "$d2" ]; then
  src="$out.source.bin"
  cat -- "$d1" "$d2" > "$src"
fi
# -9: best compression; -S djw: secondary compression; -A: no app header (keeps local paths out of the patch);
# -B: source window covering both discs, so the appended disc 2 files are found (decoding needs no -B)
xdelta3 -e -f -9 -S djw -A -B 1610612736 -s "$src" "$tgt" "$out"
tmp="$out.verify.bin"
xdelta3 -d -f -s "$src" "$out" "$tmp"
a=$(sha1sum < "$tgt" | cut -d' ' -f1)
b=$(sha1sum < "$tmp" | cut -d' ' -f1)
s=$(sha1sum < "$src" | cut -d' ' -f1)
rm -f -- "$tmp"
[ -n "$d2" ] && rm -f -- "$src"
if [ "$a" != "$b" ]; then
  echo "verify FAILED: $a != $b" >&2
  exit 1
fi
echo "$out: $(stat -c %s "$out") bytes, source sha1 $s, target sha1 $a (verified)"
