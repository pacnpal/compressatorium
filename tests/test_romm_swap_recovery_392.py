"""Startup recovery of unfinished RomM swaps (#392).

Each test seeds what a swap left when the process stopped on one step (the
journal row, the files, RomM's record) and runs the recovery entry,
``swap_recovery.recover_once``, against the fake RomM: a swap that is safe
to finish is finished, anything else is a Finding naming the ROM, the step,
where the Source and the output are, and what to do by hand.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from .fake_romm import FakeRomm
from .test_romm_swap_391 import (
    OUTPUT_BYTES,
    PS2,
    ROM_ID,
    SCAN,
    SOURCE_BYTES,
    _convert,
    _platform,
    _romm,
    _vol,
)

STAGE = ".compressatorium-stage"
# The #391 fixtures this module runs on (``vol``, ``romm``), by name.
FIXTURES = (_vol, _romm)


def _rows() -> list[dict]:
    from services.romm import swap_journal

    return swap_journal.rows()


def _swap_findings() -> list[dict]:
    from services import findings

    return [f for f in findings.list_findings() if f["kind"] == findings.ROMM_SWAP]


class Seed:
    """What a stopped swap left: the files, RomM's record and the row."""

    def __init__(self, vol: Path, romm: FakeRomm) -> None:
        self.platform = _platform(vol)
        self.romm = romm
        self.source = self.platform / "Game.iso"
        self.output = self.platform / "Game.chd"
        st = self.source.stat()
        self.source_size, self.source_mtime_ns = st.st_size, st.st_mtime_ns
        self.park = self.platform / STAGE / "park-abcd1234"
        self.parked = self.park / "Game.chd"
        # The job set its checked output aside when the swap held it.
        self.kept_slot = self.platform / STAGE / "park-kept5678"
        self.kept = self.kept_slot / "Game.chd"
        self.kept_slot.mkdir(parents=True)
        self.kept.write_bytes(OUTPUT_BYTES)

    def romm_renamed(self) -> None:
        """RomM renamed its record (and moved the Source to the new name)."""
        os.rename(self.source, self.output)
        FakeRomm._set_name(self.romm.roms[ROM_ID], "Game.chd")

    def park_source(self) -> None:
        """The Source, under the output's name, moved into the park slot."""
        self.park.mkdir(parents=True, exist_ok=True)
        os.rename(self.output, self.parked)

    def place_output(self) -> None:
        os.rename(self.kept, self.output)
        self.kept_slot.rmdir()

    def row(self, state: str, *, romm_url: str | None = "same", **extra) -> int:
        from services.romm import swap_journal

        row_id = swap_journal.begin(
            rom_id=ROM_ID, source_path=str(self.source), output_path=str(self.output),
            staged_path=str(self.platform / STAGE / "slot-kept5678" / "Game.chd"),
            old_name="Game.iso", new_name="Game.chd",
            romm_url=self.romm.url if romm_url == "same" else romm_url,
            library_root=str(self.platform.parent),
            source_size=self.source_size, source_mtime_ns=self.source_mtime_ns,
            output_size=len(OUTPUT_BYTES),
            output_sha1=hashlib.sha1(OUTPUT_BYTES).hexdigest(),
            **extra,
        )
        parked = str(self.parked) if state not in ("renaming",) else None
        swap_journal.step(row_id, state, parked_path=parked, detail="the app stopped")
        return row_id


def _files(platform: Path) -> list[str]:
    return sorted(str(p.relative_to(platform)) for p in platform.rglob("*"))


async def _recover():
    from services.romm import swap_recovery

    return await swap_recovery.recover_once()


# ---- finished --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["renaming", "reverting"])
async def test_a_swap_romm_never_renamed_is_settled_and_its_output_placed(
    vol, romm, state,
):
    """RomM still names the Source, which is where it was: nothing changed in
    RomM (the rename never landed, or the undo put the old name back). The
    row is settled, the output the job kept is placed beside the Source and
    its slot removed, and RomM is never asked to rename."""
    seed = Seed(vol, romm)
    seed.row(state)

    await _recover()

    (row,) = _rows()
    assert row["state"] == ("failed" if state == "renaming" else "rolled_back"), row
    assert seed.source.read_bytes() == SOURCE_BYTES
    assert seed.output.read_bytes() == OUTPUT_BYTES
    assert _files(seed.platform) == ["Game.chd", "Game.iso"]
    assert romm.renames == [] and romm.scans == []
    assert romm.roms[ROM_ID]["fs_name"] == "Game.iso"
    assert _swap_findings() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["placing", "placed", "rehashing", "deleting"])
