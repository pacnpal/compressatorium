"""Ledger (#292): one row per terminal job, surviving a restart, with savings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from models import ConversionJob, ConversionMode, JobStatus
from services import db as _db
from services import ledger
from services.preferences_store import preferences_store


@pytest.fixture(autouse=True)
def _engine(db_path, reset_db_engine):
    _db.init_engine(db_path)
    yield
    reset_db_engine()


def _job(status=JobStatus.COMPLETED, deleted=True, done=None, **kw) -> ConversionJob:
    now = done or datetime.now(timezone.utc)
    return ConversionJob(
        id=kw.pop("id", "j1"), file_path="/data/ps2/Game.iso", filename="Game.iso",
        mode=ConversionMode.CREATEDVD, status=status, created_at=now,
        started_at=now - timedelta(seconds=30), completed_at=now,
        output_path="/data/ps2/Game.chd", output_size=400, input_bytes=1000,
        check_passed="intact" if deleted else None,
        deleted_paths=["/data/ps2/Game.iso"] if deleted else [], **kw,
    )


@pytest.mark.asyncio
async def test_rows_survive_restart_and_count_savings(db_path, reset_db_engine):
    await ledger.record(_job())
    await ledger.record(_job(id="j2", status=JobStatus.FAILED, deleted=False,
                             error_message="boom"))
    await ledger.record(_job(id="j3", status=JobStatus.CANCELLED, deleted=False))
    reset_db_engine()
    _db.init_engine(db_path)

    rows = await ledger.recent(10)
    assert {r["status"] for r in rows} == {"completed", "failed", "cancelled"}
    done = next(r for r in rows if r["job_id"] == "j1")
    assert done["tool"] == "chdman" and done["input_bytes"] == 1000
    assert done["check_passed"] == "intact" and done["source_deleted"] is True
    assert done["deleted_paths"] == ["/data/ps2/Game.iso"]
    assert done["duration_s"] == pytest.approx(30)

    s = await ledger.savings()
    # Only the completed job whose Source was deleted reclaimed space.
    assert s["saved_bytes"] == 600 and s["conversions"] == 1
    assert s["by_format"] == {"chd": 600}
    assert sum(s["by_platform"].values()) == 600


@pytest.mark.asyncio
async def test_retention_prunes_old_rows():
    old = datetime.now(timezone.utc) - timedelta(days=40)
    await ledger.record(_job(id="old", done=old))
    await preferences_store.put(ledger.PREF_KEY, {"retention_days": 30})
    await ledger.record(_job(id="new"))
    assert [r["job_id"] for r in await ledger.recent(10)] == ["new"]


@pytest.mark.asyncio
async def test_kept_forever_by_default():
    old = datetime.now(timezone.utc) - timedelta(days=4000)
    await ledger.record(_job(id="old", done=old))
    await ledger.record(_job(id="new"))
    assert len(await ledger.recent(10)) == 2
