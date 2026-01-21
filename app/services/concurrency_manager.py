import asyncio
import fcntl
import os
from typing import Dict, Optional, Tuple

from app.config import settings


class ConcurrencyManager:
    """Coordinates a global concurrency limit across processes."""

    def __init__(self, max_concurrent: int, lock_dir: str):
        self.max_concurrent = max(1, max_concurrent)
        self.lock_dir = lock_dir
        os.makedirs(self.lock_dir, exist_ok=True)
        self._slot_paths = [
            os.path.join(self.lock_dir, f"convert_slot_{idx}.lock")
            for idx in range(self.max_concurrent)
        ]
        self._lock_handles: Dict[str, Tuple[int, object]] = {}

    async def acquire(self, key: str, cancel_event: Optional[asyncio.Event] = None) -> bool:
        """Acquire a global concurrency slot. Returns False if cancelled."""
        while True:
            if cancel_event and cancel_event.is_set():
                return False
            for idx, path in enumerate(self._slot_paths):
                try:
                    handle = open(path, "a")
                except OSError:
                    continue
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._lock_handles[key] = (idx, handle)
                    return True
                except BlockingIOError:
                    handle.close()
                    continue
                except OSError:
                    handle.close()
                    continue

            await asyncio.sleep(0.2)

    def release(self, key: str):
        """Release a previously acquired slot."""
        info = self._lock_handles.pop(key, None)
        if not info:
            return
        _, handle = info
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass


concurrency_manager = ConcurrencyManager(
    settings.max_concurrent_jobs,
    settings.concurrency_lock_dir
)
