"""``compressatorium``: convert, verify and run Rules from a shell or cron (#306, #307).

Run as ``python -m cli`` with ``app/`` on ``PYTHONPATH``; the Docker image
installs a ``compressatorium`` wrapper that does exactly that (and, started as
root, drops to the ``converter`` user through the image's entrypoint first).

The CLI starts no web server, and it is not a second conversion path. One
process works a data directory at a time (``services.scheduler_lock``), so
``convert`` either hands its jobs to the running server or runs them alone:

* a server running on the data directory holds its worker lock and names its
  port there; ``convert`` finds it the way it finds the rest of its config,
  through the data directory (or ``--server URL``), and queues each batch with
  the server's batch route, following the jobs there (``services.server_client``).
  Nothing runs in the CLI;
* with no server, ``convert`` takes the lock and runs the jobs in-process. A
  server started meanwhile waits for it, and another ``convert`` waits its
  turn.

Either way it drives the same pieces the web UI does:

* finding Images: ``routes.files.scan_tree``, the recursive search walk, so
  subfolders and archive members are found the way the UI finds them;
* choosing a mode: ``platform_detect.format_route`` (``--to``) and
  ``routes.convert.plan_auto_targets`` (``--auto``), both through the registry;
* planning and queueing: ``routes.convert.prepare_batch`` / ``queue_prepared``,
  the batch route's own two halves, then the ``job_manager`` singleton runs the
  jobs (``_delete_check`` decides every delete; ``ledger.record`` writes every
  Ledger row);
* locking: the job manager's output locks are ``flock`` files in the shared lock
  directory (``<data dir>/locks``); ``verify``, which reads only, holds the same
  shared read lock a job does, so it never checks a file a job is writing;
* Rules (``run-rules``): ``rules.sweep.sweep``, the server scheduler's own
  sweep, once per Library from ``services.libraries``; the scheduler lock
  (``services.scheduler_lock``) keeps it from running beside a server.

Exit status: 0 when everything finished or there was nothing to do, 1 when any
Image failed (or was cancelled), 2 on bad arguments, 3 when ``run-rules`` finds
another process (a server) using the data directory, or ``convert`` finds a
server there that does not answer, 4 when ``verify``
failed nothing but could not check at least one Image (``uncheckable``: no
verdict, e.g. a file it could not open, or its own timeout). 2 and 3 stop the
command before anything is checked or run; among the Images checked the worst
wins: 1, then 4, then 0.
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
# `verify`: nothing failed, but at least one Image could not be checked here
# (no verdict: a file the verifier could not open, a stall), #370.
EXIT_UNCHECKED = 4

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


_EXIT_STATUS = (
    "exit status: 0 done (or nothing to do); 1 an Image or job failed; 2 bad "
    "arguments or a path that does not exist; 3 run-rules found another "
    "process using the data directory, or convert found a server that does not "
    "answer; 4 verify failed nothing but could not check an Image "
    "(uncheckable). Order: 2 and 3 stop the command before anything is "
    "checked or run; among the Images checked, the worst wins: 1, then 4, "
    "then 0."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compressatorium",
        description="Convert and verify game Images, and run Rules, without the web UI.",
        epilog=_EXIT_STATUS,
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
    convert.add_argument(
        "--server", metavar="URL",
        help="hand the jobs to the server at URL; without it, a server running "
             "on this data directory is found on its own, and with none the jobs "
             "run here",
    )
    _common(convert)

    verify = sub.add_parser("verify", help="check Images are Intact", epilog=_EXIT_STATUS)
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

    dat = sub.add_parser("dat", help="import DATs, or sync Redump's (#524)")
    dat_sub = dat.add_subparsers(dest="dat_command", metavar="ACTION", required=True)
    dat_import = dat_sub.add_parser(
        "import", help="import .dat/.xml files and zips of them (No-Intro's daily pack)",
    )
    dat_import.add_argument("paths", nargs="+", metavar="PATH")
    dat_sync = dat_sub.add_parser(
        "sync", help="download Redump's DATs for the disc Platforms in your Libraries",
    )
    dat_sync.add_argument(
        "--platform", action="append", metavar="SLUG",
        help="only this Platform (psx, ps2, ngc, ...); repeatable",
    )
    dat_sync.add_argument(
        "--force", action="store_true", help="ask again even for a system synced today",
    )
    for cmd in (dat_import, dat_sync):
        cmd.add_argument("--json", action="store_true", help="print the result as JSON")
        cmd.add_argument("-v", "--verbose", action="store_true",
                         help="also log INFO messages to stderr")
        cmd.set_defaults(dry_run=False)
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
    verify target (the first part) and every later part
    (``registry.split_members``)."""
    from services.tools import registry

    mode = item.logical_mode
    try:
        target = registry.for_mode(mode).verify_target(item.path, mode)
    except (KeyError, OSError) as exc:
        logger.debug("No verify target for %s through %s: %s", item.path, mode, exc)
        return []
    if not target:
        return []
    return [target, *registry.split_members(target)]


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
        spec = registry.spec(key)
        if args.delete_source and not spec.supports_delete_on_verify:
            why = f": {spec.source_kept_reason}" if spec.source_kept_reason else ""
            raise UsageError(f"{key} cannot delete its Source (--delete-source){why}")
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
    from services.tools import not_ready_message, registry

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
            item.reason = not_ready_message(tool)
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
        spec = registry.spec(item.mode)
        if not spec.supports_delete_on_verify:
            item.reason = (
                f"Source kept: {spec.source_kept_reason}" if spec.source_kept_reason
                else f"Source kept: {item.mode} cannot delete its Source"
            )
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

    Twice the conversion slots, so a slot never waits on this run, and
    never more than MAX_QUEUE_DEPTH when that is set. On a server it keeps a
    whole library from going ahead of the web UI's work.
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
    from services import ledger, libraries, notifications
    from services.job_manager import job_manager

    job_manager.add_terminal_listener(ledger.record)
    job_manager.add_amend_listener(ledger.amend)
    notifications.attach(job_manager)  # the server's own listener, so a pass reports too
    libraries.attach(job_manager)  # a finished job drops out-of-date walks (#353)
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
        await _drain_notifications()
        await job_manager.shutdown()
        # Queued sends finish before the loop closes (bounded: a dead destination
        # must not hold the command).
        await notifications.drain(timeout=notifications.SEND_TIMEOUT_S + 2)


