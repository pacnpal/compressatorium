"""The RomM swap (#391): converting an Image in a RomM Library keeps its ROM id.

Without it, a conversion that deletes its Source leaves RomM a new file and a
missing one: RomM adds a new ROM and keeps a ghost holding the saves, states,
notes, collections and play history. With the Library's swap switch on
(``swap_keep_rom_id``, #390), this Referrer renames the ROM's file in RomM
instead, so the record, and everything on it, stays.

It is the last-phase Referrer (``Phase.NETWORK``): it runs only after every
file Referrer (``.m3u``, gamelist.xml) succeeded, so a failure there leaves
RomM untouched. It applies only to a single-file Image RomM knows as a
single-file ROM, converted to one output file beside it; anything else keeps
today's behaviour (the job deletes the Source and the re-pin queue carries the
metadata). Archives (``.zip``, ``.7z``) are left out too: RomM hashes their
largest member, so the read-back could not confirm the new file.

The flow is the one the spike proved on RomM 5.4.1 (#382,
``docs/ROMM_INTEGRATION_ROADMAP.md``, "3.0 spike findings"). The job keeps
the checked output hidden in its staging slot until this runs
(``places_output``, ``StagedPlacement``), because RomM refuses a rename onto a
name a file already has. Then, each step written to the swap journal
(``services.romm.swap_journal``) before it runs:

1. ``renaming``: ``PUT /api/roms/{id}`` with the output's ``fs_name``. RomM
   moves the Source to that name itself. The name is read back: a 409, or a
   name other than the one asked for (RomM sanitizes names silently), is a
   refusal; any other answer from RomM means nothing changed.
2. ``parking``: the Source, now under the output's name, is moved into a
   parking slot in the hidden staging folder (``staging.open_slot(...,
   parking=True)``), which no sweep removes, under its original name
   (#415), so no parked file carries the output's name.
3. ``placing``: the job's staged output is moved to that name.
4. ``rehashing``: a ``hashes`` scan of that one ROM (``apis: []``), then the
   ROM is read back. Success is judged by the read-back (name, size and
   SHA-1 equal the output's), never by the task's stats, which say
   ``updated_roms: 0`` even when it worked.
5. ``deleting``: the parked Source is deleted, once it is checked to be the
   same file (size and mtime) the swap started from.

A failure is undone so that the files and RomM are left as a kept Source
would leave them, and the job says why:

* Before RomM renamed (a 409, a 500, any other answer): nothing to undo. The
  journal row ends ``refused`` or ``failed``.
* After RomM renamed (or wrote another name, or didn't answer) but before the
  output is in place: the Source is moved back under the name RomM gave it,
  and RomM is asked to put the old name back (``reverting``; a no-op when it
  never renamed), which moves the Source back. Ends ``refused`` or
  ``rolled_back``.
* Once the output is in place (the rehash failed or didn't match, or the
  parked Source couldn't be deleted): the Source is put back under its own
  name beside the output (``restoring``), and the ROM keeps its id on the
  checked output; RomM's next scan refreshes its hashes. Ends ``kept``.

A Cancel or the hook's bound stops the forward step it lands on once that
step's outcome is recorded (each move sets its flag and writes its journal
outcome on its own thread, so a cancel can't come between the move and its
record). Then the same undo runs, each step journaled, within the hook's
``CANCEL_GRACE_SECONDS`` (#383); past the grace the hook stops waiting and
hands this task the job's holds (``ReferrerOutcome.outlive``), so the undo
never runs with them gone. The output is held back from the job
(``StagedPlacement.held``) until the undo has finished. Only an undo that
runs out of grace, or fails, or a process that dies, leaves the row on the
step it reached for the next run (``swap_recovery``, #392), with the reason, a parked Source
still parked and the output still held: fail closed. At shutdown the job's
task is cancelled: the hook cancels this task too and hands it the job's
holds on that path as well (``unawaited``), so they are let go only when the
undo ends; the undo runs as far as the event loop lets it before the process
exits, each step journaled, and a parked Source is never removed.

The swap is bound to the RomM instance it planned against (``_Binding``):
every RomM call goes to that URL with that token, and before each RomM write
it checks RomM's settings still name it. If they don't, nothing more is
changed on either instance and the row is left for the next run. A settings
save that moves the instance waits for running swaps (``identity_change``).

The parking slot is removed only when its own listing is empty, never on a
flag's word.

Which ROM: the Platform is the RomM Library's Platform folder holding the
file (``platform_folder``, both RomM layouts and subfolders), and the record
must be the file itself. A file RomM doesn't know as a ROM of its own (no
Platform folder, no RomM Platform for it, no record, or a record that is its
folder) keeps today's behaviour. One whose ROM can't be told (the folder or
RomM can't be read, or two Platform folders or records claim it) keeps the
Source, so the record is never orphaned.

With the output already in place when the hook runs (a retried delete after
a reader held the Source), RomM can't rename onto it: the delete takes
today's path, the re-pin is queued, and the job and the Ledger say the ROM
did not keep its id (``_fall_back``).

Every read of a file or of RomM runs on a detached thread under a bound
(``run_detached_bounded``, ``run_detached_within_stall_bound``), and every
move on one never abandoned (``start_detached_or_own_thread``), never on the event loop's
shared executor.

A successful swap closes the output's pending re-pin row as done
(``repin.settle_swapped``): the provider ids never left the ROM.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from logging_setup import get_logger
from services import referrers as referrers_module
from services import staging
from services.file_hasher import compute_file_sha1_sync
from services.referrers import (
    Applied,
    Phase,
    Referrer,
    ReferrerChange,
    ReferrerError,
    StagedPlacement,
    registry,
)
from services.referrers.base import check_write, hook_running, staged_placement
from services.romm import settings as romm_settings
from services.romm import swap_journal
from services.romm.client import (
    UNHASHED_PLATFORM_SLUGS,
    RommClient,
    RommError,
    romm_client,
)
from services.subprocess_runner import (
    run_detached_bounded,
    run_detached_within_stall_bound,
    start_detached_or_own_thread,
)
from utils.path_utils import FileIdentity, file_identity

logger = get_logger("romm_swap")

# The Referrer's id: what the RomM Library's swap switch turns on, and what
# the Ledger's referrers_updated records name.
SWAP_REFERRER = "romm_swap"

# What the job says when the swap keeps the Source: "converted, source kept:
# <this> (<reason>)".
REFUSED = "RomM refused the rename"
UNCONFIRMED = "RomM didn't confirm the new file"

# RomM hashes an archive's largest member, so the read-back can't confirm one.
_NOT_SWAPPED = frozenset({".zip", ".7z"})

# How long RomM's rehash of one ROM may take, and how often it is asked.
REHASH_SECONDS = 300.0
POLL_SECONDS = 0.5
_TASK_ENDED = frozenset({"finished", "failed", "stopped", "canceled", "cancelled"})


@dataclass(frozen=True)
class _Binding:
    """The RomM instance a swap planned against (Codex 4594286722): its URL
    and token, and RomM's identity generation then
    (``romm_settings.identity_generation``, bumped by every save that moves
    the instance or library root). Every call of the swap goes to this
    instance, undo included, never through the settings-following client."""

    base_url: str
    token: str
    generation: int

    @classmethod
    def now(cls) -> _Binding:
        return cls(
            base_url=romm_client.base_url,
            token=romm_settings.token() or "",
            generation=romm_settings.identity_generation(),
        )

    def client(self) -> RommClient:
        return RommClient(base_url=self.base_url, token=self.token)

    def still_live(self) -> bool:
        """Whether RomM's settings still name this instance."""
        return (
            romm_settings.identity_generation() == self.generation
            and romm_client.base_url == self.base_url
        )


