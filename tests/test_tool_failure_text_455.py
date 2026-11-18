"""A tool's failure reads as plain words in the job's error (#455).

When the OOM killer took 7z, the patch job's ``error_message`` was 7z's
progress output, backspaces and all, and only the log said "return code -9".
The shared runner now says a tool killed by a signal was killed, by which
signal, and that SIGKILL most likely means out of memory, without the progress
text. Any tool output that does reach a job error is stripped of backspaces,
escape sequences and other control characters, at the shared sinks
(``SubprocessRunner``, ``JobManager``), so every tool gets it.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest
from services.subprocess_runner import (
    SubprocessRunner,
    clean_tool_output,
    killed_by_signal_message,
)


def _py(script: str) -> list[str]:
    return [sys.executable, "-u", "-c", script]


async def _drain(gen) -> list[dict]:
    return [update async for update in gen]


def _run(tmp_path, script: str, label: str = "7z") -> str:
    runner = SubprocessRunner(owner="test")
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_drain(runner.run(
            _py(script),
            input_path=str(tmp_path / "in.bin"),
            output_path=str(tmp_path / "out.bin"),
            parse_progress=lambda line: None,
            fail_label=label,
        )))
    return str(excinfo.value)


# ----------------------------------------------------------------------
# the words
# ----------------------------------------------------------------------


def test_sigkill_is_named_and_most_likely_out_of_memory() -> None:
    assert killed_by_signal_message("7z", -9) == (
        "7z was killed (signal 9), most likely out of memory"
    )


def test_another_signal_is_named_without_guessing_why() -> None:
    message = killed_by_signal_message("chdman", -int(signal.SIGSEGV))
    assert message == f"chdman was killed (signal {int(signal.SIGSEGV)}, SIGSEGV)"
    assert "memory" not in message


def test_an_unknown_signal_number_still_reads_plainly() -> None:
    assert killed_by_signal_message("nsz", -200) == "nsz was killed (signal 200)"


def test_control_characters_are_stripped_from_tool_output() -> None:
    raw = (
        "\b\b\b\b\b 89% 1 + Big Patch Me (Patched).gba\r"
        "\x1b[31mERROR:\x1b[0m out of space\x00\x07\n\n  \n\ttabbed"
    )
    assert clean_tool_output(raw) == (
        "89% 1 + Big Patch Me (Patched).gba\nERROR: out of space\ntabbed"
    )


def test_plain_text_is_left_as_it_is() -> None:
    assert clean_tool_output("7z failed with return code 2.\nLast output:\nboom") == (
        "7z failed with return code 2.\nLast output:\nboom"
    )


# ----------------------------------------------------------------------
# the shared runner
# ----------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="signals are POSIX")
def test_a_tool_killed_by_sigkill_fails_in_plain_words_not_progress(tmp_path) -> None:
    message = _run(tmp_path, (
        "import os, signal, sys\n"
        "sys.stdout.write('\\b\\b\\b\\b 72% 1 + Game.gba\\n')\n"
        "sys.stdout.flush()\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    ))
    assert message == "7z was killed (signal 9), most likely out of memory"


def test_a_failing_tools_output_tail_has_no_control_characters(tmp_path) -> None:
    message = _run(tmp_path, (
        "import sys\n"
        "sys.stdout.write('\\b\\b\\b 40%\\x1b[1m disk full\\x1b[0m\\n')\n"
        "sys.exit(2)\n"
    ))
    assert message.startswith("7z failed with return code 2.")
    assert "40% disk full" in message
    assert not any(ch in message for ch in "\b\x1b\r\x00")


@pytest.mark.skipif(os.name != "posix", reason="signals are POSIX")
def test_hash_stdout_names_a_signal_kill_in_plain_words() -> None:
    runner = SubprocessRunner(owner="test")
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(runner.hash_stdout(
            _py("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"),
        ))
    label = os.path.basename(sys.executable)
    assert str(excinfo.value) == f"{label} was killed (signal 9), most likely out of memory"


# ----------------------------------------------------------------------
# the job sinks
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_external_jobs_error_is_stripped_of_control_characters() -> None:
    from models import ConversionMode
    from services.job_manager import job_manager

    job = job_manager.create_external_job("Patch x", ConversionMode.PATCH)
    await job_manager.finish_external_job(
        job.id, success=False,
        error_message="The patch stopped on an error: \b\b\b 89% \x1b[31mboom\x1b[0m",
    )
    assert job_manager.get_job_for_lookup(job.id).error_message == (
        "The patch stopped on an error: 89% boom"
    )