async def _drain_notifications() -> None:
    """Send what is queued before the loop closes, within a bound.

    ``notifications.notify`` queues and returns (a check run's alert, #305), so
    a command that exited at once would drop it. Each send is already bounded
    by ``SEND_TIMEOUT_S``; the wait here allows a few of them in a row, then
    gives up rather than hold the exit on a dead destination.
    """
    from services import notifications

    try:
        await asyncio.wait_for(
            notifications.drain(), _NOTIFY_DRAIN_SENDS * notifications.SEND_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Exiting with notifications still unsent (destinations not answering)")


# How many sends in a row the exit waits for (see `_drain_notifications`).
_NOTIFY_DRAIN_SENDS = 3


async def _run_jobs(prepared: list[tuple], printer: Printer, state: RunState) -> None:
    """Queue the prepared batches, below the queue cap, and report each job.

    Leaves only once every job has run to its end, cleanup included
    (``JobManager.drain``): a job reports itself finished before it removes
    its temp dir.
    """
    from fastapi import HTTPException
    from routes.convert import OUTPUT_CLAIMED_CODE, queue_prepared
    from services.job_manager import CANCEL_SHUTDOWN

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
            # Only a small window is queued at a time (below MAX_QUEUE_DEPTH
            # when that is set), as on a server (`_run_remote`).
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
            # A file another job was already writing is left out (#520).
            for skip in batch.skips:
                item = planned.get(skip.path)
                if item is not None and skip.code == OUTPUT_CLAIMED_CODE:
                    item.status, item.reason = "skipped", skip.reason
                    printer.line(item.convert_line())
            if state.stopping:
                # A stop that arrived while this batch was being queued: the
                # signal handler's cancel-all ran before these jobs existed.
                await job_manager.cancel_all_jobs(reason=CANCEL_SHUTDOWN)
            report()
        while pending:
            await wait_for_an_end()
        await job_manager.drain()


def _finish(item: Item, job) -> None:
    from services.job_manager import is_external_mode

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
    if (getattr(job, "result", None) or {}).get("waiting"):
        # Only waiting (an Inbox archive with no room to unpack, #458): not a
        # failure.
        item.status = "waiting"
    if is_external_mode(job.mode):
        # An external job has no real input path, only its internal
        # /__external_jobs__/<id>: every line names its own file (#463).
        item.path = job.filename


# How often `_run_remote` asks the server about the jobs it follows.
_REMOTE_POLL_S = 1.0


async def _run_remote(server, prepared: list[tuple], printer: Printer, state: RunState) -> None:
    """``_run_jobs`` on a running server (#533): each batch goes to the
    server's batch route, below the same window, and each job is followed
    there until it ends. A stop cancels this run's jobs on the server, and
    only those. A server that stops answering fails the jobs still followed.
    """
    from models import ConversionJob, JobStatus
    from services.server_client import ServerRefused, ServerUnreachable

    from config import settings

    ended_states = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    watching: dict[str, Item] = {}
    state.jobs_started = True  # a signal now stops the run, not the process

    def end(item: Item, status: str, reason: str) -> None:
        item.status, item.reason = status, reason
        printer.line(item.convert_line())

    async def follow() -> None:
        for job_id in list(watching):
            try:
                data = await server.job(job_id)
            except (ServerUnreachable, ServerRefused) as exc:
                for lost in list(watching):
                    end(watching.pop(lost), "failed", f"Lost the server: {exc}")
                return
            if data is None:
                end(watching.pop(job_id), "failed", "The server no longer knows this job")
                continue
            job = ConversionJob.model_validate(data)
            if job.status in ended_states:
                item = watching.pop(job_id)
                _finish(item, job)
                printer.line(item.convert_line())

    async def a_turn() -> None:
        await follow()
        if watching:
            await asyncio.sleep(_REMOTE_POLL_S)

    window = _in_flight_window(settings)
    for batch, planned in _chunks(prepared, window):
        while not state.stopping and watching and len(watching) + len(batch.plans) > window:
            await a_turn()
        if state.stopping:
            for plan in batch.plans:
                if (item := planned.get(plan.file_path)) is not None:
                    end(item, "cancelled", "Stopped before it was queued")
            continue
        request = batch.request.model_copy(
            update={"file_paths": [plan.file_path for plan in batch.plans]},
        )
        try:
            jobs, skipped = await server.queue(request.model_dump(mode="json"))
        except (ServerUnreachable, ServerRefused) as exc:
            for plan in batch.plans:
                if (item := planned.get(plan.file_path)) is not None:
                    end(item, "failed", str(exc))
            continue
        for data in jobs:
            if (item := planned.get(data.get("file_path"))) is not None:
                watching[data["id"]] = item
        for skip in skipped:
            if (item := planned.get(skip.get("path"))) is not None:
                end(item, "skipped", skip.get("reason"))
    cancelled = False
    while watching:
        if state.stopping and not cancelled:
            cancelled = True
            for job_id in list(watching):
                await server.cancel(job_id)
        await a_turn()


@contextlib.asynccontextmanager
async def _worker(args):
    """Where this run's jobs go: a running server (yielded), or here (None).

    ``--server`` names the server. Otherwise the data directory's worker lock
    says: held by a server, the jobs go to it, at the port it named there;
    free, this run takes it and the jobs run here while it holds it; held by
    another CLI, this run waits its turn. A server that does not answer stops
    the run (exit 3) before anything is queued.
    """
    from services import scheduler_lock
    from services.server_client import Server, ServerUnreachable

    if args.server:
        try:
            server = Server(args.server)
        except ValueError as exc:
            raise UsageError(f"--server: {exc}") from None
        await server.check()
        yield server
        return
    told_at = None
    while True:
        try:
            held = await asyncio.to_thread(scheduler_lock.acquire, "convert", wait=0)
        except scheduler_lock.SchedulerLockHeld as exc:
            if exc.holder.get("by") == "server":
                server = Server.of_holder(exc.holder)
                try:
                    await server.check()
                except ServerUnreachable as lost:
                    raise ServerUnreachable(
                        f"{exc}, but {lost}; pass --server URL to name it",
                    ) from None
                yield server
                return
            now = asyncio.get_running_loop().time()
            if told_at is None or now - told_at >= scheduler_lock._WAIT_LOG_EVERY:
                told_at = now
                logger.warning("Waiting for the data directory: %s", exc)
            await asyncio.sleep(scheduler_lock._WAIT_POLL)
            continue
        try:
            yield None
        finally:
            held.release()
        return


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
    elif prepared:
        async with _worker(args) as server:
            if server is not None:
                await _run_remote(server, prepared, printer, state)
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
    from services.lock_manager import LockFailed

    try:
        held = await run_in_threadpool(_hold_for_check, read, also_lock)
    except LockFailed as exc:
        # The lock file can't be made: an error on this line, not "busy".
        line["status"], line["reason"] = "failed", str(exc)
        return line
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
    from services.subprocess_runner import (
        collect_abandonment,
        stops_verify_batch,
        verify_exception_event,
    )
    from services.verification_store import cannot_check, record_check

    read = read or path
    bound = await tool.verify_timeout(read)
    # The sink sees what the check wrote off on the way, including a detached
    # read left behind when this deadline cancels it; no result says so.
    with collect_abandonment() as abandoned:
        try:
            result = await asyncio.wait_for(tool.verify(read), timeout=bound or None)
        except asyncio.TimeoutError:
            # Its bound ran out: nothing was judged, so no verdict (#351,
            # #370), the same as the routes' own timeout. The record stays.
            result = {
                "valid": False, "timed_out": True, "inconclusive": True,
                "error": f"Verification timed out after {bound}s",
            }
        except Exception as exc:
            # One bad file must not stop the rest. Read by the shared rule
            # (`verify_exception_event`): a file that could not be read is no
            # verdict, the tool rejecting the data is one.
            logger.debug("Verification of %s could not run", read, exc_info=True)
            result = verify_exception_event(exc, prefix="Verification could not run")
    if stops_verify_batch(result, abandoned):
        line["_stop"] = True
    # The shared rule every re-check records by (#351): a pass refreshes the
    # record; a real verdict that it is bad marks it failed (a stored pass
    # would be a false badge, and `--since` would skip it next time; marked,
    # not cleared, so the next check still goes to the tool that made it); no
    # verdict leaves what was stored.
    verdict = await record_check(verification_store, path, result)
    if verdict:
        line["status"], line["reason"] = "intact", result.get("message")
    elif result.get("uncheckable"):
        # The tool won't check it here (an archive safety rule refused it):
        # not a failure, and nothing to retry.
        line["status"], line["reason"] = "skipped", result.get("message")
    elif cannot_check(result):
        # No verdict this time (a file the verifier could not open, a stall,
        # this command's own bound, a missing prerequisite): the batch
        # verify's word, and its own exit code, never a failure (#370).
        line["status"] = "uncheckable"
        line["reason"] = result.get("error") or result.get("message")
    else:
        # A real verdict that it is bad.
        line["status"] = "failed"
        line["reason"] = result.get("error") or result.get("message")
    return line


async def verify(args, printer: Printer, state: RunState) -> int:
    from services.subprocess_runner import BATCH_STOPPED_MESSAGE
    from services.tools import not_ready_message, registry

    since = parse_age(args.since) if args.since else None
    paths = _check_paths(args.paths)
    from fastapi.concurrency import run_in_threadpool

    has_db = _open_db(args.dry_run)
    items = await run_in_threadpool(lambda: _discover(paths, for_verify=True))
    # A split output's numbered parts are one logical Image, named or found.
    items = await run_in_threadpool(_to_logical, items)
    from services.verification_store import verification_store

    if has_db:
        # Every stored row, failed ones included: a file whose last check
        # failed is re-checked from a folder run too.
        items = _with_stored_checks(items, paths, await verification_store.all_provenance())
    # Once, on the final input set, before any verifier is chosen.
    await run_in_threadpool(_skip_outside_volumes, items)
    failed = False
    unchecked = False  # an Image with no verdict: exit 4 unless one failed
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
        if record is None and has_db:
            # A failed check is no stored pass, but it still names the mode
            # that made the file: route by it.
            meta = await run_in_threadpool(
                verification_store.produced_meta_blocking, item.path,
            )
            if (meta or {}).get("mode"):
                record = {"produced_meta": meta}
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
                "reason": not_ready_message(tool, purpose="verify"),
            })
            failed = failed or item.explicit
            continue
        # A logical Image (a split ISO) also holds its logical path, which a
        # rebuild locks alone, and every part: an in-progress build makes the
        # check skip as busy rather than judge a half-written set.
        also_lock = (
            [item.path, target, *registry.split_members(target)]
            if item.logical_mode and target else []
        )
        line = await _verify_one(
            item.path, tool, has_db, since, args.dry_run, target=target,
            also_lock=also_lock,
        )
        stop = line.pop("_stop", False)
        failed = failed or line["status"] == "failed"
        unchecked = unchecked or line["status"] == "uncheckable"
        printer.line(line)
        if stop:
            # The shared stop rule (`stops_verify_batch`), as the web UI's
            # batch verify applies it.
            print(f"compressatorium: {BATCH_STOPPED_MESSAGE}", file=sys.stderr)
            # Stuck storage judged nothing: a failure only if an Image already
            # failed (#374).
            return EXIT_FAILED if failed else EXIT_UNCHECKED
    if failed or state.stopping:
        return EXIT_FAILED
    return EXIT_UNCHECKED if unchecked else EXIT_OK


