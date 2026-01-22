import fcntl
import os
from typing import Set
import threading


class LockManager:
    """Manages file locks to prevent concurrent conversions of the same file."""

    def __init__(self):
        self._locks: Set[str] = set()
        self._lock_mutex = threading.Lock()
        self._lock_handles = {}

    def is_locked(self, output_path: str) -> bool:
        """Check if an output file is currently locked (being converted)."""
        _, is_locked = self.check_file_status(output_path)
        return is_locked

    def check_file_status(self, output_path: str) -> tuple[bool, bool]:
        """
        Check the status of an output file atomically.

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
        lock_file_path = f"{normalized_path}.lock"
        if not os.path.exists(lock_file_path):
            return False

        try:
            with open(lock_file_path, "a") as lock_handle:
                acquired = False
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    return True
                except (IOError, OSError):
                    return False
                finally:
                    if acquired:
                        try:
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            pass
        except Exception:
            return False

        return False

    def acquire_lock(self, output_path: str, *, allow_existing: bool = False) -> bool:
        """
        Acquire a lock for the output file path.

        Returns:
            True if lock was acquired, False if already locked or CHD exists and overwrite is not allowed
        """
        normalized_path = os.path.normpath(output_path)

        with self._lock_mutex:
            # Check if already locked by another job
            if normalized_path in self._locks:
                return False

            # Try to create a lock file
            lock_file_path = f"{normalized_path}.lock"
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
                except (IOError, OSError) as e:
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
                            pass
                        lock_handle.close()
                        lock_handle = None
                        # Clean up lock file since we're not using it
                        try:
                            if os.path.exists(lock_file_path):
                                os.remove(lock_file_path)
                        except Exception:
                            pass
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
                        pass
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
                    lock_file_path = f"{normalized_path}.lock"
                    try:
                        if os.path.exists(lock_file_path):
                            os.remove(lock_file_path)
                    except Exception as e:
                        print(f"Failed to remove lock file {lock_file_path}: {e}")

    def stats(self) -> dict:
        with self._lock_mutex:
            return {"locks": len(self._locks)}


lock_manager = LockManager()
