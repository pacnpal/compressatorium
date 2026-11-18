"""Server-sent event responses that end cleanly when the server stops (#465).

``sse_starlette`` watches for uvicorn's shutdown signal. Left to its default,
it then cancels the whole response at once, so the closing body chunk is never
sent and uvicorn logs "ASGI callable returned without completing response."
for every stream that was open.

``event_stream`` uses the library's cooperative path instead. It hands the
response a shutdown event and a grace period, and wraps the generator so the
wait for its next event stops as soon as that event is set. The response then
sends its closing chunk and returns like any stream that ran out of events.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

import anyio
from sse_starlette.sse import EventSourceResponse

# How long a stream may take to wind down once the server starts stopping
# before the library falls back to cancelling it outright. The wrapped
# generator stops at its next await, so this is only ever a backstop.
SHUTDOWN_GRACE_SECONDS = 5.0

_END = object()


async def _cancel_when_set(shutdown: anyio.Event, scope: anyio.CancelScope) -> None:
    await shutdown.wait()
    scope.cancel()


async def _next_or_end(iterator: AsyncIterator[Any], shutdown: anyio.Event) -> Any:
    """The iterator's next item, or ``_END`` once it runs out or shutdown begins."""
    with anyio.CancelScope() as scope:
        watcher = asyncio.create_task(_cancel_when_set(shutdown, scope))
        try:
            return await anext(iterator)
        except StopAsyncIteration:
            return _END
        finally:
            watcher.cancel()
    # Only reached when shutdown cancelled the wait for the next item.
    return _END


async def until_shutdown(
    content: AsyncIterable[Any], shutdown: anyio.Event,
) -> AsyncIterator[Any]:
    """Yield ``content`` until it ends or ``shutdown`` is set, then close it."""
    iterator = aiter(content)
    try:
        while not shutdown.is_set():
            item = await _next_or_end(iterator, shutdown)
            if item is _END:
                return
            yield item
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


def event_stream(content: AsyncIterable[Any]) -> EventSourceResponse:
    """An ``EventSourceResponse`` over ``content`` that ends cleanly on shutdown."""
    shutdown = anyio.Event()
    return EventSourceResponse(
        until_shutdown(content, shutdown),
        shutdown_event=shutdown,
        shutdown_grace_period=SHUTDOWN_GRACE_SECONDS,
    )
