"""Every subprocess the shared runner starts reads stdin from /dev/null (#306):
a tool that asks for input (7z wanting a password) fails at once instead of
waiting on the terminal of a CLI run."""

import os
import sys

import pytest

from services.subprocess_runner import SubprocessRunner

@pytest.fixture(autouse=True)
def _stdin_is_a_pipe():
    """The test process's own stdin becomes an open pipe, as a terminal would
    be, so a child that inherited it would not see /dev/null."""
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)


_STDIN_IS_DEVNULL = (
    "import os, sys; "
    "sys.exit(0 if os.path.samestat(os.fstat(0), os.stat(os.devnull)) else 3)"
)


@pytest.mark.asyncio
async def test_run_capture_gives_the_child_no_stdin():
    runner = SubprocessRunner(owner="test")
    code, _out, _err = await runner.run_capture([sys.executable, "-c", _STDIN_IS_DEVNULL])
    assert code == 0


@pytest.mark.asyncio
async def test_run_verify_gives_the_child_no_stdin(tmp_path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")
    runner = SubprocessRunner(owner="test")
    events = [
        event async for event in runner.run_verify(
            [sys.executable, "-c", _STDIN_IS_DEVNULL],
            path=str(target),
            parse_progress=lambda line: None,
            success_message="ok",
            failure_message="bad",
        )
    ]
    assert events[-1]["valid"] is True, events


@pytest.mark.asyncio
async def test_run_gives_the_child_no_stdin(tmp_path):
    output = tmp_path / "out.bin"
    script = _STDIN_IS_DEVNULL.replace(
        "sys.exit(0 if", f"open({str(output)!r}, 'wb').write(b'x'); sys.exit(0 if",
    )
    runner = SubprocessRunner(owner="test")
    events = [
        event async for event in runner.run(
            [sys.executable, "-c", script],
            input_path=str(tmp_path),
            output_path=str(output),
            parse_progress=lambda line: None,
        )
    ]
    assert os.path.exists(output)
    assert not any(event.get("type") == "error" for event in events), events
