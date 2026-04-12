import asyncio
import threading

from config import settings


class WorkloadToken:
    """Represents one leased slot from a workload lane."""

    def __init__(self, limiter: "WorkloadLimiter", lane: str):
        self._limiter = limiter
        self._lane = lane
        self._released = False

    async def __aenter__(self) -> "WorkloadToken":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter._release(self._lane)


class WorkloadLimiter:
    """Simple lane-based concurrency limiter.

    ``acquire()`` blocks until a slot is available in the requested lane.
    ``try_acquire()`` is the best-effort, non-blocking variant: it returns
    ``None`` immediately if no slot is free within the configured timeout.
    """

    def __init__(
        self,
        *,
        verify_limit: int,
        metadata_scan_limit: int,
        match_limit: int = 1,
    ):
        self._limits = {
            "verify": max(1, int(verify_limit)),
            "metadata_scan": max(1, int(metadata_scan_limit)),
            "match": max(1, int(match_limit)),
        }
        self._semaphores = {
            lane: asyncio.Semaphore(limit)
            for lane, limit in self._limits.items()
        }
        self._in_use = {lane: 0 for lane in self._limits}
        self._counter_lock = threading.Lock()

    async def acquire(self, lane: str) -> WorkloadToken:
        semaphore = self._semaphores[lane]
        await semaphore.acquire()
        with self._counter_lock:
            self._in_use[lane] += 1
        return WorkloadToken(self, lane)

    async def try_acquire(
        self, lane: str, *, timeout_seconds: float = 0.01
    ) -> WorkloadToken | None:
        semaphore = self._semaphores[lane]
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=max(0.0, timeout_seconds))
        except asyncio.TimeoutError:
            return None
        with self._counter_lock:
            self._in_use[lane] += 1
        return WorkloadToken(self, lane)

    def _release(self, lane: str) -> None:
        semaphore = self._semaphores[lane]
        semaphore.release()
        with self._counter_lock:
            self._in_use[lane] = max(0, self._in_use[lane] - 1)

    def in_use(self, lane: str) -> int:
        with self._counter_lock:
            return self._in_use[lane]

    def limit(self, lane: str) -> int:
        return self._limits[lane]


workload_limiter = WorkloadLimiter(
    verify_limit=settings.max_verify_concurrency,
    metadata_scan_limit=settings.max_metadata_scan_concurrency,
    match_limit=settings.max_match_concurrency,
)