# --------------------------------------------------------------------------- #
# run-rules
# --------------------------------------------------------------------------- #


def _choose_libraries(wanted: str | None, *, has_db: bool) -> list:
    """The Libraries to sweep: ``services.libraries.all_libraries()``, the list
    the server's scheduler sweeps (RomM and every folder Library, #303). Asked
    once the database is open, where the folder Libraries are stored. With no
    database (a dry run on a fresh data dir) only the Libraries that need none
    exist (``without_a_database``), and ``--library`` is checked against them."""
    from services import libraries

    every = libraries.all_libraries() if has_db else libraries.without_a_database()
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
_FAILED_STOPS = ("library_unresponsive", "not_configured", "queue_full")


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
        reasons = errors.pop(platform_id, [])
        if summary.get("unqueued"):
            # The queue refused this Platform's batch (queue_full).
            unqueued = summary["unqueued"]
            plural = "s" if unqueued != 1 else ""
            reasons.append(f"queue full: {unqueued} Image{plural} not queued")
        reason = ", ".join(reasons) or None
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
        # Listed once the database is open (the folder Libraries live there);
        # with none, only the Libraries that need none, so a mistyped
        # --library is still refused.
        chosen = _choose_libraries(args.library, has_db=has_db)
        if has_db:
            # As the server does at startup: the Hasheous toggle saved in the
            # web UI, which folder-Library recognition reads. Read only.
            from routes.dat import load_hasheous_override

            await load_hasheous_override()
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
        # Intake has no dry run: an Inbox is never polled by one (#433).
        for library_id in dict.fromkeys(i.library_id for i in await _inboxes_of(chosen)):
            line = _inbox_line(library_id, status="skipped", reason="dry_run")
            printer.summary(line, _inbox_text(line))
        return EXIT_FAILED if failed or state.stopping else EXIT_OK


