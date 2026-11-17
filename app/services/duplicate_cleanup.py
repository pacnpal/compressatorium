"""A duplicate group's suggested keeper, and deleting its other copies (#389).

A ``duplicate`` Finding (#388) lists copies that hold one disc. This module
turns it into a cleanup, and never more than the user confirms:

* **The suggested keeper** (:func:`annotate`): Intact first (a passing check
  recorded after the copy last changed), then Recognized (a cached DAT or
  Hasheous match), then the smallest, then the first by path. Worked out from
  the database only, so listing the Findings touches no Volume. The reason
  shown names the rule that decided.
* **What may be deleted from.** Only a group whose key proves the copies
  hash-equal (:data:`HASH_EQUAL_KINDS`, ``raw:``), and not one the last run
  held as it was (a copy it could not see).
* **The plan** (:func:`plan`): for each copy but the keeper, the shared delete
  plan (``build_delete_plan``: a sheet's tracks, a split set's parts) and the
  Referrer changes that would point a playlist at the keeper instead. Blocked
  when any copy, or the keeper, is not the file the Finding showed (gone,
  changed, unreadable, or no longer holding the group's key), when a copy is a
  track a ``.cue``/``.gdi`` beside it lists, or when a delete would touch the
  keeper. The disk is read on a detached thread, bounded.
* **The delete** (:func:`delete`): the plan is made again and must be the one
  the user confirmed, path for path; then the job manager's own delete step
  runs for each copy under its locks
  (``JobManager.delete_duplicate_copies``). The Finding then lists what is
  left, or clears once one copy is.
"""

from __future__ import annotations

import asyncio
import functools
import os
from datetime import datetime
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services import content_keys, findings, image_sets, libraries, referrers
from services.content_keys import Identity
from services.job_manager import (
    KEPT_DELETE_PROBE_SECONDS,
    KEPT_DELETED,
    job_manager,
)
from services.referrers import ReferrerError
from services.subprocess_runner import (
    DeadlineExpired,
    ReadCancelled,
    run_detached_bounded,
)
from utils.delete_plan import build_delete_plan
from utils.path_utils import is_within_configured_volumes, lock_boundary_root

logger = get_logger("duplicate_cleanup")

# Key kinds whose equal keys prove equal content. Only these groups offer a
# delete; a group matched any looser way never does.
HASH_EQUAL_KINDS = frozenset({content_keys.RAW})
_INTACT_CHECKS = frozenset({"intact", "round_tripped"})


