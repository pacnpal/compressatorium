"""CLI ``verify`` labels a file it could not check the way the batch does (#370).

#359 made a file the verifier can't open (EACCES) no verdict: its stored
check is kept everywhere. The CLI still printed ``failed`` and exited 1 for
it, while the web's batch verify says ``uncheckable``. A script reading the
CLI could not tell a corrupt file from an unreadable one.

The CLI now reads the result through the shared rule
(``verification_store.cannot_check``): a file that could not be checked
prints ``uncheckable`` with the reason and exits 4 (``EXIT_UNCHECKED``).
Real corruption still prints ``failed`` and exits 1, a missing file keeps
its usage exit 2, and a mixed run exits with the worst: 1 over 4 over 0.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from .test_unreadable_verify_359 import GOOD, _stored
from . import test_unreadable_verify_359 as unreadable_359

# The 359 tests' checked CHD and fake chdman, shared here.
_chd_fixture = unreadable_359._chd


def _verify(capsys, *paths) -> tuple[int, list[dict]]:
    import cli

    code = cli.main(["verify", *map(str, paths), "--json"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    return code, lines


@pytest.fixture(name="unreadable")
def _unreadable(chd):
    if os.geteuid() == 0:
        pytest.skip("root reads a chmod 000 file")
    chd.chmod(0)
    return chd


def test_an_unreadable_file_is_uncheckable_and_exits_4(unreadable, capsys):
    import cli

    code, lines = _verify(capsys, unreadable)

    assert [line["status"] for line in lines] == ["uncheckable"]
    assert "Permission denied" in lines[0]["reason"]
    assert code == cli.EXIT_UNCHECKED == 4
    assert _stored(unreadable) == "intact"


def test_a_corrupt_file_still_fails_and_exits_1(chd, capsys):
    import cli

    Path(chd).write_bytes(b"something else")

    code, lines = _verify(capsys, chd)

    assert [line["status"] for line in lines] == ["failed"]
    assert code == cli.EXIT_FAILED == 1


def test_a_missing_file_keeps_its_usage_exit(chd, capsys):
    import cli

    code, _lines = _verify(capsys, Path(chd).with_name("Gone.chd"))

    assert code == cli.EXIT_USAGE == 2


def test_a_check_past_the_commands_bound_is_uncheckable_and_exits_4(
    chd, capsys, monkeypatch,
):
    """The CLI's own timeout judged nothing (#351): no verdict, exit 4, and
    the stored check stays."""
    import asyncio

    import cli
    from services.tools import registry

    tool = registry.get("chdman")

    async def short(path, *, cancel_event=None):
        return 0.2

    async def slow(path, *, cancel_event=None):
        await asyncio.sleep(5)
        return {"valid": True, "message": "too late"}

    monkeypatch.setattr(tool, "verify_timeout", short)
    monkeypatch.setitem(vars(tool), "verify", slow)

    code, lines = _verify(capsys, chd)

    assert [line["status"] for line in lines] == ["uncheckable"]
    assert "timed out" in lines[0]["reason"]
    assert code == cli.EXIT_UNCHECKED
    assert _stored(chd) == "intact"


def test_a_missing_path_stops_the_run_before_anything_is_checked(chd, capsys):
    """Exit 2 is decided before any check: a corrupt file named with it is
    not checked, so it is never hidden behind a softer code."""
    import cli

    corrupt = Path(chd).parent / "Broken.chd"
    corrupt.write_bytes(b"something else")

    code, lines = _verify(capsys, corrupt, Path(chd).with_name("Gone.chd"))

    assert code == cli.EXIT_USAGE
    assert lines == []


def test_the_help_documents_the_exit_codes():
    import cli

    text = cli.build_parser().format_help()

    assert "4 verify failed nothing but could not check" in " ".join(text.split())


def _three(chd) -> tuple[Path, Path, Path]:
    """An intact, an unreadable and a corrupt CHD side by side."""
    folder = Path(chd).parent
    unreadable = folder / "Locked.chd"
    shutil.copy(chd, unreadable)
    corrupt = folder / "Broken.chd"
    corrupt.write_bytes(b"something else")
    assert Path(chd).read_bytes() == GOOD
    return Path(chd), unreadable, corrupt


def test_a_mixed_run_exits_with_the_worst_outcome(chd, capsys):
    """Failed (1) over could not check (4) over Intact (0)."""
    import cli

    if os.geteuid() == 0:
        pytest.skip("root reads a chmod 000 file")
    intact, unreadable, corrupt = _three(chd)
    unreadable.chmod(0)
    try:
        assert _verify(capsys, intact)[0] == cli.EXIT_OK
        code, lines = _verify(capsys, intact, unreadable)
        assert code == cli.EXIT_UNCHECKED
        assert sorted(line["status"] for line in lines) == ["intact", "uncheckable"]
        code, lines = _verify(capsys, intact, unreadable, corrupt)
        assert code == cli.EXIT_FAILED
        assert sorted(line["status"] for line in lines) == ["failed", "intact", "uncheckable"]
    finally:
        unreadable.chmod(0o644)
