"""The ``.m3u`` Referrer: multi-disc playlists that name an Image (#295).

Always on. Playlists are looked for in the replaced file's own folder and in
every folder above it up to and including its Volume root (only the files
directly in those folders, no recursive walk). That covers a playlist beside
the discs and the Batocera / ES-DE layout with ``Game.m3u`` in the system
folder and the discs in a (hidden) subfolder.

An entry is rewritten only when it resolves exactly to a replaced file,
matched by ``(st_dev, st_ino)`` so any spelling that reaches the file counts.
Everything else is left byte for byte: comment and directive lines, other
entries, blank lines, line endings and a UTF-8 BOM. A playlist that isn't
valid UTF-8 is never rewritten; if it may name a replaced file the Source is
kept (``_legacy_text_check``).

Fail safe: an entry that does not resolve but whose file name (after
stripping a ``|label`` suffix and quotes, splitting on either separator,
ignoring case) is a replaced file's name is not guessed at. Planning raises
``ReferrerError`` naming the playlist and line, so the Source is kept.

A rewritten entry keeps its own prefix and separator style: the path from the
old file's folder to the output is appended to the entry's own folder part,
with both sides taken as resolved paths.

Each playlist has a lock. ``apply`` re-reads the playlist under it and
rewrites what is there now, so two jobs updating one playlist at once both
land.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from utils.path_utils import volume_root_for

from .base import Referrer, ReferrerChange, ReferrerError, atomic_write_bytes

PLAYLIST_EXT = ".m3u"
BACKUP_SUFFIX = ".bak"
_BOM = b"\xef\xbb\xbf"
_ENC = ("utf-8", "surrogateescape")

# One lock per resolved playlist path, held across read-rewrite-write.
# ponytail: entries are never removed; one small lock per playlist ever touched.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _playlist_lock(path: str) -> threading.Lock:
    key = os.path.realpath(path)
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


@dataclass(frozen=True)
class _Target:
    old_dir: str  # resolved folder of the replaced file
    new: str      # resolved output path


@dataclass(frozen=True)
class _Renames:
    by_key: dict[tuple[int, int], _Target]
    names: frozenset[str]  # casefolded file names of the replaced files


def _playlists(directory: str) -> list[str]:
    try:
        with os.scandir(directory) as it:
            return sorted(
                e.path for e in it
                if e.name.lower().endswith(PLAYLIST_EXT) and e.is_file()
            )
    except (FileNotFoundError, NotADirectoryError):
        return []


def _search_dirs(path: str) -> list[str]:
    """*path*'s resolved folder and each folder above it up to its Volume root."""
    start = os.path.dirname(os.path.realpath(path))
    root = volume_root_for(start)
    dirs = [start]
    if root is None or not (start == root or start.startswith(root.rstrip(os.sep) + os.sep)):
        return dirs
    current = start
    while current != root:
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        dirs.append(current)
    return dirs


def _file_key(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _fold(name: str) -> str:
    """Compare file names as a person would: same Unicode form, any case."""
    return unicodedata.normalize("NFC", name).casefold()


def _named_file(entry: str) -> str:
    """The file name an entry means, however it is written: no label, quotes or folders."""
    name = entry.split("|", 1)[0].strip().strip("\"'").strip()
    return _fold(re.split(r"[\\/]", name)[-1])


def _new_entry(posix_entry: str, target: _Target) -> list[str]:
    """Candidate new entries, preferred first: the entry's own folder part plus
    the step from the replaced file's folder to the output. When the output is
    in another folder the tidy (normalised) form comes first, then the literal
    one, which still resolves through a symlinked folder part."""
    folder, sep, _ = posix_entry.rpartition("/")
    step = os.path.relpath(target.new, target.old_dir)
    literal = folder + sep + step
    return [literal] if "/" not in step else [os.path.normpath(literal), literal]


def _parse_line(text: str) -> tuple[str, str, bool] | None:
    """How a reader (and _rewrite) reads one decoded line, BOM already
    removed: None for a blank or "#" line, else ``(entry, posix_entry,
    backslashed)``."""
    entry = text.strip()
    if not entry or entry.startswith("#"):
        return None
    backslashed = "\\" in entry and "/" not in entry
    return entry, (entry.replace("\\", "/") if backslashed else entry), backslashed


def _split_bom(line: bytes, first: bool) -> tuple[bytes, bytes]:
    """A reader drops a UTF-8 BOM at the very start of the file."""
    if first and line.startswith(_BOM):
        return _BOM, line[len(_BOM):]
    return b"", line


def _read_line(line: bytes, first: bool) -> tuple[str, str, bool] | None:
    """How a reader reads one raw line (no line ending): the single parser
    used for planning, rewriting and every read-back check."""
    return _parse_line(_split_bom(line, first)[1].decode(*_ENC))


def entries(data: bytes) -> list[str]:
    """The entries a reader sees in playlist *data*, as posix paths, in order."""
    found = []
    for index, line in enumerate(data.splitlines()):
        parsed = _read_line(line, first=index == 0)
        if parsed is not None:
            found.append(parsed[1])
    return found


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
    candidates: list[str], backslashed: bool, playlist_dir: str, target: _Target,
    render: Callable[[str], bytes], first: bool,
) -> str | None:
    """The first way of writing a candidate that a reader reads back, from its
    rendered line, as an entry reaching *target*; None if there is none."""
    for candidate in candidates:
        for written in _variants(candidate, backslashed):
            # A control character (CR, LF, NUL, VT, ...) can't sit in one line.
            if any(unicodedata.category(ch) == "Cc" for ch in written):
                continue
            parsed = _read_line(render(written), first)
            if parsed is not None and _points_at(playlist_dir, parsed[1], target):
                return written
    return None


