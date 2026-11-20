"""Timing patches for scripted scenes whose JP voice outlasts the US pacing.

Some battle scenes run a small event script stored in their overlay (a pack
item, SLZ compressed). Its records are 5 little-endian u16 words:

* ``02 <msg> <wait> 01 00``: show message ``msg`` and hold it for ``wait`` frames
* ``07 <slot> 00 00 00``:     load archive slot ``slot`` (voice, effect, ...)
* ``63 ...``:                 end

Opcode 07 starts a load task and keeps its handle; the handle is released only
while the script is holding a message, once the task (e.g. a voice clip) has
finished. The next 07 refuses to run while a handle is held. If the message
waits run out before the voice ends, the script spins on that 07 forever: the
scene freezes with the last text box on screen. The US waits fit the US voice
lengths, so the longer JP voice deadlocks the scene.

The fix is data only: the affected message waits are set to the values the
Japanese release uses for the same records, so the full JP line plays. Code
is never touched (hand-edited scene code is what made mugi's undub freeze).
Each patch states the expected US value and must match exactly once, so the
patch fails loudly on an unexpected input.

The final battle (overlay slot 1810, items 0 and 1, both discs) times its
voiced actions with a table of 6-byte records ``<hold> <action> <voice slot>``
(u16 LE, ended by ``.. 9999 9999``): each action holds for ``hold`` frames
while its voice line plays. The US release shortened 17 holds to fit the
English lines, so with JP voices the effects ran ahead of the speech. The
holds are set to the JP values (JP slot 1933, same table).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from ..archive import slz
from ..archive.archive import Archive
from ..assets.voice import build_pack, parse_pack


@dataclass(frozen=True)
class SceneWait:
    slot: int
    item: int
    msg: int
    us_wait: int
    new_wait: int
    note: str


# Ch. 8 Lezard battle (both discs, slot 1988 = scene overlay; the voice is slot
# 1536, sample 0x110A, loaded by the record before msg 0x8C4). JP waits for
# the same records: 0x8C5 300, 0x8C6 340 (JP slot 2111).
SCENE_WAITS: tuple[SceneWait, ...] = (
    SceneWait(1988, 0, 0x8C5, 40, 300, "Valkyrie 'For Lucian!...' (voice slot 1536)"),
    SceneWait(1988, 0, 0x8C6, 220, 340, "Valkyrie '...the one I truly cannot forgive...' (voice slot 1536)"),
)


@dataclass(frozen=True)
class LineHold:
    slot: int
    item: int
    action: int
    voice: int          # US voice slot named by the record
    us_hold: int
    new_hold: int
    note: str = ""


# (action, US voice slot, US hold, JP hold) in the final battle table; the JP
# table names the same actions with JP voice slots (US + 123).
_FINAL_BATTLE = (
    (366, 2081, 128, 417), (330, 2052, 128, 417), (365, 2022, 60, 258),
    (368, 2022, 128, 120), (331, 2084, 128, 120), (367, 2084, 168, 128),
    (332, 2095, 168, 128), (411, 2094, 300, 814), (414, 2094, 400, 758),
    (95, 2091, 400, 758), (369, 2087, 462, 562), (370, 2086, 640, 480),
    (413, 2093, 580, 840), (84, 2089, 580, 840), (89, 2088, 650, 716),
    (91, 2090, 490, 990), (83, 2092, 500, 717),
)
LINE_HOLDS: tuple[LineHold, ...] = tuple(
    LineHold(1810, item, a, v, us, jp, "final battle")
    for item in (0, 1) for a, v, us, jp in _FINAL_BATTLE
)


def _old_new(p: SceneWait | LineHold) -> tuple[bytes, bytes]:
    if isinstance(p, SceneWait):
        return _record(p.msg, p.us_wait), _record(p.msg, p.new_wait)
    return (struct.pack("<3H", p.us_hold, p.action, p.voice),
            struct.pack("<3H", p.new_hold, p.action, p.voice))


def _record(msg: int, wait: int) -> bytes:
    return struct.pack("<5H", 2, msg, wait, 1, 0)


def patch_script(data: bytes, waits) -> bytes:
    """Apply timing patches (``SceneWait`` / ``LineHold``) to a decompressed overlay item."""
    out = bytearray(data)
    for w in waits:
        old, new = _old_new(w)
        hits = [i for i in range(0, len(out) - len(old) + 1, 2) if out[i:i + len(old)] == old]
        if len(hits) != 1:
            raise ValueError(f"slot {w.slot} item {w.item}: {w} found {len(hits)} times (expected 1)")
        out[hits[0]:hits[0] + len(new)] = new
    return bytes(out)


def _items(entry: bytes) -> list[bytes]:
    items = parse_pack(entry)
    if items is None:
        raise ValueError("scene overlay entry is not a pack")
    return [entry[i.offset:i.offset + i.size] for i in items]


def patched_item(us_entry: bytes, item: int, waits) -> bytes:
    """The decompressed, patched overlay item (what the game should see in RAM)."""
    return patch_script(slz.decompress(_items(us_entry)[item]), waits)


def by_slot(waits=SCENE_WAITS + LINE_HOLDS) -> dict[int, dict[int, list]]:
    out: dict[int, dict[int, list]] = {}
    for w in waits:
        out.setdefault(w.slot, {}).setdefault(w.item, []).append(w)
    return out


def apply_scene_waits(us: Archive, new: dict[int, bytes], waits=SCENE_WAITS + LINE_HOLDS) -> dict[int, bytes]:
    """Return ``new`` with the scene patches applied (slots absent from ``us`` are skipped)."""
    new = dict(new)
    for slot, per_item in by_slot(waits).items():
        if slot not in us.order:
            continue
        src = new.get(slot, us[slot])
        raw = _items(src)
        for item, ws in per_item.items():
            typ = slz.header(raw[item])[0]
            data = patch_script(slz.decompress(raw[item]), ws)
            comp = slz.compress(data, typ)
            if slz.decompress(comp) != data:
                raise ValueError(f"slot {slot} item {item}: SLZ round trip failed")
            raw[item] = comp + b"\0" * (-len(comp) % 4)
        new[slot] = build_pack(src, raw)
    return new
