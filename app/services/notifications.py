"""Notifications (#293): tell a webhook or an Apprise URL when something finishes.

Nothing leaves the server until a destination is saved. Callers use the small
public surface at the bottom of this module and never touch a transport:

* ``notify(event)``: fire and forget; sends run off the caller's path.
* ``on_job_finished(job)``: the ``JobManager`` terminal listener. A failed job
  started by hand becomes one ``job_failed`` message.
* ``sweep_owner()`` / ``sweep_jobs_queued(jobs)`` / ``sweep_finished(result)``:
  the sweep's three hooks. A sweep queues jobs and returns; the message goes out
  once the last of its jobs is terminal, so its counts and bytes saved are real.
  Failures inside a sweep are listed in that one message, never sent one by one.

Events are a shared registry (``register_event``) so a later ticket adds its
own (check failed, for instance) without touching this module's logic. Each
destination gets only the events it chose (#523); one that never chose gets
those registered ``default=True``. A generic webhook with a signing secret
carries the raw body's HMAC-SHA256 in ``X-Compressatorium-Signature``.

Destination URLs and signing secrets are secrets (a webhook token, a bot token
inside an Apprise URL). They live in the ``notifications`` preference, are never returned by the
API, and never appear in a log line or an error message.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import importlib.util
import json
import logging
import os
import re
import socket
import string
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib import parse as urlparse

from logging_setup import get_logger
from models import ConversionJob, JobStatus
from services import ledger
from services.preferences_store import preferences_store
from services.subprocess_runner import ProbeCapacityExceeded, run_detached

logger = get_logger("notifications")

PREF_KEY = "notifications"
MAX_DESTINATIONS = 20
MAX_URL_LEN = 2048
MAX_LABEL_LEN = 60
MAX_SECRET_LEN = 256
# The generic webhook's HMAC-SHA256 of the raw body, when its destination has a
# signing secret (#523): ``sha256=<hex>``.
SIGNATURE_HEADER = "X-Compressatorium-Signature"
# A destination that hangs is abandoned after this long; the job never waits.
SEND_TIMEOUT_S = 10.0
MAX_LISTED_FAILURES = 20
# Messages waiting behind a slow destination; the oldest is dropped beyond this.
MAX_QUEUED_PER_DESTINATION = 20
MAX_ERROR_LEN = 300

# ----------------------------------------------------------------------
# events
# ----------------------------------------------------------------------

# key -> (what the user sees in Settings, whether a destination that never
# chose its events gets it). Later tickets add theirs here.
_EVENTS: dict[str, tuple[str, bool]] = {}


def register_event(key: str, title: str, *, default: bool = True) -> None:
    _EVENTS[key] = (title, default)


def event_list() -> list[dict]:
    return [{"key": k, "title": t, "default": d} for k, (t, d) in _EVENTS.items()]


def _default_events() -> list[str]:
    return [k for k, (_, d) in _EVENTS.items() if d]


register_event("sweep_finished", "A sweep finished (one message with counts and space saved)")
register_event("job_failed", "A job you started failed")
# Off unless chosen (#523): one message per job, so a batch sends one each.
register_event("job_succeeded", "A job you started finished", default=False)
register_event("rename_finished", "A rename to DAT names finished (one message with counts)")
register_event(
    "inbox_pass", "An Inbox pass placed Images or left some with a Finding (one message)",
)


@dataclass
class Event:
    kind: str
    title: str
    body: str
    data: dict[str, Any] = field(default_factory=dict)


class NotificationError(Exception):
    """A user-facing reason a destination could not be saved or reached.

    Whatever shows or logs one for a destination asks for :meth:`redacted`,
    since its text can quote the destination's URL. Raised as
    ``NotificationError(text)`` (a transport someone else wrote), the whole
    text is redacted. This module raises through :meth:`said` instead: its
    template is the module's own wording and is never redacted, so a URL whose
    path is ``/webhook`` leaves "Could not reach the webhook" readable (#567),
    and only the parts filled into it (an OS reason, a server header), which
    came from elsewhere, are.
    """

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self._template: str | None = None
        self._parts: dict[str, Any] = {}

    @classmethod
    def said(cls, template: str, /, **parts: Any) -> NotificationError:
        """This module's own message: *template* with *parts* filled in."""
        err = cls(template.format(**parts))
        err._template, err._parts = template, parts
        return err

    def redacted(self, url: str | None) -> str:
        """The text with every piece of *url* that came from elsewhere removed.

        Each part is formatted with its own spec and conversion first (a
        ``{limit:g}`` needs the number), then redacted."""
        if self._template is None:
            return _redact(str(self), url)
        out = []
        for literal, name, spec, conversion in string.Formatter().parse(self._template):
            out.append(literal)
            if name is not None:
                value = self._parts[name]
                if conversion:
                    value = {"r": repr, "s": str, "a": ascii}[conversion](value)
                out.append(_redact(format(value, spec or ""), url))
        return "".join(out)


# ----------------------------------------------------------------------
# transports: ``send(url, event)`` blocks, raises on failure
# ----------------------------------------------------------------------


def _check_http_url(url: str) -> None:
    # Whitespace and control characters make http.client raise InvalidURL, whose
    # message echoes the path and query (the token).
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise NotificationError.said("The URL must not contain spaces or control characters")
    try:
        parts = urlparse.urlsplit(url)
        port = parts.port  # raises ValueError when out of range
    except ValueError:
        raise NotificationError.said("That is not a valid URL") from None
    if port == 0:
        raise NotificationError.said("That is not a valid URL")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotificationError.said("A webhook URL must start with http:// or https://")


def _abort(conn: http.client.HTTPConnection) -> None:
    """Wake a thread blocked on this connection's socket (the deadline hit)."""
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    conn.close()


def generic_payload(event: Event) -> dict:
    """The generic webhook's body: ``{event, title, message, data}``."""
    return {"event": event.kind, "title": event.title, "message": event.body, "data": event.data}


# Discord's documented limits: content 2000; embed title 256, description 4096,
# field name 256 and value 1024, at most 25 fields (we send at most 10); and 6000
# characters over everything in one embed.
_DISCORD_COLORS = {"ok": 0x2ECC71, "bad": 0xE74C3C, "neutral": 0x95A5A6}
_DISCORD_MAX_FIELDS = 10
_DISCORD_EMBED_TOTAL = 6000


def _clip(text: str, limit: int) -> str:
    """Truncate to *limit* characters with an ellipsis; never fails."""
    text = str(text)
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def _discord_color(event: Event) -> int:
    data = event.data
    if event.kind == "job_failed" or data.get("failed") or data.get("errors"):
        return _DISCORD_COLORS["bad"]
    if event.kind == "sweep_finished":
        return _DISCORD_COLORS["ok"]
    return _DISCORD_COLORS["neutral"]


def discord_payload(event: Event) -> dict:
    """The body of a Discord-format webhook: one embed, with no mention parsed.

    ``allowed_mentions.parse`` is empty, so nothing in a filename or an error
    text can ping @everyone or a role. Every limit is met by truncating.
    """
    title = _clip(event.title, 256)
    fields = []
    for key, value in event.data.items():
        if isinstance(value, (str, int, float, bool)) and len(fields) < _DISCORD_MAX_FIELDS:
            fields.append({
                "name": _clip(str(key).replace("_", " ").capitalize(), 256),
                "value": _clip(value, 1024) or "-", "inline": True,
            })
    used = len(title) + sum(len(f["name"]) + len(f["value"]) for f in fields)
    # Drop fields, not the message, if the embed would pass 6000 characters.
    while fields and used + 1 > _DISCORD_EMBED_TOTAL:
        dropped = fields.pop()
        used -= len(dropped["name"]) + len(dropped["value"])
    description = _clip(event.body, min(4096, _DISCORD_EMBED_TOTAL - used))
    return {
        "username": "Compressatorium",
        "content": _clip(event.title, 2000),
        "embeds": [{
            "title": title,
            "description": description,
            "color": _discord_color(event),
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        "allowed_mentions": {"parse": []},
    }


def _send_webhook(url: str, event: Event, secret: str | None = None) -> None:
    _post_json(url, generic_payload(event), secret)


def _send_discord(url: str, event: Event) -> None:
    _post_json(url, discord_payload(event))


def _post_json(url: str, body: dict, secret: str | None = None) -> None:
    """POST *body* as JSON under one overall deadline. The one HTTP send path of
    every webhook kind: only the payload differs between them. With *secret*,
    the raw body's HMAC-SHA256 goes in ``SIGNATURE_HEADER`` (#523)."""
    _check_http_url(url)
    parts = urlparse.urlsplit(url)
    payload = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json", "User-Agent": "compressatorium",
        "Host": parts.netloc.rsplit("@", 1)[-1],
    }
    if secret:
        digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        headers[SIGNATURE_HEADER] = f"sha256={digest}"
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
        conn.request("POST", target, body=payload, headers=headers)
        response = conn.getresponse()
        # Only the status matters, so the body is never read: after a
        # `Connection: close` response http.client detaches the socket from
        # `conn`, where the deadline's abort could not interrupt a slow body.
        status = response.status
        retry_after = response.getheader("Retry-After")
        response.close()
    except (OSError, http.client.HTTPException) as exc:
        if expired.is_set():
            raise NotificationError.said("The webhook did not finish in time") from None
        # str(exc) can echo the URL; the reason alone is safe.
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        raise NotificationError.said(
            "Could not reach the webhook: {reason}", reason=reason,
        ) from None
    finally:
        timer.cancel()
        conn.close()
    if status == 429:
        # Never retried here: the next event is the retry.
        if retry_after:
            raise NotificationError.said(
                "The webhook is rate limiting this destination (HTTP 429, retry after"
                " {wait} s)", wait=retry_after[:20],
            )
        raise NotificationError.said("The webhook is rate limiting this destination (HTTP 429)")
    if status >= 300:
        raise NotificationError.said("The webhook answered HTTP {status}", status=status)


def apprise_available() -> bool:
    return importlib.util.find_spec("apprise") is not None


def _send_apprise(url: str, event: Event) -> None:
    try:
        import apprise  # lazy: the webhook path never needs it
    except ImportError:
        raise NotificationError.said(_APPRISE_MISSING) from None
    notifier = apprise.Apprise()
    if not notifier.add(url):
        raise NotificationError.said("Apprise does not recognise that URL")
    # Apprise reports why a send failed only through its log; capture the last
    # warning so Test can say "error=401" or "connection error", not just "failed".
    for service in notifier:  # cap each plugin's own per-operation waits (not an overall deadline)
        for attr in ("socket_connect_timeout", "socket_read_timeout"):
            if hasattr(service, attr):
                setattr(service, attr, SEND_TIMEOUT_S * 0.4)
    with apprise.LogCapture(level=logging.WARNING) as capture:
        delivered = notifier.notify(title=event.title, body=event.body)
        captured = capture.getvalue()
    if not delivered:
        reason = _apprise_reason(captured, url)
        if reason:
            raise NotificationError.said(
                "Apprise could not deliver the message: {reason}", reason=reason,
            )
        raise NotificationError.said("Apprise could not deliver the message")


def _apprise_reason(captured: str, url: str) -> str:
    lines = [ln for ln in captured.splitlines() if " - WARNING - " in ln or " - ERROR - " in ln]
    if not lines:
        return ""
    return _redact(lines[-1].split(" - ", 2)[-1].strip(), url)[:200]


_APPRISE_MISSING = (
    "Apprise is not installed in this build, so Apprise URLs cannot be used. "
    "Use a webhook URL, or run the Docker image, which includes Apprise."
)

def _check_https_url(url: str) -> None:
    _check_http_url(url)
    if urlparse.urlsplit(url).scheme != "https":
        raise NotificationError.said("A Discord webhook URL must start with https://")


def _check_apprise_url(url: str) -> None:
    if not apprise_available():
        raise NotificationError.said(_APPRISE_MISSING)
    if "://" not in url:
        raise NotificationError.said("An Apprise URL looks like discord://id/token")


def _host_hint(url: str) -> str:
    return urlparse.urlsplit(url).hostname or ""


def _scheme_hint(url: str) -> str:
    return url.split("://", 1)[0] + "://" if "://" in url else ""


@dataclass(frozen=True)
class Kind:
    """One destination type. Adding one is a row in `_KINDS`; nothing else in
    this module (or the Settings form, which reads `kind_list()`) branches on it."""

    key: str
    label: str
    placeholder: str
    send: Callable[..., None]  # (url, event), plus the secret when it `signs`
    check: Callable[[str], None]  # raises NotificationError (run on save and on Test)
    hint: Callable[[str], str]  # what may be shown of a saved URL: never the secret
    signs: bool = False  # takes an optional HMAC signing secret (#523)


_KINDS: dict[str, Kind] = {k.key: k for k in (
    Kind("webhook", "Webhook (generic JSON)", "https://example.com/hook",
         _send_webhook, _check_http_url, _host_hint, signs=True),
    Kind("discord", "Discord webhook", "https://discord.com/api/webhooks/<id>/<token>",
         _send_discord, _check_https_url, _host_hint),
    Kind("apprise", "Apprise", "discord://webhook-id/token",
         _send_apprise, _check_apprise_url, _scheme_hint),
)}
KINDS = tuple(_KINDS)
TRANSPORTS: dict[str, Callable[..., None]] ={k: v.send for k, v in _KINDS.items()}


def kind_list() -> list[dict]:
    """The picker's rows, for Settings."""
    return [
        {"value": k.key, "label": k.label, "placeholder": k.placeholder,
         "available": k.key != "apprise" or apprise_available(), "signs": k.signs}
        for k in _KINDS.values()
    ]

# Transports run on `run_detached`'s throwaway daemon threads, not a thread pool:
# a pool's workers are joined at interpreter exit, so one send that never returns
# (a slow-drip server) would hold a CLI command open after its work was done, and
# a stuck pooled worker would starve every send queued behind it. A thread cannot
# be killed, so what bounds the cost is that each saved destination holds at most
# one send in flight (`_in_flight`), released when its thread really ends, plus
# run_detached's own process-wide ceiling on abandoned threads.
_in_flight: set[str] = set()


async def _send_one(
    kind: str, url: str, event: Event, slot: str | None = None, secret: str | None = None,
) -> None:
    """Send *event* through one transport, bounded by SEND_TIMEOUT_S. Raises.

    *secret* (a signing kind's, #523) is passed only when set, so an unsigned
    destination calls its transport exactly as ``send(url, event)``.

    *slot* names a saved destination: while its previous send's thread is still
    running (it outlived the timeout), a new send to it is refused rather than
    stacking another thread behind a dead endpoint.
    """
    if slot is not None:
        if slot in _in_flight:
            raise NotificationError.said("The previous send to this destination is still running")
        _in_flight.add(slot)

    def run() -> None:
        try:
            TRANSPORTS[kind](url, event, *([secret] if secret else []))
        finally:
            # Released when the thread really ends, not when we stop waiting.
            if slot is not None:
                _in_flight.discard(slot)

    try:
        await asyncio.wait_for(run_detached(run), SEND_TIMEOUT_S)
    except ProbeCapacityExceeded:
        if slot is not None:
            _in_flight.discard(slot)  # the thread never started
        raise NotificationError.said("Too many sends are stuck; try again later") from None
    except asyncio.TimeoutError:
        raise NotificationError.said("Timed out after {limit:g}s", limit=SEND_TIMEOUT_S) from None
    except NotificationError:
        raise
    except Exception as exc:
        # Whatever a transport raised, callers only ever see a NotificationError:
        # the type name, never the text (it can echo the destination URL). The
        # original rides along as __cause__ only; nothing logs a traceback of it.
        raise NotificationError.said("The send failed ({kind})", kind=type(exc).__name__) from exc


# ----------------------------------------------------------------------
# destination store
# ----------------------------------------------------------------------

_store_lock = asyncio.Lock()


# The destination list, read once and kept: every event used to cost a database
# read on a worker thread, even with no destination configured. Writers go
# through `_save`, which refreshes it.
_dest_cache: list[dict] | None = None


async def _load() -> list[dict]:
    global _dest_cache
    if _dest_cache is None:
        # Once per process, as one primary-key row read on the loop: a worker
        # thread here would be one more thread per cold start, for every sweep
        # error report on an install with no destination at all.
        stored = preferences_store._get_sync(PREF_KEY) or {}
        dests = stored.get("destinations")
        _dest_cache = (
            [d for d in dests if isinstance(d, dict)] if isinstance(dests, list) else []
        )
    return [dict(d) for d in _dest_cache]


async def _save(dests: list[dict]) -> None:
    global _dest_cache
    await preferences_store.put(PREF_KEY, {"destinations": dests})
    _dest_cache = [dict(d) for d in dests]


def _hint(dest: dict) -> str:
    """Where it goes, with nothing secret: the host, or just the Apprise scheme."""
    kind = _KINDS.get(dest.get("kind", ""))
    return kind.hint(dest.get("url", "")) if kind else ""


def _events_of(dest: dict) -> list[str]:
    """The events *dest* gets: its own choice, or the defaults if it never chose."""
    chosen = dest.get("events")
    return [k for k in chosen if k in _EVENTS] if isinstance(chosen, list) else _default_events()


def public_view(dest: dict) -> dict:
    """What the browser may see. Never the URL or the signing secret."""
    return {
        "id": dest["id"], "kind": dest["kind"],
        "label": dest.get("label") or _hint(dest), "hint": _hint(dest),
        "events": _events_of(dest), "signed": bool(dest.get("secret")),
    }


def _clean_events(events: list[str]) -> list[str]:
    unknown = [e for e in events if e not in _EVENTS]
    if unknown:
        raise NotificationError.said("Unknown event: {event}", event=unknown[0][:40])
    return list(dict.fromkeys(events))


def _clean_secret(kind: str, secret: str) -> str:
    """*secret* stripped, or NotificationError: only a signing kind takes one."""
    secret = secret.strip()
    if secret and not _KINDS[kind].signs:
        raise NotificationError.said("Only a generic webhook can have a signing secret")
    if len(secret) > MAX_SECRET_LEN:
        raise NotificationError.said(
            "The signing secret is longer than {limit} characters", limit=MAX_SECRET_LEN,
        )
    return secret


def validate(kind: str, url: str) -> str:
    """Return the cleaned URL or raise NotificationError."""
    if kind not in KINDS:
        raise NotificationError.said("Unknown destination type")
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LEN:
        raise NotificationError.said("Enter a URL")
    _KINDS[kind].check(url)
    return url


def _clean_label(label: str | None) -> str:
    return (label or "").strip()[:MAX_LABEL_LEN]


async def list_destinations() -> list[dict]:
    return [public_view(d) for d in await _load()]


async def add_destination(
    kind: str, url: str, label: str | None,
    events: list[str] | None = None, secret: str | None = None,
) -> dict:
    """Save a destination. *events* None means the defaults, now and as events
    are added; *secret* signs a generic webhook's body (#523)."""
    url = validate(kind, url)
    dest = {"id": uuid.uuid4().hex[:8], "kind": kind, "url": url, "label": _clean_label(label)}
    if events is not None:
        dest["events"] = _clean_events(events)
    secret = _clean_secret(kind, secret or "")
    if secret:
        dest["secret"] = secret
    async with _store_lock:
        dests = await _load()
        if len(dests) >= MAX_DESTINATIONS:
            raise NotificationError.said("At most {limit} destinations", limit=MAX_DESTINATIONS)
        await _save(dests + [dest])
    return public_view(dest)


async def update_destination(
    dest_id: str, url: str | None, label: str | None,
    events: list[str] | None = None, secret: str | None = None,
) -> dict | None:
    """Change a destination. None keeps a field; a *secret* of "" stops signing."""
    async with _store_lock:
        dests = await _load()
        for dest in dests:
            if dest["id"] == dest_id:
                if url is not None:
                    dest["url"] = validate(dest["kind"], url)
                if label is not None:
                    dest["label"] = _clean_label(label)
                if events is not None:
                    dest["events"] = _clean_events(events)
                if secret is not None:
                    dest["secret"] = _clean_secret(dest["kind"], secret)
                    if not dest["secret"]:
                        del dest["secret"]
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
        _drop_queue(dest_id)
        return True


async def test_destination(
    dest_id: str | None = None, kind: str | None = None, url: str | None = None,
    secret: str | None = None,
) -> dict | None:
    """Send a test message, whatever events the destination chose, signed as its
    real messages are. ``{"ok", "error"}``, or None for an unknown saved id."""
    if dest_id is not None:
        dest = next((d for d in await _load() if d["id"] == dest_id), None)
        if dest is None:
            return None
        kind, url, secret = dest["kind"], dest["url"], dest.get("secret")
    event = Event("test", "Compressatorium test message",
                  "If you can read this, notifications are working.")
    try:
        url = validate(kind or "", url or "")
        secret = _clean_secret(kind, secret or "")
        await _send_one(kind, url, event, secret=secret)
    except NotificationError as exc:
        return {"ok": False, "error": exc.redacted(url)}
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


# One inbox for the whole module, filled synchronously by `notify`, so the order
# events were raised in is the order every destination sees them in. A single
# dispatcher task loads the destinations and hands each event to each
# destination's own FIFO queue, one event at a time.
_inbox: deque = deque()
_dispatcher: asyncio.Task | None = None
_queues: dict[str, deque] = {}
_workers: dict[str, asyncio.Task] = {}


def _spawn(coro) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _submit(event: Event) -> asyncio.Future:
    """Put *event* in the inbox (no await, so call order is delivery order).
    The returned future resolves once every destination has dealt with it."""
    global _dispatcher
    done = asyncio.get_running_loop().create_future()
    _inbox.append((event, done))
    if _dispatcher is None or _dispatcher.done():
        _dispatcher = _spawn(_dispatch())
    return done


async def _dispatch() -> None:
    while _inbox:
        event, done = _inbox.popleft()
        try:
            dests = await _load()
        except Exception:
            logger.warning("Could not read notification destinations", exc_info=True)
            done.set_result(None)
            continue
        # No await between these: each destination's queue gets events in order.
        # Only to the destinations that chose this event (#523).
        waits = [_enqueue(d["id"], event) for d in dests if event.kind in _events_of(d)]
        if waits:
            _spawn(_resolve(waits, done))
        else:
            done.set_result(None)


async def _resolve(waits: list[asyncio.Future], done: asyncio.Future) -> None:
    await asyncio.gather(*waits)
    if not done.done():
        done.set_result(None)


def _enqueue(slot: str, event: Event) -> asyncio.Future:
    """Queue *event* behind whatever destination *slot* is already sending.

    Sends to one destination go one at a time, in order, so events close
    together all arrive. A destination whose transport outlived its deadline
    refuses the next send in `_send_one` instead of stacking another thread.
    """
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    queue = _queues.setdefault(slot, deque())
    if len(queue) >= MAX_QUEUED_PER_DESTINATION:
        dropped_event, dropped = queue.popleft()
        logger.warning(
            "Notification queue for destination %s is full; dropped the oldest message "
            "(%s: %s)", slot, dropped_event.kind, dropped_event.title,
        )
        if not dropped.done():
            dropped.set_result(None)
    queue.append((event, done))
    worker = _workers.get(slot)
    if worker is None or worker.done():
        _workers[slot] = _spawn(_drain_destination(slot))
    return done


def _log_failure(dest: dict, event: Event, exc: Exception) -> None:
    # Not exc_info and not str(exc) of a foreign exception: both can carry the
    # URL. Our own NotificationError texts are redacted too.
    reason = (
        exc.redacted(dest["url"]) if isinstance(exc, NotificationError)
        else type(exc).__name__
    )
    logger.warning(
        "Notification (%s) to %s destination %s failed: %s",
        event.kind, dest["kind"], dest["id"], reason,
    )


async def _drain_destination(slot: str) -> None:
    queue = _queues[slot]
    try:
        while queue:
            event, done = queue.popleft()
            try:
                # Read at send time: an edit applies to what is already queued,
                # and a destination deleted meanwhile is skipped.
                dest = next((d for d in await _load() if d["id"] == slot), None)
                if dest is not None:
                    try:
                        await _send_one(
                            dest["kind"], dest["url"], event, slot=slot,
                            secret=dest.get("secret"),
                        )
                    except NotificationError as exc:  # every send failure arrives as one
                        _log_failure(dest, event, exc)
            except Exception:
                logger.warning("Could not read notification destinations", exc_info=True)
            finally:
                if not done.done():
                    done.set_result(None)
    finally:
        if not queue:
            _queues.pop(slot, None)
        if _workers.get(slot) is asyncio.current_task():
            del _workers[slot]


def _drop_queue(slot: str) -> None:
    """A destination was removed: nothing queued for it is sent."""
    queue = _queues.get(slot)
    while queue:
        _, done = queue.popleft()
        if not done.done():
            done.set_result(None)


async def deliver(event: Event) -> None:
    """Send *event* to every destination and wait for it. Never raises."""
    await _submit(event)


def notify(event: Event) -> None:
    """Queue *event* for delivery and return at once. Call from the event loop."""
    try:
        _submit(event)
    except RuntimeError:
        pass  # no loop (a synchronous harness): nothing to send on


async def drain(timeout: float | None = None) -> None:
    """Wait for queued sends (tests, and a CLI run before its loop closes).
    With *timeout*, give up after that long and say what was left."""

    async def settle() -> None:
        loop = asyncio.get_running_loop()
        while True:
            # Only this loop's sends: a task left on an earlier loop (one
            # asyncio.run per CLI run, or per test) can never finish here, and
            # gathering it raises "belongs to a different loop".
            running = [t for t in _tasks if not t.done() and t.get_loop() is loop]
            if not running:
                return
            await asyncio.gather(*running, return_exceptions=True)

    if timeout is None:
        await settle()
        return
    try:
        await asyncio.wait_for(settle(), timeout)
    except asyncio.TimeoutError:
        logger.warning("Gave up waiting for notifications after %.0fs", timeout)


def attach(job_manager) -> None:
    """Register the terminal listener on *job_manager*. The server's lifespan and
    the CLI's job runner both call this, so the two cannot drift. Idempotent."""
    if on_job_finished not in job_manager._terminal_listeners:
        job_manager.add_terminal_listener(on_job_finished)


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


def job_succeeded_event(job: ConversionJob) -> Event:
    mode = getattr(job.mode, "value", str(job.mode))
    saved = ledger.job_saved_bytes(job)
    return Event(
        "job_succeeded", f"Job finished: {_file(job)}", f"{mode}: {_fmt_bytes(saved)} saved",
        {"job_id": job.id, "filename": _file(job), "mode": mode, "saved_bytes": saved},
    )


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
        self.sent = False
        self.token: str | None = None
        self.seen: set[str] = set()  # every job id counted, so an announcement twice is one
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
# Owner token -> the sweep that passes it to the queue. The queue call enqueues
# and then awaits, so a job can end (and its terminal listener run) before the
# call returns and `sweep_jobs_queued` runs. The token is set on each job as it
# is created, so the listener knows whose it is from the first moment.
_owners: dict[str, _Sweep] = {}
_reported_errors: set[tuple] = set()
_TERMINAL = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
_job_sweeps: dict[str, _Sweep] = {}


_counted: dict[str, None] = {}  # job ids a sweep counted, bounded (insertion order)
_COUNTED_MAX = 5000


def _remember_counted(job_id: str) -> None:
    _counted[job_id] = None
    while len(_counted) > _COUNTED_MAX:
        del _counted[next(iter(_counted))]


def sweep_owner() -> str:
    """Sweep hook 0: the owner token to pass to the queue call (``owner=``).
    Every job created with it is the sweep's, however fast it ends."""
    global _open_sweep
    if _open_sweep is None:
        _open_sweep = _Sweep()
    if _open_sweep.token is None:
        _open_sweep.token = uuid.uuid4().hex
        _owners[_open_sweep.token] = _open_sweep
    return _open_sweep.token


def sweep_jobs_queued(jobs: list[ConversionJob]) -> None:
    """Sweep hook 1: these jobs belong to the running sweep. Call with no await
    after the queue accepts them, so a job that ends at once is already claimed."""
    global _open_sweep
    if _open_sweep is None:
        _open_sweep = _Sweep()
    for job in jobs:
        if job.id in _open_sweep.early:  # already counted by the listener
            continue
        _open_sweep.jobs.append(job)
        _open_sweep.seen.add(job.id)
        # The job is the sweep's from here on, whatever its status, so its
        # terminal listener (which may not have run yet) never sends it alone.
        _job_sweeps[job.id] = _open_sweep
        _remember_counted(job.id)
        # A job that already ended is counted but not waited on.
        if getattr(job, "status", None) not in _TERMINAL:
            _open_sweep.pending.add(job.id)


def sweep_finished(
    result: dict, *, dry_run: bool = False, library_id: str | None = None,
) -> None:
    """Sweep hook 2: the pass is over. Sends when its jobs have all ended."""
    global _open_sweep
    sweep, _open_sweep = _open_sweep, None
    if dry_run:
        return
    sweep = sweep or _Sweep()
    if sweep.token is not None:
        _owners.pop(sweep.token, None)
    sweep.closed = True
    sweep.errors = [e for e in result.get("errors") or [] if isinstance(e, dict)]
    # A failing platform is due again every scheduler tick and records the same
    # error each time. Report an error once per (platform, reason); it is
    # forgotten only when a later sweep inspected the platform without it (below).
    global _reported_errors
    keys = {
        (library_id, e.get("platform_id"), str(e.get("error"))) for e in sweep.errors
    }
    sweep.has_new_errors = bool(keys - _reported_errors)
    # Forget an error only when this sweep looked at that platform and found it
    # clean. A platform the sweep skipped (a platform_ids filter, the queue cap
    # stopping early) says nothing, so its error stays reported. A sweep that
    # died has no "platforms" at all and clears nothing.
    checked = {p.get("platform_id") for p in result.get("platforms") or []
               if isinstance(p, dict)}
    # A platform that failed before its summary was written is in `errors`
    # instead, and was just as much inspected.
    checked |= {e.get("platform_id") for e in sweep.errors}
    finished = "platforms" in result
    # Keyed by Library too: each Library is swept in turn, and one Library's
    # clean sweep says nothing about another's errors.
    kept = {
        k for k in _reported_errors
        if k[0] != library_id
        or (k[1] is None and not finished)
        or (k[1] is not None and k[1] not in checked)
    }
    _reported_errors = kept | keys
    _maybe_send(sweep)


def _maybe_send(sweep: _Sweep) -> None:
    if not sweep.closed or sweep.pending or sweep.sent:
        return
    sweep.sent = True  # a late terminal listener must not send it a second time
    event = sweep.event()
    if event is not None:
        notify(event)


async def on_job_finished(job: ConversionJob) -> None:
    """``JobManager`` terminal listener. Cheap for anything that is not news."""
    if job.status not in _TERMINAL:
        return
    sweep = _job_sweeps.pop(job.id, None)
    owner = getattr(job, "_sweep_owner", None)
    if sweep is None and owner:
        # A sweep's job (decided by its owner token alone, never by its Source).
        claimed = _owners.get(owner)
        if claimed is not None:
            # Ended inside the queue call, before the sweep got the job back, or
            # announced a second time: counted once, never sent alone.
            if job.id not in claimed.seen:
                claimed.seen.add(job.id)
                claimed.early[job.id] = job
                _remember_counted(job.id)
                claimed.jobs.append(job)
            return
        if job.id in _counted or job.status != JobStatus.FAILED:
            return  # counted by a closed sweep already, or not news
        # Its sweep closed without ever receiving this job (the pass died after
        # the batch was accepted): its failure is its own news.
    if sweep is None and job.id in _counted:
        return
    if sweep is not None:
        sweep.pending.discard(job.id)
        _maybe_send(sweep)
    elif job.status == JobStatus.FAILED and _started_by_hand(job):
        notify(job_failed_event(job))
    elif job.status == JobStatus.COMPLETED and _started_by_hand(job):
        notify(job_succeeded_event(job))


def _started_by_hand(job: ConversionJob) -> bool:
    """Whether *job*'s mode is one a person starts. A check Rule's run (#305) is
    started by the scheduler, so its failure is not "a job you started"; the
    mode declares it (``job_manager.EXTERNAL_JOB_MODES``)."""
    from services.job_manager import started_by_hand

    return started_by_hand(job.mode)


def _reset_for_tests() -> None:
    global _open_sweep, _reported_errors, _dispatcher, _dest_cache
    _dest_cache = None
    _open_sweep = None
    _reported_errors = set()
    _dispatcher = None
    _inbox.clear()
    _job_sweeps.clear()
    _owners.clear()
    _counted.clear()
    _queues.clear()
    _workers.clear()
    _in_flight.clear()
    _tasks.clear()