# Swaps running now, and identity changes waiting for them (Codex 4594286722):
# a settings save that moves RomM's instance or library root waits until no
# swap runs, and no swap starts while one is waiting. Touched on the event
# loop only.
_active_swaps = 0
_identity_changes = 0


# How long a settings save that moves RomM's identity waits for running swaps
# before it is refused (CodeRabbit 4594841850): a swap stuck on a mount that
# stopped answering must not hang the request, nor the locks it holds.
IDENTITY_CHANGE_WAIT_SECONDS = 30.0


class SwapsStillRunning(RuntimeError):
    """A RomM swap was still running when an identity change stopped waiting
    for it: nothing was changed, try again."""


@contextlib.asynccontextmanager
async def identity_change():
    """Hold off RomM swaps while RomM's identity changes: no new one starts,
    and this waits for those running (an undo outliving its hook included)
    to end. The settings route takes it around the save that moves the
    instance. It waits at most ``IDENTITY_CHANGE_WAIT_SECONDS``, then raises
    :class:`SwapsStillRunning` before the body runs, so nothing is saved
    while a swap still runs, and lets new swaps start again."""
    global _identity_changes
    _identity_changes += 1
    try:
        deadline = time.monotonic() + IDENTITY_CHANGE_WAIT_SECONDS
        while _active_swaps:
            if time.monotonic() >= deadline:
                raise SwapsStillRunning(
                    "a RomM swap is still running; try again once it has finished",
                )
            await asyncio.sleep(0.05)
        yield
    finally:
        _identity_changes -= 1


@dataclass(frozen=True)
class _Plan:
    rom_id: int
    source: str
    output: str
    old_name: str
    new_name: str
    platform_slug: str | None
    # The output was placed before the hook (a retried delete after a reader
    # held the Source): RomM can't rename onto it, so the delete takes
    # today's path and the re-pin carries the metadata (``_fall_back``).
    fallback: bool = False
    rom: dict | None = field(default=None, compare=False, repr=False)
    binding: _Binding | None = field(default=None, compare=False, repr=False)


# What the job and the Ledger say when the swap couldn't run (CodeRabbit
# 4593839018).
FALLBACK = "the ROM did not keep its id: the swap can't run after the output was placed"


def _single(renames: Mapping[str, str], *, resolve: bool = False) -> tuple[str, str] | None:
    """``(source, output)`` when *renames* is one file replaced by one output
    file beside it, under another name, that is not an archive. Compares the
    folders as written, or with *resolve* along their real paths (I/O)."""
    if len(renames) != 1:
        return None
    ((source, output),) = renames.items()
    if os.path.splitext(output)[1].lower() in _NOT_SWAPPED:
        return None
    where = os.path.realpath if resolve else os.path.abspath
    if where(os.path.dirname(source)) != where(os.path.dirname(output)):
        return None  # RomM renames a file within its own folder
    if os.path.basename(source) == os.path.basename(output):
        return None
    return source, output


