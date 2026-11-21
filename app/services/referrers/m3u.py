"""The ``.m3u`` Referrer: multi-disc playlists that name an Image (#295).

Always on. Playlists are looked for in the replaced file's own folder and in
every folder above it up to and including its Volume root (only the files
directly in those folders, no recursive walk). That covers a playlist beside
the discs and the Batocera / ES-DE layout with ``Game.m3u`` in the system
folder and the discs in a (hidden) subfolder.

One reader (``_read_line``) reads every line the way a C byte reader does:
first-line BOM dropped, C-locale spaces trimmed (Unicode spaces are part of
the name), ``#`` lines skipped, a RetroArch ``path|label`` split at the first
``|``, quotes around the path dropped (as some readers do; RetroArch keeps
them, and ``_rewrite`` checks that reading too, failing safe), and a slash-free entry with
backslashes read as backslash-separated.

An entry is rewritten only when its path resolves exactly to a replaced file,
matched by ``(st_dev, st_ino)`` so any spelling that reaches the file counts.
Its label, quotes and surrounding whitespace are kept. Everything else is left
byte for byte: comment and directive lines, other entries, blank lines, line
endings and a UTF-8 BOM. A playlist that is UTF-16 or holds a NUL byte is never
rewritten, and keeps the Source if any reading of it may name a replaced file.
A playlist that isn't valid UTF-8 keeps the Source if a cp1252 reading may name
one (``_legacy_text_check``); otherwise it is rewritten like any other, its
other bytes kept as they were.

Fail safe: an entry that does not resolve but whose file name (splitting on
either separator, ignoring case and Unicode form) is a replaced file's name is
not guessed at. Planning raises ``ReferrerError`` naming the playlist and
line, so the Source is kept.

A rewritten entry keeps its own prefix and separator style: the path from the
old file's folder to the output is appended to the entry's own folder part,
with both sides taken as resolved paths.

Each playlist has a lock, in-process and cross-process
(``lock_manager.referrer_lock``). ``apply`` re-reads the playlist under both
and rewrites what is there now, so two jobs updating one playlist at once
both land, in one process or two (a CLI run beside the server).
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from utils.path_utils import file_identity

from .base import Phase, Referrer, ReferrerChange, ReferrerError
from .files import (
    Renames,
    Target,
    dirs_for,
    fold,
    may_read,
    named_file,
    points_at,
    rewrite_in_place,
    search_dirs,
    target_lock,
)
from .files import read_target as _read

PLAYLIST_EXT = ".m3u"
# How long ``apply`` waits for another process to finish rewriting the same
# playlist. Inside the hook the wait also ends as soon as the job stops waiting
# (``check_write``), so it never outlasts the hook's own deadline.
PLAYLIST_LOCK_WAIT_SECONDS = 30.0
_BOM = b"\xef\xbb\xbf"
_ENC = ("utf-8", "surrogateescape")


def _playlists(directory: str) -> list[str]:
    try:
        with os.scandir(directory) as it:
            return sorted(
                e.path for e in it
                if e.name.lower().endswith(PLAYLIST_EXT) and e.is_file()
            )
    except (FileNotFoundError, NotADirectoryError):
        return []


def _new_entry(posix_entry: str, target: Target) -> list[str]:
    """Candidate new entries, preferred first: the entry's own folder part plus
    the step from the replaced file's folder to the output. When the output is
    in another folder the tidy (normalised) form comes first, then the literal
    one, which still resolves through a symlinked folder part."""
    folder, sep, _ = posix_entry.rpartition("/")
    step = os.path.relpath(target.new, target.old_dir)
    literal = folder + sep + step
    return [literal] if "/" not in step else [os.path.normpath(literal), literal]


# C byte readers trim only the C-locale spaces (libretro's ISSPACE, used by
# RetroArch's string_trim_whitespace); Unicode spaces (NBSP, U+3000, U+2028,
# ...) stay part of the name.
_WS = " \t\r\n\v\f"


@dataclass(frozen=True)
class _Line:
    """One entry line as a reader reads it."""

    entry: str        # the trimmed entry as written (for messages and edits)
    path: str         # the path: label and surrounding quotes removed
    posix: str        # the path with backslashes read as separators
    backslashed: bool
    before: str       # the line's text before the path
    after: str        # the line's text after the path (quote, label, spaces)


def _parse_line(text: str) -> _Line | None:
    """How a reader reads one decoded line, BOM already removed: None for a
    blank or "#" line. Trims ASCII whitespace only, splits a RetroArch
    ``path|label`` at the first "|", and drops quotes around the path."""
    start = len(text) - len(text.lstrip(_WS))
    end = len(text.rstrip(_WS))
    entry = text[start:end]
    if not entry or entry.startswith("#"):
        return None
    head, label_sep, _ = entry.partition("|")
    core = head.rstrip(_WS) if label_sep else head
    quote = ""
    if len(core) >= 2 and core[0] == core[-1] and core[0] in "\"'":
        quote, core = core[0], core[1:-1]
    backslashed = "\\" in core and "/" not in core
    return _Line(
        entry=entry,
        path=core,
        posix=core.replace("\\", "/") if backslashed else core,
        backslashed=backslashed,
        before=text[:start] + quote,
        after=quote + entry[len(core) + 2 * len(quote):] + text[end:],
    )


def _split_bom(line: bytes, first: bool) -> tuple[bytes, bytes]:
    """A reader drops a UTF-8 BOM at the very start of the file."""
    if first and line.startswith(_BOM):
        return _BOM, line[len(_BOM):]
    return b"", line


def _read_line(line: bytes, first: bool) -> _Line | None:
    """How a reader reads one raw line (no line ending): the single parser
    used for planning, rewriting and every read-back check."""
    return _parse_line(_split_bom(line, first)[1].decode(*_ENC))


def _lines(data: bytes) -> list[bytes]:
    """Split like a byte reader: on CR and LF only (never U+2028 and friends)."""
    return data.splitlines()


def entries(data: bytes) -> list[str]:
    """The entries a reader sees in playlist *data*, as posix paths, in order."""
    found = []
    for index, line in enumerate(_lines(data)):
        parsed = _read_line(line, first=index == 0)
        if parsed is not None:
            found.append(parsed.posix)
    return found


def every_entry(data: bytes) -> list[str]:
    """Every entry any reader could see in playlist *data*, as posix paths.

    Plain 8-bit text as :func:`entries` (UTF-8), cp1252 and Latin-1. A playlist
    that isn't (UTF-16, or any
    NUL byte) is read every way :func:`_wide_text_check` reads it: UTF-16 by
    its BOM or NUL parity (strictly, else with replacements), both byte orders,
    and with NULs dropped as UTF-8 and cp1252. The union is what a check that
    must not miss a disc (who else needs this file?) compares against; a
    reading that leaves a NUL in a name is no path and is dropped.
    """
    encoding = _utf16_encoding(data)
    if encoding is None and b"\x00" not in data:
        # A player may read 8-bit text in a legacy code page, so a name is
        # owned if any reading spells it; Latin-1 decodes every byte, so no
        # line is left unread.
        found = entries(data)
        body = data.removeprefix(_BOM)
        for codec in ("cp1252", "latin-1"):
            for line in body.decode(codec, errors="replace").splitlines():
                parsed = _parse_line(line)
                if parsed is not None:
                    found.append(parsed.posix)
        return list(dict.fromkeys(found))
    texts: list[str] = []
    if encoding is not None:
        texts.append(data.decode(encoding, errors="replace"))
    stripped = data.replace(b"\x00", b"")
    texts += [
        stripped.decode("utf-8", errors="replace"),
        stripped.decode("cp1252", errors="replace"),
        data.decode("utf-16-le", errors="replace"),
        data.decode("utf-16-be", errors="replace"),
    ]
    found: list[str] = []
    for text in texts:
        for line in text.lstrip("\ufeff").splitlines():
            parsed = _parse_line(line)
            if parsed is not None and "\x00" not in parsed.posix:
                found.append(parsed.posix)
    return list(dict.fromkeys(found))


def _variants(candidate: str, backslashed: bool) -> list[str]:
    """Ways to write *candidate*, preferred first: in the entry's own style,
    then protected by "./" (against a leading "#", whitespace, a BOM
    character, or a backslash a reader would split on), then forward-slash
    forms if the entry's backslash style can't carry it."""
    plain = [candidate]
    if not candidate.startswith(("/", "./")):
        plain.append(f"./{candidate}")
    forms = [c.replace("/", "\\") for c in plain] if backslashed else []
    return list(dict.fromkeys(forms + plain))


def _choose_entry(
    candidates: list[str], backslashed: bool, playlist_dir: str, target: Target,
    render: Callable[[str], bytes], first: bool, strict: bool,
) -> str | None:
    """The first way of writing a candidate that a reader reads back, from its
    rendered line, as an entry reaching *target*; None if there is none."""
    for candidate in candidates:
        for written in _variants(candidate, backslashed):
            # A control character (CR, LF, NUL, VT, ...) can't sit in one line.
            if any(unicodedata.category(ch) == "Cc" for ch in written):
                continue
            parsed = _read_line(render(written), first)
            if parsed is not None and points_at(
                playlist_dir, parsed.posix, target, strict=strict,
            ):
                return written
    return None


def entry_for(playlist_dir: str, path: str, *, first_line: bool) -> str | None:
    """A playlist entry, relative to *playlist_dir*, that a reader reads back
    to *path*, or None if it can't be written as one line. For code that
    writes new playlists, so they encode entries exactly as the Referrer does."""
    real_dir = os.path.realpath(playlist_dir)
    target = Target(real_dir, os.path.realpath(path))
    step = os.path.relpath(target.new, real_dir)
    return _choose_entry(
        [step], False, real_dir, target, lambda e: e.encode(*_ENC), first_line,
        strict=False,
    )


def playlist_bytes(lines: list[str]) -> bytes:
    """Render a fresh playlist from entries made by ``entry_for``: one per line,
    ``\\n`` endings, no BOM, UTF-8 with ``surrogateescape``. The rendered file
    is read back with ``entries``; a ``ValueError`` is raised unless that gives
    exactly *lines*. Write it with ``write_new_bytes``."""
    data = "".join(line + "\n" for line in lines).encode(*_ENC)
    if entries(data) != lines:
        raise ValueError("an entry would not read back as written")
    return data


def _utf16_encoding(data: bytes) -> str | None:
    """The UTF-16 flavour *data* is in, or None if it isn't UTF-16.

    A BOM names it. Without one, only text whose NULs fill most of one byte
    parity counts (ASCII in UTF-16 has a NUL in every other byte: odd offsets
    little-endian, even offsets big-endian). One stray NUL in an 8-bit file
    (a C-string terminator) is not UTF-16.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    even, odd = data[0::2], data[1::2]
    if odd and odd.count(0) * 2 > len(odd):
        return "utf-16-le"
    if even and even.count(0) * 2 > len(even):
        return "utf-16-be"
    return None


