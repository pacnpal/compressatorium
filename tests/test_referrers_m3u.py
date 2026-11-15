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
import services.referrers as hook_module
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


@pytest.mark.asyncio
async def test_delete_confirmation_names_the_numbered_output_a_rename_writes(volume):
    """With RENAME and the base output taken, the preview names the numbered
    path the job will write, resolved the same way the job plans it."""
    (volume / "Game (Disc 1).chd").write_bytes(b"older")
    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(volume / "Game (Disc 1).cue")],
        mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.RENAME,
    ))
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(volume / "Game (Disc 1).cue"),
        mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.RENAME,
        delete_on_verify=True,
    ))

    [change] = plan["items"][0]["referrer_changes"]
    assert job.output_path != str(volume / "Game (Disc 1).chd")
    assert change["edits"] == [
        {"old": "Game (Disc 1).cue", "new": os.path.basename(job.output_path)},
    ]


@pytest.mark.asyncio
async def test_a_locked_rename_target_is_a_plan_error_not_a_500(volume, monkeypatch):
    """When RENAME can't find a free name (or the folder is locked), the
    preview says so on the item instead of failing the whole request."""
    def refuse(*_a, **_kw):
        raise convert_routes.SkipFile(convert_routes.SkipReason.OUTPUT_LOCKED)

    monkeypatch.setattr(convert_routes, "_renamed_output", refuse)

    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(volume / "Game (Disc 1).cue")],
        mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.RENAME,
    ))

    [item] = plan["items"]
    assert plan["blocked"] is True
    assert item["errors"]
    assert item["referrer_changes"] == []


@pytest.mark.asyncio
async def test_a_track_outside_the_volumes_is_blocked_and_never_scanned(
    volume, tmp_path_factory,
):
    """A cue's track reached through a symlinked folder that leaves the Volume
    is unsafe: it is not a delete path and its folder is not searched."""
    outside = tmp_path_factory.mktemp("outside")
    (outside / "Game.bin").write_bytes(b"\0" * 2352)
    (outside / "Secret.m3u").write_text("Game.bin\n")
    (volume / "tracks").symlink_to(outside)
    (volume / "Linked.cue").write_text(
        'FILE "tracks/Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
    )

    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(volume / "Linked.cue")], mode=ConversionMode.CREATECD,
    ))

    [item] = plan["items"]
    assert plan["blocked"] is True
    assert any("outside configured volumes" in u for u in item["unsafe_paths"])
    assert str(outside / "Game.bin") not in item["delete_paths"]
    assert all("Secret.m3u" not in c["target"] for c in item["referrer_changes"])


def _wrap_hook(monkeypatch, after):
    """Run the real Referrer hook, then *after* (the race being simulated)."""
    real = hook_module.update_before_source_removal

    async def wrapped(image_path, renames, **kw):
        outcome = await real(image_path, renames, **kw)
        after()
        return outcome

    monkeypatch.setattr(hook_module, "update_before_source_removal", wrapped)


@pytest.mark.asyncio
async def test_a_source_changed_during_the_hook_is_kept(volume, monkeypatch):
    source = volume / "Game (Disc 1).cue"
    _wrap_hook(monkeypatch, lambda: source.write_text(source.read_text() + "REM edited\n"))

    job = await _convert_disc_1(volume)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.message.startswith("converted, source kept:")
    assert source.is_file() and (volume / "Game (Disc 1).bin").is_file()
    assert job.deleted_paths == []


@pytest.mark.asyncio
async def test_a_cancel_during_the_hook_keeps_the_source(volume, monkeypatch):
    def cancel_all():
        for event in list(convert_routes.job_manager._cancel_events.values()):
            event.set()

    _wrap_hook(monkeypatch, cancel_all)

    job = await _convert_disc_1(volume)

    assert "Delete skipped (cancelled)" in job.message
    assert (volume / "Game (Disc 1).cue").is_file()
    assert job.deleted_paths == []
    # The playlist already points at the output, which is still there.
    assert b"Game (Disc 1).chd" in (volume / "Game.m3u").read_bytes()
    assert (volume / "Game (Disc 1).chd").is_file()


@pytest.mark.asyncio
async def test_a_cancel_during_the_second_path_check_keeps_the_source(volume, monkeypatch):
    """The re-check after the hook awaits too; a Cancel there still wins."""
    manager_cls = type(convert_routes.job_manager)
    real_check = manager_cls._check_delete_paths
    calls = []

    async def check_then_cancel(self, *args, **kwargs):
        await real_check(self, *args, **kwargs)
        calls.append(1)
        if len(calls) == 2:
            for event in list(self._cancel_events.values()):
                event.set()

    monkeypatch.setattr(manager_cls, "_check_delete_paths", check_then_cancel)

    job = await _convert_disc_1(volume)

    assert len(calls) == 2
    assert "Delete skipped (cancelled)" in job.message
    assert (volume / "Game (Disc 1).cue").is_file()
    assert job.deleted_paths == []


@pytest.mark.asyncio
async def test_cancel_frees_the_slot_while_a_playlist_read_hangs(volume, monkeypatch):
    """A playlist on a mount that stopped answering: Cancel returns the job
    at once, keeps the Source, and doesn't wait for the read."""
    import asyncio
    import threading

    from services.referrers import m3u as m3u_module

    entered, release = threading.Event(), threading.Event()
    real_read = m3u_module._read

    def stuck_read(path):
        entered.set()
        release.wait(30)
        return real_read(path)

    monkeypatch.setattr(m3u_module, "_read", stuck_read)
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(volume / "Game (Disc 1).cue"),
        mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=True,
    ))
    task = asyncio.create_task(convert_routes.job_manager._process_job(job.id))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        await convert_routes.job_manager.cancel_job(job.id)
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()

    assert (volume / "Game (Disc 1).cue").is_file()
    assert job.deleted_paths == []
    assert (volume / "Game.m3u").read_bytes() == M3U


_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"


def test_confirmation_sends_the_duplicate_action():
    endpoints = (_SRC / "api" / "endpoints.js").read_text()
    assert "duplicate_action" in endpoints.split("getDeletePlan", 1)[1].split("},", 1)[0]
    panel = (_SRC / "components" / "panels" / "ConvertPanel.svelte").read_text()
    assert "fetchDeletePlan(planInputs, duplicateAction)" in panel


def test_confirmation_modal_shows_referrer_changes():
    """Static check: the modal renders each item's planned Referrer edits, and
    the store asks for the plan with the chosen output folder."""
    modal = (_SRC / "components" / "modals" / "DeletePlanModal.svelte").read_text()
    assert "referrer_changes" in modal
    endpoints = (_SRC / "api" / "endpoints.js").read_text()
    assert "output_dir" in endpoints.split("getDeletePlan", 1)[1].split("},", 1)[0]
    store = (_SRC / "stores" / "conversion.svelte.js").read_text()
    assert "filePaths, this.mode, this.outputDir || null, duplicateAction" in store
