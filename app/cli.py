"""``compressatorium``: convert, verify and run Rules from a shell or cron (#306, #307).

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
  write one output twice;
* Rules (``run-rules``): ``rules.sweep.sweep``, the server scheduler's own
  sweep, once per Library from ``services.library_catalog``; the scheduler lock
  (``services.scheduler_lock``) keeps it from running beside a server.

Exit status: 0 when everything finished or there was nothing to do, 1 when any
Image failed (or was cancelled), 2 on bad arguments, 3 when ``run-rules`` finds
another scheduler (a server) using the data directory.
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
EXIT_LOCKED = 3

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
    reason: str | None = None

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
        description="Convert and verify game Images, and run Rules, without the web UI.",
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

    run_rules_cmd = sub.add_parser(
        "run-rules",
        help="one pass over the stored Rules, waiting for the jobs it queues",
    )
    run_rules_cmd.add_argument(
        "--library", metavar="ID", help="only this Library's Rules (romm, ...)",
    )
    _common(run_rules_cmd)
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
    archives: bool = True,
) -> list[Item]:
    """Every Image under ``paths``, named ones first-class, in walk order.

    A folder is walked with the web UI's search (``scan_tree``); a folder that
    is itself an Image (a PS3 disc folder) is taken whole. An archive named on
    the command line brings its convertible members along. An archive whose
    member listing hit the per-archive cap gets a line saying so.
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
            files, members, cut = scan_tree(
                path, recurse, archives,
                purpose="verify" if for_verify else "convert",
            )
            for entry in files:
                if for_verify and not entry.get("verifiable_by"):
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


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


class Printer:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def line(self, data: dict, *, prefix: str = "") -> None:
        """One Image. ``prefix`` goes before the path in text mode."""
        if self.as_json:
            print(json.dumps(data), flush=True)
            return
        text = f"{data['status']:<10} {prefix}{data['path']}"
        if data.get("output"):
            text += f" -> {data['output']}"
        if data.get("check"):
            text += f" ({data['check'].replace('_', '-')})"
        if data.get("reason"):
            text += f": {data['reason']}"
        print(text, flush=True)

    def summary(self, data: dict, text: str) -> None:
        """A line that is not about one Image: ``data`` as JSON, else ``text``."""
        print(json.dumps(data) if self.as_json else text, flush=True)


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
    path = _db.resolve_db_path(settings.db_path, data_dir=settings.data_dir)
    _db.init_engine(path, create_schema=False)
    _db.apply_migrations()
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
    routed = [i for i in items if i.mode and not i.status]
    shared = get_disallowed_archive_paths([i.path for i in routed])
    for item in routed:
        if not registry.spec(item.mode).supports_delete_on_verify:
            item.reason = f"Source kept: {item.mode} cannot delete its Source"
        elif "::" in item.path and item.path.split("::", 1)[0] in shared:
            item.reason = "Source kept: several Images come from this archive"
        else:
            item.delete_source = True


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
            for path, reason in batch.skipped:
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


@contextlib.asynccontextmanager
async def _job_runner(state: RunState, *listeners):
    """Run the job manager for one command, with the server's Ledger listener.

    Every job that ends lands in the Ledger (``ledger.record``), as on the
    server; ``listeners`` are told too while the block runs. The caller drains
    its jobs inside the block. On the way out the dispatcher stops and the
    listeners' own work (a Ledger write) is finished.
    """
    from services import ledger
    from services.job_manager import job_manager

    job_manager.add_terminal_listener(ledger.record)
    for listener in listeners:
        job_manager.add_terminal_listener(listener)
    runner = asyncio.create_task(job_manager.process_queue())
    state.jobs_started = True
    try:
        yield job_manager
    finally:
        for listener in listeners:
            job_manager.remove_terminal_listener(listener)
        # Cancelled before shutdown: a runner that has not started yet would
        # otherwise start the dispatcher after shutdown and wait forever.
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        await job_manager.shutdown()


