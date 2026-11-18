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


def test_library_limits_the_inbox_pass_to_its_inboxes(folder, dolphin, capsys):
    game = _game(folder)

    for _ in range(2):
        code, _lines, _err = _cli(capsys, "run-rules", "--library", "romm", "--json")
        assert code == 0
        _new_process()

    assert game.exists()
