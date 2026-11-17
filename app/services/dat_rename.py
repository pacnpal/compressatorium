"""Rename single-file Images to their DAT name (#425).

A person picks Images and asks for their DAT names. Nothing here hashes:
the name comes from the match the app already has
(``DATStore.ranked_match_for_image``), and only a Recognized Image has one.

* **The preview** (:func:`preview`): for each Image, the old and new path,
  the DAT that names it and every Referrer update the rename makes (planned
  through the same hook and placement the rename uses, so "RomM: rename ROM
  12", "games.m3u", "gamelist.xml, 1 entry"), or a reason it is skipped: not
  Recognized, already named, the new name taken, an Image of more than one
  file (#426), a track of a sheet, or two equally ranked DATs that disagree.
  The new name is the top-ranked DAT's game name with the file's own
  extension, made safe by the one shared rule (``safe_file_name``).
* **A conflict** gets a ``rename_conflict`` Finding naming every DAT that
  disagrees; it clears once the ranking names the Image
  (:func:`settle_conflicts`, run when the ranking is saved and when the
  Findings are listed).
* **The run** (:func:`start_run`): an external job, like the duplicate
  delete. It plans every Image again: one whose plan is no longer the one
  confirmed is skipped and listed. Each confirmed Image goes through the
  shared rename (``image_rename.rename_image``). The job's result lists every
  change and every skip, and a summary goes out through the notify path.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from typing import Any

from logging_setup import get_logger
from models import ConversionMode
from services import (
    findings,
    image_rename,
    image_sets,
    libraries,
    notifications,
    referrers,
)
from services.dat_store import dat_store
from services.job_manager import job_manager
from services.referrers import ReferrerError
from services.subprocess_runner import DeadlineExpired, run_detached_bounded
from utils.path_utils import (
    lock_boundary_root,
    safe_file_name,
    same_file,
    volume_containment,
)

logger = get_logger("dat_rename")

RENAME = "rename"
SKIP = "skip"
# How many Images one preview or run may name.
MAX_IMAGES = 2000
# How long planning one Image may take: it reads the folders above it for a
# sheet that lists it, on a mount that may stop answering.
PLAN_SECONDS = 60.0
# The cap on Images listed by name in the notification.
MAX_LISTED = 20

# The job running now, if any: one rename at a time.
_running: str | None = None
_tasks: set[asyncio.Task] = set()


class RenameRefused(Exception):
    """The run may not start: *status* is the HTTP answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ----------------------------------------------------------------------
# planning one Image (blocking)
# ----------------------------------------------------------------------


def _ranked(path: str):
    """The ranked match for *path*, as listed or along its real path."""
    ranked = dat_store.ranked_match_for_image(path)
    if ranked.top is None and not ranked.conflict and not ranked.matches:
        real = os.path.realpath(path)
        if real != os.path.normpath(path):
            ranked = dat_store.ranked_match_for_image(real)
    return ranked


def _dat_of(record: dict) -> dict:
    return {
        "dat_name": record.get("dat_name") or "Hasheous",
        "family": record.get("family"),
        "game_name": record.get("game_name"),
    }


def _conflict_finding(path: str, conflict: Iterable[dict]) -> findings.Observed:
    matches = sorted(
        (_dat_of(m) | {"dat_id": m.get("dat_id")} for m in conflict),
        key=lambda m: (m["dat_name"] or "", m["game_name"] or ""),
    )
    named = " and ".join(f"{m['dat_name']} names it “{m['game_name']}”" for m in matches)
    return findings.Observed(
        kind=findings.RENAME_CONFLICT, image_key=path, path=path,
        reason=f"Not renamed: {named}, and the ranking puts them level; rank the DATs to choose",
        cause={"matches": matches},
    )


def _owner(library) -> tuple[str, Any]:
    """The Findings owner for an Image in *library*: its id and ``current``."""
    if library is None:
        return findings.NO_LIBRARY, None
    return library.id, getattr(library, "is_current", None)


def _note_conflict(path: str, library, conflict) -> None:
    """Record the rename-conflict Finding, or clear it once there is none.
    Bookkeeping: a write that fails is logged, never fails the plan."""
    owner, current = _owner(library)
    try:
        if conflict:
            findings.observe(owner, _conflict_finding(path, conflict), current=current)
        else:
            findings.resolve(owner, path, findings.RENAME_CONFLICT, current=current)
    except Exception:
        logger.warning("dat_rename: could not record the conflict of %s", path, exc_info=True)


def _sheet_problem(path: str) -> str | None:
    """Why *path* isn't a single-file Image of its own, or None."""
    if image_sets.is_playlist(path):
        return "a playlist, not an Image"
    if path.lower().endswith(image_sets.SHEET_EXTENSIONS) or image_sets.image_members(path):
        return "an Image of more than one file (a sheet and its tracks); not renamed yet"
    boundary = lock_boundary_root(path)
    if boundary is None:
        return "can't confirm its Volume boundary, so its sheet can't be looked for"
    try:
        sheet = image_sets.owning_sheet(path, stop_at=str(boundary))
    except image_sets.SheetOwnerUnknown as exc:
        return f"can't tell whether a sheet lists it as a track ({exc})"
    if sheet is not None:
        return f"a track of {os.path.basename(sheet)}: it is renamed with its sheet"
    return None