def _wide_text_check(playlist: str, data: bytes, renames: Renames) -> bool:
    """For a playlist that isn't plain 8-bit text (UTF-16, or any NUL byte):
    it is never rewritten. Raise if any reading of it could name a replaced
    file, so the Source is kept; return True (leave it alone) otherwise.
    False for a normal playlist.

    UTF-16 is decoded strictly. Anything else with a NUL (or UTF-16 that
    doesn't decode) is undecodable, so it is read every way that could name
    the Source: UTF-16 with replacements, and with NULs dropped as UTF-8 and
    as cp1252.
    """
    encoding = _utf16_encoding(data)
    if encoding is None and b"\x00" not in data:
        return False
    readings: list[tuple[str, str]] = []  # (how it was read, text)
    if encoding is not None:
        try:
            readings = [("UTF-16", data.decode(encoding))]
        except UnicodeDecodeError:
            readings = [("UTF-16", data.decode(encoding, errors="replace"))]
    # The UTF-16 guess may be wrong (an 8-bit file with a zero-filled tail
    # fills one parity with NULs), and a file can mix content (an 8-bit line
    # appended to a UTF-16 one). A NUL-stripped 8-bit reading is what a line
    # reader sees, so always check those too: for real UTF-16 ASCII text it
    # is the same text, so this adds no false keeps.
    stripped = data.replace(b"\x00", b"")
    readings += [
        ("NUL bytes", stripped.decode("utf-8", errors="replace")),
        ("NUL bytes", stripped.decode("cp1252", errors="replace")),
        # Long non-ASCII names in UTF-16 have too few NULs for the parity
        # guess: read both byte orders as well.
        ("UTF-16", data.decode("utf-16-le", errors="replace")),
        ("UTF-16", data.decode("utf-16-be", errors="replace")),
    ]
    for kind, text in readings:
        folded = fold(text)
        mention = next((n for n in sorted(renames.names) if n and n in folded), None)
        if mention:
            raise ReferrerError(
                playlist, f"isn't plain text ({kind}) and may name {mention}",
            )
    return True


