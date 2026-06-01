"""Tests for Switch (nsz) info and verification routes."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import info as info_routes


@pytest.fixture
def nsz_test_env(tmp_path, monkeypatch):
    """Fake Switch source/output files inside the configured volume."""
    source_path = tmp_path / "game.nsp"
    source_path.write_text("fake source")

    compressed_path = tmp_path / "game.nsz"
    compressed_path.write_text("fake compressed")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    return {
        "source_path": str(source_path),
        "compressed_path": str(compressed_path),
        "tmp_path": tmp_path,
    }


@pytest.fixture
def mock_nsz_service(monkeypatch):
    mock_service = Mock()

    def fake_info(path):
        return {
            "file": path,
            "size": 1024,
            "size_display": "1.00 KB",
            "format": "NSZ (compressed NSP)",
            "extension": ".nsz",
            "compressed": True,
            "compression_type": "NSZ (zstandard)",
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

    monkeypatch.setattr(info_routes, "nsz_service", mock_service)
    return mock_service


@pytest.fixture
def mock_verification_store(monkeypatch):
    mock_store = Mock()
    mock_store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", mock_store)
    return mock_store


@pytest.mark.asyncio
async def test_nsz_verify_happy_path(nsz_test_env, mock_nsz_service, mock_verification_store):
    result = await info_routes.verify_nsz(path=nsz_test_env["compressed_path"])

    assert result["valid"] is True
    assert "verified" in result["message"].lower()
    mock_verification_store.mark_verified.assert_called_once_with(
        nsz_test_env["compressed_path"]
    )


@pytest.mark.asyncio
async def test_nsz_verify_rejects_source_extension(nsz_test_env, mock_nsz_service):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_nsz(path=nsz_test_env["source_path"])

    assert exc_info.value.status_code == 400
    assert "switch" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_nsz_verify_stream_happy_path(
    nsz_test_env, mock_nsz_service, mock_verification_store,
):
    response = await info_routes.verify_nsz_events(path=nsz_test_env["compressed_path"])

    events = []
    async for event in response.body_iterator:
        events.append(event)

    assert any(e.get("event") == "verify_progress" for e in events if isinstance(e, dict))
    assert any(e.get("event") == "verify_complete" for e in events if isinstance(e, dict))
    mock_verification_store.mark_verified.assert_called_once_with(
        nsz_test_env["compressed_path"]
    )


@pytest.mark.asyncio
async def test_nsz_info_accepts_compressed_output(nsz_test_env, mock_nsz_service):
    result = await info_routes.get_nsz_info(path=nsz_test_env["compressed_path"])
    assert result.file == nsz_test_env["compressed_path"]
    assert result.compressed is True


@pytest.mark.asyncio
async def test_nsz_info_rejects_unknown_extension(nsz_test_env, mock_nsz_service):
    from fastapi import HTTPException

    other = nsz_test_env["tmp_path"] / "thing.iso"
    other.write_text("x")
    with pytest.raises(HTTPException) as exc_info:
        await info_routes.get_nsz_info(path=str(other))
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_list_tools_hides_switch_without_keys(monkeypatch):
    monkeypatch.setattr(info_routes.nsz_service, "keys_available", lambda: False)
    result = await info_routes.list_tools()
    assert "nsz" in result["unavailable"]
    assert "nsz" not in result["available"]
    assert "chdman" in result["available"]


@pytest.mark.asyncio
async def test_list_tools_shows_switch_with_keys(monkeypatch):
    monkeypatch.setattr(info_routes.nsz_service, "keys_available", lambda: True)
    result = await info_routes.list_tools()
    assert "nsz" in result["available"]
    assert "nsz" not in result["unavailable"]