async def _run_jobs(prepared: list[tuple], printer: Printer, state: RunState) -> None:
    """Queue the prepared batches, below the queue cap, and report each job.

    Leaves only once every job has run to its end, cleanup included
    (``JobManager.drain``): a job reports itself finished before it removes
    its temp dir.
    """
    from fastapi import HTTPException
    from routes.convert import queue_prepared

    from config import settings

    ended: dict = {}
    changed = asyncio.Event()

    def on_terminal(job) -> None:
        ended[job.id] = job
        changed.set()

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
    async with _job_runner(state, on_terminal) as job_manager:
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
            report()
        while pending:
            await wait_for_an_end()
        await job_manager.drain()


def _finish(item: Item, job) -> None:
    item.status = getattr(job.status, "value", str(job.status))
    item.output = job.output_path or item.output
    item.check = job.check_passed
    item.source_deleted = bool(job.deleted_paths)
    if job.error_message:
        item.reason = job.error_message


async def convert(args, printer: Printer, state: RunState) -> int:
    _validate_convert(args)
    paths = _check_paths(args.paths)
    from fastapi.concurrency import run_in_threadpool
    from services.tools import registry

    has_db = _open_db(args.dry_run)
    prefs = await _saved_compression(has_db)
    items = await run_in_threadpool(
        lambda: _discover(paths, recurse=not args.no_recurse, archives=not args.no_archives),
    )
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
    failed = any(i.status in ("failed", "cancelled") for i in items)
    return EXIT_FAILED if failed else EXIT_OK


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def _verify_tool(path: str):
    from services.tools import registry

    tools = registry.tools_verifying_path(path)
    if not tools:
        return None
    best = registry.tool_for_verify(path)
    return best if best in tools else tools[0]


