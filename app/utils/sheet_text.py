"""Reading track sheets (``.cue`` / ``.gdi``) the one way the app does.

Sheets come in UTF-8 (with or without a BOM) and in Windows-1252, and a track
name decoded the wrong way names a file that is not there. Every reader of a
sheet decodes it through here: the disc-ID probe (``services.disc_id``), the
delete plan and header-dependency list (``utils.delete_plan``), and the
Round-trip's track table (``utils.disc_sheet``).

The converter opens the files a sheet names itself, so a sheet is read here
exactly the way chdman 0.285 reads it (``src/lib/util/cdrom.cpp`` at
mame0285: ``parse_cue``, ``parse_gdi``, ``tokenize``). A difference would let a
name pass ``resolve_track`` while chdman opens another one:

* framing: ``fgets(linebuffer, 511)`` reads at most ``CHD_READ_BYTES`` (510)
  bytes, ending at ``\\n``. A longer line is read in pieces and each piece is
  parsed as a line, so a ``REM`` padded past 510 bytes would hide a ``FILE``.
  A sheet with such a line, or with a NUL byte (which ends chdman's token
  early), is unreadable, never partly parsed;
* tokens (``chd_tokens``): C-locale whitespace separates them; ``"`` and
  ``'`` each toggle a quoted run in which the other kind is literal, and are
  dropped; there is no comment syntax;
* a CUE track is the token after a ``FILE`` token (case-sensitive); a GDI
  track is the fifth token of a track line;
* the file opened is the sheet's folder plus that token, verbatim.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_UTF8_BOM = b"\xef\xbb\xbf"
# A track sheet is a few hundred bytes; anything far larger is not one.
MAX_SHEET_BYTES = 1024 * 1024
# chdman's `fgets(linebuffer, 511, ...)`: the most bytes one read returns,
# newline included. A line that needs a second read makes the sheet unreadable.
CHD_READ_BYTES = 510
# C `isspace` in the "C" locale, which chdman's `tokenize` uses.
_C_SPACE = frozenset(" \t\n\v\f\r")


def _chd_framing_ok(raw: bytes) -> bool:
    """Whether chdman reads every line of ``raw`` in one ``fgets`` call."""
    if b"\0" in raw:
        return False
    # A line plus its "\n" must fit one read; the last line may lack the "\n".
    return all(len(line) < CHD_READ_BYTES for line in raw.split(b"\n"))


def chd_tokens(line: str) -> list[str]:
    """Every token of ``line`` as chdman 0.285's ``tokenize`` returns them.

    Leading C-locale whitespace is skipped; a token runs to the next
    whitespace outside quotes. ``"`` toggles a double-quoted run unless inside
    single quotes, ``'`` a single-quoted run unless inside double quotes; the
    toggling quote characters are dropped and everything else is kept, so
    ``a"b c"d`` is ``ab cd`` and an unclosed quote runs to the end of the line.
    A quoted empty string is an empty token.
    """
    return _chd_scan(line)[0]


def _chd_scan(line: str) -> tuple[list[str], bool]:
    """``chd_tokens`` plus whether every quote on the line closed."""
    tokens: list[str] = []
    i, end = 0, len(line)
    while True:
        while i < end and line[i] in _C_SPACE:
            i += 1
        if i >= end:
            return tokens, True
        token: list[str] = []
        single = double = False
        while i < end:
            char = line[i]
            if not single and char == '"':
                double = not double
            elif not double and char == "'":
                single = not single
            elif not single and not double and char in _C_SPACE:
                break
            else:
                token.append(char)
            i += 1
        tokens.append("".join(token))
        if single or double:
            return tokens, False


def _quotes_close(text: str, *, cue: bool) -> bool:
    """Whether every line's quotes close the way chdman reads them.

    An unclosed quote runs to the end of chdman's read, newline included, so
    ``1 0 4 2048 'pipe`` opens a file named ``pipe\\n`` while a reader that
    strips the newline checks ``pipe``. A CUE ``REM`` line is exempt: chdman
    reads no file name from one, and comments carry apostrophes.
    """
    for line in _sheet_lines(text):
        tokens, closed = _chd_scan(line)
        if closed or (cue and tokens and tokens[0] == "REM"):
            continue
        return False
    return True


def _sheet_lines(text: str) -> list[str]:
    # fgets splits on "\n" only; "\r" and the other line breaks str.splitlines
    # knows are ordinary characters (and "\r" is whitespace to the tokenizer).
    return text.split("\n")


def sheet_readings(path: str | Path) -> list[str]:
    """The sheet's text, in the order its spellings should be tried.

    First the sheet as UTF-8 with a BOM stripped, or as cp1252 when it is not
    valid UTF-8; then Latin-1, which decodes any bytes and is the reading the
    disc-ID probe always used; then, when it differs from both, the
    filesystem decoding (``os.fsdecode``), which spells each name as the raw
    bytes chdman opens. A safety check reads every spelling.

    Empty when the sheet cannot be read, when it is larger than
    ``MAX_SHEET_BYTES``, and when chdman would not read it the way this module
    does (a line needing more than one ``CHD_READ_BYTES`` read, or a NUL
    byte): never a partial parse. A sheet cut at the cap would hide every
    track after it (a delete plan that orphans them, or a ``FILE "/etc/..."``
    behind a megabyte of ``REM`` lines that the tool reading the whole sheet
    still opens), so such a sheet is unreadable and every reader built on this
    treats it as unknown or unsafe.

    Empty too when the sheet is ambiguous (``sheet_ambiguity``): a decoded
    spelling and the native one name different existing files, so a reader
    acting on the decoded name (a delete plan) would touch a file chdman
    never opened. Checked here against the configured Volumes, so every
    reader built on this, the job gate included, refuses such a sheet;
    ``sheet_tracks`` checks again under its own ``within``.
    """
    spellings = _sheet_spellings(path)
    if spellings is None:
        return []
    readings, native = spellings
    if _ambiguity(path, readings, native, within=None):
        return []
    return readings


def _sheet_spellings(path: str | Path) -> tuple[list[str], str] | None:
    """``(readings, native)`` of a readable sheet, or None (see ``sheet_readings``)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SHEET_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_SHEET_BYTES or not _chd_framing_ok(raw):
        return None
    # Exactly the bytes chdman reads, BOM included: chdman does not strip a
    # BOM, so a BOM-prefixed first-line `FILE` is `﻿FILE` to it and
    # ignored. Spelled as the OS spells a file name, so its names are the
    # files chdman opens.
    native = os.fsdecode(raw)
    raw = raw.removeprefix(_UTF8_BOM)
    try:
        first = raw.decode("utf-8")
    except UnicodeDecodeError:
        first = raw.decode("cp1252", errors="replace")
    latin1 = raw.decode("latin-1")
    # Quotes and whitespace are ASCII, the same in every spelling.
    if not _quotes_close(latin1, cue=Path(path).suffix.lower() == ".cue"):
        return None
    readings = [first, latin1]
    if native not in readings:
        readings.append(native)
    return readings, native


