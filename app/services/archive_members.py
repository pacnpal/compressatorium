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

import contextlib
import os
import re
import stat
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, NamedTuple

from logging_setup import get_logger
from utils.mtime_cache import MtimeCache
from utils.path_utils import open_reporting_reads, path_present

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
    # Its content needs a password (zip flag bit 0, an encrypted 7z, a RAR
    # member that asks for one). The listing itself still reads.
    is_encrypted: bool = False
    # Its compressed size, as the archive declares it; None when the format
    # doesn't say per member (a solid 7z block).
    compressed: int | None = None


# Module-global so the cache is shared across every consumer and threadpool
# worker (MtimeCache is thread-safe). This is the one place an archive is opened.
_cache: MtimeCache[list[ArchiveMember]] = MtimeCache()

# Don't retain the raw member list of an archive with more than this many members.
# The cache holds the *unfiltered* central directory, so without a cap an archive
# of hundreds of thousands of entries (or a crafted one) could pin a large list
# in memory for every one of the cache's slots. Oversized archives are still read
# correctly — just recomputed on each access rather than cached, trading a little
# CPU for a hard memory bound. Comfortably above any real game archive.
MAX_CACHED_MEMBERS = 10_000


def read_archive_members(
    archive_path: str, progress: Callable[..., None] | None = None,
) -> list[ArchiveMember]:
    """Return every member of ``archive_path``, cached per ``(path, mtime, size)``.

    ``progress``, when given, is called whenever bytes of the archive arrive
    (``utils.path_utils.open_reporting_reads``), for a stall-bounded listing
    (#361); a cached answer reads nothing.

    Raises ``ValueError`` for unsupported extensions and ``RuntimeError`` when the
    optional backend for a supported extension is missing, so callers can decide
    how to degrade. The result is shared and cached — treat it as read-only.
    Listings larger than :data:`MAX_CACHED_MEMBERS` are returned but not cached so
    a pathological archive can't pin a huge list in memory.
    """
    return _cache.get_or_compute(
        archive_path,
        # Without a callback, the read is exactly what it always was.
        lambda: (
            _read_uncached(archive_path) if progress is None
            else _read_uncached(archive_path, progress)
        ),
        should_cache=lambda members: len(members) <= MAX_CACHED_MEMBERS,
    )


# ``<stem>.partN.rar``: RAR's own naming for the volumes of a set (#369).
_RAR_PART = re.compile(
    r"(?P<stem>.+)(?P<part>\.part)(?P<number>\d+)(?P<ext>\.rar)", re.IGNORECASE,
)
# ``<stem>.rar`` then ``<stem>.r00`` … ``.r99``, ``.s00`` …: the older naming
# for the same thing, still what most usenet sets use (#476).
_RAR_OLD_VOLUME = re.compile(
    r"(?P<stem>.+)\.(?P<letter>[r-z])(?P<number>\d\d)", re.IGNORECASE,
)
_RAR_OLD_LETTERS = "rstuvwxyz"
# The most later volumes looked for: far past any real set, and a bound on
# the stats one lookup makes.
MAX_RAR_VOLUMES = 999
# Whether a later-named volume really is one, per (path, mtime, size).
_later_volume_cache: MtimeCache[bool] = MtimeCache()


def _first_volume_name(name: str) -> str | None:
    """The first volume's file name when *name* is named like a later volume."""
    match = _RAR_PART.fullmatch(name)
    if match is not None:
        if int(match["number"]) <= 1:
            return None
        return (
            f"{match['stem']}{match['part']}{'1'.zfill(len(match['number']))}"
            f"{match['ext']}"
        )
    match = _RAR_OLD_VOLUME.fullmatch(name)
    if match is not None:
        return f"{match['stem']}.{'RAR' if match['letter'].isupper() else 'rar'}"
    return None


