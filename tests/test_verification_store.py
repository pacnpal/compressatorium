import asyncio
import json
import os
import threading
import pytest
from app.services.verification_store import VerificationStore

@pytest.fixture
def test_store_path(tmp_path):
    return tmp_path / "verified_chds.json"

@pytest.fixture
def store(test_store_path):
    # Initialize store with the temp path
    # We cheat and inject it since __init__ uses env vars
    s = VerificationStore(str(test_store_path))
    return s

@pytest.mark.asyncio
async def test_async_mark_verified(store, test_store_path, tmp_path):
    # Use tmp_path for portable test structure
    path = str(tmp_path / "test.chd")
    real_path = os.path.realpath(path)
    await store.mark_verified(path)
    
    # Store uses realpath internally, methods are now async
    assert await store.is_verified(path)
    assert await store.is_verified(real_path)
    
    assert test_store_path.exists()
    
    with open(test_store_path, "r") as f:
        data = json.load(f)
        assert data[real_path]["chd_path"] == real_path

@pytest.mark.asyncio
async def test_concurrent_writes(store, test_store_path, tmp_path):
    """
    Test safe concurrent updates. 
    """
    count = 50
    # Create valid paths in tmp_path
    paths = [str(tmp_path / f"file_{i}.chd") for i in range(count)]
    real_paths = [os.path.realpath(p) for p in paths]
    
    async def verify_one(p):
        await store.mark_verified(p)
        
    await asyncio.gather(*[verify_one(p) for p in paths])
    
    # Check memory state
    assert len(store.all_records()) == count
    
    # Check disk state
    with open(test_store_path, "r") as f:
        data = json.load(f)
        assert len(data) == count
        for p in real_paths:
            assert p in data

@pytest.mark.asyncio
async def test_race_condition_persistence(store, test_store_path, tmp_path):
    """
    Simulate the race condition where a write starts, but state changes before it finishes.
    """
    path_a = str(tmp_path / "a.chd")
    path_b = str(tmp_path / "b.chd")
    real_a = os.path.realpath(path_a)
    real_b = os.path.realpath(path_b)
    
    # Helper to constantly update B while A is being written
    async def spam_b():
        for i in range(20):
            await store.mark_verified(path_b)
            await asyncio.sleep(0.001)

    # Trigger A then B concurrently
    await asyncio.gather(
        store.mark_verified(path_a),
        spam_b()
    )
    
    assert await store.is_verified(path_a)
    assert await store.is_verified(path_b)
    
    with open(test_store_path, "r") as f:
        data = json.load(f)
        assert real_a in data
        assert real_b in data
