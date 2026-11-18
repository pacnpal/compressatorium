"""The Inbox says what the sanitizer changed on a Rule once per pass (#477).

An Inbox converts under its Library's Rule, so it runs the Rule like a sweep.
It logs a Rule's dropped codecs once per pass that converts under it, naming
the Library and the Platform: not once per Image, nor on each read.
"""
from __future__ import annotations

import logging

import pytest
from services.preferences_store import preferences_store

from .test_inbox_431 import _db_fixture, _drop, _inbox, _library, _poll, _queue, _vol

__all__ = ["_db_fixture", "_queue", "_vol"]


@pytest.fixture(autouse=True)
def _nothing_reported_yet(monkeypatch):
    from services.rules import sweep

    monkeypatch.setattr(sweep, "_reported_unrunnable", {})


@pytest.mark.asyncio
async def test_an_inbox_pass_logs_a_rules_dropped_codec_once(vol, db, queue, caplog):
    library = _library(vol)
    # Stored before #474: Create DVD with a codec it can't use.
    await preferences_store.put("rules", {
        f"{library.id}:ps2": {"enabled": True, "mode": "createdvd", "compression": "cdzl"},
    })
    box = _inbox(vol, library)
    _drop(vol / "Downloads" / "ps2" / "One.iso")
    _drop(vol / "Downloads" / "ps2" / "Two.iso")
    await _poll(box, now=0)

    with caplog.at_level(logging.DEBUG):
        result = await _poll(box, now=61)

    assert result["queued"] == 2
    assert {job["compression"] for job in queue.submitted} == {None}
    lines = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING and "cdzl" in r.getMessage()
    ]
    assert len(lines) == 1, lines
    assert lines[0].startswith("inbox:"), lines[0]
    assert "ROMs" in lines[0] and "ps2" in lines[0] and "Create DVD" in lines[0]
