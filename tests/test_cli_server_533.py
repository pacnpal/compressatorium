"""`compressatorium convert` uses the running server, or works alone (#533).

One process works a data directory at a time (``services.scheduler_lock``):

* a server holds the lock for its life, so a CLI started beside it hands its
  jobs to it over HTTP and runs none itself;
* with no server, the CLI runs its jobs in-process and holds the lock while
  they run, so a server started meanwhile waits for it.

Either way a CLI and a server never both work on one file. The server here is
a small stub of the three routes the CLI calls.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from models import ConversionJob, ConversionMode, JobStatus

from .test_cli_306 import _cli, _dolphin, _iso, _volume

# The CLI suite's own Volume and dolphin-tool stub.
FIXTURES = (_volume, _dolphin)


class _Stub:
    """``/health``, ``GET /api/tools``, ``POST /api/jobs/batch``,
    ``GET``/``DELETE /api/jobs/{id}``.

    ``max_depth`` is the server's ``MAX_QUEUE_DEPTH`` (429 past it), and
    ``busy`` how many batches it turns away first, as when other clients have
    filled its queue. ``available`` is what ``/api/tools`` says can run.
    """

    def __init__(
        self, *, finish: bool = True, max_depth: int = 0, busy: int = 0,
        available: list[str] | None = None, worker: str | None = None,
        cancel_faults: list | None = None, finish_after: int = 0,
    ) -> None:
        from services.tools import registry

        self.batches: list[dict] = []
        self.refused = 0
        self.cancelled: list[str] = []
        self.jobs: dict[str, ConversionJob] = {}
        self.finish = finish
        self.max_depth = max_depth
        self.busy = busy
        self.available = [t.id for t in registry.all()] if available is None else available
        # What /health says this server is: the id in the lock unless told.
        self.worker = worker
        # What the next DELETEs meet: "drop" (the connection closes with no
        # answer) or an HTTP status; then they cancel.
        self.cancel_faults = list(cancel_faults or [])
        # A job nobody cancels finishes after this many reads (0: never).
        self.finish_after = finish_after
        self.reads: dict[str, int] = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/health":
                    return self._send(200, {"status": "healthy", "worker": stub.worker_id()})
                if self.path == "/api/tools":
                    return self._send(200, {"available": stub.available})
                job = stub.jobs.get(self.path.rsplit("/", 1)[-1])
                if job is None:
                    return self._send(404, {"detail": "Job not found"})
                stub.reads[job.id] = stub.reads.get(job.id, 0) + 1
                if stub.finish_after and stub.reads[job.id] >= stub.finish_after and (
                    job.status == JobStatus.PROCESSING
                ):
                    job.status = JobStatus.COMPLETED
                return self._send(200, job.model_dump(mode="json"))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                active = sum(
                    1 for job in stub.jobs.values()
                    if job.status in (JobStatus.QUEUED, JobStatus.PROCESSING)
                )
                too_deep = stub.max_depth and active + len(body["file_paths"]) > stub.max_depth
                if stub.busy or too_deep:
                    stub.busy = max(0, stub.busy - 1)
                    stub.refused += 1
                    return self._send(429, {"detail": "Conversion queue full. Retry later."})
                stub.batches.append(body)
                made = []
                for i, path in enumerate(body["file_paths"]):
                    job = ConversionJob(
                        id=f"j{len(stub.jobs)}-{i}", file_path=path,
                        filename=path.rsplit("/", 1)[-1], mode=ConversionMode(body["mode"]),
                        status=JobStatus.COMPLETED if stub.finish else JobStatus.PROCESSING,
                        created_at=datetime.now(timezone.utc),
                        output_path=path.rsplit(".", 1)[0] + ".rvz",
                    )
                    stub.jobs[job.id] = job
                    made.append(job.model_dump(mode="json"))
                return self._send(200, {"jobs": made, "skipped": []})

            def do_DELETE(self):
                job_id = self.path.rsplit("/", 1)[-1]
                if stub.cancel_faults:
                    fault = stub.cancel_faults.pop(0)
                    if fault == "drop":
                        self.close_connection = True
                        return None
                    return self._send(fault, {"detail": "Try again"})
                stub.cancelled.append(job_id)
                stub.jobs[job_id].status = JobStatus.CANCELLED
                return self._send(200, {"status": "cancelled"})

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def worker_id(self) -> str | None:
        if self.worker is not None:
            return self.worker
        from services import scheduler_lock

        try:
            with open(scheduler_lock.lock_path(), encoding="utf-8") as lock:
                return json.load(lock).get("id")
        except (OSError, ValueError):
            return None

    def close(self) -> None:
        self._http.shutdown()
        self._http.server_close()


@pytest.fixture(name="stub")
def _stub_server():
    stub = _Stub()
    yield stub
    stub.close()


@pytest.fixture(name="server_lock")
def _server_lock():
    """The lock a running server holds, as another process would."""
    from services import scheduler_lock

    held = scheduler_lock.acquire("server")
    yield held
    held.release()


def test_convert_hands_its_jobs_to_the_running_server(
    volume, dolphin, capsys, stub, server_lock,
):
    iso = _iso(volume)
    server_lock.note_port(stub.port)

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0, lines
    assert [b["file_paths"] for b in stub.batches] == [[str(iso)]]
    assert stub.batches[0]["mode"] == "dolphin_rvz"
    assert [(line["path"], line["status"]) for line in lines] == [(str(iso), "completed")]
    assert dolphin["runs"] == [], "nothing ran in the CLI"


def test_server_names_the_server_outright(volume, dolphin, capsys, stub):
    iso = _iso(volume)

    code, lines = _cli(
        capsys, "convert", str(volume), "--to", "rvz", "--json",
        "--server", f"http://127.0.0.1:{stub.port}",
    )

    assert code == 0, lines
    assert [b["file_paths"] for b in stub.batches] == [[str(iso)]]
    assert dolphin["runs"] == []


def test_a_server_that_does_not_answer_stops_the_run(volume, dolphin, capsys, server_lock):
    _iso(volume)
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    server_lock.note_port(port)
    import cli

    code = cli.main(["convert", str(volume), "--to", "rvz", "--json"])
    err = capsys.readouterr().err

    assert code == cli.EXIT_LOCKED
    assert "--server" in err
    assert dolphin["runs"] == []


def test_a_stop_cancels_this_runs_jobs_on_the_server(
    volume, dolphin, capsys, server_lock, monkeypatch,
):
    """Ctrl-C ends the run's own jobs on the server, and only those."""
    import asyncio

    import cli

    stub = _Stub(finish=False)
    server_lock.note_port(stub.port)
    _iso(volume)
    real = cli._run_remote

    async def stop_once_queued(server, prepared, printer, state):
        async def stop_later():
            while not stub.jobs:
                await asyncio.sleep(0.01)
            state.stopping = True

        stopper = asyncio.ensure_future(stop_later())
        try:
            await real(server, prepared, printer, state)
        finally:
            stopper.cancel()

    monkeypatch.setattr(cli, "_run_remote", stop_once_queued)
    try:
        code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")
    finally:
        stub.close()

    assert stub.cancelled == list(stub.jobs)
    assert [line["status"] for line in lines] == ["cancelled"]
    assert code == cli.EXIT_FAILED