def _legacy_text_check(playlist: str, data: bytes, renames: Renames) -> None:
    """A playlist that isn't valid UTF-8 (Windows-1252, Latin-1) can't be
    matched reliably by name. Read it as cp1252 only to look for a replaced
    file's name; if any entry could name one, keep the Source rather than
    rewrite it. Otherwise it goes through the normal rewrite, whose
    ``surrogateescape`` decoding leaves its other bytes as they were."""
    try:
        data.decode("utf-8")
        return
    except UnicodeDecodeError:
        pass
    text = data.removeprefix(_BOM).decode("cp1252", errors="replace")
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is not None and named_file(parsed.path) in renames.names:
            raise ReferrerError(
                playlist,
                f"playlist isn't UTF-8 and may name this disc ({named_file(parsed.path)})",
            )


def _raw_names_a_source(entry: str, playlist_dir: str, renames: Renames) -> bool:
    """Whether the whole trimmed *entry*, read literally (label and quotes
    kept), reaches or names a replaced file."""
    posix = entry.replace("\\", "/") if "\\" in entry and "/" not in entry else entry
    identity = file_identity(os.path.join(playlist_dir, posix))
    if identity is not None and renames.target_for(identity) is not None:
        return True
    return identity is None and named_file(entry) in renames.names


# A BOM is the only safe way to know a playlist is UTF-16 (#334).
_UTF16_BOMS = ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))