def _skip(item: dict, reason: str) -> dict:
    item.update(status=SKIP, reason=reason, new_path=None)
    return item


def plan_blocking(path: str) -> tuple[dict, Any]:
    """One Image's preview item (without its Referrer changes) and its
    Library. Blocking: reads the disk and the database."""
    item: dict = {
        "path": path, "new_path": None, "status": SKIP, "reason": None,
        "dat": None, "referrer_changes": [],
    }
    name = os.path.basename(path)
    if volume_containment(path, treat_archives=False) is not True:
        return _skip(item, "isn't confirmed inside the configured Volumes"), None
    if os.path.islink(path):
        return _skip(item, f"{name} is a symlink"), None
    if not os.path.isfile(path):
        return _skip(item, f"{name} is gone"), None
    problem = _sheet_problem(path)
    if problem:
        return _skip(item, problem), None
    try:
        library = libraries.library_for(path)
    except libraries.AmbiguousLibrary as exc:
        return _skip(item, str(exc)), None
    except OSError as exc:
        return _skip(item, f"can't tell which Library holds it ({exc})"), None
    ranked = _ranked(path)
    _note_conflict(path, library, ranked.conflict)
    if ranked.conflict:
        dats = " and ".join(sorted({_dat_of(m)["dat_name"] for m in ranked.conflict}))
        return _skip(item, f"{dats} name it differently and are ranked level; rank them"), library
    if ranked.top is None:
        return _skip(item, "not Recognized: no DAT or Hasheous match is known for it"), library
    item["dat"] = _dat_of(ranked.top)
    new_name = safe_file_name(f"{ranked.top.get('game_name') or ''}{os.path.splitext(name)[1]}")
    if new_name is None:
        return _skip(item, "its DAT name can't be made a file name"), library
    if new_name == name:
        return _skip(item, "already has its DAT name"), library
    new_path = os.path.join(os.path.dirname(path), new_name)
    if os.path.lexists(new_path):
        if same_file(new_path, path):
            return _skip(item, "differs from its DAT name only in letter case"), library
        return _skip(item, f"{new_name} already exists"), library
    try:
        refused = referrers.registry.rename_refused(library)
    except ReferrerError as exc:
        refused = f"{exc.target}: {exc.reason}"
    if refused:
        return _skip(item, refused), library
    item.update(status=RENAME, new_path=new_path)
    return item, library


async def _plan(path: str) -> tuple[dict, Any]:
    try:
        return await run_detached_bounded(plan_blocking, path, timeout=PLAN_SECONDS)
    except DeadlineExpired:
        item = {"path": path, "dat": None, "referrer_changes": []}
        return _skip(item, "its Volume didn't answer in time"), None


async def _with_referrer_changes(item: dict, library) -> dict:
    """The Referrer updates the rename makes, planned with the rename's own
    placement. One that can't be told skips the Image."""
    if item["status"] != RENAME:
        return item
    renames = {item["path"]: item["new_path"]}
    try:
        changes = await referrers.plan_changes_bounded(
            item["path"], renames, library, image_rename.placement_for(renames),
        )
    except (ReferrerError, OSError, RuntimeError, ValueError, asyncio.TimeoutError) as exc:
        reason = f"{exc.target}: {exc.reason}" if isinstance(exc, ReferrerError) else str(exc)
        return _skip(item, f"couldn't tell what points at it ({reason})")
    item["referrer_changes"] = [change.to_dict() for change in changes]
    return item


async def preview(paths: list[str]) -> dict:
    """Each Image's rename, or why it is skipped. Changes no file."""
    items = []
    # Each new name to the Image that takes it: the first one asked for wins.
    taken: dict[str, str] = {}
    for path in dict.fromkeys(paths):
        item, library = await _plan(path)
        new_path = item["new_path"]
        if new_path is not None and new_path in taken:
            other = os.path.basename(taken[new_path])
            _skip(item, f"{other} takes that name too, so this one is left as it is")
        elif new_path is not None:
            taken[new_path] = path
        items.append(await _with_referrer_changes(item, library))
    renames = sum(1 for item in items if item["status"] == RENAME)
    return {"items": items, "renames": renames, "skipped": len(items) - renames}


def settle_conflicts() -> int:
    """Clear each rename-conflict Finding the ranking now resolves (or that
    is no longer matched at all). Database only. Returns how many cleared."""
    cleared = 0
    for row in findings.list_findings(include_dismissed=True):
        if row["kind"] != findings.RENAME_CONFLICT:
            continue
        if _ranked(row["path"]).conflict:
            continue
        cleared += findings.resolve(
            row["library_id"], row["image_key"], findings.RENAME_CONFLICT,
            fingerprint=row["fingerprint"],
        )
    return cleared


