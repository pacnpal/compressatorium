"""Parity tests for the registry-driven verify route factory (Phase 6).

These lock the wire contract that ``register_verify_routes`` regenerates:
the exact URL paths/methods, the SSE event names + JSON payloads (including
the 2-second heartbeat), the ``verification_store.mark_verified`` side-effect,
and the sync 429 / SSE ``verify_error`` backpressure behavior.
"""
import asyncio
import json
import re
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import info as info_routes

# (path, method, route_name) for every verify endpoint the factory generates.
_EXPECTED_VERIFY_ROUTES = {
    ("/verify", "GET", "verify_chd"),
    ("/verify/events", "GET", "verify_chd_events"),
    ("/verify-batch/events", "POST", "verify_batch_events"),
    ("/dolphin-verify", "GET", "verify_dolphin"),
    ("/dolphin-verify/events", "GET", "verify_dolphin_events"),
    ("/dolphin-verify-batch/events", "POST", "verify_dolphin_batch_events"),
    ("/z3ds-verify", "GET", "verify_z3ds"),
    ("/z3ds-verify/events", "GET", "verify_z3ds_events"),
    ("/z3ds-verify-batch/events", "POST", "verify_z3ds_batch_events"),
}

# tool id -> (verify extension, service module-global, function-name stems)
_TOOLS = {
    "chdman": {
        "ext": ".chd",
        "service_attr": "chdman_service",
        "sync": "verify_chd",
        "events": "verify_chd_events",
        "batch": "verify_batch_events",
    },
    "dolphin": {
        "ext": ".iso",
        "service_attr": "dolphin_tool_service",
        "sync": "verify_dolphin",
        "events": "verify_dolphin_events",
        "batch": "verify_dolphin_batch_events",
    },
    "z3ds": {
        "ext": ".z3ds",
        "service_attr": "z3ds_compress_service",
        "sync": "verify_z3ds",
        "events": "verify_z3ds_events",
        "batch": "verify_z3ds_batch_events",
    },
}


def test_verify_route_table_is_byte_identical():
    """Every verify URL path, HTTP method, and route name must be present."""
    actual = set()
    for route in info_routes.router.routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            actual.add((route.path, method, route.name))
    missing = _EXPECTED_VERIFY_ROUTES - actual
    assert not missing, f"missing verify routes: {missing}"


@pytest.fixture
def verify_env(tmp_path, monkeypatch):
    """Allow tmp_path as a configured volume and stub the verification store."""
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    store = Mock()
    store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", store)
    return {"tmp_path": tmp_path, "store": store}


def _make_file(tmp_path, ext):
    target = tmp_path / f"sample{ext}"
    target.write_text("payload")
    return str(target)


def _install_stream(monkeypatch, service_attr, updates):
    """Bind a mock service whose verify_stream yields a scripted sequence."""
    service = Mock()

    async def fake_verify_stream(path):
        for update in updates:
            yield update

    async def fake_verify(path):
        return {"valid": True, "message": "verified successfully"}

    service.verify_stream = fake_verify_stream
    service.verify = fake_verify
    monkeypatch.setattr(info_routes, service_attr, service)
    return service


async def _collect(response):
    events = []
    async for event in response.body_iterator:
        if isinstance(event, dict):
            events.append(event)
    return events


@pytest.mark.parametrize("tool_id", list(_TOOLS))
@pytest.mark.asyncio
async def test_sse_event_shape_snapshot(tool_id, verify_env, monkeypatch):
    """The single-file SSE stream emits the exact pre-refactor event sequence."""
    spec = _TOOLS[tool_id]
    path = _make_file(verify_env["tmp_path"], spec["ext"])
    updates = [
        {"type": "progress", "progress": 42, "message": "working"},
        {"type": "complete", "valid": True, "message": "done"},
    ]
    _install_stream(monkeypatch, spec["service_attr"], updates)

    events_fn = getattr(info_routes, spec["events"])
    events = await _collect(await events_fn(path=path))

    assert events == [
        {"event": "verify_progress", "data": json.dumps(updates[0])},
        {"event": "verify_complete", "data": json.dumps(updates[1])},
    ]
    verify_env["store"].mark_verified.assert_called_once_with(path)