async def test_a_swap_with_its_output_in_place_is_finished(vol, romm, state):
    """RomM names the output, which is in place, and the Source is parked:
    the rehash is retried (safe, per the spike), the read-back matches, the
    parked Source is deleted and its park slot removed. The row ends done."""
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    seed.row(state)

    await _recover()

    (row,) = _rows()
    assert row["state"] == "done", row
    assert romm.scans == [SCAN]
    assert romm.renames == []
    rom = romm.roms[ROM_ID]
    assert rom["fs_name"] == "Game.chd"
    assert rom["sha1_hash"] == hashlib.sha1(OUTPUT_BYTES).hexdigest()
    assert _files(seed.platform) == ["Game.chd"]
    assert _swap_findings() == []


@pytest.mark.asyncio
async def test_a_swap_whose_source_was_already_deleted_is_done(vol, romm):
    """Stopped after the parked Source was deleted: the rehash already
    passed, so the row is simply done."""
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    seed.parked.unlink()
    seed.row("deleting")

    await _recover()

    assert _rows()[0]["state"] == "done"
    assert _files(seed.platform) == ["Game.chd"]
    assert _swap_findings() == []


@pytest.mark.asyncio
async def test_a_read_back_that_does_not_match_keeps_the_source_beside_the_output(
    vol, romm,
):
    """The retried rehash doesn't confirm the output: as the swap's own undo
    would, the parked Source goes back under its own name beside the output,
    which keeps the ROM's id. The row ends kept."""
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    seed.row("rehashing")
    romm.scan_updates = False

    await _recover()

    assert _rows()[0]["state"] == "kept"
    assert seed.source.read_bytes() == SOURCE_BYTES
    assert seed.output.read_bytes() == OUTPUT_BYTES
    assert _files(seed.platform) == ["Game.chd", "Game.iso"]
    assert _swap_findings() == []


@pytest.mark.asyncio
async def test_an_undo_putting_the_source_back_beside_the_output_is_finished(vol, romm):
    """Stopped while restoring the parked Source beside the placed output:
    the move is finished (files only) and the row ends kept, with no RomM
    write."""
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    seed.row("restoring")

    await _recover()

    assert _rows()[0]["state"] == "kept"
    assert _files(seed.platform) == ["Game.chd", "Game.iso"]
    assert seed.source.read_bytes() == SOURCE_BYTES
    assert romm.renames == [] and romm.scans == []
    assert _swap_findings() == []


# ---- Findings ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["renaming", "parking"])
async def test_a_source_romm_moved_but_not_parked_is_a_finding(vol, romm, state):
    """RomM renamed the record and moved the Source to the output's name;
    the output was never placed. Finishing needs the output moved in, and
    undoing needs RomM to rename back, which the spike never proved: a
    Finding that names the ROM, the step, both files and what to do."""
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.row(state)
    before = _files(seed.platform)

    await _recover()

    assert _rows()[0]["state"] == state
    assert _files(seed.platform) == before
    assert romm.renames == [] and romm.scans == []
    (finding,) = _swap_findings()
    reason = finding["reason"]
    assert f"ROM {ROM_ID}" in reason
    assert state in reason
    assert str(seed.output) in reason  # where the Source is now
    assert str(seed.kept) in reason  # where the output is
    assert "Game.iso" in reason and "by hand" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["parked", "placing"])
async def test_a_parked_source_without_its_output_placed_is_a_finding(vol, romm, state):
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.row(state)
    before = _files(seed.platform)

    await _recover()

    assert _rows()[0]["state"] == state
    assert _files(seed.platform) == before
    assert romm.renames == [] and romm.scans == []
    (finding,) = _swap_findings()
    assert str(seed.parked) in finding["reason"]
    assert str(seed.park) in finding["cause"]["slots"]


@pytest.mark.asyncio
async def test_a_rename_back_that_never_landed_is_a_finding_not_retried(vol, romm):
    """The undo was asking RomM for the old name back (``reverting``) and
    RomM still has the new one. Renaming back was never proven against RomM,
    so recovery does not retry it: a Finding."""
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.row("reverting")

    await _recover()

    assert _rows()[0]["state"] == "reverting"
    assert romm.renames == []
    (finding,) = _swap_findings()
    assert "reverting" in finding["reason"]


# ---- idempotent, unreachable, identity -------------------------------------


@pytest.mark.asyncio
async def test_a_second_run_changes_nothing(vol, romm):
    """One row finished and one Finding: running recovery again sends nothing
    to RomM, moves nothing and leaves the Finding as it was."""
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.row("parking")
    await _recover()
    files, rows, found = _files(seed.platform), _rows(), _swap_findings()
    requests = (list(romm.renames), list(romm.scans))

    await _recover()

    assert _files(seed.platform) == files
    assert _rows() == rows
    assert [(f["id"], f["fingerprint"]) for f in _swap_findings()] == [
        (f["id"], f["fingerprint"]) for f in found
    ]
    assert (romm.renames, romm.scans) == requests


