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
    Stalled,
    run_detached_bounded,
    run_detached_until_stalled,
)

VOLUME_UNRESPONSIVE_DETAIL = "A Volume isn't responding. Try again later."

# How long a folder listing or walk may go without reading one entry (#361).
# Reset on every entry, so a large folder that keeps moving is never cut off.
WALK_STALL_SECONDS = VOLUME_PROBE_TIMEOUT_SECONDS


async def off_loop_until_stalled(func, *args, what: str, **kwargs):
    """Run a blocking listing or walk off the event loop; 503 once it stops moving.

    Through ``run_detached_until_stalled`` (#361): *func* is called with a
    ``progress`` keyword it reports every entry to (``utils.path_utils.
    walk_tree`` does it for a walk), on a disposable thread. A listing that
    reads nothing for ``WALK_STALL_SECONDS`` (a frozen mount), or the
    process-wide probe ceiling being full, answers 503; the thread is
    abandoned and stops at its next step. A large folder that keeps moving
    is never cut off. Anything *func* raises reaches the caller as itself.
    """
    try:
        return await run_detached_until_stalled(
            func, *args, stall=WALK_STALL_SECONDS, what=what, **kwargs,
        )
    except (Stalled, ProbeCapacityExceeded):
        raise HTTPException(status_code=503, detail=VOLUME_UNRESPONSIVE_DETAIL) from None


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