async def _run_rules_pass(chosen: list, printer: Printer, state: RunState) -> int:
    from services.inbox import intake
    from services.job_manager import CANCEL_SHUTDOWN
    from services.rules import check as rules_check
    from services.rules import sweep as rules_sweep

    owners: dict[str, str] = {}
    sweeping: list[str] = []
    reported: set[str] = set()
    failed = False

    def on_terminal(job) -> None:
        nonlocal failed
        # One line per job: the job manager may announce an end twice (a
        # queued job cancelled before it starts is told by cancel_job and
        # again by _process_job).
        if job.id in reported:
            return
        reported.add(job.id)
        item = Item(path=job.file_path, mode=getattr(job.mode, "value", job.mode))
        _finish(item, job)
        if item.status == "waiting":
            # A wait is the Inbox pass's to report, with the waits no job
            # was made for (#470): one line each, from ``_inbox_pass``.
            return
        _finish_check_run(item, job)
        # An Inbox job that fails leaves its Finding and isn't a Rule's
        # failure (CodeRabbit 4608135778); an Inbox that can't be polled
        # still fails the run, from the pass.
        if getattr(job, "inbox_id", None) is None:
            failed = failed or item.status in ("failed", "cancelled")
        # An intake job names its own Library (#433, Codex 4607940252). A
        # sweep's job that ends while its sweep is still running has no owner
        # yet; the Library being swept queued it.
        library_id = (
            getattr(job, "library_id", None) or owners.get(job.id)
            or (sweeping[-1] if sweeping else None)
        )
        printer.line({"library": library_id, **item.convert_line()},
                     prefix=f"{library_id}: " if library_id else "")

    # Registered like the server's: the Rule history hears each job's outcome.
    # And an Inbox conversion is settled the moment it ends (Codex 4608095516),
    # not when the pass looks it up again, by which time history may be pruned.
    async with _job_runner(
        state, on_terminal, rules_sweep.note_job_finished, intake.on_job_finished,
    ) as jobs:
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
                await jobs.cancel_all_jobs(reason=CANCEL_SHUTDOWN)
                break
        await jobs.drain()
        # A check Rule's run is not a queued job: it runs as its own task
        # (#305), so it is waited for here, while the job runner and the
        # listeners above still hear it end. A stop cancels it promptly.
        await rules_check.drain(should_stop=lambda: state.stopping)
        if not state.stopping:
            # Then one Inbox pass (#433), as the server's tick polls them
            # after its sweeps, under the scheduler lock this run holds.
            failed = await _inbox_pass(chosen, printer, state, jobs) or failed
    return EXIT_FAILED if failed or state.stopping else EXIT_OK


