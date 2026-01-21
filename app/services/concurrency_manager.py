import asyncio
import fcntl
import os
from typing import Dict, Optional, Tuple

from app.config import settings


class ConcurrencyManager:
    """Coordinates a global concurrency limit and FIFO ordering across processes."""

    def __init__(self, max_concurrent: int, lock_dir: str):
        self.max_concurrent = max(1, max_concurrent)
        self.lock_dir = lock_dir
        os.makedirs(self.lock_dir, exist_ok=True)
        self._slot_paths = [
            os.path.join(self.lock_dir, f"convert_slot_{idx}.lock")
            for idx in range(self.max_concurrent)
        ]
        self._lock_handles: Dict[str, Tuple[int, object]] = {}
        self._ticket_handles: Dict[str, Tuple[int, str]] = {}
        self._ticket_counter_path = os.path.join(self.lock_dir, "queue_counter")
        if not os.path.exists(self._ticket_counter_path):
            try:
                with open(self._ticket_counter_path, "w") as fh:
                    fh.write("0")
            except OSError:
                pass

    def reserve_ticket(self, key: str) -> int:
        """Reserve a FIFO ticket for the job."""
        if key in self._ticket_handles:
            return self._ticket_handles[key][0]

        ticket = self._next_ticket()
        ticket_path = os.path.join(self.lock_dir, f"queue_{ticket}_{key}.ticket")
        try:
            with open(ticket_path, "w") as fh:
                fh.write(str(ticket))
        except OSError:
            pass
        self._ticket_handles[key] = (ticket, ticket_path)
        return ticket

    def release_ticket(self, key: str):
        """Release the FIFO ticket for the job."""
        info = self._ticket_handles.pop(key, None)
        if not info:
            return
        _, ticket_path = info
        try:
            if os.path.exists(ticket_path):
                os.remove(ticket_path)
        except OSError:
            pass

    async def acquire(self, key: str, cancel_event: Optional[asyncio.Event] = None) -> bool:
        """Acquire a global concurrency slot in FIFO order. Returns False if cancelled."""
        self.reserve_ticket(key)
        has_turn = await self._wait_for_turn(key, cancel_event=cancel_event)
        if not has_turn:
            return False

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
        """Release a previously acquired slot and ticket."""
        info = self._lock_handles.pop(key, None)
        if not info:
            self.release_ticket(key)
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
        self.release_ticket(key)

    async def _wait_for_turn(self, key: str, cancel_event: Optional[asyncio.Event] = None) -> bool:
        ticket = self.reserve_ticket(key)
        while True:
            if cancel_event and cancel_event.is_set():
                return False
            tickets = self._list_tickets()
            if ticket in tickets[:self.max_concurrent]:
                return True
            await asyncio.sleep(0.2)

    def _list_tickets(self) -> list[int]:
        tickets = []
        try:
            for name in os.listdir(self.lock_dir):
                if not name.startswith("queue_") or not name.endswith(".ticket"):
                    continue
                parts = name.split("_", 2)
                if len(parts) < 3:
                    continue
                try:
                    ticket = int(parts[1])
                except ValueError:
                    continue
                tickets.append(ticket)
        except OSError:
            return []
        tickets.sort()
        return tickets

    def _next_ticket(self) -> int:
        try:
            with open(self._ticket_counter_path, "r+") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                raw = fh.read().strip()
                current = int(raw) if raw else 0
                next_ticket = current + 1
                fh.seek(0)
                fh.truncate()
                fh.write(str(next_ticket))
                fh.flush()
                os.fsync(fh.fileno())
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                return next_ticket
        except OSError:
            return int(os.times().elapsed * 1000)


concurrency_manager = ConcurrencyManager(
    settings.max_concurrent_jobs,
    settings.concurrency_lock_dir
)