class CleanupRefused(Exception):
    """The plan or the delete may not go ahead: *status* is the HTTP answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ----------------------------------------------------------------------
# the suggested keeper (database only)
# ----------------------------------------------------------------------


def _key_kind(key: str) -> str:
    return str(key).split(":", 1)[0]


def _stamp_ns(stamp: str | None) -> int | None:
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return int(moment.timestamp() * 1_000_000_000)


def _is_intact(copy: dict, record: dict | None) -> bool:
    """Whether the copy's latest check passed and still describes it: the
    check was made after the copy last changed (Codex 4593499342).

    The latest check decides, so a Verify that passes after a change makes the
    copy eligible again. The fingerprint a conversion recorded
    (``produced_meta.output``) is not read here: a re-check keeps it as it was,
    so it would bar a file that changed and then passed a check. The delete
    reads the keeper in full anyway, so this is the gate, not the proof.
    """
    if not record or record.get("check_passed") not in _INTACT_CHECKS:
        return False
    checked = _stamp_ns(record.get("verified_at"))
    return checked is not None and copy.get("mtime_ns") is not None and (
        checked >= int(copy["mtime_ns"])
    )


def _failed(record: dict | None) -> bool:
    return bool(record) and record.get("check_passed") == "failed"


def keeper_problem(path: str, record: dict | None, copy: dict) -> str | None:
    """Why *path* may not be the copy kept, or None when it is proven (#389).

    A copy whose check failed never is. Otherwise it must be proven to decode
    to the image: a passing check since it last changed (:func:`_is_intact`),
    or a key computed from its whole content
    (``content_keys.key_reads_whole_content``). A key read from a header is
    only a claim, so such a copy needs a check first.
    """
    name = os.path.basename(path)
    if _failed(record):
        return f"{name} failed its check; a copy that failed is never kept"
    if _is_intact(copy, record) or content_keys.key_reads_whole_content(path):
        return None
    return (
        f"{name}'s content key comes from its header and it has not passed a "
        "check since it last changed: check it first (Verify), or keep another copy"
    )


def suggest_keeper(
    copies: list[dict], intact: set[str], recognized: set[str],
    *, proven: set[str] | None = None, failed: frozenset[str] | set[str] = frozenset(),
) -> tuple[str | None, str]:
    """``(the copy to keep, why)``. A copy that failed its check is never one.

    Intact first (a passed check outranks everything), then a copy whose key
    was read from its whole content (*proven*), then Recognized, then the
    smallest, then the first by path. None when every copy failed its check.
    """
    proven = set(intact) if proven is None else set(proven) | set(intact)
    candidates = [c for c in copies if c["path"] not in failed]
    if not candidates:
        return None, "every copy failed its check"
    ordered = sorted(candidates, key=lambda c: (
        c["path"] not in intact, c["path"] not in proven, c["path"] not in recognized,
        int(c.get("size") or 0), c["path"],
    ))
    keeper = ordered[0]
    path = keeper["path"]
    paths = {c["path"] for c in candidates}
    if path in intact and len(intact & paths) < len(paths):
        return path, "Intact: it passed its check"
    if path in proven and len(proven & paths) < len(paths):
        return path, "its content hash was read from the whole file"
    if path in recognized and len(recognized & paths) < len(paths):
        return path, "Recognized: a DAT or Hasheous names it"
    if any(int(c.get("size") or 0) > int(keeper.get("size") or 0) for c in candidates):
        return path, "the smallest copy"
    return path, "every copy is alike; the first by path"


def not_deletable(row: dict, run: dict | None) -> str | None:
    """Why a duplicate group offers no delete, or None when it does.

    *run* is the Library's last find-duplicates run, None when none is
    recorded: then whether this group was fully seen is unknown, so no delete.
    Nor without the run's record of every sheet's tracks (``sheet_tracks``):
    a copy a sheet elsewhere reaches through a link could not be ruled out.
    """
    cause = row.get("cause") or {}
    key = str(cause.get("key") or row.get("image_key") or "")
    if _key_kind(key) not in HASH_EQUAL_KINDS:
        return "its copies are not proven equal by a content hash"
    if run is None:
        return "no find-duplicates run is recorded for its Library; run one first"
    if run.get("sheet_tracks") is None:
        problem = run.get("sheet_tracks_problem")
        if problem:
            return (
                f"the last run could not tell which files are sheets' tracks ({problem})"
            )
        return (
            "the last run has no record of which files are sheets' tracks; "
            "run Find duplicates again"
        )
    if run.get("unlisted"):
        # A run that also kept a sheet-track record (one from before the
        # listing counted): the two must agree, so partial wins.
        return "the last run could not list every folder of its Library; run Find duplicates again"
    if key in set(run.get("held_keys") or ()):
        return "the last run could not see every copy; run Find duplicates again"
    if len(cause.get(findings.COPIES) or []) < 2:
        return "fewer than two copies are left"
    return None


def _last_run(library_id: str) -> dict | None:
    """*library_id*'s last find-duplicates run, None when none is recorded. Blocking."""
    from services import duplicates

    for run in duplicates.list_runs():
        if run.get("library_id") == library_id:
            return run
    return None


def _recorded_tracks(run: dict) -> frozenset[tuple[int, int]]:
    return frozenset((int(dev), int(ino)) for dev, ino in run.get("sheet_tracks") or ())


def annotate(rows: list[dict]) -> None:
    """Add ``keeper``, ``keeper_reason``, ``deletable`` and ``not_deletable``
    to each duplicate row. Blocking: database reads only, no Volume."""
    from services import duplicates
    from services.dat_store import dat_store
    from services.verification_store import verification_store

    groups = [row for row in rows if row.get("kind") == findings.DUPLICATE]
    if not groups:
        return
    paths = [
        copy["path"] for row in groups
        for copy in (row.get("cause") or {}).get(findings.COPIES) or []
    ]
    records = verification_store.records_blocking(paths)
    try:
        matches = dat_store.get_matches_batch(paths)
    except Exception:  # a hint for the suggestion only, never a failure
        logger.warning("duplicate_cleanup: could not read the match cache", exc_info=True)
        matches = {}
    runs = {run["library_id"]: run for run in duplicates.list_runs()}
    for row in groups:
        copies = list((row.get("cause") or {}).get(findings.COPIES) or [])
        intact = {c["path"] for c in copies if _is_intact(c, records.get(c["path"]))}
        proven = {c["path"] for c in copies if content_keys.key_reads_whole_content(c["path"])}
        failed = {c["path"] for c in copies if _failed(records.get(c["path"]))}
        recognized = {c["path"] for c in copies if (matches.get(c["path"]) or {}).get("matched")}
        row["keeper"], row["keeper_reason"] = (
            suggest_keeper(copies, intact, recognized, proven=proven, failed=failed)
            if copies else (None, None)
        )
        keeper = next((c for c in copies if c["path"] == row["keeper"]), None)
        # Shown beside the suggestion: what must happen before it can be kept.
        row["keeper_problem"] = (
            keeper_problem(keeper["path"], records.get(keeper["path"]), keeper)
            if keeper else row["keeper_reason"]
        )
        row["not_deletable"] = (
            not_deletable(row, runs.get(row["library_id"]))
            if row["keeper"] else row["keeper_reason"] or "no copy can be kept"
        )
        row["deletable"] = row["not_deletable"] is None


# ----------------------------------------------------------------------
# the plan
# ----------------------------------------------------------------------


def _keeper_problem(keep: str, copies: list[dict]) -> str | None:
    """:func:`keeper_problem` for the chosen copy, from its stored check. Blocking."""
    from services.verification_store import verification_store

    copy = next(c for c in copies if c["path"] == keep)
    return keeper_problem(keep, verification_store.records_blocking([keep]).get(keep), copy)


def _shown(finding_id: int, fingerprint: str) -> dict:
    row = findings.get_finding(finding_id)
    if row is None or row["kind"] != findings.DUPLICATE:
        raise CleanupRefused(404, "Duplicate Finding not found")
    if row["fingerprint"] != fingerprint:
        raise CleanupRefused(409, "This Finding changed since it was listed; list it again")
    return row


def _copy_problem(copy: dict, key: str) -> str | None:
    """Why *copy* is not the file the Finding showed, holding *key*. Blocking."""
    path = copy["path"]
    if not is_within_configured_volumes(path, treat_archives=False):
        return "is outside the configured Volumes"
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "is gone"
    except OSError as exc:
        return f"could not be read ({exc.strerror or exc})"
    identity = Identity(size=int(st.st_size), mtime_ns=int(st.st_mtime_ns))
    if (identity.size, identity.mtime_ns) != (copy.get("size"), copy.get("mtime_ns")):
        return "changed since the Finding was recorded"
    stored = content_keys.stored({path: identity}).get(path)
    if stored is None or stored.key != key:
        return "no longer holds this group's content key"
    return None


def _sheet_problem(path: str) -> str | None:
    """Why *path* may be a sheet's track, or None when no sheet lists it. Blocking.

    Through the shared ``image_sets.owning_sheet``: *path*'s folder and every
    folder above it up to its Volume boundary (``lock_boundary_root``), since a
    sheet may name a track in a subfolder. Fails closed: a folder on the way
    that cannot be listed, or a ``.cue``/``.gdi`` that cannot be read whole,
    leaves "is this a track?" unanswered, which is never taken as
    "standalone"; so does a path with no Volume boundary.
    """
    name = os.path.basename(path)
    boundary = lock_boundary_root(path)
    if boundary is None:
        return f"{name} is outside the configured Volumes"
    try:
        sheet = image_sets.owning_sheet(path, stop_at=str(boundary))
    except image_sets.SheetOwnerUnknown as exc:
        return f"{exc}, so whether a sheet needs {name} is unknown"
    if sheet is None:
        return None
    return (
        f"{name} is a track of {os.path.basename(sheet)}; it is never deleted on its own"
    )


def _copy_sheet_problem(
    path: str, recorded: frozenset[tuple[int, int]] = frozenset(),
) -> str | None:
    """Why *path* may be a sheet's track, or None. Blocking.

    The walk up from it (:func:`_sheet_problem`), and the last run's record of
    every sheet's track identities in the Library: a sheet in another folder
    that reaches this file through a linked track (Codex PRRT_kwDOQ9nULM64NUn9).
    """
    problem = _sheet_problem(path)
    if problem:
        return problem
    name = os.path.basename(path)
    try:
        st = os.stat(path)
    except OSError as exc:
        return f"{name} could not be read ({exc.strerror or exc})"
    if (st.st_dev, st.st_ino) in recorded:
        return (
            f"{name} is a track a sheet in this Library names (through a link); "
            "it is never deleted on its own"
        )
    return None


def _plan_blocking(row: dict, keep: str, recorded: frozenset = frozenset()) -> dict:
    """The plan, from the disk. Blocking: run detached and bounded."""
    cause = row.get("cause") or {}
    key = str(cause.get("key") or "")
    copies = list(cause.get(findings.COPIES) or [])
    keeper = next(c for c in copies if c["path"] == keep)
    problem = _copy_problem(keeper, key)
    if problem:
        raise CleanupRefused(409, f"The copy to keep, {os.path.basename(keep)}, {problem}")
    keep_real = os.path.realpath(keep)
    items = []
    for copy in copies:
        if copy["path"] == keep:
            continue
        item = build_delete_plan(copy["path"], confine_to_volumes=True)
        item.setdefault("errors", [])
        problem = _copy_problem(copy, key)
        if problem:
            item["errors"].append(f"{os.path.basename(copy['path'])} {problem}")
        sheet = _copy_sheet_problem(copy["path"], recorded)
        if sheet:
            item["errors"].append(sheet)
        if keep_real in {os.path.realpath(p) for p in item.get("delete_paths") or []}:
            item["errors"].append("Deleting it would delete the copy to keep")
        item["identity"] = [copy.get("size"), copy.get("mtime_ns")]
        items.append(item)
    return {"keeper_identity": [keeper.get("size"), keeper.get("mtime_ns")], "items": items}


async def plan(finding_id: int, fingerprint: str, keep: str) -> dict:
    """What deleting every copy but *keep* would remove, for the user to confirm."""
    shown, _recorded = await _plan(finding_id, fingerprint, keep)
    return shown


async def _plan(
    finding_id: int, fingerprint: str, keep: str,
) -> tuple[dict, frozenset[tuple[int, int]]]:
    """:func:`plan`, and the run's record of sheet tracks it was checked against."""
    row = await run_in_threadpool(_shown, finding_id, fingerprint)
    copies = (row.get("cause") or {}).get(findings.COPIES) or []
    if keep not in {c["path"] for c in copies}:
        raise CleanupRefused(400, "The copy to keep is not one of this group's copies")
    run = await run_in_threadpool(_last_run, row["library_id"])
    reason = not_deletable(row, run)
    if reason:
        raise CleanupRefused(409, f"No delete is offered for this group: {reason}")
    recorded = _recorded_tracks(run)
    problem = await run_in_threadpool(_keeper_problem, keep, copies)
    if problem:
        raise CleanupRefused(409, problem)
    library = await run_in_threadpool(libraries.get, row["library_id"])
    if library is None:
        raise CleanupRefused(409, "This Finding's Library is gone")
    try:
        found = await run_detached_bounded(
            _plan_blocking, row, keep, recorded, timeout=KEPT_DELETE_PROBE_SECONDS,
        )
    except (DeadlineExpired, ReadCancelled):
        raise CleanupRefused(503, "The copies' Volume did not answer in time") from None
    total = 0
    blocked = False
    for item in found["items"]:
        await _add_referrer_changes(item, keep, library)
        total += len(item.get("delete_paths") or [])
        if item.get("errors") or item.get("unsafe_paths") or item.get("missing_paths"):
            blocked = True
    return {
        "finding_id": finding_id, "fingerprint": fingerprint, "keep": keep,
        "key": str((row.get("cause") or {}).get("key") or ""),
        "keeper_identity": found["keeper_identity"],
        "items": found["items"], "blocked": blocked, "total_delete_count": total,
    }, recorded


async def _add_referrer_changes(item: dict, keep: str, library: Any) -> None:
    """The Referrer changes the delete step would make: a playlist naming the
    copy is pointed at the keeper (as a conversion points one at its output)."""
    item["referrer_changes"] = []
    delete_paths = item.get("delete_paths") or []
    if not delete_paths:
        return
    try:
        changes = await referrers.plan_changes_bounded(
            item["source_path"], {path: keep for path in delete_paths}, library,
        )
    except (ReferrerError, OSError, RuntimeError, ValueError, asyncio.TimeoutError) as exc:
        logger.warning("duplicate_cleanup: planning the Referrers failed: %s", exc)
        item.setdefault("warnings", []).append(
            "Couldn't check the playlists that name this copy; it will be kept",
        )
        return
    item["referrer_changes"] = [change.to_dict() for change in changes]


# ----------------------------------------------------------------------
# the delete
# ----------------------------------------------------------------------


async def delete(
    finding_id: int, fingerprint: str, keep: str, confirmed: list[str],
) -> dict:
    """Delete every copy but *keep*, as the confirmed plan listed them."""
    shown, recorded = await _plan(finding_id, fingerprint, keep)
    if shown["blocked"]:
        reasons = [e for item in shown["items"] for e in item.get("errors") or []]
        detail = "; ".join(reasons or ["see the plan"])
        raise CleanupRefused(409, f"The delete is blocked: {detail}")
    planned = sorted(p for item in shown["items"] for p in item.get("delete_paths") or [])
    if planned != sorted(confirmed):
        raise CleanupRefused(409, "The delete plan changed since it was shown; review it again")
    outcomes, keeper_changed = await job_manager.delete_duplicate_copies(
        keeper=keep, keeper_identity=tuple(shown["keeper_identity"]),
        keeper_key=shown["key"],
        copy_problem=functools.partial(_copy_sheet_problem, recorded=recorded),
        copies=[
            (item["source_path"], list(item["delete_paths"]), tuple(item["identity"]))
            for item in shown["items"]
        ],
    )
    gone = {path for path, outcome in outcomes if outcome.status == KEPT_DELETED}
    await run_in_threadpool(_settle, finding_id, gone, fingerprint)
    return {
        "keep": keep,
        # Set when the copy kept no longer looked like the file that was
        # hashed once the last delete was done: the user should check it.
        "keeper_changed": keeper_changed,
        "results": [
            {"path": path, "status": outcome.status, "reason": outcome.reason,
             "deleted": list(outcome.deleted)}
            for path, outcome in outcomes
        ],
    }


def _settle(finding_id: int, gone: set[str], fingerprint: str) -> None:
    """The Finding after a delete: what is left of the group, or nothing.

    Only the Finding the delete was confirmed on (*fingerprint*): a run that
    wrote a newer cause meanwhile is left as it is, checked under the store's
    write lock.
    """
    from services import duplicates

    row = findings.get_finding(finding_id)
    if row is None:
        return
    library = libraries.get(row["library_id"])
    if library is None:
        return
    is_current = getattr(library, "is_current", None)

    def current() -> bool:
        return is_current() if is_current is not None else True

    cause = row.get("cause") or {}
    left = [c for c in cause.get(findings.COPIES) or [] if c["path"] not in gone]
    if row["fingerprint"] != fingerprint:
        return
    if len(left) < 2:
        findings.resolve(
            row["library_id"], row["image_key"], findings.DUPLICATE, current=current,
            fingerprint=fingerprint,
        )
        return
    findings.observe(
        row["library_id"], duplicates.finding_from_copies(str(cause.get("key")), left),
        current=current, fingerprint=fingerprint,
    )
