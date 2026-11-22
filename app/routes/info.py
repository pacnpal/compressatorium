import asyncio
import contextlib
import json
from logging_setup import get_logger
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import get_type_hints

from pydantic import BaseModel

from config import settings
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from models import (
    BulkVerifyRequest,
    ConversionMode,
    MetadataBatchRequest,
)
from services.archive import ARCHIVE_EXTENSIONS
from services.chd_metadata_store import chd_metadata_store
from services.chdman import chdman_service
from services.disc_id import (
    extract_from_chd as disc_id_extract_from_chd,
)
from services.dolphin_tool import (
    DOLPHIN_CONVERTIBLE_EXTENSIONS,
    dolphin_tool_service,
)
from services.event_stream import event_stream
from services.exodos import exodos_service
from services.extract_xiso import (
    extract_xiso_service,
)
from services.jwudtool import (
    jwudtool_service,
)
from services.rom_converto import (
    rom_converto_service,
)
from services.tools import not_ready_message, registry
from services.tools.base import ToolPlugin
from services.subprocess_runner import (
    BATCH_STOPPED_MESSAGE,
    ReadCancelled,
    Stalled,
    StorageAbandoned,
    bounded_path_check,
    collect_abandonment,
    run_detached_within_stall_bound,
    stops_verify_batch,
)
from services.workload_limiter import WorkloadToken, workload_limiter
from services.job_manager import (
    VOLUME_PROBE_TIMEOUT_SECONDS,
    ExternalJobCancelled,
    job_manager,
)
from services.maxcso import (
    maxcso_service,
)
from services.nsz import (
    nsz_service,
)
from services.romz import (
    romz_service,
)
from services.z3ds_compress import (
    z3ds_compress_service,
)
from services.verification_store import cannot_check, record_check, verification_store
from sse_starlette.sse import EventSourceResponse
from routes.volume_guard import off_loop_until_stalled
from utils.path_utils import is_within_configured_volumes, match_extension, walk_tree

DOLPHIN_INFO_EXTENSIONS = DOLPHIN_CONVERTIBLE_EXTENSIONS

logger = get_logger()
router = APIRouter()

# Lock for scanning to prevent concurrent scans
_scan_lock = asyncio.Lock()
_is_scanning = False

# How long the scan's file listing may go without reading one entry before
# the folder it is in counts as stopped answering (#361). Reset on every
# entry, so a large Library that keeps moving is never cut off.
_WALK_STALL_SECONDS = VOLUME_PROBE_TIMEOUT_SECONDS


def _verification_backpressure_detail() -> str:
    in_use = workload_limiter.in_use("verify")
    limit = workload_limiter.limit("verify")
    return (
        "Verification lane is at capacity "
        f"({in_use}/{limit}). Retry later."
    )


# How many verify events may sit undelivered before progress starts being
# dropped. The producer runs independently of the socket (that is what makes the
# bound enforceable), so a peer that stays connected without reading would
# otherwise let a chatty verifier queue events for the whole of its bound --
# hours, at the default -- with nothing to stop the buffer growing.
_VERIFY_QUEUE_LIMIT = 64


async def _offer_verify_update(queue: asyncio.Queue, update: dict) -> None:
    """Enqueue a verify event without ever blocking or growing without bound.

    Progress events are a *level*, not a log: a reader that has fallen behind
    needs the latest one, not every one it missed, so under backpressure the
    newest is simply dropped. A terminal event is the opposite -- losing it
    would leave the consumer waiting on a verdict that already happened -- so it
    makes room by discarding buffered progress, which the verdict has just made
    stale anyway. Neither path awaits, because the producer must not be
    suspended by a peer that stopped reading; that is the whole reason the
    deadline lives in the producer.
    """
    if update.get("type") == "progress":
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(update)
        return
    while queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - single producer
            break
    queue.put_nowait(update)


def _abandonment(abandoned: list) -> dict:
    """``{"abandoned": True}`` when this verify left something stuck behind.

    Two kinds of wreckage count, because both mean the same thing to a caller
    walking a list -- the storage just cost us a resource we cannot get back, so
    stop: a child that outlived SIGKILL, and a detached read whose awaiter gave
    up while it was still blocked (the only trace a verify with no child leaves).

    Both are reported into the sink opened by ``collect_abandonment()`` around
    *this* file's work, rather than read off global state, so a second request
    wedged on its own mount cannot make this batch abort files on a healthy one.
    An outer deadline is the case that needs it most: it cancels the verify
    before any terminal event exists, so there is nothing to carry a flag.
    """
    return {"abandoned": True} if abandoned else {}


async def _acquire_verify_lane_or_429() -> WorkloadToken:
    token = await workload_limiter.try_acquire("verify")
    if token is None:
        raise HTTPException(status_code=429, detail=_verification_backpressure_detail())
    return token