def later_volume_of(
    archive_path: str, progress: Callable[..., None] | None = None,
) -> str | None:
    """The first volume's path when *archive_path* is a later volume of a
    multi-volume RAR set, else ``None`` (#369).

    The one place that decides it. By name first: ``<stem>.partN.rar`` with
    N > 1, or an old-style ``<stem>.r00`` … beside ``<stem>.rar`` (#476; the
    listing never asks about those, which are not archive extensions it
    lists, but the rar tool does). Then confirmed by rarfile itself, which
    refuses a volume whose flags say it is not the first
    (``NeedFirstVolume``): a single-volume RAR that is merely named
    ``x.part2.rar`` is not hidden, and a stray ``x.r00`` that is no RAR at
    all is no later volume. A later volume is listed as a plain file, never
    summarized or searched as an archive, and never logs an error.
    ``progress`` is reported per header rarfile parses. Cached per
    ``(path, mtime, size)``. Blocking.
    """
    first = _first_volume_name(os.path.basename(archive_path))
    if first is None:
        return None
    is_later = _later_volume_cache.get_or_compute(
        archive_path, lambda: _needs_first_volume(archive_path, progress),
    )
    if not is_later:
        return None
    return os.path.join(os.path.dirname(archive_path), first)


def _later_volume_names(name: str) -> Iterator[str] | None:
    """Every name a later volume of the set *name* leads could have, in
    order, or None when *name* does not lead a set by its name."""
    match = _RAR_PART.fullmatch(name)
    if match is not None:
        if int(match["number"]) != 1:
            return None
        width = len(match["number"])
        return (
            f"{match['stem']}{match['part']}{str(n).zfill(width)}{match['ext']}"
            for n in range(2, MAX_RAR_VOLUMES + 2)
        )
    stem, ext = os.path.splitext(name)
    if ext.lower() != ".rar":
        return None
    letters = _RAR_OLD_LETTERS.upper() if ext.isupper() else _RAR_OLD_LETTERS
    return (
        f"{stem}.{letter}{n:02d}"
        for letter in letters for n in range(100)
    )


def rar_later_volumes(archive_path: str) -> list[str]:
    """The later volumes of the multi-volume RAR set *archive_path* leads,
    in order, by name (#476): ``x.part2.rar`` … after ``x.part1.rar``, or
    ``x.r00`` … after ``x.rar``. Empty for a later volume, a single-volume
    archive, or anything else.

    Stops at the first name that is not a file, as a split Wii U set's parts
    do: the set is unusable past a gap, so nothing after it is part of a
    usable Source. This is what makes a set one Source (the rar tool's
    ``source_companions``: locks, the delete plan, deleting the Source). Pure
    name math plus one stat per volume, never a read of a header. Blocking.
    """
    names = _later_volume_names(os.path.basename(archive_path))
    if names is None:
        return []
    folder = os.path.dirname(archive_path)
    later: list[str] = []
    for name in names:
        candidate = os.path.join(folder, name)
        if not path_present(candidate, regular=True):
            break
        later.append(candidate)
    return later


def _needs_first_volume(
    archive_path: str, progress: Callable[..., None] | None,
) -> bool:
    """Whether rarfile refuses *archive_path* as not the first volume of its set.

    Anything else (it opens, it is broken, rarfile is not installed) is
    False: the file is then listed as an archive and the listing reports
    whatever is wrong with it.
    """
    try:
        import rarfile as rar  # optional backend, as at the top of this module
    except ImportError:
        return False
    on_header = (lambda _header: progress()) if progress is not None else None
    try:
        with rar.RarFile(archive_path, "r", info_callback=on_header):
            return False
    except rar.NeedFirstVolume:
        return True
    except (rar.Error, OSError):
        return False


def invalidate(archive_path: str) -> None:
    """Drop the cached member list for ``archive_path`` (used by tests)."""
    _cache.invalidate(archive_path)


def clear_cache() -> None:
    """Drop every cached member list and volume answer (used by tests)."""
    _cache.clear()
    _later_volume_cache.clear()


def read_member_crcs(archive_path: str) -> list[tuple[str, int | None, int | None, bool]]:
    """``(name, size, crc32, is_dir)`` for every member of a ``.zip``/``.7z``.

    The CRC32 each format stores per member, read from the listing without
    decompressing anything; the Round-trip compares them with the Source (#301).
    Not cached: it runs once per check, and the listing cache holds no CRCs.
    ``crc32`` is ``None`` when the archive stores none for a member.
    """
    ext = Path(archive_path).suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            return [
                (_zip_name(i), int(i.file_size), int(i.CRC), i.is_dir())
                for i in zf.infolist()
            ]
    if ext == ".7z":
        if not HAS_7Z:
            raise RuntimeError("py7zr is required to read .7z archives")
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            return [
                (
                    e.filename,
                    int(e.uncompressed) if e.uncompressed is not None else None,
                    int(e.crc32) if e.crc32 is not None else None,
                    bool(e.is_directory),
                )
                for e in zf.list()
            ]
    raise ValueError(f"Unsupported archive extension: {ext}")


