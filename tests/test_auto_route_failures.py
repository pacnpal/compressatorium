"""Auto route: what the response says when part of a batch can't be queued."""
from __future__ import annotations

from pathlib import Path

import pytest
from app.models import AutoJobCreateRequest
from app.routes import convert as convert_routes

from .test_auto_convert import _fresh_cache, _mixed_selection, _route_env, _volume  # noqa: F401


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
