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
def _work_area(tmp_path, monkeypatch, env):
    """The app's own work area, where archives are unpacked. After ``env``,
    which sets a work area of its own (CodeRabbit 4608968273), so the
    cleanup check reads the folder extraction really used."""
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


def test_a_stop_cancels_a_job_intake_queued_after_the_last_cancel(monkeypatch):
    """CodeRabbit 4608968227: under a stop, a conversion intake queues while
    its own work drains (after the queue was cancelled and drained) is
    cancelled and drained too before finish_all, so the run never ends with
    a live job."""
    import cli
    from services.inbox import intake, report

    state = cli.RunState(stopping=True)
    queued: list[str] = []
    cancels: list[str] = []
    seen_by_finish: list[bool] = []

    class Jobs:
        jobs: dict = {}

        async def cancel_all_jobs(self, *, reason):
            cancels.append(reason)
            queued.clear()

        async def drain(self):
            return None

        def has_live_conversions(self):
            return bool(queued)

    async def poll_all(**_kwargs):
        return []

    async def intake_drain():
        queued.append("late")  # an unpack's take-on queues as the stop lands

    async def finish_all(**_kwargs):
        seen_by_finish.append(bool(queued))
        return []

    async def inboxes_of(_chosen):
        return [type("Box", (), {"id": 1, "library_id": "lib"})()]

    async def nothing(*_args, **_kwargs):
        return None

    async def no_memory(_key):
        return None

    from services.preferences_store import preferences_store

    monkeypatch.setattr(cli, "_inboxes_of", inboxes_of)
    monkeypatch.setattr(intake, "poll_all", poll_all)
    monkeypatch.setattr(intake, "drain", intake_drain)
    monkeypatch.setattr(intake, "busy", lambda: False)
    monkeypatch.setattr(intake, "finish_all", finish_all)
    monkeypatch.setattr(intake, "restore_settle_memory", lambda _memory: None)
    monkeypatch.setattr(intake, "settle_memory", lambda _ids: {})
    monkeypatch.setattr(preferences_store, "get", no_memory)
    monkeypatch.setattr(preferences_store, "put", nothing)
    monkeypatch.setattr(report, "after_pass", nothing, raising=False)

    printer = type("P", (), {"summary": lambda *_a, **_k: None,
                             "line": lambda *_a, **_k: None})()
    try:
        asyncio.run(cli._inbox_pass([], printer, state, Jobs()))
    except Exception:
        pass  # what runs after finish_all isn't this test's concern

    assert seen_by_finish == [False]
    assert len(cancels) >= 2
