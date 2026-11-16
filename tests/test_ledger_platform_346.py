"""A Ledger row records the Platform its Source was detected as (#346).

A hand-started job (file browser, Auto, the CLI) has no Library to say what
its Image is, and a mode that serves several Platforms (chdman's createdvd)
cannot say either, so its row had no Platform and the savings breakdown put it
under "unsorted". The job now detects its Source's Platform before anything can
delete the Source (the shared detection Auto uses), and a Rule's job carries
its Library's Platform. Drives the real convert route and job manager; only the
tool's convert, post-convert and verify are stubbed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
from routes import convert as convert_routes
from services import db as _db
from services import ledger, platform_detect

from .test_cli_run_rules_307 import (  # the env and dolphin fixtures, by import
    RVZ_RULE,
    _cli,
    _dolphin,
    _env,
    _iso,
    _ledger_rows,
    _store_rules,
)
from .test_disc_id import _make_iso

__all__ = ["_dolphin", "_env"]


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch, db_path, reset_db_engine):
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    platform_detect.clear_cache()

    async def fake_convert(input_path, output_path, mode, *, compression=None,
                           split=False, cancel_event=None):
        Path(output_path).write_bytes(b"CHD")
        yield {"progress": 100, "message": "done"}

    async def noop_post_convert(input_path, output_path, mode):
        return None

    async def fake_verify(path, *, cancel_event=None):
        return {"valid": True, "message": "ok"}

    tool = convert_routes.registry.get("chdman")
    monkeypatch.setitem(vars(tool), "convert", fake_convert)
    monkeypatch.setitem(vars(tool), "post_convert", noop_post_convert)
    monkeypatch.setitem(vars(tool), "verify", fake_verify)
    _db.init_engine(db_path)
    # Not a Platform folder name, so only the Image's own header can place it.
    folder = tmp_path / "games"
    folder.mkdir()
    yield folder
    reset_db_engine()
    platform_detect.clear_cache()


async def _convert(path: Path):
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(path), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True, container_only=True,
    ))
    await convert_routes.job_manager._process_job(job.id)
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.deleted_paths, "the Source was deleted, so the row counts as saved"
    await ledger.record(job)
    return job


@pytest.mark.asyncio
async def test_a_hand_started_ps2_job_is_filed_under_ps2(volume):
    iso = volume / "Game.iso"
    iso.write_bytes(_make_iso({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_203.12;1\nVER = 1.00\n"}))

    job = await _convert(iso)

    assert job.platform == "ps2"
    [row] = await ledger.recent(1)
    assert row["platform"] == "ps2"
    savings = await ledger.savings()
    assert set(savings["by_platform"]) == {"ps2"}


@pytest.mark.asyncio
async def test_a_source_detection_cannot_place_stays_unsorted(volume):
    iso = volume / "Mystery.iso"
    iso.write_bytes(b"\0" * 64 * 1024)

    job = await _convert(iso)

    assert job.platform is None
    [row] = await ledger.recent(1)
    assert row["platform"] is None
    savings = await ledger.savings()
    assert set(savings["by_platform"]) == {"unsorted"}


def test_a_rule_job_is_filed_under_its_librarys_platform(env, dolphin, capsys):
    """A Rule's job takes its Library's Platform: here the Library files
    ``gc/Game.iso`` under ``wii``, which detection from the folder would call
    ``ngc``."""
    env["shelf"].slug = "wii"
    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0, lines
    assert [r["platform"] for r in _ledger_rows()] == ["wii"]
