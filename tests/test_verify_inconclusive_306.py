"""A check that could not finish is not a failed check (#306).

The shared verify machinery marks "no verdict" endings (a stall, its own
timeout, a stat or read error before the verifier ran) with ``inconclusive``,
next to ``cancelled`` and ``abandoned``. Only a conclusive failure may drop a
stored check.
"""
from __future__ import annotations

import asyncio

from services import subprocess_runner as runner_mod
from services.subprocess_runner import (
    SubprocessRunner,
    collect_verify,
    verify_preflight,
)


def test_verify_error_carries_the_inconclusive_flag():
    event = SubprocessRunner._verify_error("Verification stalled", inconclusive=True)
    assert event == {
        "type": "error", "valid": False, "message": "Verification stalled",
        "inconclusive": True,
    }
    assert "inconclusive" not in SubprocessRunner._verify_error("Integrity check failed")


def test_collect_verify_keeps_the_flag():
    async def stream():
        yield {"type": "progress", "progress": 0}
        yield SubprocessRunner._verify_error("Verification timed out", inconclusive=True)

    result = asyncio.run(collect_verify(stream(), fallback_message="failed"))
    assert result["inconclusive"] is True
    assert result["valid"] is False


def test_preflight_read_errors_are_inconclusive(tmp_path, monkeypatch):
    missing, _ = asyncio.run(verify_preflight(str(tmp_path / "gone.chd"), {".chd"}))
    assert missing["inconclusive"] is True

    def unreadable(path):
        raise PermissionError("permission denied")

    monkeypatch.setattr(runner_mod.os.path, "getsize", unreadable)
    error, _ = asyncio.run(verify_preflight(str(tmp_path / "x.chd"), {".chd"}))
    assert error["inconclusive"] is True


def test_preflight_verdicts_on_the_file_are_conclusive(tmp_path):
    empty = tmp_path / "empty.chd"
    empty.write_bytes(b"")
    wrong = tmp_path / "file.txt"
    wrong.write_bytes(b"x")
    for path in (empty, wrong):
        event, _ = asyncio.run(verify_preflight(str(path), {".chd"}))
        assert "inconclusive" not in event, event
