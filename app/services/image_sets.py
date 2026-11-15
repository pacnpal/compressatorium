"""Which files are Images, and which are parts of another Image.

An Image is one convertible unit made of one or more files (``CONTEXT.md``).
A disc sheet (``.cue``, ``.gdi``) owns the track files it references; a tool
whose inputs are a set of files reports the rest of the set through
``ToolPlugin.source_companions`` (jwud: ``game_part2.wud`` ... belong to
``game_part1.wud``). Owned files are never Images of their own. Playlists
(``.m3u``) only point at Images, so they are never Images either.

Shared by anything that lists Images from a folder (the folder Library today).
The sheet parsing is the delete plan's (``utils.delete_plan.sheet_track_paths``),
so what a conversion treats as one Image and what delete-on-verify removes
with it are decided by the same code.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

from services.tools import registry
from utils.delete_plan import sheet_track_paths

# Lists of Images, never Images. Compared on the lowercased suffix.
PLAYLIST_EXTENSIONS = frozenset({".m3u", ".m3u8"})


def is_playlist(path: str) -> bool:
    return Path(path).suffix.lower() in PLAYLIST_EXTENSIONS


def image_members(path: str) -> list[str]:
    """The other files the Image at *path* is made of. Reads the sheet; blocking."""
    members = [*sheet_track_paths(path), *registry.source_companions(path)]
    own = os.path.normpath(path)
    return [m for m in dict.fromkeys(members) if os.path.normpath(m) != own]


def group_images(
    paths: Iterable[str], *, members: Callable[[str], list[str]] = image_members,
) -> list[str]:
    """The Images among *paths*: every file that no other file owns, minus playlists.

    Order is kept. A track no sheet references (a loose ``.bin``) stays an
    Image; whether anything can convert it is the Rule's question. *members*
    is :func:`image_members` unless the caller keeps its own cache of it.
    """
    files = [p for p in paths if not is_playlist(p)]
    owned: set[str] = set()
    for path in files:
        owned.update(os.path.normpath(m) for m in members(path))
    return [p for p in files if os.path.normpath(p) not in owned]
