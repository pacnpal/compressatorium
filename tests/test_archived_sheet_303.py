"""Archived track sheets (PR #320, Codex 4576751005).

A ``.cue`` / ``.gdi`` inside an archive is extracted with its tracks into the
job's own temp folder, which is not a Volume. The track check (#303) must take
that extraction folder as its containment root: tracks inside it are fine, and
an absolute path or a ``..`` reference is still refused. A sheet in a Volume
keeps Volume containment, and there is never a "no root" that allows anything.

The temp folder sits outside the Volume here, as in a real deployment, which
is what the regression needed to show.
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
from app.routes import convert as convert_routes


@pytest.fixture(name="env")
def _env(tmp_path: Path, monkeypatch):
    volume = tmp_path / "vol"
    volume.mkdir()
    temp = tmp_path / "temp"  # outside the Volume, as deployed
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(volume))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(volume))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(temp))
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
    return {"volume": volume, "calls": calls}


async def _convert_member(volume: Path, members: dict[str, bytes], sheet: str):
    archive = volume / "set.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=f"{archive}::{sheet}",
        mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=False,
    ))
    await convert_routes.job_manager._process_job(job.id)
    return job


@pytest.mark.asyncio
async def test_an_archived_cue_and_its_bin_convert(env) -> None:
    job = await _convert_member(env["volume"], {
        "Game.cue": b'FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n',
        "Game.bin": b"\0" * 4096,
    }, "Game.cue")

    assert job.status.value == JobStatus.COMPLETED.value, job.error_message
    assert len(env["calls"]) == 1
    assert "Game.bin" in env["calls"][0]["files"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["/etc/hosts", "../x.bin"])
async def test_an_archived_cue_naming_a_file_outside_its_folder_is_refused(
    env, reference: str,
) -> None:
    job = await _convert_member(env["volume"], {
        "Game.cue": f'FILE "{reference}" BINARY\n'.encode(),
        "x.bin": b"\0" * 64,
    }, "Game.cue")

    assert job.status.value == JobStatus.FAILED.value
    assert env["calls"] == []


def test_a_sheet_in_a_volume_keeps_volume_rules(env, tmp_path: Path) -> None:
    from utils.path_utils import source_companions_are_safe

    volume = env["volume"]
    (volume / "Inside.bin").write_bytes(b"\0" * 64)
    (volume / "Good.cue").write_text('FILE "Inside.bin" BINARY\n')
    (tmp_path / "secret.bin").write_bytes(b"secret")
    (volume / "Out.cue").write_text('FILE "../secret.bin" BINARY\n')

    assert source_companions_are_safe(str(volume / "Good.cue"), "createcd") is True
    assert source_companions_are_safe(str(volume / "Out.cue"), "createcd") is False


def test_an_extraction_root_confines_tracks_to_itself(tmp_path: Path) -> None:
    """The explicit root replaces the Volumes; it never widens to "anything"."""
    from utils.delete_plan import sheet_reference_problems

    root = tmp_path / "job-temp"
    root.mkdir()
    (root / "Game.bin").write_bytes(b"\0")
    (root / "Game.cue").write_text('FILE "Game.bin" BINARY\n')
    (tmp_path / "secret.bin").write_bytes(b"secret")
    (root / "Link.bin").symlink_to(tmp_path / "secret.bin")
    (root / "Link.cue").write_text('FILE "Link.bin" BINARY\n')

    assert sheet_reference_problems(str(root / "Game.cue"), within=[str(root)]) == []
    assert sheet_reference_problems(str(root / "Link.cue"), within=[str(root)]) != []


def test_an_empty_root_list_trusts_nothing(env) -> None:
    """``within=[]`` is a caller bug, refused by the shared resolver."""
    from utils.path_utils import source_companions_are_safe

    volume = env["volume"]
    (volume / "Inside.bin").write_bytes(b"\0" * 64)
    (volume / "Good.cue").write_text('FILE "Inside.bin" BINARY\n')

    assert source_companions_are_safe(str(volume / "Good.cue"), "createcd", within=[]) is False


@pytest.mark.asyncio
async def test_the_worker_hands_one_root_list_to_the_gate_and_post_convert(
    env, monkeypatch,
) -> None:
    """One ``within`` in ``_process_job``: the safety gate and ``post_convert``
    get the same list, ``[the job's extraction dir]``, for an archive member."""
    from services import job_manager as jm_mod

    seen: dict[str, object] = {}
    real_gate = jm_mod.source_companions_are_safe

    def gate(file_path, mode, *, within=None):
        seen["gate"] = within
        seen["input"] = file_path
        return real_gate(file_path, mode, within=within)

    async def post_convert(input_path, output_path, mode, *, within=None):
        seen["post_convert"] = within

    monkeypatch.setattr(jm_mod, "source_companions_are_safe", gate)
    for tool in convert_routes.registry.all():
        monkeypatch.setattr(tool, "post_convert", post_convert)

    job = await _convert_member(env["volume"], {
        "Game.cue": b'FILE "Game.bin" BINARY\n',
        "Game.bin": b"\0" * 4096,
    }, "Game.cue")

    assert job.status.value == JobStatus.COMPLETED.value, job.error_message
    roots = seen["gate"]
    assert isinstance(roots, list) and len(roots) == 1
    assert os.path.dirname(str(seen["input"])) == roots[0]
    assert seen["post_convert"] is roots
