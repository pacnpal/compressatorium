"""`run-rules` names every external job by its file (#463).

#458 named a waiting Inbox archive by its file. An external job (one with no
real input path, ``/__external_jobs__/<id>``) is now named by its own file on
every status line, whatever its result: an unpack, a copy, a failure.
"""
from __future__ import annotations

import zipfile
from datetime import datetime, timezone

import pytest

from . import test_cli_inbox_433 as inbox_cli
from . import test_cli_run_rules_307 as rr
from .test_cli_run_rules_307 import _cli

env_fixture = rr._env
dolphin_fixture = rr._dolphin
folder_fixture = inbox_cli._folder


def _job(mode, status):
    from models import ConversionJob

    return ConversionJob(
        id="abcd1234", file_path="/__external_jobs__/abcd1234", filename="Game.zip",
        mode=mode, status=status, created_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("mode", ["inbox_unpack", "inbox_copy", "patch"])
def test_an_external_job_is_named_by_its_file_whatever_its_result(mode, status):
    import cli

    job = _job(mode, status)
    item = cli.Item(path=job.file_path, mode=mode)
    cli._finish(item, job)

    assert item.path == "Game.zip"
    assert item.status == status


def test_a_conversion_keeps_its_own_path():
    import cli

    job = _job("createdvd", "completed").model_copy(update={"file_path": "/vol/Game.iso"})
    item = cli.Item(path=job.file_path, mode="createdvd")
    cli._finish(item, job)

    assert item.path == "/vol/Game.iso"


def test_run_rules_names_unpack_and_copy_jobs_by_their_file(folder, dolphin, capsys):
    gba = folder["vol"] / "Downloads" / "gba"
    gba.mkdir()
    with zipfile.ZipFile(gba / "Named Me.zip", "w") as zf:
        zf.writestr("Named Me.gba", b"rom" * 1000)
    _cli(capsys, "run-rules", "--json")  # seen
    inbox_cli._new_process()

    code, lines, err = _cli(capsys, "run-rules", "--json")

    assert code == 0, (lines, err)
    jobs = [line for line in lines if line.get("mode") in ("inbox_unpack", "inbox_copy")]
    assert jobs, lines
    for line in jobs:
        assert "__external_jobs__" not in line["path"], line
    unpacks = [line for line in jobs if line["mode"] == "inbox_unpack"]
    assert [line["path"] for line in unpacks] == ["Named Me.zip"]
