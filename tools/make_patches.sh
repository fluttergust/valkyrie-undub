#!/usr/bin/env bash
# Build the undub discs and the single-disc image from your own disc images, write the three xdelta3
# patches and compare them with patches/SHA1SUMS.
#
#   1. vpundub_disc1.xdelta, vpundub_disc2.xdelta   source: US disc N + JP disc N joined (US first)
#   2. vpsingle_undub.xdelta                        source: undub disc 1 + undub disc 2 joined
#
# usage (from the repo root): tools/make_patches.sh [--update]
#   --update   copy the new patches into patches/ (git-ignored, for upload to a release) and rewrite
#              patches/SHA1SUMS (after a source change)
#
# Inputs: roms/us/... and roms/japan/... (see README "Generating the patches").
# Outputs: build/out/ (images, patches, build manifests). About 8 minutes, 4 GB of disk space.
set -euo pipefail
cd "$(dirname "$0")/.."
update=0
[ "${1:-}" = "--update" ] && update=1

out=build/out
u1="$out/Valkyrie Profile (USA) (Disc 1) [Undub].bin"
u2="$out/Valkyrie Profile (USA) (Disc 2) [Undub].bin"
single="$out/Valkyrie Profile (USA) (Single Disc) [Undub].bin"

# 1. undub discs + vpundub_disc{1,2}.xdelta (the build verifies inputs, output and patch)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m vpundub build --disc 1 --disc 2 --out "$out"

# 2. single disc from the two undub discs + vpsingle_undub.xdelta
PYTHONDONTWRITEBYTECODE=1 python3 src/vpsingle/builder.py "$u1" "$u2" -o "$single"
tools/make_xdelta.sh "$single" "$out/vpsingle_undub.xdelta" "$u1" "$u2"

if [ "$update" = 1 ]; then
  cp "$out/vpundub_disc1.xdelta" "$out/vpundub_disc2.xdelta" "$out/vpsingle_undub.xdelta" patches/
  (cd patches && sha1sum vpundub_disc1.xdelta vpundub_disc2.xdelta vpsingle_undub.xdelta > SHA1SUMS)
  echo "patches/ updated"
else
  (cd "$out" && sha1sum -c ../../patches/SHA1SUMS)
  echo "OK: the generated patches match patches/SHA1SUMS"
fi
