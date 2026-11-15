"""A CD sheet (``.cue`` / ``.gdi``) as the Round-trip check compares it (#300).

The Round-trip check compares a Source sheet with the sheet ``chdman
extractcd`` writes back. ``read_disc_sheet`` gives both what is compared: the
track files (each once, in sheet order) and a fingerprint of the track table.

There is no parser here. The sheet is read through ``utils.sheet_text``, the
one sheet reader: chdman's framing, tokens, size cap and decodings, and its
refusals (an unreadable or ambiguous sheet, a track outside the trusted roots).
The track files come from ``sheet_text.sheet_track_list``; the table is built
from the same reading's ``sheet_line_tokens`` (CUE) or ``gdi_tracks`` (GDI).

The table is reduced to what does not depend on how the tracks are split into
files (a Redump set has one ``.bin`` per track, chdman writes one). Per CUE
track: the type token (``MODE1/2352``, ``MODE2/2352``, ``AUDIO``, ...), its
sector size, the pregap frames held in the file (``INDEX 01`` minus ``INDEX
00``), the ``PREGAP`` and ``POSTGAP`` frames, and the frames from ``INDEX 01``
to the end of the track. Per GDI track: start LBA, data or audio, sector size
and frames. Positions follow MAME's cue reader: ``INDEX`` times are frame
counts within the file, and a track's bytes start where the previous track in
that file ended at the previous track's sector size. Keywords are matched as
chdman matches them (case-sensitive). ``INDEX 02`` and later, ``FLAGS``,
``CATALOG`` and ``ISRC`` are not compared: a CD CHD does not store them.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from utils.sheet_text import (
    cue_file_name,
    gdi_tracks,
    sheet_line_tokens,
    sheet_readings,
    sheet_track_list,
)

Track = tuple[object, ...]

_MSF_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})$")
# What chdman infers for a bare image file, in the order it tries
# (cdrom.cpp, the raw-image branch): 2048-byte mode 1, then 2336 and 2352
# byte mode 2.
_RAW_TRACK_TYPES = (("MODE1/2048", 2048), ("MODE2/2336", 2336), ("MODE2/2352", 2352))


@dataclass(frozen=True)
class DiscSheet:
    """``tracks``: each track file once, in sheet order. ``layout``: SHA1 of
    the track table, or None when it says no more than a bare image would
    (``_fingerprint``)."""

    tracks: list[str]
    layout: str | None


def read_disc_sheet(
    sheet_path: str, *, within: Sequence[str] | None = None,
) -> DiscSheet:
    """The track files and track-table fingerprint of a ``.cue`` / ``.gdi``.

    ``within`` is passed to ``sheet_text``: the trusted roots the sheet's
    tracks must resolve under (None: the configured Volumes). Raises
    ``ValueError`` when the sheet is unreadable, ambiguous, names a refused
    track or no track at all, or has a table this cannot read; ``OSError``
    when a track file cannot be sized. Never a partial answer.
    """
    sheet = Path(sheet_path)
    ext = sheet.suffix.lower()
    if ext not in (".cue", ".gdi"):
        raise ValueError(f"not a CD sheet: {sheet.name}")
    paths = sheet_track_list(sheet, within=within)
    if not paths:
        raise ValueError(
            f"{sheet.name} is unreadable, names no track, or names a track "
            "outside the trusted folders",
        )
    readings = sheet_readings(sheet)
    if not readings:
        raise ValueError(f"{sheet.name} is unreadable")
    files = [str(p) for p in paths]
    if ext == ".cue":
        layout = _cue_layout(readings[0], files)
    else:
        layout = _gdi_layout(readings[0], files)
    unique = list(dict.fromkeys(files))
    total_bytes = sum(os.path.getsize(f) for f in unique)
    return DiscSheet(tracks=unique, layout=_fingerprint(layout, total_bytes))


def _frames(msf: str) -> int:
    match = _MSF_RE.match(msf)
    if not match:
        raise ValueError(f"bad MSF time {msf!r}")
    minutes, seconds, frames = (int(g) for g in match.groups())
    return (minutes * 60 + seconds) * 75 + frames


def _sector_size(track_type: str) -> int:
    if track_type == "AUDIO":
        return 2352
    if track_type == "CDG":
        return 2448
    _, _, size = track_type.partition("/")
    if not size.isdigit():
        raise ValueError(f"unknown track type {track_type!r}")
    return int(size)


def _cue_layout(text: str, files: list[str]) -> list[Track]:
    """The CUE track table. ``files`` is ``sheet_track_list``'s answer: one
    resolved path per ``FILE`` line, in order."""
    tracks: list[dict] = []
    current_file: str | None = None
    file_index = 0
    for tokens in sheet_line_tokens(text):
        if not tokens:
            continue
        keyword = tokens[0]
        if cue_file_name(tokens) is not None:
            if file_index >= len(files):
                raise ValueError("more FILE lines than tracks resolved")
            current_file = files[file_index]
            file_index += 1
        elif keyword == "TRACK" and len(tokens) >= 3:
            if current_file is None:
                raise ValueError("TRACK before any FILE")
            tracks.append({
                "type": tokens[2], "file": current_file,
                "index": {}, "pregap": 0, "postgap": 0,
            })
        elif keyword == "INDEX" and len(tokens) >= 3 and tracks:
            if not tokens[1].isdigit():
                raise ValueError(f"bad INDEX number {tokens[1]!r}")
            tracks[-1]["index"][int(tokens[1])] = _frames(tokens[2])
        elif keyword in ("PREGAP", "POSTGAP") and len(tokens) >= 2 and tracks:
            tracks[-1][keyword.lower()] = _frames(tokens[1])
    if not tracks:
        raise ValueError("the cue has no tracks")

    sizes: dict[str, int] = {}
    layout: list[Track] = []
    for i, track in enumerate(tracks):
        if 1 not in track["index"]:
            raise ValueError(f"track {i + 1} has no INDEX 01")
        size = _sector_size(track["type"])
        start = _start(track)
        nxt = tracks[i + 1] if i + 1 < len(tracks) else None
        if nxt is not None and nxt["file"] == track["file"]:
            total = _start(nxt) - start
        else:
            file_bytes = sizes.setdefault(track["file"], os.path.getsize(track["file"]))
            total = (file_bytes - _byte_start(tracks, i)) // size
        pregap_in_file = track["index"][1] - start
        layout.append((
            track["type"], size, pregap_in_file, track["pregap"],
            total - pregap_in_file, track["postgap"],
        ))
    return layout


def _start(track: dict) -> int:
    """Where the track's bytes start in its file: INDEX 00, else INDEX 01."""
    return track["index"].get(0, track["index"].get(1, 0))


