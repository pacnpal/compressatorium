"""An output Volume that stops answering after conversion fails the job (#352).

recording a new CHD's disc ID stats the output, bounded. If
that stat does not answer, the output's storage has stopped answering, and
every step after the hook reads it (sizing the output, verify, delete). So the
hook lets ``DiscIdStorageAbandoned`` through and the job fails promptly with an
error naming the file, as a stuck chdman child did before. An ordinary database
failure stays best effort. Sizing the output is bounded too, so a stalled
``getsize`` cannot hold the job and its queue slot either.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

import pytest

from .test_disc_id import _make_iso

SYSTEM_CNF = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\n"
BOUND = 10.0     # far past the shrunk bounds; an unbounded wait fails here


@pytest.fixture(name="env")
def _env(tmp_path, monkeypatch, db_path, reset_db_engine):
    from services import db as _db
    from services import subprocess_runner
    from services.tools import registry

    from app.routes import convert as convert_routes

    _db.init_engine(db_path)
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    monkeypatch.setattr(subprocess_runner.settings, "progress_timeout", 1)
    monkeypatch.setattr(subprocess_runner, "_MIN_STALL_TIMEOUT", 1)
    monkeypatch.setattr(subprocess_runner, "_STAT_TIMEOUT", 0.3)

    async def fake_convert(input_path, output_path, mode="", **_kwargs):
        Path(output_path).write_bytes(b"MComprHD converted")
        yield {"progress": 100, "message": "done"}

    for tool_id in ("chdman", "z3ds"):
        monkeypatch.setattr(registry.get(tool_id)._service, "convert", fake_convert)

    release = threading.Event()
    yield {"tmp_path": tmp_path, "routes": convert_routes, "release": release}
    release.set()
    reset_db_engine()


async def _run(env, source, mode):
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest

    routes = env["routes"]
    job = await routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode(mode),
        duplicate_action=DuplicateAction.SKIP,
    ))
    await asyncio.wait_for(routes.job_manager._process_job(job.id), BOUND)
    return job


def _block_for(monkeypatch, module_os_path, name: str, target: str, release):
    """Make ``os.path.<name>`` block on *target* only (a Volume gone quiet)."""
    real = getattr(module_os_path, name)

    def stuck(path, *args, **kwargs):
        if os.path.basename(str(path)) == target:
            release.wait(60)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(module_os_path, name, stuck)


def _iso(env):
    iso = env["tmp_path"] / "Game.iso"
    iso.write_bytes(_make_iso({"SYSTEM.CNF": SYSTEM_CNF}))
    return iso


@pytest.mark.asyncio
async def test_an_output_volume_that_stops_answering_fails_the_job(env, monkeypatch):
    # The disc-ID record's stat of the output never answers.
    _block_for(monkeypatch, os.path, "getmtime", "Game.chd", env["release"])

    job = await _run(env, _iso(env), "createdvd")

    assert job.status.value == "failed"
    assert "Game.chd" in (job.error_message or "")
    # The job's slot is free again: nothing of it is still running.
    assert job.id not in env["routes"].job_manager._cancel_events


@pytest.mark.asyncio
async def test_a_database_failure_recording_the_disc_id_is_only_a_warning(
    env, monkeypatch, caplog,
):
    from services.chd_metadata_store import chd_metadata_store

    async def locked(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(chd_metadata_store, "update_disc_id_info", locked)

    with caplog.at_level(logging.WARNING):
        job = await _run(env, _iso(env), "createdvd")

    assert job.status.value == "completed", job.error_message
    assert any(
        "Game.chd" in r.getMessage() and "SLUS-20312" in r.getMessage()
        for r in caplog.records if r.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_sizing_an_output_that_never_answers_does_not_hold_the_job(
    env, monkeypatch,
):
    rom = env["tmp_path"] / "Game.3ds"
    rom.write_bytes(b"cartridge")
    _block_for(monkeypatch, os.path, "getsize", "Game.z3ds", env["release"])

    job = await _run(env, rom, "z3ds_compress")

    assert job.status.value == "failed"
    assert "Game.z3ds" in (job.error_message or "")
