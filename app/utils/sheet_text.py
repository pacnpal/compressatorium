"""Reading track sheets (``.cue`` / ``.gdi``) the one way the app does.

Sheets come in UTF-8 (with or without a BOM) and in Windows-1252, and a track
name decoded the wrong way names a file that is not there. Every reader of a
sheet decodes it through here: the disc-ID probe (``services.disc_id``) and
the delete plan and header-dependency list (``utils.delete_plan``).
"""
from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_UTF8_BOM = b"\xef\xbb\xbf"
# A track sheet is a few hundred bytes; anything far larger is not one.
MAX_SHEET_BYTES = 1024 * 1024


def sheet_readings(path: str | Path) -> list[str]:
    """The sheet's text, in the order its spellings should be tried.

    First the sheet as UTF-8 with a BOM stripped, or as cp1252 when it is not
    valid UTF-8; then Latin-1, which decodes any bytes and is the reading the
    disc-ID probe always used.

    Empty when the sheet cannot be read, and when it is larger than
    ``MAX_SHEET_BYTES``: never a partial parse. A sheet cut at the cap would
    hide every track after it (a delete plan that orphans them, or a
    ``FILE "/etc/..."`` behind a megabyte of ``REM`` lines that the tool
    reading the whole sheet still opens), so an oversized sheet is unreadable
    and every reader built on this treats it as unknown or unsafe.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SHEET_BYTES + 1)
    except OSError:
        return []
    if len(raw) > MAX_SHEET_BYTES:
        return []
    raw = raw.removeprefix(_UTF8_BOM)
    try:
        first = raw.decode("utf-8")
    except UnicodeDecodeError:
        first = raw.decode("cp1252", errors="replace")
    return [first, raw.decode("latin-1")]


_WIN_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def _is_absolute_reference(ref: str) -> bool:
    ref = ref.strip()
    if not ref:
        return False
    if ref.startswith("~") or ref.startswith("\\\\") or _WIN_ABS_RE.match(ref):
        return True
    return Path(ref).is_absolute()


def resolve_track(sheet_dir: str | Path, ref: str) -> Path:
    """The track a sheet names, resolved beside the sheet, or ``ValueError``.

    The one track resolver: the Platform probe, the detection cache's track
    list, the delete plan and the job gate all use it. Refused, so nothing
    opens them: an empty name, an absolute name (``/x``, ``C:\\x``, ``\\\\host``,
    ``~``), a name with any ``..`` segment, and a name whose real path (through
    a symlink) lies outside the configured Volumes. Returns the path beside
    the sheet, not its real path, so callers see what the sheet named.
    """
    from utils.path_utils import (
        is_within_configured_volumes,  # local: no cycle at import
    )

    raw = ref.strip().strip('"')
    if not raw:
        raise ValueError("Empty track reference")
    normalized = raw.replace("\\", "/")
    if _is_absolute_reference(normalized):
        raise ValueError(f"Absolute track reference is not allowed: {raw}")
    if ".." in normalized.split("/"):
        raise ValueError(f"Track reference escapes source directory: {raw}")
    base = Path(os.path.normpath(str(Path(sheet_dir).absolute())))
    candidate = Path(os.path.normpath(os.path.join(str(base), normalized)))
    if not is_within_configured_volumes(os.path.realpath(candidate), treat_archives=False):
        raise ValueError(f"Track reference resolves outside the volumes: {raw}")
    return candidate


def sheet_tracks(
    path: str | Path, parse: Callable[[str], list[str]],
) -> list[Path] | None:
    """Every track the sheet names, resolved and confined (``resolve_track``).

    None when the sheet is unreadable (missing, or over the size cap) or when
    any track is refused: a reader must then say nothing about the sheet
    rather than act on the tracks it was allowed to see. The first spelling of
    each name that exists wins (``pick_existing``).
    """
    texts = sheet_readings(path)
    if not texts:
        return None
    readings = [parse(text) for text in texts]
    folder = Path(path).parent
    tracks = pick_existing(readings, lambda ref: resolve_track(folder, ref))
    if len(tracks) != len(readings[0]):
        return None
    return tracks


def sheet_is_readable(path: str | Path) -> bool:
    """Whether the sheet can be read whole (exists, readable, within the cap)."""
    return bool(sheet_readings(path))


def cue_tracks(text: str) -> list[str]:
    """The file name of every ``FILE`` line of a ``.cue`` sheet, in order.

    ``FILE "name" TYPE``: the name is everything between the quotes, spaces
    included. Unquoted, ``FILE name TYPE``: everything before the last word
    (the file type), so ``FILE Game (Track 1).bin BINARY`` keeps its spaces; a
    line with no type keeps the whole rest. ``REM`` lines and other commands
    are not tracks. The one CUE FILE-line parser: the disc-ID probe, the delete
    plan and the detection cache's track list all read sheets through it.
    """
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        head, _, rest = stripped.partition(" ")
        if head.upper() != "FILE":
            # A tab after FILE is as valid as a space.
            head, _, rest = stripped.partition("\t")
            if head.upper() != "FILE":
                continue
        rest = rest.strip()
        if rest.startswith('"'):
            body = rest[1:]
            closing = body.find('"')
            if closing >= 0:
                name = body[:closing]
            else:  # an unbalanced quote: read it as unquoted
                parts = body.rsplit(None, 1)
                name = parts[0] if len(parts) == 2 else body
        else:
            parts = rest.rsplit(None, 1)
            name = parts[0] if len(parts) == 2 else rest
        name = name.strip()
        if name:
            names.append(name)
    return names


@dataclass(frozen=True)
class GdiTrack:
    """One track line of a ``.gdi``: ``number lba type sector_size "name" offset``."""

    number: str
    sector_size: int | None
    name: str


def _split_line(line: str) -> list[str]:
    # Quoted names keep their spaces; a line shlex can't parse (an unbalanced
    # quote) falls back to whitespace splitting.
    try:
        return shlex.split(line, comments=True, posix=True)
    except ValueError:
        return line.split()


def gdi_tracks(text: str) -> list[GdiTrack]:
    """Every track line of a ``.gdi`` sheet, in order.

    The first line is the track count and is skipped when it is a number.
    Lines with fewer than five fields, or no file name, are left out.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    head = _split_line(lines[0])
    start = 1 if head and head[0].isdigit() else 0
    tracks: list[GdiTrack] = []
    for line in lines[start:]:
        parts = _split_line(line)
        if len(parts) < 5:
            continue
        name = parts[4].strip('"').strip()
        if not name:
            continue
        size = int(parts[3]) if parts[3].isdigit() else None
        tracks.append(GdiTrack(parts[0], size, name))
    return tracks


def pick_existing(
    readings: Sequence[Sequence[str]],
    resolve: Callable[[str], Path],
) -> list[Path]:
    """One path per track: the first spelling across ``readings`` that exists.

    ``readings`` holds the track names each decoding of the sheet produced, in
    preference order and in the same track order. ``resolve`` turns a name into
    a path beside the sheet and may raise ``ValueError`` to refuse one (a
    reference that escapes the sheet's folder). A track none of whose
    spellings exists keeps the first spelling ``resolve`` accepts, so callers
    that check existence see it missing exactly as before.
    """
    if not readings:
        return []
    tracks: list[Path] = []
    for index in range(len(readings[0])):
        chosen: Path | None = None
        fallback: Path | None = None
        for names in readings:
            if index >= len(names):
                continue
            try:
                candidate = resolve(names[index])
            except ValueError:
                continue
            if fallback is None:
                fallback = candidate
            if candidate.exists():
                chosen = candidate
                break
        if chosen is not None or fallback is not None:
            tracks.append(chosen or fallback)
    return tracks
