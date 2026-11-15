"""Auto route: what the response says when part of a batch can't be queued."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.models import (
    AutoJobCreateRequest,
    ConversionMode,
    DuplicateAction,
    JobCreateRequest,
    JobStatus,
)
from app.routes import convert as convert_routes

from .test_archive_conversion_e2e import _e2e_env  # noqa: F401
from .test_auto_convert import (  # noqa: F401
    _fresh_cache,
    _mixed_selection,
    _route_env,
    _volume,
)


@pytest.mark.asyncio
async def test_an_ordinary_failure_still_returns_the_kept_jobs(route_env, monkeypatch, caplog):
    paths = _mixed_selection(route_env["tmp_path"])

    async def fake_atomic(job_specs, mode, **_kwargs):
        if mode.value == "dolphin_rvz":
            raise RuntimeError("queue fell over")
        spec = job_specs[0]
        return [convert_routes.ConversionJob(
            id=f"job-{mode.value}", file_path=spec["file_path"],
            filename=Path(spec["file_path"]).name, output_path=spec["output_path"],
            mode=mode, status="completed", created_at="2026-11-14T00:00:00Z",
        )]

    async def fake_cancel(_job_id):
        return False   # already finished

    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)
    monkeypatch.setattr(convert_routes.job_manager, "cancel_job", fake_cancel)

    with pytest.raises(convert_routes.HTTPException) as exc:
        await convert_routes.create_auto_jobs(AutoJobCreateRequest(file_paths=paths))

    assert exc.value.status_code == 500
    assert {job["id"] for job in exc.value.detail["jobs"]} == {"job-createdvd"}
    assert "queue fell over" in caplog.text   # the original error is logged


@pytest.mark.asyncio
async def test_an_image_skipped_while_preparing_says_why_in_the_plan(route_env):
    tmp = route_env["tmp_path"]
    paths = _mixed_selection(tmp)
    # The output appears after planning: preparation skips it (duplicate: skip).
    (tmp / "pile" / "ps2.chd").write_bytes(b"\0")

    result = await convert_routes.create_auto_jobs(AutoJobCreateRequest(file_paths=paths))

    rows = {Path(p.file_path).name: p for p in result.plan}
    assert rows["ps2.iso"].mode is None
    assert rows["ps2.iso"].skip_reason
    assert all("ps2.iso" not in q["files"] for q in route_env["queued"])


# --- Rollback waits for running jobs (Codex 4576827979) ----------------------
# cancel_job() returns True for a processing job once it has only set the
# job's cancel event. A worker past its last cancellation check (paused in
# post_convert, say) still completes with its output. The rollback must wait
# for the real final state and retain such a job, never read "cancel
# requested" as "withdrawn".


async def _job_paused_in_post_convert(e2e_env, monkeypatch, name="rom.iso"):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_post_convert(input_path, output_path, mode, **_kwargs):
        entered.set()
        await release.wait()

    for tool in convert_routes.registry.all():
        monkeypatch.setattr(tool, "post_convert", paused_post_convert)
    source = e2e_env["tmp_path"] / name
    source.write_bytes(b"PAYLOAD")
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
    ))
    worker = asyncio.create_task(convert_routes.job_manager._process_job(job.id))
    await asyncio.wait_for(entered.wait(), 5)
    return job, worker, release


@pytest.mark.asyncio
async def test_a_job_that_finishes_during_rollback_is_retained(e2e_env, monkeypatch):
    job, worker, release = await _job_paused_in_post_convert(e2e_env, monkeypatch)

    rollback = asyncio.create_task(convert_routes._cancel_queued([job]))
    await asyncio.sleep(0.2)
    assert not rollback.done(), "rollback must wait for the running job"
    release.set()
    kept = await asyncio.wait_for(rollback, 5)
    await asyncio.wait_for(worker, 5)

    final = convert_routes.job_manager.get_job(job.id)
    assert final.status == JobStatus.COMPLETED
    assert Path(final.output_path).exists()
    assert [k.id for k in kept] == [job.id]


@pytest.mark.asyncio
async def test_a_job_still_running_when_the_wait_ends_is_retained(e2e_env, monkeypatch):
    monkeypatch.setattr(convert_routes.settings, "progress_timeout", 1)
    job, worker, release = await _job_paused_in_post_convert(e2e_env, monkeypatch)

    try:
        kept = await asyncio.wait_for(convert_routes._cancel_queued([job]), 5)
    finally:
        release.set()
        await asyncio.wait_for(worker, 5)

    # Not finished, not withdrawn: its output may still land, so the client
    # keeps it rather than retiring its records.
    assert [k.id for k in kept] == [job.id]


@pytest.mark.asyncio
async def test_an_unbounded_job_timeout_still_caps_the_wait_in_the_request(
    e2e_env, monkeypatch,
):
    # progress_timeout = 0 means no job bound, but the rollback runs inside the
    # Auto request: it waits at most the in-request cap, then keeps the job.
    monkeypatch.setattr(convert_routes.settings, "progress_timeout", 0)
    monkeypatch.setattr(convert_routes, "ROLLBACK_WAIT_CAP_SECONDS", 1.0)
    job, worker, release = await _job_paused_in_post_convert(e2e_env, monkeypatch)

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        kept = await asyncio.wait_for(convert_routes._cancel_queued([job]), 5)
    finally:
        elapsed = loop.time() - started
        release.set()
        await asyncio.wait_for(worker, 5)

    assert elapsed < 3, elapsed
    assert [k.id for k in kept] == [job.id]


def test_the_in_request_cap_is_thirty_seconds():
    from services import job_manager as job_manager_module

    assert job_manager_module.ROLLBACK_WAIT_CAP_SECONDS == 30
    assert convert_routes.ROLLBACK_WAIT_CAP_SECONDS == 30
