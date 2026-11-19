"""Companion rows in the listing, and why a batch item was skipped (#504).

``GET /api/files`` marks a row that is a ``source_companions`` entry of another
Source in the same listing (a later RAR volume, a split Wii U dump's part) with
``companion_of``: that Source's path. The registry hook decides it, never an
extension, and a set whose members can't be told now gets no mark.

``POST /api/jobs/batch?report_skipped=true`` answers ``{jobs, skipped}``, each
skipped item with its ``path``, ``reason`` and ``code``. Without the flag the
answer is the bare job list it always was.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from .rar_fixtures import RAR4_SET, RAR5_SET, copy_set

SETS = pytest.mark.parametrize("names", [RAR5_SET, RAR4_SET], ids=["rar5-partN", "rar4-rNN"])


@pytest.fixture(autouse=True)
def _fresh_listing_cache():
    from services import archive_members

    archive_members.clear_cache()
    yield
    archive_members.clear_cache()


@pytest.fixture(autouse=True)
def _tmp_path_is_the_volume(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(settings, "max_queue_depth", 0)


async def _listing(folder: Path) -> dict[str, str | None]:
    from routes import files as files_routes

    listing = await files_routes.list_files(
        path=str(folder), show_archives=True, summarize_archives=False,
    )
    return {entry.name: entry.companion_of for entry in listing.entries}


# --------------------------------------------------------------------------- #
# The listing: companion rows name their Source
# --------------------------------------------------------------------------- #


@SETS
@pytest.mark.asyncio
async def test_the_listing_marks_every_later_rar_volume(tmp_path, names):
    folder = tmp_path / "usenet"
    volumes = copy_set(names, folder)

    marks = await _listing(folder)

    first = str(volumes[0])
    assert marks == {names[0]: None, names[1]: first, names[2]: first}, marks


@pytest.mark.asyncio
async def test_the_listing_marks_every_split_wiiu_part(tmp_path):
    folder = tmp_path / "wiiu"
    folder.mkdir()
    for index in (1, 2, 3):
        (folder / f"game_part{index}.wud").write_bytes(b"\0" * 16)
    (folder / "other.wud").write_bytes(b"\0" * 16)

    marks = await _listing(folder)

    first = str(folder / "game_part1.wud")
    assert marks == {
        "game_part1.wud": None,
        "game_part2.wud": first,
        "game_part3.wud": first,
        "other.wud": None,
    }, marks


@pytest.mark.asyncio
async def test_an_unreadable_set_gets_no_mark(tmp_path, monkeypatch):
    from services import archive_members

    folder = tmp_path / "usenet"
    copy_set(RAR5_SET, folder)

    def broken(path):
        raise archive_members.IncompleteArchive("a volume is missing")

    monkeypatch.setattr(archive_members, "_read_rar_set_now", broken)

    marks = await _listing(folder)

    assert set(marks) == set(RAR5_SET), marks
    assert all(mark is None for mark in marks.values()), marks


@pytest.mark.asyncio
async def test_a_header_read_that_hangs_gets_no_mark_and_still_lists(tmp_path, monkeypatch):
    import threading

    from services import archive_members

    folder = tmp_path / "usenet"
    copy_set(RAR5_SET, folder)
    release = threading.Event()

    def hangs(path):
        release.wait(5)
        raise archive_members.IncompleteArchive("never answered")

    monkeypatch.setattr(archive_members, "_read_rar_set_now", hangs)
    monkeypatch.setattr(archive_members, "RAR_HEADER_READ_SECONDS", 0.05)
    try:
        marks = await _listing(folder)
    finally:
        release.set()

    assert set(marks) == set(RAR5_SET), marks
    assert all(mark is None for mark in marks.values()), marks


def test_the_mark_comes_from_the_registry_hook_not_the_extension(tmp_path, monkeypatch):
    """A tool's own companions mark rows, whatever their extension."""
    from routes import files as files_routes
    from services.tools import registry

    names = ["disc.cue", "disc (Track 2).bin", "lone.bin"]
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    cue = str(tmp_path / "disc.cue")
    track = str(tmp_path / "disc (Track 2).bin")
    real = registry.source_companions

    def companions(path, *, strict=False):
        return [track] if path == cue else real(path, strict=strict)

    monkeypatch.setattr(registry, "source_companions", companions)

    marks = files_routes.companion_marks([str(tmp_path / name) for name in names])

    assert marks == {track: cue}, marks


