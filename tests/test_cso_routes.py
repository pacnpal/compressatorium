"""Tests for CSO info and verification routes."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import info as info_routes


@pytest.fixture
def cso_test_env(tmp_path, monkeypatch):
    """Set up test environment with fake ISO source / CSO output files."""
    source_path = tmp_path / "game.iso"
    source_path.write_text("fake source")

    compressed_path = tmp_path / "game.cso"
    compressed_path.write_text("fake compressed")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    return {
        "source_path": str(source_path),
        "compressed_path": str(compressed_path),
        "tmp_path": tmp_path,
    }


@pytest.fixture
def mock_maxcso_service(monkeypatch):
    """Mock the maxcso_service for testing."""
    mock_service = Mock()

    def fake_info(path):
        return {
            "file": path,
            "size": 1024,
            "size_display": "1.00 KB",
            "format": "CSO (compressed ISO)",
            "extension": ".cso",
            "compressed": True,
            "compression_type": "CSO (deflate)",
        }

    async def fake_verify(path):
        return {"valid": True, "message": "File verified successfully"}

    async def fake_verify_stream(path):
        yield {"type": "progress", "progress": 50, "message": "Verifying integrity..."}
        await asyncio.sleep(0.01)
        yield {"type": "complete", "valid": True, "message": "File verified successfully"}

    mock_service.info = fake_info
    mock_service.verify = fake_verify
    mock_service.verify_stream = fake_verify_stream

    monkeypatch.setattr(info_routes, "maxcso_service", mock_service)
    return mock_service


@pytest.fixture
def mock_verification_store(monkeypatch):
    """Mock the verification store used for persisted verify state."""
    mock_store = Mock()
    mock_store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", mock_store)
    return mock_store


@pytest.mark.asyncio
async def test_cso_verify_happy_path(
    cso_test_env,
    mock_maxcso_service,
    mock_verification_store,
):
    """Verify compressed CSO output successfully."""
    result = await info_routes.verify_cso(path=cso_test_env["compressed_path"])

    assert result["valid"] is True
    assert "verified" in result["message"].lower()
    mock_verification_store.mark_verified.assert_called_once_with(
        cso_test_env["compressed_path"]
    )


@pytest.mark.asyncio
async def test_cso_verify_rejects_source_extension(cso_test_env, mock_maxcso_service):
    """Reject verification requests for uncompressed ISO source files."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_cso(path=cso_test_env["source_path"])

    assert exc_info.value.status_code == 400
    assert "cso" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_cso_verify_stream_happy_path(
    cso_test_env,
    mock_maxcso_service,
    mock_verification_store,
):
    """Stream CSO verification progress and completion events."""
    response = await info_routes.verify_cso_events(path=cso_test_env["compressed_path"])

    events = []
    async for event in response.body_iterator:
        events.append(event)

    assert len(events) >= 2
    assert any(e.get("event") == "verify_progress" for e in events if isinstance(e, dict))
    assert any(e.get("event") == "verify_complete" for e in events if isinstance(e, dict))
    mock_verification_store.mark_verified.assert_called_once_with(
        cso_test_env["compressed_path"]
    )


@pytest.mark.asyncio
async def test_cso_verify_stream_returns_verify_error_when_lane_is_saturated(
    cso_test_env,
    mock_maxcso_service,
    mock_verification_store,
    monkeypatch,
):
    """Streaming verify should emit verify_error when the verify lane is saturated."""
    monkeypatch.setattr(
        info_routes.workload_limiter,
        "try_acquire",
        AsyncMock(return_value=None),
    )

    response = await info_routes.verify_cso_events(path=cso_test_env["compressed_path"])

    events = []
    async for event in response.body_iterator:
        events.append(event)

    assert any(e.get("event") == "verify_error" for e in events if isinstance(e, dict))
    payloads = [
        e.get("data")
        for e in events
        if isinstance(e, dict) and e.get("event") == "verify_error"
    ]
    assert payloads
    assert "capacity" in payloads[0].lower()


@pytest.mark.asyncio
async def test_cso_info_accepts_compressed_output(cso_test_env, mock_maxcso_service):
    """cso-info accepts compressed output extensions such as .cso."""
    result = await info_routes.get_cso_info(path=cso_test_env["compressed_path"])

    assert result.file == cso_test_env["compressed_path"]
    assert result.compressed is True


@pytest.mark.asyncio
async def test_cso_info_accepts_iso_source(cso_test_env, mock_maxcso_service, monkeypatch):
    """cso-info accepts a plain .iso source too (it is a convertible input)."""
    def fake_info(path):
        return {
            "file": path,
            "size": 2048,
            "size_display": "2.00 KB",
            "format": "ISO (disc image)",
            "extension": ".iso",
            "compressed": False,
            "compression_type": None,
        }

    monkeypatch.setattr(mock_maxcso_service, "info", fake_info)
    result = await info_routes.get_cso_info(path=cso_test_env["source_path"])

    assert result.file == cso_test_env["source_path"]
    assert result.compressed is False