def test_with_no_server_the_cli_works_alone_and_holds_the_lock(
    volume, dolphin, capsys, monkeypatch,
):
    """A server started now would wait (``acquire_waiting``) until it ends."""
    from services import scheduler_lock

    iso = _iso(volume)
    seen = []
    from services.tools import registry

    service = registry.get("dolphin")._service
    real_run = service._runner.run

    async def run_and_look(cmd, **kwargs):
        try:
            scheduler_lock.check()
        except scheduler_lock.SchedulerLockHeld as exc:
            seen.append(exc.holder.get("by"))
        async for event in real_run(cmd, **kwargs):
            yield event

    monkeypatch.setattr(service._runner, "run", run_and_look)

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0, lines
    assert iso.with_suffix(".rvz").exists()
    assert seen == ["convert"]
    scheduler_lock.check()  # let go once the run ended


@pytest.mark.asyncio
async def test_a_server_waits_for_a_cli_that_works_alone():
    from services import scheduler_lock

    cli_run = scheduler_lock.acquire("convert")
    told = []
    try:
        with pytest.raises(scheduler_lock.StoppedWaiting):
            await scheduler_lock.acquire_waiting(
                "server", on_wait=told.append, should_stop=lambda: bool(told),
            )
    finally:
        cli_run.release()

    assert told[0].holder["by"] == "convert"
    assert "compressatorium convert" in str(told[0])


def test_a_server_names_its_port_on_health(server_lock):
    import asyncio

    from main import app, health_check
    from services import scheduler_lock
    from starlette.requests import Request

    app.state.scheduler = server_lock
    try:
        asyncio.run(health_check(
            Request({"type": "http", "app": app, "server": ("127.0.0.1", 8123)}),
        ))
    finally:
        app.state.scheduler = None
    with pytest.raises(scheduler_lock.SchedulerLockHeld) as held:
        scheduler_lock.check()

    assert held.value.holder["port"] == 8123


def _isos(volume, count):
    return [_iso(volume, f"gc/Game {n}.iso") for n in range(count)]


