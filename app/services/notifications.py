"""Notifications (#293): tell a webhook or an Apprise URL when something finishes.

Nothing leaves the server until a destination is saved. Callers use the small
public surface at the bottom of this module and never touch a transport:

* ``notify(event)``: fire and forget; sends run off the caller's path.
* ``on_job_finished(job)``: the ``JobManager`` terminal listener. A failed job
  started by hand becomes one ``job_failed`` message.
* ``sweep_jobs_queued(jobs)`` / ``sweep_finished(result)``: the sweep's two
  hooks. A sweep queues jobs and returns; the message goes out once the last of
  its jobs is terminal, so its counts and bytes saved are real. Failures inside
  a sweep are listed in that one message, never sent one by one.

Events are a shared registry (``register_event``) so a later ticket adds its
own (check failed, for instance) without touching this module's logic.

Destination URLs are secrets (a webhook token, a bot token inside an Apprise
URL). They live in the ``notifications`` preference, are never returned by the
API, and never appear in a log line or an error message.
"""

from __future__ import annotations

import asyncio
import http.client
import importlib.util
import json
import logging
import os
import re
import socket
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib import parse as urlparse

from logging_setup import get_logger
from models import ConversionJob, JobStatus
from services import ledger
from services.preferences_store import preferences_store

logger = get_logger("notifications")

PREF_KEY = "notifications"
KINDS = ("webhook", "apprise")
MAX_DESTINATIONS = 20
MAX_URL_LEN = 2048
MAX_LABEL_LEN = 60
# A destination that hangs is abandoned after this long; the job never waits.
SEND_TIMEOUT_S = 10.0
MAX_LISTED_FAILURES = 20
# Messages waiting behind a slow destination; the oldest is dropped beyond this.
MAX_QUEUED_PER_DESTINATION = 20
MAX_ERROR_LEN = 300

# ----------------------------------------------------------------------
# events
# ----------------------------------------------------------------------

# key -> what the user sees in Settings. Later tickets add theirs here.
_EVENTS: dict[str, str] = {}


def register_event(key: str, title: str) -> None:
    _EVENTS[key] = title


def event_list() -> list[dict[str, str]]:
    return [{"key": k, "title": t} for k, t in _EVENTS.items()]


register_event("sweep_finished", "A sweep finished (one message with counts and space saved)")
register_event("job_failed", "A job you started failed")


@dataclass
class Event:
    kind: str
    title: str
    body: str
    data: dict[str, Any] = field(default_factory=dict)


class NotificationError(Exception):
    """A user-facing reason a destination could not be saved or reached."""


# ----------------------------------------------------------------------
# transports: ``send(url, event)`` blocks, raises on failure
# ----------------------------------------------------------------------


def _check_http_url(url: str) -> None:
    # Whitespace and control characters make http.client raise InvalidURL, whose
    # message echoes the path and query (the token).
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise NotificationError("The URL must not contain spaces or control characters")
    try:
        parts = urlparse.urlsplit(url)
        port = parts.port  # raises ValueError when out of range
    except ValueError:
        raise NotificationError("That is not a valid URL") from None
    if port == 0:
        raise NotificationError("That is not a valid URL")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotificationError("A webhook URL must start with http:// or https://")


def _abort(conn: http.client.HTTPConnection) -> None:
    """Wake a thread blocked on this connection's socket (the deadline hit)."""
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    conn.close()


def _send_webhook(url: str, event: Event) -> None:
    _check_http_url(url)
    parts = urlparse.urlsplit(url)
    payload = json.dumps({
        "event": event.kind, "title": event.title, "message": event.body, "data": event.data,
    }).encode()
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    # http.client, not urlopen: the scheme is fixed to http/https by the check
    # above, redirects are never followed, and the whole exchange gets one
    # deadline (a socket timeout alone lets a slow-drip server hold the thread).
    factory = (
        http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    )
    # An explicit port: without one, http.client reads the last ":" group of an
    # unbracketed IPv6 literal as the port.
    default_port = 443 if parts.scheme == "https" else 80
    conn = factory(parts.hostname, parts.port or default_port, timeout=SEND_TIMEOUT_S)
    expired = threading.Event()

    def on_deadline() -> None:
        expired.set()
        _abort(conn)

    timer = threading.Timer(SEND_TIMEOUT_S * 0.9, on_deadline)
    timer.daemon = True
    timer.start()
    try:
        conn.request("POST", target, body=payload, headers={
            "Content-Type": "application/json", "User-Agent": "compressatorium",
            "Host": parts.netloc.rsplit("@", 1)[-1],
        })
        response = conn.getresponse()
        response.read(1024)
        status = response.status
    except (OSError, http.client.HTTPException) as exc:
        if expired.is_set():
            raise NotificationError("The webhook did not finish in time") from None
        # str(exc) can echo the URL; the reason alone is safe.
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        raise NotificationError(f"Could not reach the webhook: {reason}") from None
    finally:
        timer.cancel()
        conn.close()
    if status >= 300:
        raise NotificationError(f"The webhook answered HTTP {status}")


