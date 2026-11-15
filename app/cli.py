"""``compressatorium``: convert and verify Images from a shell or cron (#306).

Run as ``python -m cli`` with ``app/`` on ``PYTHONPATH``; the Docker image
installs a ``compressatorium`` wrapper that does exactly that (and, started as
root, drops to the ``converter`` user through the image's entrypoint first).

The CLI starts no web server, but it is not a second conversion path. It
drives the same pieces the web UI does, in-process:

* finding Images: ``routes.files.scan_tree``, the recursive search walk, so
  subfolders and archive members are found the way the UI finds them;
* choosing a mode: ``platform_detect.format_route`` (``--to``) and
  ``routes.convert.plan_auto_targets`` (``--auto``), both through the registry;
* planning and queueing: ``routes.convert.prepare_batch`` / ``queue_prepared``,
  the batch route's own two halves, then the ``job_manager`` singleton runs the
  jobs (``_delete_check`` decides every delete; ``ledger.record`` writes every
  Ledger row);
* locking: the job manager's output locks are ``flock`` files in the shared lock
  directory (``<data dir>/locks``), so a running server and this process never
  write one output twice.

Exit status: 0 when everything finished or there was nothing to do, 1 when any
Image failed (or was cancelled), 2 on bad arguments.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from logging_setup import get_logger

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# `--compression default`: the tool's own default, not the saved web UI choice.
TOOL_DEFAULT = "default"

logger = get_logger("cli")


class UsageError(Exception):
    """A bad argument: printed to stderr, exit 2."""


@dataclass
class RunState:
    """What a SIGINT/SIGTERM should do at this point of the run."""

    jobs_started: bool = False
    stopping: bool = False


@dataclass
class Item:
    """One Image and what happened to it: one output line."""

    path: str
    explicit: bool = False
    platform: str | None = None
    detection: str | None = None
    mode: str | None = None
    compression: str | None = None
    status: str | None = None
    output: str | None = None
    check: str | None = None
    source_deleted: bool = False
    delete_source: bool = False
    # --delete-source was asked for and this Source is kept: the run reports it.
    kept_source: bool = False
    reason: str | None = None
    # A logical output whose bytes live in other files (a split PS3 ISO's
    # numbered parts): the mode that writes it, so its tool's verify_target
    # names the file to read. The line and the record use ``path``.
    logical_mode: str | None = None

    def convert_line(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "platform": self.platform,
            "mode": self.mode,
            "output": self.output,
            "check": self.check,
            "source_deleted": self.source_deleted,
            "reason": self.reason,
            "delete_source": self.delete_source,
            "detection": self.detection,
            "compression": self.compression,
        }


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compressatorium",
        description="Convert and verify game Images without the web UI.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    convert = sub.add_parser(
        "convert", help="convert Images, recursing into folders and archives",
    )
    convert.add_argument("paths", nargs="+", metavar="PATH")
    target = convert.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--to", metavar="FORMAT",
        help="output format (chd, rvz, cso, nsz, 7z, ...) or a mode id (createdvd)",
    )
    target.add_argument(
        "--auto", action="store_true",
        help="each Image to its Platform's default format; Conflicted and "
             "Unsorted Images are skipped",
    )
    convert.add_argument(
        "--delete-source", action="store_true",
        help="delete each Source once its output passes the strongest check "
             "the mode has (Round-trip where available)",
    )
    convert.add_argument(
        "--container-only", action="store_true",
        help="with --delete-source: delete on the container check alone",
    )
    convert.add_argument("--output-dir", metavar="DIR")
    convert.add_argument(
        "--duplicate", choices=("skip", "overwrite", "rename"), default="skip",
        help="when the output already exists (default: skip)",
    )
    convert.add_argument(
        "--compression", metavar="CODECS",
        help="compression for every Image (as in the web UI), or 'default' for "
             "each tool's own default; without it, the saved web UI choice",
    )
    convert.add_argument(
        "--no-recurse", action="store_true",
        help="only the top level of each folder",
    )
    convert.add_argument(
        "--no-archives", action="store_true",
        help="leave archives alone (no members, no archive Images)",
    )
    convert.add_argument(
        "--any-platform", action="store_true",
        help="with --to: do not narrow by Platform or skip Conflicted Images",
    )
    _common(convert)

    verify = sub.add_parser("verify", help="check Images are Intact")
    verify.add_argument("paths", nargs="+", metavar="PATH")
    verify.add_argument(
        "--since", metavar="AGE",
        help="skip Images checked within AGE (30d, 12h, 2w)",
    )
    _common(verify)
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would happen; change nothing in the Volumes or the database",
    )
    parser.add_argument("--json", action="store_true", help="one JSON line per Image")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="also log INFO messages to stderr (warnings always are)",
    )


_AGE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_AGE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_age(value: str) -> timedelta:
    match = _AGE.match(value or "")
    if not match:
        raise UsageError(f"--since takes an age such as 30d, 12h or 2w, not {value!r}")
    return timedelta(**{_AGE_UNITS[match.group(2).lower()]: int(match.group(1))})


def _absolute(path: str) -> str:
    if "::" in path:
        archive, member = path.split("::", 1)
        return f"{os.path.abspath(archive)}::{member}"
    return os.path.abspath(path)


def _outside_volumes(raw: str) -> UsageError:
    from config import settings

    volumes = ", ".join(settings.volumes) or "none"
    return UsageError(
        f"{raw} is outside the configured Volumes ({volumes}); "
        "set COMPRESSATORIUM_VOLUMES to include it"
    )


def _check_paths(paths: list[str]) -> list[str]:
    from utils.path_utils import is_within_configured_volumes

    checked = []
    for raw in paths:
        path = _absolute(raw)
        if not os.path.exists(path.split("::", 1)[0]):
            raise UsageError(f"{raw}: no such file or directory")
        if not is_within_configured_volumes(path):
            raise _outside_volumes(raw)
        checked.append(path)
    return checked


# --------------------------------------------------------------------------- #
# Finding Images
# --------------------------------------------------------------------------- #


def _discover(
    paths: list[str], *, for_verify: bool = False, recurse: bool = True,
    archives: bool = True, reads: frozenset[str] | None = None,
) -> list[Item]:
    """Every Image under ``paths``, named ones first-class, in walk order.

    A folder is walked with the web UI's search (``scan_tree``); a folder that
    is itself an Image (a PS3 disc folder) is taken whole. An archive named on
    the command line brings its convertible members along. An archive whose
    member listing hit the per-archive cap gets a line saying so. ``reads``
    (``--to``'s accepted extensions) also lists every file in a walked folder
    with one of them, which the tool-wide walk leaves out when the mode reads
    what its tool doesn't (a ``.chd`` for chdman's extract modes), in the same
    walk. A folder that can't be read is reported as skipped.
    """
    from routes.files import scan_tree
    from services.archive import archive_service
    from services.tools import registry

    found: dict[str, Item] = {}

    def add(path: str, explicit: bool) -> Item:
        if path not in found:
            found[path] = Item(path=path, explicit=explicit)
        return found[path]

    def truncated(archive: str) -> None:
        logger.warning("Archive %s lists more members than the cap; the rest were skipped",
                       archive)
        item = add(f"{archive}::*", False)
        item.status = "skipped"
        item.reason = "The archive holds more members than the listing cap; the rest were not read"

    for path in paths:
        if "::" not in path and os.path.isdir(path):
            if not for_verify and registry.tools_for_directory(path):
                add(path, True)
                continue
            unreadable: list[tuple[str, str]] = []
            files, members, cut = scan_tree(
                path, recurse, archives,
                purpose="verify" if for_verify else "convert",
                also_reads=reads, unreadable=unreadable,
                # A verify also lists a split output's numbered parts, which
                # become their one logical Image (``_to_logical``).
                also_include=_is_split_part if for_verify else None,
            )
            for folder, why in unreadable:
                item = add(folder, False)
                item.status = "skipped"
                item.reason = f"This folder can't be read: {why}"
            for entry in files:
                if (
                    for_verify and not entry.get("verifiable_by")
                    and not _is_split_part(entry["path"])
                ):
                    continue
                add(entry["path"], False)
            if not for_verify:  # a check reads files, never archive members
                for entry in members:
                    add(entry["path"], False)
                for archive in cut:
                    truncated(archive)
            continue
        add(path, True)
        if (
            not for_verify and archives and "::" not in path
            and archive_service.is_archive(path)
        ):
            listing = archive_service.list_archive_contents(
                path, include_meta=True, convertible_only=True,
            )
            for entry in listing["entries"]:
                add(f"{path}::{entry['internal_path']}", False)
            if listing.get("truncated"):
                truncated(path)
    if not archives:
        return [
            i for i in found.values()
            if i.explicit or not archive_service.is_archive(i.path.split("::", 1)[0])
        ]
    return list(found.values())


def _skip_outside_volumes(items) -> None:
    """Skip every discovered input outside the configured Volumes (#306).

    A folder walk follows a file symlink, so a link in a Volume can point
    anywhere. Run once on the final input set (after a verify adds its stored
    checks), before any tool is chosen: each discovered file, folder and
    archive (for a member) goes through the shared containment check the
    web's verify guard uses (``is_within_configured_volumes``, which resolves
    the path); one outside is never read, checked or recorded. Paths named on
    the command line were checked already (``_check_paths``).
    """
    from utils.path_utils import is_within_configured_volumes

    verdicts: dict[str, bool] = {}

    def inside(path: str) -> bool:
        if path not in verdicts:
            verdicts[path] = is_within_configured_volumes(path, treat_archives=False)
        return verdicts[path]

    for item in items:
        if item.status:
            continue
        if item.logical_mode:
            # A logical Image (a split ISO): every file the check reads, the
            # verify target and every part, named or not. One outside skips
            # the whole Image.
            read = _logical_files(item)
            if not read or not all(inside(path) for path in read):
                item.status = "skipped"
                item.reason = "It resolves outside the configured Volumes"
            continue
        if item.explicit:
            continue
        if not inside(item.path.split("::", 1)[0]):
            item.status = "skipped"
            item.reason = "It resolves outside the configured Volumes"


def _logical_files(item) -> list[str]:
    """The real files a logical Image's check reads: its producing tool's
    verify target and every split part (``companion_outputs``)."""
    from services.tools import registry

    mode = item.logical_mode
    try:
        target = registry.for_mode(mode).verify_target(item.path, mode)
    except (KeyError, OSError) as exc:
        logger.debug("No verify target for %s through %s: %s", item.path, mode, exc)
        return []
    files = [target] if target else []
    return files + [p for p in registry._companions(mode, item.path) if p not in files]


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


class Printer:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def line(self, data: dict) -> None:
        if self.as_json:
            print(json.dumps(data), flush=True)
            return
        text = f"{data['status']:<10} {data['path']}"
        if data.get("output"):
            text += f" -> {data['output']}"
        if data.get("check"):
            text += f" ({data['check'].replace('_', '-')})"
        if data.get("reason"):
            text += f": {data['reason']}"
        print(text, flush=True)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


def _open_db(dry_run: bool) -> bool:
    """Open the app's database the way the server does. False when there is none.

    A dry run never creates or migrates it: it reads an existing one (saved
    compression, stored checks) or goes without.
    """
    from services import db as _db

    from config import settings

    if dry_run:
        path = settings.db_path or str(Path(settings.data_dir) / "compressatorium.db")
        if not os.path.isfile(path):
            return False
        _db.init_engine(path, create_schema=False)
        return True
    # The web startup's own initialisation, legacy JSON import included.
    _db.init_app_database(settings)
    return True


async def _saved_compression(has_db: bool) -> dict:
    if not has_db:
        return {}
    from routes.preferences import CONVERSION_KEY
    from services.preferences_store import preferences_store

    try:
        return await preferences_store.get(CONVERSION_KEY) or {}
    except Exception:
        logger.warning("Could not read the saved compression; using tool defaults",
                       exc_info=True)
        return {}


# --------------------------------------------------------------------------- #
# convert
# --------------------------------------------------------------------------- #


def _validate_convert(args) -> None:
    from fastapi import HTTPException
    from routes.convert import normalize_compression
    from services.platform_detect import target_formats
    from services.tools import registry
    from utils.path_utils import is_within_configured_volumes

    if args.container_only and not args.delete_source:
        raise UsageError("--container-only only applies with --delete-source")
    if args.any_platform and args.auto:
        raise UsageError("--any-platform only applies with --to")
    if args.output_dir is not None:
        if not args.output_dir.strip():
            raise UsageError("--output-dir cannot be empty")
        args.output_dir = os.path.abspath(args.output_dir)
        if not is_within_configured_volumes(args.output_dir, treat_archives=False):
            raise _outside_volumes(args.output_dir)
    if args.compression is not None and args.compression.strip().lower() != TOOL_DEFAULT:
        try:
            args.compression = normalize_compression(args.compression)
        except HTTPException as exc:
            raise UsageError(f"--compression: {exc.detail}") from None
    if args.to is None:
        return
    key = args.to.strip().lower()
    modes = {spec.mode for spec in registry.mode_specs()}
    if key in modes:
        if args.delete_source and not registry.spec(key).supports_delete_on_verify:
            raise UsageError(f"{key} cannot delete its Source (--delete-source)")
        return
    formats = target_formats(registry)
    if (key if key.startswith(".") else f".{key}") not in formats:
        names = ", ".join(f.lstrip(".") for f in formats)
        raise UsageError(f"--to {args.to}: not a format or mode. Formats: {names}")


def _compression_for(mode: str, args, prefs: dict) -> tuple[str | None, str | None]:
    """``(compression, error)`` for one Image's mode.

    ``--compression default`` is the tool's own default; a value given on the
    command line must fit the mode; without the flag, the saved web UI choice
    is used when the mode takes it, as Auto does.
    """
    from fastapi import HTTPException
    from routes.convert import _validate_request_compression, checked_compression
    from services.tools import registry

    spec = registry.spec(mode)
    takes = spec.supports_compression or spec.supports_compression_level
    if args.compression is None:
        saved = prefs.get(spec.tool_id) if takes else None
        return checked_compression(mode, saved or None), None
    if args.compression.lower() == TOOL_DEFAULT or not takes:
        return None, None
    try:
        _validate_request_compression(spec, mode, args.compression)
    except HTTPException as exc:
        return None, f"--compression {args.compression}: {exc.detail}"
    return args.compression, None


def _route_to(items: list[Item], args, prefs: dict, ready: list[str]) -> list[Item]:
    """Pick each Image's mode for ``--to``; drop discovered Images out of scope.

    A Conflicted Image is skipped with its detection reason, as Auto does; a
    known Platform narrows the modes; an Unsorted Image goes ahead only to a
    mode whose command reads its extension (``format_candidates``).
    ``--any-platform`` (the ``CHD_MODE=cli`` alias) turns both Platform tests
    off. A mode whose tool cannot run here is skipped with Auto's reason.
    """
    from services.platform_detect import (
        CONFLICT,
        detect_platform,
        format_candidates,
        format_route,
        serves_platform,
    )
    from services.tools import registry

    target = args.to
    kept = []
    for item in items:
        if item.status:  # a line already decided in discovery
            kept.append(item)
            continue
        candidates = format_candidates(item.path, target, registry)
        if not item.explicit and not candidates:
            continue
        if args.any_platform:
            platform = None
        else:
            detection = detect_platform(item.path)
            item.platform, item.detection = detection.platform, detection.how
            if detection.how == CONFLICT:
                item.status = "skipped"
                item.reason = f"Platform conflict: {detection.reason}"
                kept.append(item)
                continue
            platform = detection.platform
            if not item.explicit and platform and not any(
                serves_platform(spec, platform, registry) for spec in candidates
            ):
                continue  # another Platform's Image: not in scope for this target
        item.mode, item.reason = format_route(item.path, target, platform, registry)
        tool = registry.for_mode(item.mode) if item.mode else None
        if tool is not None and tool.id not in ready:
            item.mode = None
            item.reason = f"{tool.display_name} isn't ready: {tool.not_ready_reason}"
        if item.mode:
            item.compression, error = _compression_for(item.mode, args, prefs)
            if error:
                item.status, item.reason = "failed", error
        else:
            item.status = "skipped"
        kept.append(item)
    return kept


async def _route_auto(items: list[Item], args, prefs: dict, ready: list[str]) -> list[Item]:
    from routes.convert import plan_auto_targets
    from services.platform_detect import auto_reads
    from services.tools import registry

    decided = [i for i in items if i.status]
    kept = [i for i in items if not i.status and (i.explicit or auto_reads(i.path, registry))]
    targets = await plan_auto_targets(
        [i.path for i in kept], args.output_dir, prefs, ready_tool_ids=ready,
    )
    for item, target in zip(kept, targets, strict=True):
        item.platform = target.detection.platform
        item.detection = target.detection.how
        item.mode = target.mode
        if not target.mode:
            item.status, item.reason = "skipped", target.skip_reason
        elif args.compression is not None:
            item.compression, error = _compression_for(target.mode, args, prefs)
            if error:
                item.status, item.reason = "failed", error
        else:
            item.compression = target.compression
    return decided + kept


def _decide_deletes(items: list[Item], args) -> None:
    from routes.convert import get_disallowed_archive_paths
    from services.tools import registry

    if not args.delete_source:
        return
    from utils.delete_plan import archive_kept_summary

    routed = [i for i in items if i.mode and not i.status]
    shared = get_disallowed_archive_paths([i.path for i in routed])
    for item in routed:
        if not registry.spec(item.mode).supports_delete_on_verify:
            item.reason = f"Source kept: {item.mode} cannot delete its Source"
        elif "::" in item.path and item.path.split("::", 1)[0] in shared:
            item.reason = "Source kept: several Images come from this archive"
        elif kept := archive_kept_summary(item.path):
            # Fail closed: an archive goes only when every member belongs to
            # this Image (the delete plan's own rule, which the web submit
            # hits too).
            archive = os.path.basename(item.path.split("::", 1)[0])
            item.reason = f"Source kept: {archive} {kept}"
        else:
            item.delete_source = True
            continue
        item.kept_source = True


async def _prepare_group(request, group: list[Item]):
    """``prepare_batch`` for one group; on a refusal, retry Image by Image.

    The planner refuses a whole batch for one file (a delete snapshot that
    cannot be built, a compression the mode refuses). Retried one at a time,
    only the Image at fault fails and the rest are planned. Returns the
    prepared batches.
    """
    from fastapi import HTTPException
    from routes.convert import prepare_batch

    try:
        return [await prepare_batch(request)]
    except HTTPException as exc:
        if len(group) == 1:
            group[0].status, group[0].reason = "failed", str(exc.detail)
            return []
    batches = []
    for item in group:
        batches += await _prepare_group(
            request.model_copy(update={"file_paths": [item.path]}), [item],
        )
    return batches


async def _prepare(items: list[Item], args) -> list[tuple]:
    """Plan every routed Image through the batch route's planner, by group.

    Returns ``[(PreparedBatch, {planned file path: Item}), ...]``. Images the
    planner leaves out are marked skipped with its reason (a file collapsed
    into another's output is dropped when it was only discovered, like a
    ``.bin`` beside its ``.cue``).
    """
    from models import BatchJobCreateRequest, ConversionMode, DuplicateAction

    groups: dict[tuple, list[Item]] = {}
    for item in items:
        if item.mode and not item.status:
            groups.setdefault(
                (item.mode, item.compression, item.delete_source), [],
            ).append(item)

    prepared = []
    # Every output this run will write, across all groups and re-planned
    # batches: two jobs for one output path are never queued in one run. The
    # planner collapses duplicates only within one batch, and a group re-planned
    # Image by Image (or two groups) would otherwise queue both -- with
    # --duplicate overwrite and --delete-source, both Sources deleted for one
    # surviving output.
    claimed: dict[str, Item] = {}
    for (mode, compression, delete), group in groups.items():
        request = BatchJobCreateRequest(
            file_paths=[i.path for i in group],
            mode=ConversionMode(mode),
            output_dir=args.output_dir,
            duplicate_action=DuplicateAction(args.duplicate),
            compression=compression,
            delete_on_verify=delete,
            container_only=bool(args.container_only and delete),
        )
        by_path: dict[str, Item] = {}
        for item in group:
            by_path[item.path] = item
            by_path.setdefault(os.path.realpath(item.path), item)
        for batch in await _prepare_group(request, group):
            for path, reason in batch.skipped.items():
                item = by_path.get(path)
                if item is None:
                    continue
                if not item.explicit and reason.startswith("Same output as"):
                    item.status = "dropped"
                else:
                    item.status, item.reason = "skipped", reason
            planned = {}
            kept_plans = []
            for plan in batch.plans:
                item = by_path.get(plan.file_path)
                key = os.path.realpath(plan.output_path)
                first = claimed.get(key)
                if first is not None:
                    if item is not None:
                        item.status = "skipped"
                        item.reason = f"Same output as {os.path.basename(first.path)}"
                    continue
                kept_plans.append(plan)
                if item is not None:
                    claimed[key] = item
                    item.output = plan.output_path
                    planned[plan.file_path] = item
            if kept_plans:
                prepared.append((dataclasses.replace(batch, plans=kept_plans), planned))
    return prepared


def _in_flight_window(settings) -> int:
    """How many of this run's jobs may be queued at once.

    Twice the conversion slots, so a slot never waits on this process, and
    never more than MAX_QUEUE_DEPTH when that is set.
    """
    window = max(2, 2 * max(1, int(getattr(settings, "max_concurrent_jobs", 1) or 1)))
    max_depth = max(0, int(getattr(settings, "max_queue_depth", 0) or 0))
    return min(window, max_depth) if max_depth else window


def _chunks(prepared: list[tuple], size: int) -> list[tuple]:
    """Split prepared batches so none queues more than ``size`` jobs at once."""
    if size <= 0:
        return prepared
    out = []
    for batch, planned in prepared:
        for start in range(0, len(batch.plans), size):
            out.append((
                dataclasses.replace(batch, plans=batch.plans[start:start + size]),
                planned,
            ))
    return out


async def _run_jobs(prepared: list[tuple], printer: Printer, state: RunState) -> None:
    """Queue the prepared batches, below the queue cap, and report each job.

    Leaves only once every job has run to its end, cleanup included
    (``JobManager.drain``): a job reports itself finished before it removes
    its temp dir.
    """
    from fastapi import HTTPException
    from routes.convert import queue_prepared
    from services import ledger
    from services.job_manager import job_manager

    from config import settings

    ended: dict = {}
    changed = asyncio.Event()

    def on_terminal(job) -> None:
        ended[job.id] = job
        changed.set()

    job_manager.add_terminal_listener(ledger.record)
    job_manager.add_terminal_listener(on_terminal)
    runner = asyncio.create_task(job_manager.process_queue())
    state.jobs_started = True
    watching: dict[str, Item] = {}
    pending: set[str] = set()

    def report() -> None:
        for job_id in [j for j in pending if j in ended]:
            pending.discard(job_id)
            _finish(watching[job_id], ended[job_id])
            printer.line(watching[job_id].convert_line())

    async def wait_for_an_end() -> None:
        changed.clear()
        report()
        if pending:
            await changed.wait()
        report()

    window = _in_flight_window(settings)
    try:
        for batch, planned in _chunks(prepared, window):
            # Each queued job holds a ticket in the queue a running server
            # shares, so only a small window is queued at a time (below
            # MAX_QUEUE_DEPTH when that is set) and web UI work can interleave.
            while (
                not state.stopping
                and job_manager.get_queue_depth() + len(batch.plans) > window
            ):
                await wait_for_an_end()
                # Never spin: if nothing pending can end, give the loop a turn.
                await asyncio.sleep(0 if pending else 0.2)
            if state.stopping:
                for plan in batch.plans:
                    item = planned.get(plan.file_path)
                    if item is not None:
                        item.status, item.reason = "cancelled", "Stopped before it was queued"
                        printer.line(item.convert_line())
                continue
            try:
                jobs = await queue_prepared(batch)
            except HTTPException as exc:
                for plan in batch.plans:
                    item = planned.get(plan.file_path)
                    if item is not None:
                        item.status, item.reason = "failed", str(exc.detail)
                        printer.line(item.convert_line())
                continue
            for job in jobs:
                item = planned.get(job.file_path)
                if item is not None:
                    watching[job.id] = item
                    pending.add(job.id)
            if state.stopping:
                # A stop that arrived while this batch was being queued: the
                # signal handler's cancel-all ran before these jobs existed.
                await job_manager.cancel_all_jobs()
            report()
        while pending:
            await wait_for_an_end()
        await job_manager.drain()
    finally:
        job_manager.remove_terminal_listener(on_terminal)
        # Cancelled before shutdown: a runner that has not started yet would
        # otherwise start the dispatcher after shutdown and wait forever.
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        await job_manager.shutdown()


def _finish(item: Item, job) -> None:
    item.status = getattr(job.status, "value", str(job.status))
    item.output = job.output_path or item.output
    item.check = job.check_passed
    item.source_deleted = bool(job.deleted_paths)
    if getattr(job, "warning", None):
        # Completed, but did less than asked: a Source kept because another
        # job was reading it. Transient, and the conversion succeeded, so it
        # is a warning on the line, not a failure (exit 0, as #307's run
        # rules do). Safety refusals (``kept_source``) still exit 1.
        item.reason = job.warning
    if job.error_message:
        item.reason = job.error_message


async def convert(args, printer: Printer, state: RunState) -> int:
    _validate_convert(args)
    paths = _check_paths(args.paths)
    from fastapi.concurrency import run_in_threadpool
    from services.tools import registry

    has_db = _open_db(args.dry_run)
    prefs = await _saved_compression(has_db)
    from services.platform_detect import target_reads_extensions

    # With --to, the walk lists what that target's modes accept; without it
    # (--auto), the tool-wide walk as before.
    reads = target_reads_extensions(args.to, registry) if args.to else None
    items = await run_in_threadpool(
        lambda: _discover(
            paths, recurse=not args.no_recurse, archives=not args.no_archives,
            reads=reads,
        ),
    )
    # Once, on the final input set, before any mode is chosen.
    await run_in_threadpool(_skip_outside_volumes, items)
    # The shared, bounded readiness probe the Auto route uses.
    ready = await registry.ready_tool_ids()
    if args.auto:
        items = await _route_auto(items, args, prefs, ready)
    else:
        items = await run_in_threadpool(_route_to, items, args, prefs, ready)
    _decide_deletes(items, args)
    prepared = await _prepare(items, args)

    for item in items:
        if item.status and item.status != "dropped":
            printer.line(item.convert_line())
    if args.dry_run:
        for _batch, planned in prepared:
            for item in planned.values():
                item.status = "planned"
                printer.line(item.convert_line())
    else:
        await _run_jobs(prepared, printer, state)
    failed = any(i.status in ("failed", "cancelled") or i.kept_source for i in items)
    return EXIT_FAILED if failed else EXIT_OK


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def _verify_tool(path: str, record: dict | None = None) -> tuple:
    """``(tool, None)`` to check ``path`` with, ``(None, reason)`` to skip it.

    ``(None, None)`` means no tool checks this kind of file at all. The order
    (#306, review r7):

    1. **A stored check** (``record``): this app checked the file before, so
       it goes back to the tool of the mode recorded in ``produced_meta``
       (makeps3iso for a PS3 ISO it built), else to the tool that claims its
       name, with no Platform narrowing: damage must fail, not vanish. A
       recorded mode whose tool cannot verify is reported, not failed.
    2. **The detected Platform**, where the extension is shared with a tool
       for another Platform (a generic ``.iso``): it picks among the
       claimants, and an Image no claimant serves is skipped as unsupported.
    3. **Skip**: a shared extension whose Platform cannot be determined is
       never handed to a guessed verifier.
    """
    from services.tools import registry

    if record is not None:
        return _route_stored(path, record, registry)
    tools = registry.tools_checking_path(path)
    if not tools:
        return None, None
    if _shared_across_platforms(path, tools, registry):
        from services.platform_detect import detect_platform

        platform = detect_platform(path).platform
        if not platform:
            return None, "Its Platform couldn't be determined, so no check was chosen"
        narrowed = [t for t in tools if _tool_serves(t, platform, registry)]
        if not narrowed:
            return None, f"No check for {platform} Images in this format"
        tools = narrowed
    best = registry.tool_for_verify(path)
    return (best if best in tools else tools[0]), None


def _route_stored(path: str, record: dict, registry) -> tuple:
    mode = (record.get("produced_meta") or {}).get("mode")
    if mode:
        try:
            tool = registry.for_mode(mode)
        except KeyError:
            tool = None
        if tool is not None:
            if registry.mode_supports_verify(mode):
                return tool, None
            return None, f"Its stored check came from {mode}, which can't be checked here"
    claimants = registry.tools_checking_path(path)
    best = registry.tool_for_verify(path)
    tool = best if best is not None else (claimants[0] if claimants else None)
    if tool is None:
        return None, "It has a stored check, but no tool can check this kind of file"
    # A record with no mode (a legacy one) never guesses among tools for
    # different Platforms (a generic .iso): the detected Platform decides,
    # and without one the file is skipped (the r7 order: stored mode, then
    # Platform, then skip). codex 4577381607.
    # Every tool whose verify reads the name: a stored check must reach its
    # tool even when the file no longer looks like that tool's (damage must
    # fail, not vanish), so this is wider than the content claim.
    from utils.path_utils import match_extension

    candidates = [
        t for t in registry.all() if match_extension(path, t.verify_extensions)
    ]
    # And every tool with a checking mode that writes this kind of file (a
    # PS3 ISO is makeps3iso's product though it declares no verify extension).
    for spec in registry.mode_specs():
        producer = registry.for_mode(spec.mode)
        if (
            producer not in candidates and spec.output_ext
            and match_extension(path, {spec.output_ext}) is not None
            and registry.mode_supports_verify(spec.mode)
        ):
            candidates.append(producer)
    candidates = candidates or claimants or [tool]
    if _shared_across_platforms(path, candidates, registry):
        from services.platform_detect import detect_platform

        platform = detect_platform(path).platform
        if not platform:
            return None, (
                "Its stored check names no producing mode and its Platform "
                "couldn't be determined, so no check was chosen"
            )
        narrowed = [t for t in candidates if _tool_serves(t, platform, registry)]
        if not narrowed:
            return None, f"No check for {platform} Images in this format"
        return (best if best in narrowed else narrowed[0]), None
    return tool, None


def _tool_slugs(tool) -> set:
    from services.platform_detect import canonical_slug

    return {canonical_slug(slug) for slug in tool.platform_slugs}


def _shared_across_platforms(path: str, tools: list, registry) -> bool:
    """Whether a tool for another Platform also reads ``path``'s extension.

    A generic ``.iso`` is (chdman's PS2 discs, Dolphin's GameCube discs); a
    ``.cso`` is not, though the chain tool also reads it, because the chain
    names no Platform of its own.
    """
    from utils.path_utils import match_extension

    claimed = set().union(*(_tool_slugs(t) for t in tools))
    for other in registry.all():
        if other in tools:
            continue
        if not match_extension(path, other.input_extensions | other.verify_extensions):
            continue
        if _tool_slugs(other) - claimed:
            return True
    return False


def _tool_serves(tool, platform: str, registry) -> bool:
    slugs = _tool_slugs(tool)
    if not slugs or platform in slugs:
        return True
    # A Platform no tool names rules nothing out (as `serves_platform` does).
    return not any(platform in _tool_slugs(t) for t in registry.all())


def _is_split_part(path: str) -> bool:
    """A numbered part of a split output (``registry.split_owner``)."""
    from services.tools import registry

    return registry.split_owner(path) is not None


def _to_logical(items: list[Item]) -> list[Item]:
    """Numbered parts become their one logical Image (#306), deduped."""
    from services.tools import registry

    out: list[Item] = []
    index: dict[str, Item] = {}
    for item in items:
        owner = None if item.status or "::" in item.path else registry.split_owner(item.path)
        if owner is not None:
            item = Item(path=owner[0], explicit=item.explicit, logical_mode=owner[1])
        key = os.path.realpath(item.path)
        if key in index:
            index[key].explicit = index[key].explicit or item.explicit
            continue
        index[key] = item
        out.append(item)
    return out


def _stored_logical(stored: str, record: dict) -> str | None:
    """The mode of a stored output whose own path is gone but whose producing
    tool still finds it (a split ISO's parts, through ``verify_target``)."""
    from services.tools import registry

    mode = (record.get("produced_meta") or {}).get("mode")
    if not mode:
        return None
    try:
        target = registry.for_mode(mode).verify_target(stored, mode)
    except (KeyError, OSError) as exc:  # a mode no longer registered, a dead disk
        logger.debug("Stored output %s not found through %s: %s", stored, mode, exc)
        return None
    return mode if target and target != stored and os.path.isfile(target) else None


def _with_stored_checks(items: list[Item], paths: list[str], records: list[dict]) -> list[Item]:
    """Add files under ``paths`` that have a stored check but no claimant now.

    Stored paths are real paths; each is spelled under the folder the user
    named, as the walk spells them.
    """
    # Both sides compared by real path: a stored path whose file is now a
    # symlink resolves to the same file the walk already listed.
    seen = {os.path.realpath(i.path) for i in items}
    for folder in paths:
        if "::" in folder or not os.path.isdir(folder):
            continue
        real = os.path.realpath(folder)
        for record in records:
            stored = record.get("chd_path") or ""
            if not stored.startswith(real + os.sep):
                continue
            if os.path.realpath(stored) in seen:
                continue
            logical_mode = None
            if not os.path.isfile(stored):
                # Gone as a file: still an Image when its producing tool finds
                # it elsewhere (a split ISO's numbered parts).
                logical_mode = _stored_logical(stored, record)
                if logical_mode is None:
                    continue
            seen.add(os.path.realpath(stored))
            items.append(Item(
                path=folder.rstrip(os.sep) + stored[len(real):], logical_mode=logical_mode,
            ))
    return items


def _checked_at(record: dict | None) -> datetime | None:
    if not record or not record.get("verified_at"):
        return None
    try:
        return datetime.fromisoformat(str(record["verified_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None


def _hold_for_check(path: str, also: list[str] | tuple = ()) -> list[str] | None:
    """Take the shared read lock jobs use on ``path`` and its companions.

    Held through the check and its record, so no other process can overwrite
    or replace the file meanwhile (#306). None, holding nothing, when a writer
    holds one of them.
    """
    from services.lock_manager import lock_manager
    from services.tools import registry

    held: list[str] = []
    try:
        for each in dict.fromkeys([path, *also, *registry.source_companions(path)]):
            if not lock_manager.acquire_input_lock(each):
                _let_go_of(held)
                return None
            held.append(each)
    except BaseException:
        # Taking one raised part way (an OSError on the lock dir, say): let go
        # of the ones already held before the error goes on.
        _let_go_of(held)
        raise
    return held


def _let_go_of(held: list[str]) -> None:
    from services.lock_manager import lock_manager

    for each in held:
        lock_manager.release_input_lock(each)


async def _verify_one(
    path: str, tool, has_db: bool, since, dry_run: bool, *, target: str | None = None,
    also_lock: list[str] | tuple = (),
) -> dict:
    """Check one Image. The line carries ``_stop`` when the run must end.

    ``target`` is the file the check reads when it isn't ``path`` (a split
    output's first part); the line and the record stay on ``path``.
    ``also_lock`` names more files the check depends on (a split output's
    logical path and every part), held the same way for the whole check.
    """
    read = target or path
    from fastapi.concurrency import run_in_threadpool
    from services.lock_manager import lock_manager
    from services.verification_store import verification_store

    line = {"path": path, "status": None, "tool": tool.id, "reason": None}
    if since is not None and has_db:
        checked = _checked_at(await verification_store.get_record(path))
        if checked is not None and checked >= datetime.now(timezone.utc) - since:
            line["status"] = "skipped"
            line["reason"] = f"Checked {checked.isoformat(timespec='seconds')}"
            return line
    if dry_run:
        # Read-only: look, take nothing.
        if any(lock_manager.is_locked(each) for each in (read, *also_lock)):
            line["status"], line["reason"] = "skipped", "Busy: another job is writing it"
        else:
            line["status"] = "planned"
        return line
    held = await run_in_threadpool(_hold_for_check, read, also_lock)
    if held is None:
        # A writer has it: no verdict either way, and what was stored stays.
        line["status"], line["reason"] = "skipped", "Busy: another job is writing it"
        return line
    try:
        return await _check_held(path, tool, line, verification_store, read=read)
    finally:
        # Every exit, a cancel or a stop included.
        _let_go_of(held)


async def _check_held(
    path: str, tool, line: dict, verification_store, *, read: str | None = None,
) -> dict:
    """The check and its record, run while ``_verify_one`` holds the file.
    ``read`` is the file checked; ``path`` the one recorded."""
    from services.subprocess_runner import collect_abandonment, stops_verify_batch

    read = read or path
    bound = await tool.verify_timeout(read)
    # The sink sees what the check wrote off on the way, including a detached
    # read left behind when this deadline cancels it; no result says so.
    with collect_abandonment() as abandoned:
        try:
            result = await asyncio.wait_for(tool.verify(read), timeout=bound or None)
        except asyncio.TimeoutError:
            result = {"error": f"Verification timed out after {bound}s"}
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            result = {"error": f"Verification could not run: {exc}"}
    if stops_verify_batch(result, abandoned):
        line["_stop"] = True
    if result.get("valid"):
        line["status"], line["reason"] = "intact", result.get("message")
        await verification_store.refresh_check(path)
        return line
    if result.get("uncheckable"):
        # The tool won't check it here (an archive safety rule refused it):
        # not a failure, and what was stored stays.
        line["status"], line["reason"] = "skipped", result.get("message")
        return line
    line["status"] = "failed"
    no_verdict = ("cancelled", "abandoned", "inconclusive")
    if "error" in result or any(result.get(flag) for flag in no_verdict):
        # No verdict: a timeout or stall, a file that could not be read, a tool
        # that would not run, a check cut short.
        # The file is not known bad, so what was stored stays.
        line["reason"] = result.get("error") or result.get("message")
        return line
    line["reason"] = result.get("message")
    # A real verdict: it is not Intact any more. A stored pass would be a false
    # badge, and `--since` would skip it next time.
    await verification_store.clear(path)
    return line


async def verify(args, printer: Printer, state: RunState) -> int:
    from services.subprocess_runner import BATCH_STOPPED_MESSAGE
    from services.tools import registry

    since = parse_age(args.since) if args.since else None
    paths = _check_paths(args.paths)
    from fastapi.concurrency import run_in_threadpool

    has_db = _open_db(args.dry_run)
    items = await run_in_threadpool(lambda: _discover(paths, for_verify=True))
    # A split output's numbered parts are one logical Image, named or found.
    items = await run_in_threadpool(_to_logical, items)
    from services.verification_store import verification_store

    if has_db:
        items = _with_stored_checks(items, paths, await verification_store.all_records())
    # Once, on the final input set, before any verifier is chosen.
    await run_in_threadpool(_skip_outside_volumes, items)
    failed = False
    # Asked once per tool per run, dry runs included: a probe can walk the
    # volumes (nsz looking for prod.keys), so never once per file.
    ready: dict[str, bool] = {}
    for item in items:
        if state.stopping:
            break
        if item.status:  # decided in discovery (a folder that can't be read)
            printer.line({"path": item.path, "status": item.status, "tool": None,
                          "reason": item.reason})
            continue
        record = await verification_store.get_record(item.path) if has_db else None
        target = None
        if item.logical_mode:
            tool = registry.for_mode(item.logical_mode)
            unsupported = None
            target = await run_in_threadpool(
                tool.verify_target, item.path, item.logical_mode,
            )
            if not registry.mode_supports_verify(item.logical_mode) or not target:
                tool, unsupported = None, "Its producing mode can't check it here"
        else:
            tool, unsupported = await run_in_threadpool(_verify_tool, item.path, record)
        if tool is None:
            if unsupported or item.explicit:
                printer.line({"path": item.path, "status": "skipped", "tool": None,
                              "reason": unsupported or "No tool checks this file"})
            continue
        if tool.id not in ready:
            ready[tool.id] = await registry.tool_is_ready(tool, purpose="verify")
        if not ready[tool.id]:
            # Nothing can be checked here: no verdict, so the stored check
            # stays. A file the user named is a failure to do what was asked;
            # inside a folder it is reported and the run goes on.
            printer.line({
                "path": item.path, "status": "skipped", "tool": tool.id,
                "reason": f"{tool.display_name} isn't ready: {tool.verify_not_ready_reason}",
            })
            failed = failed or item.explicit
            continue
        # A logical Image (a split ISO) also holds its logical path, which a
        # rebuild locks alone, and every part: an in-progress build makes the
        # check skip as busy rather than judge a half-written set.
        also_lock = (
            [item.path, *registry._companions(item.logical_mode, item.path)]
            if item.logical_mode else []
        )
        line = await _verify_one(
            item.path, tool, has_db, since, args.dry_run, target=target,
            also_lock=also_lock,
        )
        stop = line.pop("_stop", False)
        failed = failed or line["status"] == "failed"
        printer.line(line)
        if stop:
            # The shared stop rule (`stops_verify_batch`), as the web UI's
            # batch verify applies it.
            print(f"compressatorium: {BATCH_STOPPED_MESSAGE}", file=sys.stderr)
            return EXIT_FAILED
    return EXIT_FAILED if failed or state.stopping else EXIT_OK


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _logging(verbose: bool):
    """App logs to stderr (stdout carries the result lines) for one run."""
    root = get_logger()
    saved = (root.level, root.propagate)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO if verbose else logging.WARNING)
    root.propagate = False
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(saved[0])
        root.propagate = saved[1]


@contextlib.contextmanager
def _signals(state: RunState):
    """SIGINT, SIGTERM and SIGHUP for the whole run.

    The CLI is PID 1 under CHD_MODE=cli, and a closed terminal sends SIGHUP to
    a `docker exec -it` run; each is handled like SIGTERM.

    Before any job starts the run is cancelled at once; once jobs run they are
    cancelled through the job manager, nothing more is queued, and the run
    ends when they have.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def on_signal() -> None:
        state.stopping = True
        if not state.jobs_started:
            task.cancel()
            return
        from services.job_manager import job_manager

        asyncio.ensure_future(job_manager.cancel_all_jobs())

    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, on_signal)
            installed.append(sig)
    try:
        yield
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)


async def _run(args) -> int:
    from services.lock_manager import lock_manager

    printer = Printer(args.json)
    state = RunState()
    # A dry run takes no locks and removes no lock files: the lock directory
    # is shared with a server that may be running (#306). Tickets and slots are
    # never touched either, because a dry run queues nothing.
    readonly = lock_manager.read_only() if args.dry_run else contextlib.nullcontext()
    with _signals(state), readonly:
        if args.command == "convert":
            return await convert(args, printer, state)
        return await verify(args, printer, state)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help is 0, a bad argument 2
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    with _logging(args.verbose):
        from utils.process_limits import raise_open_file_limit

        raise_open_file_limit()
        try:
            return asyncio.run(_run(args))
        except UsageError as exc:
            print(f"compressatorium: error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("compressatorium: stopped", file=sys.stderr)
            return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
