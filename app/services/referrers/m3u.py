"""The ``.m3u`` Referrer: multi-disc playlists beside an Image (#295).

Always on. A playlist in the same directory as a replaced file has each entry
naming that file rewritten to the new file. Everything else is left byte for
byte: comment and directive lines, other entries, blank lines, line endings,
a UTF-8 BOM and any non-UTF-8 bytes (entries are decoded with
``surrogateescape`` so they re-encode to exactly what was read).

An entry keeps its style: a relative entry stays relative to the playlist, an
absolute one stays absolute, and a backslash-separated entry keeps backslashes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .base import Referrer, ReferrerChange, ReferrerError, atomic_write_bytes

PLAYLIST_EXT = ".m3u"
BACKUP_SUFFIX = ".bak"
_BOM = b"\xef\xbb\xbf"
_ENC = ("utf-8", "surrogateescape")


def _playlists(directory: str) -> list[str]:
    try:
        with os.scandir(directory) as it:
            return sorted(
                e.path for e in it
                if e.name.lower().endswith(PLAYLIST_EXT) and e.is_file()
            )
    except FileNotFoundError:
        return []


def _file_key(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _resolve(entry: str, playlist_dir: str) -> str:
    if os.sep == "/" and "\\" in entry and "/" not in entry:
        entry = entry.replace("\\", "/")
    return os.path.join(playlist_dir, entry)


def _format(new_path: str, entry: str, playlist_dir: str) -> str:
    if os.path.isabs(entry):
        new = new_path
    else:
        new = os.path.relpath(new_path, playlist_dir)
    if "\\" in entry and "/" not in entry:
        new = new.replace("/", "\\")
    return new


def rewrite(
    data: bytes, playlist_dir: str, renames: Mapping[tuple[int, int], str],
) -> tuple[bytes, list[tuple[str, str]]]:
    """*data* with every entry naming a renamed file rewritten.

    *renames* is keyed by the old file's ``(st_dev, st_ino)``, so an entry
    matches however it spells the path (relative, absolute, other case on a
    case-insensitive volume).
    """
    out: list[bytes] = []
    edits: list[tuple[str, str]] = []
    for i, line in enumerate(data.splitlines(keepends=True)):
        prefix = b""
        if i == 0 and line.startswith(_BOM):
            prefix, line = _BOM, line[len(_BOM):]
        body = line.rstrip(b"\r\n")
        ending = line[len(body):]
        text = body.decode(*_ENC)
        entry = text.strip()
        new_path = None
        if entry and not entry.startswith("#"):
            key = _file_key(_resolve(entry, playlist_dir))
            new_path = renames.get(key) if key else None
        if new_path is None:
            out.append(prefix + line)
            continue
        new_entry = _format(new_path, entry, playlist_dir)
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        out.append(prefix + (lead + new_entry + trail).encode(*_ENC) + ending)
        edits.append((entry, new_entry))
    return b"".join(out), edits


class M3uReferrer(Referrer):
    id = "m3u"
    label = ".m3u playlist"

    def detect(self, image_path: str) -> bool:
        return bool(_playlists(os.path.dirname(image_path)))

    def plan(self, renames: Mapping[str, str]) -> list[ReferrerChange]:
        by_key = {}
        for old, new in renames.items():
            key = _file_key(old)
            if key:
                by_key[key] = new
        directories = sorted({os.path.dirname(old) for old in renames})
        changes = []
        for directory in directories:
            for playlist in _playlists(directory):
                try:
                    with open(playlist, "rb") as f:
                        original = f.read()
                except OSError as exc:
                    raise ReferrerError(playlist, f"can't read: {exc.strerror}") from exc
                updated, edits = rewrite(original, directory, by_key)
                if edits:
                    changes.append(ReferrerChange(
                        referrer=self.id, target=playlist, edits=tuple(edits),
                        payload=(original, updated),
                    ))
        return changes

    def apply(self, change: ReferrerChange) -> None:
        path = change.target
        original, updated = change.payload
        if os.path.islink(path):
            raise ReferrerError(path, "is a symlink")
        if not os.access(path, os.W_OK):
            raise ReferrerError(path, "is read-only")
        try:
            with open(path, "rb") as f:
                if f.read() != original:
                    raise ReferrerError(path, "changed since it was planned")
            backup = path + BACKUP_SUFFIX
            if not os.path.lexists(backup):
                atomic_write_bytes(backup, original)
            atomic_write_bytes(path, updated)
        except OSError as exc:
            raise ReferrerError(path, exc.strerror or str(exc)) from exc
