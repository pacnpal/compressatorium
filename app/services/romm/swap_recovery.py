"""Startup recovery of unfinished RomM swaps (#392).

A RomM swap (``services.romm.swap_referrer``) writes each step to the swap
journal (``services.romm.swap_journal``) before it runs. If the app stops
part way, the row stays on the step it reached, the Source may be parked in a
``park-`` slot and the job's checked output may be set aside in the hidden
stage folder. After the next start, :func:`run_forever` settles those rows in
the background: it never holds up startup, every file and RomM call in it is
bounded, and a row it can't settle now is tried again every
:data:`RETRY_SECONDS` until it can.

**What it looks at.** For each row unfinished when the app started (a row
written since belongs to a swap of this run): the files, told apart by the
fingerprints the row recorded (the Source by size and mtime, the output by
size and SHA-1) at the Source's own path, the output's path and the parked
path, and RomM's name for the ROM. The files are read first, so a Finding
says where they are even when RomM can't be reached.

**What it finishes** (only these; anything else is a Finding):

* RomM still names the Source and the Source is where it was (a rename that
  never landed at ``renaming``, or an undo that already put the old name
  back): nothing changed in RomM. The row ends ``failed`` (from
  ``renaming``) or ``rolled_back``, and the output the job set aside is put
  beside the Source, as the job does for a kept Source.
* RomM names the output and the output is in place (``placing`` once it
  moved, ``placed``, ``rehashing``, ``deleting``): the rehash is retried
  (a ``hashes`` scan of the one ROM, which the spike, #382, showed is safe to
  repeat) and the ROM read back. A match deletes the parked Source, after
  checking it is still the Source, and the row ends ``done`` (and the
  output's re-pin is settled). A mismatch puts the parked Source back under
  its own name beside the output, as the swap's own undo does, and the row
  ends ``kept``. At ``deleting`` with the parked Source already gone, the
  read-back had passed: the row ends ``done``.
* The undo was putting the parked Source back beside the placed output
  (``restoring``): the move is finished, files only, and the row ends
  ``kept``.

**What it never does.** It never renames a ROM in RomM. Renaming back to the
old ``fs_name`` was not proven against RomM in the spike, so a Source RomM
moved to the output's name (``renaming``, ``parking``), a Source parked
while the output was never placed (``parked``, ``placing``), and an undo
that asked for the old name back but RomM still has the new one
(``reverting``) are Findings. It never acts on another RomM instance: a row
whose recorded instance (URL and library root) is not the one RomM's
settings name now, or a row written before the journal recorded it, is a
Finding, and nothing is sent to the instance configured now. Every RomM
write (the rehash) is counted as a running swap, so a settings save that
moves the instance waits for it (``swap_referrer.identity_change``), and is
refused while one is waiting.

**Findings.** One per row (``findings.ROMM_SWAP``, owned by the RomM Library,
image key ``romm-swap-<row id>``): the ROM, the step it stopped at, why it
can't be finished, where the Source and the output are, the slots left in
the stage folder, and what to do by hand. It stays until the row is settled:
by this, or by a later pass once the files and RomM agree (the user did
what it says). A pass that sees the same thing writes the same Finding, so
running it again changes nothing.

**Slots.** The ``park-`` slot of a settled row, and the slot the job kept its
output in once that output is placed, are removed, but only once their own
listing is empty (``staging.reopen_slot``). A Finding leaves them and names
them.

Every file read runs on a detached thread under a bound
(``swap_referrer._probe``, ``_read_whole``) and every move on one never
abandoned (``_mutate``), as the swap's own steps do.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass

from logging_setup import get_logger
from services import db as _db
from services import findings, partial_writes, staging
from services.lock_manager import lock_manager
from services.romm import settings as romm_settings
from services.romm import swap_journal, swap_referrer
from services.romm.client import RommError
from services.romm.swap_referrer import (
    POLL_SECONDS,
    REHASH_SECONDS,
    ROMM_SECONDS,
    _Binding,
    _mutate,
    _Plan,
    _probe,
    _read_back_problem,
    _read_whole,
    _settle_repin,
)
from services.subprocess_runner import run_detached_undoable
from utils.path_utils import file_identity

logger = get_logger("romm_swap_recovery")

# How long the background run waits before trying the rows it couldn't settle
# again.
RETRY_SECONDS = 600.0
# How long taking a row's job locks may take.
LOCK_SECONDS = 20.0
# The RomM Library's id (``RommLibrary.id``): the owner of these Findings.
LIBRARY_ID = "romm"

_TASK_ENDED = frozenset({"finished", "failed", "stopped", "canceled", "cancelled"})
# A file at one of the swap's paths is one of these.
ABSENT, SOURCE, OUTPUT, OTHER = "absent", "source", "output", "other"


class _Left(Exception):
    """This row can't be settled now: why, for its Finding."""


