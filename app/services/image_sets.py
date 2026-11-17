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


def _named_tracks(path: str) -> list[str] | None:
    """The tracks a ``.cue``/``.gdi`` at *path* names, or None when that
    cannot be told. Blocking.

    Never ``[]`` for "could not tell" (Codex PRRT_kwDOQ9nULM64N2yy): a sheet
    that cannot be read whole (unreadable, or over the size cap) is None, and
    every caller decides what an unknown track set means for it. ``[]`` only
    for a file that is not a sheet, or a sheet that names nothing.

    The resolved set (``sheet_track_paths``) when the sheet resolves as one.
    When it does not (a reference it may not use, such as ``../Audio.bin``),
    still every name that does resolve, from every spelling (Codex
    PRRT_kwDOQ9nULM64MWFq): "the set is unknown" must not read as "the sheet
    owns nothing", or its tracks would be listed, converted and deleted as
    Images of their own.
    """
    from utils.sheet_text import (
        cue_tracks,
        gdi_track_names,
        resolve_track,
        sheet_is_readable,
        sheet_readings,
    )

    parse = {".cue": cue_tracks, ".gdi": gdi_track_names}.get(Path(path).suffix.lower())
    if parse is None:
        return []
    if not sheet_is_readable(path):
        return None
    tracks = sheet_track_paths(path)
    if tracks:
        return tracks
    folder = Path(path).parent
    named: list[str] = []
    for text in sheet_readings(path):
        for ref in parse(text):
            try:
                named.append(str(resolve_track(folder, ref)))
            except ValueError:
                continue
    return list(dict.fromkeys(named))


