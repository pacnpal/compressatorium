"""Volume work for a request, off the event loop and bounded (#294, #327).

Volume containment (``is_within_configured_volumes``,
``ensure_path_within_volumes``, ``realpath``) stats and resolves paths, and the
Volume root probes behind it wait on mounts. On the event loop, a mount that
stops answering froze every API request. Every route runs that work through
:func:`off_loop_bounded`, the one copy of this rule.
"""
from __future__ import annotations

import asyncio

from fastapi import HTTPException
from services.job_manager import VOLUME_PROBE_TIMEOUT_SECONDS
from services.subprocess_runner import run_detached

VOLUME_UNRESPONSIVE_DETAIL = "A Volume isn't responding. Try again later."


async def off_loop_bounded(func, *args, **kwargs):
    """Run blocking Volume work off the event loop, bounded; 503 past the bound.

    On a disposable thread (``run_detached``), so a Volume that stops answering
    holds neither the loop nor a shared pool worker. Past
    ``VOLUME_PROBE_TIMEOUT_SECONDS`` the request answers 503 and the thread is
    abandoned; it may linger until the mount answers, and its result is
    dropped. A process-wide probe ceiling being full
    (``ProbeCapacityExceeded``, an ``asyncio.TimeoutError``) answers 503 the
    same way. Exceptions *func* raises (an ``HTTPException`` 403 for a path
    outside the Volumes) reach the caller unchanged.
    """
    try:
        return await asyncio.wait_for(
            run_detached(func, *args, **kwargs), VOLUME_PROBE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail=VOLUME_UNRESPONSIVE_DETAIL) from None
