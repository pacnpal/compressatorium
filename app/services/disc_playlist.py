"""Create an ``.m3u`` from selected discs (#299).

Pure planning plus one write. ``plan_playlist`` turns a selection of disc
Images into a playlist name and ordered entries without touching the disk
(the preview). ``create_playlist`` writes it with the shared playlist writer
from the ``.m3u`` Referrer package (``playlist_bytes`` + ``write_new_bytes``),
which creates the file exclusively, so an existing playlist is never
overwritten and two requests racing for one name have one winner.

Accepted: two or more files that are disc Images by the registry
(``registry.disc_extensions()``), all directly in one folder inside a Volume.
Entries are file names relative to the playlist's own folder.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field

from services.referrers import write_new_bytes
from services.referrers.m3u import PLAYLIST_EXT, playlist_bytes
from services.tools import registry
from utils.path_utils import is_within_configured_volumes, match_extension

_DISC = r"(?:disc|disk|cd)[\s_-]*(\d+)(?:\s*of\s*\d+)?"
# "(Disc 1)", "[CD 2]", "(Disc 1 of 3)": the whole bracket group goes.
_BRACKETED_RE = re.compile(r"\s*[(\[]\s*" + _DISC + r"\s*[)\]]", re.IGNORECASE)
# "Disc 1", "CD2" written bare, with the separator before it ("Game - CD2").
_BARE_RE = re.compile(r"(?<![A-Za-z0-9])" + _DISC + r"(?![A-Za-z0-9])", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])" + _DISC + r"(?![A-Za-z0-9])", re.IGNORECASE)
_TRAILING_NUMBER_SUFFIX_RE = re.compile(r"\s\(\d+\)$")
_SEPARATORS = " \t-_,.:;"
_MAX_NAME_BYTES = 200  # leaves room for ".m3u" under the 255-byte file name limit
_MAX_SUFFIX = 999


class PlaylistError(Exception):
    """The selection or name can't make a playlist. ``str(exc)`` is user-facing."""


class PlaylistNameTaken(PlaylistError):
    """The file name is taken. ``suggested_name`` is the next free one."""

    def __init__(self, file_name: str, suggested_name: str):
        super().__init__(f"{file_name} already exists")
        self.file_name = file_name
        self.suggested_name = suggested_name


@dataclass
class PlaylistPlan:
    directory: str
    title: str
    requested_name: str  # "<title>.m3u", before any collision handling
    file_name: str       # the name that is free now (may be "<title> (2).m3u")
    name_taken: bool     # requested_name existed, so file_name differs
    entries: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _stem(name: str) -> str:
    ext = match_extension(name, registry.disc_extensions())
    return name[: -len(ext)] if ext else os.path.splitext(name)[0]


def disc_number(name: str) -> int | None:
    """The disc number a file name carries (``(Disc 2)``, ``Disc 2 of 3``, ``CD2``), if any."""
    match = _NUMBER_RE.search(_stem(name))
    return int(match.group(1)) if match else None


def disc_title(name: str) -> str:
    """The game title: the file name without its extension, disc marker and
    the separators left dangling around it. Other tags such as ``(USA)`` stay."""
    stem = _stem(name)
    title = _BRACKETED_RE.sub(" ", stem)
    title = _BARE_RE.sub(" ", title)
    title = re.sub(r"\s+", " ", title).strip(_SEPARATORS)
    title = re.sub(r"\s+([)\]])", r"\1", title)
    return title or stem.strip()


def _order_key(name: str) -> tuple:
    number = disc_number(name)
    # By disc number; unnumbered ones after them, in name order.
    return (number is None, number or 0, unicodedata.normalize("NFC", name).casefold(), name)


def _check_file_name_part(value: str, what: str) -> None:
    if not value or value in (".", ".."):
        raise PlaylistError(f"{what} can't be empty")
    if "/" in value or "\\" in value:
        raise PlaylistError(f"{what} can't contain a path separator")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise PlaylistError(f"{what} can't contain control characters")
    if len(value.encode("utf-8", "surrogateescape")) > _MAX_NAME_BYTES:
        raise PlaylistError(f"{what} is too long")


def _clean_title(title: str) -> str:
    title = title.strip()
    if title.lower().endswith(PLAYLIST_EXT):
        title = title[: -len(PLAYLIST_EXT)].rstrip()
    _check_file_name_part(title, "The playlist name")
    return title


