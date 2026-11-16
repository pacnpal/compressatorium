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
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from services.tools import registry
from utils.delete_plan import sheet_track_paths
from utils.path_utils import path_fingerprint

# Lists of Images, never Images. Compared on the lowercased suffix.
PLAYLIST_EXTENSIONS = frozenset({".m3u", ".m3u8"})


def is_playlist(path: str) -> bool:
    return Path(path).suffix.lower() in PLAYLIST_EXTENSIONS


def image_members(path: str) -> list[str]:
    """The other files the Image at *path* is made of. Reads the sheet; blocking."""
    members = [
        *sheet_track_paths(path), *registry.source_companions(path),
        # A split output (`Game.iso.0` owns `.1`, `.2`): one logical Image.
        *registry.split_members(path),
    ]
    own = os.path.normpath(path)
    return [m for m in dict.fromkeys(members) if os.path.normpath(m) != own]


SHEET_EXTENSIONS = (".cue", ".gdi")


class SheetOwnerUnknown(Exception):
    """Whether a sheet owns the file could not be told: a folder on the way
    could not be listed, or a sheet in it could not be read whole."""


def owning_sheet(
    path: str, *, stop_at: str, progress: Callable[..., None] | None = None,
) -> str | None:
    """The ``.cue``/``.gdi`` that lists *path* as a track, or None. Blocking.

    The other direction of :func:`image_members`: a sheet may name a track in
    a folder below it (``Disc.cue`` → ``tracks/Track.bin``), so this looks in
    *path*'s own folder and every folder above it, up to and including
    *stop_at* (the Volume boundary, ``lock_boundary_root``), never above. The
    sheet's tracks are resolved as everywhere else (``sheet_track_paths``) and
    compared by real path.

    Raises :class:`SheetOwnerUnknown` when the answer is unknown: a folder on
    the way that cannot be listed, or a sheet in one that cannot be read whole
    (``sheet_is_readable``: unreadable, or over the size cap). A caller about
    to delete *path* treats that as "it may be a track", never as "standalone".
    *progress* is called per folder and per sheet, for a stall-bounded caller.
    """
    from utils.sheet_text import sheet_is_readable

    tick = progress or (lambda *_args: None)
    real = os.path.realpath(path)
    boundary = os.path.normpath(os.path.realpath(stop_at))
    folder = os.path.normpath(os.path.dirname(real))
    if folder != boundary and not folder.startswith(boundary.rstrip(os.sep) + os.sep):
        raise SheetOwnerUnknown(f"{path} is not under {stop_at}")
    while True:
        tick(folder)
        try:
            names = sorted(os.listdir(folder))
        except OSError as exc:
            raise SheetOwnerUnknown(
                f"{folder} could not be listed ({exc.strerror or exc})",
            ) from exc
        for name in names:
            if not name.lower().endswith(SHEET_EXTENSIONS):
                continue
            sheet = os.path.join(folder, name)
            tick(sheet)
            if os.path.isdir(sheet):
                continue
            if not sheet_is_readable(sheet):
                raise SheetOwnerUnknown(f"{name} could not be read (unreadable or too large)")
            if real in {os.path.realpath(track) for track in sheet_track_paths(sheet)}:
                return sheet
        if folder == boundary:
            return None
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def group_images(
    paths: Iterable[str], *, members: Callable[[str], list[str]] = image_members,
) -> list[str]:
    """The Images among *paths*: every file that no other file owns, minus playlists.

    Order is kept. A track no sheet references (a loose ``.bin``) stays an
    Image; whether anything can convert it is the Rule's question. *members*
    is :func:`image_members` unless the caller keeps its own cache of it.

    A sheet may name a track in another case (``FILE "game.bin"`` beside
    ``Game.bin``). It owns that file only where the filesystem says the two
    names are one file (``samefile``, on a case-insensitive share); on a
    case-sensitive one they are different names and nothing is hidden.
    """
    files = [p for p in paths if not is_playlist(p)]
    owned: set[str] = set()
    owned_folded: dict[str, str] = {}
    for path in files:
        for member in members(path):
            key = os.path.normpath(member)
            owned.add(key)
            owned_folded.setdefault(key.casefold(), key)

    def is_owned(path: str) -> bool:
        key = os.path.normpath(path)
        if key in owned:
            return True
        other = owned_folded.get(key.casefold())
        return other is not None and _same_file(other, key)

    return [p for p in files if not is_owned(p)]


def image_files(
    path: str, *, members: Callable[[str], list[str]] = image_members,
) -> list[str]:
    """Every file of the Image at *path*: the file itself, then its members."""
    return [path, *members(path)]


def image_size(
    path: str, *, members: Callable[[str], list[str]] = image_members,
    known: Mapping[str, os.stat_result] | None = None,
) -> int:
    """Bytes of every file of the Image at *path*: a sheet and all its tracks.

    *known* maps normalised paths to stat results the caller already has (a
    walk's), so only a member it did not list is stat'ed. A member that is
    missing counts as nothing. Blocking.
    """
    total = 0
    for name in image_files(path, members=members):
        st = (known or {}).get(os.path.normpath(name))
        if st is None:
            try:
                st = os.lstat(name)
            except OSError:
                continue
        total += st.st_size
    return total


def image_fingerprint(path: str) -> str:
    """Size and mtime of every file of the Image at *path* (the file and its members).

    Changes when any of them changes: replacing a bad track changes it even
    though the sheet did not. Blocking (reads the sheet, stats each file).
    """
    files = [path, *sorted(image_members(path))]
    return "|".join(path_fingerprint(f) for f in files)
