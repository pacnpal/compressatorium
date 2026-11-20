"""Planning a job for a tool that isn't ready refuses it before it is queued (#505).

``/api/tools`` hid a tool whose program was gone (#494), but ``POST /api/jobs``
still queued it and the job failed at run time with the spawn error. The check
is in ``plan_job``, through ``registry.tool_is_ready``: a single job answers 503
with ``not_ready_message(tool)``, a batch skips the item with that reason and
the code ``tool_not_ready``, and a chain mode is refused when any step tool is.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from services.tools import binaries, not_ready_message, registry


@pytest.fixture(autouse=True)
def _tmp_path_is_the_volume(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(settings, "max_queue_depth", 0)


@pytest.fixture(name="queued")
def _queued(monkeypatch):
    from routes import convert as convert_routes

    created: list = []

    async def fake_create_job(*args, **_kwargs):
        created.append(args)
        raise AssertionError("a job was queued")

    async def fake_atomic(job_specs, _mode, **_kwargs):
        created.append(job_specs)
        return []

    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 0)
    monkeypatch.setattr(convert_routes.job_manager, "create_job", fake_create_job)
    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)
    return created


def _chdman_is_gone(monkeypatch, folder) -> None:
    binaries.forget_binary_probes()
    monkeypatch.setattr(registry.get("chdman")._service, "chdman_path", str(folder / "gone"))


async def _post(url: str, body: dict, **params) -> httpx.Response:
    from routes.convert import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(url, json=body, params=params)


def _file(folder, name):
    path = folder / name
    path.write_bytes(b"\0" * 4096)
    return path


@pytest.mark.asyncio
async def test_a_single_job_for_a_tool_that_is_not_ready_is_503(tmp_path, monkeypatch, queued):
    source = _file(tmp_path, "Game.iso")
    _chdman_is_gone(monkeypatch, tmp_path)

    response = await _post("/api/jobs", {"file_path": str(source), "mode": "createdvd"})

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == not_ready_message(registry.get("chdman"))
    assert queued == []


@pytest.mark.asyncio
async def test_a_batch_skips_each_item_of_a_tool_that_is_not_ready(
    tmp_path, monkeypatch, queued,
):
    source = _file(tmp_path, "Game.iso")
    _chdman_is_gone(monkeypatch, tmp_path)

    response = await _post(
        "/api/jobs/batch",
        {"file_paths": [str(source)], "mode": "createdvd"},
        report_skipped="true",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["jobs"] == [], body
    [skip] = body["skipped"]
    assert skip["path"] == str(source)
    assert skip["reason"] == not_ready_message(registry.get("chdman"))
    assert skip["code"] == "tool_not_ready"


@pytest.mark.asyncio
async def test_a_tool_that_is_ready_is_planned_as_before(tmp_path, queued):
    source = _file(tmp_path, "Game.iso")

    response = await _post(
        "/api/jobs/batch",
        {"file_paths": [str(source)], "mode": "createdvd"},
        report_skipped="true",
    )

    assert response.status_code == 200, response.text
    assert response.json()["skipped"] == []
    [[spec]] = queued
    assert spec["file_path"] == str(source)


@pytest.mark.asyncio
async def test_a_chain_with_a_step_tool_that_is_not_ready_is_refused(
    tmp_path, monkeypatch, queued,
):
    source = _file(tmp_path, "Game.cso")
    _chdman_is_gone(monkeypatch, tmp_path)
    assert registry.for_mode("cso_to_chd").id == "chain"

    response = await _post("/api/jobs", {"file_path": str(source), "mode": "cso_to_chd"})

    assert response.status_code == 503, response.text
    assert "chdman" in response.json()["detail"]
    assert queued == []