async def _scan_phase_dat_match(
    scan_job_id: str,
    all_paths: list[str],
    *,
    force: bool,
) -> int:
    """Prime the DAT-match cache for discovered outputs (scan Phase 3).

    Runs every discovered path (any registered format) through the shared
    per-file matcher and persists cacheable results, so RVZ/3DS/Switch
    libraries are matched by the scan just like CHDs. Already-cached paths are
    skipped unless ``force``. Non-cacheable outcomes (size-cap skips, transient
    errors) are intentionally not written, mirroring the /dat/match-batch job.
    Progress band: 65 % → 97 %. Returns the number of matched files.
    """
    # Lazy import keeps the routes modules import-order independent.
    from routes.dat import (
        HASHEOUS_ERROR,
        _match_single_file,
        cached_result_usable,
        drop_if_content_changed,
        matching_available,
        rematch_refused_verdicts,
    )
    from services.dat_store import CANDIDATE_HASHES_KEY, dat_store

    if not all_paths:
        return 0, 0
    store_ok = True
    try:
        has_dats = await run_in_threadpool(dat_store.has_dats)
    except Exception as e:
        # No DAT store available (e.g. DB not initialised); nothing to prime.
        logger.debug("Phase 3: DAT store unavailable, skipping match priming: %s", e)
        has_dats = False
        store_ok = False
    # A broken store skips the phase even when Hasheous could answer lookups:
    # this phase exists to *prime the match cache*, and every write below goes
    # through the same store. Proceeding would just fail the whole scan on the
    # next dat_store call instead of degrading quietly the way it used to.
    if not store_ok or not matching_available(has_dats):
        # Advance through the Phase 3 band so the job doesn't appear stuck at
        # the Phase 2 progress (and then jump straight to the flush) for
        # libraries with no DATs imported / no CHDs.
        await job_manager.update_external_job(
            scan_job_id,
            progress=97,
            message=(
                "Phase 3: DAT store unavailable — skipping DAT match"
                if not store_ok
                else "Phase 3: no DATs imported — skipping DAT match"
            ),
        )
        return 0, 0

    total = len(all_paths)
    logger.info("Phase 3: DAT-matching %d discovered file(s)...", total)
    await job_manager.update_external_job(
        scan_job_id,
        progress=65,
        message=f"Phase 3: DAT-matching {total} file(s)…",
    )

    # Forwarded into expensive embedded-hash hooks (e.g. dolphin-tool verify)
    # so cancelling the scan aborts the in-flight file promptly.
    cancel_event = job_manager.get_cancel_event(scan_job_id)

    # Skip paths already present in the match cache unless forced.
    cached: dict[str, dict | None] = {}
    if not force:
        cached = await run_in_threadpool(dat_store.get_matches_batch, all_paths)

    matched = 0
    # Counted, not swallowed: with the remote source down every remote-only
    # path lands on a non-cacheable error, and the phase would otherwise finish
    # "0 matched" -- indistinguishable from a library genuinely in no DAT, with
    # nothing telling the operator the rescan they asked for did not refresh
    # those identities.
    hasheous_errors = 0
    for idx, path in enumerate(all_paths, start=1):
        if job_manager.is_cancelled(scan_job_id):
            raise ExternalJobCancelled()
        if not force and cached_result_usable(cached.get(path)):
            if cached[path].get("matched"):
                matched += 1
        else:
            try:
                with collect_abandonment() as abandoned:
                    result = await _match_single_file(
                        path, cancel_event=cancel_event,
                    )
                # Abandonment before cancellation: a cancel is usually what
                # triggered the teardown that then could not kill the child, so
                # both are true at once and a clean CANCELLED would be the more
                # misleading report -- the process is still running.
                if abandoned:
                    # Not a fact about this file: the hash helper outlived
                    # SIGKILL and is still reading the volume the rest of the
                    # library is on, so every remaining path would strand
                    # another one (issue #268). Fail the scan here.
                    raise RuntimeError(
                        f"Phase 3: matching {os.path.basename(path)} left a "
                        f"process stuck on unresponsive storage "
                        f"({', '.join(abandoned)}); it is still running"
                    )
                # A cancellable hook (dolphin verify) may have been aborted
                # mid-file, returning a non-cacheable error. Treat that as
                # cancellation rather than deleting/keeping a stale row or
                # finishing "successfully" on the last/only file.
                if job_manager.is_cancelled(scan_job_id):
                    raise ExternalJobCancelled()
                # Don't cache size-cap skips or transient errors (same policy
                # as the /dat/match-batch job).
                if not result.get("reason") and not result.get("error"):
                    # A verdict a DAT change overtook is not written; it goes
                    # to a rematch instead (issue #492).
                    await rematch_refused_verdicts(
                        await dat_store.set_match(path, result), source="scan",
                    )
                elif result.get("error") == HASHEOUS_ERROR:
                    hasheous_errors += 1
                    # The remote service is down, which says nothing about this
                    # file: deleting here would let one forced rescan during an
                    # outage erase every remote match in the library (fast, too,
                    # since the breaker makes each failure instant) and still
                    # finish looking normal.
                    #
                    # But "unchanged" is a claim, not an assumption. When the
                    # rescan recomputed a file-level hash we can check it: if it
                    # no longer matches the hash the cached row was built from,
                    # the file really did change and keeping the row would show
                    # a badge naming whatever the file used to be -- and nothing
                    # would re-check it, since cached_result_usable() accepts
                    # hits unconditionally. With no recomputed hash (size cap,
                    # embedded-only) we cannot prove staleness, so the row
                    # stays: an unprovable suspicion is not worth the data loss.
                    await drop_if_content_changed(path, result)
                elif result.get(CANDIDATE_HASHES_KEY) or result.get("file_hash"):
                    # A size-capped recompute is non-cacheable, but it is no
                    # longer evidence-free: a large CHD's embedded hashes are
                    # read even when its container is not. Deleting on that
                    # unconditionally threw away a valid remote identity for a
                    # file nobody had touched, purely because the container was
                    # over the cap -- so it goes through the same same-domain
                    # comparison as the outage case and drops the row only when
                    # a recomputed hash actually contradicts it.
                    await drop_if_content_changed(path, result)
                else:
                    # Non-cacheable recompute where the *file* is the problem
                    # and nothing was recomputed at all (hash unavailable):
                    # drop any stale prior row so /dat/matches/lookup doesn't
                    # keep showing an outdated match after a (forced) rescan.
                    await dat_store.delete_match(path)
                if result.get("matched"):
                    matched += 1
            except ExternalJobCancelled:
                raise
            except Exception:
                # _match_single_file turns expected file-level problems into
                # result dicts, so reaching here means an unexpected failure
                # (matcher bug, cache write/delete error). Surface it with a
                # full trace and fail the scan rather than silently "succeed"
                # with an un-primed cache.
                logger.exception("Phase 3: DAT match failed for %s", path)
                raise
        await job_manager.update_external_job(
            scan_job_id,
            progress=65 + int(32 * idx / total),
            message=f"Phase 3 [{idx}/{total}]: {os.path.basename(path)}",
        )

    if hasheous_errors:
        logger.warning(
            "Phase 3 complete: %d/%d file(s) matched a DAT; %d could not be checked "
            "remotely (Hasheous unavailable) and will be retried",
            matched, total, hasheous_errors,
        )
    else:
        logger.info("Phase 3 complete: %d/%d file(s) matched a DAT", matched, total)
    return matched, hasheous_errors


