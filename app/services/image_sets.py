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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from services.tools import registry
from utils.delete_plan import sheet_track_paths
from utils.path_utils import path_fingerprint

# Lists of Images, never Images. Compared on the lowercased suffix.
PLAYLIST_EXTENSIONS = frozenset({".m3u", ".m3u8"})


def is_playlist(path: str) -> bool:
    return Path(path).suffix.lower() in PLAYLIST_EXTENSIONS


def _named_tracks(
    path: str, within: Sequence[str | Path] | None = None,
) -> list[str] | None:
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
    tracks = sheet_track_paths(path, within=within)
    if tracks:
        return tracks
    folder = Path(path).parent
    named: list[str] = []
    for text in sheet_readings(path):
        for ref in parse(text):
            try:
                named.append(str(resolve_track(folder, ref, within=within)))
            except ValueError:
                continue
    return list(dict.fromkeys(named))


def image_members(path: str, *, within: Sequence[str | Path] | None = None) -> list[str]:
    """The other files the Image at *path* is made of. Reads the sheet; blocking.

    A sheet's tracks resolve within the configured Volumes, or within the
    trusted roots *within* names for a sheet outside them (an Inbox's unpack
    folder in the app's work area, #432), as ``resolve_track`` takes them.

    A sheet that cannot be read whole contributes no members here: a listing
    cannot hide files it cannot name, so they stay listed. Nothing that deletes
    relies on that: the duplicate finder's record of sheet tracks
    (:func:`sheet_track_identities`) is marked incomplete for such a sheet, and
    a delete is then not offered.
    """
    tracks = _named_tracks(path, within)
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
    return next(_sheets_naming(path, stop_at, progress), None)


def sheets_naming(
    path: str, *, stop_at: str, progress: Callable[..., None] | None = None,
) -> list[str]:
    """Every ``.cue``/``.gdi`` that lists *path* as a track. Blocking.

    :func:`owning_sheet`'s walk, carried on past the first sheet: two games'
    sheets can share a track, and deleting it for one breaks the other.
    Raises :class:`SheetOwnerUnknown` exactly as :func:`owning_sheet` does.
    """
    return list(_sheets_naming(path, stop_at, progress))


PLAYLIST_EXTENSIONS = (".m3u", ".m3u8")
# A playlist is a few lines; a bigger file is not one to read for this.
PLAYLIST_READ_BYTES = 64 * 1024


def owners_naming(
    path: str, *, stop_at: str, progress: Callable[..., None] | None = None,
    companions_of: dict[str, list[str]] | None = None,
) -> list[str]:
    """Every file that names *path* as part of itself. Blocking.

    The one ownership question every delete asks before it takes a file that
    belongs to a game: a ``.cue``/``.gdi`` that lists it as a track
    (:func:`sheets_naming`, from its folder up to *stop_at*), a ``.m3u``/``.m3u8``
    that lists it as a disc (the same walk), and a file beside it whose tools
    read it with them (``registry.source_companions``: a RAR set's later
    volume, a split dump's part; ``registry.split_members``). A file is safe
    to delete with its game only when every owner is going too.

    Fails closed: a folder that can't be listed, a sheet or playlist that
    can't be read, or a set whose members can't be told raises
    :class:`SheetOwnerUnknown`, never "no owner". *companions_of* caches each
    sibling's companions across the calls of one batch.
    """
    owners = list(_sheets_naming(path, stop_at, progress))
    owners += _playlists_naming(path, stop_at, progress)
    owners += _companion_owners(path, progress, companions_of)
    return list(dict.fromkeys(owners))


def _folders_up(real: str, stop_at: str, path: str):
    boundary = os.path.normpath(os.path.realpath(stop_at))
    folder = os.path.normpath(os.path.dirname(real))
    if folder != boundary and not folder.startswith(boundary.rstrip(os.sep) + os.sep):
        raise SheetOwnerUnknown(f"{path} is not under {stop_at}")
    while True:
        yield folder
        if folder == boundary:
            return
        parent = os.path.dirname(folder)
        if parent == folder:
            return
        folder = parent


def _listdir(folder: str) -> list[str]:
    try:
        return sorted(os.listdir(folder))
    except OSError as exc:
        raise SheetOwnerUnknown(f"{folder} could not be listed ({exc.strerror or exc})") from exc


