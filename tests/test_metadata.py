import pytest

from app.config import settings
from app.routes import info as info_routes
from app.services.chd_metadata_store import CHDMetadataStore


@pytest.fixture
def media_type_cases():
    return [
        ({"raw_data": "Metadata: CHCD, Tag: CD-ROM"}, "cd"),
        ({"raw_data": "Tag: DVD-VIDEO"}, "dvd"),
        ({"raw_data": "Tag: GD-ROM"}, "cd"),
        ({"metadata_lines": ["CHCD"]}, "cd"),
    ]


def test_extract_media_type_cases(media_type_cases):
    for info, expected in media_type_cases:
        assert CHDMetadataStore.extract_media_type(info) == expected


@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    chd_path = tmp_path / "game.chd"
    chd_path.write_text("test")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))

    calls = []

    async def fake_info(path):
        calls.append(path)
        return {"raw_data": "Tag: CD-ROM"}

    async def fake_set_metadata(path, info, persist=False):
        return {"media_type": "cd"}

    async def fake_flush_async():
        return None

    monkeypatch.setattr(info_routes.chdman_service, "info", fake_info)
    monkeypatch.setattr(info_routes.chd_metadata_store, "set_metadata", fake_set_metadata)
    monkeypatch.setattr(info_routes.chd_metadata_store, "flush_async", fake_flush_async)

    return {"chd_path": str(chd_path), "calls": calls}


@pytest.mark.asyncio
async def test_scan_metadata_force_ignores_cache(scan_env, monkeypatch):
    async def fake_false(_): return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    await info_routes.scan_metadata_task(force=True)

    assert set(scan_env["calls"]) == {scan_env["chd_path"]}


@pytest.mark.asyncio
async def test_scan_metadata_respects_cache(scan_env, monkeypatch):
    async def fake_false(_): return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    await info_routes.scan_metadata_task(force=False)

    assert scan_env["calls"] == []


import asyncio
import json
import os


@pytest.fixture
def metadata_store_path(tmp_path):
    return tmp_path / "chd_metadata.json"


@pytest.fixture
def metadata_store(metadata_store_path):
    return CHDMetadataStore(str(metadata_store_path))


@pytest.mark.asyncio
async def test_concurrent_metadata_writes(metadata_store, metadata_store_path, tmp_path):
    """
    Test that concurrent writes don't lose data (last-write-wins).
    Simulates the race condition where multiple set_metadata calls
    happen concurrently.
    """
    count = 50
    paths = [str(tmp_path / f"game_{i}.chd") for i in range(count)]
    # Create fake files so realpath works
    for p in paths:
        open(p, 'w').close()
    
    async def set_one(p):
        await metadata_store.set_metadata(p, {"raw_data": f"Tag: CD-ROM for {p}"}, persist=True)
    
    await asyncio.gather(*[set_one(p) for p in paths])
    
    # Verify all records are in memory
    records = metadata_store.all_records()
    assert len(records) == count
    
    # Verify disk state is consistent
    with open(metadata_store_path, "r") as f:
        data = json.load(f)
        assert len(data) == count


@pytest.mark.asyncio
async def test_metadata_persist_version_gate(metadata_store, metadata_store_path, tmp_path):
    """
    Test that version-gated replace prevents stale overwrites.
    """
    path_a = str(tmp_path / "a.chd")
    path_b = str(tmp_path / "b.chd")
    open(path_a, 'w').close()
    open(path_b, 'w').close()
    
    # Set A
    await metadata_store.set_metadata(path_a, {"raw_data": "A"}, persist=True)
    
    # Spam B while A might still be writing
    async def spam_b():
        for _ in range(20):
            await metadata_store.set_metadata(path_b, {"raw_data": "B"}, persist=True)
            await asyncio.sleep(0.001)
    
    await asyncio.gather(
        metadata_store.set_metadata(path_a, {"raw_data": "A2"}, persist=True),
        spam_b()
    )
    
    # Both should be present
    real_a = os.path.realpath(path_a)
    real_b = os.path.realpath(path_b)
    
    with open(metadata_store_path, "r") as f:
        data = json.load(f)
        assert real_a in data
        assert real_b in data
