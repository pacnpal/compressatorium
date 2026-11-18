"""`run-rules` names an Inbox archive that waits for room, whichever check
held it back (#470).

#458 printed a ``waiting`` line for an archive whose unpack job's gate found
no room. The Inbox looks for room first (``_room_problem``, before any job is
made); an archive that check holds back made no job, so ``run-rules`` said
nothing about it. Both waits now come out of the Inbox pass as data and are
reported in one place: the same line, under the archive's own name, with the
room reason.

Each check is driven deterministically: another unpack's reservation is put
in place (``intake._slot_sizes``) so there is no room, and for the job's gate
the first look is told there is room.
"""
from __future__ import annotations

import zipfile

import pytest

from . import test_cli_inbox_433 as inbox_cli
from . import test_cli_run_rules_307 as rr
from .test_cli_run_rules_307 import _cli

env_fixture = rr._env
dolphin_fixture = rr._dolphin
folder_fixture = inbox_cli._folder

CAP = 100 * 1024
OTHER = 90 * 1024  # what another unpack holds: no room left for the archive


@pytest.fixture(name="no_room")
def _no_room(folder, monkeypatch):
    """One settled archive in the Inbox, and the unpack cap taken up by
    another archive's reservation."""
    from services.inbox import intake

    from config import settings

    monkeypatch.setattr(settings, "archive_inbox_max_unpacked_size", CAP)
    gba = folder["vol"] / "Downloads" / "gba"
    gba.mkdir()
    with zipfile.ZipFile(gba / "Waits.zip", "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("Waits.gba", b"rom" * 8192)
    yield intake
    with intake._slots_lock:
        intake._slot_sizes.pop(-470, None)


def _run_with_no_room(capsys, intake, *argv):
    _cli(capsys, "run-rules", "--json")  # seen
    inbox_cli._new_process()
    with intake._slots_lock:
        intake._slot_sizes[-470] = OTHER
    return _cli(capsys, "run-rules", *argv)


def _waiting(lines):
    return [line for line in lines if line.get("status") == "waiting"]


def test_an_archive_the_room_pre_check_holds_back_is_named(folder, dolphin, no_room, capsys):
    from services import findings
    from services.job_manager import job_manager

    code, lines, err = _run_with_no_room(capsys, no_room, "--json")

    assert code == 0, (lines, err)
    # The first look held it back: no job was made.
    assert [j for j in job_manager.jobs.values() if j.mode == "inbox_unpack"] == []
    [line] = _waiting(lines)
    assert line["path"] == "Waits.zip"
    assert line["library"] == folder["library"].id
    assert line["mode"] == "inbox_unpack"
    assert line["reason"].startswith("No room to unpack it yet: ")
    assert "CHD_ARCHIVE_INBOX_MAX_UNPACKED_SIZE" in line["reason"]
    assert not [line for line in lines if line.get("status") == "failed"]
    assert [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS] == []


def test_the_pre_check_wait_is_named_in_text_mode(folder, dolphin, no_room, capsys):
    _cli(capsys, "run-rules", "--json")  # seen
    inbox_cli._new_process()
    with no_room._slots_lock:
        no_room._slot_sizes[-470] = OTHER
    import cli

    code = cli.main(["run-rules"])
    out = capsys.readouterr().out

    assert code == 0, out
    [said] = [text for text in out.splitlines() if text.startswith("waiting")]
    assert f"{folder['library'].id}: Waits.zip: No room to unpack it yet: " in said


def _line_from_each_check(capsys, intake, monkeypatch, check):
    if check == "job_gate":
        # The first look finds room, so the unpack job's own gate refuses it.
        monkeypatch.setattr(intake, "_room_problem", lambda _path: None)
    _code, lines, _err = _run_with_no_room(capsys, intake, "--json")
    return lines


@pytest.mark.parametrize("check", ["pre_check", "job_gate"])
def test_either_check_gives_one_line_of_the_same_shape(
    folder, dolphin, no_room, capsys, monkeypatch, check,
):
    from services.job_manager import job_manager

    lines = _line_from_each_check(capsys, no_room, monkeypatch, check)

    [line] = _waiting(lines)  # once, never twice
    unpack_jobs = [j for j in job_manager.jobs.values() if j.mode == "inbox_unpack"]
    assert len(unpack_jobs) == (1 if check == "job_gate" else 0)
    assert {k: line[k] for k in ("library", "path", "mode", "status")} == {
        "library": folder["library"].id, "path": "Waits.zip",
        "mode": "inbox_unpack", "status": "waiting",
    }
    assert line["reason"].startswith("No room to unpack it yet: ")
    assert set(line) == {
        "library", "path", "status", "platform", "mode", "output", "check",
        "source_deleted", "reason", "delete_source", "detection", "compression",
    }