def _converted_from(source: str, output: str) -> bool:
    """Whether *output*'s check record says it was converted from *source*:
    what a delete with the output already in place must show before the
    swap's fallback applies (a retried delete, or a job that placed its
    output first). Blocking (a database read, two path resolutions). False
    when it can't be told."""
    # Imported here: the store imports the job pipeline's helpers.
    from services.verification_store import verification_store

    try:
        record = verification_store.get_record_blocking(output)
    except Exception:
        logger.warning("romm_swap: could not read the check record of %s", output, exc_info=True)
        return False
    recorded = (record or {}).get("source_path")
    return bool(recorded) and os.path.realpath(recorded) == os.path.realpath(source)


def _rom_for(source: str) -> dict | None:
    """RomM's single-file ROM whose file is *source*, or None when the file is
    not a ROM RomM knows on its own. Blocking.

    The Platform is the RomM Library's Platform folder holding the file
    (``RommLibrary.platform_folder``: ``<root>/roms/<platform>/...`` or
    ``<root>/<platform>/...``, ``<root>/<platform>/roms/...`` and subfolders
    included), never the file's parent folder. Only that Platform's ROMs are
    listed, and the record must be the file itself.

    None (the swap doesn't apply, today's behaviour) when the file is in no
    Platform folder, RomM has no Platform for that folder, no record covers
    the file, or the record covering it is its folder (a multi-file ROM: the
    folder, and so the record, stays). Raises ReferrerError (fail closed: the
    Source is kept, so the record is never orphaned) when it can't be told:
    the folder or RomM can't be read, more than one Platform folder or more
    than one record claims the file, or the record that is the file says it
    has several."""
    # Imported here: services.romm.library imports this module.
    from services.romm.library import romm_library
    from services.rules.library import AmbiguousFolder

    try:
        folder = romm_library.platform_folder(source)
    except (AmbiguousFolder, OSError) as exc:
        raise ReferrerError("RomM", f"can't tell the ROM's Platform ({exc})") from exc
    if folder is None:
        return None
    slug = os.path.basename(folder)
    real = os.path.realpath(source)
    found: list[dict] = []
    for platform in romm_client.platforms():
        pid = platform.get("id")
        if pid is None or slug not in (platform.get("fs_slug"), platform.get("slug")):
            continue
        roms = romm_client.roms(int(pid), with_files=False)
        found.extend(
            rom for rom in roms
            if rom.get("id")
            and rom.get("fs_name") == os.path.basename(source)
            and romm_client.local_path(rom) == real
        )
    if len(found) > 1:
        ids = ", ".join(str(rom["id"]) for rom in found)
        raise ReferrerError("RomM", f"more than one ROM is {os.path.basename(source)} ({ids})")
    if not found:
        return None
    if found[0].get("missing_from_fs"):
        # RomM says the file is gone while it is here: its scan state can't
        # be trusted, so nothing is renamed and the Source is kept (fail
        # closed, CodeRabbit 4594841868).
        raise ReferrerError(
            "RomM", f"RomM marks ROM {found[0]['id']} ({os.path.basename(source)}) missing",
        )
    if found[0].get("has_multiple_files"):
        raise ReferrerError(
            "RomM", f"ROM {found[0]['id']} is {os.path.basename(source)} but has several files",
        )
    return found[0]


class RommSwapReferrer(Referrer):
    """RomM's ROM, as a Referrer: rename its file instead of letting RomM add
    a new ROM (see the module docstring)."""

    id = SWAP_REFERRER
    label = "RomM"
    opt_in = True
    phase = Phase.NETWORK

    def detect(self, image_path: str) -> bool:
        # Cheap and always yes: whether RomM knows the file is a network
        # question, answered by `plan`.
        return True

    def places_output(self, image_path: str, renames: Mapping[str, str]) -> bool:
        return _single(renames) is not None

    def plan(self, renames: Mapping[str, str]) -> list[ReferrerChange]:
        pair = _single(renames, resolve=True)
        if pair is None:
            return []
        source, output = pair
        # In the hook with the output already in place (the job placed it
        # before the hook: a retried delete after a reader held the Source),
        # RomM can't rename onto a name that is taken.
        placement = staged_placement()
        fallback = hook_running() and (placement is None or output not in placement.staged)
        if fallback and not _converted_from(source, output):
            # The "output" was not made from this Source (#389's duplicate
            # delete keeps another copy): no swap was owed, so none failed.
            return []
        # The instance the ROM is looked up on, which the swap then keeps to.
        binding = _Binding.now()
        try:
            rom = _rom_for(source)
        except (RommError, OSError, ValueError) as exc:
            raise ReferrerError("RomM", f"couldn't look up the ROM ({exc})") from exc
        if not binding.still_live():
            raise ReferrerError("RomM", "the RomM instance changed while the ROM was looked up")
        if rom is None:
            return []
        rom_id = int(rom["id"])
        old_name, new_name = os.path.basename(source), os.path.basename(output)
        plan = _Plan(
            rom_id=rom_id, source=source, output=output, old_name=old_name,
            new_name=new_name, platform_slug=rom.get("platform_slug"),
            fallback=fallback, rom=rom, binding=binding,
        )
        if fallback:
            return [ReferrerChange(
                referrer=self.id, target=f"RomM ROM {rom_id}", edits=((old_name, new_name),),
                payload=plan, summary=f"RomM: ROM {rom_id} can't keep its id ({FALLBACK})",
                record={"rom_id": rom_id, "kept_rom_id": False},
            )]
        return [ReferrerChange(
            referrer=self.id,
            target=f"RomM ROM {rom_id}",
            edits=((old_name, new_name),),
            payload=plan,
            summary=f"RomM: rename ROM {rom_id} to {new_name}",
            record={"rom_id": rom_id, "kept_rom_id": True},
        )]

    def apply(self, change: ReferrerChange) -> Applied:
        """The swap, for a caller off the event loop: it runs
        :meth:`apply_async` on a loop of its own. The hook never calls it:
        it awaits :meth:`apply_async` on its own loop, bounded."""
        return asyncio.run(self.apply_async(change))


    async def apply_async(self, change: ReferrerChange) -> Applied:
        plan: _Plan = change.payload
        if plan.fallback:
            return await _fall_back(plan, change.target)
        placement = staged_placement()
        staged = placement.staged.get(plan.output) if placement is not None else None
        if placement is None or staged is None:
            raise ReferrerError(
                change.target, "the output is already in place, so RomM can't rename onto it",
            )
        return await _Swap(plan, change.target, staged, placement).run()


