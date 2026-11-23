"""A watch folder's copy is filed under its console and says where it went (#572).

``intake._copy`` knew the Image's Platform (its folder's, or the one picked
with Place it) and the folder it went into, but finished its ``inbox_copy``
job keeping the last progress line ("Copying into …") and no Platform, so
History showed **Console —** and a copy still under way.
"""
from __future__ import annotations

import pytest

from .test_inbox_431 import _db_fixture, _drop, _inbox, _library, _poll, _vol

__all__ = ["_db_fixture", "_vol"]


@pytest.fixture(name="new_jobs")
def _new_jobs():
    """The jobs this test made: the job manager is shared across the suite, so
    jobs left by earlier tests are set aside."""
    from services.inbox import intake

    before = set(intake.job_manager.jobs)
    return lambda: [j for i, j in intake.job_manager.jobs.items() if i not in before]


def _copies(new_jobs):
    from models import ConversionMode

    return [j for j in new_jobs() if j.mode == ConversionMode.INBOX_COPY]


@pytest.mark.asyncio
async def test_a_finished_copy_has_its_console_and_says_where_it_went(vol, db, new_jobs):
    from models import JobStatus

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=1)

    [job] = _copies(new_jobs)
    assert job.status == JobStatus.COMPLETED
    assert job.platform == "ps2"
    assert job.message == f"Copied into {vol / 'ROMs' / 'ps2'}"


@pytest.mark.asyncio
async def test_unpack_and_recognize_jobs_say_what_they_did_with_their_console(
        vol, db, new_jobs, monkeypatch):
    """The same for a watch folder's other jobs: an archive unpacked and its
    game checked against the game lists, both in a console's folder."""
    from models import ConversionMode, JobStatus

    from . import test_inbox_432 as inbox_432

    inbox_432._verifies(monkeypatch)
    rom = inbox_432._bytes("history")
    await inbox_432._dat(
        "Nintendo - Game Boy Advance (No-Intro)", {"Job Game": inbox_432._sha1(rom)},
    )
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    inbox_432._zip(vol / "Downloads" / "gba" / "jobs.zip", {"jobs.gba": rom})

    await inbox_432._polls(box)

    by_mode = {j.mode: j for j in new_jobs()}
    unpack = by_mode[ConversionMode.INBOX_UNPACK]
    recognize = by_mode[ConversionMode.INBOX_RECOGNIZE]
    # An archive's own console isn't worked out (it isn't a game); what it
    # held is, and its jobs say so.
    assert (unpack.status, unpack.message) == (JobStatus.COMPLETED, "Unpacked 1 game")
    assert (recognize.status, recognize.platform, recognize.message) == (
        JobStatus.COMPLETED, "gba", "Checked against the game lists",
    )
