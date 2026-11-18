"""`run-rules`'s Inbox pass with archives (#432, Codex 4608417841).

An archive's Images are taken on by the unpack job that readied them, so a
conversion can be queued after the pass's first drain of the job queue. The
pass drains intake and the queue to a fixed point before it finishes, so the
run never exits with work still queued.
"""
from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

from . import test_cli_inbox_433 as inbox_433
from . import test_cli_run_rules_307 as rr
from .test_cli_run_rules_307 import RVZ_RULE, _cli
from .test_platform_detect import _gamecube

env_fixture = rr._env
dolphin_fixture = rr._dolphin
folder_fixture = inbox_433._folder
_new_process = inbox_433._new_process
_inbox_lines = inbox_433._inbox_lines


@pytest.fixture(autouse=True)
def _work_area(tmp_path, monkeypatch):
    """The app's own work area, where archives are unpacked."""
    from config import settings

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(settings, "temp_dir", str(work))
    return work


def test_one_run_converts_what_it_unpacks_and_moves_the_archive(
    folder, dolphin, capsys, _work_area,
):
    """A zip holding a GameCube disc, under a conversion Rule: the run that
    takes it unpacks it, converts the disc, and moves the zip to processed/
    before it exits."""
    from services.rules import sweep as rules_sweep

    asyncio.run(rules_sweep.set_rules(folder["library"], {"ngc": RVZ_RULE["gc"]}))
    disc = folder["vol"] / "Sunshine.iso"
    _gamecube(disc)
    archive = folder["vol"] / "Downloads" / "Sunshine.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.write(disc, "Sunshine.iso")
    disc.unlink()
    _cli(capsys, "run-rules", "--json")  # seen once: not settled yet
    _new_process()

    code, lines, err = _cli(capsys, "run-rules", "--json")

    assert code == 0, (lines, err)
    [output] = list((folder["vol"] / "ROMs").rglob("Sunshine.rvz"))
    assert output.read_bytes() == b"converted output"
    assert not archive.exists()
    assert (folder["vol"] / "Downloads" / "processed" / "Sunshine.zip").exists()
    assert list(Path(_work_area).glob("inbox_unpack_*")) == []
    [line] = _inbox_lines(lines)
    assert [Path(p).name for p in line["placed"]] == ["Sunshine.rvz"]
