"""The shared Image rename (#425): every file of an Image to a new name, with
its Referrers following, or nothing at all.

:func:`rename_image` takes an Image and its renames map (``{old: new}``, one
entry per file), so a multi-file Image (a sheet and its tracks, #426) is the
same call with a longer map. It goes through the Referrer hook a conversion
uses (``referrers.update_before_source_removal``), with a
:class:`~services.referrers.StagedPlacement` whose ``rename`` is set: each new
name's "staged" file is the Image's own file under its old name, so a
playlist or gamelist is pointed at the new name while the file is still under
the old one, as for a conversion's staged output. A Referrer that renames the
file itself does so (RomM: its ROM rename moves the file, and the ROM keeps
its id); whatever is still under its old name afterwards is renamed here,
never over another file (``place_without_clobbering``).

All or nothing. Every old and new path is locked first (a conversion can't
read or write either meanwhile). A Referrer that fails stops the hook, and
whichever file Referrers it had already updated are put back: the files take
the new name for a moment, the file phase runs again with the renames
reversed, and the files go back. The Image is then reported as not renamed,
with the reason. Files a Referrer left elsewhere (a RomM that renamed and then
stopped answering) are never guessed at: the reason says where they are.

After a rename, what the app knows about each file follows it: its check
record, its cached ``chdman info`` and its DAT match.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services import referrers
from services.lock_manager import lock_manager
from services.referrers import HandedOff, Phase, ReferrerOutcome, StagedPlacement
from services.subprocess_runner import (
    DeadlineExpired,
    run_detached_bounded,
    run_detached_to_end,
    start_detached_or_own_thread,
)
from utils.path_utils import (
    file_identity,
    place_without_clobbering,
    volume_containment,
)

logger = get_logger("image_rename")

# How long a stat of a file on a Volume may take (the job's delete probes take
# the same).
PROBE_SECONDS = 20.0


@dataclass
class RenameOutcome:
    """What :func:`rename_image` did. ``records`` are the Referrer hook's
    (``ReferrerOutcome.records``), for the job's report."""

    renamed: bool
    reason: str | None = None
    records: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _problem_blocking(renames: Mapping[str, str]) -> str | None:
    """Why *renames* can't start, or None. Blocking. Fail closed: a path not
    confirmed inside the Volumes refuses it."""
    for old, new in renames.items():
        for path in (old, new):
            if volume_containment(path, treat_archives=False) is not True:
                return f"{path} isn't confirmed inside the configured Volumes"
        if os.path.islink(old):
            return f"{os.path.basename(old)} is a symlink"
        if not os.path.isfile(old):
            return f"{os.path.basename(old)} is gone"
        if not os.path.isdir(os.path.dirname(new)):
            return f"{os.path.dirname(new)} isn't a folder"
        if os.path.lexists(new):
            return f"{os.path.basename(new)} already exists"
    return None


def _lock_blocking(renames: Mapping[str, str]) -> tuple[list[str], str | None]:
    """Lock every old path (as it is) and every new one (which must be free).
    Returns the paths locked and, when one couldn't be, why; the caller
    releases what was locked either way."""
    held: list[str] = []
    for path, existing in [
        *((old, True) for old in renames), *((new, False) for new in renames.values()),
    ]:
        if not lock_manager.acquire_lock(path, allow_existing=existing):
            what = "is in use by a job" if existing else "is taken, or in use by a job"
            return held, f"{os.path.basename(path)} {what}"
        held.append(path)
    return held, None


def _release_blocking(paths: list[str]) -> None:
    for path in paths:
        try:
            lock_manager.release_lock(path)
        except Exception:
            # Releasing is cleanup: a lock left behind is cleared as stale.
            logger.warning("image_rename: could not release the lock on %s", path, exc_info=True)


def _fingerprints_blocking(paths) -> dict[str, dict[str, int]]:
    """Each file's fingerprint as a delete snapshot has it (size, mtime_ns,
    inode, device): what a Referrer that moves a file checks it against."""
    out: dict[str, dict[str, int]] = {}
    for path in paths:
        identity = file_identity(path)
        if identity is not None:
            out[path] = {
                "size": identity.size, "mtime_ns": identity.mtime_ns,
                "inode": identity.ino, "device": identity.dev,
            }
    return out