# --------------------------------------------------------------------------- #
# The batch: each skipped item with its reason and code
# --------------------------------------------------------------------------- #


def _app() -> FastAPI:
    from routes.convert import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


@pytest.fixture(name="queued")
def _queued(monkeypatch):
    from routes import convert as convert_routes

    captured: list = []

    async def fake_atomic(job_specs, _mode, **_kwargs):
        captured.append(job_specs)
        return []

    monkeypatch.setattr(convert_routes.job_manager, "get_queue_depth", lambda: 0)
    monkeypatch.setattr(convert_routes.job_manager, "create_jobs_atomic", fake_atomic)
    return captured


async def _post_batch(body: dict, **params) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/jobs/batch", json=body, params=params)


@pytest.mark.asyncio
async def test_a_later_volume_comes_back_skipped_with_its_reason(tmp_path, queued):
    volumes = copy_set(RAR5_SET, tmp_path / "usenet")

    response = await _post_batch(
        {"file_paths": [str(volumes[1])], "mode": "rar_extract"}, report_skipped="true",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["jobs"] == [], body
    [skip] = body["skipped"]
    assert skip["path"] == str(volumes[1]), skip
    assert "later volume of a RAR set" in skip["reason"], skip
    assert "rar5-vols.part1.rar" in skip["reason"], skip
    assert skip["code"] == "rar_set_not_extractable", skip


@pytest.mark.asyncio
async def test_existing_batch_clients_still_get_a_job_list(tmp_path, queued):
    volumes = copy_set(RAR5_SET, tmp_path / "usenet")

    response = await _post_batch({"file_paths": [str(volumes[1])], "mode": "rar_extract"})

    assert response.status_code == 200, response.text
    assert response.json() == []


@pytest.mark.asyncio
async def test_a_batch_called_in_process_still_returns_the_job_list(tmp_path, queued):
    """The CLI and tests call the route function itself: the default holds."""
    from models import BatchJobCreateRequest
    from routes import convert as convert_routes

    volumes = copy_set(RAR5_SET, tmp_path / "usenet")

    result = await convert_routes.create_batch_jobs(
        BatchJobCreateRequest(file_paths=[str(volumes[1])], mode="rar_extract"),
    )

    assert result == []


@pytest.mark.asyncio
async def test_every_plan_job_skip_carries_its_code(tmp_path, queued):
    """Generic: a skip any tool raises reaches the report with its code."""
    missing = tmp_path / "gone.iso"
    taken = tmp_path / "Game.iso"
    taken.write_bytes(b"x")
    (tmp_path / "Game.chd").write_bytes(b"x")

    response = await _post_batch(
        {"file_paths": [str(missing), str(taken)], "mode": "createdvd"},
        report_skipped="true",
    )

    assert response.status_code == 200, response.text
    skipped = {s["path"]: s for s in response.json()["skipped"]}
    assert skipped[str(missing)]["code"] == "file_not_found", skipped
    assert skipped[str(missing)]["reason"] == "File not found", skipped
    assert skipped[str(taken)]["code"] == "output_exists", skipped
    assert skipped[str(taken)]["reason"], skipped


def test_a_prepared_batch_keeps_its_path_to_reason_map():
    """Auto and the CLI read ``PreparedBatch.skipped`` as path -> reason."""
    from routes.convert import BatchSkip, PreparedBatch

    batch = PreparedBatch(
        request=None, compression=None, plans=[],
        skips=[BatchSkip(path="/v/a", reason="why", code="file_not_found")],
    )

    assert batch.skipped == {"/v/a": "why"}
