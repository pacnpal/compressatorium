"""A verifier killed by a signal is no verdict on the file (#306).

A negative return code means the verifier was killed (SIGKILL, the OOM
killer), not that it judged the file: the run is inconclusive and a stored
pass is kept.
"""
from __future__ import annotations

import asyncio

from services.subprocess_runner import SubprocessRunner, collect_verify

KILL_SELF = ["sh", "-c", "kill -KILL $$"]


def test_a_one_shot_verifier_killed_by_a_signal_is_inconclusive(tmp_path):
    target = tmp_path / "Game.cso"
    target.write_bytes(b"x" * 64)
    runner = SubprocessRunner(owner="maxcso")

    result = asyncio.run(collect_verify(
        runner.capture_verify(KILL_SELF, path=str(target), success_message="ok"),
        fallback_message="failed",
    ))

    assert result["valid"] is False
    assert result.get("inconclusive") is True, result


def test_a_streaming_verifier_killed_by_a_signal_is_inconclusive(tmp_path):
    target = tmp_path / "Game.chd"
    target.write_bytes(b"x" * 64)
    runner = SubprocessRunner(owner="chdman")

    result = asyncio.run(collect_verify(
        runner.run_verify(
            KILL_SELF, path=str(target), parse_progress=lambda line: None,
            success_message="ok", failure_message="failed",
        ),
        fallback_message="failed",
    ))

    assert result["valid"] is False
    assert result.get("inconclusive") is True, result


def test_a_verifier_that_exits_non_zero_is_still_a_verdict(tmp_path):
    target = tmp_path / "Game.cso"
    target.write_bytes(b"x" * 64)
    runner = SubprocessRunner(owner="maxcso")

    result = asyncio.run(collect_verify(
        runner.capture_verify(["sh", "-c", "echo bad crc; exit 1"], path=str(target),
                              success_message="ok"),
        fallback_message="failed",
    ))

    assert result["valid"] is False
    assert "inconclusive" not in result
