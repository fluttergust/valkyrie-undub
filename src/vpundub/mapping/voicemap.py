"""Build the US -> JP voice replacement plan for one disc.

Every voice in the game lives in a *sound object* (see ``assets/voice.py``),
either as a whole archive entry or as an item inside a pack. US and JP sound
objects are matched by their tuple of sample ids, which is unique per content:
when several JP objects share a key, their bytes are identical, so the match is
deterministic.

Policy (decided in Phase 3, see TECHNICAL.md):

* ``clip`` / ``table`` objects (dialogue and scene voices): replace with the JP
  object when matched and different. This includes the object's sound script,
  whose wait/timing commands are tuned to the clip length.
* Pack items: only the sound-object item is replaced. All other items (code,
  graphics, SLZ data) stay US, because packs contain region-specific code.
* Pure-voice *sequence packs* (every item is a sound object): when a JP pack
  holds all of the US clips in the same order, is itself pure voice and has
  the same number of 0x270f sync tags, the whole JP pack is used (Phase 5).
  The JP recordings split many lines into extra segments (with small delay
  tags) between the same sync points; per-clip replacement would cut those
  lines short.
* ``slz`` objects (battle sound banks): these are shared sound effects (all but
  2 samples identical across regions; the US has extra samples). Keep US.
  Banks whose matched samples differ are listed for review.
* FMV audio (clip groups 126/127, stereo pairs next to STR entries): replaced
  when matched, but flagged ``review``, because the video timing must be
  checked in Phase 6.
* Unmatched US objects stay US and are listed.
* A clip's sound script is a short note sequence (``3c <len> <gate> 7f`` notes
  of up to 255 frames, ``8d``/``8e n`` loops, ``80`` end) whose total length is
  the clip duration, so the JP script is kept with the JP audio. Scenes that
  hold a text box for a fixed time shorter than the JP line are retimed in
  ``scenefix.py`` (message waits set to the JP release's values).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..archive.archive import Archive
from ..assets.clipfit import WHOLE_LIMIT, fit_clip
from ..assets.voice import (SoundRef, build_pack, find_sounds, item_bytes, pad_pack_item,
                            parse_pack, sound_bytes)

FMV_GROUPS = (126, 127)
SYNC_TAG = 0x270F


@dataclass
class Replacement:
    slot: int
    item: int | None
    kind: str
    key: list[int]
    jp_slot: int
    jp_item: int | None
    us_len: int
    jp_len: int
    us_adpcm: int
    jp_adpcm: int
    category: str            # "voice" | "fmv-audio"
    us_compressed: bool = False
    us_slz_type: int | None = None
    jp_compressed: bool = False

    @property
    def delta(self) -> int:
        return self.jp_len - self.us_len


@dataclass
class SequencePack:
    slot: int
    jp_slot: int
    us_tags: list[int]
    jp_tags: list[int]
    us_len: int
    jp_len: int

    @property
    def delta(self) -> int:
        return self.jp_len - self.us_len


@dataclass
class VoicePlan:
    replacements: list[Replacement] = field(default_factory=list)
    sequence_packs: list[SequencePack] = field(default_factory=list)
    identical: int = 0
    kept_battle_banks: list[int] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        reps = [asdict(r) | {"delta": r.delta} for r in self.replacements]
        return {
            "summary": {
                "replacements": len(self.replacements),
                "standalone": sum(1 for r in self.replacements if r.item is None),
                "pack_items": sum(1 for r in self.replacements if r.item is not None),
                "sequence_packs": len(self.sequence_packs),
                "fmv_audio": sum(1 for r in self.replacements if r.category == "fmv-audio"),
                "identical_to_jp": self.identical,
                "kept_battle_banks": len(self.kept_battle_banks),
                "review": len(self.review),
                "unmatched": len(self.unmatched),
                "bytes_delta": sum(r.delta for r in self.replacements)
                + sum(p.delta for p in self.sequence_packs),
            },
            "replacements": reps,
            "sequence_packs": [asdict(p) | {"delta": p.delta} for p in self.sequence_packs],
            "kept_battle_banks": self.kept_battle_banks,
            "review": self.review,
            "unmatched": self.unmatched,
        }


def _obj_bytes(arc: Archive, ref: SoundRef) -> bytes:
    return sound_bytes(arc[ref.slot], ref)


def _samples(arc: Archive, ref: SoundRef) -> dict[int, bytes]:
    b = _obj_bytes(arc, ref)          # object bytes, offset 0 = object start
    o = ref.obj
    base = o.script_end + 4 + o.table_size
    return {s.sample_id: b"".join(b[base + r.adpcm_offset:base + r.adpcm_offset + r.adpcm_length]
                                  for r in s.regions) for s in o.samples}


def scan(arc: Archive) -> list[SoundRef]:
    return [r for s in arc.order for r in find_sounds(s, arc[s])]


def _pure_packs(arc: Archive) -> dict[int, list[SoundRef]]:
    """Packs whose every item is a sound object."""
    out = {}
    for s in arc.order:
        items = parse_pack(arc[s])
        if items:
            refs = find_sounds(s, arc[s])
            if refs and len(refs) == len(items):
                out[s] = refs
    return out


def _find_sequences(us: Archive, jp: Archive) -> dict[int, SequencePack]:
    jp_pure = _pure_packs(jp)
    by_key: dict[tuple, set[int]] = {}
    for s, refs in jp_pure.items():
        for r in refs:
            by_key.setdefault(r.obj.key, set()).add(s)
    seqs = {}
    for s, refs in _pure_packs(us).items():
        keys = [r.obj.key for r in refs]
        cands = set.intersection(*(by_key.get(k, set()) for k in keys))
        for js in sorted(cands):
            jkeys = [r.obj.key for r in jp_pure[js]]
            pos = [jkeys.index(k) for k in keys]
            us_tags = [i.tag for i in parse_pack(us[s])]
            jp_tags = [i.tag for i in parse_pack(jp[js])]
            if pos == sorted(pos) and us_tags.count(SYNC_TAG) == jp_tags.count(SYNC_TAG):
                seqs[s] = SequencePack(s, js, us_tags, jp_tags, len(us[s]), len(jp[js]))
                break
    return seqs


def build_plan(us: Archive, jp: Archive) -> VoicePlan:
    us_refs = scan(us)
    seqs = _find_sequences(us, jp)
    jp_all: dict[tuple, list[SoundRef]] = {}
    for r in scan(jp):
        lst = jp_all.setdefault(r.obj.key, [])
        if lst and _obj_bytes(jp, lst[0]) != _obj_bytes(jp, r):
            raise ValueError(f"ambiguous JP key {r.obj.key}: slots {lst[0].slot}/{r.slot} differ")
        lst.append(r)

    def pick(ref: SoundRef) -> SoundRef | None:
        """Prefer a JP copy stored the same way (standalone / raw item / SLZ item)."""
        lst = jp_all.get(ref.obj.key)
        if not lst:
            return None
        for want in ((ref.item is None, ref.compressed), (None, ref.compressed)):
            for c in lst:
                if (want[0] is None or (c.item is None) == want[0]) and c.compressed == want[1]:
                    return c
        return lst[0]

    plan = VoicePlan()
    for s_, sp in sorted(seqs.items()):
        if us[s_].rstrip(b"\0") == jp[sp.jp_slot].rstrip(b"\0"):
            plan.identical += 1
        else:
            plan.sequence_packs.append(sp)
    for r in us_refs:
        if r.slot in seqs:
            continue
        o = r.obj
        loc = {"slot": r.slot, "item": r.item, "kind": o.script_kind,
               "key": [f"{x:#06x}" for x in o.key]}
        j = pick(r)
        if j is None:
            plan.unmatched.append(loc)
            continue
        ub, jb = _obj_bytes(us, r), _obj_bytes(jp, j)
        if ub == jb:
            plan.identical += 1
            continue
        if o.script_kind == "slz":
            plan.kept_battle_banks.append(r.slot)
            us_s, jp_s = _samples(us, r), _samples(jp, j)
            diff = [f"{k:#06x}" for k in us_s if k in jp_s and us_s[k] != jp_s[k]]
            if diff:
                plan.review.append(loc | {"reason": "battle bank samples differ", "samples": diff,
                                          "jp_slot": j.slot})
            continue
        if r.item is None:
            # A whole-entry replacement is only safe if the entry holds nothing
            # but the object plus zero padding.
            tail = us[r.slot][o.offset + o.length:]
            if any(tail):
                plan.review.append(loc | {"reason": "data after sound object in entry"})
                continue
        elif not r.compressed and pad_pack_item(ub, o) != item_bytes(us[r.slot], r.item):
            plan.review.append(loc | {"reason": "pack item does not follow the padding rule"})
            continue
        cat = "fmv-audio" if o.script_kind == "clip" and o.group in FMV_GROUPS else "voice"
        if cat == "fmv-audio":
            plan.review.append(loc | {"reason": "FMV audio: verify sync with US video",
                                      "jp_slot": j.slot})
        plan.replacements.append(Replacement(
            r.slot, r.item, o.script_kind, list(o.key), j.slot, j.item,
            len(ub), len(jb), o.adpcm_size, j.obj.adpcm_size, cat,
            r.compressed, r.slz_type, j.compressed))
    plan.replacements.sort(key=lambda x: (x.slot, -1 if x.item is None else x.item))
    _size_guard(plan, us, jp)
    return plan


def _sectors(n: int) -> int:
    return (n + 2047) // 2048


# Mixed packs checked in the emulator that may grow past the size guard: slot ->
# largest allowed size in sectors. Slot 4795 (ending/staff roll pack, voice item
# 227 = sample 0x11BA, JP slot 4912 item 275) is loaded into the top of an 802 KB
# free heap block, placed by its file size (US 229 sectors fill 469 KB of it);
# the JP clip makes it 231 sectors. Tested from the end of the TAS through the
# credits (TECHNICAL.md, "Size guard exception").
SIZE_GUARD_OK: dict[int, int] = {4795: 231}


def _size_guard(plan: VoicePlan, us: Archive, jp: Archive) -> None:
    """Never create an entry larger than every version an engine has loaded.

    Standalone clips, sequence packs and banks are JP entries verbatim (the JP
    game loads them as they are). Only mixed packs (US code/graphics + JP clips)
    are new combinations: if the rebuilt pack needs more sectors than both the
    US original and every JP pack its clips come from, its voice items stay US
    and are reported for review.
    """
    by_slot: dict[int, list[Replacement]] = {}
    for r in plan.replacements:
        if r.item is not None:
            by_slot.setdefault(r.slot, []).append(r)
    drop = set()
    for slot, reps in by_slot.items():
        buf = us[slot]
        items = parse_pack(buf)
        payload = [buf[it.offset:it.offset + it.size] for it in items]
        for r in reps:
            payload[r.item] = jp_item_payload(jp, r)
        new = _sectors(len(build_pack(buf, payload)))
        limit = max([len(buf) // 2048] + [len(jp[r.jp_slot]) // 2048 for r in reps]
                    + [SIZE_GUARD_OK.get(slot, 0)])
        if new > limit:
            drop.add(slot)
            plan.review.append({"slot": slot, "item": [r.item for r in reps],
                                "reason": f"size guard: {new} sectors > max(US, JP) {limit}; kept US"})
    plan.replacements = [r for r in plan.replacements if r.slot not in drop or r.item is None]


def _jp_ref(jp: Archive, rep: Replacement) -> SoundRef:
    refs = [r for r in find_sounds(rep.jp_slot, jp[rep.jp_slot]) if r.item == rep.jp_item]
    if len(refs) != 1:
        raise ValueError(f"JP slot {rep.jp_slot} item {rep.jp_item}: sound object not found")
    return refs[0]


def jp_object(jp: Archive, rep: Replacement) -> bytes:
    """The (decompressed) JP sound-object bytes for a replacement."""
    return sound_bytes(jp[rep.jp_slot], _jp_ref(jp, rep))


_FIT_CACHE: dict[tuple[int, int, int], tuple[bytes, int]] = {}

# (US slot, JP slot) of the clips resampled to stay whole. Resampling costs
# treble (6 semitones: 15.6 kHz, 14: 9.8 kHz), and a streamed clip only causes
# a problem where the game loads another voice line while it plays, so only
# clips known to be overlapped are listed. 1630/1753: Hrist battle (ch. 8),
# line 1106 (slot 1496 item 2) loads during it; 3.1 s, 20 -> 15 sectors, -5
# semitones. Same archive slot on both discs.
FIT_CLIPS: frozenset[tuple[int, int]] = frozenset({(1630, 1753)})


def would_stream(rep: Replacement, obj: bytes) -> bool:
    """True if the JP clip is streamed where the US clip was uploaded whole."""
    return rep.item is None and rep.kind == "clip" and rep.us_len <= WHOLE_LIMIT < len(obj)


def jp_whole_clip(jp: Archive, rep: Replacement, fit: frozenset = FIT_CLIPS) -> tuple[bytes, int]:
    """JP object for a whole-entry replacement, kept under the whole-upload limit.

    A standalone US clip of at most ``WHOLE_LIMIT`` bytes is uploaded to SPU
    RAM in one piece; a bigger one is streamed and is cut off when the game
    reads the CD again (see ``assets.clipfit``). A JP clip that would cross
    the limit is resampled down by whole semitones (pitch field lowered by the
    same amount, so it plays at the same speed). Returns (bytes, semitones);
    only clips listed in ``fit`` (default ``FIT_CLIPS``) are resampled; clips that cannot be
    fitted within ``MAX_SEMITONES`` are returned as is.
    """
    obj = jp_object(jp, rep)
    if (rep.slot, rep.jp_slot) not in fit or not would_stream(rep, obj):
        return obj, 0
    key = (rep.slot, rep.jp_slot, len(obj))
    if key not in _FIT_CACHE:
        try:
            _FIT_CACHE[key] = fit_clip(rep.slot, obj)
        except ValueError:
            _FIT_CACHE[key] = (obj, 0)
    return _FIT_CACHE[key]


def jp_item_payload(jp: Archive, rep: Replacement, fit: frozenset = FIT_CLIPS) -> bytes:
    """Bytes to put in the US pack item / entry for a replacement.

    The JP item is reused verbatim when its encoding (raw or SLZ) matches the
    US item, so tri-Ace's original compression is kept. Otherwise the object
    is re-encoded to match what the US code expects for that item.
    """
    if rep.item is None:
        return jp_whole_clip(jp, rep, fit)[0]        # whole entry; archive pads to a sector
    if not rep.us_compressed:
        ref = _jp_ref(jp, rep)
        return pad_pack_item(sound_bytes(jp[rep.jp_slot], ref), ref.obj)
    if rep.jp_compressed:
        return item_bytes(jp[rep.jp_slot], rep.jp_item)
    from ..archive import slz
    return slz.compress(jp_object(jp, rep), rep.us_slz_type or 2)


def apply_plan(us: Archive, jp: Archive, plan: VoicePlan, fit: frozenset = FIT_CLIPS) -> dict[int, bytes]:
    """Return {slot: new entry bytes} for every slot touched by the plan.

    Standalone entries become the JP object; packs are rebuilt with only their
    sound-object items swapped (all other items byte-identical to US).
    """
    by_slot: dict[int, list[Replacement]] = {}
    for r in plan.replacements:
        by_slot.setdefault(r.slot, []).append(r)
    out: dict[int, bytes] = {}
    for sp in plan.sequence_packs:
        out[sp.slot] = jp[sp.jp_slot]
    for slot, reps in sorted(by_slot.items()):
        whole = [r for r in reps if r.item is None]
        if whole:
            out[slot] = jp_item_payload(jp, whole[0], fit)
            continue
        buf = us[slot]
        items = parse_pack(buf)
        payload = [buf[it.offset:it.offset + it.size] for it in items]
        for r in reps:
            payload[r.item] = jp_item_payload(jp, r)
        out[slot] = build_pack(buf, payload)
    return out