@pytest.mark.asyncio
async def test_a_finished_swap_is_not_finished_twice(vol, romm):
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    seed.row("placed")
    await _recover()
    rows, scans = _rows(), list(romm.scans)

    await _recover()

    assert _rows() == rows and romm.scans == scans


@pytest.mark.asyncio
async def test_an_unreachable_romm_leaves_the_row_and_a_finding_then_retries(
    vol, romm, monkeypatch,
):
    """RomM doesn't answer: the pass ends within its bound, nothing is moved,
    the row stays and a Finding says so. Once RomM answers, the next pass
    finishes the swap and the Finding goes."""
    from services.romm import swap_recovery
    from services.romm.client import RommClient, RommError

    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    seed.row("rehashing")
    before = _files(seed.platform)
    real_rom = RommClient.rom

    def down(self, rom_id):
        raise RommError("connection refused")

    monkeypatch.setattr(RommClient, "rom", down)
    await asyncio.wait_for(swap_recovery.recover_once(), 10)

    assert _rows()[0]["state"] == "rehashing"
    assert _files(seed.platform) == before
    (finding,) = _swap_findings()
    assert "couldn't be reached" in finding["reason"]

    monkeypatch.setattr(RommClient, "rom", real_rom)
    await swap_recovery.recover_once()

    assert _rows()[0]["state"] == "done"
    assert _swap_findings() == []


@pytest.mark.asyncio
async def test_the_background_run_retries_until_the_rows_are_settled(
    vol, romm, monkeypatch,
):
    """``run_forever`` never blocks startup: it runs after it, and while a
    row is left (RomM unreachable) it tries again later, then stops once
    every row it found at startup is settled."""
    from services.romm import swap_recovery
    from services.romm.client import RommClient, RommError

    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    seed.row("placed")
    real_rom = RommClient.rom
    calls = 0

    def down_once(self, rom_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RommError("connection refused")
        return real_rom(self, rom_id)

    monkeypatch.setattr(RommClient, "rom", down_once)
    monkeypatch.setattr(swap_recovery, "RETRY_SECONDS", 0.05)
    await asyncio.wait_for(swap_recovery.run_forever(), 10)

    assert _rows()[0]["state"] == "done"
    assert _swap_findings() == []


@pytest.mark.asyncio
async def test_a_row_from_another_romm_instance_is_a_finding_never_touched(
    vol, romm,
):
    """RomM's settings name another instance than the one the swap ran
    against: nothing is sent to the new instance, nothing moves, and the row
    is a Finding."""
    from services.romm import settings as romm_settings

    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    seed.row("placed")
    other = FakeRomm(platforms=[PS2], library=vol / "romm").start()
    try:
        await romm_settings.save({"url": other.url}, cleanup_pending=True)
        before = _files(seed.platform)

        await _recover()

        assert other.renames == [] and other.scans == []
        assert not [p for p in other.paths() if p.startswith("/api/roms")]
    finally:
        other.stop()
    assert romm.scans == []
    assert _rows()[0]["state"] == "placed"
    assert _files(seed.platform) == before
    (finding,) = _swap_findings()
    assert "another RomM instance" in finding["reason"]


@pytest.mark.asyncio
async def test_a_row_that_does_not_say_its_instance_is_a_finding(vol, romm):
    """A row written before the journal named its RomM instance can't be
    told to be this one: fail closed, a Finding."""
    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    seed.row("placed", romm_url=None)

    await _recover()

    assert romm.scans == []
    assert _rows()[0]["state"] == "placed"
    assert len(_swap_findings()) == 1


@pytest.mark.asyncio
async def test_a_row_of_a_swap_running_now_is_left_alone(vol, romm):
    """Only the rows that were unfinished when the app started are
    recovered: one written since belongs to a swap of this run."""
    from services.romm import swap_recovery

    seed = Seed(vol, romm)
    seed.romm_renamed()
    seed.park_source()
    seed.place_output()
    started = "2000-01-01T00:00:00Z"
    seed.row("placed")

    await swap_recovery.recover_once(started_before=started)

    assert _rows()[0]["state"] == "placed"
    assert romm.scans == []
    assert _swap_findings() == []


# ---- the journal records what recovery needs --------------------------------


@pytest.mark.asyncio
async def test_a_swap_records_its_instance_and_its_files_fingerprints(vol, romm):
    platform = _platform(vol)
    st = (platform / "Game.iso").stat()

    await _convert(platform / "Game.iso")

    (row,) = _rows()
    assert row["romm_url"] == romm.url
    assert row["library_root"] == str(vol / "romm")
    assert (row["source_size"], row["source_mtime_ns"]) == (st.st_size, st.st_mtime_ns)
    assert row["output_size"] == len(OUTPUT_BYTES)
    assert row["output_sha1"] == hashlib.sha1(OUTPUT_BYTES).hexdigest()
