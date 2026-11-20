"""Code patches applied to the merged single-disc image.

Each patch names an ISO9660 file, a byte offset in it, the original bytes (verified before patching) and the
replacement. Offsets into SLUS_011.56 are PS-X EXE file offsets: file_off = 0x800 + (vaddr - 0x80010000).

Patch "lid_wait" (SLUS_011.56, function 0x80011F3C)
  The disc-change routine (overlay code, prologue 0x800374C0) shows "Please insert DISC n", stops the drive
  (0x80011E90: Setmode + Stop), then loops: 0x80011F3C waits for the lid to open and close (polling Getstat
  for the shell-open bit), 0x80011FCC re-reads the TOC/header from the new disc, and 0x80011E50
  (file_present) checks the disc marker entry (entry 2 = disc 1, entry 3 = disc 2). The single disc's header
  has both markers, so only the lid wait blocks. It is replaced by: CdSync(0) (let the pending Stop finish),
  then CdControlB(CdlStandby) until accepted, so the motor spins up again (0x80011FCC waits for the
  motor-on status bit) without a lid cycle. 0x80011F3C has no other caller.

  Note the R3000 load delay: `lw ra` must not be followed directly by `jr ra`.

Patch "prompt_blank" (archive entries 1, 2174 and 4806, overlay word 0x800377AC)
  The disc-change routine is linked into three overlays, all loaded at addresses that put it at 0x800374C0:
  entry 1 (base 0x80027F74), entry 2174 (base 0x800324C4) and entry 4806 (base 0x8002F824; the overlay in RAM
  at the end-of-Chapter-4 prompt, matched against a RAM dump). The three copies are identical code; only the
  globals they address differ. All four discs (US and vptrans undub, disc 1 and 2) carry the same three entries.
  The routine blanks the display (SetDispMask(0), libgpu 0x80020D14), uploads the prompt screen (character art
  + "Please insert DISC n") into VRAM, turns the display on with SetDispMask(1) at 0x800377A8 and loops; on exit
  it blanks the display again and the next scene turns it back on. The argument of that SetDispMask(1)
  (`li a0, 1` in the delay slot) becomes 0, so the display stays blank for the ~2 s that the drive needs to
  spin up again. The loop itself (lid_wait, header re-read, marker check) is kept.
  The overlays are SLZ-compressed; src/slz.py rewrites only the tokens that cover the changed word (a few
  bytes longer, the entries keep their sector counts).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import slz

EXE = "SLUS_011.56"
EXE_BASE = 0x80010000
EXE_HDR = 0x800

CD_SYNC = 0x80025464        # libcd CdSync(mode, result)
CD_CONTROL_B = 0x80025718   # libcd CdControlB(com, param, result) -> 1 on success
CDL_STANDBY = 0x07


def exe_off(vaddr: int) -> int:
    return EXE_HDR + vaddr - EXE_BASE


def words(*ws: int) -> bytes:
    return struct.pack(f"<{len(ws)}I", *ws)


def jal(addr: int) -> int:
    return 0x0C000000 | ((addr & 0x0FFFFFFF) >> 2)


@dataclass(frozen=True)
class Patch:
    name: str
    file: str
    offset: int        # byte offset in the file
    orig: bytes
    new: bytes
    desc: str

    def __post_init__(self):
        assert len(self.orig) == len(self.new), self.name


LID_WAIT = Patch(
    name="lid_wait",
    file=EXE,
    offset=exe_off(0x80011F3C),
    orig=words(
        0x27BDFFE0, 0xAFBF0018, 0x24040001, 0x00002821, 0x0C0095C6, 0x27A60010, 0x1040FFFC, 0x24040001,
        0x93A20010, 0x00000000, 0x30420010, 0x1440FFF8, 0x00002821, 0x24040001, 0x00002821),
    new=words(
        0x27BDFFE0,                 # addiu sp, sp, -32
        0xAFBF0018,                 # sw    ra, 24(sp)
        0x00002021,                 # move  a0, zero
        jal(CD_SYNC),               # jal   CdSync            ; wait for the pending Stop
        0x27A50010,                 # addiu a1, sp, 16
        0x24040000 | CDL_STANDBY,   # L: li a0, CdlStandby
        0x00002821,                 # move  a1, zero
        jal(CD_CONTROL_B),          # jal   CdControlB
        0x27A60010,                 # addiu a2, sp, 16
        0x1040FFFB,                 # beqz  v0, L
        0x00000000,                 # nop
        0x8FBF0018,                 # lw    ra, 24(sp)
        0x27BD0020,                 # addiu sp, sp, 32        ; load delay slot
        0x03E00008,                 # jr    ra
        0x00000000),                # nop
    desc="disc-change prompt: spin the drive up instead of waiting for a lid open/close",
)

PATCHES = [LID_WAIT]

SET_DISP_MASK = 0x80020D14  # libgpu SetDispMask(mask): GP1 command 0x03 (display enable)


@dataclass(frozen=True)
class ModulePatch:
    """A patch of an SLZ-compressed archive entry that is loaded at `base` (offsets are RAM addresses)."""
    name: str
    entry: int
    base: int
    vaddr: int
    orig: bytes
    new: bytes
    desc: str

    def __post_init__(self):
        assert len(self.orig) == len(self.new), self.name


# 0x800377A8: jal SetDispMask; li a0, 1 (delay slot); beqz s2, exit; li a1, 2
PROMPT_BLANK_ORIG = words(jal(SET_DISP_MASK), 0x24040001, 0x124000C1, 0x24050002)
PROMPT_BLANK_NEW = words(jal(SET_DISP_MASK), 0x24040000, 0x124000C1, 0x24050002)
MODULE_PATCHES = [
    ModulePatch(f"prompt_blank_{entry}", entry, base, 0x800377A8, PROMPT_BLANK_ORIG, PROMPT_BLANK_NEW,
                "disc-change prompt: keep the display blanked (SetDispMask(1) -> SetDispMask(0))")
    for entry, base in ((1, 0x80027F74), (2174, 0x800324C4), (4806, 0x8002F824))
]


def apply_module(data: bytes, mps: list[ModulePatch]) -> bytes:
    """Apply module patches (all for the same entry) to its SLZ block; already patched data is returned as is."""
    plain = slz.decompress(data)
    changes = {}
    for mp in mps:
        o = mp.vaddr - mp.base
        cur = plain[o:o + len(mp.orig)]
        if cur == mp.new:
            continue
        if cur != mp.orig:
            raise ValueError(f"patch {mp.name}: unexpected bytes {cur.hex()} at {mp.vaddr:#x}")
        changes[o] = mp.new
    return slz.patch(data, changes) if changes else data


def apply(data: bytearray, patch: Patch, base: int = 0) -> None:
    """Apply a patch to a buffer holding the file's bytes starting at file offset `base`."""
    o = patch.offset - base
    cur = bytes(data[o:o + len(patch.orig)])
    if cur == patch.new:
        return
    if cur != patch.orig:
        raise ValueError(f"patch {patch.name}: unexpected bytes {cur.hex()} at file offset {patch.offset:#x}")
    data[o:o + len(patch.new)] = patch.new