def _own_stat(path: str) -> tuple[str, os.stat_result]:
    real = os.path.realpath(path)
    try:
        return real, os.stat(real)
    except OSError as exc:
        raise SheetOwnerUnknown(f"{path} could not be read ({exc.strerror or exc})") from exc


def _playlists_naming(path: str, stop_at: str, progress: Callable[..., None] | None):
    """Every playlist from *path*'s folder up to *stop_at* that lists it."""
    from services.referrers import m3u
    from utils.path_utils import read_beneath

    tick = progress or (lambda *_args: None)
    real, own = _own_stat(path)
    for folder in _folders_up(real, stop_at, path):
        tick(folder)
        for name in _listdir(folder):
            playlist = os.path.join(folder, name)
            if not name.lower().endswith(PLAYLIST_EXTENSIONS) or playlist == real:
                continue
            tick(playlist)
            if os.path.isdir(playlist):
                continue
            try:
                data = read_beneath(folder, name, max_bytes=PLAYLIST_READ_BYTES)
            except (OSError, ValueError) as exc:
                raise SheetOwnerUnknown(f"{name} could not be read ({exc})") from exc
            entries = m3u.entries(data)
            if any(_is_track(own, real, os.path.join(folder, entry)) for entry in entries):
                yield playlist


def _companion_owners(
    path: str, progress: Callable[..., None] | None,
    companions_of: dict[str, list[str]] | None,
) -> list[str]:
    """The files beside *path* whose tools read it as part of their set."""
    from services.tools.base import CompanionsUnknown

    tick = progress or (lambda *_args: None)
    real, own = _own_stat(path)
    folder = os.path.dirname(real)
    cache = companions_of if companions_of is not None else {}
    owners = []
    for name in _listdir(folder):
        sibling = os.path.join(folder, name)
        if sibling == real or os.path.isdir(sibling):
            continue
        tick(sibling)
        members = cache.get(sibling)
        if members is None:
            try:
                members = [
                    *registry.source_companions(sibling, strict=True),
                    *registry.split_members(sibling),
                ]
            except CompanionsUnknown as exc:
                raise SheetOwnerUnknown(f"the set {name} belongs to can't be told ({exc})") from exc
            cache[sibling] = members
        if any(_is_track(own, real, member) for member in members):
            owners.append(sibling)
    return owners


def _sheets_naming(path: str, stop_at: str, progress: Callable[..., None] | None):
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
                yield sheet
        if folder == boundary:
            return
        parent = os.path.dirname(folder)
        if parent == folder:
            return
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


Identity = tuple[int, int]


@dataclass(frozen=True)
class SheetOwners:
    """Every sheet's tracks over the Volumes (:func:`volume_sheet_owners_scoped`).

    *owners*: each track identity, with the identities of every sheet that
    names it. *unknown*: each file identity a sheet whose tracks can't be told
    (an absolute ``FILE`` name, a sheet that can't be read whole) may name,
    with why. Such a sheet names only files in its own folder and below,
    reached through any link (``resolve_track``), so only those are unknown:
    a file in neither map is no sheet's track (#454)."""

    owners: Mapping[Identity, frozenset[Identity]]
    unknown: Mapping[Identity, str]


def _walk_volume_sheets(
    roots: Iterable[str], progress: Callable[..., None],
) -> tuple[dict[Identity, set[Identity]], list[tuple[str, str]], list[str]]:
    """Every sheet over *roots*: the track owners of each one read whole,
    each sheet that can't be (its path and why), and what the walk missed."""
    from utils.path_utils import walk_tree

    owners: dict[Identity, set[Identity]] = {}
    unresolved: list[tuple[str, str]] = []
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
                    unresolved.append((sheet, f"{exc} (in {folder})"))
                    continue
                except OSError as exc:
                    unresolved.append(
                        (sheet, f"{name} could not be read ({exc.strerror or exc}) (in {folder})"),
                    )
                    continue
                for track in tracks:
                    owners.setdefault(track, set()).add((st.st_dev, st.st_ino))
        missed.extend(f"{path} could not be listed" for path in unlisted)
    return owners, unresolved, missed


def _problem(missed: list[str]) -> str:
    more = len(missed) - 3
    return "; ".join(missed[:3]) + (f"; and {more} more" if more > 0 else "")


