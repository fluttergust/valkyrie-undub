"""End-to-end undub build: US disc + JP disc -> undubbed US disc (+ xdelta patch whose
source is the US and JP images joined, so it holds almost no game data).

    python -m vpundub build --disc 1            # default ROM paths (see README)
    python -m vpundub build --disc 2 --us X.cue --jp Y.cue --out build/out

Pipeline (each step is deterministic):
  1. verify input SHA-1 (Redump) unless --no-verify-input
  2. read VALKYRIE.BIN from both images (disc layer)
  3. voice plan (mapping.voicemap.build_plan) and apply it
  4. rebuild the archive keeping the US file size (filler tail absorbs growth)
  5. Spiritual Concentration subtitles: the world-map module (slot 4796) gets the
     subtitle overlay and pages timed by the undub's clips (subtitles/patch.py), the FMV
     player (pack 4616) the ending-FMV subtitle routine (subtitles/fmvsub.py), and
     the archive is rebuilt with it
  6. rebuild the disc from the US template (VALKYRIE.BIN and the boot
     executable's small code patch, see codepatch.py, change)
  7. verify the output: EDC/ECC, TOC, every replacement, invariants
  8. write <out>/<name>.bin/.cue, plan JSON, manifest JSON, xdelta3 patch
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .archive import slz
from .archive.archive import Archive
from .assets.voice import find_sounds, parse_pack, parse_sound, sound_bytes
from .codepatch import DRIVERS, EXE_NAME, SLOT_PATCHES, apply_slot_patches, patch_exe, whole_limit_patches, patched_slot_data, slot_patches
from .disc.image import DiscImage, write_cue
from .disc.iso9660 import parse, rebuild
from .mapping.scenefix import apply_scene_waits, by_slot, patched_item
from .mapping.voicemap import FIT_CLIPS, apply_plan, build_plan, jp_whole_clip, would_stream

REDUMP_SHA1 = {
    ("us", 1): "fa13cbde448a47fa87b6c7829df1c5e6215f5f66",
    ("us", 2): "7cc65766b46273b4ee166269170ecbaa433908fd",
    ("jp", 1): "463af3fc81befbbf92c89c47b16dac72fc85f30d",
    ("jp", 2): "b992244720af08b97e37c1fba8dc6e86790c7fbc",
}
DEFAULT_CUE = {
    ("us", n): Path(f"roms/us/Valkyrie Profile (USA) (Disc {n})/Valkyrie Profile (USA) (Disc {n}).cue")
    for n in (1, 2)
} | {
    ("jp", n): Path(f"roms/japan/Valkyrie Profile (Japan) (Disc {n})/Valkyrie Profile (Japan) (Disc {n}).cue")
    for n in (1, 2)
}
VERSION = "0.1.0"


class BuildError(RuntimeError):
    pass


def sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _open(p: Path) -> DiscImage:
    return DiscImage.from_cue(p) if p.suffix.lower() == ".cue" else DiscImage(p)


def _archive(img: DiscImage) -> tuple[Archive, int]:
    vol = parse(img)
    e = vol.find("VALKYRIE.BIN")
    return Archive.from_bytes(img.read_form1(e.lba, e.size), base_lba=e.lba), e.lba


def _voice_archive(us: Archive, jp: Archive, base: int) -> Archive:
    """The undub's archive with only the JP voices and scene waits (no code patches, no subtitles): what
    the subtitle timing reads its clip lengths from."""
    new = apply_scene_waits(us, apply_plan(us, jp, build_plan(us, jp)))
    return Archive.from_bytes(us.build(new, keep_size=True), base_lba=base)


def _other_disc(disc: int, verify_input: bool, log) -> tuple[int, Archive, Archive]:
    """``(disc, US archive, undub voice archive)`` of the other disc, from its default images: the
    subtitles take each vision from the disc that holds all its clips (subtitles.pages.merge_discs)."""
    o = 3 - disc
    imgs = {k: _open(Path(DEFAULT_CUE[(k, o)])) for k in ("us", "jp")}
    if verify_input:
        for k, img in imgs.items():
            log(f"hash disc {o} {k} input (subtitle data of both discs)")
            if sha1_file(img.path) != REDUMP_SHA1[(k, o)]:
                raise BuildError(f"{img.path}: not the Redump dump of disc {o} (use --no-verify-input to override)")
    us, base = _archive(imgs["us"])
    jp, _ = _archive(imgs["jp"])
    return o, us, _voice_archive(us, jp, base)


@dataclass
class BuildResult:
    bin_path: Path
    cue_path: Path
    manifest: dict


def verify_subtitle_slot(us_data: bytes, out_data: bytes) -> None:
    """The world-map module: the US module with only the vision-step call redirected, then the overlay."""
    from .subtitles import patch as sp
    u, a = slz.decompress(us_data), slz.decompress(out_data)
    h = sp.HOOK_SITE - sp.MODULE_BASE
    if len(u) != sp.MODULE_LEN or a[:h] != u[:h] or a[h + 4:sp.MODULE_LEN] != u[h + 4:]:
        raise BuildError(f"slot {sp.MODULE_SLOT}: module changed outside the hook site")
    if a[h:h + 4] != sp.jal(sp.HOOK).to_bytes(4, "little"):
        raise BuildError(f"slot {sp.MODULE_SLOT}: hook site not redirected")
    if not a[sp.MODULE_LEN:].startswith(sp.overlay_code()) or sp.OVERLAY_BASE + len(a) - sp.MODULE_LEN > sp.SAVE_BUF:
        raise BuildError(f"slot {sp.MODULE_SLOT}: overlay missing or too large")


def verify_fmv_slot(us_data: bytes, out_data: bytes) -> None:
    """Pack 4616: the US pack with only the FMV player's hook pair changed and the routine appended."""
    from .subtitles import fmvsub
    try:
        fmvsub.verify_slot(us_data, out_data)
    except fmvsub.FmvError as e:
        raise BuildError(str(e)) from e


