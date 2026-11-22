"""A watch folder's copy is filed under its console and says where it went (#572).

``intake._copy`` knew the Image's Platform (its folder's, or the one picked
with Place it) and the folder it went into, but finished its ``inbox_copy``
job keeping the last progress line ("Copying into …") and no Platform, so
History showed **Console —** and a copy still under way.
"""
from __future__ import annotations

import pytest

from .test_inbox_431 import (  # noqa: F401
    _db_fixture,
    _drop,
    _inbox,
    _library,
    _poll,
    _vol,
)


def _copies():
    from models import ConversionMode
    from services.inbox import intake

    return [j for j in intake.job_manager.jobs.values() if j.mode == ConversionMode.INBOX_COPY]


@pytest.mark.asyncio
async def test_a_finished_copy_has_its_console_and_says_where_it_went(vol, db):
    from models import JobStatus

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=1)

    [job] = _copies()
    assert job.status == JobStatus.COMPLETED
    assert job.platform == "ps2"
    assert job.message == f"Copied into {vol / 'ROMs' / 'ps2'}"
