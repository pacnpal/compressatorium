# Codacy's pylintpython3 ships `pylint-django` and emits E5110 / I1010
# `django-not-configured` on every Python module despite this not being a
# Django project. `.pylintrc` already disables the named check; the inline
# pragma is a belt-and-braces signal for the wrapper, which does not always
# honour the project-level config.
# pylint: disable=django-not-configured
import contextlib
import fcntl
import hashlib
import logging
from logging_setup import get_logger
import os
import threading
import time
from pathlib import Path

from config import settings

logger = get_logger("lock_manager")

# Lock file kinds. Every lock on an Image path (output, input, folder) is a
# ``filelock-`` file; a Referrer's lock on a file it rewrites (a playlist) is a
# ``referrer-`` file, so the two can never be the same lock, even on one path.
IMAGE_LOCK = "filelock"
REFERRER_LOCK = "referrer"
_LOCK_KINDS = (IMAGE_LOCK, REFERRER_LOCK)


class LockWaitTimeout(TimeoutError):
    """A bounded wait for a lock ran out while someone else still held it."""


class LockFailed(RuntimeError):
    """A lock could not be taken for a reason other than someone holding it:
    the lock file can't be made or locked (ENAMETOOLONG, EACCES, EROFS,
    ENOSPC, ENOLCK ...). Not contention, so the caller fails the job with the
    reason instead of waiting for a turn that never comes (codex 4577893015).
    """

    def __init__(self, path: str, exc: BaseException):
        reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        super().__init__(f"Could not lock {path}: {reason}")


# The readable part of a lock file name, in UTF-8 bytes. The full-key hash
# keeps names unique; this keeps every name (kind, base, 64-hex digest,
# ".lock") far under the 255-byte NAME_MAX of common filesystems.
_LOCK_NAME_BASE_BYTES = 64


def _lock_name_base(path: str) -> str:
    """The sanitised basename of ``path``, cut to ``_LOCK_NAME_BASE_BYTES``
    bytes on a character boundary."""
    base = os.path.basename(path) or "chd"
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in base)
    cut = safe.encode("utf-8", "replace")[:_LOCK_NAME_BASE_BYTES]
    return cut.decode("utf-8", "ignore") or "chd"


def still_linked(handle, path: str) -> bool:
    """Whether the file ``handle`` holds is still the one at ``path``.

    Compares device and inode; a missing path (ENOENT) is a mismatch. Shared
    by every flock-based lock file here and in ``concurrency_manager``: after
    an flock succeeds, a mismatch means the lock is on a file its last holder
    unlinked, which no other process can see.
    """
    try:
        held = os.fstat(handle.fileno())
        current = os.stat(path)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def unlink_held(handle, path: str) -> bool:
    """Unlink the lock file ``handle`` holds, before letting go of it.

    Removes it only when ``path`` still names the held file. Call it while the
    flock is held, then unlock. True when the file was removed.
    """
    if not still_linked(handle, path):
        return False
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    return True


