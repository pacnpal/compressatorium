"""The track table of a CD sheet (``.cue`` / ``.gdi``), independent of file layout.

The Round-trip check (#300) compares a Source sheet with the sheet ``chdman
extractcd`` writes back. Their track bytes may be split across files
differently (a Redump set has one ``.bin`` per track, chdman writes one), so
the table is reduced to what does not depend on that split. Per track, for a
cue: the type token (``MODE1/2352``, ``MODE2/2352``, ``AUDIO``, ...), its sector
size, the pregap frames held in the file (``INDEX 01`` minus ``INDEX 00``), the
``PREGAP`` and ``POSTGAP`` frames, and the frames from ``INDEX 01`` to the end of
the track. For a gdi: the start LBA, data or audio, sector size and frames.

Positions follow MAME's cue reader: ``INDEX`` times are frame counts within the
file, and a track's bytes start where the previous track in that file ended at
the previous track's sector size. ``INDEX 02`` and later, ``FLAGS``, ``CATALOG``
and ``ISRC`` are not compared: a CD CHD does not store them.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
from pathlib import Path

from utils.sheet_text import resolve_track as resolve_track_path

Track = tuple[object, ...]

_MSF_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})$")
# What chdman infers for a bare image file, in the order it tries
# (cdrom.cpp, the raw-image branch): 2048-byte mode 1, then 2336 and 2352
# byte mode 2.
_RAW_TRACK_TYPES = (("MODE1/2048", 2048), ("MODE2/2336", 2336), ("MODE2/2352", 2352))


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


def _split(line: str) -> list[str]:
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def _cue_layout(sheet: Path) -> tuple[list[Track], int]:
    tracks: list[dict] = []
    current_file: str | None = None
    for raw in sheet.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = _split(raw.strip())
        if not parts:
            continue
        keyword = parts[0].upper()
        if keyword == "FILE" and len(parts) >= 2:
            current_file = str(resolve_track_path(sheet.parent, parts[1]))
        elif keyword == "TRACK" and len(parts) >= 3:
            if current_file is None:
                raise ValueError("TRACK before any FILE")
            tracks.append({
                "type": parts[2].upper(), "file": current_file,
                "index": {}, "pregap": 0, "postgap": 0,
            })
        elif keyword == "INDEX" and len(parts) >= 3 and tracks:
            tracks[-1]["index"][int(parts[1])] = _frames(parts[2])
        elif keyword in ("PREGAP", "POSTGAP") and len(parts) >= 2 and tracks:
            tracks[-1][keyword.lower()] = _frames(parts[1])
    if not tracks:
        raise ValueError(f"{sheet.name} has no tracks")

    sizes: dict[str, int] = {}
    layout: list[Track] = []
    for i, track in enumerate(tracks):
        if 1 not in track["index"]:
            raise ValueError(f"track {i + 1} has no INDEX 01")
        size = _sector_size(track["type"])
        start = track["index"].get(0, track["index"][1])
        nxt = tracks[i + 1] if i + 1 < len(tracks) else None
        if nxt is not None and nxt["file"] == track["file"]:
            total = nxt["index"].get(0, nxt["index"].get(1, start)) - start
        else:
            file_bytes = sizes.setdefault(track["file"], os.path.getsize(track["file"]))
            total = (file_bytes - _byte_start(tracks, i)) // size
        pregap_in_file = track["index"][1] - start
        layout.append((
            track["type"], size, pregap_in_file, track["pregap"],
            total - pregap_in_file, track["postgap"],
        ))
    for track in tracks:
        sizes.setdefault(track["file"], os.path.getsize(track["file"]))
    return layout, sum(sizes.values())


def _byte_start(tracks: list[dict], i: int) -> int:
    """Byte offset of track ``i`` in its file: earlier tracks of that file at
    their own sector sizes, as MAME's cue reader lays them out."""
    track = tracks[i]
    first = i
    while first > 0 and tracks[first - 1]["file"] == track["file"]:
        first -= 1
    first_track = tracks[first]
    offset = first_track["index"].get(0, first_track["index"][1]) * _sector_size(
        first_track["type"],
    )
    for j in range(first, i):
        here = tracks[j]["index"].get(0, tracks[j]["index"][1])
        nxt = tracks[j + 1]["index"].get(0, tracks[j + 1]["index"][1])
        offset += (nxt - here) * _sector_size(tracks[j]["type"])
    return offset


def _gdi_layout(sheet: Path) -> tuple[list[Track], int]:
    lines = [
        _split(line.strip())
        for line in sheet.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip()
    ]
    if lines and len(lines[0]) == 1 and lines[0][0].isdigit():
        lines = lines[1:]
    layout: list[Track] = []
    total_bytes = 0
    for parts in lines:
        if len(parts) < 5:
            continue
        lba, kind, size = int(parts[1]), int(parts[2]), int(parts[3])
        file_bytes = os.path.getsize(resolve_track_path(sheet.parent, parts[4]))
        total_bytes += file_bytes
        layout.append((lba, "AUDIO" if kind == 0 else "DATA", size, file_bytes // size))
    if not layout:
        raise ValueError(f"{sheet.name} has no tracks")
    return layout, total_bytes


def layout_fingerprint(sheet_path: str) -> str | None:
    """SHA1 of the sheet's track table, or None when it adds nothing.

    None when the table is exactly what chdman infers for a bare image of the
    same bytes (one track, no gaps): an ``.iso`` Source and the one-track cue a
    ``createcd`` of it extracts to then compare by their bytes alone. Raises
    ``ValueError`` or ``OSError`` when the sheet cannot be read.
    """
    sheet = Path(sheet_path)
    ext = sheet.suffix.lower()
    if ext == ".cue":
        layout, total_bytes = _cue_layout(sheet)
    elif ext == ".gdi":
        layout, total_bytes = _gdi_layout(sheet)
    else:
        raise ValueError(f"not a CD sheet: {sheet.name}")
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
