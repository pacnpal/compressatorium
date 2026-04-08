import contextlib
import fcntl
import hashlib
import logging
import os
import threading
from pathlib import Path

from config import settings

logger = logging.getLogger("chd.lock_manager")


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
        self._lock_mutex = threading.Lock()
        self._lock_handles = {}
        self._cleanup_stale_locks()

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
                    with open(lock_path, "a") as lock_handle:
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
            with open(lock_file_path, "a") as lock_handle:
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
                            logger.debug("Failed to release lock during staleness check")
            
            # If we successfully acquired the lock, the file is stale - remove it
            if acquired:
                try:
                    os.remove(lock_file_path)
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Removed stale lock file during check: %s", os.path.basename(lock_file_path))
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
            True if lock was acquired, False if already locked or CHD exists and overwrite is not allowed

        """
        normalized_path = os.path.normpath(output_path)

        with self._lock_mutex:
            # Check if already locked by another job
            if normalized_path in self._locks:
                return False

            # Try to create a lock file
            lock_file_path = self._lock_file_path(normalized_path)
            lock_dir = os.path.dirname(lock_file_path)
            if lock_dir:
                os.makedirs(lock_dir, exist_ok=True)
            lock_handle = None
            try:
                # Open lock file in append mode to avoid truncating existing content
                lock_handle = open(lock_file_path, "a")

                # Try to acquire an exclusive lock (non-blocking)
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # Lock is held by another process
                    lock_handle.close()
                    return False
                except OSError as e:
                    # Other error (permission denied, etc.)
                    lock_handle.close()
                    print(f"Failed to acquire lock for {normalized_path}: {e}")
                    return False

                # Now that we have the lock, check if CHD already exists (atomic with lock)
                if os.path.exists(normalized_path):
                    if not allow_existing or not os.path.isfile(normalized_path):
                        # File exists and overwrite not allowed (or not a file): release lock and clean up
                        try:
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            logger.debug("Failed to release lock for existing file: %s", normalized_path)
                        lock_handle.close()
                        lock_handle = None
                        # Clean up lock file since we're not using it
                        try:
                            if os.path.exists(lock_file_path):
                                os.remove(lock_file_path)
                        except Exception:
                            logger.debug("Failed to clean up lock file: %s", lock_file_path)
                        return False

                # Successfully acquired the lock and file doesn't exist
                self._locks.add(normalized_path)
                self._lock_handles[normalized_path] = lock_handle
                return True

            except Exception as e:
                # Ensure file handle is closed on any error
                if lock_handle is not None:
                    try:
                        lock_handle.close()
                    except Exception:
                        logger.debug("Failed to close lock handle for: %s", normalized_path)
                print(f"Failed to acquire lock for {normalized_path}: {e}")
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
                    except Exception as e:
                        print(f"Error releasing lock for {normalized_path}: {e}")
                    finally:
                        del self._lock_handles[normalized_path]

                    # Clean up the lock file
                    lock_file_path = self._lock_file_path(normalized_path)
                    try:
                        if os.path.exists(lock_file_path):
                            os.remove(lock_file_path)
                    except Exception as e:
                        print(f"Failed to remove lock file {lock_file_path}: {e}")

    def stats(self) -> dict:
        with self._lock_mutex:
            return {"locks": len(self._locks)}


lock_manager = LockManager()