# How long one short read may take: a stat of a file on a Volume (the job's
# delete-step probes take the same), and one RomM call (its own HTTP timeout,
# with a margin).
PROBE_SECONDS = 20.0
ROMM_SECONDS = 90.0


async def _probe(func: Callable[..., Any], *args: Any, seconds: float = PROBE_SECONDS) -> Any:
    """A short blocking read on a detached thread, bounded (``run_detached_bounded``).

    Never the event loop's shared executor: a mount or a RomM that stops
    answering strands this throwaway thread, not a worker every other
    offload needs. Runs in the caller's context, so ``check_write`` sees the
    hook's guard. Raises ``DeadlineExpired`` (a ``TimeoutError``) past the
    bound."""
    return await run_detached_bounded(
        contextvars.copy_context().run, func, *args, timeout=seconds,
    )


async def _read_whole(func: Callable[..., Any], *args: Any, what: str) -> Any:
    """A whole-file read on a detached thread, given up on after the
    conversion's no-progress bound (``run_detached_within_stall_bound``)."""
    return await run_detached_within_stall_bound(
        contextvars.copy_context().run, func, *args, what=what,
    )


async def _mutate(func: Callable[..., Any], *args: Any) -> Any:
    """A step that changes something, on a detached thread, or one of its own
    when none is free (``start_detached_or_own_thread``: the step, and an
    undo, always start), never abandoned. A cancel waits for it to finish
    before it goes on, and *func* records its own outcome on its thread (a
    flag, the journal), so what happened is known before anything acts on
    the cancel."""
    future = start_detached_or_own_thread(contextvars.copy_context().run, func, *args)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancel:
        # Every further cancel (a shutdown cancelling again during the
        # hook's grace) waits too: the step ends before anything released
        # after it can race it (CodeRabbit 4594841855).
        while not future.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({future})
        raise cancel


def _size_and_sha1(path: str) -> tuple[int, str]:
    return os.stat(path).st_size, compute_file_sha1_sync(path).lower()


def _is_snapshot(now: FileIdentity | None, snapshot: Mapping[str, int] | None) -> bool:
    """Whether *now* is the file the delete snapshot *snapshot* fingerprinted
    before the conversion (Codex 4594537162), by the job's own rule
    (``JobManager._delete_path_problem``): the same size and ``mtime_ns``,
    and the same inode and device where both sides know them. A rename on
    one filesystem keeps all four, so the Source RomM moved, and the one
    parked, still match. False when either is missing."""
    if now is None or not snapshot:
        return False
    if (int(snapshot.get("size", -1)), int(snapshot.get("mtime_ns", -1))) != (
        now.size, now.mtime_ns,
    ):
        return False
    inode, device = int(snapshot.get("inode", 0) or 0), int(snapshot.get("device", 0) or 0)
    if inode and now.ino and inode != now.ino:
        return False
    return not (device and now.dev and device != now.dev)


def _read_back_problem(rom: dict, plan: _Plan, size: int, sha1: str) -> str | None:
    """Why RomM's record after the rehash is not the output's, or None."""
    rom_id = plan.rom_id
    if rom.get("id") != rom_id:
        return f"RomM read back ROM {rom.get('id')}, not ROM {rom_id}"
    if rom.get("fs_name") != plan.new_name:
        return f"ROM {rom_id} names {rom.get('fs_name')}, not {plan.new_name}"
    if rom.get("fs_size_bytes") != size:
        return f"ROM {rom_id} reports {rom.get('fs_size_bytes')} bytes, the output has {size}"
    reported = str(rom.get("sha1_hash") or "").lower()
    slug = rom.get("platform_slug") or plan.platform_slug
    if not reported and slug in UNHASHED_PLATFORM_SLUGS:
        # RomM never hashes this Platform (its NON_HASHABLE_PLATFORMS: Switch,
        # Wii U, PS3, ...): with no SHA-1 to compare, the size is all it has.
        # A SHA-1 it does report is always compared.
        return None
    if reported != sha1:
        return f"ROM {rom_id}'s SHA-1 is not the output's"
    return None


def _settle_repin(output: str, rom_id: int) -> None:
    """Close the output's pending re-pin: the ids never left the ROM."""
    # Imported here: repin imports the job manager, which imports the hook.
    from services.romm import repin

    try:
        repin.settle_swapped(output, rom_id)
    except Exception:
        # Bookkeeping only: a row left pending re-applies the ids the ROM
        # already has, which changes nothing.
        logger.warning("romm_swap: could not settle the re-pin of %s", output, exc_info=True)