# Where run-rules keeps intake's memory between runs (#433).
INBOX_SETTLE_KEY = "inbox_settle"


async def _inboxes_of(chosen: list) -> list:
    """The Inboxes feeding the Libraries in *chosen*."""
    from fastapi.concurrency import run_in_threadpool
    from services import inbox as inbox_store

    wanted = {library.id for library in chosen}
    return [i for i in await run_in_threadpool(inbox_store.all_inboxes) if i.library_id in wanted]


async def _inbox_pass(chosen: list, printer: Printer, state: RunState, jobs) -> bool:
    """One poll of every Inbox feeding *chosen*, then what runs after a pass
    (``services.inbox.report``): RomM's scan and the summary notification.

    A cron run is a new process each time, so what intake has seen settling
    is kept between runs in the preferences (``INBOX_SETTLE_KEY``). The
    conversions and copies the poll starts finish within the run, and their
    originals go to ``processed/`` before it exits (``intake.finish_all``).
    One line per Library whose Inboxes placed or left something, a
    ``waiting`` line for each archive that waits (#470), and an ``error``
    line for each Inbox that couldn't be polled. True when the pass
    failed or an Inbox couldn't be polled; an Image left with a Finding is
    not a failure. A stop admits nothing more; what already started drains.
    """
    from fastapi.concurrency import run_in_threadpool
    from services import inbox as inbox_store
    from services.inbox import intake, report
    from services.job_manager import CANCEL_SHUTDOWN
    from services.preferences_store import preferences_store

    inboxes = await _inboxes_of(chosen)
    if not inboxes:
        return False
    try:
        intake.restore_settle_memory(await preferences_store.get(INBOX_SETTLE_KEY))
        wanted = {inbox.library_id for inbox in inboxes}
        # A stop admits nothing more (Codex 4607809613); what started drains.
        # Each job it makes names its own Library (``job.library_id``, Codex
        # 4607940252), so the job lines don't depend on when a job was made.
        results = await intake.poll_all(
            library_ids=wanted, should_stop=lambda: state.stopping,
        )
        # Drained to a fixed point (#432, Codex 4608417841): an unpack or a
        # Recognize takes its Images on itself and may queue a conversion
        # after the queue last emptied, and a conversion ending starts intake
        # work. A stop cancels what is queued and admits nothing more, so the
        # loop ends then too.
        while True:
            if state.stopping:
                await jobs.cancel_all_jobs(reason=CANCEL_SHUTDOWN)
            await jobs.drain()
            await intake.drain()
            if state.stopping or not (jobs.has_live_conversions() or intake.busy()):
                break
        if state.stopping:
            # Intake's own work has drained, and may have queued a conversion
            # after the cancel above (CodeRabbit 4608968227): cancelled and
            # drained too, so the run never ends with a live job.
            await jobs.cancel_all_jobs(reason=CANCEL_SHUTDOWN)
            await jobs.drain()
        results += await intake.finish_all(library_ids=wanted)
    except Exception as exc:  # the Rule sweeps already ran; say so and fail the run
        logger.warning("run-rules: the Inbox pass failed: %s", exc, exc_info=True)
        line = _inbox_line(None, status="error", reason=f"inbox_pass_failed: {exc}")
        printer.summary(line, _inbox_text(line))
        return True
    failed = False
    try:
        every = {inbox.id for inbox in await run_in_threadpool(inbox_store.all_inboxes)}
        await preferences_store.put(INBOX_SETTLE_KEY, intake.settle_memory(every))
    except Exception as exc:
        # On its own (CodeRabbit 4608135785): what the pass did is still
        # reported and RomM still scans; the run fails and says why.
        logger.warning("run-rules: could not save intake's memory: %s", exc, exc_info=True)
        line = _inbox_line(None, status="error", reason=f"inbox_memory_not_saved: {exc}")
        printer.summary(line, _inbox_text(line))
        failed = True
    for result in results:
        # Every archive that waits, whichever check held it back (#470):
        # not a failure, and taken on a later run.
        for wait in result.get("waits") or []:
            _print_wait(printer, result["library_id"], wait)
    summary = await report.after_pass(results)
    for library_id, entry in summary["libraries"].items():
        # An Inbox that couldn't be polled (its folder gone, outside a
        # Volume, its Library missing) fails the run like a failed sweep
        # (Codex 4607809607).
        for error in entry.get("errors") or []:
            line = _inbox_line(library_id, status="error", error=error)
            printer.summary(line, _inbox_text(line))
            failed = True
        if entry["placed"] or entry["left"]:
            line = _inbox_line(library_id, status="inbox", entry=entry)
            printer.summary(line, _inbox_text(line))
    return failed


