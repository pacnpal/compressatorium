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
from pathlib import Path

from config import settings

logger = get_logger("lock_manager")


class LockManager:
    """Manages file locks to prevent concurrent conversions of the same file."""

    def __init__(self):
        self._lock_dir = (
            settings.concurrency_lock_dir
            or str(Path(settings.data_dir) / "locks")
        )
        # Create lock directory with restrictive permissions (owner only)
        # to address security concerns about predictable temp directories
        os.makedirs(self._lock_dir, mode=0o700, exist_ok=True)
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
        self._cleanup_stale_locks()

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
        if any(self._path_within(self._resolve(p), resolved) for p in self._locks):
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
        with self._lock_mutex:
            if not self._dir_locks:
                return False
            return self._within_locked_dir_locked(self._resolve(path))

    def dir_lock_would_conflict(self, dir_path: str) -> bool:
        """Whether :meth:`acquire_dir_lock` for ``dir_path`` would currently
        conflict with an existing lock (without acquiring anything)."""
        resolved = self._resolve(dir_path)
        with self._lock_mutex:
            return self._dir_overlaps_locked(resolved)

    def _lock_file_path(self, normalized_path: str) -> str:
        # Use a stable hash to avoid path length issues and keep locks in /tmp.
        digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()
        base = os.path.basename(normalized_path) or "chd"
        safe_base = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in base)
        filename = f"filelock-{safe_base}-{digest}.lock"
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
                    if name.startswith("filelock-") and name.endswith(".lock")
                ]
            except OSError:
                return removed_count

            for name in lock_files:
                lock_path = os.path.join(self._lock_dir, name)
                try:
                    # Try to acquire the lock to see if it's stale
                    # Binary mode: flock-only handle, no text is read or written.
                    with open(lock_path, "ab") as lock_handle:
                        acquired = False
                        try:
                            fcntl.flock(
                                lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB,
                            )
                            acquired = True
                        except BlockingIOError:
                            # Lock is held by another process; keep the file.
                            continue
                        except OSError:
                            # Error trying to lock; skip this file
                            continue
                        finally:
                            if acquired:
                                with contextlib.suppress(Exception):
                                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

                    # Lock was acquired successfully, meaning no other process holds it
                    # This is a stale lock file - remove it
                    try:
                        os.remove(lock_path)
                        removed_count += 1
                        if log_level == "info":
                            logger.info("Removed stale lock file: %s", name)
                        else:
                            logger.debug("Removed stale lock file: %s", name)
                    except OSError as e:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("Failed to remove stale lock %s: %s", name, e)
                except OSError:
                    # Error opening/checking the lock file; skip it
                    pass
        except Exception as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Error during stale lock cleanup: %s", e)

        return removed_count

    def cleanup_stale_locks_periodic(self) -> int:
        """Public method for periodic cleanup from background tasks.

        Returns:
            Number of stale locks removed
        """
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
        with self._lock_mutex:
            is_locked = normalized_path in self._locks
            # Resolve symlinks only when a directory subtree lock is actually
            # held (the uncommon case), to keep the hot listing path cheap.
            if not is_locked and self._dir_locks:
                is_locked = self._within_locked_dir_locked(self._resolve(output_path))
            file_exists = os.path.isfile(normalized_path)

        if is_locked:
            return (file_exists, True)

        return (file_exists, self._check_external_lock(normalized_path))

    def _check_external_lock(self, normalized_path: str) -> bool:
        """Check if an external process holds a lock on the output file.

        Returns:
            True if the file is locked by another process, False otherwise.

        As a side effect, if a lock file exists but is not actively held, it will be removed.
        """
        lock_file_path = self._lock_file_path(normalized_path)
        if not os.path.exists(lock_file_path):
            return False

        try:
            with open(lock_file_path, "ab") as lock_handle:
                acquired = False
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    # Lock is actively held by another process
                    return True
                except OSError:
                    # Error acquiring lock; assume not locked
                    return False
                finally:
                    if acquired:
                        try:
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            logger.debug(
                                "Failed to release lock during staleness check", exc_info=True
                            )

            # If we successfully acquired the lock, the file is stale - remove it
            if acquired:
                try:
                    os.remove(lock_file_path)
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Removed stale lock file during check: %s",
                            os.path.basename(lock_file_path),
                        )
                except OSError as e:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Failed to remove stale lock file during check: %s (%s)",
                            os.path.basename(lock_file_path),
                            e,
                        )

            return False
        except Exception:
            return False

    def acquire_lock(self, output_path: str, *, allow_existing: bool = False) -> bool:
        """Acquire a lock for the output file path.

        Returns:
            True if lock was acquired, False if already locked or CHD exists and overwrite is
            not allowed

        """
        normalized_path = os.path.normpath(output_path)

        with self._lock_mutex:
            # Check if already locked by another job
            if normalized_path in self._locks:
                return False

            # Reject a target that lives inside a directory subtree another job
            # has locked (makeps3iso packing the folder): the per-file job
            # contends here just like an exact output collision. Resolve
            # symlinks so a symlinked path into the folder can't slip through.
            if self._dir_locks and self._within_locked_dir_locked(
                self._resolve(output_path)
            ):
                return False

            # Try to create a lock file
            lock_file_path = self._lock_file_path(normalized_path)
            lock_dir = os.path.dirname(lock_file_path)
            if lock_dir:
                os.makedirs(lock_dir, exist_ok=True)
            lock_handle = None
            try:
                with contextlib.ExitStack() as stack:
                    # Open lock file in append mode (binary) to avoid truncating existing content.
                    # Binary mode: flock-only handle, no text is read or written.
                    lock_handle = stack.enter_context(
                        open(lock_file_path, "ab")
                    )

                    # Try to acquire an exclusive lock (non-blocking)
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        # Lock is held by another process
                        return False
                    except OSError:
                        # Other error (permission denied, etc.)
                        logger.error("Failed to acquire lock for %s", normalized_path, exc_info=True)
                        return False

                    # Now that we have the lock, check if CHD already exists (atomic with lock)
                    if os.path.exists(normalized_path):
                        if not allow_existing or not os.path.isfile(normalized_path):
                            # File exists and overwrite not allowed (or not a file): release lock and
                            # clean up
                            try:
                                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                            except Exception:
                                logger.debug(
                                    "Failed to release lock for existing file: %s",
                                    normalized_path,
                                    exc_info=True,
                                )
                            # Clean up lock file since we're not using it
                            try:
                                if os.path.exists(lock_file_path):
                                    os.remove(lock_file_path)
                            except Exception:
                                logger.debug(
                                    "Failed to clean up lock file: %s",
                                    lock_file_path,
                                    exc_info=True,
                                )
                            return False

                    # Successfully acquired the lock and file doesn't exist
                    self._locks.add(normalized_path)
                    self._lock_handles[normalized_path] = lock_handle
                    # Transfer ownership to _lock_handles by detaching ExitStack cleanup.
                    # This keeps lock_handle open for the conversion lifecycle.
                    stack.pop_all()
                    return True

            except Exception:
                # Ensure file handle is closed on any error
                if lock_handle is not None:
                    if self._lock_handles.get(normalized_path) is lock_handle:
                        self._lock_handles.pop(normalized_path, None)
                        self._locks.discard(normalized_path)
                    try:
                        lock_handle.close()
                    except Exception:
                        logger.debug(
                            "Failed to close lock handle for: %s",
                            normalized_path,
                            exc_info=True,
                        )
                logger.error("Failed to acquire lock for %s", normalized_path, exc_info=True)
                return False

    def release_lock(self, output_path: str):
        """Release the lock for an output file path."""
        normalized_path = os.path.normpath(output_path)

        with self._lock_mutex:
            if normalized_path in self._locks:
                self._locks.remove(normalized_path)

                # Release and close the lock file handle
                if normalized_path in self._lock_handles:
                    lock_handle = self._lock_handles[normalized_path]
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                        lock_handle.close()
                    except Exception:
                        logger.error("Error releasing lock for %s", normalized_path, exc_info=True)
                    finally:
                        del self._lock_handles[normalized_path]

                    # Clean up the lock file
                    lock_file_path = self._lock_file_path(normalized_path)
                    try:
                        if os.path.exists(lock_file_path):
                            os.remove(lock_file_path)
                    except Exception:
                        logger.error("Failed to remove lock file %s", lock_file_path, exc_info=True)

    def acquire_dir_lock(self, dir_path: str) -> bool:
        """Acquire an exclusive lock over a directory and its entire subtree.

        Used by a directory-input job (makeps3iso folder->iso) so any concurrent
        operation whose path falls inside the folder contends on this lock
        instead of mutating the tree while it is being packed. Fails when the
        subtree overlaps any existing file lock (a locked output inside it) or
        directory lock (in either nesting direction), or an external process
        already holds the directory's lock file.
        """
        # Store and compare directory locks resolved, so containment holds
        # through symlinks.
        normalized = self._resolve(dir_path)
        with self._lock_mutex:
            if self._dir_overlaps_locked(normalized):
                return False

            lock_file_path = self._lock_file_path(normalized)
            lock_dir = os.path.dirname(lock_file_path)
            if lock_dir:
                os.makedirs(lock_dir, exist_ok=True)
            lock_handle = None
            try:
                with contextlib.ExitStack() as stack:
                    # Binary mode: flock-only handle, no text is read or written.
                    lock_handle = stack.enter_context(open(lock_file_path, "ab"))
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        # Held by another process packing the same folder.
                        return False
                    except OSError:
                        logger.error(
                            "Failed to acquire dir lock for %s", normalized, exc_info=True,
                        )
                        return False
                    self._dir_locks.add(normalized)
                    self._dir_lock_handles[normalized] = lock_handle
                    # Keep the handle open for the job lifecycle.
                    stack.pop_all()
                    return True
            except Exception:
                if lock_handle is not None:
                    self._dir_lock_handles.pop(normalized, None)
                    self._dir_locks.discard(normalized)
                    with contextlib.suppress(Exception):
                        lock_handle.close()
                logger.error(
                    "Failed to acquire dir lock for %s", normalized, exc_info=True,
                )
                return False

    def release_dir_lock(self, dir_path: str):
        """Release a directory subtree lock acquired via :meth:`acquire_dir_lock`."""
        normalized = self._resolve(dir_path)
        with self._lock_mutex:
            if normalized not in self._dir_locks:
                return
            self._dir_locks.remove(normalized)
            handle = self._dir_lock_handles.pop(normalized, None)
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                except Exception:
                    logger.error(
                        "Error releasing dir lock for %s", normalized, exc_info=True,
                    )
                lock_file_path = self._lock_file_path(normalized)
                try:
                    if os.path.exists(lock_file_path):
                        os.remove(lock_file_path)
                except Exception:
                    logger.error(
                        "Failed to remove dir lock file %s", lock_file_path, exc_info=True,
                    )

    def stats(self) -> dict:
        with self._lock_mutex:
            return {"locks": len(self._locks), "dir_locks": len(self._dir_locks)}


lock_manager = LockManager()
