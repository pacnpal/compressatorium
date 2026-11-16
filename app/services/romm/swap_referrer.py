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
   parking=True)``), which no sweep removes.
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

If the undo itself fails, or the process dies, the row stays on the step it
reached, with the reason, and a parked Source stays parked: fail closed, for
the next run to finish or show as a Finding (#392).

A successful swap closes the output's pending re-pin row as done
(``repin.settle_swapped``): the provider ids never left the ROM.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from logging_setup import get_logger
from services import staging
from services.file_hasher import compute_file_sha1_sync
from services.referrers import (
    Applied,
    Phase,
    Referrer,
    ReferrerChange,
    ReferrerError,
    registry,
)
from services.referrers.base import check_write, hook_running, staged_placement
from services.romm import swap_journal
from services.romm.client import UNHASHED_PLATFORM_SLUGS, RommError, romm_client
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
class _Plan:
    rom_id: int
    source: str
    output: str
    old_name: str
    new_name: str
    platform_slug: str | None


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


def _rom_for(source: str) -> dict | None:
    """RomM's single-file ROM whose file is *source*, or None. Blocking.

    Asks only the Platform whose folder holds the file (its ``fs_slug``), not
    the whole catalog."""
    folder = os.path.basename(os.path.dirname(source))
    name = os.path.basename(source)
    real = os.path.realpath(source)
    for platform in romm_client.platforms():
        pid = platform.get("id")
        if pid is None or folder not in (platform.get("fs_slug"), platform.get("slug")):
            continue
        for rom in romm_client.roms(int(pid), with_files=False):
            if (
                rom.get("id")
                and not rom.get("missing_from_fs")
                and not rom.get("has_multiple_files")
                and rom.get("fs_name") == name
                and romm_client.local_path(rom) == real
            ):
                return rom
    return None


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
        if hook_running():
            placement = staged_placement()
            if placement is None or output not in placement.staged:
                # The job placed its output before the hook: RomM can't rename
                # onto a name that is taken, so today's behaviour applies.
                return []
        try:
            rom = _rom_for(source)
        except (RommError, OSError, ValueError) as exc:
            raise ReferrerError("RomM", f"couldn't look up the ROM ({exc})") from exc
        if rom is None:
            return []
        rom_id = int(rom["id"])
        old_name, new_name = os.path.basename(source), os.path.basename(output)
        return [ReferrerChange(
            referrer=self.id,
            target=f"RomM ROM {rom_id}",
            edits=((old_name, new_name),),
            payload=_Plan(
                rom_id=rom_id, source=source, output=output, old_name=old_name,
                new_name=new_name, platform_slug=rom.get("platform_slug"),
            ),
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
        placement = staged_placement()
        staged = placement.staged.get(plan.output) if placement is not None else None
        if placement is None or staged is None:
            raise ReferrerError(
                change.target, "the output is already in place, so RomM can't rename onto it",
            )
        return await _Swap(plan, change.target, staged, placement.place).run()


async def _read(func: Callable[..., Any], *args: Any) -> Any:
    """A blocking read off the loop. A cancel abandons it: nothing to undo."""
    return await asyncio.to_thread(func, *args)


async def _mutate(func: Callable[..., Any], *args: Any) -> Any:
    """A blocking step that changes something, off the loop. A cancel waits
    for it to finish first, so an undo never races it."""
    future = asyncio.ensure_future(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await asyncio.wait({future})
        raise


def _size_and_sha1(path: str) -> tuple[int, str]:
    return os.stat(path).st_size, compute_file_sha1_sync(path).lower()


def _same_file(now: FileIdentity | None, then: FileIdentity | None) -> bool:
    """Whether *now* is the file *then* described, after renames: a rename on
    one filesystem keeps the size and mtime (the path changes, and a FUSE
    mount may not keep the inode)."""
    return (
        now is not None and then is not None
        and (now.size, now.mtime_ns) == (then.size, then.mtime_ns)
    )


def _read_back_problem(rom: dict, plan: _Plan, size: int, sha1: str) -> str | None:
    """Why RomM's record after the rehash is not the output's, or None."""
    rom_id = plan.rom_id
    if rom.get("id") != rom_id:
        return f"RomM read back ROM {rom.get('id')}, not ROM {rom_id}"
    if rom.get("fs_name") != plan.new_name:
        return f"ROM {rom_id} names {rom.get('fs_name')}, not {plan.new_name}"
    if rom.get("fs_size_bytes") != size:
        return f"ROM {rom_id} reports {rom.get('fs_size_bytes')} bytes, the output has {size}"
    slug = rom.get("platform_slug") or plan.platform_slug
    if slug in UNHASHED_PLATFORM_SLUGS:
        return None  # RomM never hashes this Platform: the size is all it has
    if str(rom.get("sha1_hash") or "").lower() != sha1:
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


class _Swap:
    """One swap's steps and their undo (see the module docstring)."""

    def __init__(
        self, plan: _Plan, target: str, staged: str, place: Callable[[], Awaitable[None]],
    ) -> None:
        self.plan = plan
        self.target = target
        self.staged = staged
        self.place = place
        self.folder = os.path.dirname(plan.output)
        self.row: int | None = None
        self.ended = False
        # The name RomM's record has, as far as this knows: None when a
        # rename got no answer (it may or may not have happened).
        self.romm_name: str | None = plan.old_name
        self.park: staging.StagedOutput | None = None
        self.parked = False
        self.placed = False
        self.source_identity: FileIdentity | None = None

    @property
    def parked_path(self) -> str:
        assert self.park is not None
        return os.path.join(self.park.path, self.plan.new_name)

    async def run(self) -> Applied:
        plan = self.plan
        self.source_identity = await _read(file_identity, plan.source)
        if self.source_identity is None:
            raise ReferrerError(self.target, "the Source is gone")
        try:
            size, sha1 = await _read(_size_and_sha1, self.staged)
        except OSError as exc:
            raise ReferrerError(self.target, f"couldn't read the output ({exc})") from exc
        await _read(check_write, self.target)
        try:
            self.row = await _mutate(
                lambda: swap_journal.begin(
                    rom_id=plan.rom_id, source_path=plan.source, output_path=plan.output,
                    staged_path=self.staged, old_name=plan.old_name, new_name=plan.new_name,
                ),
            )
        except Exception as exc:
            raise ReferrerError(
                self.target, f"couldn't write the swap journal ({exc})",
            ) from exc
        try:
            await self._rename()
            await self._park_source()
            await self._place_output()
            await self._rehash(size, sha1)
            await self._delete_parked()
        except BaseException as exc:
            left = await self._undo(exc)
            if left and isinstance(exc, ReferrerError):
                raise ReferrerError(exc.target, f"{exc.reason}; {left}", kept=exc.kept) from exc
            raise
        try:
            await self._end("done")
        except Exception:
            # The swap is finished and the Source is gone: only the record lags.
            logger.warning("romm_swap: could not record swap %s as done", self.row, exc_info=True)
        await _read(_settle_repin, plan.output, plan.rom_id)
        logger.info(
            "romm_swap: ROM %s kept its id: %s -> %s", plan.rom_id, plan.old_name, plan.new_name,
        )
        return Applied(removed=(plan.source,))

    # ---- the journal -----------------------------------------------------

    async def _step(self, state: str, *, parked_path: str | None = None) -> None:
        await _mutate(lambda: swap_journal.step(self.row, state, parked_path=parked_path))

    async def _end(self, state: str, detail: str | None = None) -> None:
        await _mutate(lambda: swap_journal.step(self.row, state, detail=detail))
        self.ended = True

    # ---- the steps -------------------------------------------------------

    def _fail(self, reason: str, kept: str | None = None) -> ReferrerError:
        return ReferrerError(self.target, reason, kept=kept)

    async def _read_name(self) -> str | None:
        rom = await _read(romm_client.rom, self.plan.rom_id)
        return rom.get("fs_name")

    async def _rename(self) -> None:
        """Step 1 (the journal row was begun in ``renaming``)."""
        plan = self.plan
        self.romm_name = None
        try:
            await _mutate(romm_client.rename_rom, plan.rom_id, plan.new_name)
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
        except RommError as exc:
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
        final = os.path.join(self.folder, self.plan.new_name)
        moved = await _read(file_identity, final)
        if not _same_file(moved, self.source_identity):
            raise self._fail(f"{self.plan.new_name} is not the Source RomM moved there")
        try:
            self.park = await _mutate(lambda: staging.open_slot(self.folder, parking=True))
        except OSError as exc:
            raise self._fail(f"couldn't make a place to park the Source ({exc})") from exc
        await self._step("parking", parked_path=self.parked_path)
        try:
            await _mutate(self.park.take, self.plan.new_name)
        except OSError as exc:
            raise self._fail(f"couldn't park the Source ({exc})") from exc
        self.parked = True

    async def _place_output(self) -> None:
        """Step 3: the job's staged output takes the name."""
        await self._step("placing")
        try:
            await self.place()
        except Exception as exc:
            raise self._fail(f"couldn't place the output ({exc})") from exc
        self.placed = True

    async def _rehash(self, size: int, sha1: str) -> None:
        """Step 4: RomM rehashes the ROM; the read-back must match the output."""
        await self._step("rehashing")
        try:
            task_id = await _mutate(romm_client.scan_rom_hashes, [self.plan.rom_id])
            status = await self._wait(task_id)
            if status != "finished":
                raise self._fail(f"RomM's rehash ended {status}", kept=UNCONFIRMED)
            rom = await _read(romm_client.rom, self.plan.rom_id)
        except RommError as exc:
            raise self._fail(f"couldn't rehash the ROM ({exc})", kept=UNCONFIRMED) from exc
        problem = _read_back_problem(rom, self.plan, size, sha1)
        if problem:
            raise self._fail(f"after its rehash, {problem}", kept=UNCONFIRMED)

    async def _wait(self, task_id: str) -> str:
        deadline = time.monotonic() + REHASH_SECONDS
        while True:
            task = await _read(romm_client.task, task_id)
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
        if not _same_file(await _read(file_identity, self.parked_path), self.source_identity):
            raise self._fail("the parked Source changed, so it was not deleted")
        try:
            await _mutate(self.park.remove, self.plan.new_name)
        except OSError as exc:
            raise self._fail(f"couldn't delete the parked Source ({exc})") from exc
        self.parked = False
        await _mutate(self.park.close)
        self.park = None

    # ---- the undo --------------------------------------------------------

    async def _undo(self, exc: BaseException) -> str | None:
        """Leave the files and RomM as a kept Source would (see the module
        docstring). Returns what was left for the next run, or None. Never
        raises."""
        reason = exc.reason if isinstance(exc, ReferrerError) else (str(exc) or type(exc).__name__)
        refusal = isinstance(exc, ReferrerError) and exc.kept == REFUSED
        try:
            if self.placed:
                await self._restore_beside_output()
                await self._end("kept", reason)
            else:
                await self._put_old_name_back()
                if not self.ended:
                    await self._end("refused" if refusal else "rolled_back", reason)
            return None
        except BaseException as undo_exc:
            # Fail closed: the row stays on the step that failed, and a
            # parked Source stays parked, for the next run.
            left = f"undoing it failed ({undo_exc}), so it is left for the next run"
            logger.exception(
                "romm_swap: swap %s of ROM %s: %s; %s",
                self.row, self.plan.rom_id, reason, left,
            )
            try:
                await _mutate(lambda: swap_journal.step(
                    self.row, swap_journal.current(self.row), detail=f"{reason}; {left}",
                ))
            except BaseException:
                logger.warning("romm_swap: could not note swap %s", self.row, exc_info=True)
            return left
        finally:
            if self.park is not None:
                park, self.park = self.park, None
                await _mutate(park.keep if self.parked else park.close)

    async def _restore_beside_output(self) -> None:
        """The output is in place and keeps the ROM's id: the parked Source
        goes back under its own name, beside it."""
        if not self.parked:
            return
        await self._step("restoring")
        await _mutate(self.park.restore, self.plan.new_name, self.plan.old_name)
        self.parked = False

    async def _put_old_name_back(self) -> None:
        """The output is not in place: the Source goes back under the name
        RomM gave it, then RomM puts the old name back (and moves it)."""
        plan = self.plan
        if self.parked:
            await self._step("restoring")
            await _mutate(self.park.restore, plan.new_name)
            self.parked = False
        if self.romm_name == plan.old_name:
            return
        await self._step("reverting")
        await _mutate(romm_client.rename_rom, plan.rom_id, plan.old_name)
        name = await self._read_name()
        if name != plan.old_name:
            raise RommError(f"RomM still names the file {name}")
        self.romm_name = name


registry.register(RommSwapReferrer())
