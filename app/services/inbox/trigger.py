"""Take a path in now (#522): the *arr webhook and ``POST /api/scan``.

Sonarr, Radarr and ROMarr say when a file has landed. Each path they name is
rewritten from how the *arr sees it to how this app does (the Autoscan
``from``/``to`` convention, saved in the UI), confirmed inside a Volume
(``volume_containment(...) is True``; "couldn't tell" refuses it too), and
handed to the Inbox whose folder holds it: :func:`intake.mark_ready` says it
is finished, and that Inbox is polled at once. There is no second pipeline.
The poll lists, checks, converts or copies, and archives exactly as the
scheduler's does, only without the settle wait for what was named.

A path imported straight into a Library (no Inbox holds it, a Library does,
#541) runs that Library's conversion Rule for the console folder it landed in,
now: the sweep's own checks (containment, the Rule's filters and duplicate
policy, a file already converted or in the queue) decide, as for Run now. Only
when the Library converts on its own (**Convert automatically**), the console's
Rule is on, and its hours allow; otherwise the path is skipped with why, and
the next scheduled run takes it. Never the console's check Rule.

A path nothing holds is reported with why, and with a rewrite suggested from
the Volumes when one would find it.
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
from services.platform_detect import canonical_slug
from services.preferences_store import preferences_store
from services.rules.library import LibraryUnreadable
from services.subprocess_runner import run_detached
from services.task_sets import drain_tasks
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
    await drain_tasks(_tasks)


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


def _library_for(local: str) -> tuple[Any, str | None]:
    """The Library that owns *local*, or ``(None, why)``. Blocking."""
    from services import libraries

    try:
        return libraries.library_for(local), None
    except libraries.AmbiguousLibrary as exc:
        return None, str(exc)
    except OSError as exc:
        return None, f"Couldn't tell which Library holds it: {exc}"


def _platform_ids(library: Any, local: str) -> list[str]:
    """The Library's console ids for the game at *local*. Blocking.

    Its console is what Platform detection says, the person's folder
    mappings included (`My discs` mapped to ps2 is ps2),
    matched on the console's id or slug. A console detection can't name
    at all (RomM's renamed folder) is RomM's own: the folder it sits in,
    matched on the console's folder. One whose header and folder disagree
    is neither, and nothing runs for it. Consoles that are one to detection
    (RomM's nes and famicom) are narrowed to the one whose folder holds it,
    or none.
    """
    from services import platform_detect

    detection = platform_detect.detect_platform(local)
    if detection.how == platform_detect.CONFLICT:
        return []
    slug = detection.platform if detection.decided else None
    if slug is None:
        folder = _held_folder(library, local)
        return [str(p.id) for p in library.platforms() if folder and p.folder == folder]
    matches = [
        p for p in library.platforms()
        if str(p.id) == slug or canonical_slug(p.slug) == slug
    ]
    if len(matches) > 1:
        folder = _held_folder(library, local)
        matches = [p for p in matches if folder and p.folder == folder]
    return [str(p.id) for p in matches]


def _held_folder(library: Any, local: str) -> str | None:
    """The name of the Library's console folder holding *local*, or None
    when none does or it can't be told. Blocking."""
    from services.rules.library import AmbiguousFolder

    try:
        held = library.platform_folder(local)
    except (AmbiguousFolder, OSError):
        return None
    return os.path.basename(held) if held else None


def check(path: str, rules: list[dict[str, str]]) -> dict[str, Any]:
    """What would happen to *path*: where it is here, the Inbox or Library
    that takes it, or the problem, with a suggested rewrite. Touches no
    file. Blocking."""
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
        library, why = (None, None) if box is not None else _library_for(local)
        if library is not None:
            result["library_id"] = library.id
            return result
        if box is None:
            result["problem"] = why or f"{local} isn't inside an Inbox or a Library"
        else:
            result["inbox_id"], result["library_id"] = box.id, box.library_id
            return result
    suggestion = suggest(path)
    if suggestion and rewrite(path, [suggestion]) != local:
        result["suggestion"] = suggestion
    return result


def _checked(
    path: str, rules: list[dict[str, str]],
) -> tuple[dict[str, Any], str | None, list[str]]:
    """:func:`check`, the real path of what an Inbox takes, and the console
    ids of a Library it landed in, worked out in the same bounded worker so
    nothing touches the disk on the event loop (Codex PR #526). Blocking."""
    from services import libraries

    found = check(path, rules)
    real = os.path.realpath(found["local"]) if found["inbox_id"] is not None else None
    ids: list[str] = []
    if found["inbox_id"] is None and found["library_id"] is not None:
        library = libraries.get(found["library_id"])
        ids = _platform_ids(library, found["local"]) if library is not None else []
    return found, real, ids


async def check_one(path: str) -> dict[str, Any]:
    """:func:`check`, and for a Library path the answer an import would get:
    never "works" for one the import would skip."""
    rules = await load_rewrites()
    try:
        found, _real, ids = await asyncio.wait_for(
            run_detached(_checked, path, rules), _STEP_SECONDS,
        )
    except (OSError, TimeoutError, ValueError, LibraryUnreadable) as exc:
        # A busy or unreadable Library is this path's answer, as in
        # `process`.
        return {"path": path, "local": rewrite(path, rules), "inbox_id": None,
                "library_id": None, "problem": f"Couldn't check it: {exc}",
                "suggestion": None}
    if found["inbox_id"] is None and found["library_id"] is not None:
        _library, _due, why = await _due_in_library(found, ids)
        if why:
            found = {**found, "problem": why}
    return found


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
    sweeps: dict[str, tuple[Any, set[str], list[str]]] = {}
    for path in paths[:MAX_PATHS]:
        try:
            found, real, ids = await asyncio.wait_for(
                run_detached(_checked, path, rules), _STEP_SECONDS,
            )
        except (OSError, TimeoutError, ValueError, LibraryUnreadable) as exc:
            found, real, ids = {"path": path, "local": path, "inbox_id": None,
                                "library_id": None, "problem": f"Couldn't check it: {exc}",
                                "suggestion": None}, None, []
        if found["inbox_id"] is None and found["library_id"] is not None:
            library, due, why = await _due_in_library(found, ids)
            if why:
                found = {**found, "problem": why}
            else:
                _library, due_ids, locals_ = sweeps.setdefault(library.id, (library, set(), []))
                due_ids.update(due)
                locals_.append(found["local"])
                accepted.append({"path": path, "local": found["local"], "inbox_id": None,
                                 "library_id": library.id, "platform_ids": due})
        _recent.appendleft({"at": _now(), "source": source, **found})
        if found["inbox_id"] is None:
            if found["problem"]:
                skipped.append(found)
            continue
        box = inbox_store.get(found["inbox_id"])
        if box is None:
            skipped.append({**found, "inbox_id": None, "problem": "The Inbox was removed"})
            continue
        intake.mark_ready(box.id, real, resolved=True)
        boxes[box.id] = box
        accepted.append({"path": path, "local": found["local"], "inbox_id": box.id})
    for library, _ids, locals_ in sweeps.values():
        # The Library's own hook (#353), once per Library: a walk listed before
        # these files arrived is dropped,
        # so the sweep sees them.
        library.note_paths_changed(locals_)
    jobs = [_poll_now(box) for box in boxes.values()]
    jobs += [_sweep_now(library, ids) for library, ids, _locals in sweeps.values()]
    for job in jobs:
        task = asyncio.create_task(job)
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return {"accepted": accepted, "skipped": skipped}


async def _due_in_library(
    found: dict[str, Any], ids: list[str],
) -> tuple[Any, list[str], str | None]:
    """``(library, console ids to convert now, None)`` for a path imported
    straight into a Library, or ``(library, [], why not)``. Fails closed: a
    console is converted only when the Library converts on its own, its
    conversion Rule is on, and now is inside its hours."""
    from services import libraries
    from services.rules import sweep as rules_sweep
    from services.rules.schedule import _in_window

    library = libraries.get(found["library_id"])
    if library is None:
        return None, [], "The Library was removed"
    if not library.schedule_enabled():
        return library, [], (
            f"Its automation is off in {library.name}: turn on Convert "
            "automatically to convert what lands there"
        )
    if not ids:
        return library, [], f"{found['local']} isn't in a console folder of {library.name}"
    stored = await rules_sweep.get_rules(library)
    on = [pid for pid in ids if stored.get(pid, {}).get("mode") and stored[pid].get("enabled")]
    if not on:
        return library, [], f"This console isn't set to convert in {library.name}"
    now = datetime.now(timezone.utc)
    due = [pid for pid in on if _in_window(stored[pid], now)]
    if not due:
        return library, [], (
            f"It's outside this console's hours in {library.name}: "
            "the next scheduled run takes it"
        )
    return library, due, None


async def _sweep_now(library: Any, platform_ids: set[str]) -> None:
    """Run *library*'s conversion Rules for *platform_ids* now, as Run now
    does for those consoles: never their check Rules."""
    from services.rules import sweep as rules_sweep
    from services.rules.rules import CONVERT

    try:
        await rules_sweep.sweep(
            library, platform_ids=sorted(platform_ids), ignore_schedule=True,
            kinds=(CONVERT,), honour_pauses=True,
        )
    except Exception:
        logger.warning("library: converting an import in %s failed", library.name, exc_info=True)


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