def volume_sheet_owners(
    roots: Iterable[str], *, progress: Callable[..., None],
) -> tuple[dict[Identity, frozenset[Identity]] | None, str | None]:
    """:func:`volume_sheet_tracks` with, for each track identity, the
    identities of every sheet that names it. The same walk, scope and "None,
    never partial" rule: one sheet that can't be read whole makes it None."""
    owners, unresolved, missed = _walk_volume_sheets(roots, progress)
    missed = [why for _sheet, why in unresolved] + missed
    if missed:
        return None, _problem(missed)
    return {track: frozenset(sheets) for track, sheets in owners.items()}, None


def sheet_reach(
    folder: str, roots: Iterable[str], *, progress: Callable[..., None],
) -> tuple[set[Identity], list[str]]:
    """The identity of every file a sheet in *folder* could name, and what the
    walk missed. Blocking.

    A sheet's track name is relative, without ``..`` (``resolve_track``), so
    it reaches *folder* and below, and through a link a file or folder
    anywhere in *roots* (the Volumes): each linked folder is walked too, and
    each file is taken by identity, links followed, so a hard link counts.
    A link to a file out of *roots* is a track ``resolve_track`` refuses, so
    it reaches nothing. A link to a folder out of *roots* is in what was
    missed: a link inside that folder may lead back in, and ``resolve_track``
    checks only the real path, so what it reaches can't be told without
    walking outside the Volumes, which this never does. Junk names (``__MACOSX``, ``@eaDir``) are
    walked too: ``resolve_track`` takes them like any other name. Never
    partial in silence: a folder or file that can't be read is in what was
    missed."""
    from utils.path_utils import walk_tree

    trusted = [os.path.realpath(root) for root in roots]

    def inside(real: str, tops: Iterable[str]) -> bool:
        return any(real == top or real.startswith(top.rstrip(os.sep) + os.sep) for top in tops)

    reached: set[Identity] = set()
    missed: list[str] = []
    walked: list[str] = []
    pending = [os.path.realpath(folder)]
    while pending:
        top = pending.pop()
        if inside(top, walked):
            continue
        walked.append(top)
        unlisted: list[str] = []
        for current, dirs, files in walk_tree(
            top, progress, unlisted=unlisted, skip_junk=False,
        ):
            for name in dirs:
                path = os.path.join(current, name)
                if os.path.islink(path):
                    real = os.path.realpath(path)
                    if not inside(real, trusted):
                        missed.append(f"{path} links to a folder outside the Volumes")
                    elif os.path.isdir(real):
                        pending.append(real)
            for name in files:
                path = os.path.join(current, name)
                if os.path.islink(path) and not inside(os.path.realpath(path), trusted):
                    continue
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    missed.append(f"{path} could not be read ({exc.strerror or exc})")
                    continue
                reached.add((st.st_dev, st.st_ino))
        missed.extend(f"{path} could not be listed" for path in unlisted)
    return reached, missed


def volume_sheet_owners_scoped(
    roots: Iterable[str], *, progress: Callable[..., None],
) -> tuple[SheetOwners | None, str | None]:
    """:func:`volume_sheet_owners`, with a sheet whose tracks can't be told
    holding back only what it could name (#454): every file in its folder and
    below (:func:`sheet_reach`) is in ``unknown``. None and why only when the
    walk itself missed something, or the reach of an unresolved sheet did (a
    folder it can't read, a folder link out of the Volumes), naming the sheet."""
    roots = list(roots)
    owners, unresolved, missed = _walk_volume_sheets(roots, progress)
    unknown: dict[Identity, str] = {}
    reached_folders: dict[str, set[Identity]] = {}
    for sheet, why in unresolved:
        folder = os.path.dirname(sheet)
        if folder not in reached_folders:
            reached, more = sheet_reach(folder, roots, progress=progress)
            reached_folders[folder] = reached
            name = os.path.basename(sheet)
            missed.extend(f"{m}, so what {name} names through it can't be told" for m in more)
        for identity in reached_folders[folder]:
            unknown.setdefault(identity, why)
    if missed:
        return None, _problem(missed)
    return SheetOwners(
        owners={track: frozenset(sheets) for track, sheets in owners.items()},
        unknown=unknown,
    ), None


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


def image_fingerprint(path: str, *, within: Sequence[str | Path] | None = None) -> str:
    """Size and mtime of every file of the Image at *path* (the file and its members).

    Changes when any of them changes: replacing a bad track changes it even
    though the sheet did not. Blocking (reads the sheet, stats each file).
    *within*: as :func:`image_members` takes it.
    """
    files = [path, *sorted(image_members(path, within=within))]
    return "|".join(path_fingerprint(f) for f in files)