@dataclass
class _Facts:
    """What a row's files and RomM say now."""

    source: str  # what is at the Source's path
    output: str  # what is at the output's path
    parked: str  # what is at the parked path (ABSENT when none was recorded)
    kept: str | None = None  # where the job's set-aside output is, if found
    romm_name: str | None = None  # RomM's fs_name for the ROM, once read


def _image_key(row: dict) -> str:
    return f"romm-swap-{row['id']}"


def _what_is(path: str | None, row: dict, *, hashed: bool = True) -> str:
    """What is at *path*: the Source, the output, something else, or
    nothing. Blocking. The output is told by its SHA-1 too, read only when
    the size is the output's; without *hashed*, by its size alone (a
    re-check, just before a move, of a file already hashed)."""
    if not path:
        return ABSENT
    if not os.path.lexists(path):
        return ABSENT
    identity = file_identity(path)
    if identity is None or os.path.islink(path):
        return OTHER
    if (identity.size, identity.mtime_ns) == (row["source_size"], row["source_mtime_ns"]):
        return SOURCE
    if identity.size == row["output_size"]:
        if not hashed:
            return OUTPUT
        size, sha1 = swap_referrer._size_and_sha1(path)
        if (size, sha1) == (row["output_size"], row["output_sha1"]):
            return OUTPUT
    return OTHER


def _kept_slots(row: dict) -> list[str]:
    """Where the job's set-aside output may be: the slot it was staged in
    (marked kept in place) or that slot renamed to a ``park-`` slot
    (``StagedOutput.set_aside``)."""
    staged = row.get("staged_path")
    if not staged:
        return []
    slot = os.path.dirname(staged)
    name = os.path.basename(slot)
    found = [slot]
    if name.startswith(partial_writes.SLOT_PREFIX):
        found.append(os.path.join(
            os.path.dirname(slot), staging.PARK_PREFIX + name[len(partial_writes.SLOT_PREFIX):],
        ))
    return found


def _find_kept(row: dict) -> str | None:
    """The job's set-aside output, if it is in one of its slots. Blocking."""
    name = os.path.basename(row["output_path"])
    for slot in _kept_slots(row):
        candidate = os.path.join(slot, name)
        if _what_is(candidate, row) == OUTPUT:
            return candidate
    return None


def _look(row: dict) -> _Facts:
    """The row's files now. Blocking."""
    facts = _Facts(
        source=_what_is(row["source_path"], row),
        output=_what_is(row["output_path"], row),
        parked=_what_is(row.get("parked_path"), row),
    )
    if facts.output != OUTPUT:
        facts.kept = _find_kept(row)
    return facts


def _slots_left(row: dict) -> list[str]:
    """The stage-folder slots this row's swap and job left. Blocking."""
    slots = []
    if row.get("parked_path"):
        slots.append(os.path.dirname(row["parked_path"]))
    slots.extend(_kept_slots(row))
    return [slot for slot in slots if os.path.isdir(slot)]