def entry_for(playlist_dir: str, path: str, *, first_line: bool) -> str | None:
    """A playlist entry, relative to *playlist_dir*, that a reader reads back
    to *path*, or None if it can't be written as one line. For code that
    writes new playlists, so they encode entries exactly as the Referrer does."""
    real_dir = os.path.realpath(playlist_dir)
    target = _Target(real_dir, os.path.realpath(path))
    step = os.path.relpath(target.new, real_dir)
    return _choose_entry(
        [step], False, real_dir, target, lambda e: e.encode(*_ENC), first_line,
    )


def _points_at(playlist_dir: str, entry: str, target: _Target) -> bool:
    """Whether *entry*, read from *playlist_dir*, reaches the output."""
    path = os.path.join(playlist_dir, entry)
    output_key = _file_key(target.new)
    if output_key is not None:
        return _file_key(path) == output_key
    # The delete confirmation plans before the output exists.
    return os.path.realpath(path) == target.new


def _decode_wide(data: bytes) -> str | None:
    """Decode a playlist with NUL bytes as UTF-16, or None if that isn't reliable.

    A BOM names the byte order. Without one, the NULs of ASCII text fall on odd
    offsets in little-endian and even offsets in big-endian.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        even, odd = data[0::2].count(0), data[1::2].count(0)
        if odd == even:
            return None
        encoding = "utf-16-le" if odd > even else "utf-16-be"
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        return None


def _wide_text_check(playlist: str, data: bytes, renames: _Renames) -> bool:
    """For a playlist that isn't 8-bit text (UTF-16, NUL bytes): raise if its
    text names a replaced file or can't be decoded, so the Source is kept, and
    return True (leave it alone) if it is unrelated. False for a normal playlist.
    """
    if not data.startswith((b"\xff\xfe", b"\xfe\xff")) and b"\x00" not in data:
        return False
    text = _decode_wide(data)
    if text is None:
        raise ReferrerError(
            playlist, "isn't plain text (NUL bytes) and can't be decoded to check it",
        )
    folded = _fold(text)
    mention = next((name for name in sorted(renames.names) if name in folded), None)
    if mention:
        raise ReferrerError(
            playlist, f"isn't plain text (UTF-16) and may name {mention}",
        )
    return True


def _legacy_text_check(playlist: str, data: bytes, renames: _Renames) -> None:
    """A playlist that isn't valid UTF-8 (Windows-1252, Latin-1) can't be
    matched reliably byte for byte. Read it as cp1252 only to look for a
    replaced file's name; if any entry could name one, keep the Source rather
    than rewrite it."""
    try:
        data.decode("utf-8")
        return
    except UnicodeDecodeError:
        pass
    text = data.removeprefix(_BOM).decode("cp1252", errors="replace")
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is not None and _named_file(parsed[0]) in renames.names:
            raise ReferrerError(
                playlist,
                f"playlist isn't UTF-8 and may name this disc ({_named_file(parsed[0])})",
            )


def _rewrite(
    playlist: str, data: bytes, playlist_dir: str, renames: _Renames,
) -> tuple[bytes, list[tuple[str, str]]]:
    if _wide_text_check(playlist, data, renames):
        return data, []
    _legacy_text_check(playlist, data, renames)
    out: list[bytes] = []
    edits: list[tuple[str, str]] = []
    rewritten: dict[int, _Target] = {}  # entry position -> where it must point
    position = -1
    for number, line in enumerate(data.splitlines(keepends=True), start=1):
        first = number == 1
        prefix, line = _split_bom(line, first)
        body = line.rstrip(b"\r\n")
        ending = line[len(body):]
        text = body.decode(*_ENC)
        parsed = _read_line(prefix + body, first)
        if parsed is None:
            out.append(prefix + line)
            continue
        entry, posix_entry, backslashed = parsed
        position += 1
        key = _file_key(os.path.join(playlist_dir, posix_entry))
        target = renames.by_key.get(key) if key else None
        if target is None:
            if key is None and _named_file(entry) in renames.names:
                raise ReferrerError(
                    playlist,
                    f"line {number} names {_named_file(entry)} but doesn't point at it",
                )
            out.append(prefix + line)
            continue
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        new_entry = _choose_entry(
            _new_entry(posix_entry, target), backslashed, playlist_dir, target,
            lambda e, p=prefix, a=lead, z=trail: p + (a + e + z).encode(*_ENC), first,
        )
        if new_entry is None:
            raise ReferrerError(
                playlist, f"line {number}: can't write a path that reaches the output",
            )
        out.append(prefix + (lead + new_entry + trail).encode(*_ENC) + ending)
        edits.append((entry, new_entry))
        rewritten[position] = target
    result = b"".join(out)
    _check_read_back(playlist, playlist_dir, data, result, rewritten)
    return result, edits


def _check_read_back(
    playlist: str, playlist_dir: str, before: bytes, after: bytes,
    rewritten: dict[int, _Target],
) -> None:
    """Parse the bytes about to be written the way _rewrite reads them: the
    same number of entries, every untouched one unchanged, and every rewritten
    one, at its position, reaching the output. Anything else keeps the Source."""
    old, new = entries(before), entries(after)
    ok = len(old) == len(new) and all(
        _points_at(playlist_dir, new[i], rewritten[i]) if i in rewritten
        else new[i] == old[i]
        for i in range(len(new))
    )
    if not ok:
        raise ReferrerError(playlist, "the rewritten playlist wouldn't read back to the output")


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ReferrerError(path, f"can't read: {exc.strerror}") from exc


class M3uReferrer(Referrer):
    id = "m3u"
    label = ".m3u playlist"

    def detect(self, image_path: str) -> bool:
        return any(_playlists(d) for d in _search_dirs(image_path))

    def plan(self, renames: Mapping[str, str]) -> list[ReferrerChange]:
        by_key: dict[tuple[int, int], _Target] = {}
        dirs: list[str] = []
        for old, new in renames.items():
            key = _file_key(old)
            if key:
                by_key[key] = _Target(
                    os.path.realpath(os.path.dirname(old)), os.path.realpath(new),
                )
            for d in _search_dirs(old):
                if d not in dirs:
                    dirs.append(d)
        targets = _Renames(by_key, frozenset(_fold(os.path.basename(o)) for o in renames))
        changes = []
        for directory in dirs:
            for playlist in _playlists(directory):
                with _playlist_lock(playlist):
                    _, edits = _rewrite(playlist, _read(playlist), directory, targets)
                if edits:
                    changes.append(ReferrerChange(
                        referrer=self.id, target=playlist, edits=tuple(edits),
                        payload=(directory, targets),
                    ))
        return changes

    def apply(self, change: ReferrerChange) -> None:
        path = change.target
        directory, targets = change.payload
        if os.path.islink(path):
            raise ReferrerError(path, "is a symlink")
        if not os.access(path, os.W_OK):
            raise ReferrerError(path, "is read-only")
        with _playlist_lock(path):
            original = _read(path)
            updated, edits = _rewrite(path, original, directory, targets)
            if not edits:
                return
            try:
                backup = path + BACKUP_SUFFIX
                if not os.path.lexists(backup):
                    atomic_write_bytes(backup, original)
                atomic_write_bytes(path, updated)
            except OSError as exc:
                raise ReferrerError(path, exc.strerror or str(exc)) from exc
