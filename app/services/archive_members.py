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
import errno
import os
import re
import stat
import threading
import zipfile
from collections import OrderedDict
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
# ``<stem>.rar`` then ``<stem>.r00`` … ``.r99``, ``.s00`` … ``.s99``: the older
# naming for the same thing, still what most usenet sets use (#476). Only
# ``r`` and ``s``: a wider letter range reads ROMs as volumes (``.t64``,
# ``.v64``, ``.z64``), and a name is never more than a candidate anyway: a
# file is a volume of a set only once that set's headers read it
# (:func:`rar_set_volumes`).
_RAR_OLD_VOLUME = re.compile(
    r"(?P<stem>.+)\.(?P<letter>[rs])(?P<number>\d\d)", re.IGNORECASE,
)
_RAR_OLD_LETTERS = "rs"
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


def is_rar_volume_name(name: str) -> bool:
    """Whether *name* is named like a RAR volume: ``.rar`` (``.partN.rar``
    included) or an old-style ``.r00`` … ``.s99``. Names only."""
    return name.lower().endswith(".rar") or _RAR_OLD_VOLUME.fullmatch(name) is not None


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
    try:
        is_later = _later_volume_cache.get_or_compute(
            archive_path,
            lambda: _bounded_header_read(_needs_first_volume, archive_path, progress),
        )
    except RarVolumesUnknown:
        # Couldn't tell in time: not kept, and not called a later volume. A
        # caller that converts or deletes reads the set's headers itself, and
        # refuses on the same bound.
        return None
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
    """The files *named* like later volumes of the RAR set *archive_path*
    leads, in order (#476): ``x.part2.rar`` … after ``x.part1.rar``, or
    ``x.r00`` … after ``x.rar``. Empty for a later volume, or with no such
    file beside it.

    Stops at the first name that is not a file, as a split Wii U set's parts
    do. Names only, so never what decides membership: a stray
    ``x.part4.rar`` or an unrelated ``x.r00`` is named like a volume too.
    :func:`rar_set_volumes` (the headers) decides; ``rar_extract`` refuses a
    set whose named and read volumes differ. Pure name math plus one stat per
    volume, never a read of a header. Blocking.
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


class RarVolumesUnknown(OSError):
    """Which volumes a RAR set has can't be told: its headers can't be read
    whole (a missing or broken volume, an encrypted header, no rarfile) or
    the read did not finish within :data:`RAR_HEADER_READ_SECONDS`."""


# How long one header read of a set may take: the bound a route's Volume work
# gets (``VOLUME_PROBE_TIMEOUT_SECONDS``). Past it the set's volumes are unknown.
RAR_HEADER_READ_SECONDS = 30.0
# How many sets' answers are kept.
_SET_VOLUMES_MAX = 8192
# first volume -> (signature of every file named like the set, answer): the
# headers' volume list, or why they can't be read (a string).
_set_volumes_cache: OrderedDict[str, tuple[tuple, list[str] | str]] = OrderedDict()
_set_volumes_lock = threading.Lock()


def _bounded_header_read(func: Callable, *args: object) -> object:
    """*func* (a rarfile open) through ``subprocess_runner.run_detached_blocking``:
    on a throwaway thread under the shared detached-probe ceiling, given up on
    after :data:`RAR_HEADER_READ_SECONDS`. The one way this module reads a RAR
    header (the listing, a later volume's flag, a set's members and volumes),
    so a mount that stops answering costs one written-off thread, never a
    shared pool worker or an unbounded wait. Raises :class:`RarVolumesUnknown`
    on the bound or with no probe slot free; *func*'s own errors propagate.
    Blocking."""
    from services.subprocess_runner import (  # local: import cycle
        DeadlineExpired,
        ProbeCapacityExceeded,
        run_detached_blocking,
    )

    try:
        return run_detached_blocking(func, *args, timeout=RAR_HEADER_READ_SECONDS)
    except (DeadlineExpired, ProbeCapacityExceeded) as exc:
        name = os.path.basename(str(args[0])) if args else "a RAR set"
        raise RarVolumesUnknown(
            errno.ETIMEDOUT,
            f"Can't read the headers of {name} in time (detached probe: {exc})",
            str(args[0]) if args else "",
        ) from exc


def _set_signature(first_volume: str) -> tuple:
    """The stat of every file named like the set (the first volume and each
    ``.partN.rar`` / ``.rNN`` after it): a volume arriving, leaving or
    changing gives another signature, so a set read mid-download is read
    again once it is whole."""
    signature = []
    for path in [first_volume, *rar_later_volumes(first_volume)]:
        try:
            st = os.stat(path)
        except OSError:
            signature.append((path, None))
            continue
        signature.append((path, st.st_mtime_ns, st.st_ctime_ns, st.st_size))
    return tuple(signature)


def incomplete_reason(found: RarSet) -> str | None:
    """Why a read of a set is not the whole set, or None when it is.

    rarfile stops without an error at headers it can't decrypt: it then
    reports a password is needed, lists no members and names only the volumes
    it got through. A missing next volume is ``IncompleteArchive`` already.
    Whatever is partial is unknown, never a complete list of the set."""
    if found.encrypted and not found.members:
        return "the set's headers are encrypted"
    if not found.members:
        return "its headers list no members"
    return None


def _read_volumes(first_volume: str) -> list[str] | str:
    """The headers' volume list, or the reason they can't be read whole.
    Blocking."""
    try:
        found = _read_rar_set_now(first_volume)
    except Exception as exc:  # unreadable: the reason is the answer
        logger.debug("No volume list for %s", first_volume, exc_info=True)
        return str(exc) or type(exc).__name__
    reason = incomplete_reason(found)
    if reason is not None:
        return reason
    volumes = found.volumes
    folder = os.path.dirname(first_volume)
    return [os.path.join(folder, os.path.basename(v)) for v in volumes]


def rar_set_volumes(first_volume: str) -> list[str]:
    """Every volume of the RAR set *first_volume* leads, as its own headers
    read them (rarfile follows each volume's "more volumes" flag), first
    volume first; ``[first_volume]`` for a single-volume archive (#476).

    The one answer to "is this file part of that set": a file named like a
    volume that the headers never read is not one. Raises
    :class:`RarVolumesUnknown` when the headers can't be read whole, or the
    read does not finish within :data:`RAR_HEADER_READ_SECONDS`, or no
    detached-probe slot is free: a listing then shows no companions, and
    anything that deletes refuses.

    Every blocking step, the stats of the set's files and the header read
    alike, runs through ``subprocess_runner.run_detached_blocking`` under the
    ceiling every detached probe shares and :data:`RAR_HEADER_READ_SECONDS`,
    so a mount that hangs can't hold the caller or pile up threads. Both a
    list and an unreadable answer are cached against the stat of every file
    named like the set (:func:`_set_signature`),
    so a broken set is not opened again on every call, and one read while a
    volume was still arriving is read again once it is there. A read that ran
    out of time is not kept. Blocking, and bounded.
    """
    # The stats are blocking work on the same mount: under the same bound.
    signature = _bounded_header_read(_set_signature, first_volume)
    with _set_volumes_lock:
        hit = _set_volumes_cache.get(first_volume)
        if hit is not None and hit[0] == signature:
            _set_volumes_cache.move_to_end(first_volume)
            found = hit[1]
        else:
            found = None
    if found is None:
        found = _bounded_header_read(_read_volumes, first_volume)
        with _set_volumes_lock:
            _set_volumes_cache[first_volume] = (signature, found)
            _set_volumes_cache.move_to_end(first_volume)
            while len(_set_volumes_cache) > _SET_VOLUMES_MAX:
                _set_volumes_cache.popitem(last=False)
    if isinstance(found, str):
        raise RarVolumesUnknown(
            errno.EIO, f"Can't tell the volumes of {os.path.basename(first_volume)}: {found}",
            first_volume,
        )
    return list(found)


def invalidate(archive_path: str) -> None:
    """Drop the cached member list for ``archive_path`` (used by tests)."""
    _cache.invalidate(archive_path)


def clear_cache() -> None:
    """Drop every cached member list and volume answer (used by tests)."""
    _cache.clear()
    _later_volume_cache.clear()
    with _set_volumes_lock:
        _set_volumes_cache.clear()


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
    """:func:`_read_rar_now`, bounded and detached (:func:`_bounded_header_read`)."""
    return _bounded_header_read(_read_rar_now, archive_path, progress)


def _read_rar_now(
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
        if rf.needs_password() and not rf.infolist():
            # Headers it can't decrypt: rarfile stops quietly with nothing
            # listed. That is a partial read, never "an empty archive" (#476).
            raise IncompleteArchive(f"{archive_path}: the archive's headers are encrypted")
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
    # Members stored as an older version of a file (``name;1``): rarfile
    # leaves them out of ``infolist``, but an extractor still writes them.
    versioned: tuple[str, ...] = ()


def _versioned_entry(header) -> bool:
    """Whether a raw RAR file header is an older version of a file: the RAR4
    ``LHD_VERSION`` flag, or a RAR5 file-version record. rarfile skips these
    in ``infolist`` (``process_entry``), so they are caught as each header is
    parsed (``info_callback``)."""
    import rarfile as rar  # optional backend, as in _needs_first_volume

    if getattr(header, "block_type", None) == rar.RAR5_BLOCK_FILE:
        return bool(getattr(header, "file_version", None))
    if getattr(header, "type", None) == rar.RAR_BLOCK_FILE:
        return bool(getattr(header, "flags", 0) & rar.RAR_FILE_VERSION)
    return False


def _rar_plain_file(info) -> bool:
    """Whether a rarfile entry is a plain file, judged fail-closed."""
    if not info.is_file() or getattr(info, "file_redir", None):
        return False
    import rarfile as rar  # optional backend: only reached with a rarfile entry

    if info.host_os == rar.RAR_OS_UNIX:
        # The Unix mode a Unix archiver stored: a device or FIFO is no file.
        return stat.S_IFMT(info.mode or 0) in (0, stat.S_IFREG)
    return True


def read_rar_set(first_volume: str) -> RarSet:
    """:func:`_read_rar_set_now`, bounded and detached
    (:func:`_bounded_header_read`): what every caller outside this module
    uses. Raises :class:`RarVolumesUnknown` when the read does not finish in
    time. Blocking."""
    return _bounded_header_read(_read_rar_set_now, first_volume)


def _read_rar_set_now(first_volume: str) -> RarSet:
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
    versioned: list[str] = []

    def on_header(header) -> None:
        # Every header, the versions rarfile drops from infolist included.
        if _versioned_entry(header):
            versioned.append(str(getattr(header, "filename", "") or "?"))

    import rarfile as rar  # optional backend: HAS_RAR was checked above

    with rar.RarFile(first_volume, "r", info_callback=on_header) as rf:
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
        return RarSet(
            members, bool(rf.needs_password()), list(rf.volumelist()),
            tuple(dict.fromkeys(versioned)),
        )
