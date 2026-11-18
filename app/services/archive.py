import os
import shutil
import stat
import tempfile
import zipfile
import time
import logging
from logging_setup import get_logger
from pathlib import Path, PurePosixPath
from collections.abc import Callable
from typing import List, Tuple, Optional, Union, Dict

from config import settings
from services.archive_members import (
    later_volume_of,
    read_archive_members,
    read_members_of,
)
from utils.junk import is_junk_path
from utils.path_utils import match_extension, open_dir_beneath

try:
    import py7zr
    from py7zr.io import Py7zIO, WriterFactory

    HAS_7Z = True
except ImportError:
    HAS_7Z = False
    Py7zIO = WriterFactory = object

try:
    import rarfile

    HAS_RAR = True
except ImportError:
    HAS_RAR = False


ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
# The listable / convert-gate extension sets come *only* from the tool registry
# (``_listable_extensions`` browse, ``_convert_gate_extensions`` search). There is
# deliberately no hand-maintained fallback copy: a stale literal silently hid new
# tools' members (3DS issue #113, romz's .zip "Empty folder" bug), so the registry
# is the single source of truth and an empty union surfaces a real bug loudly
# instead of being masked.

logger = get_logger("archive")

# How much of a member :meth:`ArchiveService.extract_members` copies between
# two progress reports.
_UNPACK_CHUNK = 4 * 1024 * 1024


def _rar_is_link(info) -> bool:
    """Whether a rar member is a link (``is_symlink`` on recent rarfile)."""
    is_link = getattr(info, "is_symlink", None)
    return bool(is_link()) if callable(is_link) else False


class _UnpackBudget:
    """What :meth:`ArchiveService.extract_members` may write: each member at
    most what it declares (and *max_member*), all of them at most
    *max_total*; and with *max_ratio*, each member at most that many times
    its compressed size, all of them at most that many times the archive's
    own size *packed* (Codex 4608936764). 0 is no limit. Counts the bytes
    actually written, so a header that lies is caught at the first chunk
    past it (#432)."""

    def __init__(
        self, max_member: int = 0, max_total: int = 0, max_ratio: int = 0,
        packed: int = 0, reserved: int | None = None,
    ) -> None:
        # What the caller reserved room for, an exact cap: 0 allows nothing.
        self._reserved = None if reserved is None else max(0, int(reserved))
        self._max_member = max(0, int(max_member or 0))
        self._max_total = max(0, int(max_total or 0))
        self._max_ratio = max(0, int(max_ratio or 0))
        self._packed = max(1, int(packed or 0))
        self._declared: dict[str, int | None] = {}
        self._compressed: dict[str, int] = {}
        self._written: dict[str, int] = {}
        self._total = 0

    def declare(self, member: str, size, compressed=None) -> None:
        self._declared[member] = None if size is None else int(size)
        if compressed:
            self._compressed[member] = int(compressed)

    def declared(self, member: str) -> int | None:
        return self._declared.get(member)

    def take(self, member: str, count: int) -> None:
        """Count *count* more bytes of *member*, or raise
        :class:`ArchiveSafetyRefused` before they are written."""
        written = self._written.get(member, 0) + count
        declared = self._declared.get(member)
        if declared is None or written > declared:
            raise ArchiveSafetyRefused(
                f"Archive member holds more than it declares: {member}",
                rule=f"{member} holds more than it declares ({declared} bytes)",
            )
        if self._max_member and written > self._max_member:
            raise ArchiveSafetyRefused(
                f"Archive member exceeds max size: {member}",
                rule=f"{member} is over the limit ({self._max_member} bytes)",
            )
        if self._max_total and self._total + count > self._max_total:
            raise ArchiveSafetyRefused(
                "Archive extraction exceeds max total size",
                rule=f"its contents are over the limit ({self._max_total} bytes)",
            )
        if self._reserved is not None and self._total + count > self._reserved:
            raise ArchiveSafetyRefused(
                "Archive extraction exceeds the room reserved for it",
                rule=f"its contents are over the {self._reserved} bytes it declared",
            )
        if self._max_ratio:
            compressed = self._compressed.get(member)
            if compressed and written > compressed * self._max_ratio:
                raise ArchiveSafetyRefused(
                    f"Archive member expands too far: {member}",
                    rule=f"{member} expands more than CHD_ARCHIVE_INBOX_MAX_RATIO "
                    f"({self._max_ratio}:1)",
                )
            if self._total + count > self._packed * self._max_ratio:
                raise ArchiveSafetyRefused(
                    "Archive expands too far",
                    rule=f"it expands more than CHD_ARCHIVE_INBOX_MAX_RATIO "
                    f"({self._max_ratio}:1)",
                )
        self._written[member] = written
        self._total += count


_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# A folder on the way to a member: opened, never followed through a link.
_MEMBER_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | _CLOEXEC
# A member's file: created new, never over anything, never through a link.
_MEMBER_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC


def _create_member(root_fd: int, segments: list[str]):
    """Create the member at *segments* beneath the open folder *root_fd*:
    each folder on the way made (``mkdir(dir_fd=...)``) and opened with
    ``O_NOFOLLOW``, the file created with ``O_EXCL | O_NOFOLLOW``. Nothing is
    ever reached by a path name, so a folder swapped for a link can't send a
    byte anywhere else. Returns the file open for writing."""
    fd = root_fd
    try:
        for part in segments[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, _MEMBER_DIR_FLAGS, dir_fd=fd)
            if fd != root_fd:
                os.close(fd)
            fd = child
        created = os.open(segments[-1], _MEMBER_FILE_FLAGS, 0o644, dir_fd=fd)
    finally:
        if fd != root_fd:
            os.close(fd)
    return os.fdopen(created, "wb")


class _BudgetedWriter(Py7zIO):
    """One 7z member's file, created beneath the held folder, every write
    counted first."""

    def __init__(
        self, member: str, root_fd: int, segments: list[str], budget: _UnpackBudget,
        progress,
    ) -> None:
        self._member, self._budget, self._progress = member, budget, progress
        self._file = _create_member(root_fd, segments)
        self._size = 0

    def write(self, s) -> int:
        self._budget.take(self._member, len(s))
        written = self._file.write(s)
        self._size += len(s)
        if self._progress is not None:
            self._progress(self._member)
        return written

    def read(self, size=None) -> bytes:
        return b""

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._size

    def flush(self) -> None:
        self._file.flush()

    def size(self) -> int:
        return self._size

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


class _BudgetedWriters(WriterFactory):
    """py7zr's writers for :meth:`ArchiveService.extract_members`: py7zr
    hands each member's bytes to a writer, never a path; each is written to
    its checked place beneath the held folder."""

    def __init__(
        self, root_fd: int, targets: dict[str, list[str]], budget: _UnpackBudget, progress,
    ) -> None:
        self._root_fd, self._targets = root_fd, targets
        self._budget, self._progress = budget, progress
        self._writers: list[_BudgetedWriter] = []
        self.created: set[str] = set()

    def create(self, filename: str) -> _BudgetedWriter:
        segments = self._targets.get(filename)
        if segments is None:
            raise ArchiveSafetyRefused(
                f"Archive member wasn't asked for: {filename}",
                rule=f"an unexpected member ({filename!r})",
            )
        writer = _BudgetedWriter(filename, self._root_fd, segments, self._budget, self._progress)
        self._writers.append(writer)
        self.created.add(filename)
        return writer

    def close_all(self) -> None:
        for writer in self._writers:
            writer.close()


class ArchiveSafetyRefused(ValueError):
    """An archive a safety rule refuses: a limit it exceeds, or a member that
    is a symlink, has an absolute or ``..`` path, or is encrypted (#306).

    Says nothing about whether the archive is damaged. ``rule`` names the rule
    in words, for a caller that reports the file as not checkable here rather
    than as failed. A ``ValueError``, so existing callers are unchanged.
    """

    def __init__(self, message: str, *, rule: str):
        super().__init__(message)
        self.rule = rule