def apprise_available() -> bool:
    return importlib.util.find_spec("apprise") is not None


def _send_apprise(url: str, event: Event) -> None:
    try:
        import apprise  # lazy: the webhook path never needs it
    except ImportError:
        raise NotificationError(_APPRISE_MISSING) from None
    notifier = apprise.Apprise()
    if not notifier.add(url):
        raise NotificationError("Apprise does not recognise that URL")
    # Apprise reports why a send failed only through its log; capture the last
    # warning so Test can say "error=401" or "connection error", not just "failed".
    for service in notifier:  # bound each plugin's own network waits
        for attr in ("socket_connect_timeout", "socket_read_timeout"):
            if hasattr(service, attr):
                setattr(service, attr, SEND_TIMEOUT_S * 0.4)
    with apprise.LogCapture(level=logging.WARNING) as capture:
        delivered = notifier.notify(title=event.title, body=event.body)
        captured = capture.getvalue()
    if not delivered:
        reason = _apprise_reason(captured, url)
        raise NotificationError(
            f"Apprise could not deliver the message: {reason}" if reason
            else "Apprise could not deliver the message",
        )


def _apprise_reason(captured: str, url: str) -> str:
    lines = [ln for ln in captured.splitlines() if " - WARNING - " in ln or " - ERROR - " in ln]
    if not lines:
        return ""
    return _redact(lines[-1].split(" - ", 2)[-1].strip(), url)[:200]


_APPRISE_MISSING = (
    "Apprise is not installed in this build, so Apprise URLs cannot be used. "
    "Use a webhook URL, or run the Docker image, which includes Apprise."
)

TRANSPORTS: dict[str, Callable[[str, Event], None]] = {
    "webhook": _send_webhook,
    "apprise": _send_apprise,
}

# Own pool so a hung destination can never starve the app's default thread pool.
# A thread cannot be killed once a transport hangs, so two limits keep the pool
# usable: each destination holds at most one send in flight (`_in_flight`), and
# the pool is larger than MAX_DESTINATIONS plus a few Test sends.
_executor = ThreadPoolExecutor(max_workers=MAX_DESTINATIONS + 12, thread_name_prefix="notify")
_in_flight: set[str] = set()