async def scan_metadata_task(
    force: bool = False,
    lane_token: WorkloadToken | None = None,
):
    """Background task to scan all volumes for library metadata.

    Discovery is registry-driven (every registered tool's output / verify
    extensions). Phase 1 refreshes the chdman info cache and Phase 2 reads
    disc IDs into the metadata store, both CHD-only by design; Phase 3 primes
    the DAT-match cache for every discovered output (all formats).
    """
    global _is_scanning
    # Note: _is_scanning is already set to True by the trigger endpoint
    scan_start = time.monotonic()
    count = 0
    discs_found = 0
    hasheous_errors = 0
    scan_token = lane_token
    if scan_token is None:
        scan_token = await workload_limiter.acquire("metadata_scan")
    _is_scanning = True

    volumes = settings.volumes
    logger.info(
        "Metadata scan starting (force=%s) across %d volume(s): %s",
        force,
        len(volumes),
        ", ".join(volumes) if volumes else "(none)",
    )

    # Create a job entry so the scan is visible in the Jobs panel.
    scan_job = job_manager.create_external_job(
        filename="Metadata Scan",
        mode=ConversionMode.METADATA_SCAN,
        message=f"Starting scan across {len(volumes)} volume(s)\u2026",
    )
    scan_job_id = scan_job.id

    # Discovery is registry-driven: walk every extension any registered tool
    # produces or can verify (issue #131) so Dolphin/3DS/Switch outputs are
    # eligible for the scan, not just CHDs. Archive containers (.zip/.7z/.rar)
    # are excluded: they're browse-into containers handled by the archive
    # subsystem, never DAT-matchable as a whole, so the romz tool advertising
    # .7z/.zip outputs must not make the scan enumerate and hash every unrelated
    # archive under the configured volumes.
    scan_extensions = frozenset(registry.scannable_extensions()) - ARCHIVE_EXTENSIONS

    def collect_scannable_paths(*, progress):
        """Collect all scannable output paths from volumes.

        On a detached thread under a stall bound (#361): ``progress`` is
        reported for every folder, entry and file, so a frozen mount ends the
        walk at the bound and a large Library is never cut off.
        """
        paths = []
        seen: set[str] = set()
        for volume in volumes:
            progress(volume)
            if not os.path.exists(volume):
                logger.warning("Volume not found, skipping: %s", volume)
                continue
            for root, _, files in walk_tree(volume, progress):
                for file in files:
                    progress()
                    # Cheap pre-filter on the (possibly symlink) name's suffix,
                    # compared as a real suffix not a string ending so a ".ciso"
                    # isn't admitted just because it ends with ".iso".
                    if os.path.splitext(file)[1].lower() not in scan_extensions:
                        continue
                    # Resolve symlinks so discovered paths share the cache key
                    # space of the on-demand DAT-match endpoints (which realpath
                    # before caching) and the CHD metadata store, and so symlink
                    # aliases are de-duplicated.
                    real = os.path.realpath(os.path.join(root, file))
                    if real in seen:
                        continue
                    # Re-check the suffix on the *resolved* path: a symlink like
                    # ``alias.iso -> actual.ciso`` passes the name filter above
                    # but resolves to a non-scannable type the later phases
                    # shouldn't touch.
                    if os.path.splitext(real)[1].lower() not in scan_extensions:
                        continue
                    # A symlink can resolve outside the configured volumes;
                    # Phase 3 would otherwise hash/cache that target without the
                    # ACL gate the DAT endpoints apply. Drop it here.
                    if not is_within_configured_volumes(
                        real, treat_archives=False,
                    ):
                        continue
                    # Per-file veto on top of the type filter: a name a tool
                    # knows is a *slice* of an artifact rather than one itself
                    # (jwud's game_partN.wud, twelve 2 GiB pieces of one disc)
                    # can never match a DAT, so hashing it is pure waste.
                    if not registry.scannable_path(real):
                        continue
                    seen.add(real)
                    paths.append(real)
        return paths

    # Tri-state: True = success, False = failure, None = cancelled.
    scan_success: bool | None = False
    scan_error: str | None = None
    try:
        # The walk runs on a detached thread, never a shared pool worker, and
        # is given up on when it stops moving (a frozen mount) or on Cancel
        # (#361), so neither holds `_is_scanning` or the scan lane.
        try:
            all_paths = await run_detached_within_stall_bound(
                collect_scannable_paths,
                stall=_WALK_STALL_SECONDS,
                report_progress=True,
                what="Listing the files to scan",
                cancel_event=job_manager.get_cancel_event(scan_job_id),
            )
        except ReadCancelled:
            raise ExternalJobCancelled() from None
        except Stalled as exc:
            where = exc.where or "a Volume"
            logger.warning(
                "Metadata scan: the folder %s stopped answering while its files "
                "were listed (no answer in %gs); the scan stopped there",
                where, exc.seconds,
            )
            raise RuntimeError(
                f"The folder {where} stopped answering while the scan listed its "
                f"files (no answer in {exc.seconds:g}s). Scan again once it answers."
            ) from None
        total_files = len(all_paths)
        # The chdman-specific phases (info cache + disc ID) stay CHD-only; the
        # rest of the discovered outputs participate in DAT matching (Phase 3).
        chd_all_paths = [p for p in all_paths if p.lower().endswith(".chd")]
        logger.info(
            "Discovery complete: found %d output file(s) (%d CHD) across %d volume(s)",
            total_files,
            len(chd_all_paths),
            len(volumes),
        )
        await job_manager.update_external_job(
            scan_job_id,
            progress=5,
            message=f"Found {total_files} file(s) \u2014 starting metadata refresh\u2026",
        )

        # Filter stales asynchronously (CHD metadata cache is CHD-only)
        chd_paths = []
        for path in chd_all_paths:
            if force or await chd_metadata_store.is_stale(path):
                chd_paths.append(path)

        if force:
            logger.info(
                "Phase 1: Force-refreshing metadata for all %d CHD file(s)",
                len(chd_paths),
            )
        else:
            cached_count = len(chd_all_paths) - len(chd_paths)
            logger.info(
                "Phase 1: %d CHD file(s) need metadata refresh, %d already up-to-date",
                len(chd_paths),
                cached_count,
            )

        # Phase 1: Update chdman info cache for stale / forced CHDs.
        # Progress band: 5 % → 45 %.
        phase1_total = len(chd_paths)
        for idx, path in enumerate(chd_paths, start=1):
            if job_manager.is_cancelled(scan_job_id):
                raise ExternalJobCancelled()
            logger.info(
                "Phase 1 [%d/%d]: Extracting metadata from %s",
                idx,
                phase1_total,
                os.path.basename(path),
            )
            # The sink wraps the handler, not the reverse: a corrupt or
            # unreadable CHD stays isolated to its own file, but an info child
            # we could not kill has to escape it. That child is still reading
            # the volume the rest of the library is on, so continuing costs one
            # stuck process per remaining file (issue #268).
            with collect_abandonment() as abandoned:
                try:
                    cancel = job_manager.get_cancel_event(scan_job_id)
                    info, record = await chd_metadata_store.read_and_cache(
                        path,
                        lambda path=path, cancel=cancel: chdman_service.info(
                            path, cancel_event=cancel,
                        ),
                    )
                    # Changed while it was read: nothing cached, the next
                    # scan reads it again.
                    record = record or {}
                    count += 1
                    logger.info(
                        "Phase 1 [%d/%d]: Metadata cached for %s",
                        idx,
                        phase1_total,
                        os.path.basename(path),
                    )
                    logger.debug(
                        "Phase 1 [%d/%d]: Metadata for %s: game_id=%r, title=%r, info=%s",
                        idx,
                        phase1_total,
                        os.path.basename(path),
                        record.get("game_id"),
                        record.get("title"),
                        info,
                    )
                except Exception as e:
                    logger.warning(
                        "Phase 1 [%d/%d]: Failed to extract metadata from %s: %s",
                        idx,
                        phase1_total,
                        path,
                        e,
                    )
            if abandoned:
                raise RuntimeError(
                    f"Phase 1: reading {os.path.basename(path)} left a process "
                    f"stuck on unresponsive storage ({', '.join(abandoned)}); "
                    "it is still running"
                )
            if phase1_total > 0:
                await job_manager.update_external_job(
                    scan_job_id,
                    progress=5 + int(40 * idx / phase1_total),
                    message=f"Phase 1 [{idx}/{phase1_total}]: {os.path.basename(path)}",
                )

        logger.info(
            "Phase 1 complete: metadata refreshed for %d/%d CHD file(s)",
            count,
            phase1_total,
        )

        # Phase 2: read the disc ID of every CHD not checked yet (its sectors,
        # the Dreamcast GDRO tag or a companion file) and store it in the
        # metadata store, the cache /api/info reads. Nothing is written into
        # the CHD: chdman opens a compressed CHD read-only, so addmeta cannot
        # tag one (#352). Skip CHDs already checked whose file has not changed.
        # Progress band: 45 % → 65 %.
        phase2_total = len(chd_all_paths)
        already_checked = 0
        newly_checked = 0
        logger.info(
            "Phase 2: Reading disc IDs for %d CHD file(s)...",
            phase2_total,
        )
        await job_manager.update_external_job(
            scan_job_id,
            progress=45,
            message=f"Phase 2: Reading disc IDs for {phase2_total} CHD file(s)\u2026",
        )
        for idx2, path in enumerate(chd_all_paths, start=1):
            if job_manager.is_cancelled(scan_job_id):
                raise ExternalJobCancelled()
            # Sink outside the handler: the handler only logs a warning per
            # file, so an abandoned chdman child would otherwise not stop the
            # scan walking the rest of the library on that storage (#268).
            with collect_abandonment() as abandoned:
                try:
                    if await chd_metadata_store.is_disc_id_checked(path):
                        already_checked += 1
                        # Still update progress so the scan doesn't appear stuck at 45%
                        if phase2_total > 0:
                            await job_manager.update_external_job(
                                scan_job_id,
                                progress=45 + int(20 * idx2 / phase2_total),
                                message=(
                                    f"Phase 2 [{idx2}/{phase2_total}]: "
                                    f"{os.path.basename(path)} (already checked)"
                                ),
                            )
                        continue
                    logger.info("Phase 2: Scanning disc ID for %s", os.path.basename(path))
                    # Bounded and raced against Cancel (#352): a Volume that
                    # answers stat but blocks reads ends this read, not the scan.
                    result = await disc_id_extract_from_chd(
                        path, settings.chdman_path,
                        cancel_event=job_manager.get_cancel_event(scan_job_id),
                    )
                    if result and result.get("game_id"):
                        await chd_metadata_store.update_disc_id_info(
                            path, result["game_id"], result.get("title"), persist=False
                        )
                    await chd_metadata_store.mark_disc_id_checked(path)
                    newly_checked += 1
                    if result:
                        discs_found += 1
                        logger.info(
                            "Phase 2: Disc ID found for %s (game_id=%r)",
                            os.path.basename(path),
                            result.get("game_id"),
                        )
                    else:
                        logger.info(
                            "Phase 2: No disc ID found for %s, file marked as checked",
                            os.path.basename(path),
                        )
                except ReadCancelled:
                    pass  # Cancel pressed mid-read: handled just below
                except Exception:
                    # Not marked checked: a read that could not run
                    # (DiscIdReadFailed) says nothing about the file, so the
                    # next scan retries it (issue #490).
                    logger.warning(
                        "Phase 2: could not read the disc ID of %s", path, exc_info=True,
                    )
            # Cancel first: a read Cancel cut short also leaves its thread
            # behind, and that must end the scan as cancelled, not failed.
            if job_manager.is_cancelled(scan_job_id):
                raise ExternalJobCancelled()
            if abandoned:
                raise RuntimeError(
                    f"Phase 2: reading {os.path.basename(path)} left a process "
                    f"stuck on unresponsive storage ({', '.join(abandoned)}); "
                    "it is still running"
                )
            if phase2_total > 0:
                await job_manager.update_external_job(
                    scan_job_id,
                    progress=45 + int(20 * idx2 / phase2_total),
                    message=f"Phase 2 [{idx2}/{phase2_total}]: {os.path.basename(path)}",
                )

        logger.info(
            "Phase 2 complete: %d already checked, %d newly checked, %d disc ID(s) found",
            already_checked,
            newly_checked,
            discs_found,
        )

        # Phase 3: Prime the DAT-match cache for every discovered output
        # (registry-driven, all formats). This is what lets non-CHD libraries
        # participate in the scan-driven DAT-matching workflow: each file goes
        # through the per-tool embedded-hash fast path (CHD header SHA1, Dolphin
        # disc SHA1, ...) and falls back to a file-level SHA1. CHD metadata and
        # disc IDs are untouched here. Progress band: 65 % → 97 %.
        _, hasheous_errors = await _scan_phase_dat_match(
            scan_job_id, all_paths, force=force,
        )

        # Flush all accumulated changes once at the end (async, non-blocking)
        logger.info("Flushing metadata store to disk...")
        await job_manager.update_external_job(
            scan_job_id,
            progress=97,
            message="Flushing metadata store to disk\u2026",
        )
        await chd_metadata_store.flush_async()
        logger.info("Metadata store flushed.")
        scan_success = True

    except ExternalJobCancelled:
        # Clean cancellation path: partial metadata already written to the
        # store is kept (by design, see chd_metadata_store callers above).
        scan_success = None
    except Exception as e:
        logger.error("Metadata scan failed: %s", e)
        scan_error = str(e)
    finally:
        if scan_token:
            scan_token.release()
        _is_scanning = False
        elapsed = time.monotonic() - scan_start
        logger.info(
            "Metadata scan complete: %d metadata refreshed, %d disc ID(s) found, elapsed %.1fs",
            count,
            discs_found,
            elapsed,
        )
        if scan_success is None:
            final_msg = (
                f"Cancelled \u2014 {count} refreshed, {discs_found} disc ID(s) found "
                f"({elapsed:.1f}s)"
            )
            await job_manager.finish_external_job_cancelled(
                scan_job_id,
                message=final_msg,
            )
        else:
            if scan_success:
                parts = [f"{count} refreshed", f"{discs_found} disc ID(s) found"]
                if hasheous_errors:
                    # The scan itself succeeded -- metadata was collected -- so
                    # this is not a failure. But saying so without saying the
                    # remote matching phase did not run would let a rescan
                    # during an outage look like a clean "nothing matched".
                    parts.append(
                        f"{hasheous_errors} not checked (Hasheous unreachable)"
                    )
                final_msg = ", ".join(parts) + f" \u2014 {elapsed:.1f}s"
            else:
                final_msg = f"Scan failed: {scan_error or 'unknown error'}"
            await job_manager.update_external_job(
                scan_job_id,
                progress=100 if scan_success else scan_job.progress,
                message=final_msg,
            )
            await job_manager.finish_external_job(
                scan_job_id,
                success=scan_success,
                error_message=scan_error,
            )


