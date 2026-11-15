import asyncio
import contextlib
import fcntl
import json
import os
import re
import time
import uuid

from config import settings
from logging_setup import get_logger
from services.lock_manager import open_lock_handle, still_linked, unlink_held

logger = get_logger("concurrency_manager")

# How often one process sweeps the queue for tickets whose holder died.
_PRUNE_INTERVAL = 1.0
# How often an unreadable queue is reported while a waiter retries.
_UNREADABLE_LOG_INTERVAL = 30.0
# The shared counter is retried this many times before the in-process fallback.
_COUNTER_ATTEMPTS = 3

_HOLDER_TOKEN = re.compile(r"h[0-9a-f]{12}")


class ConcurrencyManager:
    """Coordinates a global concurrency limit and FIFO ordering across processes.

    Everything lives in the shared lock directory (``<data dir>/locks`` by
    default), so the server and any CLI process, in this container or another
    that mounts the same ``/config``, share one queue (#306):

    * **Slots** are ``convert_slot_<i>.lock``, held by ``flock`` while a job
      runs. They are a fixed set and are never unlinked, so a slot lock is
      always on the file every process opens.
    * **Holders.** Each process creates one ``holder_<id>.lock`` on first use
      and keeps an ``flock`` on it for its whole life: one open file per
      process, however many jobs it queues.
    * **Tickets** are plain files, ``queue_<ticket>_<seq>_<holder>_<key>.ticket``,
      one per queued job, in FIFO order, naming their holder. A holder whose
      lock file can be locked (or is gone) is dead -- it was killed without
      cleanup (SIGKILL, SIGHUP, the OOM killer), and the kernel dropped its lock
      in any container -- so any waiter removes its tickets, then its holder
      file, while holding that lock (``_prune_dead_tickets``). PIDs are not
      used: they mean nothing across PID namespaces.
    """

    def __init__(self, max_concurrent: int, lock_dir: str, *, lazy: bool = False):
        self.max_concurrent = max(1, max_concurrent)
        self.lock_dir = lock_dir
        self._slot_paths = [
            os.path.join(self.lock_dir, f"convert_slot_{idx}.lock")
            for idx in range(self.max_concurrent)
        ]
        self._lock_handles: dict[str, tuple[int, object]] = {}
        # key -> (ticket, ticket file path)
        self._ticket_handles: dict[str, tuple[int, str]] = {}
        self._ticket_counter_path = os.path.join(self.lock_dir, "queue_counter")
        self._holder_id = f"h{uuid.uuid4().hex[:12]}"
        self._holder_path = os.path.join(self.lock_dir, f"holder_{self._holder_id}.lock")
        self._holder_handle = None
        # Per-reservation sequence: a uniqueness tiebreaker baked into each
        # ticket filename so two reservations never sort to the same slot.
        self._seq = 0
        self._fallback_ticket = 0
        self._last_prune = 0.0
        self._last_unreadable_log = 0.0
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
        with contextlib.suppress(OSError):
            self._ensure_holder()
        try:
            self._prune_dead_tickets(force=True)
        except OSError:
            logger.warning("Could not sweep %s for dead tickets", self.lock_dir, exc_info=True)
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

    def _ensure_holder(self) -> None:
        """Hold this process's ``holder_<id>.lock`` (created once, kept open).

        Written under a temp name, locked, then renamed into place, so it is
        never visible unlocked. Raises OSError when it cannot be made; callers
        retry on their next poll.

        If the file was deleted from outside (a tmp cleaner, a user), other
        processes would read this process as dead and prune its tickets. So
        the held handle is checked against the path on every call (each wait
        poll makes one); when it no longer names the held file, the holder is
        made again under the same id and every ticket this process holds is
        written back.
        """
        if self._holder_handle is not None:
            if still_linked(self._holder_handle, self._holder_path):
                return
            with contextlib.suppress(OSError):
                self._holder_handle.close()
            self._holder_handle = None
            self._make_holder()
            logger.warning("Holder file %s was removed; made it again", self._holder_path)
            for key, (ticket, _path) in list(self._ticket_handles.items()):
                self._restore_ticket_file(key, ticket)
            return
        self._make_holder()

    def _make_holder(self) -> None:
        tmp_path = f"{self._holder_path}.{os.getpid()}.tmp"
        # Held for the life of the process: an ``os.open`` descriptor, not a
        # ``with`` block. Truncating text write, as ``open(tmp_path, "w")``.
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.write(json.dumps({"pid": os.getpid(), "created_at": time.time()}))
            handle.flush()
            os.replace(tmp_path, self._holder_path)
        except OSError:
            with contextlib.suppress(OSError):
                handle.close()
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise
        self._holder_handle = handle

    def reserve_ticket(self, key: str) -> int:
        """Reserve a FIFO ticket for the job: a plain file naming this holder."""
        self._ensure_ready()
        if key in self._ticket_handles:
            return self._ticket_handles[key][0]
        with contextlib.suppress(OSError):
            self._ensure_holder()

        ticket = self._next_ticket()
        self._seq += 1
        seq = self._seq
        ticket_path = os.path.join(
            self.lock_dir, f"queue_{ticket}_{seq}_{self._holder_id}_{key}.ticket"
        )
        self._create_ticket_file(ticket_path, ticket)
        self._ticket_handles[key] = (ticket, ticket_path)
        return ticket

    def release_ticket(self, key: str):
        """Release the FIFO ticket for the job.

        Unlinked while this process holds its holder lock, which it does for
        its whole life.
        """
        info = self._ticket_handles.pop(key, None)
        if not info:
            return
        with contextlib.suppress(OSError):
            os.remove(info[1])

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
                    handle = open_lock_handle(path)  # held for the slot
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
            try:
                self._ensure_holder()
                self._prune_dead_tickets()
                queued_keys = self._list_tickets()
            except OSError as exc:
                # Never "nobody is queued": that answer let no job reach the
                # front and deadlocked the queue (EMFILE). Wait and look again.
                self._log_unreadable(exc)
                await asyncio.sleep(0.2)
                continue
            if key not in queued_keys:
                self._restore_ticket_file(key, ticket)
                await asyncio.sleep(0.2)
                continue
            if key in queued_keys[: self.max_concurrent]:
                return True
            await asyncio.sleep(0.2)

    def _log_unreadable(self, exc: OSError) -> None:
        now = time.monotonic()
        if now - self._last_unreadable_log < _UNREADABLE_LOG_INTERVAL:
            return
        self._last_unreadable_log = now
        logger.warning(
            "The job queue in %s could not be read (%s); waiting and retrying",
            self.lock_dir, exc,
        )

    @staticmethod
    def _parse_ticket(name: str) -> tuple[int, int, str | None, str] | None:
        """``(ticket, seq, holder or None, key)`` for a ticket file name.

        Parses the current ``queue_<ticket>_<seq>_<holder>_<key>.ticket`` and
        the two older shapes (``queue_<ticket>_<seq>_<key>`` and the pre-seq
        ``queue_<ticket>_<key>``), which name no holder.
        """
        if not (name.startswith("queue_") and name.endswith(".ticket")):
            return None
        core = name[len("queue_"):-len(".ticket")]
        parts = core.split("_", 3)
        if len(parts) < 2:
            return None
        try:
            ticket = int(parts[0])
        except ValueError:
            return None
        if len(parts) == 4 and parts[1].isdigit() and _HOLDER_TOKEN.fullmatch(parts[2]):
            return ticket, int(parts[1]), parts[2], parts[3]
        if len(parts) >= 3 and parts[1].isdigit():
            return ticket, int(parts[1]), None, "_".join(parts[2:])
        # Legacy pre-seq filename: reserved before the upgrade, so sort it
        # ahead of new (seq >= 1) tickets at the same ticket.
        return ticket, 0, None, "_".join(parts[1:])

    def _prune_dead_tickets(self, *, force: bool = False) -> int:
        """Remove the tickets of holders that died without cleanup.

        A live holder keeps its holder file's flock, so a holder file this
        process can lock (or one that is gone) belongs to a dead process: its
        tickets and leftover temp files are removed while that lock is held,
        then the holder file itself (``unlink_held``). A ticket or temp file
        that names no holder (an older version's) goes when it can be locked.
        Throttled to once per ``_PRUNE_INTERVAL`` unless ``force``. Raises
        OSError when the directory cannot be listed. Returns how many tickets
        went.
        """
        now = time.monotonic()
        if not force and now - self._last_prune < _PRUNE_INTERVAL:
            return 0
        self._last_prune = now
        try:
            names = os.listdir(self.lock_dir)
        except FileNotFoundError:
            return 0
        by_holder: dict[str, list[str]] = {}
        unowned: list[str] = []
        for name in names:
            path = os.path.join(self.lock_dir, name)
            if name.startswith("holder_") and name.endswith(".lock"):
                holder = name[len("holder_"):-len(".lock")]
                by_holder.setdefault(holder, [])
                continue
            base = name
            if name.endswith(".tmp"):
                # `<final name>.tmp` (a ticket or holder being written) or an
                # older `<ticket>.<pid>.tmp`.
                base = re.sub(r"(\.\d+)?\.tmp$", "", name)
                if base.startswith("holder_") and base.endswith(".lock"):
                    unowned.append(path)
                    continue
            parsed = self._parse_ticket(base)
            if parsed is None:
                continue
            holder = parsed[2]
            if holder is None:
                unowned.append(path)
            else:
                by_holder.setdefault(holder, []).append(path)

        removed = 0
        for holder, paths in by_holder.items():
            if holder == self._holder_id:
                continue
            removed += self._prune_holder(holder, paths)
        for path in unowned:
            if self._remove_if_unlocked(path) and path.endswith(".ticket"):
                removed += 1
        return removed

    def _prune_holder(self, holder: str, paths: list[str]) -> int:
        """Remove a dead holder's files; leave a live holder's alone."""
        holder_path = os.path.join(self.lock_dir, f"holder_{holder}.lock")
        try:
            handle = open_lock_handle(holder_path)  # closed below
        except FileNotFoundError:
            handle = None  # no holder file: nobody can be holding these
        except OSError:
            return 0
        removed = 0
        try:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return 0  # held: its process is alive
            for path in paths:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    continue
                if path.endswith(".ticket"):
                    removed += 1
            if handle is not None:
                unlink_held(handle, holder_path)
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                with contextlib.suppress(OSError):
                    handle.close()
        return removed

    @staticmethod
    def _remove_if_unlocked(path: str) -> bool:
        try:
            with open(path, "ab") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return False
                try:
                    return unlink_held(handle, path)
                finally:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return False

    def _list_tickets(self) -> list[str]:
        """Return queued job keys in FIFO order.

        Ordered by ``(ticket, seq, key)``: the shared-counter ticket is the
        primary FIFO key, the per-reservation ``seq`` breaks ties if two tickets
        ever collide (e.g. a counter-file read fallback), and the unique job
        ``key`` is the final tiebreaker. Returning keys (not raw ticket ints)
        means a colliding ticket can never put two jobs in the same admitted
        prefix slot (issue #183, site 4).

        Every ticket shape ``_parse_ticket`` knows is listed, so a rolling
        restart that leaves an older ticket on disk keeps its FIFO position.
        A missing directory is an empty queue; any other listing error is
        raised, never read as "nobody is queued".
        """
        try:
            names = os.listdir(self.lock_dir)
        except FileNotFoundError:
            return []
        entries: list[tuple[int, int, str]] = []
        for name in names:
            parsed = self._parse_ticket(name)
            if parsed is None:
                continue
            ticket, seq, _holder, key = parsed
            entries.append((ticket, seq, key))
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
        """The next number from the shared counter.

        A failed read or write (a busy disk, EMFILE) is retried before the
        in-process fallback is used. The fallback only matters for order: two
        processes that both fall back can issue one number twice, and
        ``_list_tickets`` breaks that tie by ``(seq, key)`` the same way in every
        process, so admission stays consistent and the cap holds.
        """
        for attempt in range(_COUNTER_ATTEMPTS):
            try:
                return self._next_counter_ticket()
            except OSError:
                if attempt + 1 < _COUNTER_ATTEMPTS:
                    time.sleep(0.01)
        # Counter file unreadable/unwritable (the locked write path can't
        # complete): issue a strictly-increasing in-process ticket seeded
        # above the last value seen on disk instead of a wall-clock number
        # that could sort out of range and mis-order FIFO admission (issue
        # #183, site 4).
        self._fallback_ticket += 1
        return self._fallback_ticket

    def _next_counter_ticket(self) -> int:
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

    def _create_ticket_file(self, ticket_path: str, ticket: int) -> bool:
        """Write the ticket (a plain file) atomically. False when it could not be
        written; ``_wait_for_turn`` writes it again."""
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
            return True
        except OSError:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            return False

    def _restore_ticket_file(self, key: str, ticket: int) -> None:
        info = self._ticket_handles.get(key)
        if not info:
            return
        _, ticket_path = info
        if os.path.exists(ticket_path):
            return
        self._create_ticket_file(ticket_path, ticket)

    def stats(self) -> dict:
        try:
            tickets = len(self._list_tickets())
        except OSError:
            tickets = None
        return {"tickets": tickets, "slots": len(self._slot_paths)}


# Lazy: importing this module touches nothing on disk.
concurrency_manager = ConcurrencyManager(
    settings.max_concurrent_jobs, settings.concurrency_lock_dir, lazy=True,
)
