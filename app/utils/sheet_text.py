"""Reading track sheets (``.cue`` / ``.gdi``) the one way the app does.

Sheets come in UTF-8 (with or without a BOM) and in Windows-1252, and a track
name decoded the wrong way names a file that is not there. Every reader of a
sheet decodes it through here: the disc-ID probe (``services.disc_id``) and
the delete plan and header-dependency list (``utils.delete_plan``).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

_UTF8_BOM = b"\xef\xbb\xbf"
# A track sheet is a few hundred bytes; anything far larger is not one.
MAX_SHEET_BYTES = 1024 * 1024


def sheet_readings(path: str | Path) -> list[str]:
    """The sheet's text, in the order its spellings should be tried.

    First the sheet as UTF-8 with a BOM stripped, or as cp1252 when it is not
    valid UTF-8; then Latin-1, which decodes any bytes and is the reading the
    disc-ID probe always used. Empty when the sheet cannot be read.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SHEET_BYTES)
    except OSError:
        return []
    raw = raw.removeprefix(_UTF8_BOM)
    try:
        first = raw.decode("utf-8")
    except UnicodeDecodeError:
        first = raw.decode("cp1252", errors="replace")
    return [first, raw.decode("latin-1")]


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