def test_a_server_with_a_shallow_queue_still_gets_every_input(
    volume, dolphin, capsys, server_lock, monkeypatch,
):
    """A server whose MAX_QUEUE_DEPTH is below the CLI's window: a refused
    batch is split until it fits, never failed."""
    from config import settings

    monkeypatch.setattr(settings, "max_concurrent_jobs", 2)  # a window of 4
    stub = _Stub(max_depth=1)
    server_lock.note_port(stub.port)
    isos = _isos(volume, 3)
    try:
        code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")
    finally:
        stub.close()

    assert code == 0, lines
    assert stub.refused, "the server turned the first batch away"
    assert all(len(b["file_paths"]) == 1 for b in stub.batches)
    assert sorted(line["path"] for line in lines) == sorted(map(str, isos))
    assert {line["status"] for line in lines} == {"completed"}


def test_a_full_server_queue_is_waited_for(volume, dolphin, capsys, server_lock, monkeypatch):
    """Other clients have the server's queue full: the run waits its turn."""
    import cli

    monkeypatch.setattr(cli, "_REMOTE_POLL_S", 0.01)
    stub = _Stub(busy=3)
    server_lock.note_port(stub.port)
    iso = _iso(volume)
    try:
        code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")
    finally:
        stub.close()

    assert code == 0, lines
    assert stub.refused == 3
    assert [(line["path"], line["status"]) for line in lines] == [(str(iso), "completed")]


def test_a_tool_ready_only_on_the_server_still_converts(
    volume, dolphin, capsys, server_lock, monkeypatch,
):
    """The server's readiness decides a remote run: a tool
    that cannot run in the CLI's container (nsz with the keys mounted only on
    the server) still has its Images sent."""
    from services.tools import registry

    real = registry.tool_is_ready

    async def not_here(tool, **kwargs):
        return False if tool.id == "dolphin" else await real(tool, **kwargs)

    monkeypatch.setattr(registry, "tool_is_ready", not_here)
    stub = _Stub()
    server_lock.note_port(stub.port)
    iso = _iso(volume)
    try:
        code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")
    finally:
        stub.close()

    assert code == 0, lines
    assert [b["file_paths"] for b in stub.batches] == [[str(iso)]]
    assert [(line["path"], line["status"]) for line in lines] == [(str(iso), "completed")]


def test_a_tool_the_server_cannot_run_is_skipped(
    volume, dolphin, capsys, server_lock,
):
    """And the other way: ready here but not on the server, nothing is sent."""
    stub = _Stub(available=[])
    server_lock.note_port(stub.port)
    iso = _iso(volume)
    try:
        code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")
    finally:
        stub.close()

    assert code == 0, lines
    assert stub.batches == []
    assert [(line["path"], line["status"]) for line in lines] == [(str(iso), "skipped")]


def _advertise(held, **fields):
    """Rewrite the server's holder note, as a server elsewhere would write it."""
    from services import scheduler_lock

    held._record = {**held._record, **fields}
    scheduler_lock._write_record(held._fd, held._record)


def test_the_lock_names_the_servers_addresses(server_lock):
    """The holder note carries an id and the host's own IPv4 addresses, so a
    CLI on the default bridge (no DNS) can still reach it."""
    import ipaddress

    from services import scheduler_lock

    with pytest.raises(scheduler_lock.SchedulerLockHeld) as held:
        scheduler_lock.check()
    holder = held.value.holder

    assert holder["id"]
    for address in holder.get("addresses", []):
        ip = ipaddress.ip_address(address)
        assert ip.version == 4 and not ip.is_loopback


