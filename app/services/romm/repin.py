"""The RomM metadata re-pin queue.

RomM identifies a CHD by the SHA-1 embedded in its header and an archive by its
largest member, so converting to those keeps the Redump/No-Intro match. Every
other format we emit (RVZ/CSO/NSZ/WUX/Z3DS) is matched on the container's own
hash, which conversion necessarily changes -- the ROM goes unidentified and
loses the artwork and metadata the user curated.

This module records the provider ids *before* a conversion runs (they are only
readable while the source is still the file RomM knows about) so they can be
re-applied once RomM has rescanned.

It lives in ``services`` rather than in the route because **both** conversion
paths need it: the manual submit in ``routes.romm`` and the unattended sweep in
``services.romm.auto``. A service importing from a route would be backwards, and
duplicating the recording logic is exactly what let the automation path silently
opt out of the guarantee the manual path makes.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

from logging_setup import get_logger
from services import db as _db
from services.job_manager import job_manager
from services.romm import DAT_SAFE_OUTPUT_EXTS, METADATA_ID_FIELDS, romm_client
from services.tools import registry
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from utils.path_utils import path_fingerprint

logger = get_logger("romm_repin")


# One UTC timestamp shape for every persisted date, owned by the module that
# owns the columns. Re-exported here because this module writes `created_at`
# and `settled_at` and its callers read them back.
utcnow_iso = _db.utcnow_iso
# Same module, same shape: `settled_at < _iso_seconds_ago(...)` is string
# arithmetic over these columns, so both sides must be formed identically.
_iso_seconds_ago = _db.iso_seconds_ago


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("db.SessionLocal not initialized")
    return _db.SessionLocal()


def mode_needs_repin(output_ext: str | None) -> bool:
    """True when this output format loses RomM's DAT match on conversion.

    The single gate both conversion paths ask, so neither can disagree about
    which formats need their metadata carried across.
    """
    return (output_ext or "").lower() not in DAT_SAFE_OUTPUT_EXTS


def metadata_ids(rom: dict) -> dict:
    """The provider ids worth carrying across, dropping the ones RomM has not set."""
    return {f: rom.get(f) for f in METADATA_ID_FIELDS if rom.get(f)}


def roms_by_local_path(
    paths: list[str], platform_id: int | None = None,
) -> dict[str, dict]:
    """Index the ROMs covering *paths* by resolved local path.

    One catalog read for the whole batch rather than a lookup per file.

    *platform_id* is the platform the caller is actually browsing, and giving
    it turns this into a single request. Without it every platform's full
    paginated catalog is downloaded in turn until each path is found -- and if
    one selected path has gone stale, *all* of them are, so submitting a
    handful of ROMs from a large library could mean hundreds of serialised API
    calls before the batch is even queued. The scan remains the fallback for a
    caller that genuinely does not know (a path handed in from elsewhere).
    """
    # realpath, to match what `local_path()` returns. Comparing an abspath key
    # against a realpath value made a symlinked library silently miss every
    # lookup, so those ROMs lost their metadata without a word.
    wanted = {os.path.realpath(p) for p in paths}
    index: dict[str, dict] = {}

    def _absorb(pid: int) -> None:
        for rom in romm_client.roms(pid):
            local = romm_client.local_path(rom)
            if local and local in wanted:
                index[local] = rom

    if platform_id is not None:
        # Unconditionally, found everything or not. A path the caller listed
        # that this platform does not have is a stale selection, and falling
        # through to the scan on its account is the expensive case this
        # parameter exists to avoid -- one dead row would send a small batch
        # through every platform's paginated catalog.
        _absorb(int(platform_id))
        return index

    for platform in romm_client.platforms():
        pid = platform.get("id")
        if pid is None or pid == platform_id:
            continue
        _absorb(pid)
        if len(index) == len(wanted):
            break
    return index


def _supersede_pending(session, output_path: str) -> int:
    """Retire any pending row for *output_path*. Returns how many were retired.

    Deliberately *not* an in-place refresh. A settle pass detaches a row's id,
    provider ids and timestamp before hashing the output, which for a multi-GB
    image takes minutes. Mutating that row meanwhile would leave the settler
    working from its old snapshot and then marking the row done -- so the
    re-planned conversion silently ends up with no pending row at all and its
    metadata is never restored.

    Retiring the old row and inserting a fresh one gives the new attempt its own
    identity. The settler's ``store_sha1``/``settle`` both filter on
    ``state == "pending"``, so they no-op against the superseded row instead of
    settling the newcomer.
    """
    return (
        session.query(_db.RommRepin)
        .filter(
            _db.RommRepin.output_path == output_path,
            _db.RommRepin.state == "pending",
        )
        .update(
            {
                "state": "abandoned",
                "detail": "Superseded by a re-planned conversion",
                "settled_at": utcnow_iso(),
            },
        )
    )


# The newest plan generation handed out, seeded from the table on first use so
# a restart never issues a generation older than a row already written.
_generation_lock = threading.Lock()
_generation: dict[str, int] = {}


def plan_generation(claimed: int | None = None) -> int:
    """The generation a re-pin plan records under.

    Taken once, when the plan *starts*, and stamped on every row it writes.
    :func:`record` never supersedes a pending row with a higher generation, so
    the order that counts is the order plans began in, not the order their
    writes happened to land. That is what keeps a slow or abandoned plan from
    overwriting the snapshot of a run that started after it.

    *claimed* is a generation an earlier request of the same run was given: a
    run planning several target modes sends one request per mode, and all of
    them belong to the moment the run began. A claim can only be as new as the
    newest generation issued, so a client cannot jump ahead of a run that
    really did start later. ``None`` (or a non-positive claim) issues a new one.
    """
    with _generation_lock:
        if "last" not in _generation:
            with _session() as session:
                _generation["last"] = int(
                    session.query(func.max(_db.RommRepin.plan_generation)).scalar()
                    or 0,
                )
        if claimed is not None and claimed > 0:
            return min(int(claimed), _generation["last"])
        _generation["last"] += 1
        return _generation["last"]


def _refusal(
    session, output_path: str, generation: int,
    owned_by_live_job: Callable[[str, str | None], bool] | None,
) -> str | None:
    """Why this record must not supersede the destination's pending row, if so.

    The pending row stays when a newer plan wrote it, or when a live job is
    converting that row's own source into this destination. Either way the
    row is the one the conversion that will actually run depends on.
    """
    current = (
        session.query(_db.RommRepin)
        .filter(
            _db.RommRepin.output_path == output_path,
            _db.RommRepin.state == "pending",
        )
        .first()
    )
    if current is None:
        return None
    if (current.plan_generation or 0) > generation:
        return "a newer plan owns it"
    if owned_by_live_job is not None and owned_by_live_job(
        current.output_path, current.source_path,
    ):
        return "a queued job owns it"
    return None


def _insert_pending(
    session, rom: dict, output_path: str, ids: dict, mode: str | None,
    pre_fingerprint: str | None, source_path: str | None, generation: int,
):
    """Add the new pending row and hand it back, so its id can be returned."""
    row = _db.RommRepin(
        source_rom_id=rom.get("id"),
        source_name=rom.get("name") or rom.get("fs_name"),
        output_path=output_path,
        source_path=_canonical(source_path),
        pre_fingerprint=(
            path_fingerprint(output_path)
            if pre_fingerprint is None else pre_fingerprint
        ),
        mode=mode,
        metadata_ids=ids,
        platform_id=rom.get("platform_id"),
        # "" when RomM gave no platform at all; NULL means "ask the source ROM"
        # -- rows written before the column existed, and a record that names a
        # platform id without its slug, which the settle pass backfills.
        platform_slug=rom.get("platform_slug") or (
            None if rom.get("platform_id") is not None else ""
        ),
        plan_generation=generation,
        state="pending",
        created_at=utcnow_iso(),
    )
    session.add(row)
    return row


def record(
    rom: dict, output_path: str, ids: dict, mode: str | None = None,
    pre_fingerprint: str | None = None, source_path: str | None = None,
    *,
    generation: int | None = None,
    owned_by_live_job: Callable[[str, str | None], bool] | None = None,
) -> int:
    """Insert a pending row unless one already covers this output.

    Returns the new row's id -- the handle :func:`cancel` needs. Truthy for
    every real row (ids start at 1), so a caller that only asks "did this
    record?" reads the same as it did when this returned a bool. ``0`` means
    the record was refused (see *generation* below) and nothing was written.

    *generation* is the plan's :func:`plan_generation`, taken when the plan
    started; omitted, a new one is issued now, which is right for a caller
    that records the moment its own job is queued (the automation path). The
    destination's pending row is superseded only when it is no newer than
    this plan **and** *owned_by_live_job* (given the row's output and source)
    does not report a queued or running job converting that row's source.
    Either answer means the row belongs to the conversion that will actually
    run, and replacing it is how a late or abandoned plan used to strip that
    conversion of its metadata: the planner's own cleanup then retired the
    replacement too.

    The dedupe on ``output_path`` is what makes re-submitting the same batch
    harmless -- the second submit updates the existing row instead of stacking
    another.

    Re-recording supersedes rather than mutates: the old pending row is retired
    and a fresh one inserted, so exactly one pending row per output survives
    (which is what makes re-submitting a batch harmless) while a settle pass
    already in flight cannot settle the new attempt on the old one's behalf.

    *pre_fingerprint* lets a caller supply the destination's state as of when
    it planned the conversion. The automation path must: it records after the
    queue accepts the batch, by which point a fast job on an idle queue may
    already have written the output, and stating it here would save the
    finished file as the "before" picture. Omitted, it is taken now, which is
    correct for the manual path -- that records before the batch is submitted.

    *source_path* is the local file the ids were read from. The dedupe key is
    the destination, which is right -- one destination is produced once -- but
    it leaves the row unable to say *which* conversion it belongs to. Two
    clients can plan different sources onto one destination; recording the
    source is what lets :func:`owned_by_job` answer "is the job writing there
    the one this row describes" instead of the weaker "is anything writing
    there".

    A partial unique index (``ux_romm_repin_pending_output``) is the actual
    guarantee that only one pending row exists, so a manual submit racing an
    automation sweep cannot stack two. Losing that race is not an error: retry
    once, superseding whichever row won -- after asking the same ownership
    question of it, since the winner may be exactly the row this must not
    replace.
    """
    if generation is None:
        generation = plan_generation(None)
    with _session() as session:
        for attempt in (1, 2):
            refused = _refusal(session, output_path, generation, owned_by_live_job)
            if refused:
                logger.info(
                    "romm_repin: not recording %s; %s", output_path, refused,
                )
                session.rollback()
                return 0
            _supersede_pending(session, output_path)
            # `source_path` rides along on the retry too: a row that loses the
            # unique-index race and is re-inserted without it could not prove
            # which conversion it belongs to, and would be retired as unproven
            # the moment its planner tidied up.
            row = _insert_pending(
                session, rom, output_path, ids, mode, pre_fingerprint,
                source_path, generation,
            )
            try:
                session.commit()
                return int(row.id)
            except IntegrityError:
                session.rollback()
                if attempt == 2:
                    raise
        return 0


def produced_companions(output_path: str, mode: str | None) -> list[str]:
    """Files the owning tool actually produced for *output_path*, if not itself.

    A makeps3iso ``-s`` build only splits past 4 GB, and until it finishes
    nobody knows whether it will: under 4 GB it writes the bare output and the
    row settles normally, over it writes ``<name>.iso.0``, ``.1``, ... and no
    bare file at all. From the recorded path alone that second case is
    indistinguishable from a conversion that never ran, so the row would wait
    out ``repin_abandon_days`` and retire with a message that is simply untrue.

    Registry-driven rather than a suffix guess: ``companion_outputs`` is the
    tool's own answer, and reports the parts only when there is no primary
    file, which is exactly the case worth naming.
    """
    if not mode:
        return []
    try:
        tool = registry.for_mode(mode)
    except KeyError:
        return []
    try:
        return list(tool.companion_outputs(output_path, mode))
    except (OSError, KeyError, ValueError):
        return []


def _canonical(path: str | None) -> str | None:
    """*path* as the job manager and the settle pass both spell it.

    Pure string work: `normpath(abspath())`, no `realpath`. A comparison that
    stats is a comparison that can block on a dead mount, and this one runs on
    the request path.
    """
    if not path:
        return None
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def owned_by_job(
    output_path: str, source_path: str | None,
    *, spell: Callable[[str], str | None] = _canonical,
) -> bool:
    """Is a queued or running job converting *source_path* into *output_path*?

    The question a row's *ownership* turns on: "is anything writing here" is
    not enough, because two clients can plan different sources onto one
    destination, and keeping the wrong one's row stamps one game's identity on
    another's output.

    *spell* turns each path into the form compared. The default is
    :func:`_canonical`, pure string work, so :func:`record` can ask this inside
    its transaction without touching a volume. A caller that can afford the
    filesystem (bounded, off the loop) passes a resolving one instead, which
    also sees through symlinked ancestors; a path it cannot spell is skipped.

    A row with no ``source_path`` cannot answer, and answers **no**: it was
    written before the column existed, so keeping it would restore exactly the
    behaviour this replaced.
    """
    if not source_path:
        return False
    target, wanted = spell(output_path), spell(source_path)
    if target is None or wanted is None:
        return False
    for _job_id, paths in job_manager.get_active_job_candidates():
        spelled = {spell(candidate) for candidate in paths}
        # `get_active_job_candidates` reports a job's input *and* output paths
        # together, so one job carrying both is the job this row describes.
        if target in spelled and wanted in spelled:
            return True
    return False


def outputs_for(row_ids: list[int]) -> dict[int, tuple[str, str | None]]:
    """``{row id: (output_path, source_path)}`` for the pending rows given.

    Both halves, because the caller's real question is not "is a job writing
    there" but "is the job writing there **this row's** conversion" -- and a
    row whose ``source_path`` is null (written before the column existed)
    cannot answer it, so it is reported as unproven rather than as a match.
    """
    if not row_ids:
        return {}
    with _session() as session:
        return {
            int(row_id): (str(path), source)
            for row_id, path, source in session.query(
                _db.RommRepin.id, _db.RommRepin.output_path,
                _db.RommRepin.source_path,
            ).filter(
                _db.RommRepin.id.in_(list(row_ids)),
                _db.RommRepin.state == "pending",
            ).all()
        }


def cancel(row_ids: list[int]) -> int:
    """Retire the pending rows *row_ids*. Returns how many were retired.

    The plan-then-submit pair is two requests, and the second one can fail
    (backpressure, a validation error, a closed tab). Without this the rows sit
    pending until they age out, counting against the badge and describing a
    conversion that is never going to happen.

    Keyed by row id, which is why :func:`record` returns one. Cancelling by
    *destination* retired whatever pending row that path happened to hold, and
    the row is not necessarily the caller's: re-recording supersedes, so a
    second client planning the same output between this caller's plan and its
    cancel owns the row by then -- and its conversion, already queued, would
    lose the metadata this caller was only tidying up after itself. An id names
    one row, so a superseded caller retires nothing.

    Retire, never delete: a settled row is the history of what was planned, and
    the partial unique index only constrains *pending* rows, so retiring frees
    the path for the next attempt.
    """
    if not row_ids:
        return 0
    with _session() as session:
        retired = (
            session.query(_db.RommRepin)
            .filter(
                _db.RommRepin.id.in_(list(row_ids)),
                _db.RommRepin.state == "pending",
            )
            .update(
                {
                    "state": "abandoned",
                    "detail": "The conversion was never submitted",
                    "settled_at": utcnow_iso(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return int(retired)


def retire_all_pending(detail: str) -> int:
    """Retire every outstanding row. Returns how many were retired.

    For when the rows stop meaning anything: pointing Compressatorium at a
    different RomM instance (or a different library root) leaves rows holding
    the *old* instance's provider ids and an output digest taken from the old
    library. The settle pass would then hand those ids to whatever ROM the new
    instance happens to match that digest to -- stamping one library's identity
    onto another's game. There is no way to re-home them, so they are retired
    with the reason.

    Outstanding includes ``settling``, not just ``pending``. A claim outlives
    its holder when the process dies mid-write, and both `pending_rows` and
    `claim` deliberately hand a stale one back out again -- so a row left
    behind here would be re-claimed after `_CLAIM_STALE_SECONDS` and apply the
    *old* instance's provider ids to whatever the new one matches its digest
    to, which is the exact outcome this function exists to prevent. A claim
    held by a pass running right now is not a concern: the callers serialise
    against the settler before swapping identity.

    Retire, never delete, like :func:`cancel`: the row is the record of what
    was planned, and only *pending* rows are constrained by the unique index.
    """
    with _session() as session:
        retired = (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.state.in_(("pending", SETTLING)))
            .update(
                {
                    "state": "abandoned",
                    "detail": detail,
                    "settled_at": utcnow_iso(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return int(retired)


def pending_rows(limit: int, *, after_id: int = 0) -> list[tuple]:
    """A page of pending rows, oldest first, starting after *after_id*.

    The cursor matters: rows whose job was cancelled (or whose output RomM never
    scans) stay pending until they age out, and always taking the oldest N would
    let such a prefix occupy the whole page forever, so conversions behind it
    would never be examined. The caller walks past them.
    """
    with _session() as session:
        # Claimed rows come back too, but only once their claim is stale: the
        # only way one outlives `_CLAIM_STALE_SECONDS` is the process holding it
        # dying mid-write, and without this that row would sit in `settling`
        # forever -- invisible to every later pass and never retried.
        stale_before = _iso_seconds_ago(_CLAIM_STALE_SECONDS)
        rows = (
            session.query(_db.RommRepin)
            .filter(
                or_(
                    _db.RommRepin.state == "pending",
                    and_(
                        _db.RommRepin.state == SETTLING,
                        _db.RommRepin.settled_at < stale_before,
                    ),
                ),
            )
            .filter(_db.RommRepin.id > after_id)
            .order_by(_db.RommRepin.id)
            .limit(limit)
            .all()
        )
        # Detach into plain tuples: the session closes before the caller awaits.
        return [
            (r.output_path, r.output_sha1, r.source_rom_id, dict(r.metadata_ids or {}),
             r.created_at, r.id, r.pre_fingerprint or "", r.mode or "",
             r.platform_id, r.platform_slug)
            for r in rows
        ]


def store_platform(row_id: int, platform_id: int, platform_slug: str | None) -> None:
    """Backfill the platform onto a row written before it was recorded.

    Pending rows only, like :func:`store_sha1`: a row that settled or was
    superseded meanwhile is history and stays as it was.
    """
    with _session() as session:
        session.query(_db.RommRepin).filter(
            _db.RommRepin.id == row_id,
            _db.RommRepin.state == "pending",
        ).update({"platform_id": platform_id, "platform_slug": platform_slug})
        session.commit()


def store_sha1(row_id: int, sha1: str) -> None:
    """Cache the output's hash on the row the settle pass is working on.

    Keyed by primary key, not by ``output_path``: a row can be settled and the
    same path re-recorded by a later conversion while a pass is mid-flight, and
    matching on the path would then stamp the *new* row with the old file's
    digest -- which, because the hash is cached, that ROM could never recover
    from.
    """
    with _session() as session:
        session.query(_db.RommRepin).filter(
            _db.RommRepin.id == row_id,
            _db.RommRepin.state == "pending",
        ).update({"output_sha1": sha1})
        session.commit()


# A row being written to RomM right now. Not "pending" (so a re-plan for the
# same output inserts its own row rather than waiting on this one) and not
# settled (so the pass can still close it, and a crash mid-write leaves it
# recoverable rather than done).
SETTLING = "settling"
# How long a claim may look unfinished before another pass takes it back. Only
# reached when the process holding it died mid-write; a live claim is released
# either way within one outbound request.
_CLAIM_STALE_SECONDS = 900


def _has_newer_pending(session, row) -> bool:
    """Is another pending row already covering this row's output?"""
    return bool(
        session.query(_db.RommRepin.id)
        .filter(
            _db.RommRepin.output_path == row.output_path,
            _db.RommRepin.state == "pending",
            _db.RommRepin.id != row.id,
        )
        .first()
    )


def claim(row_id: int) -> bool:
    """Take ownership of a pending row for the outbound write. Atomic.

    The re-check this replaces was a read followed by a write, and the window
    between them is exactly where a re-plan lands: `record()` supersedes the
    row, and the pass -- already past its check -- pushes the previous
    generation's provider ids to RomM anyway. One conditional UPDATE closes it,
    because whoever flips `pending` to `settling` is the only writer, and a
    re-plan arriving afterwards gets a row of its own instead of quietly
    replacing one that is already being applied.
    """
    with _session() as session:
        row = session.get(_db.RommRepin, row_id)
        if row is None:
            return False
        stale_claim = (
            row.state == SETTLING
            and (row.settled_at or "") < _iso_seconds_ago(_CLAIM_STALE_SECONDS)
        )
        if stale_claim and _has_newer_pending(session, row):
            # A claim whose holder died, *and* a later conversion has since
            # planned the same output. Re-issuing the old row would let this
            # pass hash the newer conversion's product and stamp the abandoned
            # attempt's provider ids onto it -- the misidentification the whole
            # claim mechanism exists to prevent. The newer row is the one that
            # describes what is actually going to be written.
            row.state = "abandoned"
            row.detail = "Superseded by a re-planned conversion"
            row.settled_at = utcnow_iso()
            session.commit()
            return False
        updated = session.query(_db.RommRepin).filter(
            _db.RommRepin.id == row_id,
            or_(
                _db.RommRepin.state == "pending",
                # A claim the holder never released: the only way to outlive
                # `_CLAIM_STALE_SECONDS` is the process dying mid-write.
                # `pending_rows` hands such a row back out, so refusing it here
                # would mean every later pass fetched it and then failed on it,
                # forever.
                and_(
                    _db.RommRepin.state == SETTLING,
                    _db.RommRepin.settled_at < _iso_seconds_ago(_CLAIM_STALE_SECONDS),
                ),
            ),
        ).update(
            {"state": SETTLING, "settled_at": utcnow_iso()},
            synchronize_session=False,
        )
        session.commit()
        return bool(updated)


def release(row_id: int) -> bool:
    """Hand a claimed row back. Returns whether it went back to ``pending``.

    Restores ``pending`` unless a newer pending row already covers the same
    output, in which case this claim is retired instead. ``record()``
    supersedes only *pending* rows, so a re-plan arriving while this row was
    ``settling`` leaves both -- and restoring then violates
    ``ux_romm_repin_pending_output``. The IntegrityError surfaces inside the
    failure handler that called this, replacing the real error with a 500 and
    stranding the claim as ``settling`` until it ages out.

    Retiring is also the right answer on the merits: the newer row describes
    the conversion that is actually going to happen, and this one's ids belong
    to a generation that has been superseded.

    The insert can also land between the check and the write, so the
    constraint gets the last word: on IntegrityError, retire.
    """
    with _session() as session:
        row = session.get(_db.RommRepin, row_id)
        if row is None or row.state != SETTLING:
            return False
        superseded = _has_newer_pending(session, row)
        if not superseded:
            row.state = "pending"
            row.settled_at = None
            try:
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                row = session.get(_db.RommRepin, row_id)
                if row is None or row.state != SETTLING:
                    return False
        row.state = "abandoned"
        row.detail = "Superseded by a re-planned conversion"
        row.settled_at = utcnow_iso()
        session.commit()
        return False


def settle(
    row_id: int, state: str, detail: str | None = None, new_id: int | None = None,
) -> bool:
    """Close out one re-pin row. Keyed by primary key, for the reason above.

    Returns whether a row was actually closed, so a caller cannot count a
    settle that hit a superseded row as a success. A row this pass has claimed
    counts too -- claiming is how it stopped being superseded.
    """
    with _session() as session:
        values: dict = {"state": state, "settled_at": utcnow_iso()}
        if detail:
            values["detail"] = detail
        elif new_id is not None:
            values["detail"] = f"Re-pinned to RomM rom {new_id}"
        updated = session.query(_db.RommRepin).filter(
            _db.RommRepin.id == row_id,
            _db.RommRepin.state.in_(("pending", SETTLING)),
        ).update(values, synchronize_session=False)
        session.commit()
        return bool(updated)


def settle_swapped(output_path: str, rom_id: int) -> int:
    """Close *output_path*'s pending re-pin for ROM *rom_id* as done (#391).

    A RomM swap renamed that ROM's file to the output instead of letting RomM
    add a new ROM, so the provider ids never left it: there is nothing to
    re-apply. A row being settled right now is closed too; its settle then
    finds it closed and writes nothing. Returns how many rows were closed.
    """
    with _session() as session:
        updated = session.query(_db.RommRepin).filter(
            _db.RommRepin.output_path == output_path,
            _db.RommRepin.source_rom_id == int(rom_id),
            _db.RommRepin.state.in_(("pending", SETTLING)),
        ).update(
            {
                "state": "done",
                "settled_at": utcnow_iso(),
                "detail": f"RomM kept ROM {int(rom_id)}: its file was renamed to the output",
            },
            synchronize_session=False,
        )
        session.commit()
        return int(updated)


def is_pending(row_id: int) -> bool:
    """Whether this re-pin attempt is still the live one for its output.

    Checked immediately before the outbound metadata write. A settle pass holds
    a detached row for as long as hashing a multi-GB output takes; if the same
    output is re-planned in that window the row is superseded, and pushing its
    provider ids to RomM afterwards would overwrite the *new* conversion's
    identity with the previous generation's.
    """
    with _session() as session:
        return (
            session.query(_db.RommRepin)
            .filter(_db.RommRepin.id == row_id, _db.RommRepin.state == "pending")
            .count()
            > 0
        )


def count_pending() -> int:
    if _db.SessionLocal is None:
        return 0
    with _session() as session:
        # Rows mid-write count as outstanding, and so does a claim whose
        # holder died: both still need re-matching, and hiding them understated
        # the badge and let the background settler think there was no work.
        return (
            session.query(_db.RommRepin)
            .filter(
                _db.RommRepin.state.in_(("pending", SETTLING)),
            )
            .count()
        )
