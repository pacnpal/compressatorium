"""A conversion re-checks its Source's identity and containment when it runs (#449).

Every conversion is queued carrying the identity of the Source it saw
(``utils.path_utils.queued_source_identity``). Here a parent folder is swapped
for a symlink out of the Volumes after the job is queued, by a Rule sweep and
by a hand-started conversion: the job is refused, and the tool never runs, so
nothing outside the Volumes is read.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from .test_cli_run_rules_307 import (  # the fixtures, registered by import
    RVZ_RULE,
    _dolphin,  # noqa: F401
    _env,  # noqa: F401
    _iso,
    _store_rules,
)


def _swap_for_link(folder, outside) -> None:
    """Move *folder* aside and put a link to *outside* in its place, holding a
    file of every name the folder had."""
    outside.mkdir()
    for entry in folder.iterdir():
        (outside / entry.name).write_bytes(b"outside the Volumes")
    folder.rename(folder.with_name(folder.name + ".moved"))
    folder.symlink_to(outside, target_is_directory=True)


def test_a_rule_sweep_job_refuses_a_folder_swapped_for_a_link(env, dolphin, tmp_path):
    from models import JobStatus
    from services.concurrency_manager import concurrency_manager
    from services.job_manager import job_manager
    from services.rules import sweep as rules_sweep

    iso = _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})

    async def run():
        await rules_sweep.sweep(env["shelf"], platform_ids=["gc"])
        [job] = [j for j in job_manager.jobs.values() if j.file_path == str(iso)]
        _swap_for_link(iso.parent, tmp_path / "outside")
        try:
            await job_manager._process_job(job.id)
        finally:
            concurrency_manager.release(job.id)
        return job

    job = asyncio.run(run())

    assert job.status == JobStatus.FAILED
    assert "no longer inside the configured Volumes" in job.error_message
    assert dolphin["runs"] == []  # the tool never read anything


@pytest.mark.asyncio
async def test_a_hand_started_job_refuses_a_folder_swapped_for_a_link(e2e_env, tmp_path_factory):
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
    from app.routes import convert as convert_routes

    folder = e2e_env["tmp_path"] / "games"
    folder.mkdir()
    iso = folder / "Game.iso"
    iso.write_bytes(b"inside the Volume")
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(iso), mode=ConversionMode.CSO_COMPRESS,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
    ))

    _swap_for_link(folder, tmp_path_factory.mktemp("elsewhere") / "outside")
    await convert_routes.job_manager._process_job(job.id)

    assert job.status.value == JobStatus.FAILED.value
    assert "no longer inside the configured Volumes" in job.error_message
    assert e2e_env["calls"] == []  # the tool never read anything


@pytest.mark.asyncio
async def test_a_job_queued_without_an_identity_is_refused(tmp_volume, tmp_path):
    """A file job that reached the queue some other way carries no identity:
    nothing tells what it would read from what it was queued for."""
    from app.models import ConversionJob, ConversionMode, JobStatus
    from app.services.job_manager import job_manager

    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"disc")
    job = ConversionJob(
        id="noidentity449", file_path=str(iso), filename=iso.name,
        mode=ConversionMode.CSO_COMPRESS, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(tmp_path / "Game.cso"),
    )
    job_manager.jobs[job.id] = job
    try:
        await job_manager._process_job(job.id)
    finally:
        job_manager.jobs.pop(job.id, None)

    assert job.status == JobStatus.FAILED
    assert "queued without its Source's identity" in job.error_message
    assert not os.path.exists(job.output_path)
