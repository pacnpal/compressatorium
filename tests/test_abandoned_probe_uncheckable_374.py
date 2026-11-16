"""A verify that ran out of time is no verdict, marked at the source (#374).

Opening an output that never answers made ``_probe_for_verify`` return
``abandoned`` without ``inconclusive``, so ``cannot_check`` was false: the
CLI exited 1 ``failed`` and the batch said ``failed`` ("File stopped
responding"), though the record was kept. A probe, or a verifier, that ran
out of time judged nothing: every producer of such a result now marks it
``inconclusive`` itself, so every consumer (``cannot_check``, the batch, the
file menu, the CLI's exit 4, the check Rules) agrees with no special case.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from services.tools import registry

from . import test_unreadable_verify_359 as unreadable_359
from .test_unreadable_verify_359 import _stored

# #359's checked CHD and fake chdman, shared here.
_chd_fixture = unreadable_359._chd


@pytest.fixture(name="hung")
def _hung(chd, monkeypatch):
    """Opening the CHD to verify it never answers (a share gone silent)."""
    from services import subprocess_runner

    release = threading.Event()
    real = subprocess_runner._open_for_verify

    def blocked(path):
        if str(path) == str(chd):
            release.wait(10)
        return real(path)

    monkeypatch.setattr(subprocess_runner, "_STAT_TIMEOUT", 0.2)
    monkeypatch.setattr(subprocess_runner, "_open_for_verify", blocked)
    yield chd
    release.set()


def test_the_cli_reports_a_probe_that_never_answered_as_uncheckable(hung, capsys):
    import cli

    code = cli.main(["verify", str(hung), "--json"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert [line["status"] for line in lines] == ["uncheckable"]
    assert "stopped responding" in lines[0]["reason"]
    assert _stored(hung) == "intact"
    # Something is still reading that storage, so the run stops as before
    # (`stops_verify_batch`), but nothing was judged: exit 4, not 1.
    assert code == cli.EXIT_UNCHECKED


def test_the_batch_counts_a_probe_that_never_answered_as_uncheckable(hung):
    from routes import info as info_routes

    async def read_all():
        token = await info_routes.workload_limiter.acquire("verify")
        stream = info_routes._sse_batch_from_verify_stream(
            registry.get("chdman"), info_routes._VERIFY_CONFIG["chdman"], [str(hung)], token,
        ).body_iterator
        return [event async for event in stream]

    events = asyncio.run(read_all())

    done = [
        json.loads(event["data"]) for event in events
        if event["event"] == "verify_batch_file_complete"
    ]
    assert [line["status"] for line in done] == ["uncheckable"]
    assert done[0]["failed"] == 0
    assert _stored(hung) == "intact"


def test_every_ran_out_of_time_result_is_marked_at_the_source():
    """The other producers the sweep found: a verifier that outlived SIGKILL,
    and the verify routes' own timeout."""
    from routes import info as info_routes
    from services.subprocess_runner import abandoned_verify_error
    from services.verification_store import cannot_check

    assert cannot_check(abandoned_verify_error(4242))
    assert cannot_check(info_routes._verify_timed_out(30))


def test_a_corrupt_file_still_fails_and_exits_1(chd, capsys):
    import cli

    chd.write_bytes(b"something else")

    code = cli.main(["verify", str(chd), "--json"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert [line["status"] for line in lines] == ["failed"]
    assert code == cli.EXIT_FAILED