def _queue_repin(plan: _Plan) -> bool:
    """Queue the re-pin a conversion gets with the swap off: RomM's provider
    ids, re-applied to the ROM RomM adds for the output. False when the ROM
    has none to carry. Blocking."""
    # Imported here: repin imports the job manager, which imports the hook.
    from services.romm import repin

    # With the Hasheous switch on (#427), the ids the app's match found and
    # the ROM lacks ride along.
    ids = repin.ids_for(plan.rom or {}, plan.source)
    if not ids:
        return False
    # The output is in place already; "" says the path held nothing before
    # this conversion, so the re-pin waits on nothing but RomM's next scan.
    if not repin.record(
        plan.rom, plan.output, ids, None, pre_fingerprint="", source_path=plan.source,
    ):
        # Refused (RomM's identity changing, or no row written): the Source
        # stays rather than lose the metadata (CodeRabbit 4594841873).
        raise RuntimeError("the re-pin was refused")
    return True


async def _fall_back(plan: _Plan, target: str) -> Applied:
    """The swap can't run (the output is in place): take today's path. The
    Source is deleted by the job as with the switch off, the re-pin carries
    the metadata, and the job and the Ledger say the ROM did not keep its id
    (CodeRabbit 4593839018). A re-pin that can't be queued keeps the Source.

    Refused, keeping the Source, while RomM's identity is being changed or
    once the settings no longer name the instance planned against; counted
    as a running swap while the re-pin is written, so an identity change
    waits for it (CodeRabbit 4594841887)."""
    global _active_swaps
    binding = plan.binding
    if _identity_changes or (binding is not None and not binding.still_live()):
        raise ReferrerError(
            target, f"{FALLBACK}, and RomM's settings changed, so no re-pin was queued",
        )
    _active_swaps += 1
    try:
        queued = await _mutate(_queue_repin, plan)
    except Exception as exc:
        raise ReferrerError(
            target, f"{FALLBACK}, and the re-pin couldn't be queued ({exc})",
        ) from exc
    finally:
        _active_swaps -= 1
    what = "re-pin queued" if queued else "RomM had no metadata ids to re-pin"
    note = f"{FALLBACK}; {what} ({target})"
    logger.warning("romm_swap: %s", note)
    return Applied(note=note)


LEFT = "left for the next run"

# Bookkeeping tasks of finished swaps still running after a cancel, kept
# referenced until they end (the loop holds only weak references to tasks).
_after_swaps: set[asyncio.Task] = set()


class _Stopped(ReferrerError):
    """What the undo is given when a Cancel, a shutdown or the hook's bound
    stopped the swap: its reason is what the row records."""


class _Unbound(ReferrerError):
    """RomM's settings no longer name the instance the swap planned against
    (Codex 4594286722). Nothing more is changed on either instance: the row
    is left for the next run."""


