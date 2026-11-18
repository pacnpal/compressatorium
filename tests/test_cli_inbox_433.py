"""`run-rules` makes one Inbox pass after its Rule sweep (#433).

The same pass the server's tick makes, under the same one-scheduler-per-data-
directory lock, so intake works from cron. A cron run is a new process each
time, so what intake has seen settling is kept between runs: a file seen by
one run is taken by the next once the settle time has passed. Each test
clears intake's memory between runs, as a new process would start.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from . import test_cli_run_rules_307 as rr
from .test_cli_run_rules_307 import RVZ_RULE, _cli
from .test_platform_detect import _gamecube

# The 307 fixtures, ``env`` and ``dolphin``, registered here too.
env_fixture = rr._env
dolphin_fixture = rr._dolphin


@pytest.fixture(name="folder")
def _folder(env, monkeypatch):
    """The real Library list with one folder Library, ``vol/ROMs``, fed by
    the Inbox ``vol/Downloads`` (settle time 0)."""
    import cli
    from services import inbox, libraries
    from services.inbox import intake

    monkeypatch.setattr(libraries, "all_libraries", env["real_libraries"])
    cli._open_db(False)
    inbox.reset_for_tests()
    intake.reset_for_tests()
    (env["vol"] / "ROMs").mkdir()
    (env["vol"] / "Downloads").mkdir()
    library = libraries.register(str(env["vol"] / "ROMs"))
    box = inbox.register(str(env["vol"] / "Downloads"), library.id, 0)
    yield {"library": library, "inbox": box, "vol": env["vol"]}
    intake.reset_for_tests()
    inbox.reset_for_tests()


@pytest.fixture(name="sent")
def _sent(monkeypatch) -> list:
    from services import notifications

    events: list = []
    monkeypatch.setattr(notifications, "notify", events.append)
    return events


def _new_process() -> None:
    """What a new ``run-rules`` process starts with: no intake memory."""
    from services import inbox
    from services.inbox import intake

    intake.reset_for_tests()
    inbox.reset_for_tests()


def _inbox_lines(lines: list[dict]) -> list[dict]:
    return [line for line in lines if "inbox" in line]


def _game(folder) -> Path:
    path = folder["vol"] / "Downloads" / "Sunshine.iso"
    _gamecube(path)
    return path


def test_run_rules_takes_a_file_seen_by_the_run_before(folder, dolphin, capsys):
    game = _game(folder)

    code, lines, err = _cli(capsys, "run-rules", "--json")
    assert code == 0, (lines, err)
    assert game.exists()  # seen once: not settled yet
    _new_process()

    code, lines, err = _cli(capsys, "run-rules", "--json")

    assert code == 0, (lines, err)
    placed = list((folder["vol"] / "ROMs").rglob("Sunshine.iso"))
    assert len(placed) == 1
    assert not game.exists()
    assert (folder["vol"] / "Downloads" / "processed" / "Sunshine.iso").exists()
    [line] = _inbox_lines(lines)
    assert line["library"] == folder["library"].id
    assert [Path(p).name for p in line["placed"]] == ["Sunshine.iso"]
    assert line["left"] == []


def test_a_conversion_the_inbox_queued_finishes_within_the_run(folder, dolphin, capsys):
    """The Rule for the Platform converts it; the run waits for the job and
    moves the original to processed/ before it exits."""
    from services.rules import sweep as rules_sweep

    asyncio.run(rules_sweep.set_rules(folder["library"], {"ngc": RVZ_RULE["gc"]}))
    game = _game(folder)
    _cli(capsys, "run-rules", "--json")
    _new_process()

    code, lines, err = _cli(capsys, "run-rules", "--json")

    assert code == 0, (lines, err)
    [output] = list((folder["vol"] / "ROMs").rglob("Sunshine.rvz"))
    assert output.read_bytes() == b"converted output"
    assert not game.exists()
    assert (folder["vol"] / "Downloads" / "processed" / "Sunshine.iso").exists()
    [line] = _inbox_lines(lines)
    assert [Path(p).name for p in line["placed"]] == ["Sunshine.rvz"]


def test_a_file_left_with_a_finding_is_reported_once(folder, dolphin, capsys, sent):
    mystery = folder["vol"] / "Downloads" / "Mystery.iso"
    mystery.write_bytes(b"\0" * 4096)

    _cli(capsys, "run-rules", "--json")
    _new_process()
    code, lines, _err = _cli(capsys, "run-rules", "--json")
    _new_process()
    _again, later, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0  # a file intake can't place is not a failed run
    [line] = _inbox_lines(lines)
    [left] = line["left"]
    assert Path(left["path"]).name == "Mystery.iso"
    assert "Platform" in left["reason"]
    assert mystery.exists()
    assert _inbox_lines(later) == []  # held: not reported again
    events = [e for e in sent if e.kind == "inbox_pass"]
    assert len(events) == 1
    assert "Mystery.iso" in events[0].body


def test_the_pass_sends_one_summary_and_nothing_when_idle(folder, dolphin, capsys, sent):
    _game(folder)

    _cli(capsys, "run-rules", "--json")
    assert [e for e in sent if e.kind == "inbox_pass"] == []  # only seen
    _new_process()
    _cli(capsys, "run-rules", "--json")

    [event] = [e for e in sent if e.kind == "inbox_pass"]
    assert event.data["placed"] == 1
    assert "Sunshine.iso" in event.body


def test_while_a_server_holds_the_lock_no_inbox_is_polled(folder, dolphin, capsys):
    from services import scheduler_lock

    game = _game(folder)
    server = scheduler_lock.acquire("server")
    try:
        for _ in range(2):
            code, lines, _err = _cli(capsys, "run-rules", "--json")
            assert (code, lines) == (3, [])
            _new_process()
    finally:
        server.release()
    _cli(capsys, "run-rules", "--json")  # the first run that holds the lock

    assert game.exists()
    assert not (folder["vol"] / "Downloads" / "processed").exists()


def test_a_dry_run_takes_nothing_from_an_inbox(folder, dolphin, capsys):
    game = _game(folder)

    for _ in range(2):
        code, lines, _err = _cli(capsys, "run-rules", "--dry-run", "--json")
        assert code == 0
        _new_process()

    assert game.exists()
    assert list((folder["vol"] / "ROMs").rglob("*.iso")) == []
    [line] = _inbox_lines(lines)
    assert (line["library"], line["status"], line["reason"]) == (
        folder["library"].id, "skipped", "dry_run",
    )


def test_an_inbox_that_cant_be_read_fails_the_run(folder, dolphin, capsys, sent):
    """Codex 4607809607: a poll that couldn't read its Inbox reports an
    error; run-rules prints it and exits 1, as for a failed sweep, and the
    pass summary says so."""
    import shutil

    shutil.rmtree(folder["vol"] / "Downloads")

    code, lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 1
    [line] = _inbox_lines(lines)
    assert line["library"] == folder["library"].id
    assert line["status"] == "error"
    assert "isn't there" in line["error"]
    [event] = [e for e in sent if e.kind == "inbox_pass"]
    assert "isn't there" in event.body


def test_the_same_outage_fails_each_run_but_notifies_once(folder, dolphin, capsys, sent):
    """Codex 4608095505: the Inbox's last error is kept with the settle
    memory, so a cron run that hits the same outage again prints it and exits
    1, but doesn't send the summary again."""
    import shutil

    shutil.rmtree(folder["vol"] / "Downloads")
    errors = []
    for _ in range(2):
        code, lines, _err = _cli(capsys, "run-rules", "--json")
        assert code == 1
        errors += [line["error"] for line in _inbox_lines(lines) if line["status"] == "error"]
        _new_process()

    assert len(errors) == 2
    assert "isn't there" in errors[0]
    assert len([e for e in sent if e.kind == "inbox_pass"]) == 1