def _rewrite_utf16(
    playlist: str, data: bytes, bom: bytes, codec: str, playlist_dir: str,
    renames: Renames, *, strict: bool,
) -> tuple[bytes, list[tuple[str, str]]] | None:
    """Rewrite a UTF-16 playlist with a BOM the way a UTF-8 one is rewritten.

    The text after the BOM is decoded strictly, run through the same rewrite
    and read-back as a UTF-8 playlist, and encoded back with the same BOM and
    byte order; its line endings are part of the text, so they come back too.
    None when the text doesn't decode strictly (the caller's fail-safe for
    undecodable playlists then applies).

    One rule guards the readers that don't honour the BOM (a line reader can
    see an 8-bit line appended to the file): the bytes that would be left on
    disk, rewritten or not, are read back every way the checker knows (the
    UTF-16 text, and the NUL-stripped bytes as UTF-8 and as cp1252). Each
    reading goes through ``_text_reaches_replaced``, the same per-line check
    ``_rewrite`` uses (parsed path, literal line, quoted form, label split).
    If any reading still reaches a replaced file, the Source is kept.
    """
    try:
        text = data[len(bom):].decode(codec)
    except UnicodeDecodeError:
        return None
    # The UTF-8 stand-in gets a BOM of its own, so `_rewrite` strips exactly
    # that one: a first filename that itself starts with U+FEFF stays part of
    # the name for matching and read-back.
    updated, edits = _rewrite(
        playlist, _BOM + text.encode("utf-8"), playlist_dir, renames, strict=strict,
    )
    result = data
    if edits:
        try:
            result = bom + updated[len(_BOM):].decode(*_ENC).encode(codec)
        except UnicodeEncodeError as exc:
            raise ReferrerError(playlist, "the new entry can't be written in UTF-16") from exc
    stripped = result.replace(b"\x00", b"")
    for reading in (result[len(bom):].decode(codec),
                    stripped.decode("utf-8", errors="replace"),
                    stripped.decode("cp1252", errors="replace")):
        stale = _text_reaches_replaced(reading, playlist_dir, renames)
        if stale is not None:
            raise ReferrerError(
                playlist, f"would still name {stale} when read another way",
            )
    return result, edits


def _entry_reaches_replaced(
    parsed: _Line, playlist_dir: str, renames: Renames,
) -> tuple[Target | None, str | None]:
    """Whether one entry line reaches a replaced file, by any reading a
    player could use. The one checker: ``_rewrite`` uses it for every line,
    and the UTF-16 path for every reading of its result.

    Returns ``(target, name)``. ``target`` is set when the line's path (label
    split, quotes dropped) resolves to a replaced file: that line can be
    rewritten. ``name`` alone means another reading names or reaches one and
    the line can't be rewritten safely: the path doesn't resolve but names a
    replaced file, or the literal line (a plain line reader: no label split,
    no unquoting) or the label-split path with its quotes kept (RetroArch)
    reaches or names one. ``(None, None)``: no reading does."""
    identity = file_identity(os.path.join(playlist_dir, parsed.posix))
    target = renames.target_for(identity)
    if target is not None:
        return target, named_file(parsed.path)
    if identity is None and named_file(parsed.path) in renames.names:
        return None, named_file(parsed.path)
    if parsed.path != parsed.entry:
        label_split = parsed.entry.partition("|")[0].rstrip(_WS)
        for literal in dict.fromkeys((parsed.entry, label_split)):
            if literal and _raw_names_a_source(literal, playlist_dir, renames):
                return None, named_file(literal)
    return None, None


def _text_reaches_replaced(text: str, playlist_dir: str, renames: Renames) -> str | None:
    """The first replaced file decoded playlist *text* still reaches, by any
    reading of any entry line (``_entry_reaches_replaced``), or None."""
    for line in re.split(r"\r\n|\r|\n", text):
        parsed = _parse_line(line)
        if parsed is None:
            continue
        target, name = _entry_reaches_replaced(parsed, playlist_dir, renames)
        if target is not None or name is not None:
            return name or "a replaced file"
    return None


