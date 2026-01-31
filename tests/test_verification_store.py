"""Tests for verification store persistence and concurrency behavior."""

# Ruff: allow `assert` in test code.
# ruff: noqa: S101

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.services.verification_store import VerificationStore


@pytest.fixture(name="store_path")
def _store_path(tmp_path: Path) -> Path:
    """Provide the filesystem path for the verification store fixture."""
    return tmp_path / "verified_chds.json"


@pytest.fixture(name="verification_store")
def _verification_store(store_path: Path) -> VerificationStore:
    """Create a verification store bound to the temporary fixture path."""
    # Initialize store with the temp path.
    # We cheat and inject it since `VerificationStore.__init__` uses env vars.
    return VerificationStore(str(store_path))

@pytest.mark.asyncio
async def test_async_mark_verified(
    verification_store: VerificationStore,
    store_path: Path,
    tmp_path: Path,
) -> None:
    """Verify that marking a CHD as verified persists and resolves real paths."""
    # Use tmp_path for portable test structure
    path = str(tmp_path / "test.chd")
    real_path = os.path.realpath(path)
    await verification_store.mark_verified(path)

    # Store uses realpath internally, methods are now async
    assert await verification_store.is_verified(path)
    assert await verification_store.is_verified(real_path)

    assert store_path.exists()

    store_json = await asyncio.to_thread(store_path.read_text, encoding="utf-8")
    data = json.loads(store_json)
    assert data[real_path]["chd_path"] == real_path

@pytest.mark.asyncio
async def test_concurrent_writes(
    verification_store: VerificationStore,
    store_path: Path,
    tmp_path: Path,
) -> None:
    """Test safe concurrent updates."""
    count = 50
    # Create valid paths in tmp_path
    paths = [str(tmp_path / f"file_{i}.chd") for i in range(count)]
    real_paths = [os.path.realpath(p) for p in paths]

    async def verify_one(p: str) -> None:
        await verification_store.mark_verified(p)

    await asyncio.gather(*[verify_one(p) for p in paths])

    # Check memory state
    assert len(verification_store.all_records()) == count

    # Check disk state
    store_json = await asyncio.to_thread(store_path.read_text, encoding="utf-8")
    data = json.loads(store_json)
    assert len(data) == count
    for p in real_paths:
        assert p in data

@pytest.mark.asyncio
async def test_race_condition_persistence(
    verification_store: VerificationStore,
    store_path: Path,
    tmp_path: Path,
) -> None:
    """Simulate a race condition: state changes while another write is in-flight."""
    path_a = str(tmp_path / "a.chd")
    path_b = str(tmp_path / "b.chd")
    real_a = os.path.realpath(path_a)
    real_b = os.path.realpath(path_b)

    # Helper to constantly update B while A is being written
    async def spam_b() -> None:
        for _ in range(20):
            await verification_store.mark_verified(path_b)
            await asyncio.sleep(0.001)

    # Trigger A then B concurrently
    await asyncio.gather(
        verification_store.mark_verified(path_a),
        spam_b(),
    )

    assert await verification_store.is_verified(path_a)
    assert await verification_store.is_verified(path_b)

    store_json = await asyncio.to_thread(store_path.read_text, encoding="utf-8")
    data = json.loads(store_json)
    assert real_a in data
    assert real_b in data
