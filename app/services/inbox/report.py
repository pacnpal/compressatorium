"""What runs after an Inbox pass (#433): the Libraries are told, and one summary goes out.

A pass is one :func:`services.inbox.intake.poll_all`; each poll reports what
was placed in its Library since the last poll (``placed``) and what was left in
the Inbox with a Finding (``left``). :func:`after_pass` takes those results:

* each Library that had Images placed hears about them through its
  ``after_intake`` hook, once per pass (RomM is asked to scan just those
  Platform folders; a folder Library has no one to tell);
* if anything was placed or left, one ``inbox_pass`` notification goes out
  through the notify path, with the counts, the files and what the Library
  said (a RomM token without ``tasks.run`` is reported here). A pass that did
  nothing sends nothing.

The server's scheduler tick (``libraries.run_forever``) and ``run-rules`` both
call it, so the two cannot drift.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services import notifications

logger = get_logger("inbox")

# Registered with the others in ``services.notifications``.
EVENT = "inbox_pass"
# Files listed by name in one message; the rest are counted.
MAX_LISTED = 20


async def after_pass(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Tell each Library what a pass placed in it, then send the summary.

    Returns ``{"libraries": {library_id: {"name", "placed", "left", "scan"}}}``,
    holding only the Libraries whose Inboxes placed or left something. Never
    raises.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for result in results:
        placed = list(result.get("placed") or [])
        left = list(result.get("left") or [])
        if not (placed or left):
            continue
        entry = grouped.setdefault(
            result["library_id"],
            {"name": result["library_id"], "placed": [], "left": [], "scan": None},
        )
        entry["placed"] += placed
        entry["left"] += left
    for library_id, entry in grouped.items():
        library = await _library(library_id)
        if library is None:
            continue
        entry["name"] = getattr(library, "name", library_id)
        if entry["placed"]:
            entry["scan"] = await _tell(library, entry["placed"])
    if grouped:
        notifications.notify(event(grouped))
    return {"libraries": grouped}


async def _library(library_id: str):
    from services import libraries

    try:
        return await run_in_threadpool(libraries.get, library_id)
    except Exception:
        logger.warning("inbox: could not read the Library %s", library_id, exc_info=True)
        return None


async def _tell(library, placed: list[dict[str, Any]]) -> dict[str, Any] | None:
    hook = getattr(library, "after_intake", None)
    if hook is None:
        return None
    try:
        outcome = await hook(placed)
    except Exception as exc:
        logger.warning("inbox: %s's after_intake failed", library.id, exc_info=True)
        return {"status": "not_asked", "folders": [], "reason": f"it failed: {exc}"}
    return dict(outcome) if outcome else None


def _listed(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_LISTED:
        return lines
    return [*lines[:MAX_LISTED], f"- and {len(lines) - MAX_LISTED} more"]


def scan_text(scan: dict[str, Any] | None) -> str | None:
    """The scan outcome in a sentence, or None when nobody was asked."""
    if not scan:
        return None
    folders = ", ".join(scan.get("folders") or [])
    if scan.get("status") == "asked":
        return f"RomM was asked to scan {folders}"
    return f"RomM wasn't asked to scan {folders or 'anything'}: {scan.get('reason')}"


def event(grouped: dict[str, dict[str, Any]]) -> notifications.Event:
    """The ``inbox_pass`` message for one pass."""
    placed = sum(len(entry["placed"]) for entry in grouped.values())
    left = sum(len(entry["left"]) for entry in grouped.values())
    title = f"Inbox: {placed} placed, {left} left with a Finding"
    lines: list[str] = []
    missing_scope = False
    for entry in grouped.values():
        lines.append(f"{entry['name']}:")
        if entry["placed"]:
            lines.append(f"Placed {len(entry['placed'])}:")
            lines += _listed([
                f"- {os.path.basename(p['output'] or p['path'])} ({p['platform']})"
                for p in entry["placed"]
            ])
        if entry["left"]:
            lines.append(f"Left with a Finding {len(entry['left'])}:")
            lines += _listed([
                f"- {os.path.basename(item['path'])}: {item['reason']}" for item in entry["left"]
            ])
        said = scan_text(entry["scan"])
        if said:
            lines.append(said + ".")
        missing_scope = missing_scope or (entry["scan"] or {}).get("status") == "missing_scope"
    return notifications.Event(EVENT, title, "\n".join(lines), {
        "placed": placed,
        "left": left,
        "missing_scope": missing_scope,
        "failures": [
            {"file": os.path.basename(item["path"]), "error": item["reason"]}
            for entry in grouped.values() for item in entry["left"]
        ][:MAX_LISTED],
    })
