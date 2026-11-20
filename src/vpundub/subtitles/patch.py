"""Spiritual Concentration subtitles: the world-map module patch (ported from vpsub ``tools/mkpatch.py``).

``overlay.c`` (C, compiled with clang for MIPS I, linked at the end of the world-map module by
``overlay.ld``) is entered from the main loop instead of the vision step function: it calls the step, then
draws the subtitle page of the playing vision clip with the game's own font. The compiled code is kept in
``overlay.bin`` (built from ``overlay.c`` by ``python3 tools/subtitle_overlay.py``; a test recompiles it and
compares when clang is installed), so building the undub needs no compiler. Behind the code come the pages
(``pages.pack``): the cues of ``data/discN.json`` resolved against the disc (``cues.resolve``), timed by the
undub's own (JP) clip lengths (``visions.scan``), with the font cells read from the disc.

Module patch (``patch_module``): slot 4796 decompressed, its length and the hook site (``jal 0x80033FE8`` at
0x80037D10) checked, the call redirected to ``hook``, code + data appended (they must end before the VRAM
save buffer at 0x80083030), recompressed (SLZ type 1). See TECHNICAL.md "Spiritual Concentration subtitles".
"""
from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

from . import cues, pages, visions
from .font import Font, find_font
from ..archive import slz

HERE = Path(__file__).resolve().parent
MODULE_SLOT = 4796
MODULE_BASE = 0x8002F824
MODULE_LEN = 0x258C0
OVERLAY_BASE = MODULE_BASE + MODULE_LEN          # 0x800550E4, = overlay.ld
SAVE_BUF = 0x80083030                            # overlay.c SAVEBUF: VRAM area backup up to the free-RAM end
HOOK_SITE = 0x80037D10
STEP = 0x80033FE8
HOOK = 0x800550F0                                # ``hook`` in overlay.bin (tools/subtitle_overlay.py checks it)
CFLAGS = ["--target=mipsel-none-elf", "-march=mips1", "-mabi=32", "-msoft-float", "-mno-abicalls", "-fno-pic",
          "-G0", "-Os", "-ffreestanding", "-fno-builtin", "-Wall", "-Werror"]


class SubtitleError(RuntimeError):
    pass


def jal(target: int) -> int:
    return 0x0C000000 | (target >> 2) & 0x03FFFFFF


def overlay_code() -> bytes:
    """The compiled overlay (from OVERLAY_BASE up to ``subdata``)."""
    return (HERE / "overlay.bin").read_bytes()


def compile_overlay(work: Path) -> tuple[bytes, dict[str, int]]:
    """(image from OVERLAY_BASE up to ``subdata``, symbols): ``overlay.c`` compiled and linked with
    clang/ld.lld/llvm-objcopy/llvm-nm/llvm-objdump; every load-delay slot is checked."""
    work.mkdir(parents=True, exist_ok=True)
    obj, elf, binf = work / "overlay.o", work / "overlay.elf", work / "overlay.bin"
    subprocess.run(["clang", *CFLAGS, "-c", str(HERE / "overlay.c"), "-o", str(obj)], check=True)
    subprocess.run(["ld.lld", "-T", str(HERE / "overlay.ld"), str(obj), "-o", str(elf)], check=True)
    subprocess.run(["llvm-objcopy", "-O", "binary", "--only-section=.text", "--only-section=.rodata",
                    "--only-section=.data", str(elf), str(binf)], check=True)
    syms = {}
    for line in subprocess.run(["llvm-nm", str(elf)], check=True, capture_output=True, text=True).stdout.split("\n"):
        p = line.split()
        if len(p) == 3:
            syms[p[2]] = int(p[0], 16)
    sect = {}
    for line in subprocess.run(["llvm-objdump", "-h", str(elf)], check=True, capture_output=True,
                               text=True).stdout.split("\n"):
        p = line.split()
        if len(p) >= 4 and p[1].startswith("."):
            sect[p[1]] = (int(p[3], 16), int(p[2], 16))
    text, text_size = sect[".text"]
    data = binf.read_bytes()
    img = b"\0" * (text - OVERLAY_BASE) + data     # objcopy starts at the first section (.text)
    img = img[:syms["subdata"] - OVERLAY_BASE].ljust(syms["subdata"] - OVERLAY_BASE, b"\0")
    check_hazards(img[text - OVERLAY_BASE:], text, text + text_size)
    return img, syms


# --- MIPS I load-delay check ------------------------------------------------------------------------------
LOADS = {0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26}   # lb lh lwl lw lbu lhu lwr


def _reads(w: int) -> set[int]:
    """GPRs read by instruction ``w`` (conservative)."""
    op, rs, rt = w >> 26, w >> 21 & 31, w >> 16 & 31
    if w == 0:
        return set()
    if op == 0:
        fn = w & 63
        if fn in (0, 2, 3):                     # sll srl sra: rt
            return {rt}
        if fn in (8, 9):                        # jr jalr
            return {rs}
        if fn in (16, 18):                      # mfhi mflo
            return set()
        if fn in (17, 19):                      # mthi mtlo
            return {rs}
        return {rs, rt}
    if op in (2, 3):                            # j jal
        return set()
    if op == 1:                                 # bltz/bgez...
        return {rs}
    if op in (4, 5):                            # beq bne
        return {rs, rt}
    if op in (6, 7):                            # blez bgtz
        return {rs}
    if op == 15:                                # lui
        return set()
    if op in (0x22, 0x26):                      # lwl/lwr merge into rt: pairing is allowed
        return {rs}
    if op >= 0x28:                              # stores
        return {rs, rt}
    return {rs}                                 # immediates and loads


