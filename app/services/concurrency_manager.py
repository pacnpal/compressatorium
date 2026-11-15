import asyncio
import contextlib
import fcntl
import json
import os
import time

from config import settings
from services.lock_manager import unlink_held

# How often one process sweeps the queue for tickets whose holder died.
_PRUNE_INTERVAL = 1.0


class ConcurrencyManager:
    """Coordinates a global concurrency limit and FIFO ordering across processes.

    Everything lives in the shared lock directory (``<data dir>/locks`` by
    default), so the server and any CLI process, in this container or another
    that mounts the same ``/config``, share one queue (#306):

    * **Slots** are ``convert_slot_<i>.lock``, held by ``flock`` while a job
      runs. They are a fixed set and are never unlinked, so a slot lock is
      always on the file every process opens.
    * **Tickets** are ``queue_<ticket>_<seq>_<key>.ticket``, one per queued job,
      in FIFO order. The holder keeps an ``flock`` on its ticket for the job's
      whole life and unlinks it while still holding it. A ticket nobody holds
      belongs to a process that died without cleanup (SIGKILL, SIGHUP, the OOM
      killer): the kernel dropped its lock, in any container, so any waiter that
      can flock it removes it (``_prune_dead_tickets``). PIDs are not used: they
      mean nothing across PID namespaces.
    """

    def __init__(self, max_concurrent: int, lock_dir: str, *, lazy: bool = False):
        self.max_concurrent = max(1, max_concurrent)
        self.lock_dir = lock_dir
        self._slot_paths = [
            os.path.join(self.lock_dir, f"convert_slot_{idx}.lock")
            for idx in range(self.max_concurrent)
        ]
        self._lock_handles: dict[str, tuple[int, object]] = {}
        # key -> (ticket, path, open handle holding the ticket's flock or None)
        self._ticket_handles: dict[str, tuple[int, str, object]] = {}
        self._ticket_counter_path = os.path.join(self.lock_dir, "queue_counter")
        # Per-reservation sequence: a uniqueness tiebreaker baked into each
        # ticket filename so two reservations never sort to the same slot.
        self._seq = 0
        self._fallback_ticket = 0
        self._last_prune = 0.0
        # A lazy manager (the module singleton) touches the lock directory on
        # first use, not at import, so a process that only plans (a CLI dry run)
        # creates, prunes and removes nothing.
        self._ready = False
        if not lazy:
            self._ensure_ready()

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        # Create lock directory with restrictive permissions (owner only)
        os.makedirs(self.lock_dir, mode=0o700, exist_ok=True)
        self._ready = True
        self._prune_dead_tickets(force=True)
        if not os.path.exists(self._ticket_counter_path):
            try:
                with open(self._ticket_counter_path, "w", encoding="utf-8") as fh:
                    fh.write("0")
            except OSError:
                pass
        # Seed the in-process fallback ticket above any value already on disk
        # AND above any live ticket file, so a later counter-read failure -- or a
        # missing/corrupt counter at startup while other work is queued -- still
        # issues strictly-increasing tickets that sort after older queued work
        # (see _next_ticket).
        self._fallback_ticket = max(self._fallback_ticket, self._highest_known_ticket())

    def reserve_ticket(self, key: str) -> int:
        """Reserve a FIFO ticket for the job, held under its flock until released."""
        self._ensure_ready()
        if key in self._ticket_handles:
            return self._ticket_handles[key][0]

        ticket = self._next_ticket()
        self._seq += 1
        seq = self._seq
        ticket_path = os.path.join(
            self.lock_dir, f"queue_{ticket}_{seq}_{key}.ticket"
        )
        handle = self._create_ticket_file(ticket_path, ticket)
        self._ticket_handles[key] = (ticket, ticket_path, handle)
        return ticket

    def release_ticket(self, key: str):
        """Release the FIFO ticket for the job: unlink it while holding it."""
        info = self._ticket_handles.pop(key, None)
        if not info:
            return
        _, ticket_path, handle = info
        self._drop(handle, ticket_path)

    @staticmethod
    def _drop(handle, ticket_path: str) -> None:
        if handle is None:
            return
        try:
            unlink_held(handle, ticket_path)
        except OSError:
            pass
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()

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
                    handle = open(path, "ab")  # noqa: SIM115,ASYNC230 - held for the slot
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
            self._prune_dead_tickets()
            queued_keys = self._list_tickets()
            if key not in queued_keys:
                self._restore_ticket_file(key, ticket)
                await asyncio.sleep(0.2)
                continue
            if key in queued_keys[: self.max_concurrent]:
                return True
            await asyncio.sleep(0.2)

    def _prune_dead_tickets(self, *, force: bool = False) -> int:
        """Remove tickets no process holds; their holders died without cleanup.

        A live holder keeps its ticket's flock, so a ticket this process can
        flock is dead. It is unlinked while held (``unlink_held``), so a ticket
        being re-created at the same path is never removed. Throttled to once
        per ``_PRUNE_INTERVAL`` unless ``force``. Returns how many went.
        """
        now = time.monotonic()
        if not force and now - self._last_prune < _PRUNE_INTERVAL:
            return 0
        self._last_prune = now
        own = {info[1] for info in self._ticket_handles.values()}
        removed = 0
        try:
            names = os.listdir(self.lock_dir)
        except OSError:
            return 0
        for name in names:
            if not (name.startswith("queue_") and name.endswith(".ticket")):
                continue
            path = os.path.join(self.lock_dir, name)
            if path in own:
                continue
            try:
                # Binary mode: flock-only handle, no text is read or written.
                with open(path, "ab") as handle:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        continue  # held: its owner is alive
                    try:
                        if unlink_held(handle, path):
                            removed += 1
                    finally:
                        with contextlib.suppress(OSError):
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                continue
        return removed

    def _list_tickets(self) -> list[str]:
        """Return queued job keys in FIFO order.

        Ordered by ``(ticket, seq, key)``: the shared-counter ticket is the
        primary FIFO key, the per-reservation ``seq`` breaks ties if two tickets
        ever collide (e.g. a counter-file read fallback), and the unique job
        ``key`` is the final tiebreaker. Returning keys (not raw ticket ints)
        means a colliding ticket can never put two jobs in the same admitted
        prefix slot (issue #183, site 4).

        Both the current ``queue_<ticket>_<seq>_<key>.ticket`` shape and the
        previous ``queue_<ticket>_<key>.ticket`` shape are parsed, so a rolling
        restart that leaves a still-active legacy ticket on disk keeps its FIFO
        position (legacy tickets predate the upgrade, so they sort ahead of new
        ones at the same ticket via ``seq = 0``).
        """
        entries: list[tuple[int, int, str]] = []
        try:
            for name in os.listdir(self.lock_dir):
                if not name.startswith("queue_") or not name.endswith(".ticket"):
                    continue
                core = name[len("queue_"):-len(".ticket")]
                parts = core.split("_", 2)
                if len(parts) < 2:
                    continue
                try:
                    ticket = int(parts[0])
                except ValueError:
                    continue
                if len(parts) >= 3 and parts[1].isdigit():
                    seq, key = int(parts[1]), parts[2]
                else:
                    # Legacy pre-seq filename: reserved before the upgrade, so
                    # sort it ahead of new (seq >= 1) tickets at the same ticket.
                    seq, key = 0, "_".join(parts[1:])
                entries.append((ticket, seq, key))
        except OSError:
            return []
        entries.sort()
        return [key for _, _, key in entries]

    def _highest_known_ticket(self) -> int:
        """Highest ticket number across the on-disk counter and any live ticket
        files, so the in-process fallback resumes above existing queued work even
        when the counter is missing/corrupt (e.g. a busy-slot restart that
        preserved older, higher-numbered tickets)."""
        highest = 0
        try:
            with open(self._ticket_counter_path, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
                if raw:
                    highest = int(raw)
        except (OSError, ValueError):
            highest = 0
        try:
            for name in os.listdir(self.lock_dir):
                if not name.startswith("queue_") or not name.endswith(".ticket"):
                    continue
                first = name[len("queue_"):-len(".ticket")].split("_", 1)[0]
                if first.isdigit():
                    highest = max(highest, int(first))
        except OSError:
            pass
        return highest

    def _next_ticket(self) -> int:
        try:
            with open(self._ticket_counter_path, "r+", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                raw = fh.read().strip()
                try:
                    on_disk = int(raw) if raw else 0
                except ValueError:
                    # Corrupt body (a partial write -> non-numeric content):
                    # recover the high-water mark from the preserved ticket files
                    # and let the truncate/write below REPAIR the counter in place
                    # under the lock, so every process converges back on the
                    # shared counter instead of each falling into its own
                    # in-process counter (which would collide across processes).
                    on_disk = self._highest_known_ticket()
                # Resume above any tickets the in-process fallback handed out
                # while the counter was transiently unavailable: a recovered
                # counter still holds its pre-failure value, so without this it
                # would reissue a number the fallback already used. That
                # collision would put two jobs on the same ticket in
                # _list_tickets and disagree with job_manager's (ticket, job_id)
                # PriorityQueue order, stalling the queue at MAX_CONCURRENT_JOBS=1.
                current = max(on_disk, self._fallback_ticket)
                next_ticket = current + 1
                fh.seek(0)
                fh.truncate()
                fh.write(str(next_ticket))
                fh.flush()
                os.fsync(fh.fileno())
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                self._fallback_ticket = next_ticket
                return next_ticket
        except OSError:
            # Counter file unreadable/unwritable (the locked write path can't
            # complete): issue a strictly-increasing in-process ticket seeded
            # above the last value seen on disk instead of a wall-clock number
            # that could collide or sort out of range and mis-order FIFO
            # admission (issue #183, site 4).
            self._fallback_ticket += 1
            return self._fallback_ticket

    def _create_ticket_file(self, ticket_path: str, ticket: int):
        """Write the ticket and return its open handle, already flock-held.

        Written under a private temp name, locked, then renamed into place, so
        the ticket is never visible unlocked: a pruner could otherwise take it
        for a dead one. None when it could not be written; ``_wait_for_turn``
        writes it again.
        """
        tmp_path = f"{ticket_path}.{os.getpid()}.tmp"
        handle = None
        try:
            payload = {
                "ticket": ticket,
                "pid": os.getpid(),
                "created_at": time.time(),
            }
            handle = open(tmp_path, "w", encoding="utf-8")  # noqa: SIM115 - held while queued
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.write(json.dumps(payload))
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(tmp_path, ticket_path)
            return handle
        except OSError:
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                # Best-effort cleanup: failure to delete the temp file is non-fatal.
                pass
            return None

    def _restore_ticket_file(self, key: str, ticket: int) -> None:
        info = self._ticket_handles.get(key)
        if not info:
            return
        _, ticket_path, handle = info
        if handle is not None and os.path.exists(ticket_path):
            return
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()
        self._ticket_handles[key] = (
            ticket, ticket_path, self._create_ticket_file(ticket_path, ticket),
        )

    def stats(self) -> dict:
        return {"tickets": len(self._list_tickets()), "slots": len(self._slot_paths)}



# Lazy: importing this module touches nothing on disk.
concurrency_manager = ConcurrencyManager(
    settings.max_concurrent_jobs, settings.concurrency_lock_dir, lazy=True,
)
