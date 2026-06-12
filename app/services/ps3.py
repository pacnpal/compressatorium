"""PS3 decrypted/JB folder detection for the makeps3iso folder->iso seam.

Phase 2 scaffolding. The codebase keys input off ``Path(filename).suffix``; a
folder has no suffix, so this module is the directory analogue of an extension
check — the predicate a future ``MakePs3IsoTool.accepts_directory`` runs.

A folder is a valid makeps3iso source only when it carries the disc/JB layout (a
``PS3_GAME/`` root, plus ``PS3_DISC.SFB`` for disc rips). A bare TITLEID
directory that merely holds a ``PARAM.SFO`` — an installed / HDD ``game/``
game-data layout — is NOT something makeps3iso can package, so it is rejected
rather than advertised.
"""
from __future__ import annotations

import os
import struct

_SFO_MAGIC = b"\x00PSF"
_SFO_FMT_UTF8 = 0x0204


def is_ps3_iso_source(path: str) -> bool:
    """Whether ``path`` is a decrypted PS3 disc/JB folder makeps3iso can pack."""
    if not os.path.isdir(path):
        return False
    ps3_game = os.path.join(path, "PS3_GAME")
    if not os.path.isdir(ps3_game):
        return False
    # A disc rip carries PS3_DISC.SFB at the root; a JB game folder at least has
    # PS3_GAME/PARAM.SFO. Either proves the disc/JB layout — a folder with only
    # a PARAM.SFO under a TITLEID (and no PS3_GAME root) is intentionally not
    # matched here.
    if os.path.isfile(os.path.join(path, "PS3_DISC.SFB")):
        return True
    return os.path.isfile(os.path.join(ps3_game, "PARAM.SFO"))


def read_sfo_keys(path: str) -> dict[str, str]:
    """Return the UTF-8 string keys of a PS3/PSP PARAM.SFO (PSF container).

    A small, format-only reader (no platform assumptions), so PS3's
    ``TITLE_ID`` key is read the same way the PSP path reads ``DISC_ID``.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return {}
    if len(data) < 20 or data[:4] != _SFO_MAGIC:
        return {}
    try:
        key_table_off = struct.unpack_from("<I", data, 8)[0]
        data_table_off = struct.unpack_from("<I", data, 12)[0]
        num_entries = struct.unpack_from("<I", data, 16)[0]
    except struct.error:
        return {}

    out: dict[str, str] = {}
    for i in range(num_entries):
        entry_off = 20 + i * 16
        if entry_off + 16 > len(data):
            break
        key_off = struct.unpack_from("<H", data, entry_off)[0]
        fmt = struct.unpack_from("<H", data, entry_off + 2)[0]
        data_len = struct.unpack_from("<I", data, entry_off + 4)[0]
        val_off = struct.unpack_from("<I", data, entry_off + 12)[0]

        key_start = key_table_off + key_off
        key_end = data.find(b"\x00", key_start)
        if key_end < 0:
            continue
        key = data[key_start:key_end].decode("ascii", errors="replace")
        if fmt != _SFO_FMT_UTF8:
            continue
        val_start = data_table_off + val_off
        val_end = val_start + data_len
        if val_end > len(data):
            continue
        out[key] = data[val_start:val_end].rstrip(b"\x00").decode(
            "utf-8", errors="replace",
        )
    return out


def ps3_title_id(path: str) -> str | None:
    """Best-effort TITLE_ID for a PS3 disc/JB folder (verify/info readback)."""
    keys = read_sfo_keys(os.path.join(path, "PS3_GAME", "PARAM.SFO"))
    return keys.get("TITLE_ID") or None
