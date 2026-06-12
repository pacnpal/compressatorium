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

from services.disc_id import parse_sfo_keys, read_iso_file


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
    """Read a PARAM.SFO file's UTF-8 string keys via the shared SFO parser."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return {}
    return parse_sfo_keys(data)


def ps3_title_id(path: str) -> str | None:
    """Best-effort TITLE_ID for a PS3 disc/JB folder (verify/info readback)."""
    keys = read_sfo_keys(os.path.join(path, "PS3_GAME", "PARAM.SFO"))
    return keys.get("TITLE_ID") or None


def ps3_folder_sfo_keys(path: str) -> dict[str, str]:
    """All PARAM.SFO string keys for a PS3 disc/JB folder (info display)."""
    return read_sfo_keys(os.path.join(path, "PS3_GAME", "PARAM.SFO"))


def ps3_iso_title_id(iso_path: str) -> str | None:
    """Best-effort TITLE_ID read back from a built PS3 ``.iso``.

    The light, no-native-verify integrity check: walk the produced ISO 9660
    image to ``PS3_GAME/PARAM.SFO`` and read its ``TITLE_ID`` (reusing the
    shared ISO reader + SFO parser), so the makeps3iso service can confirm the
    packed ISO carries the same title id as the source folder. Returns ``None``
    when the image isn't a readable PS3 ISO.
    """
    data = read_iso_file(iso_path, ["PS3_GAME", "PARAM.SFO"])
    if not data:
        return None
    return parse_sfo_keys(data).get("TITLE_ID") or None
