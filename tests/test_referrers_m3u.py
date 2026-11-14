"""Referrers (#295): the ``.m3u`` beside a converted disc follows the output.

Drives the real pipeline (convert route -> planner -> job manager) against a
temp Volume. Only the tool's convert, post-convert and verify are stubbed, so
the delete plan, the Referrer hook and the delete itself all run for real.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from services import db as _db
from services import ledger as ledger_store

from app.models import (
    ConversionMode,
    DeletePlanRequest,
    DuplicateAction,
    JobCreateRequest,
    JobStatus,
)
from app.routes import convert as convert_routes
from app.services import job_manager as job_manager_module
from app.services import ledger

M3U = (
    b"#EXTM3U\r\n"
    b"Game (Disc 1).cue\r\n"
    b"Game (Disc 2).cue\r\n"
    b"Other Game.cue\r\n"
)


def _cue(path: Path) -> None:
    """A one-track cue sheet and its .bin, so the delete plan covers both."""
    track = path.with_suffix(".bin")
    track.write_bytes(b"\0" * 2352)
    path.write_text(
        f'FILE "{track.name}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
    )


@pytest.fixture(name="volume")
def _volume(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)

    async def fake_convert(input_path, output_path, mode, *, compression=None,
                           split=False, cancel_event=None):
        Path(output_path).write_bytes(b"CHD")
        yield {"progress": 100, "message": "done"}

    async def noop_post_convert(input_path, output_path, mode):
        return None

    async def fake_verify(path, *, cancel_event=None):
        return {"valid": True, "message": "ok"}

    async def noop(*_a, **_kw):
        return None

    for tool in convert_routes.registry.all():
        monkeypatch.setattr(tool, "convert", fake_convert)
        monkeypatch.setattr(tool, "post_convert", noop_post_convert)
        monkeypatch.setattr(tool, "verify", fake_verify, raising=False)
    monkeypatch.setattr(job_manager_module.verification_store, "mark_verified", noop)
    monkeypatch.setattr(job_manager_module.verification_store, "clear", noop)
    monkeypatch.setattr(job_manager_module.chd_metadata_store, "clear", noop)

    for disc in ("Game (Disc 1)", "Game (Disc 2)", "Other Game"):
        _cue(tmp_path / f"{disc}.cue")
    (tmp_path / "Game.m3u").write_bytes(M3U)
    return tmp_path


async def _convert_disc_1(volume: Path):
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(volume / "Game (Disc 1).cue"),
        mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=True,
    ))
    await convert_routes.job_manager._process_job(job.id)
    return job


@pytest.mark.asyncio
async def test_playlist_follows_the_converted_disc(volume):
    job = await _convert_disc_1(volume)

    assert job.status == JobStatus.COMPLETED, job.error_message
    # Only the replaced entry changes; CRLF endings, the header, the other
    # disc and the unrelated game are byte-for-byte the same.
    assert (volume / "Game.m3u").read_bytes() == (
        b"#EXTM3U\r\n"
        b"Game (Disc 1).chd\r\n"
        b"Game (Disc 2).cue\r\n"
        b"Other Game.cue\r\n"
    )
    # The Source went only after the playlist was updated.
    assert not (volume / "Game (Disc 1).cue").exists()
    assert not (volume / "Game (Disc 1).bin").exists()
    assert (volume / "Game (Disc 1).chd").is_file()
    # One-time backup of the playlist as it was.
    assert (volume / "Game.m3u.bak").read_bytes() == M3U
    # The Ledger row names the Referrer that was updated.
    assert ledger.row_from_job(job)["referrers_updated"] == [
        {"referrer": "m3u", "target": str(volume / "Game.m3u"), "status": "updated"},
    ]


@pytest.mark.asyncio
async def test_playlist_in_the_parent_folder_follows_a_hidden_disc(volume):
    """Batocera / ES-DE: Game.m3u in the system folder, discs in a hidden folder."""
    hidden = volume / "psx" / ".discs"
    hidden.mkdir(parents=True)
    _cue(hidden / "Game (Disc 1).cue")
    playlist = volume / "psx" / "Game.m3u"
    playlist.write_bytes(b".discs/Game (Disc 1).cue\n")

    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(hidden / "Game (Disc 1).cue"),
        mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=True,
    ))
    await convert_routes.job_manager._process_job(job.id)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert playlist.read_bytes() == b".discs/Game (Disc 1).chd\n"
    assert not (hidden / "Game (Disc 1).cue").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permissions",
)
async def test_read_only_playlist_keeps_the_source(volume, db_path, reset_db_engine):
    playlist = volume / "Game.m3u"
    playlist.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        job = await _convert_disc_1(volume)
    finally:
        playlist.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.message == "converted, source kept: couldn't update Game.m3u (is read-only)"
    assert playlist.read_bytes() == M3U
    assert (volume / "Game (Disc 1).cue").is_file()
    assert (volume / "Game (Disc 1).bin").is_file()
    assert (volume / "Game (Disc 1).chd").is_file()
    assert job.deleted_paths == []
    # The Ledger row, written and read back, says which Referrer failed and why.
    _db.init_engine(db_path)
    try:
        await ledger_store.record(job)
        [row] = await ledger_store.recent(1)
    finally:
        reset_db_engine()
    assert row["source_deleted"] is False
    assert row["referrers_updated"] == [{
        "referrer": "m3u", "target": str(playlist),
        "status": "failed", "reason": "is read-only",
    }]


def test_a_completed_job_that_kept_its_source_shows_why():
    """Static check: JobRow shows the message on a completed job whose
    delete-on-verify kept the Source."""
    row = (Path(__file__).resolve().parents[1] / "src" / "lib" / "components"
           / "panels" / "JobRow.svelte").read_text()
    assert "sourceKept" in row
    assert "delete_on_verify" in row and "deleted_paths" in row


@pytest.mark.asyncio
async def test_playlist_not_naming_the_disc_is_left_alone(volume):
    other = volume / "Other.m3u"
    other.write_bytes(b"Other Game.cue\n")

    await _convert_disc_1(volume)

    assert other.read_bytes() == b"Other Game.cue\n"
    assert not (volume / "Other.m3u.bak").exists()


@pytest.mark.asyncio
async def test_delete_confirmation_lists_the_playlist_change(volume):
    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(volume / "Game (Disc 1).cue")],
        mode=ConversionMode.CREATECD,
    ))

    assert plan["items"][0]["referrer_changes"] == [{
        "referrer": "m3u",
        "target": str(volume / "Game.m3u"),
        "edits": [{"old": "Game (Disc 1).cue", "new": "Game (Disc 1).chd"}],
    }]
    # Planning writes nothing.
    assert (volume / "Game.m3u").read_bytes() == M3U
    assert not (volume / "Game.m3u.bak").exists()


@pytest.mark.asyncio
async def test_delete_confirmation_follows_the_output_folder(volume):
    (volume / "chd").mkdir()
    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(volume / "Game (Disc 1).cue")],
        mode=ConversionMode.CREATECD,
        output_dir=str(volume / "chd"),
    ))

    [change] = plan["items"][0]["referrer_changes"]
    assert change["edits"] == [
        {"old": "Game (Disc 1).cue", "new": os.path.join("chd", "Game (Disc 1).chd")},
    ]


_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"


def test_confirmation_modal_shows_referrer_changes():
    """Static check: the modal renders each item's planned Referrer edits, and
    the store asks for the plan with the chosen output folder."""
    modal = (_SRC / "components" / "modals" / "DeletePlanModal.svelte").read_text()
    assert "referrer_changes" in modal
    endpoints = (_SRC / "api" / "endpoints.js").read_text()
    assert "output_dir" in endpoints.split("getDeletePlan", 1)[1].split("},", 1)[0]
    store = (_SRC / "stores" / "conversion.svelte.js").read_text()
    assert "getDeletePlan(filePaths, this.mode, this.outputDir" in store