def _print_wait(printer: Printer, library_id, wait: dict) -> None:
    """The line for an Inbox archive that waits (#458, #470): under its own
    file name, as a job's line, whether or not a job was made for it."""
    item = Item(
        path=os.path.basename(wait["path"]), mode=wait["mode"], status="waiting",
        reason=wait["reason"],
    )
    printer.line({"library": library_id, **item.convert_line()},
                 prefix=f"{library_id}: " if library_id else "")


def _inbox_line(library_id, *, status: str, entry: dict | None = None,
                reason: str | None = None, error: str | None = None) -> dict:
    entry = entry or {}
    return {
        "library": library_id, "inbox": True, "status": status,
        "placed": [p.get("output") or p["path"] for p in entry.get("placed") or []],
        "left": [{"path": item["path"], "reason": item["reason"]}
                 for item in entry.get("left") or []],
        "scan": entry.get("scan"), "reason": reason, "error": error,
    }


def _inbox_text(line: dict) -> str:
    from services.inbox import report

    where = line["library"] or "every Inbox"
    if line["error"]:
        return f"{'error':<10} {where} Inbox: {line['error']}"
    if line["status"] != "inbox":
        return f"{line['status']:<10} {where} Inbox pass: {line['reason']}"
    text = (f"{'inbox':<10} {where}: {len(line['placed'])} placed, "
            f"{len(line['left'])} left with a Finding")
    said = report.scan_text(line["scan"])
    return f"{text}; {said}" if said else text


