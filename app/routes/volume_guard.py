"""Volume work for a request, off the event loop and bounded (#294, #327).

Volume containment (``is_within_configured_volumes``,
``ensure_path_within_volumes``, ``realpath``) stats and resolves paths, and the
Volume root probes behind it wait on mounts. On the event loop, a mount that
stops answering froze every API request. Every route runs that work through
:func:`off_loop_bounded`: the route-level contract (the named bound, 503 past
it) on top of the shared bounded seam, ``subprocess_runner.run_detached_bounded``.
"""
from __future__ import annotations

from fastapi import HTTPException
from services.job_manager import VOLUME_PROBE_TIMEOUT_SECONDS
from services.subprocess_runner import (
    DeadlineExpired,
    ProbeCapacityExceeded,
    run_detached_bounded,
)

VOLUME_UNRESPONSIVE_DETAIL = "A Volume isn't responding. Try again later."


async def off_loop_bounded(func, *args, **kwargs):
    """Run blocking Volume work off the event loop, bounded; 503 past the bound.

    Through ``run_detached_bounded`` (#343): a disposable thread, so a Volume
    that stops answering holds neither the loop nor a shared pool worker, and
    ``VOLUME_PROBE_TIMEOUT_SECONDS`` as the bound. Past it
    (``DeadlineExpired``), or with the process-wide probe ceiling full
    (``ProbeCapacityExceeded``), the request answers 503 and the thread is
    abandoned; it may linger until the mount answers, and its result is
    dropped. Anything *func* raises reaches the caller as itself: an
    ``HTTPException`` 403 for a path outside the Volumes, and its own
    ``TimeoutError`` too, which is never mistaken for the bound.
    """
    try:
        return await run_detached_bounded(
            func, *args, timeout=VOLUME_PROBE_TIMEOUT_SECONDS, **kwargs,
        )
    except (DeadlineExpired, ProbeCapacityExceeded):
        raise HTTPException(status_code=503, detail=VOLUME_UNRESPONSIVE_DETAIL) from None
