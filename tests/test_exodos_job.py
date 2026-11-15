"""An eXoDOS repack run end to end: POST /jobs -> plan_job -> job_manager.

The real service on a synthetic tree, nothing stubbed: the tool's final
caveat must survive onto the finished job as ``job.warning``, where the job
list and the UI read it, instead of vanishing with the last progress message.
"""
from __future__ import annotations

import zipfile

import pytest

from app.models import ConversionMode, DuplicateAction, JobCreateRequest
from app.routes import convert as convert_routes

TEMPLATE = b"[autoexec]\r\nmount c .\\eXoDOS\\Abaron\r\nc:\r\nABARON.EXE\r\n"


@pytest.fixture(name="tree")
def _tree(tmp_path, monkeypatch, db_path, reset_db_engine):
    from services import db as _db

    _db.init_engine(db_path)
    dos = tmp_path / "!dos"
    (dos / "Abaron").mkdir(parents=True)
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    monkeypatch.setattr(convert_routes.settings, "exodos_dos_dir", str(dos))
    game = tmp_path / "Abaron (1995).zip"
    with zipfile.ZipFile(game, "w") as zf:
        zf.writestr("Abaron/ABARON.EXE", b"exe")
    yield {"template": dos / "Abaron" / "dosbox.conf", "game": game}
    reset_db_engine()


async def _run(game):
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(game),
        mode=ConversionMode.EXODOS_REPACK,
        duplicate_action=DuplicateAction.SKIP,
    ))
    await convert_routes.job_manager._process_job(job.id)
    return job


@pytest.mark.asyncio
async def test_the_repack_caveat_stays_on_the_completed_job(tree):
    tree["template"].write_bytes(TEMPLATE + b"imgmount e .\\eXoDOS\\Other\\X.CUE -t cdrom\r\n")

    job = await _run(tree["game"])

    assert job.status.value == "completed", job.error_message
    assert job.warning is not None
    assert "imgmount e .\\eXoDOS\\Other\\X.CUE" in job.warning
    assert job.model_dump()["warning"] == job.warning


@pytest.mark.asyncio
async def test_a_clean_repack_has_no_warning(tree):
    tree["template"].write_bytes(TEMPLATE)

    job = await _run(tree["game"])

    assert job.status.value == "completed", job.error_message
    assert job.warning is None