def _release(slot_path: str, *, marker: bool = False) -> None:
    """Remove *slot_path* if it holds nothing (or only its keep marker,
    with *marker*); otherwise leave it as it is. Blocking; never raises."""
    if not os.path.isdir(slot_path):
        return
    try:
        slot = staging.reopen_slot(slot_path)
    except (OSError, ValueError):
        logger.warning("Swap recovery: could not take %s", slot_path, exc_info=True)
        return
    try:
        held = slot.contents()
        if marker and held == [partial_writes.KEEP_MARKER]:
            slot.remove(partial_writes.KEEP_MARKER)
            held = []
    except OSError:
        logger.warning("Swap recovery: could not list %s", slot_path, exc_info=True)
        held = None
    if held == []:
        slot.close()
    else:
        slot.keep()


def _place_kept(row: dict, kept: str) -> None:
    """Move the job's set-aside output to the output's path, never over
    anything, with its check and CHD records (the placing the job does:
    ``StagedOutput.place``, then ``staging.carry_records_blocking``, Codex
    PRRT_kwDOQ9nULM64SAMu), then remove its slot once empty. Blocking."""
    slot = staging.reopen_slot(os.path.dirname(kept))
    try:
        if _what_is(kept, row, hashed=False) != OUTPUT:
            raise _Left(f"{kept} is no longer the converted output")
        if os.path.basename(kept) != os.path.basename(row["output_path"]):
            raise _Left(f"{kept} is not named as the output")
        staging.carry_records_blocking(slot.place([os.path.basename(kept)]))
    finally:
        slot.keep()
    _release(os.path.dirname(kept), marker=True)


def _delete_parked(row: dict) -> None:
    """Delete the parked Source, checked to be the Source first, then its
    slot once empty. Blocking."""
    parked = row["parked_path"]
    slot = staging.reopen_slot(os.path.dirname(parked))
    try:
        if _what_is(parked, row) != SOURCE:
            raise _Left(f"{parked} is no longer the Source, so it was not deleted")
        slot.remove(os.path.basename(parked))
    finally:
        slot.keep()
    _release(os.path.dirname(parked))


def _restore_parked(row: dict) -> None:
    """Move the parked Source back under its own name, beside the output,
    never over anything, then remove its slot once empty. Blocking."""
    parked = row["parked_path"]
    slot = staging.reopen_slot(os.path.dirname(parked))
    try:
        if _what_is(parked, row) != SOURCE:
            raise _Left(f"{parked} is no longer the Source")
        slot.restore(os.path.basename(parked), os.path.basename(row["source_path"]))
    finally:
        slot.keep()
    _release(os.path.dirname(parked))


def _where(facts: _Facts, row: dict, what: str) -> str:
    """Where the Source (*what* = SOURCE) or the output is, in words."""
    spots = [
        (facts.source, row["source_path"]), (facts.output, row["output_path"]),
        (facts.parked, row.get("parked_path")),
    ]
    if what == OUTPUT and facts.kept:
        spots.append((OUTPUT, facts.kept))
    found = [path for kind, path in spots if kind == what and path]
    if found:
        return " and ".join(f"at {path}" for path in found)
    return "not found at the paths the swap recorded"


def _finding(row: dict, facts: _Facts | None, why: str) -> findings.Observed:
    """The Finding for a row that can't be settled now."""
    old, new = row["old_name"], row["new_name"]
    folder = os.path.dirname(row["output_path"])
    parts = [
        f"RomM swap of ROM {row['rom_id']} ({old} to {new}) stopped at {row['state']}: {why}.",
    ]
    if facts is not None:
        if facts.romm_name is not None:
            parts.append(f"RomM names its file {facts.romm_name}.")
        parts.append(f"The Source is {_where(facts, row, SOURCE)}.")
        parts.append(f"The converted output is {_where(facts, row, OUTPUT)}.")
    slots = _slots_left(row)
    if slots:
        parts.append(f"Left in the stage folder: {', '.join(slots)}.")
    parts.append(
        "To settle it by hand, either keep the conversion (put the output at "
        f"{row['output_path']}, delete the Source, and rescan the ROM in RomM) or undo it "
        f"(put the Source at {os.path.join(folder, new)}, rename the ROM's file back to {old} "
        "in RomM so RomM moves it, and keep the output aside). The next recovery pass settles "
        "the row once the files and RomM agree.",
    )
    cause = {
        "rom_id": row["rom_id"], "row": row["id"], "state": row["state"], "why": why,
        "romm_name": facts.romm_name if facts else None,
        "source": row["source_path"], "output": row["output_path"],
        "parked": row.get("parked_path"), "kept": facts.kept if facts else None,
        "slots": slots,
    }
    return findings.Observed(
        kind=findings.ROMM_SWAP, image_key=_image_key(row), path=row["source_path"],
        reason=" ".join(parts), cause=cause,
    )


