"""Job creation refuses a tool that isn't ready (#505).

``/api/tools`` hid a tool whose program was missing (#494/#501), but
``POST /api/jobs`` and the batch route still queued a job for it, which then
failed at run time. The check lives in ``plan_job``, through
``registry.tool_is_ready``, so both routes (and Auto, which prepares through
``prepare_batch``) refuse with ``not_ready_message`` before anything queues.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from services.tools import not_ready_message, registry

from app.models import BatchJobCreateRequest, ConversionMode, JobCreateRequest
from app.routes import convert as convert_routes

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch):
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)

    async def refuse(*_args, **_kwargs):
        raise AssertionError("a job was queued for a tool that isn't ready")

    monkeypatch.setattr(convert_routes.job_manager, "create_job", refuse)
    monkeypatch.setattr(convert_routes.job_manager, "create_batch_jobs", refuse)
    return tmp_path


def _not_ready(monkeypatch, tool_id: str) -> list[str]:
    tool = registry.get(tool_id)
    probed: list[str] = []

    async def is_ready() -> bool:
        probed.append(tool_id)
        return False

    monkeypatch.setattr(tool, "is_ready", is_ready)
    monkeypatch.setattr(tool, "not_ready_reason", "chdman not found", raising=False)
    return probed


async def test_single_job_answers_503_with_the_reason(volume, monkeypatch):
    _not_ready(monkeypatch, "chdman")
    (volume / "Game.iso").write_bytes(b"\0" * 2048)

    with pytest.raises(HTTPException) as exc:
        await convert_routes.create_job(JobCreateRequest(
            file_path=str(volume / "Game.iso"), mode=ConversionMode.CREATEDVD,
        ))

    assert exc.value.status_code == 503
    assert exc.value.detail == not_ready_message(registry.get("chdman"))
    assert "chdman not found" in exc.value.detail


async def test_batch_skips_every_file_with_the_reason_and_probes_once(
    volume, monkeypatch,
):
    probed = _not_ready(monkeypatch, "chdman")
    paths = [volume / f"Game{i}.iso" for i in range(3)]
    for path in paths:
        path.write_bytes(b"")

    batch = await convert_routes.prepare_batch(BatchJobCreateRequest(
        file_paths=[str(p) for p in paths], mode=ConversionMode.CREATEDVD,
    ))

    assert batch.plans == []
    reason = not_ready_message(registry.get("chdman"))
    assert batch.skipped == dict.fromkeys(map(str, paths), reason)
    assert probed == ["chdman"]


async def test_a_chain_mode_is_refused_when_a_step_tool_isnt_ready(volume, monkeypatch):
    _not_ready(monkeypatch, "chdman")
    (volume / "Game.cso").write_bytes(b"\0" * 64)

    with pytest.raises(HTTPException) as exc:
        await convert_routes.create_job(JobCreateRequest(
            file_path=str(volume / "Game.cso"), mode=ConversionMode.CSO_TO_CHD,
        ))

    assert exc.value.status_code == 503
    assert "chdman not found" in exc.value.detail


async def test_a_ready_tool_plans_as_before(volume):
    # chdman is ready here (conftest), so the file's own problem answers.
    with pytest.raises(HTTPException) as exc:
        await convert_routes.create_job(JobCreateRequest(
            file_path=str(volume / "Missing.iso"), mode=ConversionMode.CREATEDVD,
        ))

    assert exc.value.status_code == 404