def test_a_big_pass_settles_each_conversion_as_it_ends(folder, dolphin, capsys, monkeypatch):
    """Codex 4608095516: with a tiny job history, the jobs that ended first
    are pruned before the pass ends. Each conversion is settled as it ends
    (the run's terminal listener), so every original still moves to
    processed/ and none is "Lost track of"."""
    from services import findings
    from services.job_manager import job_manager
    from services.rules import sweep as rules_sweep

    monkeypatch.setattr(job_manager, "max_job_history", 2)
    monkeypatch.setattr(job_manager, "MAX_ARCHIVED_JOBS", 0)
    asyncio.run(rules_sweep.set_rules(folder["library"], {"ngc": RVZ_RULE["gc"]}))
    names = [f"Game {n}.iso" for n in range(7)]
    for name in names:
        _gamecube(folder["vol"] / "Downloads" / name)
    _cli(capsys, "run-rules", "--json")  # seen
    _new_process()

    code, _lines, err = _cli(capsys, "run-rules", "--json")

    assert code == 0, err
    processed = folder["vol"] / "Downloads" / "processed"
    assert sorted(p.name for p in processed.iterdir()) == sorted(names)
    assert len(list((folder["vol"] / "ROMs").rglob("*.rvz"))) == len(names)
    assert [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS] == []