def sheet_native_text(path: str | Path) -> str | None:
    """The sheet in its native spelling (``os.fsdecode`` of the raw bytes).

    Its names are the files chdman opens, so this is the reading a caller
    that acts on the tracks (deleting them) must use. None when the sheet is
    unreadable or ambiguous (``sheet_readings``).
    """
    spellings = _sheet_spellings(path)
    if spellings is None:
        return None
    readings, native = spellings
    if _ambiguity(path, readings, native, within=None):
        return None
    return native


def sheet_ambiguity(
    path: str | Path, *, within: Sequence[str | Path] | None = None,
) -> str | None:
    """Why the sheet's decoded and native spellings disagree on a file, or None.

    For a sheet that is not UTF-8 a decoded name (cp1252 or Latin-1, re-encoded
    as UTF-8 by the OS) and the raw bytes chdman opens can name two different
    files. When both exist the sheet is ambiguous: refuse it rather than guess.
    Names are compared through ``resolve_track`` (``within`` as there); one it
    refuses is refused on its own elsewhere.
    """
    spellings = _sheet_spellings(path)
    if spellings is None:
        return None
    readings, native = spellings
    return _ambiguity(path, readings, native, within=within)


def _ambiguity(
    path: str | Path,
    readings: Sequence[str],
    native: str,
    *,
    within: Sequence[str | Path] | None,
) -> str | None:
    parse = _parser_for(path)
    if parse is None or all(text == native for text in readings):
        return None
    folder = Path(path).parent
    native_names = parse(native)
    for text in readings:
        if text == native:
            continue
        decoded_names = parse(text)
        if len(decoded_names) != len(native_names):
            # The readings disagree on which commands name files (a BOM
            # hides a first-line FILE from chdman): a reader acting on the
            # decoded set would touch a file chdman never opened.
            return (
                "Ambiguous track sheet: its decoded text names "
                f"{len(decoded_names)} track(s), the bytes chdman reads {len(native_names)}"
            )
        for decoded, opened in zip(decoded_names, native_names):
            if decoded == opened:
                continue
            try:
                decoded_path = resolve_track(folder, decoded, within=within)
                opened_path = resolve_track(folder, opened, within=within)
            except ValueError:
                continue
            try:
                both = decoded_path.exists() and opened_path.exists()
                if both and not os.path.samefile(decoded_path, opened_path):
                    return (
                        f"Ambiguous track sheet: {decoded!r} names a different file "
                        "than the bytes chdman opens"
                    )
            except OSError:
                continue
    return None


