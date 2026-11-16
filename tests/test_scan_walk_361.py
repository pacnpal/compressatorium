"""Listing files is bounded by a stall bound and cancellable (#361).

A Volume mount that freezes blocks a directory listing in the kernel. The
metadata scan's walk (``collect_scannable_paths``), the browser's folder
listing and its recursive search ran that listing with no bound, so a frozen
mount held the scan (``_is_scanning``, the ``metadata_scan`` lane) or a request
forever, and Cancel could not stop it. Now each runs on a detached thread
under a stall bound that resets on every entry it reads: a frozen listing
ends at the bound with a warning naming the folder, Cancel ends it at once,
and a large tree that keeps moving is never cut off.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

import pytest
from fastapi import BackgroundTasks, HTTPException

BOUND = 5.0     # far past the shrunk stall bound; an unbounded walk fails here
STALL = 0.3


@pytest.fixture(name="frozen")
def _frozen(monkeypatch):
    """``os.scandir``/``os.listdir`` on a folder named ``Frozen`` block on an
    Event that never fires (until teardown), as a frozen mount does."""
    release = threading.Event()
    real_scandir, real_listdir = os.scandir, os.listdir
    hits: list[str] = []

    def is_frozen(path) -> bool:
        return os.path.basename(os.fspath(path)) == "Frozen" and not release.is_set()

    def scandir(path="."):
        if is_frozen(path):
            hits.append(os.fspath(path))
            release.wait(60)
        return real_scandir(path)

    def listdir(path="."):
        if is_frozen(path):
            hits.append(os.fspath(path))
            release.wait(60)
        return real_listdir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    yield hits, release
    release.set()


def _scan_env(tmp_path, monkeypatch, *, stall=STALL):
    from routes import info as info_routes

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(info_routes, "_WALK_STALL_SECONDS", stall, raising=False)

    async def fresh(_path):
        return False

    async def nothing(*_a, **_k):
        return None

    store = info_routes.chd_metadata_store
    monkeypatch.setattr(store, "flush_async", nothing)
    monkeypatch.setattr(store, "is_stale", fresh)
    monkeypatch.setattr(store, "is_disc_id_checked", fresh)
    monkeypatch.setattr(store, "mark_disc_id_checked", nothing)

    async def no_match(_job_id, paths, *, force):
        return len(paths), 0

    monkeypatch.setattr(info_routes, "_scan_phase_dat_match", no_match)

    created: list[str] = []
    original = info_routes.job_manager.create_external_job

    def capture(*args, **kwargs):
        job = original(*args, **kwargs)
        created.append(job.id)
        return job

    monkeypatch.setattr(info_routes.job_manager, "create_external_job", capture)
    return info_routes, created


def _lane_in_use() -> int:
    from services.workload_limiter import workload_limiter

    return workload_limiter.in_use("metadata_scan")


# --- the metadata scan ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_frozen_folder_ends_the_scan_at_the_bound_with_a_warning(
    tmp_path, frozen, monkeypatch, caplog,
):
    hits, _release = frozen
    (tmp_path / "Frozen").mkdir()
    info_routes, created = _scan_env(tmp_path, monkeypatch)

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)

    job = info_routes.job_manager.jobs[created[0]]
    frozen_dir = str(tmp_path / "Frozen")
    assert hits == [frozen_dir]
    assert job.status.value == "failed"
    assert frozen_dir in job.error_message
    assert any(
        r.levelno == logging.WARNING and frozen_dir in r.getMessage() for r in caplog.records
    )
    assert info_routes._is_scanning is False
    assert _lane_in_use() == 0


@pytest.mark.asyncio
async def test_cancel_during_a_frozen_walk_stops_the_scan_at_once(
    tmp_path, monkeypatch,
):
    (tmp_path / "Frozen").mkdir()
    info_routes, created = _scan_env(tmp_path, monkeypatch, stall=30)
    release = threading.Event()
    real_scandir = os.scandir
    loop = asyncio.get_running_loop()
    cancelled_at: list[float] = []

    def scandir(path="."):
        if os.path.basename(os.fspath(path)) == "Frozen" and not release.is_set():
            cancelled_at.append(time.monotonic())
            asyncio.run_coroutine_threadsafe(
                info_routes.job_manager.cancel_job(created[0]), loop,
            )
            release.wait(60)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    try:
        await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)
        ended = time.monotonic()
    finally:
        release.set()

    assert ended - cancelled_at[0] < 1.0
    assert info_routes.job_manager.jobs[created[0]].status.value == "cancelled"
    assert info_routes._is_scanning is False
    assert _lane_in_use() == 0


@pytest.mark.asyncio
async def test_the_next_scan_starts_after_a_stalled_one(tmp_path, frozen, monkeypatch):
    _hits, release = frozen
    (tmp_path / "Frozen").mkdir()
    (tmp_path / "Frozen" / "Game.chd").write_bytes(b"x")
    info_routes, created = _scan_env(tmp_path, monkeypatch)
    await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)
    assert info_routes.job_manager.jobs[created[0]].status.value == "failed"

    release.set()  # the mount answers again
    tasks = BackgroundTasks()
    answer = await info_routes.trigger_metadata_scan(tasks, force=False)
    assert answer["status"] == "started"
    await asyncio.wait_for(tasks.tasks[0].func(*tasks.tasks[0].args), BOUND)

    assert info_routes.job_manager.jobs[created[1]].status.value == "completed"
    assert info_routes._is_scanning is False
    assert _lane_in_use() == 0


@pytest.mark.asyncio
async def test_a_large_tree_that_keeps_moving_is_not_cut_off(tmp_path, monkeypatch):
    """Each folder answers well inside the stall bound, but the whole walk
    takes several bounds: the bound is for no progress, not for the walk."""
    folders = 12
    for index in range(folders):
        folder = tmp_path / f"Disc {index:02d}"
        folder.mkdir()
        (folder / "Game.chd").write_bytes(b"x")
    info_routes, created = _scan_env(tmp_path, monkeypatch)
    real_scandir = os.scandir
    seen: list[str] = []

    def slow_but_alive(path="."):
        time.sleep(STALL / 3)
        result = real_scandir(path)
        seen.append(os.fspath(path))
        return result

    monkeypatch.setattr(os, "scandir", slow_but_alive)
    started = time.monotonic()
    await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)

    assert time.monotonic() - started > 2 * STALL
    job = info_routes.job_manager.jobs[created[0]]
    assert job.status.value == "completed", job.error_message
    assert len(seen) > folders


# --- the file browser ----------------------------------------------------------


@pytest.fixture(name="files_env")
def _files_env(tmp_path, monkeypatch):
    from routes import files as files_routes
    from routes import volume_guard

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(volume_guard, "WALK_STALL_SECONDS", STALL, raising=False)
    (tmp_path / "Frozen").mkdir()
    return files_routes


async def _answer_of(call) -> int:
    try:
        await asyncio.wait_for(call, BOUND)
    except HTTPException as exc:
        return exc.status_code
    return 200


@pytest.mark.asyncio
async def test_listing_a_frozen_folder_answers_503(tmp_path, files_env, frozen):
    status = await _answer_of(
        files_env.list_files(path=str(tmp_path / "Frozen"), show_archives=True,
                             summarize_archives=True),
    )
    assert status == 503


@pytest.mark.asyncio
async def test_searching_under_a_frozen_folder_answers_503(tmp_path, files_env, frozen):
    status = await _answer_of(
        files_env.search_files(path=str(tmp_path), recursive=True, include_archives=True),
    )
    assert status == 503


# --- the shared helper ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_stall_bound_resets_on_progress_and_names_where_it_stopped():
    from services.subprocess_runner import Stalled, run_detached_until_stalled

    stop = threading.Event()

    def walk(*, progress):
        for index in range(8):
            progress(f"folder {index}")
            time.sleep(STALL / 4)
        progress("the last folder")
        stop.wait(60)

    try:
        with pytest.raises(Stalled) as caught:
            await asyncio.wait_for(
                run_detached_until_stalled(walk, stall=STALL, what="Listing"), BOUND,
            )
    finally:
        stop.set()
    assert caught.value.where == "the last folder"
    assert "the last folder" in str(caught.value)
