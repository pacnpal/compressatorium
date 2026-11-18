"""A bound track sheet (#431, Codex 4606794168).

An Inbox conversion reads its Source from a folder of its own in the app's
work area (``bind_source``), which is outside every Volume as deployed. Like an
archive member's extraction folder (#303), that folder is the trusted root the
sheet's tracks are checked against: a ``.cue`` bound with its tracks converts,
and a reference that leaves the bound folder is still refused.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.models import ConversionMode, JobStatus
from app.routes import convert as convert_routes


@pytest.fixture(name="env")
def _env(tmp_path: Path, monkeypatch):
    volume = tmp_path / "vol"
    volume.mkdir()
    work = tmp_path / "work"  # the app's work area, outside the Volume
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(volume))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(volume))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(work))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    calls: list[dict] = []

    async def fake_convert(input_path, output_path, mode, *, compression=None,
                           split=False, cancel_event=None):
        folder = os.path.dirname(input_path)
        calls.append({"input": input_path, "files": sorted(os.listdir(folder))})
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        Path(output_path).write_bytes(b"CHD")
        yield {"progress": 100, "message": "done"}

    async def noop_post_convert(input_path, output_path, mode, *, within=None):
        return None

    for tool in convert_routes.registry.all():
        monkeypatch.setattr(tool, "convert", fake_convert)
        monkeypatch.setattr(tool, "post_convert", noop_post_convert)
    return {"volume": volume, "work": work, "calls": calls}


async def _convert_bound(
    volume: Path, files: dict[str, bytes], sheet: str, members: list[str] | None = None,
):
    """Write *files* into an Inbox's ``ps1`` folder (beside any already
    there), queue *sheet* bound with its *members* (by default every other
    file written), and run it."""
    from services.job_manager import job_manager
    from utils import path_utils

    inbox = volume / "Downloads"
    folder = inbox / "ps1"
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    game = str(folder / sheet)
    identity = path_utils.source_identity(game, str(inbox))
    names = members if members is not None else [n for n in files if n != sheet]
    identity["members"] = {
        name: path_utils.source_identity(str(folder / name), str(inbox))["snapshot"]
        for name in names
    }
    out = volume / "ROMs"
    out.mkdir()
    [job] = await job_manager.create_batch_jobs(
        [game], ConversionMode.CREATECD, output_dir=str(out),
        source_identities={game: identity},
    )
    await job_manager._process_job(job.id)
    return job_manager.jobs[job.id]


@pytest.mark.asyncio
async def test_a_bound_cue_and_its_tracks_convert_outside_every_volume(env) -> None:
    job = await _convert_bound(env["volume"], {
        "Game.cue": b'FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n',
        "Game.bin": b"\0" * 4096,
    }, "Game.cue")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert len(env["calls"]) == 1
    assert Path(env["calls"][0]["input"]).resolve().is_relative_to(env["work"].resolve())
    assert env["calls"][0]["files"] == ["Game.bin", "Game.cue"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["/etc/hosts", "../x.bin"])
async def test_a_bound_cue_naming_a_file_outside_its_folder_is_refused(
    env, reference: str,
) -> None:
    job = await _convert_bound(env["volume"], {
        "Game.cue": f'FILE "{reference}" BINARY\n'.encode(),
    }, "Game.cue")

    assert job.status == JobStatus.FAILED
    assert env["calls"] == []


@pytest.mark.asyncio
async def test_two_track_names_linked_to_one_file_convert_cleanly(env) -> None:
    """CodeRabbit 4606887346, ruled: binding the second of two names of one
    inode changes its ctime; the baselines are taken once every name is
    bound, so the Source isn't taken for changed and the job converts."""
    volume = env["volume"]
    folder = volume / "Downloads" / "ps1"
    folder.mkdir(parents=True)
    (folder / "Track1.bin").write_bytes(b"\0" * 4096)
    os.link(folder / "Track1.bin", folder / "Track2.bin")

    job = await _convert_bound(volume, {
        "Game.cue": (
            b'FILE "Track1.bin" BINARY\n  TRACK 01 MODE1/2352\n'
            b'FILE "Track2.bin" BINARY\n  TRACK 02 AUDIO\n'
        ),
    }, "Game.cue", members=["Track1.bin", "Track2.bin"])

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert env["calls"][0]["files"] == ["Game.cue", "Track1.bin", "Track2.bin"]