def _parser_for(path: str | Path) -> Callable[[str], list[str]] | None:
    suffix = Path(path).suffix.lower()
    if suffix == ".cue":
        return cue_tracks
    if suffix == ".gdi":
        return gdi_track_names
    return None


def gdi_track_names(text: str) -> list[str]:
    """The file name of every ``gdi_tracks`` line, in order."""
    return [track.name for track in gdi_tracks(text)]


_WIN_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def _is_absolute_reference(ref: str) -> bool:
    ref = ref.strip()
    if not ref:
        return False
    if ref.startswith(("~", "\\\\")) or _WIN_ABS_RE.match(ref):
        return True
    return Path(ref).is_absolute()


def _under_any(real: str, roots: Sequence[str | Path]) -> bool:
    for root in roots:
        real_root = os.path.realpath(root)
        if real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep):
            return True
    return False


def resolve_track(
    sheet_dir: str | Path,
    ref: str,
    *,
    within: Sequence[str | Path] | None = None,
) -> Path:
    """The track a sheet names, resolved beside the sheet, or ``ValueError``.

    The one track resolver: the Platform probe, the detection cache's track
    list, the delete plan and the job gate all use it. Refused, so nothing
    opens them: an empty name, an absolute name (``/x``, ``C:\\x``, ``\\\\host``,
    ``~``), a name with any ``..`` segment, a name whose real path (through
    a symlink) lies outside the trusted roots, and a track that exists and is
    not a regular file or folder (``path_utils.is_special_file``: a FIFO would
    block the open). A missing track is not refused. Returns the path beside the
    sheet, not its real path, so callers see what the sheet named.

    ``within`` names the trusted roots. None (the default, used by the probe
    and ``header_reads``) means the configured Volumes; a caller whose sheet
    lives elsewhere (the job worker's extraction dir for an archived set)
    passes that root. An empty sequence is refused: it would trust nothing
    and is always a caller bug.
    """
    from utils.path_utils import (  # local: no cycle at import
        is_special_file,
        is_within_configured_volumes,
    )

    if within is not None and not list(within):
        raise ValueError("No trusted root to confine the track to")

    # Verbatim: the name chdman opens is the sheet's folder plus the token,
    # untrimmed and unquoted (`chd_tokens` already dropped the quotes). The
    # checks below read it with "\" as a separator too, which only refuses
    # more; the path checked is the literal one ("\" is a filename character
    # on POSIX, so "sub\x.bin" is one name there, as chdman opens it).
    raw = ref
    if not raw:
        raise ValueError("Empty track reference")
    normalized = raw.replace("\\", "/")
    if _is_absolute_reference(normalized):
        raise ValueError(f"Absolute track reference is not allowed: {raw}")
    if ".." in normalized.split("/"):
        raise ValueError(f"Track reference escapes source directory: {raw}")
    base = Path(os.path.normpath(str(Path(sheet_dir).absolute())))
    candidate = Path(os.path.normpath(os.path.join(str(base), raw)))
    real = os.path.realpath(candidate)
    if within is None:
        inside = is_within_configured_volumes(real, treat_archives=False)
    else:
        inside = _under_any(real, within)
    if not inside:
        raise ValueError(f"Track reference resolves outside the volumes: {raw}")
    if is_special_file(candidate):
        # A FIFO track would block the probe that opens it, as a FIFO input would.
        raise ValueError(f"Track reference is not a regular file: {raw}")
    return candidate