def _finish_check_run(item: Item, job) -> None:
    """A check Rule's run (#305) fails the pass like a failed Image would.

    It completes once it has checked what it picked; the CLI's rule (exit 1
    when any Image failed, as ``verify`` does) then reads its outcome: an
    Image not Intact, or one whose check reached no verdict, is a failure.
    A run that was cancelled or crashed is already a failed or cancelled job.
    """
    from models import ConversionMode
    from services.rules import check as rules_check

    outcome = rules_check.run_outcome(job.id) if job.mode == ConversionMode.CHECK else None
    if not outcome or item.status != "completed":
        return
    counts = outcome["counts"]
    if counts.get(rules_check.NOT_INTACT) or counts.get(rules_check.INCONCLUSIVE):
        item.status = "failed"
        item.reason = job.message or "A checked Image is not Intact"


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
        from services.job_manager import CANCEL_SHUTDOWN, job_manager

        asyncio.ensure_future(job_manager.cancel_all_jobs(reason=CANCEL_SHUTDOWN))

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


# --------------------------------------------------------------------------- #
# dat
# --------------------------------------------------------------------------- #


async def dat(args) -> int:
    """``dat import PATH...`` and ``dat sync``: thin wrappers over
    ``services.dat_import``, the Web UI's own. Exit 1 when anything failed.

    The files a changed DAT set may name are re-matched, local-only, as the
    server does, and the command waits for that before it exits, so a cached
    remote hit the new DAT knows upgrades to its name (Codex PR #527). RomM's
    saved settings are read first, so a sync finds its consoles.
    """
    from routes import dat as dat_routes
    from services import dat_import
    from services.romm import settings as romm_settings

    if args.dat_command == "import":
        files = [(p, os.path.abspath(p)) for p in args.paths]
        missing = [label for label, path in files if not os.path.isfile(path)]
        if missing:
            raise UsageError(f"not a file: {', '.join(missing)}")
        _open_db(False)
        result = await dat_import.import_files(files)
        await dat_routes.wait_for_match_jobs()
        lines = [f"imported {d['file']}: {d['name']} ({d['file_count']} entries)"
                 for d in result["imported"]]
        failed = bool(result["errors"])
    else:
        _open_db(False)
        await romm_settings.load()
        try:
            result = await dat_import.sync_redump(args.platform, force=args.force)
        except dat_import.SyncRunning as exc:
            print(f"compressatorium: {exc}", file=sys.stderr)
            return EXIT_FAILED
        await dat_routes.wait_for_match_jobs()
        lines = [f"{s['system']}: {s['status'].replace('_', ' ')}"
                 + (f": {s['error']}" if s.get("error") else "")
                 for s in result["systems"]]
        failed = bool(result["failed"])
    if args.json:
        print(json.dumps(result), flush=True)
    else:
        for line in lines:
            print(line, flush=True)
        for error in result.get("errors", []):
            print(f"refused {error['file']}: {error['error']}", flush=True)
        print(result["message"], flush=True)
    return EXIT_FAILED if failed else EXIT_OK


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
        if args.command == "dat":
            return await dat(args)
        return await verify(args, printer, state)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help is 0, a bad argument 2
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    with _logging(args.verbose):
        from services.scheduler_lock import SchedulerLockHeld
        from services.server_client import ServerUnreachable
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
        except ServerUnreachable as exc:
            print(f"compressatorium: not converting: {exc}", file=sys.stderr)
            return EXIT_LOCKED
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("compressatorium: stopped", file=sys.stderr)
            return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
