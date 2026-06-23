import asyncio
import fcntl
import json
import os
import time

from config import settings


class ConcurrencyManager:
    """Coordinates a global concurrency limit and FIFO ordering across processes."""

    def __init__(self, max_concurrent: int, lock_dir: str):
        self.max_concurrent = max(1, max_concurrent)
        self.lock_dir = lock_dir
        # Create lock directory with restrictive permissions (owner only)
        os.makedirs(self.lock_dir, mode=0o700, exist_ok=True)
        self._slot_paths = [
            os.path.join(self.lock_dir, f"convert_slot_{idx}.lock")
            for idx in range(self.max_concurrent)
        ]
        self._lock_handles: dict[str, tuple[int, object]] = {}
        self._ticket_handles: dict[str, tuple[int, str]] = {}
        self._ticket_counter_path = os.path.join(self.lock_dir, "queue_counter")
        self._cleanup_stale_locks()
        if not os.path.exists(self._ticket_counter_path):
            try:
                with open(self._ticket_counter_path, "w", encoding="utf-8") as fh:
                    fh.write("0")
            except OSError:
                pass
        # Per-reservation sequence: a uniqueness tiebreaker baked into each
        # ticket filename so two reservations never sort to the same slot.
        self._seq = 0
        # Seed the in-process fallback ticket above any value already on disk so
        # a later counter-read failure still issues strictly-increasing, in-range
        # tickets (see _next_ticket) rather than a wall-clock number.
        self._fallback_ticket = 0
        try:
            with open(self._ticket_counter_path, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
                self._fallback_ticket = int(raw) if raw else 0
        except (OSError, ValueError):
            self._fallback_ticket = 0

    def reserve_ticket(self, key: str) -> int:
        """Reserve a FIFO ticket for the job."""
        if key in self._ticket_handles:
            return self._ticket_handles[key][0]

        ticket = self._next_ticket()
        self._seq += 1
        seq = self._seq
        ticket_path = os.path.join(
            self.lock_dir, f"queue_{ticket}_{seq}_{key}.ticket"
        )
        self._create_ticket_file(ticket_path, ticket)
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

    async def acquire(
        self, key: str, cancel_event: asyncio.Event | None = None,
    ) -> bool:
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
                    # Binary mode avoids an "unspecified-encoding" lint warning; this
                    # handle is used only for fcntl.flock(), no text is read or written.
                    handle = open(path, "ab")
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

    async def _wait_for_turn(
        self, key: str, cancel_event: asyncio.Event | None = None,
    ) -> bool:
        ticket = self.reserve_ticket(key)
        while True:
            if cancel_event and cancel_event.is_set():
                return False
            queued_keys = self._list_tickets()
            if key not in queued_keys:
                self._restore_ticket_file(key, ticket)
                await asyncio.sleep(0.2)
                continue
            if key in queued_keys[: self.max_concurrent]:
                return True
            await asyncio.sleep(0.2)

    def _list_tickets(self) -> list[str]:
        """Return queued job keys in FIFO order.

        Ordered by ``(ticket, seq, key)``: the shared-counter ticket is the
        primary FIFO key, the per-reservation ``seq`` breaks ties if two tickets
        ever collide (e.g. a counter-file read fallback), and the unique job
        ``key`` is the final tiebreaker. Returning keys (not raw ticket ints)
        means a colliding ticket can never put two jobs in the same admitted
        prefix slot (issue #183, site 4).
        """
        entries: list[tuple[int, int, str]] = []
        try:
            for name in os.listdir(self.lock_dir):
                if not name.startswith("queue_") or not name.endswith(".ticket"):
                    continue
                core = name[len("queue_"):-len(".ticket")]
                parts = core.split("_", 2)
                if len(parts) < 3:
                    continue
                try:
                    ticket = int(parts[0])
                    seq = int(parts[1])
                except ValueError:
                    continue
                entries.append((ticket, seq, parts[2]))
        except OSError:
            return []
        entries.sort()
        return [key for _, _, key in entries]

    def _next_ticket(self) -> int:
        try:
            with open(self._ticket_counter_path, "r+", encoding="utf-8") as fh:
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
                # Track the high-water mark so the OSError fallback below stays
                # strictly increasing and in the same numeric range.
                self._fallback_ticket = max(self._fallback_ticket, next_ticket)
                return next_ticket
        except OSError:
            # Counter file unreadable/unwritable: issue a strictly-increasing
            # in-process ticket seeded above the last value seen on disk instead
            # of a wall-clock number that could collide or sort out of range and
            # mis-order FIFO admission (issue #183, site 4). MAX_CONCURRENT_JOBS
            # concurrency lives in one process, so this counter is authoritative.
            self._fallback_ticket += 1
            return self._fallback_ticket

    def _create_ticket_file(self, ticket_path: str, ticket: int) -> None:
        tmp_path = f"{ticket_path}.tmp"
        try:
            payload = {
                "ticket": ticket,
                "pid": os.getpid(),
                "created_at": time.time(),
            }
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, ticket_path)
        except OSError:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                # Best-effort cleanup: failure to delete the temp file is non-fatal.
                pass

    def _restore_ticket_file(self, key: str, ticket: int) -> None:
        info = self._ticket_handles.get(key)
        if not info:
            return
        _, ticket_path = info
        if os.path.exists(ticket_path):
            return
        self._create_ticket_file(ticket_path, ticket)
        self._ticket_handles[key] = (ticket, ticket_path)

    def _cleanup_stale_locks(self):
        """Remove stale queue tickets and slot locks from previous runs."""
        try:
            slot_handles = []
            slots_busy = False
            for path in self._slot_paths:
                try:
                    # Binary mode avoids an "unspecified-encoding" lint warning; this
                    # handle is used only for fcntl.flock(), no text is read or written.
                    handle = open(path, "ab")
                except OSError:
                    continue
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    slot_handles.append((path, handle))
                except BlockingIOError:
                    handle.close()
                    slots_busy = True
                    break
                except OSError:
                    handle.close()
                    continue

            if slots_busy:
                for _, handle in slot_handles:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        # Best-effort unlock; closing the file descriptor below will release the
                        # lock if still held.
                        pass
                    finally:
                        try:
                            handle.close()
                        except OSError:
                            # Best-effort close; ignore errors during cleanup
                            pass
                slot_handles = []

            try:
                for name in os.listdir(self.lock_dir):
                    if not (name.startswith("queue_") and name.endswith(".ticket")):
                        continue
                    ticket_path = os.path.join(self.lock_dir, name)
                    if not slots_busy:
                        try:
                            os.remove(ticket_path)
                        except OSError:
                            # Best-effort stale ticket cleanup; ignore if the file was already
                            # removed.
                            pass
                        continue
                    payload, legacy = self._load_ticket_payload(ticket_path)
                    if payload:
                        pid = payload.get("pid")
                        if isinstance(pid, int) and pid > 0:
                            if self._pid_is_alive(pid):
                                continue
                        else:
                            if legacy and self._ticket_locked(ticket_path):
                                continue
                            if not legacy:
                                continue
                        try:
                            os.remove(ticket_path)
                        except OSError:
                            # Best-effort stale ticket cleanup; ignore if the file was already
                            # removed by another process or cannot be deleted.
                            pass
                    elif legacy:
                        if self._ticket_locked(ticket_path):
                            continue
                        try:
                            os.remove(ticket_path)
                        except OSError:
                            # Best-effort stale ticket cleanup; ignore if the file was already
                            # removed.
                            pass
            finally:
                for path, handle in slot_handles:
                    if not slots_busy:
                        try:
                            if os.path.exists(path):
                                os.remove(path)
                        except OSError:
                            # Best-effort cleanup: ignore failures to remove temporary slot lock
                            # files.
                            pass
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        # Best-effort unlock; closing the file descriptor below will release the
                        # lock if still held.
                        pass
                    finally:
                        try:
                            handle.close()
                        except OSError:
                            # Best-effort cleanup: ignore errors when closing the handle
                            pass
        except OSError:
            # Best-effort cleanup: ignore top-level errors during stale ticket cleanup
            pass

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _load_ticket_payload(self, ticket_path: str) -> tuple[dict | None, bool]:
        try:
            with open(ticket_path, encoding="utf-8") as fh:
                content = fh.read().strip()
        except OSError:
            return None, False
        if not content:
            return None, False
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                return payload, False
        except json.JSONDecodeError:
            # If JSON parsing fails, fall back to interpreting legacy numeric ticket format below.
            pass
        if content.isdigit():
            return {"ticket": int(content)}, True
        return None, False

    @staticmethod
    def _ticket_locked(ticket_path: str) -> bool:
        try:
            with open(ticket_path, "ab") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    return True
        except OSError:
            return True
        return False

    def stats(self) -> dict:
        return {"tickets": len(self._list_tickets()), "slots": len(self._slot_paths)}


concurrency_manager = ConcurrencyManager(
    settings.max_concurrent_jobs, settings.concurrency_lock_dir,
)
