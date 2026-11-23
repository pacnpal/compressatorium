"""A job the Auto plan queued carries the Platform the plan showed (#564).

The person picks a console for a game Compressatorium couldn't place; the plan
then reads "1 PlayStation 2 game → CHD". The job used to be queued with no
Platform and detection at run time can't find one either, so History filed
it under no console. The CLI's ``--auto`` and the Rules sweep already carry
the planned Platform onto their jobs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import AutoJobCreateRequest, AutoJobItem, AutoPlanRequest
from app.routes import convert as convert_routes

from .test_auto_target_4 import queue_env  # noqa: F401  (fixture)
from .test_platform_detect import _blank, _ps2

pytestmark = pytest.mark.usefixtures("fresh_detect_cache", "tmp_volume")


@pytest.fixture
def made_jobs(queue_env, monkeypatch):  # noqa: F811  (the fixture above)
    """The queue answers real jobs, so the route's handling of them shows."""
    made: list = []

    async def atomic(job_specs, mode, **kwargs):
        jobs = [
            convert_routes.ConversionJob(
                id=f"j{len(made) + n}", file_path=spec["file_path"],
                filename=Path(spec["file_path"]).name, mode=mode, status="queued",
                created_at="2026-11-22T00:00:00Z",
            )
            for n, spec in enumerate(job_specs)
        ]
        made.extend(jobs)
        return jobs

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", atomic)
    return {**queue_env, "made": made}


async def _queue(paths, overrides=None):
    plan = await convert_routes.auto_plan(
        AutoPlanRequest(file_paths=paths, platform_overrides=overrides or {}),
    )
    return plan, await convert_routes.create_auto_jobs(AutoJobCreateRequest(
        items=[AutoJobItem(file_path=row.file_path, mode=row.mode) for row in plan if row.mode],
        platform_overrides=overrides or {},
    ))


@pytest.mark.asyncio
async def test_the_console_the_person_picked_reaches_the_job(made_jobs):
    mystery = _blank(made_jobs["tmp_path"] / "downloads" / "mystery.iso")

    plan, result = await _queue([mystery], {mystery: "ps2"})

    assert plan[0].platform == "ps2"
    [job] = result.jobs
    assert job.platform == "ps2"


@pytest.mark.asyncio
async def test_a_detected_console_reaches_the_job_too(made_jobs):
    iso = _ps2(made_jobs["tmp_path"] / "pile" / "game.iso")

    plan, result = await _queue([iso])

    [job] = result.jobs
    assert job.platform == plan[0].platform == "ps2"