def verify_output(out_img: DiscImage, us: Archive, jp: Archive, plan, new: dict[int, bytes],
                  log=print, subtitle_slots: dict | None = None, fit: frozenset = FIT_CLIPS,
                  code_patches: tuple = SLOT_PATCHES) -> dict:
    """Independent checks on the written image. Raises BuildError on failure."""
    from .disc.__main__ import verify_image

    log("verify: EDC/ECC and headers of every sector")
    bad = verify_image(out_img)
    if bad:
        raise BuildError(f"{len(bad)} sectors with bad EDC/ECC, first {bad[:5]}")
    log("verify: archive TOC and contents")
    arc, _ = _archive(out_img)
    if arc.order != us.order:
        raise BuildError("slot order changed")
    for slot in us.order:
        expect = new.get(slot, us[slot])
        if arc[slot].rstrip(b"\0") != expect.rstrip(b"\0"):
            raise BuildError(f"slot {slot}: content mismatch")
    log("verify: replacements and invariants")
    for r in plan.replacements:
        refs = [x for x in find_sounds(r.slot, arc[r.slot]) if x.item == r.item]
        if len(refs) != 1 or sound_bytes(arc[r.slot], refs[0]) != jp_whole_clip(jp, r, fit)[0]:
            raise BuildError(f"slot {r.slot} item {r.item}: replacement not applied")
    seq = {sp.slot for sp in plan.sequence_packs}
    code = slot_patches(code_patches)
    for slot in new:
        u, a = us[slot], arc[slot]
        if slot in (subtitle_slots or {}):
            subtitle_slots[slot](u, a)
            continue
        if slot in code:
            if slz.decompress(a) != patched_slot_data(u, code[slot]):
                raise BuildError(f"slot {slot}: code patch mismatch")
            continue
        if slz.is_slz(u) or u[:4] == b"\x60\x01\x01\x80":
            raise BuildError(f"slot {slot}: code/video entry modified")
        if (parse_sound(u) is None) != (parse_sound(a) is None) or \
           (parse_pack(u) is None) != (parse_pack(a) is None):
            raise BuildError(f"slot {slot}: entry kind changed")
        if parse_pack(u) and slot not in seq:
            if [i.tag for i in parse_pack(u)] != [i.tag for i in parse_pack(a)]:
                raise BuildError(f"slot {slot}: pack layout changed")
            voice = {x.item for x in find_sounds(slot, u)}
            scene = by_slot().get(slot, {})
            for i, (x, y) in enumerate(zip(parse_pack(u), parse_pack(a))):
                if i in scene:
                    if slz.decompress(a[y.offset:y.offset + y.size]) != patched_item(u, i, scene[i]):
                        raise BuildError(f"slot {slot} item {i}: scene timing patch mismatch")
                    continue
                if i not in voice and u[x.offset:x.offset + x.size] != a[y.offset:y.offset + y.size]:
                    raise BuildError(f"slot {slot} item {i}: non-voice item modified")
    return {"sectors_checked": len(out_img), "slots_checked": len(us.order)}