def open_lock_handle(path: str):
    """Open (creating) a lock file for ``flock``, as ``open(path, "ab")`` does.

    The handle is held past the call (for as long as its lock is), so it is
    made from an ``os.open`` descriptor rather than a ``with`` block. Binary
    append: a flock-only handle, or one that records a path with ``write``.
    Shared by every lock file here and in ``concurrency_manager``.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    try:
        return os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise


# The path a lock file records (``_record_path``), one encoding for every lock
# kind: ``hex:`` and the hex of the path's filesystem bytes, then a newline.
# Lossless for any pathname, a newline or a non-UTF-8 byte included (codex
# 4578027196). A first line without the prefix is the older plain record
# (``os.fsencode(path)``, newline-terminated), still read during an upgrade.
_RECORD_PREFIX = b"hex:"
# PATH_MAX (4096) bytes as hex, the prefix and the newline, with room to spare.
_RECORD_LINE_MAX = 2 * 4096 + 64


def _encode_recorded_path(path: str) -> bytes:
    return _RECORD_PREFIX + os.fsencode(path).hex().encode("ascii") + b"\n"


def _decode_recorded_path(first_line: bytes) -> str | None:
    """The path a lock file's first line records; None when it is empty,
    still being written, or unreadable."""
    if not first_line.endswith(b"\n"):
        return None  # empty, or being written right now
    body = first_line[:-1]
    if body.startswith(_RECORD_PREFIX):
        try:
            raw = bytes.fromhex(body[len(_RECORD_PREFIX):].decode("ascii"))
        except ValueError:
            return None
        return os.fsdecode(raw) or None
    return os.fsdecode(body) or None


class LockManager:
    """Manages file locks to prevent concurrent conversions of the same file."""

    def __init__(self, *, lazy: bool = False):
        self._lock_dir = (
            settings.concurrency_lock_dir
            or str(Path(settings.data_dir) / "locks")
        )
        # A lazy manager (the module singleton) creates its directory and
        # sweeps stale lock files on first use, not at import, so a process
        # that only plans (a CLI dry run) leaves the shared lock dir alone.
        self._ready = False
        # Read-only: report a lock file as held without probing or removing it,
        # and take no locks. Set for a CLI dry run (`read_only()`).
        self.is_read_only = False
        self._locks: set[str] = set()
        # Directory subtree locks: a directory-input job (makeps3iso
        # folder->iso) locks its whole source tree here so a concurrent
        # per-file job / rename / delete whose path falls *inside* the folder
        # contends on the lock — exactly like an output-path collision — instead
        # of mutating the tree while it's being packed. Keyed by the normalized
        # directory path; containment is checked in-process (the documented
        # MAX_CONCURRENT_JOBS concurrency all lives in this one process).
        self._dir_locks: set[str] = set()
        self._lock_mutex = threading.Lock()
        self._lock_handles = {}
        self._dir_lock_handles = {}
        # Shared (read) locks on the files running jobs read, one handle per
        # acquisition, so two jobs here reading one Source each hold their own.
        self._input_handles: dict[str, list] = {}
        # Lock identity is the real path (#306): a path reached through a
        # symlinked folder locks, records and probes as the file it names, so
        # a probe on the real path sees it. Each caller path maps to the
        # identity it was locked under, kept unchanged through release.
        self._lock_keys: dict[str, list[str]] = {}
        if not lazy:
            self._ensure_ready()

    def _ensure_ready(self) -> None:
        """Create the lock directory (owner only) and sweep stale lock files, once."""
        if self._ready:
            return
        # Create lock directory with restrictive permissions (owner only)
        # to address security concerns about predictable temp directories
        os.makedirs(self._lock_dir, mode=0o700, exist_ok=True)
        self._ready = True
        self._cleanup_stale_locks()

    @contextlib.contextmanager
    def read_only(self):
        """Within the block, look at lock files but never take, probe or remove one."""
        previous = self.is_read_only
        self.is_read_only = True
        try:
            yield self
        finally:
            self.is_read_only = previous

    @staticmethod
    def _path_within(child: str, parent: str) -> bool:
        """True when resolved ``child`` is ``parent`` or nested under it."""
        if child == parent:
            return True
        return child.startswith(parent + os.sep)

    @staticmethod
    def _resolve(path: str) -> str:
        """Resolve symlinks (and normalize) so subtree containment can't be
        bypassed by reaching a locked directory through a symlinked path
        (e.g. ``/vol/link_to_Game/PS3_GAME/...`` vs the locked ``/vol/Game``).
        ``realpath`` resolves the existing ancestors of a not-yet-created output
        and leaves the leaf lexical, which is exactly what containment needs.
        """
        try:
            return os.path.realpath(path)
        except OSError:
            return os.path.normpath(path)

    def _within_locked_dir_locked(self, resolved_path: str) -> bool:
        """Whether an already-resolved path falls inside a locked dir subtree.

        Caller must hold ``self._lock_mutex`` and pass a ``_resolve``-d path.
        ``self._dir_locks`` entries are stored resolved.
        """
        return any(
            self._path_within(resolved_path, locked_dir)
            for locked_dir in self._dir_locks
        )

    def _dir_overlaps_locked(self, resolved: str) -> bool:
        """Whether a directory subtree at resolved path overlaps any lock.

        Caller must hold ``self._lock_mutex``. True when the directory itself is
        locked, a locked output file resolves inside it, or another directory
        lock nests with it in either direction.
        """
        if resolved in self._dir_locks:
            return True
        # The keys are resolved identities already (`_output_keys`): compared
        # as they are, with no path resolution under the mutex.
        if any(self._path_within(p, resolved) for p in self._locks):
            return True
        return any(
            self._path_within(resolved, d) or self._path_within(d, resolved)
            for d in self._dir_locks
        )

    def is_within_locked_dir(self, path: str) -> bool:
        """Whether ``path`` falls inside a directory subtree another job locked.

        The read-only companion to :meth:`acquire_lock`'s subtree check: lets the
        job pipeline tell a *transient* conflict (an active folder->iso job is
        packing a tree this path lives in — wait and retry) apart from a
        permanent one (the output already exists — fail). Resolves symlinks.
        """
        resolved = self._resolve(path)  # filesystem I/O: outside the mutex
        with self._lock_mutex:
            if not self._dir_locks:
                return False
            return self._within_locked_dir_locked(resolved)

    def locked_folder_above(
        self, path: str, stop_at: str | None = None, *, include_self: bool = True,
    ) -> str | None:
        """The nearest folder at or above ``path`` another process has dir-locked.

        A directory lock (makeps3iso packing a folder) protects its subtree only
        inside the process that holds it (``_dir_locks``). Another process, a
        CLI run beside the server, holds the same lock *file*, so this walks
        ``path`` and each parent up to ``stop_at`` (the Volume root) and probes
        each one's lock file without blocking: one held by someone else means
        the folder is being read (#306). A stale lock file found on the way is
        removed, as ``check_file_status`` does. Returns the folder, or None.
        ``include_self=False`` starts at the parent: a caller that holds the
        path's own lock asks only about the folders around it.
        """
        current = self._resolve(path)
        stop = self._resolve(stop_at) if stop_at else None
        first = True
        while True:
            lock_file = self._lock_file_path(current)
            if (include_self or not first) and os.path.exists(lock_file) and (
                self.is_read_only or self._remove_if_stale(lock_file) is False
            ):
                return current
            first = False
            parent = os.path.dirname(current)
            if current in (stop, parent):
                return None
            current = parent

    def folder_locked_above(self, path: str) -> str | None:
        """A folder above ``path``, up to its Volume root, that some process
        holds a lock on (``locked_folder_above`` without the path itself)."""
        from utils.path_utils import lock_boundary_root

        root = lock_boundary_root(path)
        return self.locked_folder_above(
            path, str(root) if root is not None else None, include_self=False,
        )

    def locked_path_below(self, dir_path: str, *, include_self: bool = True) -> str | None:
        """A path at or inside ``dir_path`` that some process holds a lock on.

        Lock files are named by a hash, so each one records the path it locks
        (``_record_path``) when it is taken. This reads every lock file in the
        lock dir, and for one naming a path inside the folder probes it without
        blocking, as ``check_file_status`` does: held by anyone (another
        process, or another handle here) means something inside is being
        written or read, so the folder must not be renamed or deleted (#306).
        A stale one found on the way is removed. Returns that path, or None.
        ``include_self=False`` skips the folder's own lock (a caller holding it).
        """
        prefixes = {os.path.normpath(dir_path), self._resolve(dir_path)}
        try:
            names = [
                name for name in os.listdir(self._lock_dir)
                if name.startswith("filelock-") and name.endswith(".lock")
            ]
        except OSError:
            return None
        for name in names:
            lock_file = os.path.join(self._lock_dir, name)
            locked = self._recorded_path(lock_file)
            if not locked or not any(
                self._path_within(locked, prefix) for prefix in prefixes
            ):
                continue
            if not include_self and locked in prefixes:
                continue
            if self.is_read_only or self._remove_if_stale(lock_file) is False:
                return locked
        return None

    @staticmethod
    def _record_path(handle, locked_path: str) -> None:
        """Write the locked path into a lock file just taken, once.

        Readers sharing one file may both see it empty and both write; the
        first line is what counts. Best effort: a lock without its path is
        still a lock, only ``locked_path_below`` can't see it.
        """
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(_encode_recorded_path(locked_path))
                handle.flush()
        except OSError:
            logger.debug("Could not record the path of a lock", exc_info=True)

    @staticmethod
    def _recorded_path(lock_file: str) -> str | None:
        try:
            with open(lock_file, "rb") as handle:
                first = handle.readline(_RECORD_LINE_MAX)
        except OSError:
            return None
        return _decode_recorded_path(first)

    def dir_lock_would_conflict(self, dir_path: str) -> bool:
        """Whether :meth:`acquire_dir_lock` for ``dir_path`` would currently
        conflict with an existing lock (without acquiring anything)."""
        resolved = self._resolve(dir_path)
        with self._lock_mutex:
            return self._dir_overlaps_locked(resolved)

    # How the lock files stay safe across processes (#306). Two rules, kept by
    # every path below:
    #
    # 1. A lock file is unlinked only by a process that holds its flock, and
    #    only while it still holds it (`_unlink_held`). Unlinking after the
    #    unlock let another process lock the doomed file in between: it then
    #    held a lock on a file no one else could see, and a third process
    #    created the path afresh and locked that too.
    # 2. Every acquirer, after its flock succeeds, checks the path still names
    #    the file it locked (`_still_linked`) and retries if not. Together with
    #    rule 1 this means a lock taken is a lock on the file every other
    #    process opens.

    _still_linked = staticmethod(still_linked)
    _unlink_held = staticmethod(unlink_held)

    def _remove_if_stale(self, lock_file_path: str) -> bool | None:
        """Remove a lock file no process holds.

        True when it was stale and is gone, False when someone holds it (or
        the path now names a newer file), None when it could not be probed.
        """
        try:
            # Binary mode: flock-only handle, no text is read or written.
            with open(lock_file_path, "ab") as lock_handle:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                except OSError:
                    return None
                try:
                    return self._unlink_held(lock_handle, lock_file_path)
                finally:
                    with contextlib.suppress(OSError):
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return None

    def _lock_file_path(self, normalized_path: str, kind: str = IMAGE_LOCK) -> str:
        # Use a stable hash to avoid path length issues and keep locks in /tmp.
        # Hashed as filesystem bytes (#306): a native name a sheet reads
        # (``Caf\xe9.bin``, carried as a surrogate escape) is a path like any
        # other, and every key derivation here goes through this one place.
        # Only the readable part is cut (``_lock_name_base``); the identity is
        # the hash of the whole key, unchanged.
        digest = hashlib.sha256(os.fsencode(normalized_path)).hexdigest()
        filename = f"{kind}-{_lock_name_base(normalized_path)}-{digest}.lock"
        return os.path.join(self._lock_dir, filename)

    def _cleanup_stale_locks(self, *, log_level: str = "info") -> int:
        """Remove stale file lock artifacts from the lock directory.

        Args:
            log_level: Logging level for removed locks - "info" or "debug"

        Returns:
            Number of stale locks removed
        """
        removed_count = 0
        try:
            lock_files = []
            try:
                lock_files = [
                    name for name in os.listdir(self._lock_dir)
                    if name.startswith(tuple(f"{kind}-" for kind in _LOCK_KINDS))
                    and name.endswith(".lock")
                ]
            except OSError:
                return removed_count

            for name in lock_files:
                lock_path = os.path.join(self._lock_dir, name)
                if self._remove_if_stale(lock_path):
                    removed_count += 1
                    if log_level == "info":
                        logger.info("Removed stale lock file: %s", name)
                    else:
                        logger.debug("Removed stale lock file: %s", name)
        except Exception as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Error during stale lock cleanup: %s", e)

        return removed_count

    def cleanup_stale_locks_periodic(self) -> int:
        """Public method for periodic cleanup from background tasks.

        Returns:
            Number of stale locks removed
        """
        if self.is_read_only:
            return 0
        self._ensure_ready()
        return self._cleanup_stale_locks(log_level="debug")

    def is_locked(self, output_path: str) -> bool:
        """Check if an output file is currently locked (being converted)."""
        _, is_locked = self.check_file_status(output_path)
        return is_locked

    def check_file_status(self, output_path: str) -> tuple[bool, bool]:
        """Check the status of an output file atomically.

        Returns:
            Tuple of (file_exists, is_locked)

        """
        normalized_path = os.path.normpath(output_path)
        key = self._resolve(output_path)  # the lock identity: the real path
        with self._lock_mutex:
            is_locked = key in self._locks
            if not is_locked and self._dir_locks:
                is_locked = self._within_locked_dir_locked(key)
        file_exists = os.path.isfile(normalized_path)  # outside the mutex

        if is_locked:
            return (file_exists, True)

        return (file_exists, self._check_external_lock(key))

    def _check_external_lock(self, normalized_path: str) -> bool:
        """Check if an external process holds a lock on the output file.

        Returns:
            True if the file is locked by another process, False otherwise.

        As a side effect, if a lock file exists but is not actively held, it will be removed.
        """
        lock_file_path = self._lock_file_path(normalized_path)
        if not os.path.exists(lock_file_path):
            return False
        if self.is_read_only:
            # No probe (that would take the lock for a moment) and no cleanup:
            # a lock file that is there counts as held.
            return True
        removed = self._remove_if_stale(lock_file_path)
        if removed and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Removed stale lock file during check: %s",
                os.path.basename(lock_file_path),
            )
        # Held by another process, or replaced under us by a newer lock file:
        # report it locked. A probe that could not run says "not locked", as
        # before.
        return removed is False

    # Subtree exclusion across processes (#306). A folder lock (makeps3iso
    # packing a folder) and a lock on anything inside it never coexist, in any
    # two processes and in either order. Each acquirer takes its own lock first
    # and then looks for the other kind: a file, output or input lock looks for
    # a folder lock above it, up to the Volume root (`folder_locked_above`); a
    # folder lock looks for any lock inside it (`locked_path_below`) and for a
    # folder lock above. On a conflict it lets go and reports failure, and the
    # job manager defers and retries with jitter. Because both sides lock
    # before they look, two racing acquirers can't both miss each other: at
    # worst both back off and retry.

    def acquire_lock(self, output_path: str, *, allow_existing: bool = False) -> bool:
        """Acquire a lock for the output file path.

        Returns:
            True if lock was acquired, False if already locked, CHD exists and
            overwrite is not allowed, or some process holds a folder above it

        """
        if not self._take_lock(output_path, allow_existing=allow_existing):
            return False
        with self._lock_mutex:
            keys = list(self._lock_keys.get(os.path.normpath(output_path), ()))
        if self._folder_locked_above_keys(keys) is not None:
            self.release_lock(output_path)
            return False
        return True

    def output_folder_locked_above(self, output_path: str) -> str | None:
        """A folder above any identity an output lock on ``output_path``
        takes (``_output_keys``), locked here or in any process; or None.

        The dispatch probe: the same identities, so a symlinked output whose
        own folder is being packed is seen even when its target is elsewhere
        (codex 4577847011).
        """
        return self._folder_locked_above_keys(self._output_keys(output_path))

    def _folder_locked_above_keys(self, keys: list[str]) -> str | None:
        """The first folder above any of ``keys`` (already-resolved lock
        identities) that a folder lock covers: in this process
        (``_dir_locks``) or by a held lock file, up to its Volume root.

        Each key's folder is walked as it stands; the key itself is never
        resolved again, so the non-following destination of a symlinked
        output is checked where the converter will write.
        """
        from utils.path_utils import lock_boundary_root

        for key in keys:
            with self._lock_mutex:
                for locked_dir in self._dir_locks:
                    if self._path_within(key, locked_dir):
                        return locked_dir
            root = lock_boundary_root(key)
            above = self.locked_folder_above(
                os.path.dirname(key), str(root) if root is not None else None,
            )
            if above is not None:
                return above
        return None

    def _output_keys(self, output_path: str) -> list[str]:
        """The identities an output lock takes: both places a write can land.

        The destination the converter creates, ``realpath(dirname)`` joined
        with the basename, which does not follow a symlink at the final
        component, so once an overwrite sweep has unlinked that symlink the new
        file's path is still the one locked; and, when the output is a symlink
        today, its current real target, so nobody rewrites the old target
        mid-job (codex 4577612344). One key when the output is no symlink.
        """
        absolute = os.path.abspath(output_path)
        destination = os.path.join(
            self._resolve(os.path.dirname(absolute)), os.path.basename(absolute),
        )
        target = self._resolve(output_path)
        return [destination] if target == destination else [destination, target]

    def _take_lock(self, output_path: str, *, allow_existing: bool) -> bool:
        """Every identity of ``_output_keys``, or none of them."""
        taken: list[str] = []
        try:
            for key in self._output_keys(output_path):
                if not self._take_key(key, allow_existing=allow_existing):
                    for held in taken:
                        self._release_key(held)
                    return False
                taken.append(key)
        except LockFailed:
            for held in taken:
                self._release_key(held)
            raise
        with self._lock_mutex:
            self._lock_keys[os.path.normpath(output_path)] = taken
        return True

    def _take_key(self, normalized_path: str, *, allow_existing: bool) -> bool:
        """One exclusive output lock on an identity from ``_output_keys``."""
        if self.is_read_only:
            return False
        self._ensure_ready()

        # No filesystem I/O under `_lock_mutex` (codex 4579852997): the mutex
        # only claims the identity in memory, so a probe that stalls on a dead
        # mount can never hold it against a caller on the event loop.
        with self._lock_mutex:
            # Check if already locked by another job
            if normalized_path in self._locks:
                return False

            # Reject a target that lives inside a directory subtree another job
            # has locked (makeps3iso packing the folder): the per-file job
            # contends here just like an exact output collision.
            if self._dir_locks and self._within_locked_dir_locked(normalized_path):
                return False
            # Claimed: everyone else here sees it locked from now on.
            self._locks.add(normalized_path)

        taken = False
        try:
            # Try to create a lock file
            lock_file_path = self._lock_file_path(normalized_path)
            self._make_lock_dir(lock_file_path, normalized_path)
            for _attempt in range(3):
                outcome = self._try_file_lock(
                    normalized_path, lock_file_path, allow_existing,
                )
                if outcome is not None:
                    taken = outcome
                    return outcome
            return False
        finally:
            if not taken:
                with self._lock_mutex:
                    self._locks.discard(normalized_path)

    def _try_file_lock(
        self, normalized_path: str, lock_file_path: str, allow_existing: bool,
    ) -> bool | None:
        """One attempt at :meth:`acquire_lock`; None when the lock file was
        unlinked under us and the attempt should be repeated. The caller has
        claimed *normalized_path* in ``_locks``; this runs without the mutex
        and takes it only to record the handle."""
        lock_handle = None
        try:
            with contextlib.ExitStack() as stack:
                # Open lock file in append mode (binary) to avoid truncating existing content.
                # Binary mode: flock-only handle, no text is read or written.
                lock_handle = stack.enter_context(open_lock_handle(lock_file_path))

                # Try to acquire an exclusive lock (non-blocking)
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # Lock is held by another process
                    return False

                if not self._still_linked(lock_handle, lock_file_path):
                    return None

                # Now that we have the lock, check if CHD already exists (atomic with lock)
                if os.path.exists(normalized_path):
                    if not allow_existing or not os.path.isfile(normalized_path):
                        # File exists and overwrite not allowed (or not a file):
                        # drop the lock file we are not using, while still
                        # holding it, then let go.
                        try:
                            self._unlink_held(lock_handle, lock_file_path)
                        except OSError:
                            logger.debug(
                                "Failed to clean up lock file: %s",
                                lock_file_path,
                                exc_info=True,
                            )
                        try:
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            logger.debug(
                                "Failed to release lock for existing file: %s",
                                normalized_path,
                                exc_info=True,
                            )
                        return False

                # Successfully acquired the lock and file doesn't exist
                self._record_path(lock_handle, normalized_path)
                with self._lock_mutex:
                    self._lock_handles[normalized_path] = lock_handle
                # Transfer ownership to _lock_handles by detaching ExitStack cleanup.
                # This keeps lock_handle open for the conversion lifecycle.
                stack.pop_all()
                return True

        except Exception as exc:
            # Ensure file handle is closed on any error
            if lock_handle is not None:
                with self._lock_mutex:
                    if self._lock_handles.get(normalized_path) is lock_handle:
                        self._lock_handles.pop(normalized_path, None)
                try:
                    lock_handle.close()
                except Exception:
                    logger.debug(
                        "Failed to close lock handle for: %s",
                        normalized_path,
                        exc_info=True,
                    )
            logger.error("Failed to acquire lock for %s", normalized_path, exc_info=True)
            # Not contention (that returned False above): an error, so the job
            # fails with the reason rather than being requeued forever.
            raise LockFailed(normalized_path, exc) from exc

    def release_lock(self, output_path: str):
        """Release the lock for an output file path: every identity it was
        taken under (``_lock_keys``), even if a symlink changed since."""
        with self._lock_mutex:
            keys = self._lock_keys.pop(os.path.normpath(output_path), None)
        for key in keys if keys is not None else self._output_keys(output_path):
            self._release_key(key)

    def _release_key(self, normalized_path: str) -> None:
        """Let go of one identity: the flock first, then the memory record.

        The handle is taken out under the mutex, the lock file is unlinked and
        unlocked outside it (no I/O under the mutex), and only then is the
        identity dropped from ``_locks``: another job here sees the path as
        free only once the flock is actually gone (codex 4579986280).
        """
        with self._lock_mutex:
            if normalized_path not in self._locks:
                return
            lock_handle = self._lock_handles.pop(normalized_path, None)
        try:
            if lock_handle is not None:
                self._unlink_then_close(
                    lock_handle, self._lock_file_path(normalized_path), normalized_path,
                )
        finally:
            with self._lock_mutex:
                self._locks.discard(normalized_path)

    def acquire_dir_lock(self, dir_path: str) -> bool:
        """Acquire an exclusive lock over a directory and its entire subtree.

        Also fails, after taking it, when any process holds a lock on a path
        inside the folder or on a folder above it (see the note above
        ``acquire_lock``).
        """
        if not self._take_dir_lock(dir_path):
            return False
        if (
            self.locked_path_below(dir_path, include_self=False) is not None
            or self.folder_locked_above(dir_path) is not None
        ):
            self.release_dir_lock(dir_path)
            return False
        return True

    def _take_dir_lock(self, dir_path: str) -> bool:
        """The exclusive lock itself, with the in-process overlap checks.

        Used by a directory-input job (makeps3iso folder->iso) so any concurrent
        operation whose path falls inside the folder contends on this lock
        instead of mutating the tree while it is being packed. Fails when the
        subtree overlaps any existing file lock (a locked output inside it) or
        directory lock (in either nesting direction), or an external process
        already holds the directory's lock file.
        """
        if self.is_read_only:
            return False
        self._ensure_ready()
        # Store and compare directory locks resolved, so containment holds
        # through symlinks.
        normalized = self._resolve(dir_path)
        with self._lock_mutex:
            if self._dir_overlaps_locked(normalized):
                return False
            # Claimed in memory; the lock file I/O happens outside the mutex.
            self._dir_locks.add(normalized)

        taken = False
        lock_handle = None
        try:
            lock_file_path = self._lock_file_path(normalized)
            self._make_lock_dir(lock_file_path, normalized)
            with contextlib.ExitStack() as stack:
                lock_handle = stack.enter_context(open_lock_handle(lock_file_path))
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # Held by another process packing the same folder.
                    return False
                if not self._still_linked(lock_handle, lock_file_path):
                    # Unlinked by its last holder under us: a lock no one
                    # else can see. The job defers and tries again.
                    return False
                self._record_path(lock_handle, normalized)
                with self._lock_mutex:
                    self._dir_lock_handles[normalized] = lock_handle
                # Keep the handle open for the job lifecycle.
                stack.pop_all()
                taken = True
                return True
        except LockFailed:
            raise
        except Exception as exc:
            if lock_handle is not None:
                with self._lock_mutex:
                    self._dir_lock_handles.pop(normalized, None)
                with contextlib.suppress(Exception):
                    lock_handle.close()
            logger.error(
                "Failed to acquire dir lock for %s", normalized, exc_info=True,
            )
            raise LockFailed(normalized, exc) from exc
        finally:
            if not taken:
                with self._lock_mutex:
                    self._dir_locks.discard(normalized)

    def release_dir_lock(self, dir_path: str):
        """Release a directory subtree lock acquired via :meth:`acquire_dir_lock`."""
        normalized = self._resolve(dir_path)
        with self._lock_mutex:
            if normalized not in self._dir_locks:
                return
            handle = self._dir_lock_handles.pop(normalized, None)
        try:
            if handle is not None:  # the flock first, outside the mutex
                self._unlink_then_close(handle, self._lock_file_path(normalized), normalized)
        finally:
            with self._lock_mutex:  # then the memory record
                self._dir_locks.discard(normalized)

    # Input locks (#306). A job holds a *shared* flock on each file it reads
    # (its Source and the Source's companions) for as long as it runs. Readers
    # share it, so two jobs reading one Source never block each other; anything
    # that wants the path exclusively (an output lock, the stale-lock probe
    # behind ``check_file_status``) is refused while one is held. That probe is
    # what the web guard runs on a path before a rename or delete, so a file a
    # job in *another* process is reading answers 409 there. The lock file is
    # the same one an output lock on that path uses, and both rules above
    # hold: the last reader to let go upgrades to exclusive and unlinks while
    # holding it; an acquirer checks the file is still linked.

    def acquire_input_lock(self, path: str) -> bool:
        """Take a shared lock on a file a job reads. False when it can't be had:
        someone holds it exclusively (a job writing that path)."""
        if self.is_read_only:
            return False
        self._ensure_ready()
        normalized = os.path.normpath(path)
        key = self._resolve(path)  # the identity: the real path
        lock_file_path = self._lock_file_path(key)
        self._make_lock_dir(lock_file_path, key)
        handle = self._shared_lock(lock_file_path, key)
        if handle is None:
            return False
        self._record_path(handle, key)
        with self._lock_mutex:
            self._input_handles.setdefault(normalized, []).append((key, handle))
        if self.folder_locked_above(path) is not None:
            # A folder above is being packed: reading inside it is a conflict.
            self.release_input_lock(path)
            return False
        return True

    def release_input_lock(self, path: str) -> None:
        """Let go of one shared lock :meth:`acquire_input_lock` took on ``path``."""
        normalized = os.path.normpath(path)
        with self._lock_mutex:
            handles = self._input_handles.get(normalized)
            if not handles:
                return
            key, handle = handles.pop()  # the identity it was taken under
            if not handles:
                del self._input_handles[normalized]
        lock_file_path = self._lock_file_path(key)
        try:
            # The last reader removes the file. Upgrading fails while another
            # reader holds it, and that reader removes it in turn.
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pass
        else:
            with contextlib.suppress(OSError):
                self._unlink_held(handle, lock_file_path)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _shared_lock(self, lock_file_path: str, key: str, attempts: int = 50):
        """An open handle holding a shared flock on ``lock_file_path``, or None
        when a writer holds it. Raises :class:`LockFailed` when the lock file
        can't be made or locked at all.

        The stale-lock probe holds a lock file exclusively for a moment, so a
        refusal is retried briefly before it counts as a writer holding it.
        """
        for _attempt in range(attempts):
            try:
                handle = open_lock_handle(lock_file_path)
            except OSError as exc:
                logger.error("Failed to open input lock %s", lock_file_path, exc_info=True)
                raise LockFailed(key, exc) from exc
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                time.sleep(0.01)
                continue
            except OSError as exc:
                handle.close()
                logger.error("Failed to take input lock %s", lock_file_path, exc_info=True)
                raise LockFailed(key, exc) from exc
            if self._still_linked(handle, lock_file_path):
                return handle
            # Unlinked by its last holder under us: open the file there now.
            handle.close()
        return None

    def _unlink_then_close(self, lock_handle, lock_file_path: str, label: str) -> None:
        """Release a held lock: unlink its file while holding it, then let go."""
        try:
            self._unlink_held(lock_handle, lock_file_path)
        except Exception:
            logger.error("Failed to remove lock file %s", lock_file_path, exc_info=True)
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        except Exception:
            logger.error("Error releasing lock for %s", label, exc_info=True)

    # Referrer locks (codex 4577804154). A Referrer rewriting a file other
    # processes may rewrite too (one ``.m3u`` naming discs that a CLI run and
    # the server convert at once) holds this exclusive lock on the file's real
    # path for its whole read, rewrite and replace. Its own kind of lock file
    # (``referrer-``), so it never meets an output, input or folder lock: a
    # holder waits on nothing else while it holds one, and nothing that holds
    # an Image lock is refused by it. Both rules above hold here too.

    @contextlib.contextmanager
    def referrer_lock(self, path: str, *, wait: float, keep_waiting=None, poll: float = 0.05):
        """Hold the cross-process Referrer lock on ``path`` within the block.

        Polls without blocking for up to ``wait`` seconds while another
        process (or another handle here) holds it; ``keep_waiting``, when
        given, is called before every attempt and may raise to stop waiting.
        Raises :class:`LockWaitTimeout` when the wait runs out.
        """
        key = self._resolve(path)
        lock_file_path = self._lock_file_path(key, REFERRER_LOCK)
        handle = self._take_referrer_lock(lock_file_path, key, wait, keep_waiting, poll)
        try:
            yield
        finally:
            self._unlink_then_close(handle, lock_file_path, key)

    def _take_referrer_lock(
        self, lock_file_path: str, key: str, wait: float, keep_waiting, poll: float,
    ):
        if self.is_read_only:
            raise LockWaitTimeout(f"read-only: not locking {lock_file_path}")
        self._ensure_ready()
        self._make_lock_dir(lock_file_path, key)
        deadline = time.monotonic() + wait
        while True:
            if keep_waiting is not None:
                keep_waiting()
            try:
                handle = open_lock_handle(lock_file_path)
            except OSError as exc:
                raise LockFailed(key, exc) from exc
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
            except OSError as exc:
                handle.close()
                raise LockFailed(key, exc) from exc
            else:
                if self._still_linked(handle, lock_file_path):
                    return handle
                # Unlinked by its last holder under us: lock the file there now.
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                continue
            if time.monotonic() >= deadline:
                raise LockWaitTimeout(f"still held after {wait:g} s: {lock_file_path}")
            time.sleep(poll)

    @staticmethod
    def _make_lock_dir(lock_file_path: str, key: str) -> None:
        """The lock file's folder, or :class:`LockFailed` naming ``key``."""
        try:
            os.makedirs(os.path.dirname(lock_file_path), exist_ok=True)
        except OSError as exc:
            raise LockFailed(key, exc) from exc

    def stats(self) -> dict:
        with self._lock_mutex:
            return {"locks": len(self._locks), "dir_locks": len(self._dir_locks)}


# Lazy: importing this module touches nothing on disk.
lock_manager = LockManager(lazy=True)