def _taken_names(directory: str) -> set[str]:
    try:
        with os.scandir(directory) as it:
            return {unicodedata.normalize("NFC", e.name).casefold() for e in it}
    except OSError as exc:
        raise PlaylistError(f"Can't read the folder: {exc.strerror}") from exc


def _free_name(directory: str, title: str) -> str:
    """``<title>.m3u``, or the first ``<title> (N).m3u`` nothing is using."""
    taken = _taken_names(directory)
    for n in range(1, _MAX_SUFFIX + 1):
        name = f"{title}{PLAYLIST_EXT}" if n == 1 else f"{title} ({n}){PLAYLIST_EXT}"
        if unicodedata.normalize("NFC", name).casefold() not in taken:
            return name
    raise PlaylistError("Too many playlists with this name already exist")


def _validated_discs(paths: list[str]) -> tuple[str, list[str]]:
    """The common folder and the disc file names, or PlaylistError."""
    seen: dict[str, str] = {}
    for raw in paths:
        if "::" in raw:
            raise PlaylistError("Files inside an archive can't go in a playlist")
        if not os.path.isabs(raw):
            raise PlaylistError("Paths must be absolute")
        if not is_within_configured_volumes(raw, treat_archives=False):
            raise PlaylistError(f"{os.path.basename(raw)} is outside the configured volumes")
        if not os.path.isfile(raw):
            raise PlaylistError(f"{os.path.basename(raw)} was not found")
        seen.setdefault(os.path.realpath(raw), raw)
    if len(seen) < 2:
        raise PlaylistError("Select two or more discs")
    disc_exts = registry.disc_extensions()
    folders = {os.path.dirname(os.path.realpath(p)) for p in seen}
    if len(folders) != 1:
        raise PlaylistError("The discs must be in one folder")
    names = []
    for raw in seen.values():
        name = os.path.basename(raw)
        if match_extension(name, disc_exts) is None:
            raise PlaylistError(f"{name} is not a disc image")
        if any(ch in name for ch in "\r\n\0"):
            raise PlaylistError(f"{name} has a line break in its name")
        names.append(name)
    return os.path.dirname(next(iter(seen.values()))), names


def plan_playlist(paths: list[str], title: str | None = None) -> PlaylistPlan:
    """The playlist these discs would make. Writes nothing."""
    directory, names = _validated_discs(paths)
    names.sort(key=_order_key)
    titles = {disc_title(n).casefold() for n in names}
    warnings = []
    if len(titles) > 1:
        warnings.append("These names don't share one title; check the playlist name.")
    chosen = _clean_title(title) if title is not None else disc_title(names[0])
    _check_file_name_part(chosen, "The playlist name")
    requested = f"{chosen}{PLAYLIST_EXT}"
    free = _free_name(directory, chosen)
    # "#" starts a comment line; "./" keeps such a name an entry.
    entries = [f"./{n}" if n.startswith("#") else n for n in names]
    return PlaylistPlan(
        directory=directory,
        title=chosen,
        requested_name=requested,
        file_name=free,
        name_taken=free != requested,
        entries=entries,
        warnings=warnings,
    )


def _check_playlist_file_name(file_name: str) -> str:
    """The title part of *file_name*, which must be a plain ``<name>.m3u``."""
    if not file_name.lower().endswith(PLAYLIST_EXT):
        raise PlaylistError("The playlist file name must end in .m3u")
    stem = file_name[: -len(PLAYLIST_EXT)]
    _check_file_name_part(stem, "The playlist name")
    return stem


def create_playlist(paths: list[str], file_name: str) -> PlaylistPlan:
    """Write the playlist as *file_name* beside the discs, never over anything.

    Raises ``PlaylistNameTaken`` (with the next free name) if *file_name*
    exists now, including when it appeared after the preview.
    """
    stem = _check_playlist_file_name(file_name)
    plan = plan_playlist(paths, title=stem)
    target = os.path.join(plan.directory, file_name)
    if not is_within_configured_volumes(target, treat_archives=False):
        raise PlaylistError("The playlist would be outside the configured volumes")
    try:
        write_new_bytes(target, playlist_bytes(plan.entries))
    except FileExistsError:
        base = _TRAILING_NUMBER_SUFFIX_RE.sub("", stem)
        raise PlaylistNameTaken(file_name, _free_name(plan.directory, base)) from None
    plan.requested_name = plan.file_name = file_name
    plan.title = stem
    plan.name_taken = False
    return plan