def image_members(path: str) -> list[str]:
    """The other files the Image at *path* is made of. Reads the sheet; blocking.

    A sheet that cannot be read whole contributes no members here: a listing
    cannot hide files it cannot name, so they stay listed. Nothing that deletes
    relies on that: the duplicate finder's record of sheet tracks
    (:func:`sheet_track_identities`) is marked incomplete for such a sheet, and
    a delete is then not offered.
    """
    tracks = _named_tracks(path)
    members = [
        *(tracks or ()), *registry.source_companions(path),
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
    the way that cannot be listed, a sheet in one that cannot be read whole
    (``sheet_is_readable``: unreadable, or over the size cap), or a sheet whose
    tracks do not resolve as one set (``sheet_track_list`` is None: a reference
    it may not use, Codex PRRT_kwDOQ9nULM64MWFq). A caller about
    to delete *path* treats that as "it may be a track", never as "standalone".
    *progress* is called per folder and per sheet, for a stall-bounded caller.
    """
    from utils.sheet_text import sheet_is_readable, sheet_track_list

    tick = progress or (lambda *_args: None)
    real = os.path.realpath(path)
    try:
        own = os.stat(real)
    except OSError as exc:
        raise SheetOwnerUnknown(f"{path} could not be read ({exc.strerror or exc})") from exc
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
            tracks = sheet_track_list(sheet)
            if tracks is None:
                raise SheetOwnerUnknown(
                    f"{name} names a track it may not use, so its tracks are unknown",
                )
            if any(_is_track(own, real, str(track)) for track in tracks):
                return sheet
        if folder == boundary:
            return None
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def sheet_track_identities(sheet: str) -> set[tuple[int, int]]:
    """``(st_dev, st_ino)`` of every track *sheet* names, links followed. Blocking.

    The sheet's whole resolved track set (``sheet_track_list``, the same
    ``resolve_track`` every sheet reader uses), each stat'ed through its links,
    so a sheet that reaches a file in another folder through a linked track
    names that file (Codex PRRT_kwDOQ9nULM64NUn9). A track that does not exist
    names nothing. Raises :class:`SheetOwnerUnknown` whenever the set is not
    known whole (Codex PRRT_kwDOQ9nULM64N2yy): a sheet that cannot be read
    whole (unreadable, or over the size cap), one whose tracks do not resolve
    as one set, or a track that may exist but cannot be stat'ed. Never an
    empty set for "could not tell".
    """
    from utils.sheet_text import sheet_is_readable, sheet_track_list

    name = os.path.basename(sheet)
    if not sheet_is_readable(sheet):
        raise SheetOwnerUnknown(f"{name} could not be read (unreadable or too large)")
    tracks = sheet_track_list(sheet)
    if tracks is None:
        raise SheetOwnerUnknown(f"{name} names a track it may not use, so its tracks are unknown")
    out: set[tuple[int, int]] = set()
    for track in tracks:
        try:
            st = os.stat(track)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SheetOwnerUnknown(
                f"{track} could not be read ({exc.strerror or exc})",
            ) from exc
        out.add((st.st_dev, st.st_ino))
    return out


def volume_sheet_tracks(
    roots: Iterable[str], *, progress: Callable[..., None],
) -> tuple[set[tuple[int, int]] | None, str | None]:
    """Every ``.cue``/``.gdi``'s track identities over *roots* (the configured
    Volumes), and None, or None and what was missed. Blocking: run detached
    under the stall bound (``walk_tree`` reports every step).

    ``resolve_track`` accepts a track anywhere in the Volumes, so a sheet
    anywhere in them, in any Library or none, may own a file through a link
    (Codex PRRT_kwDOQ9nULM64OXPI): ownership is read over the same scope, from
    the disk, never from a Library's listing. Sheets only, no hashing. Junk and
    RomM's ``.romm_tmp_*`` staging are skipped as every walk skips them, and a
    linked folder is not walked into (its target is walked where it is, if in
    a Volume). Never an empty set for "could not tell": a root that is not a
    folder, a folder or entry the walk could not see, or a sheet that cannot
    be read whole makes the answer None, with what was missed.
    """
    owners, problem = volume_sheet_owners(roots, progress=progress)
    return (None, problem) if owners is None else (set(owners), None)


def volume_sheet_owners(
    roots: Iterable[str], *, progress: Callable[..., None],
) -> tuple[dict[tuple[int, int], frozenset[tuple[int, int]]] | None, str | None]:
    """:func:`volume_sheet_tracks` with, for each track identity, the
    identities of every sheet that names it: what an Image rename (#426)
    needs to know that a track belongs to its own sheet and to no other one.
    The same walk, scope and "None, never partial" rule."""
    from utils.path_utils import walk_tree

    owners: dict[tuple[int, int], set[tuple[int, int]]] = {}
    missed: list[str] = []
    walked: list[str] = []
    for root in roots:
        progress(root)
        real = os.path.realpath(root)
        if any(real == done or real.startswith(done + os.sep) for done in walked):
            continue
        walked.append(real)
        if not os.path.isdir(root):
            missed.append(f"{root} is not a folder")
            continue
        unlisted: list[str] = []
        for folder, _dirs, files in walk_tree(root, progress, unlisted=unlisted):
            for name in files:
                if not name.lower().endswith(SHEET_EXTENSIONS):
                    continue
                sheet = os.path.join(folder, name)
                try:
                    tracks = sheet_track_identities(sheet)
                    st = os.stat(sheet)
                except SheetOwnerUnknown as exc:
                    missed.append(f"{exc} (in {folder})")
                    continue
                except OSError as exc:
                    missed.append(f"{name} could not be read ({exc.strerror or exc}) (in {folder})")
                    continue
                for track in tracks:
                    owners.setdefault(track, set()).add((st.st_dev, st.st_ino))
        missed.extend(f"{path} could not be listed" for path in unlisted)
    if missed:
        more = len(missed) - 3
        return None, "; ".join(missed[:3]) + (f"; and {more} more" if more > 0 else "")
    return {track: frozenset(sheets) for track, sheets in owners.items()}, None


def _is_track(own: os.stat_result, real: str, track: str) -> bool:
    """Whether *track* (a sheet's spelling) is the file *own* is a stat of.

    By file identity (device and inode), so a case-insensitive share's
    ``track.bin`` is ``Track.bin``. A track the filesystem reports missing is
    compared by its real path with case folded on both sides (``casefold``;
    ``os.path.normcase`` folds nothing on POSIX): that only widens what counts
    as the sheet's own, so it fails closed. Any other stat failure leaves it
    unknown.
    """
    try:
        st = os.stat(track)
    except FileNotFoundError:
        return os.path.realpath(track).casefold() == real.casefold()
    except OSError as exc:
        raise SheetOwnerUnknown(f"{track} could not be read ({exc.strerror or exc})") from exc
    return (st.st_dev, st.st_ino) == (own.st_dev, own.st_ino)


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
            # A linked member (`disc/Track.bin` -> `shared/Track.bin`) owns
            # its target too: that file is the sheet's, never an Image of its
            # own (Codex PRRT_kwDOQ9nULM64NUn9).
            target = os.path.normpath(os.path.realpath(member))
            if target != key:
                owned.add(target)
                owned_folded.setdefault(target.casefold(), target)

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
