"""Small, verified code patches to the US boot executable and two engine slots.

The undub otherwise only changes data. One driver behaviour has to change for
the Japanese voices: a voice clip whose sound object is larger than 0x7fff
bytes is streamed to SPU RAM instead of uploaded whole (32 KB per voice
channel buffer, SPU 0x38000/0x40000). When the game asks to play a *whole*
clip while a stream is running, the sound driver's play function (0x80019834,
mode 1 path at 0x80019970) stops every streaming track and returns -1 (busy);
the caller retries and the new clip then plays. With the US voices only one
clip per disc is long enough to stream, so this hardly mattered; ~157 JP clips
per disc are, and the next clip cuts them off (e.g. disc 2 slot 1630, Brahms's
opening line in the Hrist fight scene: 0.55 s of a 3.1 s line).

The patch keeps the "busy" answer but skips stopping the stream, so the next
clip waits for the stream to finish:

    80019978  beqz $2, 0x800199dc     ; not busy -> upload (unchanged)
    8001997c  addiu $19, $zero, 1     ; (unchanged delay slot)
    80019980  move $17, $zero         -> j 0x80019b3c   (function epilogue)
    80019984  move $16, $18           -> addiu $2, $zero, -1

Both US discs boot the same executable (SLUS_011.56 / SLUS_011.79 are
byte-identical), so the same patch applies to each.

Alternative (``DRIVER_OVERLAP``, from the user's ../vpdecomp, under trial
2026-11-17; ``build --driver overlap``): the mode 1 busy test checks only the
voice and upload flags (+0x10dc, +0x10de) and ignores the stream flag
(+0x10dd), so a whole clip uploads and plays next to a running stream, as both
lines did with the US voices. Nothing is stopped and nothing waits, so slot 1630
does not need resampling (``build --no-fit-clips``). On the upload path s3 is
set from the upload result (0x800199ec) and s0 at 0x80019a1c before use, so
dropping their setup is safe:

    80019970  jal 0x80018f70      -> lbu   $2, 0x10dc($18)
    80019974  move $4,$18         -> lbu   $3, 0x10de($18)
    80019978  beqz $2,0x800199dc  -> nop
    8001997c  addiu $19,$zero,1   -> or    $2, $2, $3
    80019980  move $17,$zero      -> beqz  $2, 0x800199dc
    80019984  move $16,$18        -> addiu $2, $zero, -1
    80019988  lw $2,0x112c($16)   -> j     0x80019b3c
    8001998c  nop                 -> nop

Busy is not retried everywhere, though: the battle engine (archive slot 1490,
SLZ, decompressed to 0x8002f824, the same on both discs) stores the handle and
moves its dialogue voice state machine on even when the driver returns -1, so
a line requested while a stream plays was dropped (Hrist fight scene: slot 1496
item 2 after slot 1630). ``SLOT_PATCHES`` make both play sites (0x80052cfc,
0x800530ac) leave the state unchanged on -1, so the request is repeated on the
next frame and the line plays once the stream ends. To make room for the test
(`addiu $3,$2,1; beqz $3, <frame end>`) the sign correction of the
`size / 100` division is dropped (sizes are positive).

Not voice related: the field engine (slot 2292, SLZ type 2, also decompressed to
0x8002f824) draws mirrored dialogue portraits with tiles at sheet column u = 0
clamped to 15 texels over 16 pixels (a seam, reported by the vpandroidextract
HD texture work). The patch at 0x8004e454 takes the "-1" texel from the 8bpp
page to the left instead (src/vpundub/asm/portrait_tile.s; TECHNICAL.md,
"Portrait seam").
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .archive import slz

EXE_NAME = {1: "SLUS_011.56", 2: "SLUS_011.79"}
LOAD_ADDR = 0x80010000
HEADER = 0x800


@dataclass(frozen=True)
class CodePatch:
    name: str
    addr: int
    old: bytes
    new: bytes

    @property
    def offset(self) -> int:
        return self.addr - LOAD_ADDR + HEADER


def _w(*words: int) -> bytes:
    return b"".join(w.to_bytes(4, "little") for w in words)


def _j(target: int) -> int:
    return 0x08000000 | ((target >> 2) & 0x03FFFFFF)


PATCHES: tuple[CodePatch, ...] = (
    CodePatch(
        "keep voice stream when a whole clip is requested",
        0x80019980,
        old=_w(0x00008821, 0x02408021),          # move $17,$zero ; move $16,$18
        new=_w(_j(0x80019B3C), 0x2402FFFF),      # j epilogue ; addiu $2,$zero,-1
    ),
)


DRIVER_WAIT = PATCHES[0]
DRIVER_OVERLAP = CodePatch(
    "play a whole clip next to a running voice stream",
    0x80019970,
    old=_w(0x0C0063DC, 0x02402021, 0x10400018, 0x24130001, 0x00008821, 0x02408021, 0x8E02112C, 0x00000000),
    new=_w(0x924210DC, 0x924310DE, 0x00000000, 0x00431025, 0x10400016, 0x2402FFFF, _j(0x80019B3C), 0x00000000),
)
DRIVERS = {"wait": (DRIVER_WAIT,), "overlap": (DRIVER_OVERLAP,)}


BATTLE_SLOT = 1490
BATTLE_BASE = 0x8002F824          # RAM address of decompressed offset 0


@dataclass(frozen=True)
class SlotPatch:
    name: str
    slot: int
    base: int
    addr: int
    old: bytes
    new: bytes

    @property
    def offset(self) -> int:
        return self.addr - self.base


def _beqz(reg: int, pc: int, target: int) -> int:
    return 0x10000000 | reg << 21 | (((target - pc - 4) >> 2) & 0xFFFF)


# Both sites: sw handle; (new) skip to frame end if handle == -1; state = 2/1;
# 0x48 = size / 100 (0x51eb851f >> 5).
SLOT_PATCHES: tuple[SlotPatch, ...] = (
    SlotPatch(
        "battle voice line: retry while the driver is busy (site 1)",
        BATTLE_SLOT, BATTLE_BASE, 0x80052CFC,
        old=_w(0xAE220018, 0x3C0451EB, 0x24020002, 0xAE220020, 0x8E030004, 0x3484851F, 0x00640018,
               0x00031FC3, 0x00004010, 0x00081143, 0x00431023, _j(0x80052E34), 0xAE220048),
        new=_w(0xAE220018, 0x24430001, _beqz(3, 0x80052D04, 0x80052E34), 0x3C0451EB, 0x8E030004,
               0x3484851F, 0x00640018, 0x24020002, 0xAE220020, 0x00004010, 0x00081143,
               _j(0x80052E34), 0xAE220048),
    ),
    SlotPatch(
        "battle voice line: retry while the driver is busy (site 2)",
        BATTLE_SLOT, BATTLE_BASE, 0x800530AC,
        old=_w(0xAE020018, 0x3C0251EB, 0x8E430044, 0x3442851F, 0x00620018, 0x24020001, 0x00031FC3,
               0xAE420020, 0x00004010, 0x00081143, 0x00431023, _j(0x800531B4), 0xAE420048),
        new=_w(0xAE020018, 0x24430001, _beqz(3, 0x800530B4, 0x800531B4), 0x3C0251EB, 0x8E430044,
               0x3442851F, 0x00620018, 0x24020001, 0xAE420020, 0x00004010, 0x00081143,
               _j(0x800531B4), 0xAE420048),
    ),
)


# Battle engine: a voice clip whose archive size (TOC sectors * 2048) is > 0x7FFF is streamed
# (driver mode 2), otherwise uploaded whole (mode 1); `addiu v0, zero, 0x7fff` at both play sites.
# Trial option (build --whole-sectors N): raise the limit so JP clips of up to N sectors stay whole.
# Not safe as is: the two voice lanes are 32 KB banks at SPU 0x38000/0x40000 and battle sound bank 0
# starts at 0x48000, so a clip over 32 KB overwrites the next bank (TECHNICAL, "Whole-clip limit trial").
WHOLE_LIMIT_SITES = (0x80052CD0, 0x80053078)
WHOLE_LIMIT_OLD = 0x24027FFF


def whole_limit_patches(sectors: int) -> tuple[SlotPatch, ...]:
    """Slot 1490 patches that make clips of up to ``sectors`` sectors play whole (15 = original)."""
    if sectors == 15:
        return ()
    if not 15 < sectors < 32:
        raise CodePatchError(f"whole-clip limit {sectors} sectors out of range 16..31")
    limit = (sectors + 1) * 2048 - 1                 # ori v0, zero, limit (15 sectors -> 0x7fff)
    return tuple(SlotPatch(f"battle voice: clips up to {sectors} sectors play whole (site {i + 1})",
                           BATTLE_SLOT, BATTLE_BASE, a, old=_w(WHOLE_LIMIT_OLD), new=_w(0x34020000 | limit))
                 for i, a in enumerate(WHOLE_LIMIT_SITES))


FIELD_SLOT = 2292
FIELD_BASE = 0x8002F824           # same load address as the battle engine

# Dialogue portrait renderer 0x8004e334 (portraits are always mirrored here: x1 = x0 - w),
# per-tile uv + packet copy block 0x8004e454-0x8004e54c. Original: u0 = u - 1, and a tile
# at sheet column u = 0 is clamped to u0 = 0 with the width cut by one, so it maps 15
# texels over 16 pixels: invisible at 1x (the same texels are sampled), a 1-texel seam
# with upscaled rendering or HD texture packs (reported on young Lucian, Platina, Odin).
# New: src/vpundub/asm/portrait_tile.s (u0 = 0x7f from the 8bpp page to the left, the
# copied packet's tpage x base - 1), then nop padding up to 0x8004e550.
_PORTRAIT_TILE_OLD = bytes.fromhex(
    "020084910400c5850600c88503008991ffff8424040081042110850021200000ffffa524211085000001"
    "42280200401421102801ffffa5240001422802004014ff008230ffff0825001a0900251043000c00c2a4"
    "211085000d00c390ff004230001a0300251043001400c2a421102801ff0042300c00c390001202002518"
    "62001c00c3a41d00c2901400c39000120200251862000300e0102400c3a43f390108407f022400008295"
    "000000000e00c2a40880093ce0f0298d2140c00020000a250000028d0400038d0800048d0c00058d0000"
    "22ad040023ad080024ad0c0025ad10000825f6ff0a15100029250000028d0400038d000022ad040023ad")
_PORTRAIT_TILE_CODE = bytes.fromhex(
    "020084910400c5850600c885030089912508000010008014ffff84241600c29400000000800143308000"
    "6338080060140f004330060060108100a32c04006010000000007f000424030000100100012425200000"
    "ffffa52421108500000142280200401421102801ffffa524000142280200401400000000ffff08252110"
    "8500211828010c00c4a00d00c9a01400c2a01500c9a01c00c4a01d00c3a02400c2a02500c3a00300e014"
    "407f022400008295000000000e00c2a40880093ce0f0298d2540c0002800ca240000028d040008250000"
    "22adfcff0a150400292504002010eeff229500000000ffff4224eeff22a5")
_PORTRAIT_TILE_NEW = _PORTRAIT_TILE_CODE.ljust(len(_PORTRAIT_TILE_OLD), b"\0")

SLOT_PATCHES += (
    SlotPatch(
        "mirrored portrait tiles at u=0: exact texel span",
        FIELD_SLOT, FIELD_BASE, 0x8004E454, old=_PORTRAIT_TILE_OLD, new=_PORTRAIT_TILE_NEW,
    ),
)


class CodePatchError(ValueError):
    pass


def patch_exe(exe: bytes, patches: tuple[CodePatch, ...] = PATCHES) -> bytes:
    """Apply ``patches`` to a US boot executable, checking the original bytes."""
    if exe[:8] != b"PS-X EXE":
        raise CodePatchError("not a PS-X EXE")
    out = bytearray(exe)
    for p in patches:
        cur = bytes(out[p.offset:p.offset + len(p.old)])
        if cur != p.old:
            raise CodePatchError(f"{p.name}: expected {p.old.hex()} at {p.addr:#x}, found {cur.hex()}")
        out[p.offset:p.offset + len(p.new)] = p.new
    return bytes(out)


def patch_data(data: bytes, patches) -> bytes:
    """Apply ``SlotPatch``es to decompressed slot data, checking the original bytes."""
    out = bytearray(data)
    for p in patches:
        cur = bytes(out[p.offset:p.offset + len(p.old)])
        if cur != p.old:
            raise CodePatchError(f"{p.name}: expected {p.old.hex()} at {p.addr:#x}, found {cur.hex()}")
        out[p.offset:p.offset + len(p.new)] = p.new
    return bytes(out)


def slot_patches(patches: tuple[SlotPatch, ...] = SLOT_PATCHES) -> dict[int, list[SlotPatch]]:
    per: dict[int, list[SlotPatch]] = {}
    for p in patches:
        per.setdefault(p.slot, []).append(p)
    return per


def patched_slot_data(us_entry: bytes, patches) -> bytes:
    """Decompressed contents the patched slot must have."""
    return patch_data(slz.decompress(us_entry), patches)


def apply_slot_patches(us_entry_of, new: dict[int, bytes],
                       patches: tuple[SlotPatch, ...] = SLOT_PATCHES) -> dict[int, bytes]:
    """Return ``new`` with the SLZ code slots patched (the original token stream is
    kept, see ``slz.patch_block``, so the slot does not grow)."""
    new = dict(new)
    for slot, ps in slot_patches(patches).items():
        src = us_entry_of(slot)
        if slot in new:
            raise CodePatchError(f"slot {slot} already modified")
        blob = slz.patch_block(src, patched_slot_data(src, ps))
        new[slot] = blob + b"\0" * (-len(blob) % 4)
    return new


def inject_records(base: int, old: bytes, new: bytes, gap: int = 16) -> bytes:
    """Guarded RAM patch for emulator tests, from a code slot as the reference disc and as the
    test disc decompress it (loaded at ``base``): records ``u32 address, u32 length, old bytes,
    new bytes`` for every run where they differ (runs closer than ``gap`` bytes are joined).
    A savestate made on the reference disc may already hold the slot in RAM, where a later load
    from the test disc never replaces it; the runner writes ``new`` only where RAM still holds
    exactly ``old`` (the slot is resident and unpatched). b"" when the two are the same."""
    if len(old) != len(new):
        raise CodePatchError(f"slot sizes differ: {len(old)} / {len(new)}")
    runs: list[list[int]] = []
    for i in range(len(old)):
        if old[i] != new[i]:
            if runs and i - runs[-1][1] < gap:
                runs[-1][1] = i + 1
            else:
                runs.append([i, i + 1])
    return b"".join(struct.pack("<II", base + a, b - a) + old[a:b] + new[a:b] for a, b in runs)