def _rewrite(
    playlist: str, data: bytes, playlist_dir: str, renames: Renames, *, strict: bool,
) -> tuple[bytes, list[tuple[str, str]]]:
    for bom, codec in _UTF16_BOMS:
        if data.startswith(bom):
            done = _rewrite_utf16(
                playlist, data, bom, codec, playlist_dir, renames, strict=strict,
            )
            if done is not None:
                return done
    if _wide_text_check(playlist, data, renames):
        return data, []
    _legacy_text_check(playlist, data, renames)
    out: list[bytes] = []
    edits: list[tuple[str, str]] = []
    rewritten: dict[int, Target] = {}  # entry position -> where it must point
    position = -1
    for number, line in enumerate(data.splitlines(keepends=True), start=1):
        first = number == 1
        prefix, line = _split_bom(line, first)
        body = line.rstrip(b"\r\n")
        ending = line[len(body):]
        parsed = _read_line(prefix + body, first)
        if parsed is None:
            out.append(prefix + line)
            continue
        position += 1
        target, named = _entry_reaches_replaced(parsed, playlist_dir, renames)
        if target is None:
            # Another reading names or reaches a replaced file but this one
            # can't be rewritten: don't guess, keep the Source.
            if named is not None:
                raise ReferrerError(
                    playlist, f"line {number} names {named} but doesn't point at it",
                )
            out.append(prefix + line)
            continue
        new_entry = _choose_entry(
            _new_entry(parsed.posix, target), parsed.backslashed, playlist_dir, target,
            lambda e, p=prefix, a=parsed.before, z=parsed.after: p + (a + e + z).encode(*_ENC),
            first, strict,
        )
        if new_entry is None:
            raise ReferrerError(
                playlist, f"line {number}: can't write a path that reaches the output",
            )
        new_line = parsed.before + new_entry + parsed.after
        out.append(prefix + new_line.encode(*_ENC) + ending)
        edits.append((parsed.entry, new_line.strip(_WS)))
        rewritten[position] = target
    result = b"".join(out)
    _check_read_back(playlist, playlist_dir, data, result, rewritten, strict)
    return result, edits


def _check_read_back(
    playlist: str, playlist_dir: str, before: bytes, after: bytes,
    rewritten: dict[int, Target], strict: bool,
) -> None:
    """Parse the bytes about to be written the way _rewrite reads them: the
    same number of entries, every untouched one unchanged, and every rewritten
    one, at its position, reaching the output. Anything else keeps the Source."""
    old, new = entries(before), entries(after)
    ok = len(old) == len(new) and all(
        points_at(playlist_dir, new[i], rewritten[i], strict=strict) if i in rewritten
        else new[i] == old[i]
        for i in range(len(new))
    )
    if not ok:
        raise ReferrerError(playlist, "the rewritten playlist wouldn't read back to the output")


class M3uReferrer(Referrer):
    id = "m3u"
    label = ".m3u playlist"
    opt_in = False
    phase = Phase.FILE

    def detect(self, image_path: str) -> bool:
        return any(_playlists(d) for d in search_dirs(image_path))

    def plan(self, renames: Mapping[str, str]) -> list[ReferrerChange]:
        # Names as written and as a reader trims them, so a leading ASCII
        # space doesn't hide a mention.
        targets = Renames.of(renames, lambda name: (name, name.strip(_WS)))
        changes = []
        for directory in dirs_for(renames):
            for playlist in _playlists(directory):
                # The walk's upper limit may come from a cached root spelling
                # (right for lock probing); `may_read` decides against the
                # Volumes as they are now.
                if not may_read(playlist):
                    continue
                with target_lock(playlist):
                    _, edits = _rewrite(
                        playlist, _read(playlist), directory, targets, strict=False,
                    )
                if edits:
                    changes.append(ReferrerChange(
                        referrer=self.id, target=playlist, edits=tuple(edits),
                        payload=(directory, targets),
                    ))
        return changes

    def apply(self, change: ReferrerChange) -> None:
        path = change.target
        directory, targets = change.payload

        def rewrite(original: bytes) -> bytes | None:
            # strict: the output must exist now, however late this runs.
            updated, edits = _rewrite(path, original, directory, targets, strict=True)
            return updated if edits else None

        # `_read` is looked up here, on every apply, so a patched reader
        # (the cross-process test's slow read) is the one used.
        rewrite_in_place(path, rewrite, read=_read, wait=PLAYLIST_LOCK_WAIT_SECONDS)