def make_patch(sources: list[Path], dst: Path, patch: Path, log=print) -> str | None:
    """xdelta3 patch from the files in `sources`, joined in that order (US then JP disc image),
    to `dst`. The JP voice data the undub copies is then a copy from the source, not data in
    the patch; what the patch still holds is listed in TECHNICAL.md ("Patch"). To apply:
    join the same images in the same order and run `xdelta3 -d -s <joined> <patch> <out>`.
    The patch is decoded once here and checked against `dst`."""
    exe = shutil.which("xdelta3")
    if exe is None:
        log("xdelta3 not found: skipping patch")
        return None
    joined, check = patch.with_suffix(".source.tmp"), patch.with_suffix(".check.tmp")
    try:
        with open(joined, "wb") as out:
            for src in sources:
                with open(src, "rb") as f:
                    shutil.copyfileobj(f, out, 1 << 24)
        patch.unlink(missing_ok=True)
        # -B: source window over the whole joined source (default 64 MiB would miss the JP part);
        # -A: no application header (it holds the file names, which would make the patch depend on them)
        window = str(max(1 << 26, joined.stat().st_size))
        subprocess.run([exe, "-e", "-9", "-f", "-A", "-B", window, "-s", str(joined), str(dst), str(patch)], check=True)
        subprocess.run([exe, "-d", "-f", "-s", str(joined), str(patch), str(check)], check=True)
        if sha1_file(check) != sha1_file(dst):
            raise BuildError(f"{patch}: decoding the patch does not give {dst}")
    finally:
        joined.unlink(missing_ok=True)
        check.unlink(missing_ok=True)
    return sha1_file(patch)