@router.get("/version")
async def get_app_version() -> dict:
    """Get the application version."""
    # Use importlib for reliable import regardless of how app is started
    import importlib
    try:
        main_module = importlib.import_module("main")
    except ModuleNotFoundError:
        main_module = importlib.import_module("app.main")
    return {"version": main_module.get_version()}


# ============ Z3DS endpoints ============


async def _require_info_ready(tool_id: str) -> ToolPlugin:
    """The tool for a File info read, or a 503 saying why it can't run (#488).

    The info route asks this once its 403/404/400 checks pass. Whether the
    read needs the tool at all is the plugin's ``info_needs_tool``: a header
    read in Python answers without a probe, and one that runs the program asks
    ``is_ready()`` first, so a missing program is a 503 with the plugin's own
    reason (:func:`not_ready_message`) rather than a 500 from the failed run.
    The probe goes through ``registry.tool_is_ready``, whose bound keeps an
    unresponsive mount from holding the request past it.
    """
    tool = registry.get(tool_id)
    if not await registry.tool_is_ready(tool, purpose="info"):
        raise HTTPException(
            status_code=503, detail=not_ready_message(tool, purpose="info"),
        )
    return tool


# ============ File info (registry-driven) ============


async def _describe(tool: ToolPlugin, path: str):
    """One File info read, the same for every tool (#539).

    403 outside the Volumes, 404 missing, 400 for a file the tool does not
    describe (``info_extensions``), 503 when the read needs a program that
    isn't there, then ``tool.describe(path)``: 422 when the file is not what
    its name says (``info_file_errors``), 503 for a read that left a process
    on unresponsive storage (a refresh would strand another, #268), 500 else.
    """
    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    # Whole-filename suffix match, so a compound extension (`.nkit.iso`) is seen.
    if match_extension(path, tool.info_extensions) is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Not a file {tool.display_name} describes "
                f"({', '.join(sorted(tool.info_extensions))})"
            ),
        )
    await _require_info_ready(tool.id)
    try:
        return await tool.describe(path)
    except tool.info_file_errors as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except StorageAbandoned as e:
        logger.error("%s info abandoned for %s: %s", tool.display_name, path, e)
        raise HTTPException(status_code=503, detail=str(e)) from None
    except Exception as e:
        # Broad by design at the file-read boundary (OSError, struct/zlib
        # errors, a failed run); logged so the 500 is traceable.
        logger.exception("Failed to read %s info for %s", tool.display_name, path)
        raise HTTPException(
            status_code=500, detail=f"Failed to read {tool.display_name} info: {e!s}",
        ) from None


@router.get("/info/{tool_id}")
async def get_tool_info(
    tool_id: str, path: str = Query(..., description="Path to the file"),
):
    """What *tool_id* reads from the file at *path* (its info model)."""
    try:
        tool = registry.get(tool_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {tool_id}") from None
    return await _describe(tool, path)


# The URLs each tool's info had before the one route above, kept as aliases so
# the web UI's older builds and API clients (ROMarr) keep working. Each alias
# keeps its handler name (the OpenAPI operation id, and the module attribute
# the route tests call) and its typed response model. A new tool needs no
# entry: ``/api/info/{tool_id}`` answers for it.
_LEGACY_INFO_ROUTES = {
    "chdman": ("info", "get_chd_info"),
    "dolphin": ("dolphin-info", "get_dolphin_info"),
    "z3ds": ("z3ds-info", "get_z3ds_info"),
    "nkit": ("nkit-info", "get_nkit_info"),
    "xiso": ("xiso-info", "get_xiso_info"),
    "rom_converto": ("rom-converto-info", "get_rom_converto_info"),
    "cso": ("cso-info", "get_cso_info"),
    "romz": ("romz-info", "get_romz_info"),
    "jwud": ("jwud-info", "get_jwud_info"),
    "nsz": ("nsz-info", "get_nsz_info"),
}


def _register_legacy_info_route(tool: ToolPlugin, url: str, name: str) -> Callable:
    async def handler(path: str = Query(..., description="Path to the file")):
        return await _describe(tool, path)

    handler.__name__ = name
    handler.__doc__ = f"What {tool.display_name} reads from a file. Alias of /api/info/{tool.id}."
    model = get_type_hints(tool.info_model).get("return")
    router.add_api_route(f"/{url}", handler, methods=["GET"], name=name, response_model=model)
    return handler


for _tool_id, (_url, _name) in _LEGACY_INFO_ROUTES.items():
    globals()[_name] = _register_legacy_info_route(registry.get(_tool_id), _url, _name)


@router.post("/chd-metadata")
async def get_chd_metadata_batch(
    request: MetadataBatchRequest,
) -> dict:
    """Get cached metadata for multiple CHD files.

    For every requested path, an entry is returned. Invalid or non-CHD paths
    will have media_type=None, cached=False, and an error field.

    One pass on a detached thread under the no-progress bound, one report per
    path (#373): a frozen Volume answers 503 like the other bounded routes,
    instead of holding the request and a shared pool worker.
    """
    return await off_loop_until_stalled(
        _chd_metadata_entries, list(request.paths), what="Reading the CHD badges",
    )


def _chd_metadata_entries(paths: list[str], *, progress) -> dict:
    """The badge entry for each path. Blocking; via ``off_loop_until_stalled``."""
    result = {}
    for path in paths:
        progress(path)
        # Default entry for each requested path
        entry = {"media_type": None, "cached": False}
        if not is_within_configured_volumes(path, treat_archives=False):
            entry["error"] = "path_outside_configured_volumes"
        elif not os.path.isfile(path):
            entry["error"] = "file_not_found"
        elif not path.lower().endswith(".chd"):
            entry["error"] = "not_a_chd_file"
        else:
            # Only return cached data, don't run chdman info
            cached, media_type = chd_metadata_store.cached_media_type_blocking(path)
            if cached:
                entry["media_type"] = media_type
                entry["cached"] = True
                # no error field for successful, cached entries
            else:
                # Not cached yet - frontend can trigger individual info calls
                entry["error"] = "not_cached"
        result[path] = entry
    return result


@router.post("/chd-metadata/scan")
async def trigger_metadata_scan(
    background_tasks: BackgroundTasks,
    force: bool = Query(False, description="Rescan all CHD files, ignoring cache"),
):
    """Trigger a background scan of all volumes for missing CHD metadata."""
    global _is_scanning

    # Use lock to prevent race condition between check and set
    async with _scan_lock:
        if _is_scanning:
            return {"status": "scanning", "message": "Scan already in progress"}
        scan_token = await workload_limiter.try_acquire("metadata_scan")
        if scan_token is None:
            return {"status": "scanning", "message": "Scan already in progress"}
        _is_scanning = True

    background_tasks.add_task(scan_metadata_task, force, scan_token)
    if force:
        return {
            "status": "started",
            "message": "Forced metadata scan started in background",
        }
    return {"status": "started", "message": "Metadata scan started in background"}


@router.get("/chd-metadata/scan/status")
async def get_scan_status():
    """Get the status of the metadata scan."""
    return {"scanning": _is_scanning}


@router.get("/verified")
async def list_verified() -> dict:
    """List outputs that passed a check, and which check each passed.

    ``checks`` maps each path to ``"intact"``, ``"round_tripped"`` (#298) or
    ``"recognized"`` (#423) so the UI names the check instead of calling the
    file "verified".

    Prunes the records whose output is confirmed gone first. Every record is
    checked in one pass on a detached thread under the no-progress bound
    (#373): a frozen Volume answers 503 like the other bounded routes, and a
    record that can't be checked is kept.
    """
    return await off_loop_until_stalled(
        _verified_blocking, None, what="Listing the files that passed a check",
    )


# How many paths one ``/verified/lookup`` may ask about: a check run's budget
# of small Images, with room to spare, and still one bounded read.
MAX_VERIFIED_LOOKUP = 2000


class VerifiedLookup(BaseModel):
    paths: list[str]


@router.post("/verified/lookup")
async def lookup_verified(body: VerifiedLookup) -> dict:
    """``/verified``, for just *paths*: the re-read a page makes when a check
    run ends (#305), for the files it settled, in case the page missed one of
    their progress events. A path not listed has no passing check.
    """
    if len(body.paths) > MAX_VERIFIED_LOOKUP:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_VERIFIED_LOOKUP} paths per lookup",
        )
    return await off_loop_until_stalled(
        _verified_blocking, frozenset(body.paths), what="Listing the files that passed a check",
    )