def check_hazards(img: bytes, base: int, end: int) -> None:
    n = (end - base) // 4
    code = struct.unpack_from(f"<{n}I", img)
    bad = []
    for i in range(n - 1):
        w = code[i]
        if w >> 26 in LOADS:
            rt = w >> 16 & 31
            nxt = code[i + 1]
            if rt and rt in _reads(nxt) and not (nxt >> 26 in (0x22, 0x26) and nxt >> 16 & 31 == rt):
                bad.append(base + 4 * i)
    if bad:
        raise SubtitleError("load-delay hazards at " + ", ".join(f"{a:#x}" for a in bad))


def patch_module(module: bytes, overlay: bytes, hook: int = HOOK) -> bytes:
    if len(module) != MODULE_LEN:
        raise SubtitleError(f"slot {MODULE_SLOT}: length {len(module):#x}, expected {MODULE_LEN:#x}")
    off = HOOK_SITE - MODULE_BASE
    cur = struct.unpack_from("<I", module, off)[0]
    if cur != jal(STEP):
        raise SubtitleError(f"hook site {HOOK_SITE:#x}: {cur:#010x}, expected jal {STEP:#x}")
    if OVERLAY_BASE + len(overlay) > SAVE_BUF:
        raise SubtitleError("overlay + data do not fit in free RAM")
    out = bytearray(module)
    struct.pack_into("<I", out, off, jal(hook))
    return bytes(out) + overlay


def load_cues(disc: int) -> dict:
    return json.loads((HERE / "data" / f"disc{disc}.json").read_text())


def _disc_data(disc: int, text_archive, voice_archive) -> tuple[dict, dict]:
    """(vision tables by id, resolved cues by id) of one disc."""
    res, problems = cues.resolve(load_cues(disc), text_archive)
    if problems:
        raise SubtitleError(f"disc {disc} subtitles: " + "; ".join(problems))
    vis = {v["id"]: v for v in visions.scan(voice_archive, MODULE_SLOT)["visions"]}
    return vis, {v["id"]: v for v in res["visions"]}


def subtitle_pages(disc: int, text_archive, voice_archive, others=()) -> tuple[dict, dict]:
    """(pages per vision id and clip, vision tables by id). ``text_archive``: the font and the on-disc lines
    (US and undub hold the same); ``voice_archive``: the undub, whose clip lengths time the pages.
    ``others``: ``(disc, text_archive, voice_archive)`` of the other discs. With them every vision is taken
    from a disc that holds all its clips (``pages.merge_discs``), so all discs get the same subtitle data
    (vision 16, Shiho, chapter 5 or 6, has only 1 of its 7 clips on disc 1)."""
    own = _disc_data(disc, text_archive, voice_archive)
    if others:
        try:
            vis, subs = pages.merge_discs([own] + [_disc_data(*o) for o in others])
        except ValueError as e:
            raise SubtitleError(str(e)) from e
    else:
        vis, subs = own
    font = Font(find_font(text_archive))
    return pages.build_visions(font, subs, vis), vis


def module_slot(disc: int, text_archive, voice_archive, others=()) -> tuple[bytes, dict]:
    """(new SLZ data for slot 4796, summary) for the undub ``voice_archive`` (``others``: see
    ``subtitle_pages``)."""
    visions_pages, _ = subtitle_pages(disc, text_archive, voice_archive, others)
    font = Font(find_font(text_archive))
    code = overlay_code()
    data = pages.pack(font, visions_pages)
    module = slz.decompress(voice_archive[MODULE_SLOT])
    new_module = patch_module(module, code + data)
    comp = slz.compress(new_module, 1)
    if slz.decompress(comp) != new_module:
        raise SubtitleError("SLZ round trip failed")
    npages = sum(len(p or []) for c in visions_pages.values() for p in c)
    return comp, {"pages": npages, "code_bytes": len(code), "data_bytes": len(data),
                  "slot_bytes": [len(voice_archive[MODULE_SLOT]), len(comp)]}


def inject_records(module: bytes) -> bytes:
    """RAM patch for emulator tests from states with the world map already resident, from a patched module
    (decompressed): records ``u32 address, u32 length, bytes``, the overlay first, the hook word last.
    Apply only while the hook site still holds ``jal STEP`` (the unpatched world map is resident)."""
    if len(module) <= MODULE_LEN:
        return b""
    tail = module[MODULE_LEN:]
    hook = module[HOOK_SITE - MODULE_BASE:HOOK_SITE - MODULE_BASE + 4]
    return struct.pack("<II", OVERLAY_BASE, len(tail)) + tail + struct.pack("<II", HOOK_SITE, 4) + hook