def build_disc(disc: int, us_path: Path | None = None, jp_path: Path | None = None,
               out_dir: Path = Path("build/out"), verify_input: bool = True,
               patch: bool = True, log=print, code_patches: bool = True,
               subtitles: bool = True, driver: str = "wait", fit_clips: bool = True,
               whole_sectors: int = 15) -> BuildResult:
    """Build one undubbed disc. code_patches=False leaves the boot exe
    and the engine slots 1490/2292 as US (experiment builds only; the release build always applies them).
    subtitles=False leaves out the Spiritual Concentration subtitles (world-map module as US).
    driver: the boot-exe sound driver patch, "wait" (release) or "overlap" (codepatch.DRIVER_OVERLAP,
    trial); fit_clips=False keeps every JP clip as is (no FIT_CLIPS resampling, e.g. slot 1630).
    whole_sectors > 15 (trial): battle voice clips of up to that many sectors are uploaded whole
    instead of streamed (codepatch.whole_limit_patches)."""
    fit = FIT_CLIPS if fit_clips else frozenset()
    slot_code = SLOT_PATCHES + whole_limit_patches(whole_sectors)
    us_path = Path(us_path or DEFAULT_CUE[("us", disc)])
    jp_path = Path(jp_path or DEFAULT_CUE[("jp", disc)])
    us_img, jp_img = _open(us_path), _open(jp_path)
    inputs = {"us": {"path": str(us_img.path), "sha1": None}, "jp": {"path": str(jp_img.path), "sha1": None}}
    if verify_input:
        for key, img in (("us", us_img), ("jp", jp_img)):
            log(f"hash {key} input")
            h = sha1_file(img.path)
            inputs[key]["sha1"] = h
            if h != REDUMP_SHA1[(key, disc)]:
                raise BuildError(f"{img.path}: SHA-1 {h} is not the Redump dump "
                                 f"{REDUMP_SHA1[(key, disc)]} (use --no-verify-input to override)")

    log("read archives")
    us, base = _archive(us_img)
    jp, _ = _archive(jp_img)
    log("build voice plan")
    plan = build_plan(us, jp)
    new = apply_scene_waits(us, apply_plan(us, jp, plan, fit))
    if code_patches:
        new = apply_slot_patches(lambda s: us[s], new, slot_code)
    else:
        log("code patches disabled (experiment build)")
    blob = us.build(new, keep_size=True)
    sub_info, sub_slots = None, {}
    if subtitles:
        from .subtitles import patch as sp
        log("Spiritual Concentration subtitles")
        try:
            others = [_other_disc(disc, verify_input, log)]
            comp, sub_info = sp.module_slot(disc, us, Archive.from_bytes(blob, base_lba=base), others)
        except sp.SubtitleError as e:
            raise BuildError(str(e)) from e
        from .subtitles import fmvsub
        log(f"subtitles: {sub_info['pages']} pages, overlay {sub_info['code_bytes']} B + data "
            f"{sub_info['data_bytes']} B, slot {sp.MODULE_SLOT} {sub_info['slot_bytes'][0]} -> "
            f"{sub_info['slot_bytes'][1]} B")
        log("ending FMV subtitles")
        try:
            fslot, sub_info["fmv"] = fmvsub.fmv_slot(us)
        except fmvsub.FmvError as e:
            raise BuildError(str(e)) from e
        log(f"FMV subtitles: {sub_info['fmv']['cues']} lines, {sub_info['fmv']['code_bytes']} B code + "
            f"{sub_info['fmv']['data_bytes']} B data in slot {fmvsub.SLOT}, {sub_info['fmv']['slot_bytes_left']} B left")
        sub_slots = {sp.MODULE_SLOT: verify_subtitle_slot, fmvsub.SLOT: verify_fmv_slot}
        new = {**new, sp.MODULE_SLOT: comp, fmvsub.SLOT: fslot}
        blob = us.build(new, keep_size=True)
    if len(blob) != us.size_sectors * 2048:
        raise BuildError("archive grew beyond the original size (filler tail exhausted)")
    tail_left = len(Archive.from_bytes(blob, base_lba=base).tail) // 2048

    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"Valkyrie Profile (USA) (Disc {disc}) [Undub]"
    bin_path, cue_path = out_dir / f"{name}.bin", out_dir / f"{name}.cue"
    log(f"write {bin_path}")
    us_vol = parse(us_img)
    exe = us_vol.read(EXE_NAME[disc])
    exe = patch_exe(exe, DRIVERS[driver]) if code_patches else exe
    rebuild(us_vol, str(bin_path), {"VALKYRIE.BIN": blob, EXE_NAME[disc]: exe})
    write_cue(bin_path, cue_path)

    out_img = DiscImage(bin_path)
    checks = verify_output(out_img, us, jp, plan, new, log, sub_slots, fit, slot_code)
    if len(out_img) != len(us_img):
        raise BuildError("output image size differs from the US image")

    fitted, streamed = {}, []
    for r in plan.replacements:
        obj, k = jp_whole_clip(jp, r, fit)
        if k:
            fitted[r.slot] = k
        elif would_stream(r, obj):
            streamed.append(r.slot)
    log(f"clips resampled to stay whole: {fitted}; streamed where US is whole: {len(streamed)}")

    out_sha1 = sha1_file(bin_path)
    plan_json = plan.to_json()
    (out_dir / f"plan_disc{disc}.json").write_text(json.dumps(plan_json, indent=1))
    patch_sha1 = None
    patch_path = out_dir / f"vpundub_disc{disc}.xdelta"
    if patch:
        log("create xdelta3 patch (source: US image + JP image, joined)")
        patch_sha1 = make_patch([us_img.path, jp_img.path], bin_path, patch_path, log)
    manifest = {
        "tool": "vpundub", "version": VERSION, "disc": disc,
        "inputs": inputs,
        "output": {"bin": bin_path.name, "sha1": out_sha1, "sectors": len(out_img)},
        "patch": {"file": patch_path.name, "sha1": patch_sha1,
                  "source": [Path(us_img.path).name, Path(jp_img.path).name]} if patch_sha1 else None,
        "plan_summary": plan_json["summary"],
        "fitted_clips": fitted,
        "streamed_clips": streamed,
        "code_patches": code_patches,
        "driver": driver if code_patches else None,
        "whole_sectors": whole_sectors if code_patches else None,
        "subtitles": sub_info,
        "filler_tail_sectors_left": tail_left,
        "checks": checks,
    }
    (out_dir / f"manifest_disc{disc}.json").write_text(json.dumps(manifest, indent=1))
    log(f"done: {bin_path.name} sha1 {out_sha1}")
    return BuildResult(bin_path, cue_path, manifest)