def _verified_blocking(wanted: frozenset[str] | None, *, progress) -> dict:
    """The ``{verified, checks}`` answer, inside the Volumes (#373).

    ``wanted`` None is the full list, pruned first; a set narrows it to those
    paths, with no prune. Blocking; via ``off_loop_until_stalled``, which
    answers 503 once a record's Volume stops answering.
    """
    if wanted is None:
        verification_store.prune_missing_blocking(progress)
    verified = []
    checks: dict[str, str] = {}
    for record in verification_store.all_records_blocking():
        chd_path = record.get("chd_path")
        if not chd_path or (wanted is not None and chd_path not in wanted):
            continue
        progress(chd_path)
        if is_within_configured_volumes(chd_path, treat_archives=False):
            verified.append(chd_path)
            checks[chd_path] = record.get("check_passed") or "intact"
    return {"verified": verified, "checks": checks}


def tool_catalog() -> list[dict]:
    """Every tool and every mode, as the frontend registry is built from (#539).

    Each tool's extensions and default codec, and each mode's every
    ``ModeSpec`` field (``ModeSpec.as_json``), with ``reads_extensions``
    resolved by the registry. ``src/lib/tools/registry.js`` keeps only what is
    UI (glyphs, labels for codecs and groups, the info and verify bindings),
    so a new tool or mode reaches the UI without a frontend mirror.
    """
    return [
        {
            "id": tool.id,
            "label": tool.display_name,
            "input_extensions": sorted(tool.input_extensions),
            "output_extensions": sorted(tool.output_extensions),
            "verify_extensions": sorted(tool.verify_extensions),
            "disc_extensions": sorted(tool.disc_extensions),
            "default_compression": tool.default_compression,
            "modes": [
                {
                    **spec.as_json(),
                    "reads_extensions": sorted(registry.mode_reads_extensions(spec.mode)),
                }
                for spec in tool.modes
            ],
        }
        for tool in registry.all()
    ]


@router.get("/tools")
async def list_tools():
    """Every tool's formats and modes, which can run, and why the others can't.

    ``tools`` is :func:`tool_catalog`, what the frontend registry loads. A
    tool is unavailable when its own ``is_ready()`` says its runtime
    prerequisites are missing — today that's Switch (nsz), which needs the
    operator's prod.keys, but the check is the plugin's, not this route's, so
    any future gated tool is covered by implementing the hook.

    Asked through the registry's bounded seam. nsz's answer is a recursive walk
    of every configured volume, so an NFS/SMB/rclone mount that stops answering
    used to hang this route outright — and this one is the sidebar, awaited
    once per tool in turn, so a single dead volume held up the tools that were
    perfectly fine behind it. A probe that expires reports the tool
    unavailable, which is what a tool whose prerequisites cannot be read is.
    """
    ready = set(await registry.ready_tool_ids())
    available = [t.id for t in registry.all() if t.id in ready]
    unavailable = [t for t in registry.all() if t.id not in ready]
    # Why each one can't run, in the tool's own words with the fix (#515), so
    # the UI shows the tool and what to do rather than hiding it.
    return {
        "available": available,
        "unavailable": [t.id for t in unavailable],
        "reasons": {t.id: not_ready_message(t) for t in unavailable},
        "tools": tool_catalog(),
    }


# ============ Generic verify routes (registry-driven) ============
#
# The three tools expose identical verify machinery (sync verify, an SSE
# progress stream, and a batch SSE stream). The only per-tool differences are
# the URL prefix, the underlying service object, and a couple of error strings.
# ``register_verify_routes`` generates the trio for any registered tool, and
# the two ``_sse_*`` adapters hold the queue/heartbeat loop that used to be
# copy-pasted once per endpoint.


@dataclass(frozen=True)
class _VerifyRouteConfig:
    """Per-tool data the verify-route factory needs (no behavior branching)."""

    url_prefix: str          # "" -> /verify, "dolphin-" -> /dolphin-verify, ...
    service: Callable        # returns the verify service (read at call time)
    sync_name: str           # FastAPI route name == legacy function name
    events_name: str
    batch_name: str
    bad_ext_detail: str      # 400 detail when the extension is unsupported
    verify_error_prefix: str  # 500 detail prefix raised by the sync endpoint