def sheet_tracks(
    path: str | Path,
    parse: Callable[[str], list[str]],
    *,
    within: Sequence[str | Path] | None = None,
) -> list[Path] | None:
    """Every track the sheet names, resolved and confined (``resolve_track``).

    None when the sheet is unreadable (missing, over the size cap, or framed
    in a way chdman would read differently) or when any spelling of any track
    is refused: chdman opens one of those spellings, so a reader must then say
    nothing about the sheet rather than act on the ones it was allowed to see.
    The first spelling of each name that exists wins (``pick_existing``).
    ``within`` is passed to ``resolve_track``: the configured Volumes unless a
    caller names other trusted roots.
    """
    texts = sheet_readings(path)
    if not texts:
        return None
    if within is not None and sheet_ambiguity(path, within=within):
        return None   # ambiguous under the caller's roots (an extracted set)
    readings = [parse(text) for text in texts]
    folder = Path(path).parent
    for names in readings:
        for ref in names:
            try:
                resolve_track(folder, ref, within=within)
            except ValueError:
                return None
    tracks = pick_existing(readings, lambda ref: resolve_track(folder, ref, within=within))
    if len(tracks) != len(readings[0]):
        return None
    return tracks


def sheet_track_list(
    path: str | Path, *, within: Sequence[str | Path] | None = None,
) -> list[Path] | None:
    """``sheet_tracks`` for a ``.cue`` or ``.gdi``, parser chosen by suffix.

    None for any other file, an unreadable sheet, or a refused track.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".cue":
        return sheet_tracks(path, cue_tracks, within=within)
    if suffix == ".gdi":
        return sheet_tracks(path, gdi_track_names, within=within)
    return None


def sheet_is_readable(path: str | Path) -> bool:
    """Whether the sheet can be read whole (exists, readable, within the cap)."""
    return bool(sheet_readings(path))


def cue_tracks(text: str) -> list[str]:
    """The file name of every ``FILE`` line of a ``.cue`` sheet, in order.

    As chdman's ``parse_cue`` reads it: a line whose first token
    (``chd_tokens``) is exactly ``FILE`` names the file in its second token.
    So ``FILE "Game (Track 1).bin" BINARY`` names ``Game (Track 1).bin``,
    ``FILE '../x.bin' BINARY`` names ``../x.bin``, and unquoted
    ``FILE Game (Track 1).bin BINARY`` names ``Game``. ``REM`` lines, other
    commands and a lowercase ``file`` are not tracks. The one CUE FILE-line
    parser: the disc-ID probe, the delete plan, the job gate and the
    detection cache's track list all read sheets through it.
    """
    names: list[str] = []
    for tokens in sheet_line_tokens(text):
        name = cue_file_name(tokens)
        if name is not None:
            names.append(name)
    return names


def sheet_line_tokens(text: str) -> list[list[str]]:
    """Every line of a sheet as chdman reads it, split into ``chd_tokens``.

    For a reader that needs more than the file names (the Round-trip's track
    table, ``utils.disc_sheet``): the same framing and tokens as every other
    reader here, so it sees the commands chdman sees.
    """
    return [chd_tokens(line) for line in _sheet_lines(text)]


def cue_file_name(tokens: Sequence[str]) -> str | None:
    """The file a CUE line names, when it is a ``FILE`` line (``cue_tracks``)."""
    if len(tokens) >= 2 and tokens[0] == "FILE" and tokens[1]:
        return tokens[1]
    return None


@dataclass(frozen=True)
class GdiTrack:
    """One track line of a ``.gdi``: ``number lba type sector_size "name" offset``."""

    number: str
    sector_size: int | None
    name: str
    # The start LBA and track type (0 audio, 4 data), for the Round-trip's
    # track table; None when the field is not a plain number.
    lba: int | None = None
    track_type: int | None = None


def _is_number(field: str) -> bool:
    # isdigit() is True for "²" and friends, which int() refuses; a sheet field
    # is a number only when it is plain ASCII decimal digits.
    return field.isascii() and field.isdecimal()


# A GDI sector size is 2048 or 2352; nothing real needs more digits than this,
# and int() refuses strings past Python's 4,300-digit limit anyway.
_MAX_NUMBER_DIGITS = 18


def _as_number(field: str) -> int | None:
    """``field`` as an int when it is a plausible ASCII decimal, else None."""
    if not _is_number(field) or len(field) > _MAX_NUMBER_DIGITS:
        return None
    try:
        return int(field)
    except ValueError:
        return None


def gdi_tracks(text: str) -> list[GdiTrack]:
    """Every track line of a ``.gdi`` sheet, in order.

    As chdman's ``parse_gdi`` reads it, token by token (``chd_tokens``): the
    name is the fifth token, so ``"Track 01.bin"`` keeps its space and
    ``#link/x.bin`` is a name (there is no comment syntax). The first
    non-blank line is the track count and is skipped when it is a number
    (chdman opens nothing for a sheet whose count is not one, so reading that
    line as a track only checks more). Lines with fewer than five tokens, or
    an empty name, are left out.
    """
    lines = [tokens for tokens in map(chd_tokens, _sheet_lines(text)) if tokens]
    if not lines:
        return []
    start = 1 if _is_number(lines[0][0]) else 0
    tracks: list[GdiTrack] = []
    for parts in lines[start:]:
        if len(parts) < 5 or not parts[4]:
            continue
        tracks.append(GdiTrack(
            parts[0], _as_number(parts[3]), parts[4],
            lba=_as_number(parts[1]), track_type=_as_number(parts[2]),
        ))
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


def _chd_spans(line: str) -> list[tuple[int, int]]:
    """Where each ``chd_tokens`` token of ``line`` starts and ends, its
    quotes included: the same scan, keeping positions instead of text."""
    spans: list[tuple[int, int]] = []
    i, end = 0, len(line)
    while True:
        while i < end and line[i] in _C_SPACE:
            i += 1
        if i >= end:
            return spans
        start = i
        single = double = False
        while i < end:
            char = line[i]
            if not single and char == '"':
                double = not double
            elif not double and char == "'":
                single = not single
            elif not single and not double and char in _C_SPACE:
                break
            i += 1
        spans.append((start, i))
        if single or double:
            return spans


def _track_token_lines(lines: Sequence[str], gdi: bool) -> dict[int, int]:
    """``{line index: token index}`` of every line naming a track, as
    ``cue_tracks`` / ``gdi_tracks`` read them."""
    found: dict[int, int] = {}
    first = True
    for index, line in enumerate(lines):
        tokens = chd_tokens(line)
        if not tokens:
            continue
        if gdi:
            if first and _is_number(tokens[0]):
                first = False
                continue
            first = False
            if len(tokens) >= 5 and tokens[4]:
                found[index] = 4
        elif cue_file_name(tokens) is not None:
            found[index] = 1
    return found


def _written_token(old: str, new: str) -> str | None:
    """*new* written as one token in the quoting *old* used, or quoted when
    it needs it. None when no quoting reads back as *new*."""
    if old[:1] in ("'", '"') and old[-1:] == old[:1] and len(old) >= 2 and old[0] not in new:
        return f"{old[0]}{new}{old[0]}"
    if new and not any(ch in _C_SPACE or ch in "\"'" for ch in new):
        return new
    return f'"{new}"' if '"' not in new else None


def rewrite_track_names(raw: bytes, suffix: str, names: Mapping[str, str]) -> bytes | None:
    """The sheet *raw* (a ``.cue`` or ``.gdi`` by *suffix*) with each track
    reference in *names* (``{as written: new}``) replaced, and every other
    byte kept (an Image renamed to its DAT name, #426).

    Read as chdman reads it (the native spelling, ``chd_tokens``): only the
    reference's own token changes, in the quoting it had, or in double
    quotes when the new name needs them. None when that can't be done
    exactly: the sheet isn't one chdman reads whole, a new name can't be
    written as one token, or the result doesn't read back as the old
    references with *names* applied."""
    gdi = suffix.lower() == ".gdi"
    parse = gdi_track_names if gdi else cue_tracks
    if len(raw) > MAX_SHEET_BYTES or not _chd_framing_ok(raw):
        return None
    text = os.fsdecode(raw)
    lines = _sheet_lines(text)
    for index, position in _track_token_lines(lines, gdi).items():
        line = lines[index]
        tokens = chd_tokens(line)
        new = names.get(tokens[position])
        if new is None:
            continue
        spans = _chd_spans(line)
        if len(spans) != len(tokens):
            return None
        start, end = spans[position]
        token = _written_token(line[start:end], new)
        if token is None:
            return None
        lines[index] = line[:start] + token + line[end:]
    rewritten = "\n".join(lines)
    if parse(rewritten) != [names.get(ref, ref) for ref in parse(text)]:
        return None
    out = os.fsencode(rewritten)
    return out if _chd_framing_ok(out) else None
