"""#465: stopping the server with an event stream open shuts down cleanly.

The server runs as a real uvicorn process, because the failure lives in how
uvicorn drains open connections on SIGTERM: a stream whose response was cut
off without its closing chunk logs "ASGI callable returned without completing
response." Only the PID this test starts is ever signalled.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BOOT_TIMEOUT = 60
EXIT_TIMEOUT = 30


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(port: int, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early with {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
                sock.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
                if sock.recv(64).startswith(b"HTTP/1.1 200"):
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError("server did not become healthy")


def _open_stream(
    port: int, path: str,
) -> tuple[socket.socket, bytearray, threading.Thread]:
    """Open an SSE stream and read it on a thread until the server closes it."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=EXIT_TIMEOUT)
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: t\r\nAccept: text/event-stream\r\n\r\n".encode()
    )
    received = bytearray()
    first = sock.recv(4096)
    assert first.startswith(b"HTTP/1.1 200"), first[:200]
    received += first

    def _read() -> None:
        try:
            while chunk := sock.recv(4096):
                received.extend(chunk)
        except OSError:
            pass

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    return sock, received, reader


@pytest.mark.parametrize("path", ["/api/jobs/events"])
def test_sigterm_with_an_open_job_stream_logs_no_error(tmp_path, path):
    port = _free_port()
    data_dir = tmp_path / "data"
    volume = tmp_path / "vol"
    data_dir.mkdir()
    volume.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": str(APP_DIR),
        "CHD_DATA_DIR": str(data_dir),
        "COMPRESSATORIUM_VOLUMES": str(volume),
        "COMPRESSATORIUM_ENABLE_AUTH": "false",
    }
    log_path = tmp_path / "server.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "main:app",
                "--host", "127.0.0.1", "--port", str(port),
            ],
            cwd=str(APP_DIR),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    sock = None
    try:
        _wait_for_health(port, proc)
        sock, received, reader = _open_stream(port, path)
        time.sleep(0.5)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=EXIT_TIMEOUT)
        reader.join(timeout=EXIT_TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if sock is not None:
            sock.close()

    output = log_path.read_text(errors="replace")
    assert "Waiting for application shutdown" in output, output
    assert "ERROR" not in output, output
    # Chunked transfer ends with a zero-length chunk once the body is complete.
    assert bytes(received).endswith(b"0\r\n\r\n"), bytes(received[-200:])


def test_until_shutdown_stops_a_waiting_generator_and_closes_it():
    import asyncio

    import anyio
    from services.event_stream import until_shutdown

    closed = []

    async def source():
        try:
            yield "first"
            await asyncio.sleep(3600)
            yield "never"
        finally:
            closed.append(True)

    async def main():
        shutdown = anyio.Event()
        seen = []
        with anyio.fail_after(5):
            async for item in until_shutdown(source(), shutdown):
                seen.append(item)
                asyncio.get_running_loop().call_later(0.05, shutdown.set)
        return seen

    assert asyncio.run(main()) == ["first"]
    assert closed == [True]


def test_until_shutdown_passes_items_and_errors_through_unwrapped():
    import asyncio

    import anyio
    from services.event_stream import until_shutdown

    async def source():
        yield 1
        yield 2
        raise ValueError("boom")

    async def main():
        seen = []
        with pytest.raises(ValueError, match="boom"):
            async for item in until_shutdown(source(), anyio.Event()):
                seen.append(item)
        return seen

    assert asyncio.run(main()) == [1, 2]


def test_every_event_stream_route_uses_the_shared_helper():
    """No route builds a bare EventSourceResponse, which would cut off on shutdown."""
    routes = APP_DIR / "routes"
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(routes.glob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if "return EventSourceResponse(" in line
    ]
    assert offenders == []