class ArchiveService:
    """Service for handling compressed archives."""

    def __init__(self):
        self._temp_dirs: dict = {}

    @staticmethod
    def _archive_limits() -> Tuple[int, int, int]:
        max_entries = max(0, int(getattr(settings, "archive_max_entries", 0) or 0))
        max_member_size = max(
            0, int(getattr(settings, "archive_max_member_size", 0) or 0)
        )
        max_total_size = max(
            0, int(getattr(settings, "archive_max_total_size", 0) or 0)
        )
        return max_entries, max_member_size, max_total_size

    @staticmethod
    def _format_size(size: int) -> str:
        return f"{size} bytes"

    @staticmethod
    def _coerce_size(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        try:
            size = int(value)
        except (TypeError, ValueError):
            return None
        if size < 0:
            return None
        return size

    def _check_member_size(self, size: int, *, member: str) -> None:
        _, max_member_size, _ = self._archive_limits()
        if max_member_size > 0 and size > max_member_size:
            raise ArchiveSafetyRefused(
                f"Archive member exceeds max size: {member} ({self._format_size(size)})",
                rule=f"{member} is over CHD_ARCHIVE_MAX_MEMBER_SIZE",
            )

    def _check_total_size(self, total_size: int) -> None:
        _, _, max_total_size = self._archive_limits()
        if max_total_size > 0 and total_size > max_total_size:
            raise ArchiveSafetyRefused(
                f"Archive extraction exceeds max total size ({self._format_size(total_size)})",
                rule="its contents are over CHD_ARCHIVE_MAX_TOTAL_SIZE",
            )

    def _size_limits_enabled(self) -> bool:
        _, max_member_size, max_total_size = self._archive_limits()
        return max_member_size > 0 or max_total_size > 0

    def enforce_archive_limits(self, members: list[Tuple[str, int]]) -> None:
        """Apply the configured archive entry/size limits to a member listing.

        Shared with romz, which reads archives via its own member listing and
        runs ``7z`` directly rather than going through this service's extract
        path. Without this, deployments that set ``CHD_ARCHIVE_MAX_ENTRIES`` /
        ``CHD_ARCHIVE_MAX_MEMBER_SIZE`` / ``CHD_ARCHIVE_MAX_TOTAL_SIZE`` to guard
        against oversized archives / zip bombs would not have those limits
        applied to romz extract or verify. ``members`` is ``(name, size)`` with
        uncompressed sizes.
        """
        max_entries, _, _ = self._archive_limits()
        if max_entries > 0 and len(members) > max_entries:
            raise ArchiveSafetyRefused(
                f"Archive exceeds max entries ({max_entries})",
                rule="it has more entries than CHD_ARCHIVE_MAX_ENTRIES",
            )
        total = 0
        for name, size in members:
            self._check_member_size(size, member=name)
            total += size
        self._check_total_size(total)

    @staticmethod
    def inbox_limits() -> Tuple[int, int, int]:
        """``(max member, max total, max ratio)`` for an archive an Inbox
        takes in (#431, Codex 4607318277): the declared uncompressed size per
        member and in all (``CHD_ARCHIVE_MAX_MEMBER_SIZE`` /
        ``CHD_ARCHIVE_MAX_TOTAL_SIZE`` where set, else
        ``CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE``), and the uncompressed to
        compressed ratio (``CHD_ARCHIVE_INBOX_MAX_RATIO``). 0 is no limit."""
        _, member, total = ArchiveService._archive_limits()
        fallback = max(0, int(getattr(settings, "archive_inbox_max_total_size", 0) or 0))
        ratio = max(0, int(getattr(settings, "archive_inbox_max_ratio", 0) or 0))
        return member or fallback, total or fallback, ratio

    @staticmethod
    def inbox_unpacked_limit() -> int:
        """What every archive an Inbox has unpacked at once may hold in all
        (``CHD_ARCHIVE_INBOX_MAX_UNPACKED_SIZE``, #432): the Inbox's total cap
        (:meth:`inbox_limits`) when unset. 0 is no limit."""
        value = getattr(settings, "archive_inbox_max_unpacked_size", None)
        if value is None:
            return ArchiveService.inbox_limits()[1]
        return max(0, int(value))

    def check_inbox_archive(self, archive_path: str, progress=None) -> int:
        """Refuse an archive an Inbox must not check or copy: the configured
        entry limit, the Inbox's declared-size and expansion caps
        (:meth:`inbox_limits`), per member and in all, a member whose size
        isn't declared (it can't be bounded), a link, an absolute or ``..``
        path. Raises :class:`ArchiveSafetyRefused` naming the limit, or what
        reading the listing raises. Returns the declared uncompressed total.
        Blocking; reads the listing only."""
        members = read_archive_members(archive_path, progress)
        return self._check_inbox_members(members, os.path.getsize(archive_path))

    def check_inbox_source(self, source, archive_path: str) -> int:
        """:meth:`check_inbox_archive` on the archive already open as
        *source* (a binary file object), reading its listing and size from
        that open file, never from *archive_path* (#432, Codex 4608007766):
        so the gate judges the very file :meth:`extract_members` then reads
        from the same *source*. A zip or 7z; anything else raises
        ``ValueError``. Blocking."""
        source.seek(0)
        members = read_members_of(source, Path(archive_path).suffix.lower())
        return self._check_inbox_members(members, os.fstat(source.fileno()).st_size)

    def _check_inbox_members(self, members: list, packed: int) -> int:
        """The Inbox gate over a member listing and the archive's size."""
        max_member, max_total, max_ratio = self.inbox_limits()
        max_entries, _, _ = self._archive_limits()
        if max_entries > 0 and len(members) > max_entries:
            raise ArchiveSafetyRefused(
                f"Archive exceeds max entries ({max_entries})",
                rule="it has more entries than CHD_ARCHIVE_MAX_ENTRIES",
            )
        member_rule = (
            "CHD_ARCHIVE_MAX_MEMBER_SIZE" if self._archive_limits()[1]
            else "CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE"
        )
        total_rule = (
            "CHD_ARCHIVE_MAX_TOTAL_SIZE" if self._archive_limits()[2]
            else "CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE"
        )
        total = 0
        for member in members:
            if member.is_symlink:
                raise ArchiveSafetyRefused(
                    f"Archive member is a link: {member.name}", rule=f"a link ({member.name})",
                )
            stripped = member.name.rstrip("/")
            if stripped.startswith("/") or ".." in stripped.split("/"):
                raise ArchiveSafetyRefused(
                    f"Unsafe archive member path: {member.name}",
                    rule=f"an absolute or .. member path ({member.name})",
                )
            if member.is_dir:
                continue
            if member.size is None:
                raise ArchiveSafetyRefused(
                    f"Archive member declares no size: {member.name}",
                    rule=f"{member.name} declares no uncompressed size",
                )
            if max_member and member.size > max_member:
                raise ArchiveSafetyRefused(
                    f"Archive member exceeds max size: {member.name}",
                    rule=f"{member.name} is over {member_rule} ({max_member} bytes)",
                )
            if max_ratio and member.compressed and member.size > member.compressed * max_ratio:
                raise ArchiveSafetyRefused(
                    f"Archive member expands too far: {member.name}",
                    rule=f"{member.name} expands more than CHD_ARCHIVE_INBOX_MAX_RATIO "
                    f"({max_ratio}:1)",
                )
            total += member.size
        if max_total and total > max_total:
            raise ArchiveSafetyRefused(
                f"Archive extraction exceeds max total size ({total} bytes)",
                rule=f"its contents are over {total_rule} ({max_total} bytes)",
            )
        if max_ratio and total > max(packed, 1) * max_ratio:
            raise ArchiveSafetyRefused(
                f"Archive expands too far ({total} bytes from {packed})",
                rule=f"it expands more than CHD_ARCHIVE_INBOX_MAX_RATIO ({max_ratio}:1)",
            )
        return total

    def extract_members(
        self, archive_path: str, destination: str, *, destination_fd: int | None = None,
        source=None, progress=None, max_member: int = 0, max_total: int = 0,
        max_ratio: int = 0, reserved_total: int | None = None,
    ) -> List[str]:
        """Unpack every file in *archive_path* beneath the folder
        *destination*, keeping the folders inside it (#432, the Inbox): the
        paths written. The one shared extraction path that never hands a
        library a path name.

        The caller gates the archive first (:meth:`check_inbox_source` for an
        Inbox: the declared sizes, links, absolute and ``..`` paths). Here the
        members actually read are checked again before anything is written,
        and the whole archive is refused (:class:`ArchiveSafetyRefused`) when
        one is a link, or a name the extract path refuses
        (``_validate_member``: a backslash, an absolute or ``..`` path, an
        empty segment, a drive letter), or two members would land on one
        path. Junk is skipped.

        Every byte goes through the library's read API (a zip's or rar's
        ``open``, py7zr's writer factory) into a file created beneath the
        held folder *destination_fd* (opened from *destination* with no link
        followed when not given) by ``mkdir``/``open`` with ``dir_fd`` and
        ``O_NOFOLLOW``, the file ``O_EXCL``: nothing is reached by path name
        (Codex 4608030847). The bytes are counted as they are written, never
        the header trusted (Codex 4608007766): a member that yields more than
        it declares, or more than *max_member*, or a total past *max_total*
        (0: no limit), is refused before the chunk that would cross the line
        is written. With *max_ratio*, the bytes written are held to that
        many times each member's compressed size, and in all to that many
        times the archive's own size (Codex 4608936764). With
        *reserved_total*, the total written is held to it exactly, the room
        the caller reserved (CodeRabbit 4608968265); 0 allows nothing.

        *source*, when given, is the archive already opened by the caller (a
        binary file object it read through its own folder, no link
        followed); otherwise *archive_path* is opened. *progress*, when given,
        is called as each member's bytes are written. Blocking.
        """
        ext = Path(archive_path).suffix.lower()
        if source is not None:
            source.seek(0)
        opened = source if source is not None else archive_path
        packed = (
            os.fstat(source.fileno()).st_size if source is not None
            else os.path.getsize(archive_path)
        )
        budget = _UnpackBudget(max_member, max_total, max_ratio, packed, reserved_total)
        own_fd = None
        if destination_fd is None:
            own_fd = destination_fd = open_dir_beneath(os.path.realpath(destination))
        try:
            if ext == ".zip":
                with zipfile.ZipFile(opened, "r") as zf:
                    infos = zf.infolist()
                    targets = self._extract_targets([
                        (i.filename, i.is_dir(), stat.S_ISLNK(i.external_attr >> 16))
                        for i in infos
                    ])
                    for info in infos:
                        if info.filename in targets:
                            budget.declare(
                                info.filename, info.file_size, info.compress_size,
                            )
                            # By name: the reader's own record of the member.
                            with zf.open(info.filename) as src:
                                self._write_member(
                                    src, destination_fd, targets[info.filename],
                                    progress, budget, info.filename,
                                )
            elif ext == ".7z" and HAS_7Z:
                with py7zr.SevenZipFile(opened, "r") as zf:
                    entries = zf.list()
                    targets = self._extract_targets([
                        # Neither a folder nor a file (a link, a junction): refused.
                        (e.filename, e.is_directory, not (e.is_file or e.is_directory))
                        for e in entries
                    ])
                    for entry in entries:
                        if entry.filename in targets:
                            budget.declare(
                                entry.filename, entry.uncompressed,
                                getattr(entry, "compressed", None),
                            )
                    factory = _BudgetedWriters(destination_fd, targets, budget, progress)
                    try:
                        if targets:
                            zf.extract(targets=list(targets), factory=factory)
                    finally:
                        factory.close_all()
                for member, segments in targets.items():
                    if member in factory.created:
                        continue
                    # py7zr hands an empty member no writer: it is made here.
                    if budget.declared(member) != 0:
                        raise ArchiveSafetyRefused(
                            f"Archive member wasn't unpacked: {member}",
                            rule=f"{member} didn't unpack",
                        )
                    _create_member(destination_fd, segments).close()
            elif ext == ".rar" and HAS_RAR:
                with rarfile.RarFile(opened, "r") as rf:
                    infos = rf.infolist()
                    targets = self._extract_targets([
                        (i.filename, i.is_dir(), _rar_is_link(i)) for i in infos
                    ])
                    for info in infos:
                        if info.filename in targets:
                            budget.declare(
                                info.filename, info.file_size,
                                getattr(info, "compress_size", None),
                            )
                            with rf.open(info) as src:
                                self._write_member(
                                    src, destination_fd, targets[info.filename],
                                    progress, budget, info.filename,
                                )
            else:
                raise ValueError(f"Unsupported archive format: {ext}")
        finally:
            if own_fd is not None:
                os.close(own_fd)
        return [os.path.join(destination, *segments) for segments in targets.values()]

    def _extract_targets(
        self, members: list[Tuple[str, bool, bool]],
    ) -> Dict[str, List[str]]:
        """``member -> its path segments`` for every file member of
        ``(name, is_dir, is_link)``, all checked before anything is written.
        Raises :class:`ArchiveSafetyRefused` (see :meth:`extract_members`)."""
        targets: Dict[str, List[str]] = {}
        seen: set[str] = set()
        for name, is_dir, is_link in members:
            if is_link:
                raise ArchiveSafetyRefused(
                    f"Archive member is a link: {name}", rule=f"a link ({name})",
                )
            if is_dir or is_junk_path(name):
                continue
            try:
                segments = self._validate_member(name)
            except ValueError as exc:
                raise ArchiveSafetyRefused(
                    f"Unsafe archive member path: {name}",
                    rule=f"a member name it refuses ({name!r}: {exc})",
                ) from exc
            key = os.path.normcase("/".join(segments))
            if key in seen:
                raise ArchiveSafetyRefused(
                    f"Two archive members unpack to one path: {name}",
                    rule=f"two members named {name}",
                )
            seen.add(key)
            targets[name] = segments
        return targets

    @staticmethod
    def _write_member(
        src, root_fd: int, segments: List[str], progress, budget, member: str,
    ) -> None:
        """Copy one member's bytes to a file created beneath *root_fd*
        (:func:`_create_member`), each chunk counted against *budget* before
        it is written."""
        with _create_member(root_fd, segments) as dst:
            while chunk := src.read(_UNPACK_CHUNK):
                budget.take(member, len(chunk))
                dst.write(chunk)
                if progress is not None:
                    progress(member)

    def _create_temp_dir(self) -> str:
        base_dir = settings.temp_dir
        if base_dir is None:
            base_dir = str(Path(settings.data_dir) / "temp")
        if base_dir:
            try:
                Path(base_dir).mkdir(parents=True, exist_ok=True)
                return tempfile.mkdtemp(prefix="chd_extract_", dir=base_dir)
            except OSError as exc:
                logger.warning("Failed to create temp dir %s: %s", base_dir, exc)
        return tempfile.mkdtemp(prefix="chd_extract_")

    def is_archive(self, filename: str) -> bool:
        """Check if a file is a supported archive."""
        ext = Path(filename).suffix.lower()
        return ext in ARCHIVE_EXTENSIONS

    @staticmethod
    def _listable_extensions() -> frozenset:
        """Extensions surfaced when *browsing* the members of an archive.

        Global, scoped to known extensions: every source extension any tool
        recognizes (``registry.convertible_extensions()``) plus anything a mode
        can accept straight from an archive (``archive_input_extensions()`` —
        which adds ``.chd``, an output chdman disowns as a loose source but
        decompresses from inside an archive), minus the archive container
        extensions themselves (no point listing a ``.zip`` inside a ``.zip``).
        This is a superset of the convert-gate set, so it also shows members
        that are visible-only — a romz ROM appears when you browse into its
        archive even though no mode will re-convert it in place (its
        ``convertible_by`` stays empty, see ``tools_accepting_archive_member``).
        The registry is the single source of truth (it always loads in-app), so
        an empty union surfaces a registry bug loudly instead of being masked by
        a stale hardcoded fallback.
        """
        from services.tools import registry

        exts = (
            frozenset(registry.convertible_extensions())
            | frozenset(registry.archive_input_extensions())
        ) - ARCHIVE_EXTENSIONS
        if not exts:
            # An empty union means the registry is unloaded/broken, not that
            # nothing is convertible. Fail loudly so the archive listing surfaces
            # the bug rather than silently rendering an "empty archive".
            raise RuntimeError(
                "Tool registry produced no listable extensions — registry not loaded?"
            )
        return exts

    @staticmethod
    def _convert_gate_extensions() -> frozenset:
        """Extensions a member must have to be a real archive-conversion input.

        The narrower convert-gate subset (``registry.archive_input_extensions``):
        only members some mode can actually accept from an archive. Used by the
        recursive *search* path, which surfaces convertible hits — not the wider
        browse listing — so list-only members (romz ROMs) never count toward the
        per-archive entry cap before a genuine convertible member is reached.
        """
        from services.tools import registry

        exts = frozenset(registry.archive_input_extensions())
        if not exts:
            raise RuntimeError(
                "Tool registry produced no archive-input extensions — registry not loaded?"
            )
        return exts

    def list_archive_contents(
        self, archive_path: str, *, include_meta: bool = False,
        convertible_only: bool = False,
        progress: Optional[Callable[..., None]] = None,
    ) -> Union[List[dict], Dict[str, Union[List[dict], bool]]]:
        """List the files inside an archive.

        Members are read through the shared, mtime-cached
        :func:`services.archive_members.read_archive_members`, so an archive is
        opened at most once per ``(path, mtime, size)`` no matter how many times
        the browser/summary path lists it (and the romz gate shares that same
        cached read). The extension/limit filtering and ``output_stem`` shaping
        are applied per call against the cached raw members, returning fresh dicts
        the archive route is free to annotate in place.

        ``convertible_only`` narrows the extension gate from the browse listing
        (every known source, :meth:`_listable_extensions`) to the convert-gate
        subset (:meth:`_convert_gate_extensions`). The recursive search path sets
        it so list-only members never consume the entry cap ahead of a genuine
        convertible member.

        ``progress``, when given, is called as the archive's bytes arrive
        (:func:`services.archive_members.read_archive_members`), for a
        stall-bounded listing (#361).
        """
        ext = Path(archive_path).suffix.lower()
        entries: List[dict] = []
        truncated = False
        allowed = (
            self._convert_gate_extensions()
            if convertible_only
            else self._listable_extensions()
        )
        if ext in ARCHIVE_EXTENSIONS:
            start = time.monotonic()
            try:
                if later_volume_of(archive_path, progress) is not None:
                    # A later volume of a RAR set (#369): its members are
                    # listed under the first volume, and rarfile refuses it.
                    raw_members = []
                else:
                    raw_members = read_archive_members(archive_path, progress)
                entries, truncated = self._filter_members(
                    archive_path, raw_members, allowed,
                )
            except Exception as e:
                logger.exception("Error listing archive %s: %s", archive_path, e)
            finally:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Listed archive %s entries=%d in %.2fs",
                        archive_path,
                        len(entries),
                        time.monotonic() - start,
                    )

        entries = self._filter_preferred_entries(entries)

        for entry in entries:
            entry["output_stem"] = self._output_stem_for_member(entry["internal_path"])

        if include_meta:
            return {"entries": entries, "truncated": truncated}
        return entries

    def _filter_members(
        self, archive_path: str, members: list, allowed: frozenset,
    ) -> Tuple[List[dict], bool]:
        """Filter raw archive members to entries within size limits.

        Format-agnostic replacement for the old per-format ``_list_zip`` /
        ``_list_7z`` / ``_list_rar`` triplet: every container now yields a
        uniform :class:`~services.archive_members.ArchiveMember` list, so a single
        pass applies the junk filter, member-path validation, the caller's
        ``allowed`` extension gate, and the configured entry/size limits. ``size``
        of ``None`` means the container didn't record an uncompressed size; that's
        a skip-and-truncate when any size limit is active, otherwise treated as 0.
        The entry cap counts only members that pass ``allowed``, so a caller
        gating on the convert-gate subset never burns the cap on list-only rows.
        """
        entries: List[dict] = []
        max_entries, max_member_size, max_total_size = self._archive_limits()
        convertible = allowed
        total_size = 0
        truncated = False
        for member in members:
            if member.is_dir:
                continue
            if is_junk_path(member.name):
                continue
            try:
                self._validate_member(member.name)
            except ValueError:
                continue
            # Suffix match, not Path.suffix equality, so a member declared with
            # a compound extension (nkit2iso's .nkit.iso) passes the gate. The
            # recorded ``extension`` stays the plain trailing suffix: it feeds
            # the synthetic-sibling path and the icon buckets, both of which key
            # on the generic tail, while convertibility is re-derived from the
            # member name via ``tools_accepting_archive_member``.
            if match_extension(member.name, convertible) is None:
                continue
            ext = Path(member.name).suffix.lower()
            size = member.size
            if size is None:
                if max_member_size > 0 or max_total_size > 0:
                    logger.warning(
                        "Skipping archive member %s (unknown size)", member.name,
                    )
                    truncated = True
                    continue
                size = 0
            if max_member_size > 0 and size > max_member_size:
                logger.warning(
                    "Skipping oversized archive member %s (%s)",
                    member.name,
                    self._format_size(size),
                )
                truncated = True
                continue
            if max_total_size > 0 and (total_size + size) > max_total_size:
                logger.warning(
                    "Archive %s hit max total size limit (%s)",
                    archive_path,
                    self._format_size(max_total_size),
                )
                truncated = True
                break
            total_size += size
            entries.append(
                {
                    "archive_path": archive_path,
                    "internal_path": member.name,
                    "name": os.path.basename(member.name),
                    "size": size,
                    "extension": ext,
                    # No "convertible" flag here: a member can be *listable*
                    # (e.g. a romz ROM, surfaced for visibility) without being a
                    # valid archive-conversion input. Convertibility is decided
                    # per consumer from the registry (route layer derives
                    # ``convertible_by`` via ``tools_accepting_archive_member``),
                    # so stamping a blanket True here would misreport list-only
                    # members to any direct caller of this service.
                }
            )
            if max_entries > 0 and len(entries) >= max_entries:
                logger.warning(
                    "Archive %s hit max entry limit (%s)",
                    archive_path,
                    max_entries,
                )
                truncated = True
                break
        return entries, truncated

    @staticmethod
    def _filter_preferred_entries(entries: List[dict]) -> List[dict]:
        if not entries:
            return entries

        exts_by_parent = {}
        for entry in entries:
            parent = PurePosixPath(entry["internal_path"]).parent
            exts_by_parent.setdefault(parent, set()).add(entry.get("extension"))

        filtered = []
        for entry in entries:
            ext = entry.get("extension")
            parent = PurePosixPath(entry["internal_path"]).parent
            exts = exts_by_parent.get(parent, set())
            if ext == ".bin" and (".cue" in exts or ".gdi" in exts):
                continue
            filtered.append(entry)

        return filtered

    def extract_file(self, archive_path: str, internal_path: str) -> Tuple[str, str]:
        """Extract a specific file from an archive into a temp directory."""
        ext = Path(archive_path).suffix.lower()
        temp_dir = self._create_temp_dir()

        try:
            destination = self._prepare_destination(temp_dir, internal_path)
            size = self._coerce_size(self._get_member_size(archive_path, internal_path))
            if size is None:
                if self._size_limits_enabled():
                    raise ValueError(
                        "Archive member size unknown; cannot enforce limits"
                    )
            else:
                self._check_member_size(size, member=internal_path)
                self._check_total_size(size)

            if ext == ".zip":
                extracted = self._extract_from_zip(
                    archive_path, internal_path, destination
                )
            elif ext == ".7z" and HAS_7Z:
                extracted = self._extract_from_7z(
                    archive_path, internal_path, destination
                )
            elif ext == ".rar" and HAS_RAR:
                extracted = self._extract_from_rar(
                    archive_path, internal_path, destination
                )
            else:
                raise ValueError(f"Unsupported archive format: {ext}")

            return extracted, temp_dir

        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _validate_member(self, member: str) -> List[str]:
        """Ensure an archive member path cannot escape the extraction directory."""
        if "\x00" in member:
            raise ValueError("NUL bytes are not allowed in archive members")
        if "\\" in member:
            raise ValueError("Backslashes are not allowed in archive members")
        normalized = member.strip()
        if normalized.startswith("/"):
            raise ValueError("Absolute paths are not allowed inside archives")
        segments = normalized.split("/")
        if not segments or any(part in ("", ".", "..") for part in segments):
            raise ValueError("Invalid archive member path")
        if any(":" in part for part in segments):
            raise ValueError("Drive letters are not allowed inside archives")
        return segments

    def member_segments(self, member: str) -> List[str]:
        """The path segments of a member name, or ``ValueError`` if it is unsafe.

        The same rules the extract path applies (no backslashes, absolute paths,
        ``..``, empty segments or drive letters), for callers that read archives
        themselves. A name with leading or trailing whitespace is refused too:
        the rules check the stripped name, and a caller that then uses the raw
        name would act on a different path than the one checked. A directory
        entry's trailing ``/`` is the caller's to strip.
        """
        segments = self._validate_member(member)
        if "/".join(segments) != member:
            raise ValueError(f"Member name has leading or trailing spaces: {member!r}")
        return segments

    def _prepare_destination(self, temp_dir: str, member: str) -> str:
        segments = self._validate_member(member)
        base = Path(temp_dir).resolve()
        candidate = base.joinpath(*segments).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError("Archive member resolves outside temp directory") from exc
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return str(candidate)

    def _extract_from_zip(
        self, archive_path: str, internal_path: str, destination: str
    ) -> str:
        with zipfile.ZipFile(archive_path, "r") as zf:
            try:
                with zf.open(internal_path) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except KeyError as exc:
                raise FileNotFoundError(
                    f"{internal_path} not found in archive"
                ) from exc
        return destination

    def extract_related_files(
        self, archive_path: str, internal_path: str, temp_dir: str
    ):
        """Extract sibling files for multi-file formats like .cue or .gdi."""
        entry_ext = Path(internal_path).suffix.lower()
        if entry_ext not in {".cue", ".gdi"}:
            return

        parent = PurePosixPath(internal_path).parent
        if str(parent) == ".":
            parent = PurePosixPath("")

        initial_total_size = 0
        primary_size = self._coerce_size(
            self._get_member_size(archive_path, internal_path)
        )
        if primary_size is None:
            if self._size_limits_enabled():
                raise ValueError("Archive member size unknown; cannot enforce limits")
        else:
            initial_total_size = primary_size
            self._check_total_size(initial_total_size)

        archive_ext = Path(archive_path).suffix.lower()
        if archive_ext == ".zip":
            self._extract_related_from_zip(
                archive_path,
                parent,
                temp_dir,
                initial_total_size,
                primary_member=internal_path,
            )
        elif archive_ext == ".7z" and HAS_7Z:
            self._extract_related_from_7z(
                archive_path,
                parent,
                temp_dir,
                initial_total_size,
                primary_member=internal_path,
            )
        elif archive_ext == ".rar" and HAS_RAR:
            self._extract_related_from_rar(
                archive_path,
                parent,
                temp_dir,
                initial_total_size,
                primary_member=internal_path,
            )
        else:
            raise ValueError(f"Unsupported archive format: {archive_ext}")

    def _get_member_size(self, archive_path: str, internal_path: str) -> Optional[int]:
        # Reuse the shared, cached member read instead of re-opening the archive
        # for a single size lookup. Returns None when the member is absent or the
        # container didn't record an uncompressed size (mirrors the prior 7z
        # behaviour, which returned ``entry.uncompressed`` unmodified).
        #
        # For a ZIP with duplicate member names, ``zipfile.open(name)`` extracts
        # the *last* such entry (NameToInfo maps a name to its final occurrence),
        # so the size guard must read the last match too — otherwise a small
        # leading duplicate could let the limit check pass while a larger trailing
        # payload is the one actually extracted.
        size: Optional[int] = None
        try:
            for member in read_archive_members(archive_path):
                if member.name == internal_path:
                    size = member.size
        except Exception:
            return None
        return size

    def _extract_related_from_zip(
        self,
        archive_path: str,
        parent: PurePosixPath,
        temp_dir: str,
        initial_total_size: int = 0,
        *,
        primary_member: Optional[str] = None,
    ):
        total_size = max(0, int(initial_total_size))
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if is_junk_path(info.filename):
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                if not self._is_same_parent(info.filename, parent):
                    continue
                if primary_member and info.filename == primary_member:
                    continue
                self._check_member_size(info.file_size, member=info.filename)
                total_size += info.file_size
                self._check_total_size(total_size)
                destination = self._prepare_destination(temp_dir, info.filename)
                with zf.open(info.filename) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    def _extract_related_from_7z(
        self,
        archive_path: str,
        parent: PurePosixPath,
        temp_dir: str,
        initial_total_size: int = 0,
        *,
        primary_member: Optional[str] = None,
    ):
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            targets = []
            total_size = max(0, int(initial_total_size))
            _, max_member_size, max_total_size = self._archive_limits()
            for entry in zf.list():
                if entry.is_directory:
                    continue
                name = entry.filename
                if is_junk_path(name):
                    continue
                try:
                    self._validate_member(name)
                except ValueError:
                    continue
                if self._is_same_parent(name, parent):
                    if primary_member and name == primary_member:
                        continue
                    size = self._coerce_size(entry.uncompressed)
                    if size is None:
                        if max_member_size > 0 or max_total_size > 0:
                            raise ValueError(
                                "Archive member size unknown; cannot enforce limits"
                            )
                        size = 0
                    self._check_member_size(size, member=name)
                    total_size += size
                    self._check_total_size(total_size)
                    targets.append(name)
            if targets:
                zf.extract(targets=targets, path=temp_dir)

    def _extract_related_from_rar(
        self,
        archive_path: str,
        parent: PurePosixPath,
        temp_dir: str,
        initial_total_size: int = 0,
        *,
        primary_member: Optional[str] = None,
    ):
        total_size = max(0, int(initial_total_size))
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            for info in rf.infolist():
                if info.is_dir():
                    continue
                if is_junk_path(info.filename):
                    continue
                try:
                    self._validate_member(info.filename)
                except ValueError:
                    continue
                if not self._is_same_parent(info.filename, parent):
                    continue
                if primary_member and info.filename == primary_member:
                    continue
                self._check_member_size(info.file_size, member=info.filename)
                total_size += info.file_size
                self._check_total_size(total_size)
                destination = self._prepare_destination(temp_dir, info.filename)
                with rf.open(info.filename) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    @staticmethod
    def _is_same_parent(member: str, parent: PurePosixPath) -> bool:
        member_parent = PurePosixPath(member).parent
        if str(parent) == "":
            return str(member_parent) in ("", ".")
        return member_parent == parent

    @staticmethod
    def _output_stem_for_member(member: str) -> str:
        member_path = PurePosixPath(member)
        parent = member_path.parent
        stem = member_path.stem
        if str(parent) in ("", "."):
            return stem
        safe_parent = "_".join([p for p in parent.parts if p not in ("", ".")])
        return f"{safe_parent}_{stem}"

    @staticmethod
    def _output_name_for_member(member: str) -> str:
        """Flattened *filename* (subdirectories collapsed) that preserves the
        member's original extension.

        Tools whose output extension is derived from the input, z3ds maps
        ``.3ds`` -> ``.z3ds``, ``.cci`` -> ``.zcci``, need the original
        extension to pick the right output name. ``_output_stem_for_member``
        drops it (it exists only for chd existing-output detection, which is
        always ``<stem>.chd``), so archive conversions route the output path
        through this helper instead. Each tool's ``get_output_path_for_mode``
        treats the result as a normal filename when ``treat_as_stem=True``.
        """
        stem = ArchiveService._output_stem_for_member(member)
        suffix = PurePosixPath(member).suffix
        return f"{stem}{suffix}"

    def _extract_from_7z(
        self, archive_path: str, internal_path: str, destination: str
    ) -> str:
        with py7zr.SevenZipFile(archive_path, "r") as zf:  # type: ignore[name-defined]
            try:
                parts = PurePosixPath(internal_path).parts
                if not parts:
                    raise FileNotFoundError(f"{internal_path} not found in archive")
                base_dir = Path(destination).parents[len(parts) - 1]
                zf.extract(targets=[internal_path], path=str(base_dir))
            except Exception as exc:
                raise FileNotFoundError(
                    f"{internal_path} not found in archive"
                ) from exc

        if not os.path.isfile(destination):
            raise FileNotFoundError(f"{internal_path} not found in archive")
        return destination

    def _extract_from_rar(
        self, archive_path: str, internal_path: str, destination: str
    ) -> str:
        with rarfile.RarFile(archive_path, "r") as rf:  # type: ignore[name-defined]
            try:
                with rf.open(internal_path) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except (rarfile.Error, KeyError) as exc:  # type: ignore[name-defined]
                raise FileNotFoundError(
                    f"{internal_path} not found in archive"
                ) from exc
        return destination

    def cleanup_temp_dir(self, temp_dir: str):
        """Clean up a temporary extraction directory."""
        shutil.rmtree(temp_dir, ignore_errors=True)


archive_service = ArchiveService()
