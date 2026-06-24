"""Direct tests for ``SubprocessRunner``, the shared subprocess loop that
``chdman`` and ``dolphin_tool`` now delegate their ``convert()`` to.

These drive the highest-risk paths (cancel, stall timeout, non-zero exit) with
a real child process spawned via ``sys.executable`` so the spawn / line-buffer
/ cancel-watcher / finalize machinery is exercised end to end, since the real
CLIs are unavailable in the sandbox.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

import pytest

from app.services import subprocess_runner as runner_module
from app.services.subprocess_runner import ConversionCancelled, SubprocessRunner


def _parse_pct(line: str) -> int | None:
    match = re.search(r"(\d+)\s*%", line)
    return int(match.group(1)) if match else None


def _py_cmd(script: str) -> list[str]:
    return [sys.executable, "-u", "-c", script]


async def _drain(gen) -> list[dict]:
    return [update async for update in gen]


def test_happy_path_streams_progress_then_final_100(tmp_path):
    out = tmp_path / "out.bin"
    script = (
        "import sys\n"
        "for p in (10, 50, 90):\n"
        "    sys.stdout.write(f'Progress {p}%\\n')\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(out),
                parse_progress=_parse_pct,
                fail_label="testproc",
            )
        )
    )

    progresses = [u["progress"] for u in updates]
    assert progresses[:3] == [10, 50, 90]
    assert updates[-1] == {"progress": 100, "message": "Conversion complete"}
    assert not runner.active_pids()


def test_nonzero_exit_raises_runtimeerror_with_tail(tmp_path):
    script = (
        "import sys\n"
        "sys.stdout.write('starting\\n')\n"
        "sys.stdout.write('boom error\\n')\n"
        "sys.exit(3)\n"
    )
    runner = SubprocessRunner(owner="test")

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            _drain(
                runner.run(
                    _py_cmd(script),
                    input_path=str(tmp_path / "in.bin"),
                    output_path=str(tmp_path / "out.bin"),
                    parse_progress=_parse_pct,
                    fail_label="testproc",
                )
            )
        )

    message = str(excinfo.value)
    assert "testproc failed with return code 3" in message
    assert "boom error" in message
    assert not runner.active_pids()


def test_cancel_event_raises_conversion_cancelled(tmp_path):
    script = (
        "import sys, time\n"
        "sys.stdout.write('working 10%\\n')\n"
        "time.sleep(30)\n"
    )
    runner = SubprocessRunner(owner="test")
    cancel_event = asyncio.Event()

    async def _run():
        gen = runner.run(
            _py_cmd(script),
            input_path=str(tmp_path / "in.bin"),
            output_path=str(tmp_path / "out.bin"),
            parse_progress=_parse_pct,
            cancel_event=cancel_event,
            fail_label="testproc",
        )
        async for update in gen:
            if "10%" in update["message"]:
                cancel_event.set()

    with pytest.raises(ConversionCancelled):
        asyncio.run(_run())
    assert not runner.active_pids()


def test_stall_timeout_raises_runtimeerror(tmp_path, monkeypatch):
    # Force a short stall window; the child emits one line then goes silent and
    # never grows the output file, so the stall watchdog must fire.
    monkeypatch.setattr(
        runner_module, "compute_progress_stall_timeout", lambda **_: 1,
    )
    script = (
        "import sys, time\n"
        "sys.stdout.write('starting\\n')\n"
        "time.sleep(30)\n"
    )
    runner = SubprocessRunner(owner="test")

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            _drain(
                runner.run(
                    _py_cmd(script),
                    input_path=str(tmp_path / "in.bin"),
                    output_path=str(tmp_path / "out.bin"),
                    parse_progress=_parse_pct,
                    fail_label="testproc",
                )
            )
        )

    assert "Conversion stalled" in str(excinfo.value)
    assert not runner.active_pids()


def test_size_progress_emits_from_output_growth(tmp_path):
    # A tool whose CLI prints no parseable percent: progress must come from the
    # growing output file via the size_progress hook, ending at the runner's
    # terminal 100%. The child writes the output file then a stdout line each
    # step, so the post-line size tick fires without waiting on the read timeout.
    out = tmp_path / "out.bin"
    script = (
        "import sys\n"
        f"out = {str(out)!r}\n"
        "with open(out, 'wb') as f:\n"
        "    for i in range(3):\n"
        "        f.write(b'x' * 100); f.flush()\n"
        "        sys.stdout.write(f'step {i}\\n'); sys.stdout.flush()\n"
    )
    runner = SubprocessRunner(owner="test")

    def _size_progress(size: int) -> dict:
        return {
            "progress": runner_module.output_size_progress(size, 1000),
            "message": f"sz {size}",
        }

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(out),
                parse_progress=lambda _line: None,
                size_progress=_size_progress,
                fail_label="testproc",
            )
        )
    )

    size_updates = [u for u in updates if u["message"].startswith("sz ")]
    assert size_updates, "expected at least one size-based progress update"
    # The estimate matches the shared helper (100 B against expected 1000 -> 14%)
    # and the emitted bar never goes backwards.
    assert max(u["progress"] for u in size_updates) >= 14
    progresses = [u["progress"] for u in updates]
    assert progresses == sorted(progresses)
    assert updates[-1] == {"progress": 100, "message": "Conversion complete"}
    assert not runner.active_pids()


def test_env_is_forwarded_to_subprocess(tmp_path):
    script = (
        "import os, sys\n"
        "sys.stdout.write('VAL=' + os.environ.get('CMP_RUNNER_TEST', 'unset') + '\\n')\n"
    )
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(tmp_path / "out.bin"),
                parse_progress=lambda _line: None,
                env={**os.environ, "CMP_RUNNER_TEST": "from-env"},
                fail_label="testproc",
            )
        )
    )

    messages = " ".join(u["message"] for u in updates)
    assert "VAL=from-env" in messages
    assert not runner.active_pids()


def test_nice_via_wrapper_runs_without_preexec(tmp_path):
    # With nice_via_wrapper the renice is the caller's command-wrapper concern,
    # so run() skips preexec_fn; the conversion must still complete normally.
    out = tmp_path / "out.bin"
    script = "import sys; sys.stdout.write('done 50%\\n')"
    runner = SubprocessRunner(owner="test")

    updates = asyncio.run(
        _drain(
            runner.run(
                _py_cmd(script),
                input_path=str(tmp_path / "in.bin"),
                output_path=str(out),
                parse_progress=_parse_pct,
                nice_via_wrapper=True,
                fail_label="testproc",
            )
        )
    )

    assert 50 in [u["progress"] for u in updates]
    assert updates[-1] == {"progress": 100, "message": "Conversion complete"}
    assert not runner.active_pids()


# ---------------------------------------------------------------------------
# run_capture  (shared one-shot capture with cancel + timeout)
# ---------------------------------------------------------------------------


def test_run_capture_returns_output_and_returncode():
    runner = SubprocessRunner(owner="test")
    rc, stdout, stderr = asyncio.run(
        runner.run_capture(_py_cmd("import sys; sys.stdout.write('hello')")),
    )
    assert rc == 0
    assert stdout == b"hello"
    assert stderr == b""
    assert not runner.active_pids()


def test_run_capture_nonzero_returncode():
    runner = SubprocessRunner(owner="test")
    rc, _stdout, _stderr = asyncio.run(
        runner.run_capture(_py_cmd("import sys; sys.exit(3)")),
    )
    assert rc == 3
    assert not runner.active_pids()


def test_run_capture_cancel_returns_none_and_terminates():
    runner = SubprocessRunner(owner="test")

    async def go():
        cancel = asyncio.Event()
        cancel.set()  # already cancelled: must abort before the sleep elapses
        return await runner.run_capture(
            _py_cmd("import time; time.sleep(30)"), cancel_event=cancel,
        )

    rc, _stdout, _stderr = asyncio.run(go())
    # None signals the abort; the child was terminated, not waited out.
    assert rc is None
    assert not runner.active_pids()


def test_run_capture_timeout_returns_none_and_terminates():
    runner = SubprocessRunner(owner="test")
    rc, _stdout, _stderr = asyncio.run(
        runner.run_capture(
            _py_cmd("import time; time.sleep(30)"), timeout=0.2,
        ),
    )
    assert rc is None
    assert not runner.active_pids()