@contextlib.contextmanager
def _counted() -> Iterator[None]:
    """Count a RomM write as a running swap, so an identity change waits
    for it; refused while one is waiting. On the event loop."""
    if swap_referrer._identity_changes:
        raise _Left("RomM's settings are being changed; tried again later")
    swap_referrer._active_swaps += 1
    try:
        yield
    finally:
        swap_referrer._active_swaps -= 1


class _Recovery:
    """One row's recovery: look, then finish or leave (see the module)."""

    def __init__(self, row: dict, binding: _Binding) -> None:
        self.row = row
        self.binding = binding
        self.client = binding.client()
        self.facts: _Facts | None = None

    def _same_instance(self) -> None:
        row = self.row
        root = str(romm_settings.effective().get("library_root") or "")
        if not self.binding.base_url or (row["romm_url"], row["library_root"]) != (
            self.binding.base_url, root,
        ):
            raise _Left(
                "RomM's settings now name another RomM instance or library root than the "
                "one it ran against, so nothing was sent to it",
            )

    async def _romm_name(self) -> str | None:
        try:
            rom = await _probe(self.client.rom, self.row["rom_id"], seconds=ROMM_SECONDS)
        except (RommError, OSError, asyncio.TimeoutError) as exc:
            raise _Left(f"RomM couldn't be reached ({exc}); tried again later") from exc
        return rom.get("fs_name")

    async def run(self) -> str:
        """Settle the row; its ending. Raises :class:`_Left`."""
        row = self.row
        if any(row.get(name) in (None, "") for name in swap_journal.RECOVERY_FIELDS):
            raise _Left(
                "its journal row doesn't record which RomM instance it ran against or how "
                "to tell its files apart (written by an earlier version)",
            )
        # The files first: a Finding says where they are even when RomM
        # can't be asked.
        self.facts = facts = await _read_whole(
            _look, row, what=f"Checking the files of RomM swap {row['id']}",
        )
        self._same_instance()
        facts.romm_name = await self._romm_name()
        if facts.romm_name == row["old_name"]:
            return await self._romm_never_renamed()
        if facts.romm_name == row["new_name"] and facts.output == OUTPUT:
            return await self._output_in_place()
        if facts.romm_name == row["new_name"]:
            raise _Left(
                "RomM names the output but the output is not in place; finishing needs it "
                "moved in, and undoing needs RomM to rename the file back, which recovery "
                "never does",
            )
        raise _Left(f"RomM names the file {facts.romm_name}, neither name the swap used")

    async def _romm_never_renamed(self) -> str:
        row, facts = self.row, self.facts
        if facts.source != SOURCE or facts.parked == SOURCE or facts.output not in (
            ABSENT, OUTPUT,
        ):
            raise _Left("RomM names the Source, but the files are not where that leaves them")
        if facts.output == ABSENT and facts.kept:
            await _mutate(_place_kept, row, facts.kept)
        return "failed" if row["state"] == "renaming" else "rolled_back"

    async def _output_in_place(self) -> str:
        row, facts = self.row, self.facts
        if facts.parked == OTHER:
            raise _Left(f"{row['parked_path']} holds something that is not the Source")
        if row["state"] == "restoring" or facts.source == SOURCE:
            return await self._source_beside_output()
        if row["state"] == "deleting" and facts.parked == ABSENT and facts.source == ABSENT:
            # The read-back passed before the delete was recorded.
            await self._settle_repin()
            return "done"
        if facts.parked == ABSENT and facts.source == ABSENT:
            # Finished by hand, as the Finding says (Codex
            # PRRT_kwDOQ9nULM64SAMo): the output in place and the Source
            # gone. Done only once RomM's rehash confirms it; until then the
            # Finding stays.
            await self._step("rehashing")
            problem = await self._rehash()
            if problem:
                raise _Left(f"the output is in place and the Source gone, but {problem}")
            await self._settle_repin()
            return "done"
        if facts.parked != SOURCE:
            raise _Left("the Source is not where the swap parked it")
        await self._step("rehashing")
        problem = await self._rehash()
        if problem:
            await self._step("restoring")
            await _mutate(_restore_parked, row)
            return "kept"
        # Journal first (Codex 4595373639): stopped after the delete, the
        # next run sees ``deleting`` with the Source gone, and ends it done.
        await self._step("deleting")
        await _mutate(_delete_parked, row)
        await self._settle_repin()
        return "done"

    async def _source_beside_output(self) -> str:
        row, facts = self.row, self.facts
        if facts.parked == SOURCE and facts.source == ABSENT:
            await self._step("restoring")
            await _mutate(_restore_parked, row)
        elif not (facts.source == SOURCE and facts.parked == ABSENT):
            raise _Left("the Source is neither parked nor back under its own name")
        return "kept"

    async def _rehash(self) -> str | None:
        """Retry the rehash and read the ROM back: why it doesn't match the
        output, or None. Raises :class:`_Left` when RomM can't be reached."""
        row = self.row
        with _counted():
            if not self.binding.still_live():
                raise _Left("RomM's settings changed; nothing was sent")
            try:
                task_id = await _mutate(self.client.scan_rom_hashes, [row["rom_id"]])
                deadline = time.monotonic() + REHASH_SECONDS
                while True:
                    task = await _probe(self.client.task, task_id, seconds=ROMM_SECONDS)
                    status = str(task.get("status") or "")
                    if status in _TASK_ENDED:
                        break
                    if time.monotonic() >= deadline:
                        raise _Left(f"RomM's rehash didn't finish within {REHASH_SECONDS:g}s")
                    await asyncio.sleep(POLL_SECONDS)
                if status != "finished":
                    return f"RomM's rehash ended {status}"
                rom = await _probe(self.client.rom, row["rom_id"], seconds=ROMM_SECONDS)
            except (RommError, OSError, asyncio.TimeoutError) as exc:
                raise _Left(f"RomM couldn't be reached ({exc}); tried again later") from exc
        plan = _Plan(
            rom_id=row["rom_id"], source=row["source_path"], output=row["output_path"],
            old_name=row["old_name"], new_name=row["new_name"], platform_slug=None,
        )
        return _read_back_problem(rom, plan, row["output_size"], row["output_sha1"])

    async def _step(self, state: str) -> None:
        """Write *state* to the row before the step it names runs, as the
        swap does: a stop part way leaves the step it was on."""
        row_id = self.row["id"]
        await _mutate(lambda: swap_journal.step(
            row_id, state, detail="recovering at startup",
        ))
        self.row = {**self.row, "state": state}

    async def _settle_repin(self) -> None:
        try:
            await _probe(_settle_repin, self.row["output_path"], self.row["rom_id"])
        except Exception:
            logger.warning("Swap recovery: could not settle the re-pin", exc_info=True)


