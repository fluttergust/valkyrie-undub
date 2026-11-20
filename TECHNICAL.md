# Technical notes

## Images

| Image | Sectors | SHA-1 |
|---|---|---|
| US disc 1 (SLUS-01156) | 310,565 | `fa13cbde448a47fa87b6c7829df1c5e6215f5f66` |
| US disc 2 (SLUS-01179) | 317,752 | `7cc65766b46273b4ee166269170ecbaa433908fd` |
| Japan disc 1 | 313,223 | `463af3fc81befbbf92c89c47b16dac72fc85f30d` |
| Japan disc 2 | 311,937 | `b992244720af08b97e37c1fba8dc6e86790c7fbc` |
| Undub disc 1 | 310,565 | `fb2874c3f0c6a3aec7b930a19ece7f7c7be9617f` |
| Undub disc 2 | 317,752 | `0ac85d3fd1427ec26c6cfd6c6174fa48c1f1bec1` |
| Undub single disc | 342,269 | `31150eccb690dd980c76efc84c639f4fef7cd78b` |

All are single-track MODE2/2352 images.

## Patches

| Patch | Bytes | SHA-1 | Joined source SHA-1 |
|---|---|---|---|
| `vpundub_disc1.xdelta` | 491,608 | `fa031ab4e8e8e89ae958df229115318ec2963b06` | US 1 + JP 1 |
| `vpundub_disc2.xdelta` | 504,016 | `044f3442096e40e01599a2f2e1ecb801d83ee02b` | US 2 + JP 2 |
| `vpsingle_undub.xdelta` | 336,662 | `9a620649eae18f35a1551907376e55f8741e0f49` | `72d8528700697a1591f3eebb8177dbea53bb85c6` |

- Encoder options, undub: `xdelta3 -e -9 -A -B <joined size>` (LZMA secondary compression by default).
- Encoder options, single disc: `xdelta3 -e -9 -S djw -A -B 1610612736`.
- `-B` makes the source window cover the whole joined file; the 64 MiB default would not reach the second
  image. `-A` drops the application header, which would hold file names. Decoding needs no options.
- Made with xdelta3 3.2.2.

## What the undub changes (relative to the US discs)

| | Disc 1 | Disc 2 |
|---|---|---|
| Voice sound objects replaced | 1,859 | 1,924 |
| Sound items replaced inside mixed packs | 117 | 117 |
| Voice sequence packs replaced whole | 277 | 277 |
| Kept US (no JP match / battle banks) | 10 / 5 | 6 / 5 |
| Archive growth | +8.52 MB | +9.68 MB |

- Game data lives in `VALKYRIE.BIN`: an encrypted file table (XOR keystream) over about 5,000 slots, most of
  them SLZ-compressed. Voices are archive data (SPU-ADPCM sound objects and sequence packs), not XA or STR.
- Timing (`mapping/scenefix.py`): two waits in scene overlay 1988 and the line holds of final-battle overlay
  1810 take the JP values.
- Sound driver (boot executable): a running voice stream is no longer stopped by the next upload; the next
  line waits.
- Battle engine (slot 1490): a refused voice request is retried on the next frame.
- Slot 1630 (Brahms's opening line in the Hrist battle scene): the JP clip is resampled by 5 semitones to 15 sectors so it is uploaded whole
  instead of streamed. Clips of 16 or more sectors are streamed by the engine.
- Portrait seam (field engine, slot 2292, renderer `0x8004e334`, `asm/portrait_tile.s`): mirrored portraits
  drew sheet-column-0 tiles as 15 texels over 16 pixels.
- Vision subtitles (world-map module, slot 4796): a C overlay (`subtitles/overlay.c`) draws the cues in
  `subtitles/data/disc{1,2}.json` with the disc font. Official lines are references into the US script
  (slot, offset, word range), not text. Both discs get the same 129 pages for 18 visions; each page is timed
  against the JP clip it accompanies.
- Ending FMV subtitles (FMV player pack 4616): a hand-written MIPS routine (`subtitles/fmvsub.S`, 916 of the
  932 spare bytes in the slot's last sector) draws the four voiced lines. The disc layout is unchanged.

## What the single disc changes

- 394 files (about 39.7k sectors) exist only on disc 2. They are appended after disc 1's data, over the
  end-of-archive padding.
- File table: 5,120 entries (BCD MSF + size), keystream seed `0x64283921`, rebuilt to list every file of both
  discs. Entries 2 and 3 are the disc 1 and disc 2 markers; the single disc has both.
- `lid_wait` (boot executable, function `0x80011F3C`): the disc-change routine waited for the lid to open and
  close. It now lets the pending Stop finish and issues Standby until accepted, so the drive spins up again
  with no lid cycle. Note the R3000 load delay: `lw ra` must not directly precede `jr ra`.
- `prompt_blank` (archive entries 1, 2174, 4806): the three overlays that hold the disc-change routine turn
  the display on for the prompt with `SetDispMask(1)`; the argument becomes 0, so the screen stays blank for
  the roughly 2 s of spin-up. `slz.py` rewrites only the tokens covering that word; the entries keep their
  sector counts.
- Saves: the game names saves with disc 1's serial on both discs and stores no disc positions, so saves move
  freely between the two-disc and single-disc versions.

## References

- psx-spx, <https://psx-spx.consoledev.net/>: CD-ROM sector format, SPU and SPU-ADPCM, GPU packets, PS-X EXE.
- ECMA-130 Annex A/B (EDC/ECC), ECMA-119 (ISO 9660) with the CD-XA fields, RFC 3284 (VCDIFF).
- Neill Corlett's ECM tools: structure of the EDC/ECC algorithm (reimplemented, no code copied).
- The `VALKYRIE.BIN` formats, sound driver, battle and field engines and the disc-change routine were
  reverse-engineered for this project from the US and JP discs.

## Attributions

- mugi: the original Valkyrie Profile undub, used as a reference only.
- xdelta3 (Joshua MacDonald, Apache-2.0): patch format and tool.
- BizHawk (Nymashock / Mednafen PSX), Beetle PSX, SwanStation, DuckStation, gamescope: testing.
- faster-whisper (SYSTRAN) and Whisper large-v3 (OpenAI): first transcription of vision-only lines,
  corrected by hand.
- *Valkyrie Profile* © 1999 tri-Ace / Enix (now Square Enix).