async def _send_one(kind: str, url: str, event: Event, slot: str | None = None) -> None:
    """Send *event* through one transport, bounded by SEND_TIMEOUT_S. Raises.

    *slot* names a saved destination: while its previous send's thread is still
    running (it outlived the timeout), a new send to it is refused rather than
    stacking another thread behind a dead endpoint.
    """
    if slot is not None:
        if slot in _in_flight:
            raise NotificationError("The previous send to this destination is still running")
        _in_flight.add(slot)
    future = _executor.submit(TRANSPORTS[kind], url, event)
    if slot is not None:
        # Released when the thread really ends, not when we stop waiting.
        future.add_done_callback(lambda _f: _in_flight.discard(slot))
    try:
        await asyncio.wait_for(asyncio.wrap_future(future), SEND_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise NotificationError(f"Timed out after {SEND_TIMEOUT_S:g}s") from None


# ----------------------------------------------------------------------
# destination store
# ----------------------------------------------------------------------

_store_lock = asyncio.Lock()


async def _load() -> list[dict]:
    stored = await preferences_store.get(PREF_KEY) or {}
    dests = stored.get("destinations")
    return [d for d in dests if isinstance(d, dict)] if isinstance(dests, list) else []


async def _save(dests: list[dict]) -> None:
    await preferences_store.put(PREF_KEY, {"destinations": dests})


def _hint(dest: dict) -> str:
    """Where it goes, with nothing secret: the host, or just the Apprise scheme."""
    url = dest.get("url", "")
    if dest.get("kind") == "webhook":
        return urlparse.urlsplit(url).hostname or ""
    return url.split("://", 1)[0] + "://" if "://" in url else ""


def public_view(dest: dict) -> dict:
    """What the browser may see. Never the URL."""
    return {
        "id": dest["id"], "kind": dest["kind"],
        "label": dest.get("label") or _hint(dest), "hint": _hint(dest),
    }


def validate(kind: str, url: str) -> str:
    """Return the cleaned URL or raise NotificationError."""
    if kind not in KINDS:
        raise NotificationError("Unknown destination type")
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LEN:
        raise NotificationError("Enter a URL")
    if kind == "webhook":
        _check_http_url(url)
    else:
        if not apprise_available():
            raise NotificationError(_APPRISE_MISSING)
        if "://" not in url:
            raise NotificationError("An Apprise URL looks like discord://id/token")
    return url


def _clean_label(label: str | None) -> str:
    return (label or "").strip()[:MAX_LABEL_LEN]


async def list_destinations() -> list[dict]:
    return [public_view(d) for d in await _load()]


async def add_destination(kind: str, url: str, label: str | None) -> dict:
    url = validate(kind, url)
    async with _store_lock:
        dests = await _load()
        if len(dests) >= MAX_DESTINATIONS:
            raise NotificationError(f"At most {MAX_DESTINATIONS} destinations")
        dest = {"id": uuid.uuid4().hex[:8], "kind": kind, "url": url,
                "label": _clean_label(label)}
        await _save(dests + [dest])
    return public_view(dest)


async def update_destination(dest_id: str, url: str | None, label: str | None) -> dict | None:
    async with _store_lock:
        dests = await _load()
        for dest in dests:
            if dest["id"] == dest_id:
                if url is not None:
                    dest["url"] = validate(dest["kind"], url)
                if label is not None:
                    dest["label"] = _clean_label(label)
                await _save(dests)
                return public_view(dest)
    return None


async def remove_destination(dest_id: str) -> bool:
    async with _store_lock:
        dests = await _load()
        kept = [d for d in dests if d["id"] != dest_id]
        if len(kept) == len(dests):
            return False
        await _save(kept)
        return True


async def test_destination(
    dest_id: str | None = None, kind: str | None = None, url: str | None = None,
) -> dict | None:
    """Send a test message. ``{"ok", "error"}``, or None for an unknown saved id."""
    if dest_id is not None:
        dest = next((d for d in await _load() if d["id"] == dest_id), None)
        if dest is None:
            return None
        kind, url = dest["kind"], dest["url"]
    event = Event("test", "Compressatorium test message",
                  "If you can read this, notifications are working.")
    try:
        url = validate(kind or "", url or "")
        await _send_one(kind, url, event)
    except NotificationError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001  never leak what an exception echoes
        logger.warning("Notification test failed (%s): %s", kind, type(exc).__name__)
        return {"ok": False, "error": f"The send failed ({type(exc).__name__})"}
    return {"ok": True, "error": None}


_URL_SPLIT = re.compile(r"[/?#@:&=;,]+")


def _redact(text: str, url: str | None) -> str:
    """Remove the destination URL, and any piece of it that could be a secret,
    from *text*. Pieces shorter than 4 characters (a scheme, a port) stay."""
    if not url:
        return text
    text = text.replace(url, "<url>")
    rest = url.split("://", 1)[-1]
    pieces = {p for p in _URL_SPLIT.split(rest) if len(p) >= 4}
    for piece in sorted(pieces, key=len, reverse=True):
        text = text.replace(piece, "<redacted>")
    return text


# ----------------------------------------------------------------------
# sending
# ----------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()


_queues: dict[str, deque] = {}
_workers: dict[str, asyncio.Task] = {}


def _enqueue(dest: dict, event: Event) -> asyncio.Future:
    """Queue *event* behind whatever *dest* is already sending.

    Sends to one destination go one at a time, in order, so two events close
    together both arrive. A destination whose transport outlived its deadline
    refuses the next send in `_send_one` instead of stacking another thread.
    """
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    slot = dest["id"]
    queue = _queues.setdefault(slot, deque())
    if len(queue) >= MAX_QUEUED_PER_DESTINATION:
        _, _, dropped = queue.popleft()
        logger.warning(
            "Notification queue for destination %s is full; dropped the oldest message", slot,
        )
        if not dropped.done():
            dropped.set_exception(NotificationError("Dropped: too many messages waiting"))
    queue.append((dest, event, done))
    worker = _workers.get(slot)
    if worker is None or worker.done():
        task = loop.create_task(_drain_destination(slot))
        _workers[slot] = task
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return done


async def _drain_destination(slot: str) -> None:
    queue = _queues[slot]
    while queue:
        dest, event, done = queue.popleft()
        try:
            await _send_one(dest["kind"], dest["url"], event, slot=slot)
        except Exception as exc:  # noqa: BLE001  handed to the waiting caller
            if not done.done():
                done.set_exception(exc)
        else:
            if not done.done():
                done.set_result(None)


async def deliver(event: Event) -> None:
    """Send *event* to every destination. Never raises; each failure is logged."""
    try:
        dests = await _load()
    except Exception:
        logger.warning("Could not read notification destinations", exc_info=True)
        return
    if not dests:
        return

    async def one(dest: dict) -> None:
        try:
            await _enqueue(dest, event)
        except Exception as exc:  # noqa: BLE001  one destination never fails the rest
            # Not exc_info and not str(exc) of a foreign exception: both can
            # carry the URL. Our own NotificationError texts are redacted too.
            reason = (
                _redact(str(exc), dest["url"]) if isinstance(exc, NotificationError)
                else type(exc).__name__
            )
            logger.warning(
                "Notification to %s destination %s failed: %s",
                dest["kind"], dest["id"], reason,
            )

    await asyncio.gather(*(one(d) for d in dests))


def notify(event: Event) -> None:
    """Queue *event* for delivery and return at once. Call from the event loop."""
    try:
        task = asyncio.get_running_loop().create_task(deliver(event))
    except RuntimeError:
        return  # no loop (a synchronous harness): nothing to send on
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for queued sends (tests and shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ----------------------------------------------------------------------
# messages
# ----------------------------------------------------------------------


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _short(text: str | None) -> str:
    text = " ".join((text or "unknown error").split())
    return text if len(text) <= MAX_ERROR_LEN else text[: MAX_ERROR_LEN - 1] + "…"


def _file(job: ConversionJob) -> str:
    return job.filename or os.path.basename(job.file_path or "") or job.id


def job_failed_event(job: ConversionJob) -> Event:
    mode = getattr(job.mode, "value", str(job.mode))
    err = _short(job.error_message)
    return Event(
        "job_failed", f"Job failed: {_file(job)}", f"{mode}: {err}",
        {"job_id": job.id, "filename": _file(job), "mode": mode, "error": err},
    )


class _Sweep:
    """One sweep's jobs, waited on until the last is terminal."""

    def __init__(self) -> None:
        self.pending: set[str] = set()
        self.jobs: list[ConversionJob] = []
        self.closed = False
        self.errors: list[dict] = []
        # False when every error was already reported by an earlier sweep.
        self.has_new_errors = False
        # Jobs that ended before `sweep_jobs_queued` saw them.
        self.early: dict[str, ConversionJob] = {}

    def event(self) -> Event | None:
        done = [j for j in self.jobs if j.status == JobStatus.COMPLETED]
        failed = [j for j in self.jobs if j.status == JobStatus.FAILED]
        cancelled = [j for j in self.jobs if j.status == JobStatus.CANCELLED]
        if not self.jobs and not self.has_new_errors:
            return None
        saved = sum(ledger.job_saved_bytes(j) for j in done)
        failures = [{"file": _file(j), "error": _short(j.error_message)} for j in failed]
        errors = [
            f"platform {e.get('platform_id')}: {e.get('error')}" if e.get("platform_id")
            is not None else str(e.get("error"))
            for e in self.errors
        ]
        parts = [f"{len(done)} converted"]
        if failed:
            parts.append(f"{len(failed)} failed")
        if cancelled:
            parts.append(f"{len(cancelled)} cancelled")
        title = f"Sweep finished: {', '.join(parts)}, {_fmt_bytes(saved)} saved"
        lines = [f"Queued {len(self.jobs)}. Space saved: {_fmt_bytes(saved)}."]
        if failures:
            lines.append("Failed:")
            lines += [f"- {f['file']}: {f['error']}" for f in failures[:MAX_LISTED_FAILURES]]
            if len(failures) > MAX_LISTED_FAILURES:
                lines.append(f"- and {len(failures) - MAX_LISTED_FAILURES} more")
        if errors:
            lines.append("Could not queue:")
            lines += [f"- {e}" for e in errors]
        return Event("sweep_finished", title, "\n".join(lines), {
            "queued": len(self.jobs), "completed": len(done), "failed": len(failed),
            "cancelled": len(cancelled), "saved_bytes": saved,
            "failures": failures, "errors": list(self.errors),
        })


_open_sweep: _Sweep | None = None
# Source path -> the sweep that is about to queue it. Opened before the queue
# call, because that call enqueues and then awaits: a job can end (and its
# terminal listener run) before the call returns and `sweep_jobs_queued` runs.
_expected: dict[str, _Sweep] = {}
_reported_errors: set[tuple] = set()
_TERMINAL = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
_job_sweeps: dict[str, _Sweep] = {}


def sweep_expect(paths: list[str]) -> None:
    """Sweep hook 0: these Sources are about to be queued by the running sweep.
    Call just before the queue call, so no job of the batch is ever mistaken for
    a manual one."""
    global _open_sweep
    if _open_sweep is None:
        _open_sweep = _Sweep()
    for path in paths:
        _expected[str(path)] = _open_sweep


def sweep_jobs_queued(jobs: list[ConversionJob]) -> None:
    """Sweep hook 1: these jobs belong to the running sweep. Call with no await
    after the queue accepts them, so a job that ends at once is already claimed."""
    global _open_sweep
    if _open_sweep is None:
        _open_sweep = _Sweep()
    for job in jobs:
        _expected.pop(job.file_path, None)
        if job.id in _open_sweep.early:  # already counted by the listener
            continue
        _open_sweep.jobs.append(job)
        # A job can end before this hook runs (the queue awaits after accepting
        # a batch). Nothing would ever discard it from `pending`, so it is
        # counted but not waited on.
        if getattr(job, "status", None) not in _TERMINAL:
            _open_sweep.pending.add(job.id)
            _job_sweeps[job.id] = _open_sweep


def sweep_finished(result: dict, *, dry_run: bool = False) -> None:
    """Sweep hook 2: the pass is over. Sends when its jobs have all ended."""
    global _open_sweep
    sweep, _open_sweep = _open_sweep, None
    if dry_run:
        return
    sweep = sweep or _Sweep()
    for path in [p for p, owner in _expected.items() if owner is sweep]:
        del _expected[path]  # a batch that never came back leaves no claim
    sweep.closed = True
    sweep.errors = [e for e in result.get("errors") or [] if isinstance(e, dict)]
    # A failing platform is due again every scheduler tick and records the same
    # error each time. Report an error once per (platform, reason); it is
    # forgotten only when a later sweep inspected the platform without it (below).
    global _reported_errors
    keys = {(e.get("platform_id"), str(e.get("error"))) for e in sweep.errors}
    sweep.has_new_errors = bool(keys - _reported_errors)
    # Forget an error only when this sweep looked at that platform and found it
    # clean. A platform the sweep skipped (a platform_ids filter, the queue cap
    # stopping early) says nothing, so its error stays reported. A sweep that
    # died has no "platforms" at all and clears nothing.
    checked = {p.get("platform_id") for p in result.get("platforms") or []
               if isinstance(p, dict)}
    finished = "platforms" in result
    kept = {
        k for k in _reported_errors
        if (k[0] is None and not finished) or (k[0] is not None and k[0] not in checked)
    }
    _reported_errors = kept | keys
    _maybe_send(sweep)


def _maybe_send(sweep: _Sweep) -> None:
    if not sweep.closed or sweep.pending:
        return
    event = sweep.event()
    if event is not None:
        notify(event)


async def on_job_finished(job: ConversionJob) -> None:
    """``JobManager`` terminal listener. Cheap for anything that is not news."""
    if job.status not in _TERMINAL:
        return
    sweep = _job_sweeps.pop(job.id, None)
    if sweep is None:
        claimed = _expected.get(job.file_path)
        if claimed is not None and job.id not in claimed.early:
            # Ended inside the queue call, before the sweep got the job back.
            claimed.early[job.id] = job
            claimed.jobs.append(job)
            return
    if sweep is not None:
        sweep.pending.discard(job.id)
        _maybe_send(sweep)
    elif job.status == JobStatus.FAILED:
        notify(job_failed_event(job))


def _reset_for_tests() -> None:
    global _open_sweep, _reported_errors
    _open_sweep = None
    _reported_errors = set()
    _job_sweeps.clear()
    _expected.clear()
    _queues.clear()
    _workers.clear()
    _in_flight.clear()
    _tasks.clear()