def test_an_idle_inbox_exits_0_with_no_inbox_line(folder, dolphin, capsys, sent):
    for _ in range(2):
        code, lines, err = _cli(capsys, "run-rules", "--json")
        assert code == 0, err
        assert _inbox_lines(lines) == []
        _new_process()

    assert [e for e in sent if e.kind == "inbox_pass"] == []


def test_a_stop_mid_conversion_leaves_the_file_to_be_taken_next_time(
    folder, dolphin, capsys, monkeypatch,
):
    """A `docker stop` or Ctrl-C while the Inbox's conversion runs isn't the
    file's fault: no Finding, not held, and a later run takes it."""
    import os
    import signal

    from services import findings
    from services.rules import sweep as rules_sweep
    from services.tools import registry

    asyncio.run(rules_sweep.set_rules(folder["library"], {"ngc": RVZ_RULE["gc"]}))
    game = _game(folder)
    _cli(capsys, "run-rules", "--json")  # seen
    _new_process()

    runner = registry.get("dolphin")._service._runner
    real_run = runner.run

    async def stopped_mid_run(cmd, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        async for event in real_run(cmd, **kwargs):
            yield event

    dolphin["seconds"] = 30  # runs until it is cancelled
    monkeypatch.setattr(runner, "run", stopped_mid_run)
    code, _lines, _err = _cli(capsys, "run-rules", "--json")
    assert code == 1
    assert game.exists()
    assert [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS] == []
    _new_process()

    monkeypatch.setattr(runner, "run", real_run)
    dolphin["seconds"] = 0
    for _ in range(2):  # seen as new, then taken once it has settled again
        code, _lines, err = _cli(capsys, "run-rules", "--json")
        assert code == 0, err
        _new_process()

    assert not game.exists()
    assert len(list((folder["vol"] / "ROMs").rglob("Sunshine.rvz"))) == 1
    assert [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS] == []


def test_each_inbox_job_line_names_the_library_it_came_from(
    folder, dolphin, capsys, monkeypatch,
):
    """Codex 4607940252: a copy's job is made when its task runs, which can
    be while another Library's Inbox is being polled. Each intake job carries
    its Library and Inbox, and run-rules names the Library from that."""
    from services import inbox, libraries
    from services.inbox import intake
    from services.job_manager import job_manager

    vol = folder["vol"]
    (vol / "ROMs2").mkdir()
    (vol / "Downloads2").mkdir()
    second = libraries.register(str(vol / "ROMs2"))
    second_box = inbox.register(str(vol / "Downloads2"), second.id, 0)
    first_game = _gamecube(vol / "Downloads" / "First.iso")
    second_game = _gamecube(vol / "Downloads2" / "Second.iso")
    _cli(capsys, "run-rules", "--json")  # seen
    _new_process()

    second_polling = asyncio.Event()
    real_poll, real_copy = intake.poll, intake._copy

    async def poll(box, **kwargs):
        if box.id == second_box.id:
            second_polling.set()
        return await real_poll(box, **kwargs)

    async def late_copy(box, *args):
        if box.id == folder["inbox"].id:
            await second_polling.wait()  # its job is made during the next poll
        return await real_copy(box, *args)

    monkeypatch.setattr(intake, "poll", poll)
    monkeypatch.setattr(intake, "_copy", late_copy)
    code, lines, err = _cli(capsys, "run-rules", "--json")

    assert code == 0, err
    copies = {
        job_manager.jobs[line["path"].rsplit("/", 1)[-1]].filename: line["library"]
        for line in lines if line.get("mode") == "inbox_copy"
    }
    assert copies == {
        Path(first_game).name: folder["library"].id,
        Path(second_game).name: second.id,
    }
    assert {job.library_id for job in job_manager.jobs.values()
            if job.mode == "inbox_copy"} == {folder["library"].id, second.id}


def test_library_limits_the_inbox_pass_to_its_inboxes(folder, dolphin, capsys):
    game = _game(folder)

    for _ in range(2):
        code, _lines, _err = _cli(capsys, "run-rules", "--library", "romm", "--json")
        assert code == 0
        _new_process()

    assert game.exists()
