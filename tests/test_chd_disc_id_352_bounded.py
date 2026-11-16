"""Disc-ID reads are bounded and cancellable (#352, Codex 4582416806).

A network Volume can answer a cached ``stat`` and then block every read. The
library scan's disc-ID step reads a CHD's sectors (and a companion file), and a
conversion reads its Source for the disc ID; each of those reads must end at a
deadline and on Cancel instead of holding the scan (``_is_scanning``, the
``metadata_scan`` lane) or the job forever. A read that does not answer is
written off as abandoned storage: a warning names the file, and the scan stops
its pass there, the same rule as a chdman child that outlived SIGKILL (#268).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading

import pytest

from .test_disc_id import _make_chd_v5, _make_iso

SYSTEM_CNF = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\n"
BOUND = 5.0     # far past the shrunk deadline; on an unbounded read, the test fails here


@pytest.fixture(name="disc_id")
def _disc_id():
    # The module the app imports (``services.disc_id``), not ``app.services...``.
    from services import disc_id

    return disc_id


@pytest.fixture(name="stuck_sectors")
def _stuck_sectors(disc_id, monkeypatch):
    """A sector reader on a Volume that never answers; released at teardown."""
    release = threading.Event()
    calls: list[str] = []

    def blocked(path):
        calls.append(path)
        release.wait(60)
        return None

    monkeypatch.setattr(disc_id, "_extract_from_chd_sectors", blocked)
    monkeypatch.setattr(disc_id, "_DISC_READ_TIMEOUT_SECONDS", 0.3, raising=False)
    yield calls
    release.set()


def _chd(tmp_path):
    chd = tmp_path / "Stuck.chd"
    chd.write_bytes(_make_chd_v5(_make_iso({"SYSTEM.CNF": SYSTEM_CNF})))
    return chd


@pytest.mark.asyncio
async def test_a_sector_read_that_never_answers_ends_at_the_deadline(
    tmp_path, disc_id, stuck_sectors, caplog,
):
    chd = _chd(tmp_path)

    with caplog.at_level(logging.WARNING), pytest.raises(disc_id.DiscIdStorageAbandoned):
        await asyncio.wait_for(disc_id.extract_from_chd(str(chd), "chdman"), BOUND)

    assert any(
        "Stuck.chd" in r.getMessage()
        for r in caplog.records if r.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_cancel_ends_a_sector_read_at_once(tmp_path, disc_id, stuck_sectors):
    from services.subprocess_runner import ReadCancelled

    cancel = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, cancel.set)
    # A long deadline, so only Cancel can end it in time.
    disc_id._DISC_READ_TIMEOUT_SECONDS = 30

    with pytest.raises(ReadCancelled):
        await asyncio.wait_for(
            disc_id.extract_from_chd(str(_chd(tmp_path)), "chdman", cancel_event=cancel),
            BOUND,
        )


def _scan_env(tmp_path, monkeypatch):
    from routes import info as info_routes

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    async def fresh(_path):
        return False

    async def nothing(*_a, **_k):
        return None

    async def info(_path):
        return {"raw_data": "Metadata:     Tag='DVD '  Index=0  Length=0 bytes"}

    async def set_metadata(path, info, persist=False):
        return {"media_type": "dvd"}

    store = info_routes.chd_metadata_store
    monkeypatch.setattr(info_routes.chdman_service, "info", info)
    monkeypatch.setattr(store, "set_metadata", set_metadata)
    monkeypatch.setattr(store, "flush_async", nothing)
    monkeypatch.setattr(store, "is_stale", fresh)
    monkeypatch.setattr(store, "is_disc_id_checked", fresh)
    monkeypatch.setattr(store, "mark_disc_id_checked", nothing)
    monkeypatch.setattr(store, "update_disc_id_info", nothing)

    created: list[str] = []
    original = info_routes.job_manager.create_external_job

    def capture(*args, **kwargs):
        job = original(*args, **kwargs)
        created.append(job.id)
        return job

    monkeypatch.setattr(info_routes.job_manager, "create_external_job", capture)
    return info_routes, created


@pytest.mark.asyncio
async def test_a_stuck_disc_id_read_ends_the_scan_and_frees_the_lane(
    tmp_path, stuck_sectors, monkeypatch,
):
    from services.workload_limiter import workload_limiter

    _chd(tmp_path)
    (tmp_path / "Later.chd").write_bytes(b"x")
    info_routes, created = _scan_env(tmp_path, monkeypatch)

    await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)

    job = info_routes.job_manager.jobs[created[0]]
    assert job.status.value == "failed"
    # The pass stops at the first stuck read: no second thread on that storage.
    assert len(stuck_sectors) == 1
    stuck_name = stuck_sectors[0].rsplit("/", 1)[-1]
    assert "Phase 2" in job.error_message and stuck_name in job.error_message
    assert info_routes._is_scanning is False
    assert workload_limiter.in_use("metadata_scan") == 0


@pytest.mark.asyncio
async def test_cancel_during_a_stuck_disc_id_read_stops_the_scan(
    tmp_path, disc_id, monkeypatch,
):
    from services.workload_limiter import workload_limiter

    _chd(tmp_path)
    info_routes, created = _scan_env(tmp_path, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(disc_id, "_DISC_READ_TIMEOUT_SECONDS", 30, raising=False)
    loop = asyncio.get_running_loop()

    def blocked_until_cancelled(path):
        # Cancel arrives while the read is stuck.
        asyncio.run_coroutine_threadsafe(
            info_routes.job_manager.cancel_job(created[0]), loop,
        )
        release.wait(60)
        return None

    monkeypatch.setattr(disc_id, "_extract_from_chd_sectors", blocked_until_cancelled)
    try:
        await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)
    finally:
        release.set()

    assert info_routes.job_manager.jobs[created[0]].status.value == "cancelled"
    assert info_routes._is_scanning is False
    assert workload_limiter.in_use("metadata_scan") == 0


@pytest.mark.asyncio
async def test_a_source_that_never_answers_ends_the_hook_with_storage_abandoned(
    tmp_path, disc_id, monkeypatch, caplog,
):
    """The job manager fails the job on this, rather than hanging on the
    storage in its next step (Codex 4582518760)."""
    from services.tools import registry

    chdman = registry.get("chdman")
    plugin_mod = sys.modules[type(chdman).__module__]
    release = threading.Event()

    def blocked(path, **_kwargs):
        release.wait(60)
        return None

    monkeypatch.setattr(plugin_mod, "extract_from_source", blocked)
    monkeypatch.setattr(disc_id, "_DISC_READ_TIMEOUT_SECONDS", 0.3)
    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    out = tmp_path / "Game.chd"
    out.write_bytes(b"MComprHD")

    try:
        with caplog.at_level(logging.WARNING), pytest.raises(
            disc_id.DiscIdStorageAbandoned, match="Game.iso",
        ):
            await asyncio.wait_for(
                chdman.post_convert(str(src), str(out), "createdvd"), BOUND,
            )
    finally:
        release.set()

    assert any(
        "Game.iso" in r.getMessage()
        for r in caplog.records if r.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_a_disc_id_stat_that_never_answers_is_bounded(tmp_path, monkeypatch):
    from services import chd_metadata_store as store_mod
    from services import subprocess_runner

    release = threading.Event()

    def stuck_getmtime(_path):
        release.wait(60)
        return 0.0

    monkeypatch.setattr(store_mod.os.path, "getmtime", stuck_getmtime)
    monkeypatch.setattr(subprocess_runner, "_STAT_TIMEOUT", 0.3)
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"x")
    try:
        # Not checked: the scan goes on to its own bounded read.
        assert await asyncio.wait_for(
            store_mod.chd_metadata_store.is_disc_id_checked(str(chd)), BOUND,
        ) is False
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                store_mod.chd_metadata_store.mark_disc_id_checked(str(chd)), BOUND,
            )
    finally:
        release.set()