class _Swap:
    """One swap's steps and their undo (see the module docstring).

    The flags (``romm_name``, ``parked``, ``placed``) say what has happened.
    Each step that moves something sets its flag, and writes its outcome to
    the journal, on the step's own thread, so a cancel can't come between the
    move and the record of it. The parking slot is never removed on a flag's
    word: :meth:`_release_park` lists it first."""

    def __init__(
        self, plan: _Plan, target: str, staged: str, placement: StagedPlacement,
    ) -> None:
        self.plan = plan
        self.target = target
        self.staged = staged
        self.placement = placement
        self.folder = os.path.dirname(plan.output)
        self.row: int | None = None
        self.ended = False
        # The name RomM's record has, as far as this knows: None when a
        # rename got no answer (it may or may not have happened).
        self.romm_name: str | None = plan.old_name
        self.park: staging.StagedOutput | None = None
        self.parked = False
        self.placed = False
        # The parked Source was removed: set on the removal's own thread,
        # so a cancel landing meanwhile still knows (CodeRabbit 4594326517).
        self.removed = False
        # The row was left for the next run (``_leave``): the output stays
        # held when the swap ends.
        self.left = False
        # The Source as the delete snapshot fingerprinted it before the
        # conversion: the only file this swap renames, parks or deletes.
        fingerprints = placement.source_fingerprints
        self.snapshot = fingerprints.get(plan.source) or fingerprints.get(
            os.path.realpath(plan.source),
        )
        self.binding = plan.binding or _Binding.now()
        self.client = self.binding.client()

    def _require_same_instance(self, before: str) -> None:
        """Raise :class:`_Unbound` unless RomM's settings still name the
        instance this swap planned against. Asked before every RomM write."""
        if not self.binding.still_live():
            raise _Unbound(
                self.target,
                f"the RomM instance changed before {before}; nothing more was "
                f"changed on either instance, and it is {LEFT}",
            )

    @property
    def parked_path(self) -> str:
        """Where the Source is parked: under its original name (#415), so a
        parked file never carries the output's name. The journal records it;
        recovery reads the name from there (a row written before parked it
        under the output's name)."""
        assert self.park is not None
        return os.path.join(self.park.path, self.plan.old_name)

    @property
    def changed(self) -> bool:
        """Whether anything was moved or renamed (or may have been)."""
        return self.parked or self.placed or self.romm_name != self.plan.old_name

    async def run(self) -> Applied:
        """The swap, counted as running (``identity_change`` waits for it)
        until it, and any undo, has ended. Refused while RomM's identity is
        being changed."""
        global _active_swaps
        if _identity_changes:
            raise ReferrerError(self.target, "RomM's settings are being changed")
        _active_swaps += 1
        try:
            return await self._run()
        finally:
            _active_swaps -= 1
            # The job may place the output again only once the swap has ended
            # somewhere that allows it, never while it may still be running
            # (a lingering undo) nor when its row is left for the next run.
            if not self.left:
                self.placement.held.clear()

    async def _run(self) -> Applied:
        plan = self.plan
        if not self.snapshot:
            # Fail closed: nothing to tell the converted Source by.
            raise ReferrerError(
                self.target, "the Source has no fingerprint from before it was converted",
            )
        try:
            size, sha1 = await _read_whole(
                _size_and_sha1, self.staged, what=f"Hashing {plan.new_name}",
            )
            await _probe(check_write, self.target)
        except (OSError, RuntimeError) as exc:
            raise ReferrerError(self.target, f"couldn't read the output ({exc})") from exc

        def begin() -> None:
            # With what startup recovery needs (#392): the instance, and the
            # fingerprints that tell the Source and the output apart.
            self.row = swap_journal.begin(
                rom_id=plan.rom_id, source_path=plan.source, output_path=plan.output,
                staged_path=self.staged, old_name=plan.old_name, new_name=plan.new_name,
                romm_url=self.binding.base_url,
                library_root=str(romm_settings.effective().get("library_root") or ""),
                source_size=int(self.snapshot["size"]),
                source_mtime_ns=int(self.snapshot["mtime_ns"]),
                output_size=size, output_sha1=sha1,
            )

        try:
            await _mutate(begin)
        except Exception as exc:
            raise ReferrerError(self.target, f"couldn't write the swap journal ({exc})") from exc
        # From the first move on, the job never places the output on its own
        # (Codex 4594772381): the swap places it, or releases it when it ends
        # (``run``), so a lingering swap is never overtaken.
        self.placement.held.set()
        try:
            await self._rename()
            await self._park_source()
            await self._place_output()
            await self._rehash(size, sha1)
            await self._delete_parked()
        except Exception as exc:
            if not self.removed:
                try:
                    left = await self._undo(exc)
                except asyncio.CancelledError:
                    # Cancelled during the undo: the step it was on is recorded.
                    await self._leave(f"{exc}; stopped during the undo; {LEFT}")
                    raise
                if left and isinstance(exc, ReferrerError):
                    raise ReferrerError(
                        exc.target, f"{exc.reason}; {left}", kept=exc.kept,
                    ) from exc
                raise
            # The parked Source was already removed (CodeRabbit 4594326517):
            # the swap is done, whatever came after.
            logger.warning("romm_swap: swap %s finished; then %s", self.row, exc)
        except BaseException as exc:
            if not self.removed:
                # A Cancel, a shutdown or the hook's bound, after the step it
                # landed on recorded its outcome (``_mutate``): undo within
                # the hook's grace.
                await self._undo_within_grace(exc)
                raise
            # It landed while the parked Source was removed, which finished
            # first: the swap is done, and is reported so.
        finally:
            await self._release_park()
        # The Source is gone: from here on the swap's result is never lost
        # (Codex 4594537178). The bookkeeping runs as a task of its own; a
        # cancel landing on it leaves it running and still returns the result,
        # which the hook takes (``_await_bounded``).
        bookkeeping = asyncio.ensure_future(self._finish())
        _after_swaps.add(bookkeeping)
        bookkeeping.add_done_callback(_after_swaps.discard)
        try:
            await asyncio.shield(bookkeeping)
        except asyncio.CancelledError:
            logger.warning(
                "romm_swap: swap %s finished; its bookkeeping goes on after a cancel", self.row,
            )
        return Applied(removed=(plan.source,))

    async def _finish(self) -> None:
        """Record the finished swap and settle the output's re-pin. Never
        raises: bookkeeping after the Source is gone never makes the applied
        swap look failed (CodeRabbit 4594326492)."""
        plan = self.plan
        try:
            await self._end("done")
        except Exception:
            # The swap is finished and the Source is gone: only the record lags.
            logger.warning("romm_swap: could not record swap %s as done", self.row, exc_info=True)
        try:
            await _probe(_settle_repin, plan.output, plan.rom_id)
        except Exception:
            logger.warning(
                "romm_swap: could not settle the re-pin of %s", plan.output, exc_info=True,
            )
        logger.info(
            "romm_swap: ROM %s kept its id: %s -> %s", plan.rom_id, plan.old_name, plan.new_name,
        )

    # ---- the journal -----------------------------------------------------

    async def _step(self, state: str, *, parked_path: str | None = None) -> None:
        await _mutate(lambda: swap_journal.step(self.row, state, parked_path=parked_path))

    async def _end(self, state: str, detail: str | None = None) -> None:
        await _mutate(lambda: swap_journal.step(self.row, state, detail=detail))
        self.ended = True

    async def _leave(self, detail: str) -> None:
        """Leave the row for the next run: on the state the flags say, with
        *detail*. The output is held back from the job: the name it would
        take may hold, or be owed to, the Source. Never raises."""
        self.left = True
        if not self.placed:
            self.placement.held.set()

        def note() -> None:
            # An undo step keeps its name (the step it stopped at); a forward
            # one is corrected to what the flags say was done.
            current = swap_journal.current(self.row)
            state = current
            if current not in ("reverting", "restoring"):
                state = (
                    "placed" if self.placed and self.parked
                    else "parked" if self.parked else current
                )
            swap_journal.step(self.row, state, detail=detail)

        try:
            await _mutate(note)
        except BaseException:
            logger.warning("romm_swap: could not note swap %s", self.row, exc_info=True)

    # ---- the steps -------------------------------------------------------

    def _fail(self, reason: str, kept: str | None = None) -> ReferrerError:
        return ReferrerError(self.target, reason, kept=kept)

    async def _read_name(self) -> str | None:
        rom = await _probe(self.client.rom, self.plan.rom_id, seconds=ROMM_SECONDS)
        return rom.get("fs_name")

    async def _rename(self) -> None:
        """Step 1 (the journal row was begun in ``renaming``)."""
        plan = self.plan
        self._require_same_instance("the rename")
        # Under the Source's hold (the job holds it through the hook): still
        # the file that was converted? (Codex 4594537162)
        try:
            source = await _probe(file_identity, plan.source)
        except OSError as exc:
            source, reason = None, f"the Source couldn't be read ({exc})"
        else:
            reason = "the Source changed since it was converted"
        if not _is_snapshot(source, self.snapshot):
            await self._end("failed", reason)
            raise self._fail(reason)
        self.romm_name = None
        try:
            await _mutate(self.client.rename_rom, plan.rom_id, plan.new_name)
        except RommError as exc:
            if exc.status is None:
                # No answer: RomM may have renamed. The undo puts the old name
                # back, a no-op if it didn't.
                raise self._fail(f"RomM didn't answer the rename ({exc})") from exc
            # RomM answered: nothing changed (the spike's 409 and 500 leave
            # the record and the file as they were).
            self.romm_name = plan.old_name
            reason = exc.detail or str(exc)
            if exc.status == 409:
                await self._end("refused", reason)
                raise self._fail(reason, kept=REFUSED) from exc
            await self._end("failed", reason)
            raise self._fail(f"RomM couldn't rename the file: {reason}") from exc
        try:
            name = await self._read_name()
        except (RommError, OSError) as exc:
            raise self._fail(f"couldn't read the ROM back after the rename ({exc})") from exc
        self.romm_name = name
        if name == plan.new_name:
            return
        if name == plan.old_name:
            await self._end("refused", f"RomM kept the name {name}")
            raise self._fail(f"RomM kept the name {name}", kept=REFUSED)
        raise self._fail(f"RomM named the file {name}, not {plan.new_name}", kept=REFUSED)

    async def _park_source(self) -> None:
        """Step 2: the Source RomM moved to the output's name goes into a
        parking slot."""
        name = self.plan.new_name
        moved = await _probe(file_identity, os.path.join(self.folder, name))
        if not _is_snapshot(moved, self.snapshot):
            raise self._fail(f"{name} is not the Source RomM moved there")

        def open_park() -> None:
            self.park = staging.open_slot(self.folder, parking=True)

        try:
            await _mutate(open_park)
        except OSError as exc:
            raise self._fail(f"couldn't make a place to park the Source ({exc})") from exc
        await self._step("parking", parked_path=self.parked_path)

        def take() -> None:
            self.park.take(name, self.plan.old_name)
            self.parked = True
            swap_journal.step(self.row, "parked", parked_path=self.parked_path)

        try:
            await _mutate(take)
        except OSError as exc:
            raise self._fail(f"couldn't park the Source ({exc})") from exc

    async def _place_output(self) -> None:
        """Step 3: the job's staged output takes the name.

        A step like every other move (``_mutate``, Codex 4594650576): run on a
        thread of its own and never abandoned, so a cancel waits for its real
        outcome before the undo picks its branch, and with no hand-off of the
        job's holds to a worker. The swap, and its undo, keep them until it
        ends; past the hook's grace ``outlive`` hands them to this task."""
        await self._step("placing")
        place_blocking = self.placement.place_blocking
        if place_blocking is None:
            raise self._fail("the job offers no placing the swap can wait on")

        def place() -> None:
            place_blocking()
            self.placed = True  # on the step's own thread, with the move

        try:
            await _mutate(place)
        except Exception as exc:
            # Whether it moved, from the slot itself, not from how this ended.
            self.placed = self.placed or self.placement.is_placed()
            raise self._fail(f"couldn't place the output ({exc})") from exc
        await self._step("placed")

    async def _rehash(self, size: int, sha1: str) -> None:
        """Step 4: RomM rehashes the ROM; the read-back must match the output."""
        await self._step("rehashing")
        self._require_same_instance("the rehash")
        try:
            task_id = await _mutate(self.client.scan_rom_hashes, [self.plan.rom_id])
            status = await self._wait(task_id)
            if status != "finished":
                raise self._fail(f"RomM's rehash ended {status}", kept=UNCONFIRMED)
            rom = await _probe(self.client.rom, self.plan.rom_id, seconds=ROMM_SECONDS)
        except (RommError, OSError) as exc:
            raise self._fail(f"couldn't rehash the ROM ({exc})", kept=UNCONFIRMED) from exc
        problem = _read_back_problem(rom, self.plan, size, sha1)
        if problem:
            raise self._fail(f"after its rehash, {problem}", kept=UNCONFIRMED)

    async def _wait(self, task_id: str) -> str:
        deadline = time.monotonic() + REHASH_SECONDS
        while True:
            task = await _probe(self.client.task, task_id, seconds=ROMM_SECONDS)
            status = str(task.get("status") or "")
            if status in _TASK_ENDED:
                return status
            if time.monotonic() >= deadline:
                raise self._fail(
                    f"RomM's rehash didn't finish within {REHASH_SECONDS:g}s", kept=UNCONFIRMED,
                )
            await asyncio.sleep(POLL_SECONDS)

    async def _delete_parked(self) -> None:
        """Step 5: the read-back passed; the parked Source goes."""
        await self._step("deleting")
        if not _is_snapshot(await _probe(file_identity, self.parked_path), self.snapshot):
            raise self._fail("the parked Source changed, so it was not deleted")

        def remove() -> None:
            self.park.remove(self.plan.old_name)
            self.parked = False
            self.removed = True

        try:
            await _mutate(remove)
        except OSError as exc:
            raise self._fail(f"couldn't delete the parked Source ({exc})") from exc

    # ---- stopping, undoing, letting go -----------------------------------

    async def _undo_within_grace(self, exc: BaseException) -> None:
        """A Cancel, a shutdown or the hook's bound stopped a forward step,
        after it recorded its outcome. Run the normal undo, each step
        journaled, within the hook's ``CANCEL_GRACE_SECONDS`` (#383): past
        it, the hook stops waiting and hands this task the job's holds
        (``ReferrerOutcome.outlive``), so it never undoes with them gone. The
        output is held back from the job until the undo has finished, so the
        job can't place it over a name that may hold, or be owed to, the
        Source. Only an undo that runs out of grace, or fails, leaves the row
        for the next run, with a parked Source kept in its slot and the output
        still held. Never raises."""
        why = f"stopped ({'cancelled' if isinstance(exc, asyncio.CancelledError) else exc})"
        if not self.changed:
            try:
                await self._end("failed", f"{why} before RomM renamed")
            except BaseException:
                logger.warning("romm_swap: could not end swap %s", self.row, exc_info=True)
            return
        # Read here, not imported: the hook's value at the time (tests set it).
        # The output stays held all along (set at the first move; ``run``
        # releases it once the swap ends where that is allowed).
        grace = referrers_module.CANCEL_GRACE_SECONDS
        try:
            await asyncio.wait_for(self._undo(_Stopped(self.target, why)), grace)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Each step the undo finished is journaled; the one it was on
            # finished too (``_mutate``) before this.
            await self._leave(f"{why}; the undo didn't finish within {grace:g}s; {LEFT}")

    async def _undo(self, exc: BaseException) -> str | None:
        """Leave the files and RomM as a kept Source would (see the module
        docstring). Returns what was left for the next run, or None. A
        cancel during it propagates (the caller leaves the row); nothing else
        does."""
        reason = exc.reason if isinstance(exc, ReferrerError) else (str(exc) or type(exc).__name__)
        refusal = isinstance(exc, ReferrerError) and exc.kept == REFUSED
        if isinstance(exc, _Unbound) or not self.binding.still_live():
            # The instance moved: no undo, which would change one or the
            # other. Everything stays as it is, for the next run.
            await self._leave(reason if isinstance(exc, _Unbound) else (
                f"{reason}; the RomM instance changed, so nothing was undone; {LEFT}"
            ))
            return LEFT
        try:
            if self.placed:
                await self._restore_beside_output()
                await self._end("kept", reason)
            else:
                await self._put_old_name_back()
                if not self.ended:
                    await self._end("refused" if refusal else "rolled_back", reason)
            return None
        except Exception as undo_exc:
            # Fail closed: the row stays on the step reached, a parked Source
            # stays parked, and the output is held back, for the next run. A
            # cancel (not an Exception) propagates to the caller, which
            # leaves the row itself.
            left = f"undoing it failed ({undo_exc}), so it is {LEFT}"
            logger.exception(
                "romm_swap: swap %s of ROM %s: %s; %s", self.row, self.plan.rom_id, reason, left,
            )
            await self._leave(f"{reason}; {left}")
            return left

    def _restore(self, as_name: str) -> None:
        """Move the parked Source out of the slot as *as_name* (on the step's
        thread)."""
        self.park.restore(self.plan.old_name, as_name)
        self.parked = False

    async def _restore_beside_output(self) -> None:
        """The output is in place and keeps the ROM's id: the parked Source
        goes back under its own name, beside it."""
        if not self.parked:
            return
        await self._step("restoring")
        await _mutate(self._restore, self.plan.old_name)

    async def _put_old_name_back(self) -> None:
        """The output is not in place: the Source goes back under the name
        RomM gave it, then RomM puts the old name back (and moves it)."""
        plan = self.plan
        if self.parked:
            await self._step("restoring")
            # Back under the name RomM gave it (it was parked only once RomM
            # had renamed): RomM's rename back moves it from there.
            await _mutate(self._restore, plan.new_name)
        if self.romm_name == plan.old_name:
            return
        self._require_same_instance("putting the old name back")
        await self._step("reverting")
        await _mutate(self.client.rename_rom, plan.rom_id, plan.old_name)
        name = await self._read_name()
        if name != plan.old_name:
            raise RommError(f"RomM still names the file {name}")
        self.romm_name = name

    async def _release_park(self) -> None:
        """Let go of the parking slot. It is removed only when its own
        listing shows it empty; one that holds anything (a parked Source),
        or can't be listed, is left in place, never swept, and the row says
        so. Never raises."""
        if self.park is None:
            return
        park, self.park = self.park, None
        try:
            held = await _mutate(park.contents)
        except BaseException:
            logger.warning("romm_swap: could not list %s", park.path, exc_info=True)
            held = None
        try:
            if held == []:
                await _mutate(park.close)
                return
            await _mutate(park.keep)
        except BaseException:
            logger.warning("romm_swap: could not let go of %s", park.path, exc_info=True)
            return
        if not self.parked:
            # The flags missed it: the listing has the last word.
            self.parked = True
            await self._leave(f"{park.path} still holds {held or 'something'}; {LEFT}")


registry.register(RommSwapReferrer())