@pytest.mark.parametrize("tool_id", list(_TOOLS))
@pytest.mark.asyncio
async def test_batch_sse_event_shape_snapshot(tool_id, verify_env, monkeypatch):
    """The batch SSE stream emits the exact pre-refactor event sequence."""
    from models import BulkVerifyRequest

    spec = _TOOLS[tool_id]
    path = _make_file(verify_env["tmp_path"], spec["ext"])
    updates = [
        {"type": "progress", "progress": 42, "message": "working"},
        {"type": "complete", "valid": True, "message": "done"},
    ]
    _install_stream(monkeypatch, spec["service_attr"], updates)

    batch_fn = getattr(info_routes, spec["batch"])
    response = await batch_fn(BulkVerifyRequest(paths=[path]))
    events = await _collect(response)

    filename = path.rsplit("/", 1)[-1]
    assert events == [
        {"event": "verify_batch_start",
         "data": json.dumps({"total": 1, "paths": [path]})},
        {"event": "verify_batch_progress", "data": json.dumps({
            "index": 0, "total": 1, "path": path, "filename": filename,
            "status": "verifying", "verified": 0, "failed": 0})},
        {"event": "verify_batch_file_progress", "data": json.dumps({
            "index": 0, "path": path, "filename": filename,
            "progress": 42, "message": "working"})},
        {"event": "verify_batch_file_complete", "data": json.dumps({
            "index": 0, "path": path, "filename": filename, "status": "verified",
            "valid": True, "message": "done", "verified": 1, "failed": 0})},
        {"event": "verify_batch_complete", "data": json.dumps({
            "total": 1, "verified": 1, "failed": 0})},
    ]
    verify_env["store"].mark_verified.assert_called_once_with(path)


@pytest.mark.parametrize("tool_id", list(_TOOLS))
@pytest.mark.asyncio
async def test_sse_heartbeat_shape(tool_id, verify_env, monkeypatch):
    """A stalled stream emits the 2-second heartbeat with a null progress."""
    spec = _TOOLS[tool_id]
    path = _make_file(verify_env["tmp_path"], spec["ext"])
    service = Mock()

    async def stalled_stream(path):
        await asyncio.sleep(5)
        yield {"type": "complete", "valid": True, "message": "done"}

    service.verify_stream = stalled_stream
    monkeypatch.setattr(info_routes, spec["service_attr"], service)

    events_fn = getattr(info_routes, spec["events"])
    response = await events_fn(path=path)

    heartbeat = None
    async for event in response.body_iterator:
        if isinstance(event, dict) and event.get("event") == "verify_progress":
            heartbeat = event
            break
    await response.body_iterator.aclose()

    assert heartbeat is not None
    payload = json.loads(heartbeat["data"])
    assert payload["progress"] is None
    assert re.fullmatch(r"Verifying\.\.\. \(\d+s\)", payload["message"])


@pytest.mark.parametrize("tool_id", list(_TOOLS))
@pytest.mark.asyncio
async def test_sync_verify_returns_429_when_lane_saturated(
    tool_id, verify_env, monkeypatch,
):
    """The sync verify endpoint fails fast with 429 when the lane is full."""
    from fastapi import HTTPException

    spec = _TOOLS[tool_id]
    path = _make_file(verify_env["tmp_path"], spec["ext"])
    _install_stream(monkeypatch, spec["service_attr"], [])
    monkeypatch.setattr(
        info_routes.workload_limiter, "try_acquire", AsyncMock(return_value=None),
    )

    sync_fn = getattr(info_routes, spec["sync"])
    with pytest.raises(HTTPException) as exc_info:
        await sync_fn(path=path)
    assert exc_info.value.status_code == 429
    assert "capacity" in exc_info.value.detail.lower()


@pytest.mark.parametrize("tool_id", list(_TOOLS))
@pytest.mark.asyncio
async def test_sse_emits_verify_error_when_lane_saturated(
    tool_id, verify_env, monkeypatch,
):
    """The SSE stream emits verify_error (not 429) when the lane is full."""
    spec = _TOOLS[tool_id]
    path = _make_file(verify_env["tmp_path"], spec["ext"])
    _install_stream(monkeypatch, spec["service_attr"], [])
    monkeypatch.setattr(
        info_routes.workload_limiter, "try_acquire", AsyncMock(return_value=None),
    )

    events_fn = getattr(info_routes, spec["events"])
    events = await _collect(await events_fn(path=path))

    assert len(events) == 1
    assert events[0]["event"] == "verify_error"
    payload = json.loads(events[0]["data"])
    assert payload == {
        "type": "error",
        "valid": False,
        "message": info_routes._verification_backpressure_detail(),
    }
