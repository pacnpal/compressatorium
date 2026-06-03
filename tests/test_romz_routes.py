"""Tests for the romz (handheld ROM) info and verification routes."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import info as info_routes


@pytest.fixture
def romz_test_env(tmp_path, monkeypatch):
    """Fake ROM source + .7z archive files inside a configured volume."""
    source_path = tmp_path / "Game.gba"
    source_path.write_text("fake rom")

    archive_path = tmp_path / "Game.gba.7z"
    archive_path.write_text("fake archive")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    return {
        "source_path": str(source_path),
        "archive_path": str(archive_path),
        "tmp_path": tmp_path,
    }


@pytest.fixture
def mock_romz_service(monkeypatch):
    mock_service = Mock()

    def fake_info(path):
        compressed = path.lower().endswith((".7z", ".zip"))
        return {
            "file": path,
            "size": 512,
            "size_display": "0.50 KB",
            "format": "7-Zip archive" if compressed else "Game Boy Advance ROM",
            "extension": ".7z" if compressed else ".gba",
            "compressed": compressed,
            "compression_type": "7-Zip (LZMA2)" if compressed else None,
            "contained_name": "Game.gba" if compressed else None,
            "original_size": 1024 if compressed else None,
            "ratio": "50.0%" if compressed else None,
        }

    async def fake_verify(path):
        return {"valid": True, "message": "File verified successfully"}

    async def fake_verify_stream(path):
        yield {"type": "progress", "progress": 0, "message": "Verifying integrity..."}
        await asyncio.sleep(0.01)
        yield {"type": "complete", "valid": True, "message": "File verified successfully"}

    mock_service.info = fake_info
    mock_service.verify = fake_verify
    mock_service.verify_stream = fake_verify_stream

    monkeypatch.setattr(info_routes, "romz_service", mock_service)
    return mock_service


@pytest.fixture
def mock_verification_store(monkeypatch):
    mock_store = Mock()
    mock_store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", mock_store)
    return mock_store


@pytest.mark.asyncio
async def test_romz_verify_happy_path(
    romz_test_env, mock_romz_service, mock_verification_store,
):
    result = await info_routes.verify_romz(path=romz_test_env["archive_path"])
    assert result["valid"] is True
    mock_verification_store.mark_verified.assert_called_once_with(
        romz_test_env["archive_path"],
    )


@pytest.mark.asyncio
async def test_romz_verify_rejects_loose_rom(romz_test_env, mock_romz_service):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.verify_romz(path=romz_test_env["source_path"])
    assert exc_info.value.status_code == 400
    assert ".7z" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_romz_verify_stream_happy_path(
    romz_test_env, mock_romz_service, mock_verification_store,
):
    response = await info_routes.verify_romz_events(path=romz_test_env["archive_path"])
    events = [e async for e in response.body_iterator]
    assert any(e.get("event") == "verify_progress" for e in events if isinstance(e, dict))
    assert any(e.get("event") == "verify_complete" for e in events if isinstance(e, dict))
    mock_verification_store.mark_verified.assert_called_once_with(
        romz_test_env["archive_path"],
    )


@pytest.mark.asyncio
async def test_romz_info_archive_reports_ratio(romz_test_env, mock_romz_service):
    result = await info_routes.get_romz_info(path=romz_test_env["archive_path"])
    assert result.file == romz_test_env["archive_path"]
    assert result.compressed is True
    assert result.contained_name == "Game.gba"
    assert result.ratio == "50.0%"


@pytest.mark.asyncio
async def test_romz_info_accepts_loose_rom(romz_test_env, mock_romz_service):
    result = await info_routes.get_romz_info(path=romz_test_env["source_path"])
    assert result.compressed is False
    assert result.format == "Game Boy Advance ROM"


@pytest.mark.asyncio
async def test_romz_info_rejects_unrelated_extension(romz_test_env, mock_romz_service):
    from fastapi import HTTPException

    other = romz_test_env["tmp_path"] / "notes.txt"
    other.write_text("x")
    with pytest.raises(HTTPException) as exc_info:
        await info_routes.get_romz_info(path=str(other))
    assert exc_info.value.status_code == 400
