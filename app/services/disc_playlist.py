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
from pathlib import Path

from services.referrers import write_new_bytes
from services.referrers.m3u import PLAYLIST_EXT, playlist_bytes, protect_entry
from services.tools import registry
from utils.delete_plan import _parse_cue_tracks, _parse_gdi_tracks
from utils.path_utils import is_within_configured_volumes, match_extension

_DISC = r"(?:disc|disk|cd)[\s_-]*(\d+)(?:\s*of\s*\d+)?"
# "(Disc 1)", "[CD 2]", "(Disc 1 of 3)": the whole bracket group goes.
_BRACKETED_RE = re.compile(r"\s*[(\[]\s*" + _DISC + r"\s*[)\]]", re.IGNORECASE)
# "Disc 1", "CD2" written bare, with the separator before it ("Game - CD2").
_BARE_RE = re.compile(r"(?<![A-Za-z0-9])" + _DISC + r"(?![A-Za-z0-9])", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])" + _DISC + r"(?![A-Za-z0-9])", re.IGNORECASE)
_TRAILING_NUMBER_SUFFIX_RE = re.compile(r"\s\(\d+\)$")
_SEPARATORS = " \t-_,.:;"
_MAX_FILE_NAME_BYTES = 255
# A title leaves room for the longest suffix the preview can add, " (999).m3u",
# so every name it offers can be created; a file-name stem only for ".m3u".
_MAX_TITLE_BYTES = _MAX_FILE_NAME_BYTES - len(".m3u") - len(" (999)")
_MAX_STEM_BYTES = _MAX_FILE_NAME_BYTES - len(".m3u")
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


def _check_file_name_part(value: str, what: str, max_bytes: int = _MAX_TITLE_BYTES) -> None:
    if not value or value in (".", ".."):
        raise PlaylistError(f"{what} can't be empty")
    if "/" in value or "\\" in value:
        raise PlaylistError(f"{what} can't contain a path separator")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise PlaylistError(f"{what} can't contain control characters")
    if len(value.encode("utf-8", "surrogateescape")) > max_bytes:
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


def _validated_files(paths: list[str]) -> tuple[str, dict[str, str]]:
    """The folder the user selected from and ``{file name: path}``, or PlaylistError.

    The playlist goes in the selected (unresolved) parent folder, which every
    path must share, and each entry is checked to resolve from there to the
    file that was selected.
    """
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
    directories = {os.path.dirname(raw) for raw in seen.values()}
    if len(directories) != 1:
        raise PlaylistError("The discs must be in one folder")
    directory = directories.pop()
    files: dict[str, str] = {}
    for raw in seen.values():
        name = os.path.basename(raw)
        if any(ch in name for ch in "\r\n\0"):
            raise PlaylistError(f"{name} has a line break in its name")
        if not os.path.samefile(os.path.join(directory, name), raw):
            raise PlaylistError(f"{name} would not resolve from the playlist's folder")
        files[name] = raw
    return directory, files


def _named_tracks(sheet: str) -> set[str]:
    """Casefolded track file names a .cue / .gdi sheet refers to."""
    path = Path(sheet)
    try:
        parse = _parse_gdi_tracks if sheet.lower().endswith(".gdi") else _parse_cue_tracks
        tracks = parse(path)
    except OSError:
        return set()
    return {re.split(r"[\\/]", t)[-1].casefold() for t in tracks}


def _split_discs_and_tracks(files: dict[str, str]) -> tuple[list[str], list[str]]:
    """Disc image names, and the other selected files that are tracks of them.

    A ticked ``.bin`` named by a ticked ``.cue`` / ``.gdi`` is that disc's
    track, not another disc: it is left out. Any other non-disc file refuses.
    """
    disc_exts = registry.disc_extensions()
    discs = [n for n in files if match_extension(n, disc_exts) is not None]
    others = [n for n in files if n not in discs]
    sheets = [files[n] for n in discs if n.lower().endswith((".cue", ".gdi"))]
    named = set().union(*(_named_tracks(s) for s in sheets)) if sheets else set()
    for name in others:
        if name.casefold() not in named:
            raise PlaylistError(f"{name} is not a disc image")
    return discs, others


def _refuse_duplicate_discs(names: list[str]) -> None:
    """Two files for one disc (a .cue and its .chd, say) would list it twice."""
    by_number: dict[int, list[str]] = {}
    by_stem: dict[str, list[str]] = {}
    for n in names:
        number = disc_number(n)
        if number is not None:
            by_number.setdefault(number, []).append(n)
        else:
            by_stem.setdefault(_stem(n).casefold(), []).append(n)
    for number, group in sorted(by_number.items()):
        if len(group) > 1:
            raise PlaylistError(f"Disc {number} appears twice: {', '.join(sorted(group))}")
    for group in by_stem.values():
        if len(group) > 1:
            raise PlaylistError(
                f"{' and '.join(sorted(group))} are the same disc in two formats",
            )


def plan_playlist(paths: list[str], title: str | None = None) -> PlaylistPlan:
    """The playlist these discs would make. Writes nothing."""
    directory, files = _validated_files(paths)
    names, tracks = _split_discs_and_tracks(files)
    if len(names) < 2:
        raise PlaylistError("Select two or more discs")
    _refuse_duplicate_discs(names)
    names.sort(key=_order_key)
    warnings = []
    if tracks:
        warnings.append(
            f"Left out {len(tracks)} track files that the selected cue sheets already name.",
        )
    if len({disc_title(n).casefold() for n in names}) > 1:
        warnings.append("These names don't share one title; check the playlist name.")
    chosen = _clean_title(title) if title is not None else disc_title(names[0])
    _check_file_name_part(chosen, "The playlist name")
    requested = f"{chosen}{PLAYLIST_EXT}"
    free = _free_name(directory, chosen)
    entries = [protect_entry(n) for n in names]
    try:
        playlist_bytes(entries)  # the preview must be a playlist that can be written
    except ValueError as exc:
        raise PlaylistError(f"These names can't go in a playlist: {exc}") from None
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
    _check_file_name_part(stem, "The playlist name", _MAX_STEM_BYTES)
    return stem


def create_playlist(paths: list[str], file_name: str) -> PlaylistPlan:
    """Write the playlist as *file_name* beside the discs, never over anything.

    Raises ``PlaylistNameTaken`` (with the next free name) if *file_name*
    exists now, including when it appeared after the preview.
    """
    stem = _check_playlist_file_name(file_name)
    plan = plan_playlist(paths)  # entries only; the name is the previewed file_name
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