# Each ``service`` getter resolves the module-global service name at call time
# (not at import time), so the existing route tests can rebind
# ``info_routes.<service>`` to a mock and have the factory pick it up.
_VERIFY_CONFIG: dict[str, _VerifyRouteConfig] = {
    "chdman": _VerifyRouteConfig(
        url_prefix="",
        service=lambda: chdman_service,
        sync_name="verify_chd",
        events_name="verify_chd_events",
        batch_name="verify_batch_events",
        bad_ext_detail="Not a CHD file",
        verify_error_prefix="Failed to verify CHD",
    ),
    "dolphin": _VerifyRouteConfig(
        url_prefix="dolphin-",
        service=lambda: dolphin_tool_service,
        sync_name="verify_dolphin",
        events_name="verify_dolphin_events",
        batch_name="verify_dolphin_batch_events",
        bad_ext_detail="Not a supported disc image format",
        verify_error_prefix="Failed to verify disc image",
    ),
    "z3ds": _VerifyRouteConfig(
        url_prefix="z3ds-",
        service=lambda: z3ds_compress_service,
        sync_name="verify_z3ds",
        events_name="verify_z3ds_events",
        batch_name="verify_z3ds_batch_events",
        bad_ext_detail=(
            "Not a supported compressed 3DS format "
            "(.z3ds, .zcci, .zcia, .zcxi, .z3dsx)"
        ),
        verify_error_prefix="Failed to verify 3DS ROM",
    ),
    "nsz": _VerifyRouteConfig(
        url_prefix="nsz-",
        service=lambda: nsz_service,
        sync_name="verify_nsz",
        events_name="verify_nsz_events",
        batch_name="verify_nsz_batch_events",
        bad_ext_detail="Not a supported compressed Switch format (.nsz, .xcz)",
        verify_error_prefix="Failed to verify Switch file",
    ),
    "cso": _VerifyRouteConfig(
        url_prefix="cso-",
        service=lambda: maxcso_service,
        sync_name="verify_cso",
        events_name="verify_cso_events",
        batch_name="verify_cso_batch_events",
        bad_ext_detail="Not a supported compressed CSO format (.cso, .zso, .dax)",
        verify_error_prefix="Failed to verify CSO file",
    ),
    "jwud": _VerifyRouteConfig(
        url_prefix="jwud-",
        service=lambda: jwudtool_service,
        sync_name="verify_jwud",
        events_name="verify_jwud_events",
        batch_name="verify_jwud_batch_events",
        bad_ext_detail="Not a compressed Wii U disc image (.wux)",
        verify_error_prefix="Failed to verify Wii U image",
    ),
    "romz": _VerifyRouteConfig(
        url_prefix="romz-",
        service=lambda: romz_service,
        sync_name="verify_romz",
        events_name="verify_romz_events",
        batch_name="verify_romz_batch_events",
        bad_ext_detail="Not a supported ROM archive (.7z, .zip)",
        verify_error_prefix="Failed to verify ROM archive",
    ),
    "xiso": _VerifyRouteConfig(
        url_prefix="xiso-",
        service=lambda: extract_xiso_service,
        sync_name="verify_xiso",
        events_name="verify_xiso_events",
        batch_name="verify_xiso_batch_events",
        bad_ext_detail="Not an XISO produced by this tool (.xiso.iso)",
        verify_error_prefix="Failed to verify XISO",
    ),
    "rom_converto": _VerifyRouteConfig(
        url_prefix="rom-converto-",
        service=lambda: rom_converto_service,
        sync_name="verify_rom_converto",
        events_name="verify_rom_converto_events",
        batch_name="verify_rom_converto_batch_events",
        bad_ext_detail="Not a ZAR (.zar)",
        verify_error_prefix="Failed to verify ZAR",
    ),
    "exodos": _VerifyRouteConfig(
        url_prefix="exodos-",
        service=lambda: exodos_service,
        sync_name="verify_exodos",
        events_name="verify_exodos_events",
        batch_name="verify_exodos_batch_events",
        bad_ext_detail="Not a repacked eXoDOS game (.dosz)",
        verify_error_prefix="Failed to verify eXoDOS repack",
    ),
}


async def _guard_verify_path(
    path: str, tool: ToolPlugin, cfg: _VerifyRouteConfig,
) -> None:
    """403 (outside volumes) -> 404 (missing) -> 400 (unsupported extension).

    Both filesystem checks run through the shared **bounded** seam rather than
    the app's thread pool. They land ahead of every bound the verify itself
    carries, so on a volume that stopped answering they would hang the request
    before any of that applied — and, in a shared pool, take one worker per
    attempt with them (issue #266). A volume that does not answer is a 503:
    unlike 403/404 it says nothing about the path, only that the storage is
    unreachable right now.
    """
    try:
        within_volumes = await bounded_path_check(
            is_within_configured_volumes, path, treat_archives=False,
        )
        if not within_volumes:
            raise HTTPException(
                status_code=403,
                detail="Access denied: path outside configured volumes",
            )
        if not await bounded_path_check(os.path.isfile, path):
            raise HTTPException(status_code=404, detail="File not found")
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Storage did not respond; the volume may be offline",
        ) from None
    # Whole-filename suffix match, so a compound claim (`.xiso.iso`) is seen;
    # `splitext` only ever returns the last component (`.iso`).
    if match_extension(path, tool.verify_extensions) is None:
        raise HTTPException(status_code=400, detail=cfg.bad_ext_detail)


def _verify_timed_out(bound: int) -> dict:
    """The terminal event/result for a route-level verify that outran its bound.

    The verify routes are a second entry point into the same verifiers the job
    pipeline drives, and they hold the "verify" workload lane while they run —
    so the bound has to apply here too, not only to delete-on-verify jobs
    (issue #266). Tools that spawn a subprocess enforce it themselves; this
    outer bound is what covers a verify with no child process to time out (the
    Wii U container walk, the PS3 PARAM.SFO readback) and any tool added later.
    Shaped like a tool's own timeout report, so callers need no new branch: the
    ``{"valid", "message"}`` the sync route returns, which the SSE paths widen
    with ``"type": "error"`` exactly as a tool's own terminal event carries it.
    """
    return {
        # Ran out of time: no verdict, marked at the source (#374), so the
        # batch counts it as not checkable rather than failed.
        "valid": False, "timed_out": True, "inconclusive": True,
        "message": f"Verification timed out after {bound}s",
    }


async def _record_terminal(path: str, terminal: dict | None) -> bool | None:
    """Record a verify stream's terminal result, from its producer (#351).

    Called once per verification, before the producer marks itself done and
    lets go of the verify lane, so the record is written while this check
    still holds the lane: a client that stops reading can never write an old
    verdict over a newer check's (CodeRabbit 4582189521). No terminal result
    (the verification was cancelled first) records nothing; nor does a result
    with no verdict (``check_verdict``). Never raises: a store error is logged,
    and the result still reaches the client.
    """
    if terminal is None:
        return None
    try:
        return await record_check(verification_store, path, terminal)
    except Exception:
        logger.warning("Recording the verification of %s failed", path, exc_info=True)
        return None


async def _publish_terminal(
    path: str, queue: asyncio.Queue, terminal: dict, writes: list,
) -> bool | None:
    """Persist, then publish, a verify stream's terminal result (#351).

    The verdict is written (:func:`_record_terminal`) before the event reaches
    the queue, so a reader that sees it can rely on the record (codex
    4582270883): it may stop reading and cancel the producer at once. The
    write runs as its own task under ``asyncio.shield``: a cancel (a client
    that went away) stops this wait, never the write, and the lane is let go
    only once the write has finished (:func:`_release_after_writes`, via
    *writes*). Returns the verdict recorded (``check_verdict``).
    """
    write = asyncio.ensure_future(_record_terminal(path, terminal))
    writes.append(write)
    verdict = await asyncio.shield(write)
    await _offer_verify_update(queue, terminal)
    return verdict


def _release_after_writes(writes: list, release: Callable[[], object]) -> None:
    """Let go of the verify lane once every terminal write has finished: now,
    or when the last one does (a producer cancelled mid-write leaves its
    shielded write running). Holding the lane until then means no newer check
    of the same file can be overwritten by this one's verdict."""
    pending = [write for write in writes if not write.done()]
    if not pending:
        release()
        return
    left = {"count": len(pending)}

    def one_done(_write) -> None:
        left["count"] -= 1
        if left["count"] == 0:
            release()

    for write in pending:
        write.add_done_callback(one_done)