# ----------------------------------------------------------------------
# the run
# ----------------------------------------------------------------------


async def start_run(items: list[dict]) -> dict:
    """Start renaming the confirmed preview *items* (``{path, new_path}``) as
    a background job; ``{job_id}``. One at a time."""
    global _running
    if _running is not None:
        raise RenameRefused(409, f"A rename to DAT names is already running ({_running})")
    count = len(items)
    job = job_manager.create_external_job(
        f"Rename {count} Image{'' if count == 1 else 's'} to their DAT names",
        ConversionMode.DAT_RENAME, message="Checking each Image again",
    )
    _running = job.id
    task = asyncio.get_running_loop().create_task(_run(job.id, [dict(i) for i in items]))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"job_id": job.id}


async def drain() -> None:
    """Wait for every running rename job (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _run(job_id: str, items: list[dict]) -> None:
    global _running
    try:
        result = await _rename_all(job_id, items)
    except Exception:
        logger.warning("dat_rename: the rename job %s failed", job_id, exc_info=True)
        await job_manager.finish_external_job(
            job_id, success=False, error_message="The rename stopped on an error",
        )
    else:
        job = job_manager.get_job(job_id)
        if job is not None:
            job.result = result
            job.message = _describe(result)
        notifications.notify(_event(result))
        if result["cancelled"]:
            await job_manager.finish_external_job_cancelled(job_id, message=_describe(result))
        else:
            await job_manager.finish_external_job(job_id, success=True)
    finally:
        if _running == job_id:
            _running = None


async def _rename_all(job_id: str, items: list[dict]) -> dict:
    results: list[dict] = []
    cancelled = False
    total = len(items)
    for index, confirmed in enumerate(items):
        path = str(confirmed.get("path") or "")
        if job_manager.is_cancelled(job_id):
            cancelled = True
            results.append(_result(path, None, "skipped", "cancelled before it was renamed"))
            continue
        await job_manager.update_external_job(
            job_id, progress=int(100 * index / total) if total else 0,
            message=f"[{index + 1}/{total}] {os.path.basename(path)}",
        )
        results.append(await _rename_one(job_id, path, confirmed.get("new_path")))
    counts = {
        status: sum(1 for r in results if r["status"] == status)
        for status in ("renamed", "skipped", "not_renamed")
    }
    return {"results": results, **counts, "cancelled": cancelled}


def _result(
    path: str, new_path: str | None, status: str, reason: str | None,
    referrer_records: list[dict] | None = None,
) -> dict:
    return {
        "path": path, "new_path": new_path, "status": status, "reason": reason,
        "referrers": list(referrer_records or []),
    }


async def _rename_one(job_id: str, path: str, confirmed: str | None) -> dict:
    item, library = await _plan(path)
    if item["status"] != RENAME:
        return _result(path, confirmed, "skipped", item["reason"])
    if confirmed != item["new_path"]:
        why = (
            "it wasn't to be renamed when the preview was shown" if confirmed is None
            else f"its DAT name changed since the preview (now {os.path.basename(item['new_path'])})"
        )
        return _result(path, confirmed, "skipped", why)
    outcome = await image_rename.rename_image(
        path, {path: item["new_path"]}, library=library,
        cancel_event=job_manager.get_cancel_event(job_id),
    )
    if outcome.renamed:
        return _result(path, item["new_path"], "renamed", None, outcome.records)
    return _result(path, item["new_path"], "not_renamed", outcome.reason, outcome.records)


def _describe(result: dict) -> str:
    text = f"Renamed {result['renamed']}"
    if result["skipped"]:
        text += f"; skipped {result['skipped']}"
    if result["not_renamed"]:
        text += f"; {result['not_renamed']} not renamed"
    if result["cancelled"]:
        text += "; cancelled"
    return text


def _event(result: dict) -> notifications.Event:
    title = (
        f"Rename to DAT name finished: {result['renamed']} renamed, "
        f"{result['skipped']} skipped, {result['not_renamed']} not renamed"
    )
    failed = [r for r in result["results"] if r["status"] == "not_renamed"]
    lines = [_describe(result) + "."]
    if failed:
        lines.append("Not renamed:")
        lines += [
            f"- {os.path.basename(r['path'])}: {r['reason']}" for r in failed[:MAX_LISTED]
        ]
        if len(failed) > MAX_LISTED:
            lines.append(f"- and {len(failed) - MAX_LISTED} more")
    return notifications.Event("rename_finished", title, "\n".join(lines), {
        "renamed": result["renamed"], "skipped": result["skipped"],
        "not_renamed": result["not_renamed"], "cancelled": result["cancelled"],
        "failures": [{"file": os.path.basename(r["path"]), "error": r["reason"]} for r in failed],
    })
