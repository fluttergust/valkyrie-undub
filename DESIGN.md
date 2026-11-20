# Design

## Layout

| Path | Purpose |
|---|---|
| `src/vpundub/` | undub builder (Python package, run as `python3 -m vpundub`) |
| `src/vpsingle/` | single-disc builder (plain scripts, run as `python3 src/vpsingle/builder.py`) |
| `tools/` | patch generation, patch application, subtitle routine compiler |
| `tests/` | `unittest` tests; `test_single_*.py` cover `src/vpsingle` |
| `patches/` | `SHA1SUMS` of the release patches; the `.xdelta` files are release assets (git-ignored here) |
| `docs/images/` | README screenshots |
| `roms/`, `build/` | your disc images and generated output (git-ignored) |

## Pipeline

```
US disc N + JP disc N ──vpundub──> undub disc N ──┐           (vpundub_discN.xdelta)
                                                  ├─vpsingle─> undub single disc   (vpsingle_undub.xdelta)
US disc M + JP disc M ──vpundub──> undub disc M ──┘
```

Each patch uses two images joined into one xdelta3 source, so data taken from either image is a copy
instruction and the patch carries almost no game data.

## Undub builder (`src/vpundub`)

| Module | Role |
|---|---|
| `disc/` | raw 2352-byte sectors, EDC/ECC, ISO 9660, image read/rebuild/verify |
| `archive/` | `VALKYRIE.BIN`: encrypted file table (`toc.py`), SLZ compression (`slz.py`) |
| `assets/` | voice sound objects, SPU-ADPCM, clip resampling (`clipfit.py`) |
| `mapping/voicemap.py` | which US voice slot takes which JP data |
| `mapping/scenefix.py` | data-only scene timing patches (JP values) |
| `codepatch.py`, `asm/` | sound driver, battle engine and portrait renderer patches |
| `subtitles/` | vision subtitle overlay and ending FMV subtitle routine |
| `build.py` | the build: verify inputs, plan, replace, rebuild, verify output, write patch |

Design choices:

- Inputs are checked against the Redump SHA-1s; the output is verified sector by sector and slot by slot.
- A replacement keeps the slot's kind, line, group and sample layout; anything unmatched stays US.
- Code and timing patches name their original bytes and refuse to apply to anything else.
- Compiled routines (`overlay.bin`, `fmvsub.bin`) are committed, so a build needs no compiler. Tests
  recompile them when LLVM is present and compare.
- Both discs carry the same subtitle module, so the single-disc merge needs no special case.
- The build is deterministic. xdelta3 runs with `-A`, so no file names end up in the patch.

## Single-disc builder (`src/vpsingle`)

| Module | Role |
|---|---|
| `vptoc.py` | archive file table codec |
| `cdsector.py` | Mode 2 Form 1 sector encoding |
| `file_diff.py` | file-level comparison of disc 2 against disc 1 |
| `slz.py` | token-level edit of SLZ-compressed overlays |
| `patches.py` | `lid_wait` (boot executable) and `prompt_blank` (three overlays) |
| `builder.py` | merge: disc 1 + disc-2-only files, unified file table, patches |

Design choices:

- Disc 1 stays in place; the 394 disc-2-only files go over disc 1's end-of-archive padding.
- One file table lists every file of both discs and carries both disc markers, so the game's own
  disc check passes for either disc.
- Only the lid wait of the disc-change routine is replaced; the rest of the routine runs as before.
- The builder stops if a shared file differs between the two input discs.