def _sse_from_verify_stream(
    tool: ToolPlugin, cfg: _VerifyRouteConfig, path: str,
) -> EventSourceResponse:
    """Single-file verify SSE stream (verify_progress / verify_complete / verify_error)."""

    async def event_generator():
        verify_token = await workload_limiter.try_acquire("verify")
        if verify_token is None:
            yield {
                "event": "verify_error",
                "data": json.dumps(
                    {
                        "type": "error",
                        "valid": False,
                        "message": _verification_backpressure_detail(),
                    },
                ),
            }
            return

        queue: asyncio.Queue = asyncio.Queue(maxsize=_VERIFY_QUEUE_LIMIT)
        done = asyncio.Event()
        start = time.monotonic()
        bound = 0
        # The terminal writes the producer started (`_publish_terminal`), and
        # whether it ran at all: once it has, it owns the lane's release.
        writes: list = []
        producer_ran = False

        async def run_verify():
            """Produce the verify events, under the bound, whoever is reading.

            The deadline lives here rather than in the consuming loop below: a
            client that stops draining suspends that loop at its `yield`, and a
            deadline it cannot evaluate is no deadline at all -- the verifier
            would run on with the verify lane held. Bounding the producer also
            means the elapsed clock measures verification instead of SSE
            delivery backpressure, and no terminal result can arrive after the
            timeout has been reported, because the same task decides both.
            """
            nonlocal producer_ran
            producer_ran = True

            async def _pump() -> None:
                async for update in cfg.service().verify_stream(path):
                    if update.get("type") in ("complete", "error"):
                        # Persisted before it is published (codex 4582270883).
                        await _publish_terminal(path, queue, update, writes)
                    else:
                        await _offer_verify_update(queue, update)

            try:
                if bound > 0:
                    await asyncio.wait_for(_pump(), timeout=bound)
                else:
                    await _pump()
            except asyncio.TimeoutError:
                await _publish_terminal(path, queue, {
                    "type": "error",
                    **_verify_timed_out(bound),
                    **_abandonment(abandoned),
                }, writes)
            except Exception as exc:
                # The stream itself failed: no verdict on the file (`error`).
                await _publish_terminal(path, queue, {
                    "type": "error", "valid": False, "message": str(exc),
                    "error": str(exc),
                }, writes)
            finally:
                done.set()
                # The lane covers verification, and verification is over: the
                # consumer below may still be parked at its `yield` on a peer
                # that stopped reading, and holding the one-slot lane until it
                # resumes would refuse every later verification for good.
                # The generator's own finally stays the backstop for the paths
                # that never start this. Not before the terminal write is done
                # (CodeRabbit 4582189521, codex 4582270883).
                _release_after_writes(writes, verify_token.release)

        # Everything after the token is acquired runs inside the try, including
        # resolving the bound: that await is a filesystem probe, and a client
        # disconnecting during it would otherwise unwind this generator with the
        # verify lane's only token still held -- permanently, so every later
        # verification would be refused as at-capacity.
        verify_task = None
        try:
            bound = await tool.verify_timeout(path)
            # Started inside the sink's block so the task's copied context
            # reports into `abandoned` -- and only into it.
            with collect_abandonment() as abandoned:
                verify_task = asyncio.create_task(run_verify())
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=2)
                except asyncio.TimeoutError:
                    elapsed = int(time.monotonic() - start)
                    yield {
                        "event": "verify_progress",
                        "data": json.dumps(
                            {"progress": None, "message": f"Verifying... ({elapsed}s)"},
                        ),
                    }
                    if done.is_set() and queue.empty():
                        break
                    continue

                if update.get("type") == "progress":
                    yield {"event": "verify_progress", "data": json.dumps(update)}
                elif update.get("type") == "complete":
                    yield {"event": "verify_complete", "data": json.dumps(update)}
                    break
                elif update.get("type") == "error":
                    yield {"event": "verify_error", "data": json.dumps(update)}
                    break
        finally:
            if verify_task is not None:
                verify_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await verify_task
            if not producer_ran:
                # A producer that ran releases the lane itself, once its
                # terminal write is done; one that never ran cannot.
                verify_token.release()

    return event_stream(event_generator())


def _sse_batch_from_verify_stream(
    tool: ToolPlugin,
    cfg: _VerifyRouteConfig,
    valid_paths: list[str],
    verify_token: WorkloadToken,
) -> EventSourceResponse:
    """Batch verify SSE stream over an already-validated list of paths."""

    # The lane is held per *file*, not for the whole walk. The walk only
    # advances when events are delivered, so a peer that stays connected
    # without reading parks this generator between files -- with no verifier
    # running and, if the token were held for the batch, the one-slot lane
    # locked out for good. Between files nothing is verifying, so nothing is
    # owed a slot; the next file waits for the lane again, which terminates
    # because every verify is now bounded. The route hands us the slot it
    # already took for the first file.
    lane: dict[str, WorkloadToken | None] = {"token": verify_token}

    async def event_generator():
        # The route's token gated admission (429 when the lane is full); hand it
        # straight back rather than carrying it into the walk. Otherwise it
        # spans the suspensions before the first file's verifier even starts,
        # which is the same lost-lane shape on a peer that never reads.
        if lane["token"] is not None:
            lane["token"].release()
            lane["token"] = None

        total = len(valid_paths)
        verified_count = 0
        failed_count = 0
        # Files the tool refused to check here (an archive safety rule): no
        # verdict, never counted as failed (#306). Reported only when any.
        uncheckable_count = 0

        # Send initial status
        yield {
            "event": "verify_batch_start",
            "data": json.dumps({"total": total, "paths": valid_paths}),
        }

        for idx, path in enumerate(valid_paths):
            filename = os.path.basename(path)
            # Set again just before the verify task starts (below): the clock
            # must measure verification, not the time this generator spends
            # suspended on a slow client between events.
            start = time.monotonic()

            # Send file start event
            yield {
                "event": "verify_batch_progress",
                "data": json.dumps(
                    {
                        "index": idx,
                        "total": total,
                        "path": path,
                        "filename": filename,
                        "status": "verifying",
                        "verified": verified_count,
                        "failed": failed_count,
                    },
                ),
            }

            try:
                # Use the streaming verify to get progress updates
                queue: asyncio.Queue = asyncio.Queue(maxsize=_VERIFY_QUEUE_LIMIT)
                done = asyncio.Event()
                final_result = {
                    "valid": False, "message": "Unknown error", "error": "Unknown error",
                }
                # What the producer recorded for this file (`check_verdict`):
                # None until it records, and for no verdict.
                recorded: dict = {"verdict": None}
                writes: list = []

                bound = await tool.verify_timeout(path)

                async def run_verify(
                    path=path, bound=bound, queue=queue, done=done, recorded=recorded,
                    writes=writes,
                ) -> None:
                    """Same producer-side bound as the single-file stream.

                    A batch client that stops draining suspends the loop below,
                    so a deadline evaluated there would stop being evaluated
                    exactly when it matters. Here it holds regardless of the
                    reader, the clock measures verification rather than delivery
                    backpressure, and the terminal result cannot disagree with
                    the timeout because one task produces both.
                    """
                    nonlocal final_result

                    async def publish(terminal: dict) -> None:
                        nonlocal final_result
                        # Set before anything awaits, so a consumer that breaks
                        # on the terminal event reads this file's result; the
                        # verdict is persisted before the event is published.
                        final_result = terminal
                        recorded["verdict"] = await _publish_terminal(
                            path, queue, terminal, writes,
                        )

                    async def _pump() -> None:
                        async for update in cfg.service().verify_stream(path):
                            if update.get("type") in ("complete", "error"):
                                await publish(update)
                            else:
                                await _offer_verify_update(queue, update)

                    try:
                        if bound > 0:
                            await asyncio.wait_for(_pump(), timeout=bound)
                        else:
                            await _pump()
                    except asyncio.TimeoutError:
                        await publish({
                            "type": "error",
                            **_verify_timed_out(bound),
                            **_abandonment(abandoned),
                        })
                    except Exception as exc:
                        await publish({
                            "type": "error",
                            "valid": False,
                            "message": str(exc),
                            "error": str(exc),  # the stream failed: no verdict
                        })
                    finally:
                        done.set()
                        # Released here, not in the consumer's `finally`: this
                        # file's verification is over, and the consumer may be
                        # parked mid-file at a `yield` on a peer that stopped
                        # reading -- which is exactly the case where holding
                        # the one-slot lane refuses every later verification
                        # for good. Same rule as the single-file route: not
                        # before the terminal write is done.
                        token, lane["token"] = lane["token"], None
                        if token is not None:
                            _release_after_writes(writes, token.release)

                if lane["token"] is None:
                    lane["token"] = await workload_limiter.acquire("verify")

                start = time.monotonic()
                # Started inside the sink's block so the task's copied context
                # reports into this file's `abandoned` -- and only into it.
                with collect_abandonment() as abandoned:
                    verify_task = asyncio.create_task(run_verify())
                try:
                    while not done.is_set() or not queue.empty():
                        try:
                            update = await asyncio.wait_for(queue.get(), timeout=2)
                            if update.get("type") == "progress":
                                yield {
                                    "event": "verify_batch_file_progress",
                                    "data": json.dumps(
                                        {
                                            "index": idx,
                                            "path": path,
                                            "filename": filename,
                                            "progress": update.get("progress"),
                                            "message": update.get("message"),
                                        },
                                    ),
                                }
                            elif update.get("type") in ("complete", "error"):
                                break
                        except asyncio.TimeoutError:
                            elapsed = int(time.monotonic() - start)
                            yield {
                                "event": "verify_batch_file_progress",
                                "data": json.dumps(
                                    {
                                        "index": idx,
                                        "path": path,
                                        "filename": filename,
                                        "progress": None,
                                        "message": f"Verifying... ({elapsed}s)",
                                    },
                                ),
                            }
                finally:
                    verify_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await verify_task
                    # Backstop: the producer above releases as soon as the
                    # verification ends, but it never runs if the task was
                    # cancelled before it started.
                    if lane["token"] is not None:
                        lane["token"].release()
                        lane["token"] = None

                if recorded["verdict"]:
                    verified_count += 1
                    status = "verified"
                elif cannot_check(final_result):
                    # Refused here, or it could not be checked (a file the
                    # verifier could not open, #359): no verdict, so it is not
                    # counted failed.
                    uncheckable_count += 1
                    status = "uncheckable"
                else:
                    failed_count += 1
                    status = "failed"

                file_complete = {
                    "index": idx,
                    "path": path,
                    "filename": filename,
                    "status": status,
                    "valid": final_result.get("valid", False),
                    "message": final_result.get("message"),
                    "verified": verified_count,
                    "failed": failed_count,
                }
                if uncheckable_count:
                    file_complete["uncheckable"] = uncheckable_count
                yield {
                    "event": "verify_batch_file_complete",
                    "data": json.dumps(file_complete),
                }

                if stops_verify_batch(final_result, abandoned):
                    # The verifier outlived SIGKILL, so it is still reading the
                    # storage this batch is walking. Every remaining file lives
                    # on that same storage, so continuing would spawn one more
                    # unkillable verifier per file. Stop and say why.
                    yield {
                        "event": "verify_batch_complete",
                        "data": json.dumps(
                            {
                                "total": total,
                                "verified": verified_count,
                                "failed": failed_count,
                                "aborted": True,
                                "message": BATCH_STOPPED_MESSAGE,
                            },
                        ),
                    }
                    return

            except Exception as exc:
                failed_count += 1
                yield {
                    "event": "verify_batch_file_complete",
                    "data": json.dumps(
                        {
                            "index": idx,
                            "path": path,
                            "filename": filename,
                            "status": "failed",
                            "valid": False,
                            "message": str(exc),
                            "verified": verified_count,
                            "failed": failed_count,
                        },
                    ),
                }

        # Send final completion event
        summary = {"total": total, "verified": verified_count, "failed": failed_count}
        if uncheckable_count:
            summary["uncheckable"] = uncheckable_count
        yield {
            "event": "verify_batch_complete",
            "data": json.dumps(summary),
        }

    async def wrapped_event_generator():
        try:
            async for event in event_generator():
                yield event
        finally:
            # Backstop for the paths that never reach a per-file release (an
            # empty list, an early return, a disconnect before the first file).
            # Releasing twice is a no-op.
            if lane["token"] is not None:
                lane["token"].release()
                lane["token"] = None
            verify_token.release()

    return event_stream(wrapped_event_generator())


