"""Take a path in now (#522): the *arr webhook and ``POST /api/scan``.

Sonarr, Radarr and ROMarr say when a file has landed. Each path they name is
rewritten from how the *arr sees it to how this app does (the Autoscan
``from``/``to`` convention, saved in the UI), confirmed inside a Volume
(``volume_containment(...) is True``; "couldn't tell" refuses it too), and
handed to the Inbox whose folder holds it: :func:`intake.mark_ready` says it
is finished, and that Inbox is polled at once. There is no second pipeline.
The poll lists, checks, converts or copies, and archives exactly as the
scheduler's does, only without the settle wait for what was named. A path no
Inbox holds is reported with why, and with a rewrite suggested from the
Volumes when one would find it.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from logging_setup import get_logger
from services import inbox as inbox_store
from services.preferences_store import preferences_store
from services.subprocess_runner import run_detached
from utils.path_utils import volume_containment

from config import settings

logger = get_logger("inbox")

PREFERENCE_KEY = "arr_hook"
MAX_REWRITES = 32
MAX_PATHS = 500
# The events that mean "a file was imported". Others (Grab, Rename, Health,
# deletes) carry no new file to take.
IMPORT_EVENTS = frozenset({"download", "import"})
# How long one containment or lookup step may take before it is refused.
_STEP_SECONDS = 30
# A poll already running is waited for, then this poll runs: up to this long.
_RETRY_SECONDS = 1.0
_RETRIES = 600

_recent: deque[dict[str, Any]] = deque(maxlen=20)
_tasks: set[asyncio.Task] = set()


def reset_for_tests() -> None:
    _recent.clear()


async def drain() -> None:
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ---- the payload -----------------------------------------------------------


def imports(body: dict[str, Any]) -> bool:
    """Whether *body* names files to take: an import event, or no event at
    all (a script's bare ``{"path": ...}``)."""
    event = body.get("eventType")
    return event is None or str(event).lower() in IMPORT_EVENTS


def paths_in(body: dict[str, Any]) -> list[str]:
    """The file paths an *arr webhook (or a script) names, in order, once
    each: ``path``/``paths``, and the ``path`` of every ``*File`` object and
    ``*Files`` list (Sonarr's ``episodeFile(s)``, Radarr's ``movieFile``,
    ROMarr's own), so a new *arr needs no code here."""
    found: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in found:
            found.append(value)

    add(body.get("path"))
    for value in body.get("paths") or []:
        add(value)
    for key, value in body.items():
        if key.endswith("File") and isinstance(value, dict):
            add(value.get("path"))
        elif key.endswith("Files") and isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    add(item.get("path"))
    return found[:MAX_PATHS]


# ---- rewriting -------------------------------------------------------------


def _absolute(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("/"):
        raise ValueError(f"The {what} folder must be an absolute path: {text!r}")
    if ".." in PurePosixPath(text).parts:
        raise ValueError(f"The {what} folder can't contain '..': {text!r}")
    clean = os.path.normpath(text)
    if clean == "/":
        raise ValueError(f"The {what} folder can't be '/'")
    return clean


def checked_rewrites(rules: Any) -> list[dict[str, str]]:
    """*rules* as stored: each ``{"from", "to"}`` absolute, normalised, no
    ``..``, not ``/``, each ``from`` once. Raises ValueError."""
    if not isinstance(rules, list) or len(rules) > MAX_REWRITES:
        raise ValueError(f"Give at most {MAX_REWRITES} rewrites")
    out: list[dict[str, str]] = []
    for rule in rules:
        rule = rule if isinstance(rule, dict) else {}
        source, target = _absolute(rule.get("from"), "from"), _absolute(rule.get("to"), "to")
        if any(r["from"] == source for r in out):
            raise ValueError(f"{source} is rewritten twice")
        out.append({"from": source, "to": target})
    return out


def rewrite(path: str, rules: list[dict[str, str]]) -> str:
    """*path* with the longest matching ``from`` (whole folders only)
    replaced by its ``to``; unchanged when none matches."""
    for rule in sorted(rules, key=lambda r: len(r["from"]), reverse=True):
        source = rule["from"]
        if path == source:
            return rule["to"]
        if path.startswith(source + "/"):
            return rule["to"] + path[len(source):]
    return path


async def load_rewrites() -> list[dict[str, str]]:
    stored = await preferences_store.get(PREFERENCE_KEY) or {}
    try:
        return checked_rewrites(stored.get("rewrites") or [])
    except ValueError:
        return []


async def save_rewrites(rules: Any) -> list[dict[str, str]]:
    checked = checked_rewrites(rules)
    await preferences_store.put(PREFERENCE_KEY, {"rewrites": checked})
    return checked


def suggest(path: str) -> dict[str, str] | None:
    """A rewrite that would find *path* (as the *arr sees it) in a Volume:
    the Volume folder holding the same tail of folders, the most specific
    one. None when no Volume has it. Blocking (one stat per try)."""
    parts = PurePosixPath(path).parts[1:]
    for volume in settings.volumes:
        for start in range(len(parts) - 1):
            local = os.path.join(volume, *parts[start:])
            if not os.path.lexists(local):
                continue
            # The folders before the shared tail, plus the first shared one.
            source = "/" + "/".join(parts[:start + 1])
            target = os.path.join(volume, *parts[start:start + 1])
            return {"from": source, "to": os.path.normpath(target)}
    return None


# ---- one path --------------------------------------------------------------


def _inbox_for(local: str) -> inbox_store.Inbox | None:
    real = os.path.realpath(local)
    for box in inbox_store.all_inboxes():
        if real == box.folder or real.startswith(box.folder + os.sep):
            return box
    return None


def check(path: str, rules: list[dict[str, str]]) -> dict[str, Any]:
    """What would happen to *path*: where it is here, the Inbox that takes
    it, or the problem, with a suggested rewrite. Touches no file. Blocking."""
    local = rewrite(path, rules)
    result: dict[str, Any] = {
        "path": path, "local": local, "inbox_id": None, "library_id": None,
        "problem": None, "suggestion": None,
    }
    inside = volume_containment(local, treat_archives=False) if local.startswith("/") else False
    if inside is None:
        result["problem"] = "Couldn't confirm it is inside a Volume (a Volume isn't answering)"
    elif not inside:
        result["problem"] = f"{local} isn't inside a Volume"
    elif not os.path.lexists(local):
        result["problem"] = f"Nothing is at {local}"
    else:
        box = _inbox_for(local)
        if box is None:
            result["problem"] = f"{local} isn't inside an Inbox"
        else:
            result["inbox_id"], result["library_id"] = box.id, box.library_id
            return result
    suggestion = suggest(path)
    if suggestion and rewrite(path, [suggestion]) != local:
        result["suggestion"] = suggestion
    return result


async def check_one(path: str) -> dict[str, Any]:
    rules = await load_rewrites()
    return await asyncio.wait_for(run_detached(check, path, rules), _STEP_SECONDS)


# ---- taking paths in -------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def process(paths: list[str], *, source: str) -> dict[str, Any]:
    """Hand each of *paths* to the Inbox holding it and poll those Inboxes
    now, in the background. Returns what was accepted and what was skipped,
    with why. Never raises for one bad path."""
    from services.inbox import intake

    rules = await load_rewrites()
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    boxes: dict[int, inbox_store.Inbox] = {}
    for path in paths[:MAX_PATHS]:
        try:
            found = await asyncio.wait_for(run_detached(check, path, rules), _STEP_SECONDS)
        except (OSError, TimeoutError, ValueError) as exc:
            found = {"path": path, "local": path, "inbox_id": None, "library_id": None,
                     "problem": f"Couldn't check it: {exc}", "suggestion": None}
        _recent.appendleft({"at": _now(), "source": source, **found})
        if found["inbox_id"] is None:
            skipped.append(found)
            continue
        box = inbox_store.get(found["inbox_id"])
        if box is None:
            skipped.append({**found, "inbox_id": None, "problem": "The Inbox was removed"})
            continue
        intake.mark_ready(box.id, found["local"])
        boxes[box.id] = box
        accepted.append({"path": path, "local": found["local"], "inbox_id": box.id})
    for box in boxes.values():
        task = asyncio.create_task(_poll_now(box))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return {"accepted": accepted, "skipped": skipped}


async def _poll_now(box: inbox_store.Inbox) -> None:
    """Poll *box* now; when the scheduler's poll of it is running, wait for
    that one and poll again, so what was named is never left to the next
    tick. Then what runs after any pass (RomM's scan, the summary)."""
    from services.inbox import intake, report

    try:
        for _ in range(_RETRIES):
            result = await intake.poll(box)
            if result.get("error") != "poll_running":
                await report.after_pass([result])
                return
            await asyncio.sleep(_RETRY_SECONDS)
    except Exception:
        logger.warning("inbox: polling %s on request failed", box.folder, exc_info=True)


async def overview() -> dict[str, Any]:
    """The rewrites and the most recent paths received, newest first."""
    return {"rewrites": await load_rewrites(), "recent": list(_recent)}