def _move_blocking(renames: Mapping[str, str]) -> None:
    """Rename each file still under its old name, never over another file.
    One a Referrer already renamed (old gone, new there) is left alone."""
    for old, new in renames.items():
        if not os.path.lexists(old) and os.path.lexists(new):
            continue
        place_without_clobbering(old, new)


def _where_blocking(renames: Mapping[str, str]) -> str:
    """``old`` when every file is under its old name, ``new`` when every one
    is under its new name, else ``mixed``."""
    old = all(os.path.lexists(o) and not os.path.lexists(n) for o, n in renames.items())
    new = all(os.path.lexists(n) and not os.path.lexists(o) for o, n in renames.items())
    return "old" if old else "new" if new else "mixed"


def placement_for(
    renames: Mapping[str, str], fingerprints: Mapping[str, Mapping[str, int]] | None = None,
) -> StagedPlacement:
    """The :class:`StagedPlacement` an Image rename hands the Referrer hook:
    each new name is "staged" at the file's old name, and placing it is the
    rename (``rename`` set). The preview plans with it too."""
    moves = dict(renames)

    async def place() -> None:
        await run_detached_to_end(_move_blocking, moves)

    return StagedPlacement(
        staged={new: old for old, new in moves.items()},
        place=place,
        is_placed=lambda: _where_blocking(moves) == "new",
        place_blocking=lambda: _move_blocking(moves),
        source_fingerprints=dict(fingerprints or {}),
        rename=True,
    )


def _failure(outcome: ReferrerOutcome) -> str:
    """Why the hook stopped, as the job says it."""
    if outcome.kept:
        return f"{outcome.kept} ({outcome.reason})"
    return f"couldn't update {os.path.basename(outcome.failed or '')} ({outcome.reason})"


async def _probe(func, *args):
    return await run_detached_bounded(func, *args, timeout=PROBE_SECONDS)


async def rename_image(
    image_path: str, renames: Mapping[str, str], *, library=None, cancel_event=None,
) -> RenameOutcome:
    """Rename every file of the Image at *image_path* (*renames*: ``{old:
    new}``) with its Referrers following, or nothing. *library* is the
    Image's Library, as the hook gets it. Never raises for a refusal or a
    failure: the outcome says why (see the module docstring)."""
    renames = dict(renames)
    try:
        problem = await _probe(_problem_blocking, renames)
    except (DeadlineExpired, OSError) as exc:
        problem = f"its Volume didn't answer ({exc})"
    if problem:
        return RenameOutcome(False, problem)
    held, problem = await run_detached_to_end(_lock_blocking, renames)
    handed: list[HandedOff] = []
    release = _releaser(held)
    if problem:
        release()
        return RenameOutcome(False, problem)
    try:
        return await _rename_locked(image_path, renames, library, cancel_event, handed)
    finally:
        # An apply still cleaning up keeps the locks until it ends.
        ReferrerOutcome(lingering=[h.task for h in handed]).outlive(release)


def _releaser(held: list[str]):
    def release() -> None:
        if held:
            start_detached_or_own_thread(_release_blocking, list(held))

    return release