def test_unreadable_addresses_leave_just_the_host(monkeypatch):
    from services import scheduler_lock

    def broken(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(socket, "getaddrinfo", broken)
    monkeypatch.setattr(socket, "socket", broken)
    held = scheduler_lock.acquire("server")
    try:
        record = held._record
    finally:
        held.release()

    assert "addresses" not in record
    assert record["host"] == socket.gethostname()


def test_a_server_whose_name_does_not_resolve_is_found_by_address(
    volume, dolphin, capsys, stub, server_lock,
):
    iso = _iso(volume)
    _advertise(
        server_lock, host="no-such-host.invalid", addresses=["127.0.0.1"], port=stub.port,
    )

    code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")

    assert code == 0, lines
    assert [b["file_paths"] for b in stub.batches] == [[str(iso)]]
    assert dolphin["runs"] == []


def test_another_server_answering_an_address_is_not_taken(
    volume, dolphin, capsys, server_lock,
):
    """Only the server that holds the lock: an answer with another id is not it."""
    import cli

    stub = _Stub(worker="someone-else")
    _iso(volume)
    _advertise(
        server_lock, host="no-such-host.invalid", addresses=["127.0.0.1"], port=stub.port,
    )
    try:
        code = cli.main(["convert", str(volume), "--to", "rvz", "--json"])
    finally:
        stub.close()
    err = capsys.readouterr().err

    assert code == cli.EXIT_LOCKED
    assert "--server" in err
    assert stub.batches == []
    assert dolphin["runs"] == []


@pytest.mark.parametrize(("argv", "env", "port"), [
    (["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9090", "--reload"], {}, 9090),
    (["uvicorn", "main:app", "--port=9091"], {}, 9091),
    (["uvicorn", "main:app"], {"UVICORN_PORT": "9092"}, 9092),
    (["uvicorn", "main:app", "--port", "nope"], {}, None),
    (["pytest"], {}, None),
])
def test_the_listening_port_is_read_from_the_launch(argv, env, port):
    from main import _launch_port

    assert _launch_port(argv, env) == port


def test_a_server_names_its_port_when_it_starts(monkeypatch, tmp_path):
    """Before any request (Codex PRRT_kwDOQ9nULM66RoML): a server on
    PORT=9090 that nothing has asked for /health yet is still found."""
    import asyncio
    import contextlib
    import sys

    import main
    from services import scheduler_lock

    from config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "concurrency_lock_dir", None)
    monkeypatch.setattr(
        sys, "argv", ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9090"],
    )

    @contextlib.asynccontextmanager
    async def no_serve(app):
        yield

    monkeypatch.setattr(main, "_serve", no_serve)
    # The process's logging is the logging suite's to set up, not this test's.
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    seen = {}

    async def start():
        async with main.lifespan(main.app):
            with pytest.raises(scheduler_lock.SchedulerLockHeld) as held:
                scheduler_lock.check()
            seen.update(held.value.holder)

    asyncio.run(start())

    assert seen["port"] == 9090


def _stop_with(stub, monkeypatch, jobs: int):
    """Stop the run once the server holds ``jobs`` of its jobs."""
    import asyncio

    import cli

    real = cli._run_remote

    async def stop_once_queued(server, prepared, printer, state):
        async def stop_later():
            while len(stub.jobs) < jobs:
                await asyncio.sleep(0.01)
            state.stopping = True

        stopper = asyncio.ensure_future(stop_later())
        try:
            await real(server, prepared, printer, state)
        finally:
            stopper.cancel()

    monkeypatch.setattr(cli, "_run_remote", stop_once_queued)
    monkeypatch.setattr(cli, "_REMOTE_POLL_S", 0.01)


@pytest.mark.parametrize("fault", ["drop", 503])
def test_a_cancel_the_server_did_not_take_is_tried_again(
    volume, dolphin, capsys, server_lock, monkeypatch, fault,
):
    """A cancel that lost its connection, or met a 503, is not the end of it:
    every job is cancelled, the first
    one too, and every line says so."""
    import cli

    # Left alone, the jobs would finish: a cancel given up on shows up here.
    stub = _Stub(finish=False, cancel_faults=[fault], finish_after=50)
    server_lock.note_port(stub.port)
    _isos(volume, 2)
    _stop_with(stub, monkeypatch, jobs=2)
    try:
        code, lines = _cli(capsys, "convert", str(volume), "--to", "rvz", "--json")
    finally:
        stub.close()

    assert sorted(stub.cancelled) == sorted(stub.jobs)
    assert [line["status"] for line in lines] == ["cancelled", "cancelled"]
    assert code == cli.EXIT_FAILED


def test_a_job_that_ended_before_its_cancel_is_not_cancelled_again():
    """A 404 (the job is gone) settles a cancel; a 503 leaves it pending."""
    import asyncio

    from services.server_client import Server

    stub = _Stub(cancel_faults=[404, 503])
    try:
        server = Server(f"http://127.0.0.1:{stub.port}")
        settled = [asyncio.run(server.cancel("j")) for _ in range(2)]
    finally:
        stub.close()

    assert settled == [True, False]


def test_a_failed_publish_of_the_holder_note_is_retried(server_lock, monkeypatch):
    """A write of the note that fails leaves it to the next one,
    on the same file."""
    from services import scheduler_lock

    inode = os.stat(scheduler_lock.lock_path()).st_ino
    real = os.pwrite

    def fail_once(*args):
        monkeypatch.setattr(os, "pwrite", real)
        raise OSError("disk full")

    monkeypatch.setattr(os, "pwrite", fail_once)
    server_lock.note_port(8123)  # fails
    server_lock.note_port(8123)  # the /health after it
    with pytest.raises(scheduler_lock.SchedulerLockHeld) as held:
        scheduler_lock.check()

    assert held.value.holder["port"] == 8123
    assert held.value.holder["id"] == server_lock.worker_id
    assert os.stat(scheduler_lock.lock_path()).st_ino == inode