def _byte_start(tracks: list[dict], i: int) -> int:
    """Byte offset of track ``i`` in its file: earlier tracks of that file at
    their own sector sizes, as MAME's cue reader lays them out."""
    track = tracks[i]
    first = i
    while first > 0 and tracks[first - 1]["file"] == track["file"]:
        first -= 1
    offset = _start(tracks[first]) * _sector_size(tracks[first]["type"])
    for j in range(first, i):
        offset += (_start(tracks[j + 1]) - _start(tracks[j])) * _sector_size(
            tracks[j]["type"],
        )
    return offset


def _gdi_layout(text: str, files: list[str]) -> list[Track]:
    """The GDI track table, from ``sheet_text.gdi_tracks`` (aligned with
    ``files``: one resolved path per track line)."""
    lines = gdi_tracks(text)
    if not lines or len(lines) != len(files):
        raise ValueError("the gdi tracks do not match its files")
    layout: list[Track] = []
    for line, path in zip(lines, files):
        if not line.sector_size or line.lba is None or line.track_type is None:
            raise ValueError(f"unreadable gdi track line for {line.name!r}")
        frames = os.path.getsize(path) // line.sector_size
        kind = "AUDIO" if line.track_type == 0 else "DATA"
        layout.append((line.lba, kind, line.sector_size, frames))
    return layout


def _fingerprint(layout: list[Track], total_bytes: int) -> str | None:
    """SHA1 of the track table, or None when it adds nothing.

    None when the table is exactly what chdman infers for a bare image of the
    same bytes (one track, no gaps): an ``.iso`` Source and the one-track cue a
    ``createcd`` of it extracts to then compare by their bytes alone.
    """
    if layout == raw_image_layout(total_bytes):
        return None
    text = "\n".join(",".join(str(v) for v in track) for track in layout)
    return hashlib.sha1(  # nosec # nosemgrep: insecure-hash-algorithm-sha1
        text.encode(), usedforsecurity=False,
    ).hexdigest()


def raw_image_layout(size: int) -> list[Track] | None:
    """The one-track table chdman gives a bare image file of ``size`` bytes."""
    for track_type, sector in _RAW_TRACK_TYPES:
        if size > 0 and size % sector == 0:
            return [(track_type, sector, 0, 0, size // sector, 0)]
    return None
