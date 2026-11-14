"""``compressatorium``: convert and verify Images from a shell or cron (#306).

Run as ``python -m cli`` with ``app/`` on ``PYTHONPATH``; the Docker image
installs a ``compressatorium`` wrapper that does exactly that.

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
  directory, so a running server and this process never write one output twice.

Exit status: 0 when everything finished or there was nothing to do, 1 when any
Image failed (or was cancelled), 2 on bad arguments.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
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

logger = get_logger("cli")


class UsageError(Exception):
    """A bad argument: printed to stderr, exit 2."""


@dataclass
class Item:
    """One Image and what happened to it: one output line."""

    path: str
    explicit: bool = False
    platform: str | None = None
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
        help="output format (chd, rvz, cso, nsz, ...) or a mode id (createdvd)",
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
        help="print what would happen; change nothing on disk or in the database",
    )
    parser.add_argument("--json", action="store_true", help="one JSON line per Image")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to stderr")


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


def _check_paths(paths: list[str]) -> list[str]:
    from utils.path_utils import is_within_configured_volumes

    from config import settings

    checked = []
    for raw in paths:
        path = _absolute(raw)
        if not os.path.exists(path.split("::", 1)[0]):
            raise UsageError(f"{raw}: no such file or directory")
        if not is_within_configured_volumes(path):
            volumes = ", ".join(settings.volumes) or "none"
            raise UsageError(
                f"{raw} is outside the configured Volumes ({volumes}); "
                "set COMPRESSATORIUM_VOLUMES to include it"
            )
        checked.append(path)
    return checked


# --------------------------------------------------------------------------- #
# Finding Images
# --------------------------------------------------------------------------- #


def _discover(paths: list[str], *, for_verify: bool = False) -> list[Item]:
    """Every Image under ``paths``, named ones first-class, in walk order.

    A folder is walked with the web UI's recursive search (``scan_tree``); a
    folder that is itself an Image (a PS3 disc folder) is taken whole. An
    archive named on the command line brings its convertible members along.
    """
    from routes.files import scan_tree
    from services.archive import archive_service
    from services.tools import registry

    found: dict[str, Item] = {}

    def add(path: str, explicit: bool) -> None:
        if path not in found:
            found[path] = Item(path=path, explicit=explicit)

    for path in paths:
        if "::" not in path and os.path.isdir(path):
            if not for_verify and registry.tools_for_directory(path):
                add(path, True)
                continue
            files, members, _ = scan_tree(path, True, True)
            for entry in files:
                if for_verify and not entry.get("verifiable_by"):
                    continue
                add(entry["path"], False)
            if not for_verify:  # a check reads files, never archive members
                for entry in members:
                    add(entry["path"], False)
            continue
        add(path, True)
        if not for_verify and "::" not in path and archive_service.is_archive(path):
            listing = archive_service.list_archive_contents(
                path, include_meta=True, convertible_only=True,
            )
            for entry in listing["entries"]:
                add(f"{path}::{entry['internal_path']}", False)
    return list(found.values())


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
    from services.platform_detect import target_formats
    from services.tools import registry

    if args.container_only and not args.delete_source:
        raise UsageError("--container-only only applies with --delete-source")
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


def _route_to(
    items: list[Item], target: str, prefs: dict, ready: list[str],
) -> list[Item]:
    """Pick each Image's mode for ``--to``; drop discovered Images out of scope.

    A mode whose tool cannot run here (``ready``) is skipped with the same
    reason Auto gives.
    """
    from routes.convert import checked_compression
    from services.platform_detect import (
        detect_platform,
        format_candidates,
        format_route,
    )
    from services.tools import registry

    kept = []
    for item in items:
        if not item.explicit and not format_candidates(item.path, target, registry):
            continue
        item.platform = detect_platform(item.path).platform
        item.mode, item.reason = format_route(item.path, target, item.platform, registry)
        tool = registry.for_mode(item.mode) if item.mode else None
        if tool is not None and tool.id not in ready:
            item.mode = None
            item.reason = f"{tool.display_name} isn't ready: {tool.not_ready_reason}"
        if item.mode:
            item.compression = checked_compression(
                item.mode, _pref_for(item.mode, prefs),
            )
        else:
            item.status = "skipped"
        kept.append(item)
    return kept


def _pref_for(mode: str, prefs: dict) -> str | None:
    from services.tools import registry

    spec = registry.spec(mode)
    if not (spec.supports_compression or spec.supports_compression_level):
        return None
    return prefs.get(spec.tool_id) or None


async def _route_auto(
    items: list[Item], output_dir: str | None, prefs: dict, ready: list[str],
) -> list[Item]:
    from routes.convert import plan_auto_targets
    from services.platform_detect import auto_reads
    from services.tools import registry

    kept = [i for i in items if i.explicit or auto_reads(i.path, registry)]
    targets = await plan_auto_targets(
        [i.path for i in kept], output_dir, prefs, ready_tool_ids=ready,
    )
    for item, target in zip(kept, targets, strict=True):
        item.platform = target.detection.platform
        item.mode = target.mode
        item.compression = target.compression
        if not target.mode:
            item.status, item.reason = "skipped", target.skip_reason
    return kept


def _decide_deletes(items: list[Item], args) -> None:
    from routes.convert import get_disallowed_archive_paths
    from services.tools import registry

    if not args.delete_source:
        return
    routed = [i for i in items if i.mode]
    shared = get_disallowed_archive_paths([i.path for i in routed])
    for item in routed:
        if not registry.spec(item.mode).supports_delete_on_verify:
            item.reason = f"Source kept: {item.mode} cannot delete its Source"
        elif "::" in item.path and item.path.split("::", 1)[0] in shared:
            item.reason = "Source kept: several Images come from this archive"
        else:
            item.delete_source = True


async def _prepare(items: list[Item], args) -> list[tuple]:
    """Plan every routed Image through the batch route's planner, by group.

    Returns ``[(PreparedBatch, {planned file path: Item}), ...]``. Images the
    planner leaves out are marked skipped with its reason; a group it refuses
    outright is marked failed.
    """
    from fastapi import HTTPException
    from models import BatchJobCreateRequest, ConversionMode, DuplicateAction
    from routes.convert import prepare_batch

    groups: dict[tuple, list[Item]] = {}
    for item in items:
        if item.mode and not item.status:
            groups.setdefault(
                (item.mode, item.compression, item.delete_source), [],
            ).append(item)

    prepared = []
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
        try:
            batch = await prepare_batch(request)
        except HTTPException as exc:
            for item in group:
                item.status, item.reason = "failed", str(exc.detail)
            continue
        by_path: dict[str, Item] = {}
        for item in group:
            by_path[item.path] = item
            by_path.setdefault(os.path.realpath(item.path), item)
        for path, reason in batch.skipped:
            item = by_path.get(path)
            if item is not None:
                item.status, item.reason = "skipped", reason
        planned = {}
        for plan in batch.plans:
            item = by_path.get(plan.file_path)
            if item is not None:
                item.output = plan.output_path
                planned[plan.file_path] = item
        prepared.append((batch, planned))
    return prepared


async def _run_jobs(prepared: list[tuple], printer: Printer) -> None:
    """Queue every prepared batch, then report each job as it ends."""
    from fastapi import HTTPException
    from routes.convert import queue_prepared
    from services import ledger
    from services.job_manager import job_manager

    ended: dict = {}
    changed = asyncio.Event()

    def on_terminal(job) -> None:
        ended[job.id] = job
        changed.set()

    job_manager.add_terminal_listener(ledger.record)
    job_manager.add_terminal_listener(on_terminal)
    runner = asyncio.create_task(job_manager.process_queue())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(
                sig, lambda: asyncio.ensure_future(job_manager.cancel_all_jobs()),
            )
    watching: dict[str, Item] = {}
    try:
        for batch, planned in prepared:
            try:
                jobs = await queue_prepared(batch)
            except HTTPException as exc:
                for item in planned.values():
                    item.status, item.reason = "failed", str(exc.detail)
                    printer.line(item.convert_line())
                continue
            for job in jobs:
                item = planned.get(job.file_path)
                if item is not None:
                    watching[job.id] = item
        pending = set(watching)
        while pending:
            for job_id in [j for j in pending if j in ended]:
                pending.discard(job_id)
                _finish(watching[job_id], ended[job_id])
                printer.line(watching[job_id].convert_line())
            if pending:
                changed.clear()
                if not any(j in ended for j in pending):
                    await changed.wait()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
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
    if job.error_message:
        item.reason = job.error_message


async def convert(args, printer: Printer) -> int:
    _validate_convert(args)
    paths = _check_paths(args.paths)
    from fastapi.concurrency import run_in_threadpool

    has_db = _open_db(args.dry_run)
    prefs = await _saved_compression(has_db)
    items = await run_in_threadpool(_discover, paths)
    from services.tools import registry

    # The shared, bounded readiness probe the Auto route uses.
    ready = await registry.ready_tool_ids()
    if args.auto:
        items = await _route_auto(items, args.output_dir, prefs, ready)
    else:
        items = await run_in_threadpool(_route_to, items, args.to, prefs, ready)
    _decide_deletes(items, args)
    prepared = await _prepare(items, args)

    for item in items:
        if item.status:
            printer.line(item.convert_line())
    if args.dry_run:
        for _batch, planned in prepared:
            for item in planned.values():
                item.status = "planned"
                printer.line(item.convert_line())
    else:
        await _run_jobs(prepared, printer)
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
    from services.lock_manager import lock_manager
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
    try:
        result = await asyncio.wait_for(tool.verify(path), timeout=bound or None)
    except asyncio.TimeoutError:
        result = {"valid": False, "message": f"Verification timed out after {bound}s"}
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
        result = {"valid": False, "message": str(exc)}
    line["reason"] = result.get("message")
    if result.get("valid"):
        line["status"] = "intact"
        await verification_store.mark_verified(path)
    else:
        line["status"] = "failed"
        # It is not Intact any more: a stored pass would be a false badge, and
        # `--since` would skip it next time.
        await verification_store.clear(path)
    return line


async def verify(args, printer: Printer) -> int:
    since = parse_age(args.since) if args.since else None
    paths = _check_paths(args.paths)
    from fastapi.concurrency import run_in_threadpool

    has_db = _open_db(args.dry_run)
    items = await run_in_threadpool(_discover, paths, for_verify=True)
    failed = False
    for item in items:
        tool = await run_in_threadpool(_verify_tool, item.path)
        if tool is None:
            if item.explicit:
                printer.line({"path": item.path, "status": "skipped", "tool": None,
                              "reason": "No tool checks this file"})
            continue
        line = await _verify_one(item.path, tool, has_db, since, args.dry_run)
        failed = failed or line["status"] == "failed"
        printer.line(line)
    return EXIT_FAILED if failed else EXIT_OK


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


async def _run(args) -> int:
    printer = Printer(args.json)
    if args.command == "convert":
        return await convert(args, printer)
    return await verify(args, printer)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help is 0, a bad argument 2
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    with _logging(args.verbose):
        try:
            return asyncio.run(_run(args))
        except UsageError as exc:
            print(f"compressatorium: error: {exc}", file=sys.stderr)
            return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