async def _rename_locked(
    image_path: str, renames: dict[str, str], library, cancel_event,
    handed: list[HandedOff],
) -> RenameOutcome:
    """:func:`rename_image` under the locks."""
    try:
        fingerprints = await _probe(_fingerprints_blocking, list(renames))
    except OSError as exc:  # DeadlineExpired is a TimeoutError
        return RenameOutcome(False, f"its Volume didn't answer ({exc})")
    if len(fingerprints) != len(renames):
        return RenameOutcome(False, "a file of the Image couldn't be read")
    placement = placement_for(renames, fingerprints)
    hook = await referrers.update_before_source_removal(
        image_path, renames, library=library, cancel_event=cancel_event,
        placement=placement, unawaited=handed,
    )
    handed.extend(hook.handed_off)
    if hook.ok:
        try:
            await placement.place()
        except OSError as exc:
            reason = f"couldn't rename the file ({exc.strerror or exc})"
            undone = await _put_back(image_path, renames, library, hook)
            return RenameOutcome(False, _with(reason, undone), hook.records)
        await run_in_threadpool(_carry_records, renames)
        return RenameOutcome(True, records=hook.records, notes=hook.notes)
    reason = _failure(hook)
    try:
        where = await _probe(_where_blocking, renames)
    except OSError:
        where = "unknown"
    if where != "old":
        # A Referrer moved files and stopped: never guessed at.
        state = {
            "new": "the Image was left renamed", "mixed": "the Image was left partly renamed",
        }.get(where, "where its files are now couldn't be told")
        return RenameOutcome(False, _with(
            reason, f"{state}; check it and its Referrers by hand",
        ), hook.records)
    undone = await _put_back(image_path, renames, library, hook)
    return RenameOutcome(False, _with(reason, undone), hook.records)


def _with(reason: str, more: str | None) -> str:
    return f"{reason}; {more}" if more else reason


async def _put_back(
    image_path: str, renames: dict[str, str], library, hook: ReferrerOutcome,
) -> str | None:
    """Point every Referrer the hook updated back at the old names, the files
    being under their old names. None when nothing needed it or it worked,
    else what is left pointing at the new names.

    Only file Referrers can have been updated before a failure (a later phase
    runs only after every earlier one succeeded, and RomM's rename undoes
    itself): the files take the new names, the file phase runs with the
    renames reversed, and the files go back."""
    updated = [r["target"] for r in hook.records if r.get("status") == "updated"]
    if not updated:
        return None
    left = ", ".join(os.path.basename(t) for t in updated)
    back = {new: old for old, new in renames.items()}
    try:
        await run_detached_to_end(_move_blocking, renames)
    except OSError as exc:
        return f"{left} still name the new name: the file couldn't be moved to put them back ({exc})"
    undo: ReferrerOutcome | None = None
    failed: str | None = None
    try:
        fingerprints = await _probe(_fingerprints_blocking, list(back))
        undo = await referrers.update_before_source_removal(
            renames.get(image_path, next(iter(back))), back, library=library,
            placement=placement_for(back, fingerprints), upto=Phase.FILE,
        )
    except OSError as exc:  # DeadlineExpired is a TimeoutError
        failed = f"{left} may still name the new name (its Volume didn't answer: {exc})"
    finally:
        # The files go back whatever happened above.
        moved_back = await _move_back(image_path, back)
    if moved_back is not None:
        return moved_back
    if failed is not None:
        return failed
    if undo is not None and not undo.ok:
        return f"{left} may still name the new name ({_failure(undo)})"
    return None


async def _move_back(image_path: str, back: dict[str, str]) -> str | None:
    """Move the files back to their old names; None, or what went wrong."""
    try:
        await run_detached_to_end(_move_blocking, back)
    except OSError as exc:
        logger.exception("image_rename: couldn't move %s back", image_path)
        return f"the file was left under its new name: moving it back failed ({exc})"
    return None


def _carry_records(renames: Mapping[str, str]) -> None:
    """What the app knows about each renamed file follows it. Bookkeeping:
    one that can't be moved is logged, and is found again by the next check
    or match."""
    # Imported here: the stores import the job pipeline's helpers.
    from services.chd_metadata_store import chd_metadata_store
    from services.dat_store import dat_store
    from services.verification_store import verification_store

    for old, new in renames.items():
        for store, move in (
            ("check record", verification_store.move_blocking),
            ("chdman info", chd_metadata_store.move_blocking),
            ("DAT match", dat_store.move_match_blocking),
        ):
            try:
                move(old, new)
            except Exception:
                logger.warning(
                    "image_rename: couldn't carry the %s of %s to %s", store, old, new,
                    exc_info=True,
                )
