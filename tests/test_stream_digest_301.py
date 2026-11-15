"""The shared seams the Round-trip uses to hash a decoded stream (#301).

``services.stream_digest.hash_stream`` hashes chunks a Python decoder yields on
a detached thread; ``SubprocessRunner.hash_stdout`` hashes what a decoder
child writes to stdout. Neither writes a temp file, both are cancellable, and
a cancel stops the decoder rather than leaving it running.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
import zlib

import pytest
from services.stream_digest import hash_files, hash_stream
from services.subprocess_runner import ReadCancelled, SubprocessRunner


@pytest.mark.asyncio
async def test_hash_stream_hashes_every_chunk_in_order():
    def produce(_stop):
        yield b"abc"
        yield b"def"

    assert await hash_stream(produce) == hashlib.sha1(b"abcdef").hexdigest()
    assert await hash_stream(produce, algorithm="sha256") == (
        hashlib.sha256(b"abcdef").hexdigest()
    )
    assert await hash_stream(produce, algorithm="crc32") == (
        f"{zlib.crc32(b'abcdef'):08x}"
    )


@pytest.mark.asyncio
async def test_hash_files_reads_the_files_end_to_end(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"first part")
    b.write_bytes(b"second part")
    assert await hash_files([str(a), str(b)]) == (
        hashlib.sha1(b"first partsecond part").hexdigest()
    )


@pytest.mark.asyncio
async def test_cancel_stops_the_decoder_thread():
    """A cancel raises at once and the producer sees its stop flag, so the
    thread ends instead of decoding the rest of the image unattended."""
    cancel = asyncio.Event()
    started, finished = threading.Event(), threading.Event()

    def produce(stop):
        started.set()
        try:
            while not stop.is_set():
                yield b"x" * 1024
        finally:
            finished.set()

    task = asyncio.ensure_future(hash_stream(produce, cancel_event=cancel))
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    cancel.set()
    with pytest.raises(ReadCancelled):
        await task
    assert await asyncio.get_running_loop().run_in_executor(None, finished.wait, 5)


@pytest.mark.asyncio
async def test_a_producer_that_ends_on_its_stop_flag_gives_no_digest():
    """A producer returns, rather than raises, when it sees the stop flag. The
    prefix it hashed until then must never come back as the image's digest."""
    def produce(stop):
        yield b"first half"
        stop.set()  # stands in for a cancel landing between two chunks

    with pytest.raises(ReadCancelled):
        await hash_stream(produce)


@pytest.mark.asyncio
async def test_an_outer_deadline_also_stops_the_decoder_thread():
    finished = threading.Event()

    def produce(stop):
        try:
            while not stop.is_set():
                yield b"x" * 1024
        finally:
            finished.set()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(hash_stream(produce), timeout=0.2)
    assert await asyncio.get_running_loop().run_in_executor(None, finished.wait, 5)


# ---------------------------------------------------------------------------
# SubprocessRunner.hash_stdout
# ---------------------------------------------------------------------------


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.mark.asyncio
async def test_hash_stdout_hashes_what_the_child_writes():
    runner = SubprocessRunner(owner="test")
    cmd = _py("import sys\nfor _ in range(64): sys.stdout.buffer.write(b'y' * 65536)")
    assert await runner.hash_stdout(cmd) == hashlib.sha1(b"y" * 65536 * 64).hexdigest()
    assert runner.active_pids() == []


@pytest.mark.asyncio
async def test_hash_stdout_feeds_stdin_from_a_file_region(tmp_path):
    src = tmp_path / "container"
    src.write_bytes(b"HEADER" + b"payload bytes" * 1000)
    runner = SubprocessRunner(owner="test")
    cat = _py("import shutil, sys\nshutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)")

    got = await runner.hash_stdout(cat, stdin_path=str(src), stdin_offset=6)

    assert got == hashlib.sha1(b"payload bytes" * 1000).hexdigest()


@pytest.mark.asyncio
async def test_hash_stdout_fails_on_a_nonzero_exit_with_the_reason():
    runner = SubprocessRunner(owner="test")
    cmd = _py("import sys\nsys.stdout.write('half'); sys.stderr.write('bad frame'); sys.exit(3)")
    with pytest.raises(RuntimeError, match="bad frame"):
        await runner.hash_stdout(cmd)
    assert runner.active_pids() == []


@pytest.mark.asyncio
async def test_hash_stdout_cancel_stops_the_decoder():
    runner = SubprocessRunner(owner="test")
    cancel = asyncio.Event()
    cmd = _py(
        "import sys, time\nwhile True:\n sys.stdout.buffer.write(b'z'*4096)\n time.sleep(0.01)",
    )
    task = asyncio.ensure_future(runner.hash_stdout(cmd, cancel_event=cancel))
    await asyncio.sleep(0.3)
    assert runner.active_pids()
    cancel.set()
    with pytest.raises(ReadCancelled):
        await task
    assert runner.active_pids() == []


@pytest.mark.asyncio
async def test_a_stuck_stdin_read_does_not_hold_up_a_cancel(tmp_path, monkeypatch):
    """The input read blocks (a mount that stopped answering, here a FIFO
    nobody writes to). Cancel must return promptly: the stuck handle is
    abandoned, not closed, since closing waits on the read's lock."""
    import os
    import threading

    from services import subprocess_runner

    detached_calls: list[str] = []
    real_run_detached = subprocess_runner.run_detached

    async def recording(func, *args, **kwargs):
        detached_calls.append(getattr(func, "__name__", repr(func)))
        return await real_run_detached(func, *args, **kwargs)

    monkeypatch.setattr(subprocess_runner, "run_detached", recording)

    fifo = tmp_path / "stuck"
    os.mkfifo(fifo)
    writer: list[int] = []
    opener = threading.Thread(target=lambda: writer.append(os.open(fifo, os.O_WRONLY)))
    opener.start()
    runner = SubprocessRunner(owner="test")
    cancel = asyncio.Event()
    cat = _py("import shutil, sys\nshutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)")
    task = asyncio.ensure_future(
        runner.hash_stdout(cat, stdin_path=str(fifo), cancel_event=cancel),
    )
    try:
        await asyncio.sleep(0.5)
        cancel.set()
        with pytest.raises(ReadCancelled):
            await asyncio.wait_for(task, timeout=20)
        assert runner.active_pids() == []
        assert "read" in detached_calls
        assert "close" not in detached_calls  # abandoned with its stuck read
    finally:
        opener.join(5)
        for fd in writer:
            os.close(fd)  # unblocks the abandoned read so its thread can end


@pytest.mark.asyncio
async def test_hash_stdout_does_not_spawn_into_a_cancelled_operation():
    runner = SubprocessRunner(owner="test")
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(ReadCancelled):
        await runner.hash_stdout(_py("print('never')"), cancel_event=cancel)


@pytest.mark.asyncio
async def test_hash_stdout_is_bounded_by_its_timeout():
    runner = SubprocessRunner(owner="test")
    cmd = _py("import time\ntime.sleep(30)")
    with pytest.raises(asyncio.TimeoutError):
        await runner.hash_stdout(cmd, timeout=0.3)
    assert runner.active_pids() == []