def register_verify_routes(router_: APIRouter, tool: ToolPlugin) -> tuple:
    """Register the verify trio for ``tool`` and return ``(sync, events, batch)``.

    Paths stay byte-identical via the ``tool_id -> url_prefix`` alias map, and
    each route is named after its legacy handler so OpenAPI operation ids and
    the module-level attribute names the tests rely on are preserved. A tool
    with no entry there gets :func:`_default_verify_config`.
    """
    cfg = _verify_config(tool)
    prefix = cfg.url_prefix

    @router_.get(f"/{prefix}verify", name=cfg.sync_name)
    async def _verify(path: str = Query(..., description="Path to file to verify")) -> dict:
        await _guard_verify_path(path, tool, cfg)
        verify_token = await _acquire_verify_lane_or_429()
        bound = 0
        try:
            # Inside the try, so a client that disconnects while this
            # filesystem probe is pending cannot strand the verify lane's token.
            bound = await tool.verify_timeout(path)
            result = await asyncio.wait_for(
                cfg.service().verify(path), timeout=bound or None,
            )
            # A pass refreshes the record, a real verdict that it is bad marks
            # it failed, no verdict leaves it (#351).
            await record_check(verification_store, path, result)
            return result
        except asyncio.TimeoutError:
            # A verdict-shaped answer, not a 500: the file is not known bad, the
            # check simply did not finish inside its bound. Must precede the
            # generic handler -- asyncio.TimeoutError is an Exception.
            return _verify_timed_out(bound)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"{cfg.verify_error_prefix}: {e!s}",
            ) from None
        finally:
            verify_token.release()

    @router_.get(f"/{prefix}verify/events", name=cfg.events_name)
    async def _verify_events(
        path: str = Query(..., description="Path to file to verify"),
    ) -> EventSourceResponse:
        await _guard_verify_path(path, tool, cfg)
        return _sse_from_verify_stream(tool, cfg, path)

    @router_.post(f"/{prefix}verify-batch/events", name=cfg.batch_name)
    async def _verify_batch(request: BulkVerifyRequest) -> EventSourceResponse:
        if not request.paths:
            raise HTTPException(status_code=400, detail="No paths provided")

        valid_paths = []
        for path in request.paths:
            # Bounded, like the single-file guard: one unreachable path in a
            # batch must not hang the whole request before any verify starts.
            try:
                if not await bounded_path_check(
                    is_within_configured_volumes, path, treat_archives=False,
                ):
                    continue
                if not await bounded_path_check(os.path.isfile, path):
                    continue
            except asyncio.TimeoutError as exc:
                # The *first* timeout ends validation rather than dropping this
                # path and probing the next. A batch is a list of paths on the
                # same storage: carrying on means one more abandoned thread and
                # one more probe bound per path -- minutes of delay before any
                # verify starts, and enough of them to exhaust the process-wide
                # probe ceiling, which then fails path checks on volumes that
                # are answering perfectly well.
                logger.warning(
                    "Batch verify: storage did not respond for %s; "
                    "abandoning validation of %d path(s)",
                    path, len(request.paths),
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Storage is not responding. Verification was not "
                        "started for any of the selected files."
                    ),
                ) from exc
            if match_extension(path, tool.verify_extensions) is None:
                continue
            valid_paths.append(path)

        verify_token = await _acquire_verify_lane_or_429()
        return _sse_batch_from_verify_stream(tool, cfg, valid_paths, verify_token)

    return _verify, _verify_events, _verify_batch




def _verify_config(tool: ToolPlugin) -> _VerifyRouteConfig:
    """``tool``'s verify routes: its legacy entry, else :func:`_default_verify_config`."""
    return _VERIFY_CONFIG.get(tool.id) or _default_verify_config(tool)


def _default_verify_config(tool: ToolPlugin) -> _VerifyRouteConfig:
    """Verify routes for a tool with no legacy entry: ``/api/<id>-verify``,
    read through the plugin, so a new tool that verifies gets them for free."""
    prefix = tool.id.replace("_", "-")
    return _VerifyRouteConfig(
        url_prefix=f"{prefix}-",
        service=lambda: tool,
        sync_name=f"verify_{tool.id}",
        events_name=f"verify_{tool.id}_events",
        batch_name=f"verify_{tool.id}_batch_events",
        bad_ext_detail=(
            f"Not a file {tool.display_name} verifies "
            f"({', '.join(sorted(tool.verify_extensions))})"
        ),
        verify_error_prefix=f"Failed to verify {tool.display_name}",
    )


# One loop over the registry: every tool that verifies gets the trio. The
# handlers are bound to their legacy names (``info_routes.verify_chd``, ...),
# which the route tests call directly.
for _tool in registry.all():
    if _tool.verify_extensions:
        _cfg = _verify_config(_tool)
        globals().update(zip(
            (_cfg.sync_name, _cfg.events_name, _cfg.batch_name),
            register_verify_routes(router, _tool),
        ))
