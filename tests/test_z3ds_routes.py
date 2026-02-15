"""Tests for 3DS info and verification routes."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import info as info_routes


@pytest.fixture
def z3ds_test_env(tmp_path, monkeypatch):
    """Set up test environment with fake 3DS source/output files."""
    source_path = tmp_path / "game.3ds"
    source_path.write_text("fake source")

    compressed_path = tmp_path / "game.z3ds"
    compressed_path.write_text("fake compressed")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    return {
        "source_path": str(source_path),
        "compressed_path": str(compressed_path),
        "tmp_path": tmp_path,
    }


@pytest.fixture
def mock_z3ds_service(monkeypatch):
    """Mock the z3ds_compress_service for testing."""
    mock_service = Mock()

    def fake_info(path):
        return {
            "file": path,
            "size": 1024,
            "size_display": "1.00 KB",
            "format": "3DS (Cart Image)",
            "extension": ".z3ds",
            "compressed": True,
            "compression_type": "zstd",
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

    monkeypatch.setattr(info_routes, "z3ds_compress_service", mock_service)
    return mock_service


@pytest.fixture
def mock_verification_store(monkeypatch):
    """Mock the verification store used for persisted verify state."""
    mock_store = Mock()
    mock_store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", mock_store)
    return mock_store


@pytest.mark.asyncio
async def test_z3ds_verify_happy_path(
    z3ds_test_env,
    mock_z3ds_service,
    mock_verification_store,
):
    """Verify compressed 3DS output successfully."""
    result = await info_routes.verify_z3ds(path=z3ds_test_env["compressed_path"])

    assert result["valid"] is True
    assert "verified" in result["message"].lower()
    mock_verification_store.mark_verified.assert_called_once_with(
        z3ds_test_env["compressed_path"]
    )


@pytest.mark.asyncio
async def test_z3ds_verify_rejects_source_extension(z3ds_test_env, mock_z3ds_service):
    """Reject verification requests for uncompressed 3DS source files."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_z3ds(path=z3ds_test_env["source_path"])

    assert exc_info.value.status_code == 400
    assert "compressed 3ds format" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_z3ds_verify_stream_happy_path(
    z3ds_test_env,
    mock_z3ds_service,
    mock_verification_store,
):
    """Stream 3DS verification progress and completion events."""
    response = await info_routes.verify_z3ds_events(path=z3ds_test_env["compressed_path"])

    events = []
    async for event in response.body_iterator:
        events.append(event)

    assert len(events) >= 2
    assert any(e.get("event") == "verify_progress" for e in events if isinstance(e, dict))
    assert any(e.get("event") == "verify_complete" for e in events if isinstance(e, dict))
    mock_verification_store.mark_verified.assert_called_once_with(
        z3ds_test_env["compressed_path"]
    )


@pytest.mark.asyncio
async def test_z3ds_verify_stream_returns_verify_error_when_lane_is_saturated(
    z3ds_test_env,
    mock_z3ds_service,
    mock_verification_store,
    monkeypatch,
):
    """Streaming verify should emit verify_error when verify lane is saturated."""
    monkeypatch.setattr(
        info_routes.workload_limiter,
        "try_acquire",
        AsyncMock(return_value=None),
    )

    response = await info_routes.verify_z3ds_events(path=z3ds_test_env["compressed_path"])

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
async def test_z3ds_info_accepts_compressed_output(z3ds_test_env, mock_z3ds_service):
    """z3ds-info accepts compressed output extensions such as .z3ds."""
    result = await info_routes.get_z3ds_info(path=z3ds_test_env["compressed_path"])

    assert result.file == z3ds_test_env["compressed_path"]
    assert result.compressed is True
