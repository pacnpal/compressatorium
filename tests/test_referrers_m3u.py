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

from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
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
        {"referrer": "m3u", "target": str(volume / "Game.m3u")},
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permissions",
)
async def test_read_only_playlist_keeps_the_source(volume):
    playlist = volume / "Game.m3u"
    playlist.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        job = await _convert_disc_1(volume)
    finally:
        playlist.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.message == "converted, source kept: couldn't update Game.m3u"
    assert playlist.read_bytes() == M3U
    assert (volume / "Game (Disc 1).cue").is_file()
    assert (volume / "Game (Disc 1).bin").is_file()
    assert (volume / "Game (Disc 1).chd").is_file()
    assert job.deleted_paths == []
    row = ledger.row_from_job(job)
    assert row["source_deleted"] is False
    assert row["referrers_updated"] == []


@pytest.mark.asyncio
async def test_playlist_not_naming_the_disc_is_left_alone(volume):
    other = volume / "Other.m3u"
    other.write_bytes(b"Other Game.cue\n")

    await _convert_disc_1(volume)

    assert other.read_bytes() == b"Other Game.cue\n"
    assert not (volume / "Other.m3u.bak").exists()