def _zip_name(info: zipfile.ZipInfo) -> str:
    """A zip member's name as its writer meant it.

    Without the UTF-8 flag (bit 11) ``zipfile`` reads the name as cp437. p7zip
    writes UTF-8 names without that flag, so a cp437 name that is valid UTF-8
    once re-encoded is read as UTF-8; anything else stays as cp437 read it.
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


@contextlib.contextmanager
def _source(
    archive_path: str, progress: Callable[..., None] | None,
) -> Iterator[str | IO[bytes]]:
    """What a backend opens: the path itself, or with ``progress`` a reader
    that reports every chunk of the archive as it arrives (#361)."""
    if progress is None:
        yield archive_path
        return
    with open_reporting_reads(archive_path, progress) as handle:
        yield handle


def _read_uncached(
    archive_path: str, progress: Callable[..., None] | None = None,
) -> list[ArchiveMember]:
    ext = Path(archive_path).suffix.lower()
    if ext == ".zip":
        return _read_zip(archive_path, progress)
    if ext == ".7z":
        if not HAS_7Z:
            from services.subprocess_runner import ToolUnavailable

            raise ToolUnavailable("py7zr is required to read .7z archives")
        return _read_7z(archive_path, progress)
    if ext == ".rar":
        if not HAS_RAR:
            from services.subprocess_runner import ToolUnavailable

            raise ToolUnavailable("rarfile is required to read .rar archives")
        return _read_rar(archive_path, progress)
    raise ValueError(f"Unsupported archive extension: {ext}")


def _read_zip(
    archive_path: str, progress: Callable[..., None] | None = None,
) -> list[ArchiveMember]:
    """Read every ZIP member, flagging symlinks via the Unix `external_attr`."""
    with _source(archive_path, progress) as source:
        return _zip_members(source)


def read_members_of(source: IO[bytes], ext: str) -> list[ArchiveMember]:
    """Every member of the zip or 7z already open as *source*, uncached: for
    a caller that must read the listing and the contents from one open file
    (the Inbox, #432, gating the very archive it unpacks). A rar is refused
    (``ValueError``): rarfile finds a later volume by the file's name and
    gives up silently on a file object (Codex 4584380491)."""
    if ext == ".zip":
        return _zip_members(source)
    if ext == ".7z" and HAS_7Z:
        return _7z_members(source)
    raise ValueError(f"A {ext} archive can't be read from an open file")


def _zip_members(source: str | IO[bytes]) -> list[ArchiveMember]:
    members: list[ArchiveMember] = []
    with zipfile.ZipFile(source, "r") as zf:
        for info in zf.infolist():
            is_symlink = stat.S_ISLNK(info.external_attr >> 16)
            members.append(
                ArchiveMember(
                    info.filename, int(info.file_size), info.is_dir(), is_symlink,
                    bool(info.flag_bits & 0x1), int(info.compress_size),
                ),
            )
    return members


def _read_7z(
    archive_path: str, progress: Callable[..., None] | None = None,
) -> list[ArchiveMember]:
    """Read every 7z member; ``uncompressed`` is ``None`` when the archive omits it."""
    with _source(archive_path, progress) as source:
        return _7z_members(source)


def _7z_members(source: str | IO[bytes]) -> list[ArchiveMember]:
    members: list[ArchiveMember] = []
    with py7zr.SevenZipFile(source, "r") as zf:  # type: ignore[name-defined]
        # py7zr knows encryption per archive, not per entry.
        encrypted = bool(zf.needs_password())
        for entry in zf.list():
            size = entry.uncompressed
            packed = getattr(entry, "compressed", None)
            members.append(
                ArchiveMember(
                    entry.filename,
                    int(size) if size is not None else None,
                    entry.is_directory,
                    bool(entry.is_symlink),
                    encrypted and not entry.is_directory,
                    int(packed) if packed else None,
                ),
            )
    return members


class IncompleteArchive(RuntimeError):
    """The backend stopped part way through the archive (a later volume
    missing or unreadable, a broken header): its member list is not the
    whole archive, so it is never returned or cached."""


def _read_rar(
    archive_path: str, progress: Callable[..., None] | None = None,
) -> list[ArchiveMember]:
    """Read every RAR member, flagging symlinks when the rarfile backend reports them.

    Always opened by path: rarfile finds a multi-volume archive's next volume
    from the file name and gives up silently on a file object, which would
    list only the first volume (Codex 4584380491). Progress comes from its
    supported ``info_callback``, one report per header parsed, across every
    volume. rarfile's default ``errors="stop"`` ends a parse quietly at a
    missing or broken volume; that is :class:`IncompleteArchive` here.
    """
    members: list[ArchiveMember] = []
    on_header = (lambda _header: progress()) if progress is not None else None
    with rarfile.RarFile(  # type: ignore[name-defined]
        archive_path, "r", info_callback=on_header,
    ) as rf:
        if rf.strerror():
            raise IncompleteArchive(f"{archive_path}: {rf.strerror()}")
        for info in rf.infolist():
            # rarfile exposes is_symlink() on recent versions; default to False
            # so an older backend simply doesn't flag symlinks (ArchiveService
            # ignores the flag anyway; romz only reads .zip/.7z).
            is_symlink_attr = getattr(info, "is_symlink", None)
            is_symlink = bool(is_symlink_attr()) if callable(is_symlink_attr) else False
            needs_password = getattr(info, "needs_password", None)
            members.append(
                ArchiveMember(
                    info.filename, int(info.file_size), info.is_dir(), is_symlink,
                    bool(needs_password()) if callable(needs_password) else False,
                    int(getattr(info, "compress_size", 0) or 0) or None,
                ),
            )
    return members


class RarSetMember(NamedTuple):
    """One member of a RAR set, with what an extractor has to know."""

    name: str
    size: int
    # CRC32 of the unpacked file, None when the set stores another hash
    # (RAR5 can store BLAKE2sp instead).
    crc: int | None
    is_dir: bool
    # A plain file: no link of any kind (a symlink, a hard link, a RAR5 file
    # copy), no device, FIFO or socket.
    is_plain_file: bool
    is_encrypted: bool


class RarSet(NamedTuple):
    """A RAR set read through its first volume (:func:`read_rar_set`)."""

    members: list[RarSetMember]
    # A password is needed: for the headers (nothing lists) or for any
    # member's data.
    encrypted: bool
    # Every volume rarfile read, first volume first.
    volumes: list[str]


def _rar_plain_file(info) -> bool:
    """Whether a rarfile entry is a plain file, judged fail-closed."""
    if not info.is_file() or getattr(info, "file_redir", None):
        return False
    if info.host_os == rarfile.RAR_OS_UNIX:  # type: ignore[name-defined]
        # The Unix mode a Unix archiver stored: a device or FIFO is no file.
        return stat.S_IFMT(info.mode or 0) in (0, stat.S_IFREG)
    return True


def read_rar_set(first_volume: str) -> RarSet:
    """Every member of the RAR set *first_volume* leads, uncached (#476).

    For a caller that extracts the set, so it needs more than the listing
    cache keeps: each member's CRC, whether it is a plain file (a RAR5 hard
    link or file copy reads as a file to rarfile), whether a password is
    needed anywhere, and which volumes were read. Opened by path, so rarfile
    finds the later volumes itself. A missing, truncated or unreadable volume
    raises :class:`IncompleteArchive`, a later volume ``NeedFirstVolume``.
    Blocking.
    """
    if not HAS_RAR:
        from services.subprocess_runner import ToolUnavailable

        raise ToolUnavailable("rarfile is required to read .rar archives")
    with rarfile.RarFile(first_volume, "r") as rf:  # type: ignore[name-defined]
        if rf.strerror():
            raise IncompleteArchive(f"{first_volume}: {rf.strerror()}")
        members = [
            RarSetMember(
                info.filename, int(info.file_size or 0),
                None if info.CRC is None else int(info.CRC),
                info.is_dir(), _rar_plain_file(info), bool(info.needs_password()),
            )
            for info in rf.infolist()
        ]
        return RarSet(members, bool(rf.needs_password()), list(rf.volumelist()))
