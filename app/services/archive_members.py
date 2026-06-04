"""Single source of truth for reading an archive's raw member list.

Both :class:`~services.archive.ArchiveService` (member summaries for the file
browser) and :class:`~services.romz.RomzService` (its single-ROM verify/extract
gate) need to know what's inside a ``.zip`` / ``.7z`` / ``.rar``. Historically
each opened and parsed the archive itself, so one archive on screen was opened
twice — and a directory of thousands of single-ROM archives turned a listing into
thousands of redundant opens.

This module centralises the read behind one mtime-keyed cache
(:class:`utils.mtime_cache.MtimeCache`) so each archive is opened at most once per
``(path, mtime, size)`` — cold or warm — and every consumer derives its
format-specific view from the same cached raw listing. Any write that replaces
the archive bumps mtime/size and invalidates the entry, so a conversion can't be
served a stale member list.

"Raw" means unfiltered: every member is returned (directories, junk sidecars,
symlinks) with enough metadata for each consumer to apply its own policy —
ArchiveService filters to convertible members and enforces size limits; romz
rejects symlink members as a security gate. ``size`` is ``None`` when the
container doesn't record an uncompressed size (some 7z entries) so callers can
decide whether that's a skip-under-limits or a treat-as-zero.

The returned list is shared across callers and cached; treat it as read-only.
"""
from __future__ import annotations

import stat
import zipfile
from pathlib import Path
from typing import NamedTuple

from logging_setup import get_logger
from utils.mtime_cache import MtimeCache

try:
    import py7zr

    HAS_7Z = True
except ImportError:  # pragma: no cover - py7zr ships in requirements
    HAS_7Z = False

try:
    import rarfile

    HAS_RAR = True
except ImportError:  # pragma: no cover - rarfile is optional
    HAS_RAR = False

logger = get_logger("archive_members")


class ArchiveMember(NamedTuple):
    """One entry in an archive, with the metadata every consumer needs."""

    name: str  # internal path within the archive
    size: int | None  # uncompressed size; None when the format omits it
    is_dir: bool
    is_symlink: bool


# Module-global so the cache is shared across every consumer and threadpool
# worker (MtimeCache is thread-safe). This is the one place an archive is opened.
_cache: MtimeCache[list[ArchiveMember]] = MtimeCache()


def read_archive_members(archive_path: str) -> list[ArchiveMember]:
    """Return every member of ``archive_path``, cached per ``(path, mtime, size)``.

    Raises ``ValueError`` for unsupported extensions and ``RuntimeError`` when the
    optional backend for a supported extension is missing, so callers can decide
    how to degrade. The result is shared and cached — treat it as read-only.
    """
    return _cache.get_or_compute(
        archive_path, lambda: _read_uncached(archive_path),
    )


def invalidate(archive_path: str) -> None:
    """Drop the cached member list for ``archive_path`` (used by tests)."""
    _cache.invalidate(archive_path)


def clear_cache() -> None:
    """Drop every cached member list (used by tests)."""
    _cache.clear()


def _read_uncached(archive_path: str) -> list[ArchiveMember]:
    ext = Path(archive_path).suffix.lower()
    if ext == ".zip":
        return _read_zip(archive_path)
    if ext == ".7z":
        if not HAS_7Z:
            raise RuntimeError("py7zr is required to read .7z archives")
        return _read_7z(archive_path)
    if ext == ".rar":
        if not HAS_RAR:
            raise RuntimeError("rarfile is required to read .rar archives")
        return _read_rar(archive_path)
    raise ValueError(f"Unsupported archive extension: {ext}")


def _read_zip(archive_path: str) -> list[ArchiveMember]:
    members: list[ArchiveMember] = []
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            is_symlink = stat.S_ISLNK(info.external_attr >> 16)
            members.append(
                ArchiveMember(
                    info.filename, int(info.file_size), info.is_dir(), is_symlink,
                ),
            )
    return members


def _read_7z(archive_path: str) -> list[ArchiveMember]:
    members: list[ArchiveMember] = []
    with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
        for entry in zf.list():
            size = entry.uncompressed
            members.append(
                ArchiveMember(
                    entry.filename,
                    int(size) if size is not None else None,
                    entry.is_directory,
                    bool(entry.is_symlink),
                ),
            )
    return members


def _read_rar(archive_path: str) -> list[ArchiveMember]:
    members: list[ArchiveMember] = []
    with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
        for info in rf.infolist():
            # rarfile exposes is_symlink() on recent versions; default to False
            # so an older backend simply doesn't flag symlinks (ArchiveService
            # ignores the flag anyway; romz only reads .zip/.7z).
            is_symlink_attr = getattr(info, "is_symlink", None)
            is_symlink = bool(is_symlink_attr()) if callable(is_symlink_attr) else False
            members.append(
                ArchiveMember(
                    info.filename, int(info.file_size), info.is_dir(), is_symlink,
                ),
            )
    return members