def _lock(paths: list[str]) -> list[str]:
    """Take the job locks on *paths*, all or none: the ones taken. Blocking.
    A refusal or an error lets go of any already taken."""
    taken: list[str] = []
    try:
        for path in paths:
            if not lock_manager.acquire_lock(path, allow_existing=True):
                _unlock(taken)
                return []
            taken.append(path)
    except BaseException:
        _unlock(taken)
        raise
    return taken


def _unlock(paths: list[str]) -> None:
    for path in paths:
        lock_manager.release_lock(path)


async def _recover_row(row: dict, binding: _Binding) -> bool:
    """Settle one row, or write its Finding. True when settled. Never raises
    (but a cancel)."""
    recovery = _Recovery(row, binding)
    paths = [row["source_path"], row["output_path"]]
    locked: list[str] = []
    try:
        try:
            # Under a bound; a worker that takes them after it gives them
            # back (Codex 4595373643).
            locked = await run_detached_undoable(
                _lock, paths, timeout=LOCK_SECONDS, undo=_unlock,
            )
        except asyncio.TimeoutError as exc:
            raise _Left(
                "the job locks on its files couldn't be taken in time; tried again later",
            ) from exc
        if not locked:
            raise _Left("a job is using its files (it holds their locks); tried again later")
        ending = await recovery.run()
        if row.get("parked_path"):
            # Settled: its park slot goes, once its own listing is empty.
            await _mutate(_release, os.path.dirname(row["parked_path"]))
        for slot in _kept_slots(row):
            # And the job's kept-output slot, once empty (emptied by hand).
            await _mutate(lambda kept=slot: _release(kept, marker=True))
        detail = f"recovered at startup from {row['state']}"
        await _mutate(lambda: swap_journal.step(row["id"], ending, detail=detail))
    except _Left as exc:
        why = str(exc)
        logger.warning("Swap recovery: swap %s of ROM %s: %s", row["id"], row["rom_id"], why)
        await _leave_finding(row, recovery.facts, why)
        return False
    except Exception as exc:
        logger.exception("Swap recovery: swap %s of ROM %s failed", row["id"], row["rom_id"])
        await _leave_finding(row, recovery.facts, f"recovery failed ({exc})")
        return False
    finally:
        if locked:
            with contextlib.suppress(Exception):
                await _probe(_unlock, locked)
    try:
        await _probe(findings.resolve, LIBRARY_ID, _image_key(row), findings.ROMM_SWAP)
    except Exception:
        logger.warning("Swap recovery: could not clear the Finding", exc_info=True)
    logger.info("Swap recovery: swap %s of ROM %s ended %s", row["id"], row["rom_id"], ending)
    return True


