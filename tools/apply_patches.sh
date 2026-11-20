#!/usr/bin/env bash
# Apply the release patches (Linux/macOS). Needs xdelta3 and about 5 GB of free space.
#
# The four .xdelta files come from the GitHub release: put them in patches/ or set PATCHES=<folder>.
#
# usage: tools/apply_patches.sh <US disc 1.bin> <JP disc 1.bin> <US disc 2.bin> <JP disc 2.bin> [out dir]
#
# Writes into the out dir (default: out/):
#   Valkyrie Profile (USA) (Disc 1) [Undub].bin/.cue, (Disc 2) [Undub].bin/.cue    (patch 1)
#   Valkyrie Profile (USA) (Single Disc) [Undub].bin/.cue                           (patch 2)
#   Valkyrie Profile (USA) (Single Disc).bin/.cue          US voices, single disc   (patch 3)
set -euo pipefail
if [ $# -lt 4 ] || [ $# -gt 5 ]; then
  sed -n 2,11p "$0" >&2
  exit 2
fi
p=${PATCHES:-"$(cd "$(dirname "$0")/.." && pwd)/patches"}
for f in vpundub_disc1.xdelta vpundub_disc2.xdelta vpsingle_undub.xdelta vpsingle_us.xdelta; do
  [ -f "$p/$f" ] || { echo "missing $p/$f: download it from the GitHub release" >&2; exit 1; }
done
us1=$1 jp1=$2 us2=$3 jp2=$4 out=${5:-out}
mkdir -p "$out"
tmp="$out/source.tmp.bin"
cue() { printf 'FILE "%s.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n' "$1" > "$out/$1.cue"; }

n1="Valkyrie Profile (USA) (Disc 1) [Undub]"
n2="Valkyrie Profile (USA) (Disc 2) [Undub]"
ns="Valkyrie Profile (USA) (Single Disc) [Undub]"
nu="Valkyrie Profile (USA) (Single Disc)"

cat -- "$us1" "$jp1" > "$tmp"
xdelta3 -d -f -s "$tmp" "$p/vpundub_disc1.xdelta" "$out/$n1.bin"; cue "$n1"
cat -- "$us2" "$jp2" > "$tmp"
xdelta3 -d -f -s "$tmp" "$p/vpundub_disc2.xdelta" "$out/$n2.bin"; cue "$n2"
cat -- "$out/$n1.bin" "$out/$n2.bin" > "$tmp"
xdelta3 -d -f -s "$tmp" "$p/vpsingle_undub.xdelta" "$out/$ns.bin"; cue "$ns"
cat -- "$us1" "$us2" > "$tmp"
xdelta3 -d -f -s "$tmp" "$p/vpsingle_us.xdelta" "$out/$nu.bin"; cue "$nu"
rm -f -- "$tmp"
(cd "$out" && sha1sum "$n1.bin" "$n2.bin" "$ns.bin" "$nu.bin")
