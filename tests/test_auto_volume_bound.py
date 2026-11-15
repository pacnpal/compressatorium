"""The Auto and batch routes never touch a Volume on the event loop.

Each selected path is checked against the Volumes (``is_within_configured_volumes``,
which stats and resolves). On the event loop, a mount that stops answering
froze every API request. The checks now run with planning, off the loop, on a
disposable thread (``run_detached``), bounded by ``VOLUME_PROBE_TIMEOUT_SECONDS``;
past the bound the request answers 503 and the stuck thread is abandoned.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from app.models import (
    AutoJobCreateRequest,
    AutoJobItem,
    AutoPlanRequest,
    BatchJobCreateRequest,
)
from app.routes import convert as convert_routes

from .test_auto_convert import _route_env  # noqa: F401

_BLOCK_SECONDS = 3.0


def _auto_plan(paths):
    return convert_routes.auto_plan(AutoPlanRequest(file_paths=paths))


def _auto_jobs(paths):
    return convert_routes.create_auto_jobs(AutoJobCreateRequest(
        items=[AutoJobItem(file_path=p, mode="createcd") for p in paths],
    ))


def _batch(paths):
    return convert_routes.create_batch_jobs(
        BatchJobCreateRequest(file_paths=paths, mode="createcd"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_auto_plan, _auto_jobs, _batch], ids=["plan", "auto", "batch"])
async def test_a_stuck_volume_check_leaves_the_loop_free_and_answers_503(
    route_env, monkeypatch, call,
):
    release = threading.Event()

    def stuck(_path, **_kwargs):
        release.wait(_BLOCK_SECONDS)   # a mount that stops answering
        return True

    monkeypatch.setattr(convert_routes, "is_within_configured_volumes", stuck)
    monkeypatch.setattr(convert_routes, "VOLUME_PROBE_TIMEOUT_SECONDS", 0.5)
    path = str(route_env["tmp_path"] / "game.iso")

    loop = asyncio.get_running_loop()
    try:
        request = asyncio.ensure_future(call([path]))
        await asyncio.sleep(0.05)              # the request is in its volume check
        started = loop.time()
        await convert_routes.list_jobs()       # a trivial request, concurrently
        served_in = loop.time() - started
        with pytest.raises(convert_routes.HTTPException) as exc:
            await asyncio.wait_for(request, 2)
        answered_in = loop.time() - started
    finally:
        release.set()

    assert served_in < 0.25, served_in        # the loop was not blocked
    assert exc.value.status_code == 503
    assert "Volume" in str(exc.value.detail)
    assert answered_in < 1.5, answered_in      # at the bound, not when the mount wakes


@pytest.mark.asyncio
async def test_a_path_outside_the_volumes_is_still_refused(route_env, tmp_path):
    with pytest.raises(convert_routes.HTTPException) as exc:
        await _auto_plan(["/definitely/not/a/volume/game.iso"])

    assert exc.value.status_code == 403


def test_the_volume_probe_timeout_is_thirty_seconds():
    from services import job_manager as job_manager_module

    assert job_manager_module.VOLUME_PROBE_TIMEOUT_SECONDS == 30