def _observe_finding(row: dict, facts: _Facts | None, why: str) -> None:
    """Build the row's Finding (which looks at the Volume for the slots
    left) and write it. Blocking."""
    findings.observe(LIBRARY_ID, _finding(row, facts, why))


async def _leave_finding(row: dict, facts: _Facts | None, why: str) -> None:
    """Write the row's Finding, built and written on one bounded detached
    call, never on the event loop (Codex 4595373645). Never raises (but a
    cancel)."""
    try:
        await _probe(_observe_finding, row, facts, why)
    except Exception:
        logger.warning(
            "Swap recovery: could not write the Finding of swap %s", row["id"], exc_info=True,
        )


async def recover_once(*, started_before: str | None = None) -> dict:
    """One recovery pass over the unfinished rows (only those written before
    *started_before*, when given). ``{"settled", "left"}``. Never raises
    (but a cancel)."""
    try:
        rows = await _probe(swap_journal.unfinished)
    except Exception:
        logger.warning("Swap recovery: could not read the swap journal", exc_info=True)
        return {"settled": 0, "left": -1}
    if started_before is not None:
        rows = [row for row in rows if row["created_at"] < started_before]
    binding = _Binding.now()
    settled = 0
    for row in rows:
        settled += await _recover_row(row, binding)
    return {"settled": settled, "left": len(rows) - settled}


async def run_forever() -> None:
    """The background recovery run, started after startup: a pass over the
    rows unfinished when the app started, then again every
    :data:`RETRY_SECONDS` while any is left. Never raises (but a cancel)."""
    started = _db.utcnow_iso()
    while True:
        try:
            result = await recover_once(started_before=started)
        except Exception:
            logger.warning("Swap recovery: a pass failed", exc_info=True)
            result = {"left": -1}
        if result["left"] == 0:
            return
        await asyncio.sleep(RETRY_SECONDS)
