"""A pending cancel is recorded even when the child's stdout ends before the
cancel watcher task has run.

The read loop can drain a child's stdout to EOF without ever yielding to the
event loop (a fast tool, or a pipe that already holds everything). When the
cancel event is already set, the runner then sends TERM itself through
``reap``. If the watcher has not run yet, it later sees an exited process and
never records the cancel, so a job the user cancelled was reported as a
failure ("return code -15") instead of ``ConversionCancelled``.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import subprocess_runner as runner_module
from app.services.subprocess_runner import ConversionCancelled, SubprocessRunner


class _InstantProcess:
    """A child whose stdout reads never suspend: one chunk, then EOF."""

    def __init__(self, chunks: list[bytes], *, exited_with: int | None = None):
        self.pid = 4243
        self._chunks = list(chunks)
        # None: still running at EOF. An int: already exited with that code.
        self.returncode = exited_with
        self.stdout = self
        self.signals: list[str] = []

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.signals.append("TERM")
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        self.signals.append("KILL")
        if self.returncode is None:
            self.returncode = -9


def _install(monkeypatch, process: _InstantProcess) -> None:
    async def fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", fake_exec)


async def _run(tmp_path, cancel_event: asyncio.Event) -> list[dict]:
    runner = SubprocessRunner(owner="test")
    return [
        update
        async for update in runner.run(
            ["fake-tool"],
            input_path=str(tmp_path / "in.bin"),
            output_path=str(tmp_path / "out.bin"),
            parse_progress=lambda _line: None,
            cancel_event=cancel_event,
            fail_label="faketool",
        )
    ]


def test_preset_cancel_is_recorded_when_stdout_ends_before_the_watcher_runs(
    tmp_path, monkeypatch,
):
    process = _InstantProcess([b"working\n"])
    _install(monkeypatch, process)
    cancel_event = asyncio.Event()
    cancel_event.set()

    with pytest.raises(ConversionCancelled):
        asyncio.run(_run(tmp_path, cancel_event))
    assert process.signals[:1] == ["TERM"]


def test_child_that_already_exited_cleanly_is_complete_despite_a_late_cancel(
    tmp_path, monkeypatch,
):
    """A child that finished before the cancel took effect keeps its output."""
    process = _InstantProcess([b"done\n"], exited_with=0)
    _install(monkeypatch, process)
    cancel_event = asyncio.Event()
    cancel_event.set()

    updates = asyncio.run(_run(tmp_path, cancel_event))

    assert updates[-1]["progress"] == 100
    assert process.signals == []