def _checked_at(record: dict | None) -> datetime | None:
    if not record or not record.get("verified_at"):
        return None
    try:
        return datetime.fromisoformat(str(record["verified_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _verify_one(path: str, tool, has_db: bool, since, dry_run: bool) -> dict:
    """Check one Image. The line carries ``_stop`` when the run must end."""
    from services.lock_manager import lock_manager
    from services.subprocess_runner import collect_abandonment, stops_verify_batch
    from services.verification_store import verification_store

    line = {"path": path, "status": None, "tool": tool.id, "reason": None}
    if since is not None and has_db:
        checked = _checked_at(await verification_store.get_record(path))
        if checked is not None and checked >= datetime.now(timezone.utc) - since:
            line["status"] = "skipped"
            line["reason"] = f"Checked {checked.isoformat(timespec='seconds')}"
            return line
    if lock_manager.is_locked(path):
        line["status"], line["reason"] = "skipped", "Being written by another job"
        return line
    if dry_run:
        line["status"] = "planned"
        return line
    bound = await tool.verify_timeout(path)
    # The sink sees what the check wrote off on the way, including a detached
    # read left behind when this deadline cancels it; no result says so.
    with collect_abandonment() as abandoned:
        try:
            result = await asyncio.wait_for(tool.verify(path), timeout=bound or None)
        except asyncio.TimeoutError:
            result = {"error": f"Verification timed out after {bound}s"}
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            result = {"error": f"Verification could not run: {exc}"}
    if stops_verify_batch(result, abandoned):
        line["_stop"] = True
    if result.get("valid"):
        line["status"], line["reason"] = "intact", result.get("message")
        await verification_store.mark_verified(path)
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
    failed = False
    # Asked once per tool per run, dry runs included: a probe can walk the
    # volumes (nsz looking for prod.keys), so never once per file.
    ready: dict[str, bool] = {}
    for item in items:
        if state.stopping:
            break
        tool = await run_in_threadpool(_verify_tool, item.path)
        if tool is None:
            if item.explicit:
                printer.line({"path": item.path, "status": "skipped", "tool": None,
                              "reason": "No tool checks this file"})
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
        line = await _verify_one(item.path, tool, has_db, since, args.dry_run)
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
# run-rules
# --------------------------------------------------------------------------- #


def _choose_libraries(wanted: str | None) -> list:
    """The Libraries to sweep. Asked once the database is open: a Library list
    may be stored there (folder Libraries, #303)."""
    from services import library_catalog

    every = library_catalog.all_libraries()
    if wanted is None:
        return every
    chosen = [library for library in every if library.id == wanted]
    if not chosen:
        names = ", ".join(library.id for library in every) or "none"
        raise UsageError(f"--library {wanted}: no such Library. Libraries: {names}")
    return chosen


def _rules_line(library_id, platform_id=None, *, status, mode=None, queued=0,
                considered=0, candidates=(), reason=None) -> dict:
    return {
        "library": library_id, "platform_id": platform_id, "mode": mode,
        "status": status, "queued": queued, "considered": considered,
        "candidates": list(candidates), "reason": reason,
    }


def _rules_text(line: dict) -> str:
    where = line["library"] or "every Library"
    if line["platform_id"] is not None:
        where += f"/{line['platform_id']} {line['mode'] or ''}".rstrip()
    text = f"{line['status']:<10} {where}"
    if line["mode"]:
        verb = "to queue" if line["status"] == "planned" else "queued"
        text += f": {line['queued']} {verb} of {line['considered']} considered"
        if line["candidates"]:
            text += f" ({', '.join(line['candidates'])})"
    if line["reason"]:
        text += f": {line['reason']}" if not line["mode"] else f"; {line['reason']}"
    return text


# A sweep that stopped for one of these did not do what its Rules say: exit 1.
# `not_configured` is only reported for a Library that has Rules.
_FAILED_STOPS = ("library_unresponsive", "not_configured")


def _print_sweep(printer: Printer, library, result: dict) -> bool:
    """One line per Platform the sweep looked at, and one for the Library
    when the sweep stopped early or never started (``no_rules``).

    True when the sweep reported an error: any ``errors`` entry, including a
    lost race for a destination (``destination_claimed``), or a stop in
    ``_FAILED_STOPS``.
    """
    errors: dict = {}
    for entry in result["errors"]:
        errors.setdefault(entry["platform_id"], []).append(entry["error"])
    lines = []
    for summary in result["platforms"]:
        platform_id = summary["platform_id"]
        reason = ", ".join(errors.pop(platform_id, [])) or None
        if result.get("dry_run"):
            status = "planned"
        elif reason and not summary["queued"]:
            status = "error"
        else:
            status = "queued"
        lines.append(_rules_line(
            library.id, platform_id, status=status, mode=summary["mode"],
            queued=summary["queued"], considered=summary["considered"],
            candidates=summary.get("candidates") or (), reason=reason,
        ))
    # A Platform skipped before it was read (tool not ready, Library
    # unreadable), or the whole Library refused (platform None), has an error
    # and no summary.
    for platform_id, reasons in errors.items():
        lines.append(_rules_line(
            library.id, platform_id, status="error", reason=", ".join(reasons),
        ))
    stopped = result.get("stopped_reason")
    if stopped:
        status = "skipped" if stopped in ("no_rules", "not_configured") else "stopped"
        lines.append(_rules_line(library.id, status=status, reason=stopped))
    for line in lines:
        printer.summary(line, _rules_text(line))
    return bool(result["errors"]) or stopped in _FAILED_STOPS


# How often a running hook looks for a stop.
_STOP_POLL = 0.2


async def _until_stopped(work, state: RunState) -> bool:
    """Run ``work`` until it ends or the run is stopping. False when stopped.

    A Library's ``before_pass`` can run long (RomM's settle pass gives each
    large output its full hashing time), and nothing in it reads the stop
    flag, so it is cancelled from here. Its exceptions propagate.
    """
    task = asyncio.ensure_future(work)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=_STOP_POLL)
            if state.stopping and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return False
        task.result()
        return True
    finally:
        if not task.done():
            task.cancel()


async def _sweep_one(printer: Printer, library, state: RunState, *, dry_run: bool) -> bool:
    """The Library's ``before_pass`` hook, then its sweep. True on any error.

    An exception is reported for this Library and does not reach the caller,
    as in the server's scheduler loop, so the other Libraries still run.
    """
    from services.rules import sweep as rules_sweep

    try:
        if not await _until_stopped(library.before_pass(dry_run=dry_run), state):
            line = _rules_line(library.id, status="stopped", reason="stopped")
            printer.summary(line, _rules_text(line))
            return False  # the stop itself makes the run exit 1
        result = await rules_sweep.sweep(
            library, dry_run=dry_run, should_stop=lambda: state.stopping,
        )
    except Exception as exc:  # one Library must not stop the rest
        logger.warning("run-rules: Library %s failed: %s", library.id, exc, exc_info=True)
        line = _rules_line(library.id, status="error", reason=f"sweep_failed: {exc}")
        printer.summary(line, _rules_text(line))
        return True
    return _print_sweep(printer, library, result)


async def run_rules(args, printer: Printer, state: RunState) -> int:
    """One pass over the stored Rules of each Library, like one server tick.

    Each Library runs its ``before_pass`` hook (what the server does for it at
    startup or in the background), then goes through ``rules.sweep.sweep``,
    the sweep the server's scheduler runs, so each Rule's own switch, days,
    window and interval apply. The Library's in-app master switch does not:
    running this command is the switch. The jobs it queues run here, and it
    returns once they have ended, cleanup included. While it runs it holds the
    scheduler lock, so it never runs beside a server on the same data
    directory. Exit 1 when a sweep reported an error or a job failed.
    """
    from services import scheduler_lock
    from services.preferences_store import preferences_store

    with contextlib.ExitStack() as held:
        if args.dry_run:
            # Look, but take nothing: a dry run must not hold up a server.
            scheduler_lock.check()
        else:
            # Before the database is opened: a refused run touches nothing.
            held.callback(scheduler_lock.acquire("run-rules").release)
        has_db = _open_db(args.dry_run)
        # Listed once the database is open (a Library list can live there);
        # with none, `all_libraries` lists the Libraries that need none, so a
        # mistyped --library is still refused.
        chosen = _choose_libraries(args.library)
        if not has_db:
            # A dry run with no database: nothing is stored, so no Rules.
            for library in chosen:
                line = _rules_line(library.id, status="skipped", reason="no_rules")
                printer.summary(line, _rules_text(line))
            return EXIT_OK
        if not args.dry_run:
            return await _run_rules_pass(chosen, printer, state)
        failed = False
        # The Rule store writes as it reads (its one-time copies); a dry run
        # keeps those writes in memory.
        with preferences_store.read_only():
            for library in chosen:
                failed = await _sweep_one(printer, library, state, dry_run=True) or failed
        return EXIT_FAILED if failed or state.stopping else EXIT_OK


async def _run_rules_pass(chosen: list, printer: Printer, state: RunState) -> int:
    from services.rules import sweep as rules_sweep

    owners: dict[str, str] = {}
    sweeping: list[str] = []
    failed = False

    def on_terminal(job) -> None:
        nonlocal failed
        item = Item(path=job.file_path, mode=getattr(job.mode, "value", job.mode))
        _finish(item, job)
        failed = failed or item.status in ("failed", "cancelled")
        # A job that ends while its own sweep is still running has no owner
        # yet; the Library being swept queued it.
        library_id = owners.get(job.id) or (sweeping[-1] if sweeping else None)
        printer.line({"library": library_id, **item.convert_line()},
                     prefix=f"{library_id}: " if library_id else "")

    # Registered like the server's: the Rule history hears each job's outcome.
    async with _job_runner(state, on_terminal, rules_sweep.note_job_finished) as jobs:
        for library in chosen:
            if state.stopping:
                break
            before = set(jobs.jobs)
            sweeping.append(library.id)
            if await _sweep_one(printer, library, state, dry_run=False):
                failed = True
            for job_id in set(jobs.jobs) - before:
                owners.setdefault(job_id, library.id)
            if state.stopping:
                # The signal cancelled what was queued when it arrived; the
                # sweep may have let one more batch in since. Cancel that too
                # rather than wait for it.
                await jobs.cancel_all_jobs()
                break
        await jobs.drain()
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
        if args.command == "run-rules":
            return await run_rules(args, printer, state)
        return await verify(args, printer, state)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help is 0, a bad argument 2
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    with _logging(args.verbose):
        from services.scheduler_lock import SchedulerLockHeld
        from utils.process_limits import raise_open_file_limit

        raise_open_file_limit()
        try:
            return asyncio.run(_run(args))
        except UsageError as exc:
            print(f"compressatorium: error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except SchedulerLockHeld as exc:
            print(f"compressatorium: not running the Rules: {exc}", file=sys.stderr)
            return EXIT_LOCKED
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("compressatorium: stopped", file=sys.stderr)
            return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
