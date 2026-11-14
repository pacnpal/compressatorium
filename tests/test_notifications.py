"""Notifications (#293): webhook and Apprise destinations, sweep and failed-job messages."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from models import ConversionJob, ConversionMode, JobStatus
from routes import notifications as notification_routes
from services import db as _db
from services import notifications


@pytest.fixture(autouse=True)
def _engine(db_path, reset_db_engine):
    _db.init_engine(db_path)
    notifications._reset_for_tests()
    yield
    reset_db_engine()


class Recorder:
    """Stands in for the real transports; remembers every send."""

    def __init__(self):
        self.sent: list[tuple[str, str, notifications.Event]] = []
        self.fail_with: Exception | None = None

    def make(self, kind):
        def send(url, event):
            if self.fail_with:
                raise self.fail_with
            self.sent.append((kind, url, event))
        return send


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", r.make("webhook"))
    monkeypatch.setitem(notifications.TRANSPORTS, "apprise", r.make("apprise"))
    monkeypatch.setattr(notifications, "apprise_available", lambda: True)
    return r


class Api:
    """The notification routes, called as plain functions (no HTTP client)."""

    async def _call(self, fn, *args):
        try:
            return 200, await fn(*args)
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}

    async def get(self):
        return (await self._call(notification_routes.get_notifications))[1]

    async def add(self, kind, url, label=None):
        return await self._call(
            notification_routes.add_destination,
            notification_routes.NewDestination(kind=kind, url=url, label=label))

    async def edit(self, dest_id, **body):
        return await self._call(
            notification_routes.edit_destination, dest_id,
            notification_routes.DestinationEdit(**body))

    async def delete(self, dest_id):
        return await self._call(notification_routes.delete_destination, dest_id)

    async def test_saved(self, dest_id):
        return (await self._call(notification_routes.test_saved_destination, dest_id))[1]

    async def test_draft(self, kind, url):
        return (await self._call(
            notification_routes.test_draft,
            notification_routes.DraftTest(kind=kind, url=url)))[1]


@pytest.fixture
def client():
    return Api()


def _job(status=JobStatus.FAILED, jid="j1", name="Game.iso", **kw) -> ConversionJob:
    now = datetime.now(timezone.utc)
    return ConversionJob(
        id=jid, file_path=f"/data/ps2/{name}", filename=name,
        mode=ConversionMode.CREATEDVD, status=status, created_at=now, completed_at=now,
        error_message=kw.pop("error_message", "boom"), **kw,
    )


async def _add(client, url="https://hooks.example/abc?token=SECRET", kind="webhook",
               label="Phone"):
    status, body = await client.add(kind, url, label)
    assert status == 200, body
    return body


def _event():
    return notifications.Event("job_failed", "t", "b")


# --- destinations -------------------------------------------------------


@pytest.mark.asyncio
async def test_add_edit_remove_and_never_return_the_url(client, rec):
    dest = await _add(client)
    assert "url" not in dest and "SECRET" not in str(dest)
    listing = await client.get()
    assert [d["label"] for d in listing["destinations"]] == ["Phone"]
    assert "SECRET" not in str(listing)
    assert {e["key"] for e in listing["events"]} >= {"sweep_finished", "job_failed"}

    _, edited = await client.edit(dest["id"], label="Tablet")
    assert edited["label"] == "Tablet" and "url" not in edited
    # Omitting the url keeps the stored one.
    await notifications.deliver(_event())
    assert rec.sent[-1][1] == "https://hooks.example/abc?token=SECRET"

    await client.edit(dest["id"], url="https://other.example/x")
    await notifications.deliver(_event())
    assert rec.sent[-1][1] == "https://other.example/x"

    assert (await client.delete(dest["id"]))[0] == 200
    assert (await client.get())["destinations"] == []
    assert (await client.delete(dest["id"]))[0] == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "", "ftp://x/y", "file:///etc/passwd", "not a url",
    "https://h/a b?token=X", "http://[abc/x", "https://h/\x00x",
])
async def test_webhook_url_must_be_http(client, rec, url):
    status, _ = await client.add("webhook", url)
    assert status == 400


@pytest.mark.asyncio
async def test_apprise_is_refused_with_a_clear_message_when_not_installed(client, monkeypatch):
    monkeypatch.setattr(notifications, "apprise_available", lambda: False)
    status, body = await client.add("apprise", "discord://id/token")
    assert status == 400 and "Apprise is not installed" in body["detail"]
    assert (await client.get())["apprise_available"] is False
    # Webhooks still work.
    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", lambda u, e: None)
    await _add(client)


@pytest.mark.asyncio
async def test_test_button_reports_success_and_error(client, rec):
    dest = await _add(client)
    assert await client.test_saved(dest["id"]) == {"ok": True, "error": None}
    assert rec.sent[-1][2].kind == "test"

    rec.fail_with = notifications.NotificationError("The webhook answered HTTP 500")
    bad = await client.test_saved(dest["id"])
    assert bad["ok"] is False and "HTTP 500" in bad["error"]

    # An unsaved draft can be tested too.
    rec.fail_with = None
    draft = await client.test_draft("webhook", "https://x.example/h")
    assert draft["ok"] is True and rec.sent[-1][1] == "https://x.example/h"


@pytest.mark.asyncio
async def test_urls_never_reach_the_log_or_the_test_response(client, rec, caplog):
    secret = "https://h.example/path/TOKEN123?k=SECRETQ"
    await _add(client, secret)

    def boom(url, event):
        # What urllib's InvalidURL and friends do: echo a piece of the URL.
        raise ValueError(f"bad url {url.split('h.example')[1]}")

    notifications.TRANSPORTS["webhook"] = boom
    await notifications.deliver(_event())
    bad = await client.test_draft("webhook", secret)
    for text in (caplog.text, str(bad)):
        assert "TOKEN123" not in text and "SECRETQ" not in text
    assert bad["ok"] is False and "ValueError" in bad["error"]

    # A NotificationError that quotes the URL is redacted piece by piece.
    def quoting(url, event):
        raise notifications.NotificationError(f"cannot POST {url}")

    notifications.TRANSPORTS["webhook"] = quoting
    caplog.clear()
    await notifications.deliver(_event())
    assert "TOKEN123" not in caplog.text and "SECRETQ" not in caplog.text


@pytest.mark.asyncio
async def test_apprise_test_failure_names_the_reason_without_the_token(
        client, monkeypatch):
    """The real Apprise reports why only through its log; Test surfaces it."""
    import logging
    import sys
    import types

    class FakeApprise:
        def add(self, url):
            return url.startswith("json://")

        def __iter__(self):
            return iter([types.SimpleNamespace(socket_read_timeout=4)])

        def notify(self, title, body):
            logging.getLogger("apprise-fake").warning(
                "Failed to send JSON POST notification to /SECRETPATH: "
                "Verification Failed., error=401.")
            return False

    class FakeCapture:
        def __init__(self, level):
            self.handler = logging.StreamHandler(__import__("io").StringIO())
            self.handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            self.handler.setLevel(level)

        def __enter__(self):
            logging.getLogger("apprise-fake").addHandler(self.handler)
            return self

        def __exit__(self, *a):
            logging.getLogger("apprise-fake").removeHandler(self.handler)

        def getvalue(self):
            return self.handler.stream.getvalue()

    fake = types.SimpleNamespace(Apprise=FakeApprise, LogCapture=FakeCapture)
    monkeypatch.setitem(sys.modules, "apprise", fake)
    monkeypatch.setattr(notifications, "apprise_available", lambda: True)
    result = await client.test_draft("apprise", "json://host.example/SECRETPATH")
    assert result["ok"] is False
    assert "401" in result["error"] and "SECRETPATH" not in result["error"]


# --- sending --------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_sent_without_a_destination(rec):
    await notifications.on_job_finished(_job())
    await notifications.deliver(_event())
    await notifications.drain()
    assert rec.sent == []


@pytest.mark.asyncio
async def test_a_failing_destination_does_not_stop_the_others(client, rec, caplog):
    await _add(client, "https://a.example/1", label="A")
    await _add(client, "discord://x/y", kind="apprise", label="B")
    calls = []

    def flaky(url, event):
        calls.append(url)
        raise RuntimeError("down")

    notifications.TRANSPORTS["webhook"] = flaky
    await notifications.deliver(_event())
    assert len(calls) == 1 and len(rec.sent) == 1
    assert "https://a.example/1" not in caplog.text  # URLs are secrets


@pytest.mark.asyncio
async def test_a_hanging_destination_times_out_and_never_blocks(client, rec, monkeypatch):
    await _add(client)
    gate = threading.Event()
    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", lambda u, e: gate.wait(5))
    monkeypatch.setattr(notifications, "SEND_TIMEOUT_S", 0.05)
    started = asyncio.get_running_loop().time()
    await notifications.deliver(_event())
    assert asyncio.get_running_loop().time() - started < 1
    gate.set()


@pytest.mark.asyncio
async def test_notify_returns_at_once_and_sends_in_the_background(client, rec):
    await _add(client)
    gate = threading.Event()
    sent = threading.Event()

    def slow(url, event):
        gate.wait(5)
        sent.set()

    notifications.TRANSPORTS["webhook"] = slow
    notifications.notify(_event())  # not awaited
    assert not sent.is_set()
    gate.set()
    await notifications.drain()
    assert sent.is_set()


@pytest.mark.asyncio
async def test_failed_manual_job_sends_one_message(client, rec):
    await _add(client)
    await notifications.on_job_finished(_job(error_message="chdman exited 1"))
    await notifications.on_job_finished(_job(JobStatus.COMPLETED, jid="ok"))
    await notifications.on_job_finished(_job(JobStatus.CANCELLED, jid="c"))
    await notifications.drain()
    assert len(rec.sent) == 1
    ev = rec.sent[0][2]
    assert ev.kind == "job_failed" and "Game.iso" in ev.title
    assert "chdman exited 1" in ev.body and ev.data["filename"] == "Game.iso"


@pytest.mark.asyncio
async def test_sweep_sends_one_message_with_counts_bytes_and_failures(client, rec):
    await _add(client)
    jobs = [_job(JobStatus.PROCESSING, f"s{i}", f"G{i}.iso") for i in range(2)]
    bad = _job(JobStatus.PROCESSING, "sf", "Bad.iso", error_message="disc read error")
    notifications.sweep_jobs_queued(jobs + [bad])
    notifications.sweep_finished({"queued": 3, "errors": [
        {"platform_id": 9, "error": "queue_failed"}]})
    for j in jobs:
        j.status = JobStatus.COMPLETED
        j.input_bytes, j.output_size, j.deleted_paths = 1000, 400, ["/x"]
        await notifications.on_job_finished(j)
    await notifications.drain()
    assert rec.sent == []  # still waiting on the last job
    bad.status = JobStatus.FAILED
    await notifications.on_job_finished(bad)
    await notifications.drain()

    assert len(rec.sent) == 1  # one message; the failure is inside it, not separate
    ev = rec.sent[0][2]
    assert ev.kind == "sweep_finished"
    assert ev.data["completed"] == 2 and ev.data["failed"] == 1
    assert ev.data["saved_bytes"] == 1200
    assert ev.data["failures"] == [{"file": "Bad.iso", "error": "disc read error"}]
    assert "Bad.iso" in ev.body and "disc read error" in ev.body
    assert "queue_failed" in ev.body


@pytest.mark.asyncio
async def test_sweep_that_queued_nothing_stays_quiet(client, rec):
    await _add(client)
    notifications.sweep_finished({"queued": 0, "errors": []})
    notifications.sweep_finished({"queued": 0, "errors": []}, dry_run=True)
    await notifications.drain()
    assert rec.sent == []


@pytest.mark.asyncio
async def test_sweep_with_only_queue_errors_still_reports(client, rec):
    await _add(client)
    notifications.sweep_finished({"queued": 0, "errors": [
        {"platform_id": 1, "error": "sources_unreadable"}]})
    await notifications.drain()
    assert len(rec.sent) == 1 and "sources_unreadable" in rec.sent[0][2].body


@pytest.mark.asyncio
async def test_job_that_finishes_before_the_sweep_closes_is_still_counted(client, rec):
    await _add(client)
    job = _job(JobStatus.PROCESSING, "early", "Early.iso")
    notifications.sweep_jobs_queued([job])
    job.status = JobStatus.FAILED
    await notifications.on_job_finished(job)  # ends before the sweep returns
    await notifications.drain()
    assert rec.sent == []  # a sweep job never gets a lone failure message
    notifications.sweep_finished({"queued": 1, "errors": []})
    await notifications.drain()
    assert len(rec.sent) == 1 and rec.sent[0][2].data["failed"] == 1


@pytest.mark.asyncio
async def test_job_already_terminal_when_queued_does_not_hang_the_sweep(client, rec):
    """The queue awaits after accepting a batch, so a job can end first."""
    await _add(client)
    quick = _job(JobStatus.FAILED, "quick", "Quick.iso", error_message="no disc")
    notifications.sweep_jobs_queued([quick])
    notifications.sweep_finished({"queued": 1, "errors": []})
    await notifications.drain()
    assert len(rec.sent) == 1 and rec.sent[0][2].data["failed"] == 1
    # Its listener runs later and must find the claim: no lone job_failed.
    await notifications.on_job_finished(quick)
    await notifications.drain()
    assert len(rec.sent) == 1
    assert not notifications._job_sweeps  # nothing left waiting in memory


@pytest.mark.asyncio
async def test_a_repeating_error_is_reported_once_until_it_changes(client, rec):
    await _add(client)
    err = [{"platform_id": 7, "error": "tool_not_ready"}]
    for _ in range(10):  # ten scheduler ticks hitting the same failure
        notifications.sweep_finished({"queued": 0, "errors": list(err)})
    await notifications.drain()
    assert len(rec.sent) == 1 and "tool_not_ready" in rec.sent[0][2].body

    notifications.sweep_finished({"queued": 0, "errors": [
        {"platform_id": 7, "error": "unreadable"}]})
    await notifications.drain()
    assert len(rec.sent) == 2 and "unreadable" in rec.sent[1][2].body

    # A clean sweep sends nothing (no recovery message) but forgets the error,
    # so a recurrence is news again.
    notifications.sweep_finished(
        {"queued": 0, "errors": [], "platforms": [{"platform_id": 7}]})
    await notifications.drain()
    assert len(rec.sent) == 2
    notifications.sweep_finished({"queued": 0, "errors": [
        {"platform_id": 7, "error": "unreadable"}]})
    await notifications.drain()
    assert len(rec.sent) == 3


@pytest.mark.asyncio
async def test_a_second_platform_failing_is_new_even_if_the_first_repeats(client, rec):
    await _add(client)
    a = {"platform_id": 1, "error": "tool_not_ready"}
    b = {"platform_id": 2, "error": "tool_not_ready"}
    notifications.sweep_finished({"queued": 0, "errors": [a]})
    notifications.sweep_finished({"queued": 0, "errors": [a, b]})
    await notifications.drain()
    assert len(rec.sent) == 2


def test_events_are_a_shared_registry(monkeypatch):
    monkeypatch.setattr(notifications, "_EVENTS", dict(notifications._EVENTS))
    notifications.register_event("check_failed", "A check failed")
    assert "check_failed" in {e["key"] for e in notifications.event_list()}


@pytest.mark.asyncio
async def test_hung_destinations_cannot_starve_a_healthy_one(client, rec, monkeypatch):
    """Four blocked transports used to fill the 4-thread pool; a fifth never ran."""
    gate = threading.Event()
    monkeypatch.setattr(notifications, "SEND_TIMEOUT_S", 0.05)
    delivered = []

    def transport(url, event):
        if "hang" in url:
            gate.wait(5)
        else:
            delivered.append(url)

    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", transport)
    for i in range(4):
        await _add(client, f"https://hang{i}.example/x", label=f"h{i}")
    await _add(client, "https://good.example/x", label="ok")
    await notifications.deliver(_event())
    assert delivered == ["https://good.example/x"]
    gate.set()


@pytest.mark.asyncio
async def test_a_destination_still_stuck_is_skipped_not_stacked(client, rec, monkeypatch):
    gate = threading.Event()
    calls = []
    monkeypatch.setattr(notifications, "SEND_TIMEOUT_S", 0.05)

    def transport(url, event):
        calls.append(url)
        gate.wait(5)

    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", transport)
    await _add(client, "https://hang.example/x")
    await notifications.deliver(_event())
    await notifications.deliver(_event())  # the first send is still blocked
    assert len(calls) == 1
    gate.set()
    await asyncio.sleep(0.1)
    await notifications.deliver(_event())  # free again
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_overlapping_sends_to_one_healthy_destination_all_arrive_in_order(
        client, rec, monkeypatch):
    """Two events close together must not drop the second."""
    await _add(client)
    order = []
    release = threading.Event()

    def transport(url, event):
        if event.title == "first":
            release.wait(2)
        order.append(event.title)

    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", transport)
    first = asyncio.ensure_future(
        notifications.deliver(notifications.Event("job_failed", "first", "b")))
    await asyncio.sleep(0.05)  # the first send is now in flight
    second = asyncio.ensure_future(
        notifications.deliver(notifications.Event("sweep_finished", "second", "b")))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(first, second)
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_a_full_queue_drops_the_oldest_with_a_warning(client, rec, monkeypatch, caplog):
    dest = await _add(client)
    release = threading.Event()
    seen = []

    def transport(url, event):
        release.wait(2)
        seen.append(event.title)

    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", transport)
    monkeypatch.setattr(notifications, "MAX_QUEUED_PER_DESTINATION", 2)

    def ev(n):
        return notifications.Event("job_failed", n, "b")

    futs = [notifications._enqueue(dest["id"], ev("e0"))]
    await asyncio.sleep(0.1)  # e0 is in flight, the queue is empty
    futs += [notifications._enqueue(dest["id"], ev(n)) for n in ("e1", "e2", "e3")]
    release.set()
    await asyncio.gather(*futs)
    assert seen == ["e0", "e2", "e3"]  # e1 was the oldest waiting when e3 arrived
    assert "dropped" in caplog.text.lower() and "e1" in caplog.text


def test_webhook_has_an_overall_deadline_against_a_slow_drip(monkeypatch):
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def drip():
        conn, _ = srv.accept()
        conn.recv(65536)
        while not stop.is_set():  # one byte at a time, never finishing
            try:
                conn.send(b"H")
            except OSError:
                return
            time.sleep(0.1)

    threading.Thread(target=drip, daemon=True).start()
    monkeypatch.setattr(notifications, "SEND_TIMEOUT_S", 0.6)
    started = time.monotonic()
    with pytest.raises(notifications.NotificationError):
        notifications._send_webhook(f"http://127.0.0.1:{srv.getsockname()[1]}/h", _event())
    stop.set()
    assert time.monotonic() - started < 3


def test_webhook_does_not_follow_redirects():
    target, reached = _serve()
    redirecting, _ = _serve(
        status=302, location=f"http://127.0.0.1:{target.server_port}/elsewhere")
    try:
        with pytest.raises(notifications.NotificationError, match="HTTP 302"):
            notifications._send_webhook(
                f"http://127.0.0.1:{redirecting.server_port}/h", _event())
        assert reached == []  # the Location was never visited
    finally:
        target.shutdown()
        redirecting.shutdown()


@pytest.mark.parametrize("url", ["http://h:99999/x", "http://h:0/x", "https://h:70000/x"])
def test_out_of_range_port_is_refused_at_save(url):
    with pytest.raises(notifications.NotificationError):
        notifications.validate("webhook", url)


def test_ipv6_literal_without_a_port_reaches_the_right_host():
    import socket

    try:
        srv = socket.socket(socket.AF_INET6)
        srv.bind(("::1", 0))
    except OSError:
        pytest.skip("no IPv6 loopback")
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []

    def accept():
        conn, _ = srv.accept()
        got.append(conn.recv(65536))
        conn.send(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        conn.close()

    threading.Thread(target=accept, daemon=True).start()
    notifications._send_webhook(f"http://[::1]:{port}/h", _event())
    assert got and got[0].startswith(b"POST /h")


def test_ipv6_literal_without_a_port_gets_the_scheme_default_port(monkeypatch):
    seen = []

    class Recorder:
        def __init__(self, host, port=None, timeout=None):
            seen.append((host, port))
            self.sock = None

        def request(self, *a, **k):
            raise OSError("stop here")

        def close(self):
            pass

    monkeypatch.setattr(notifications.http.client, "HTTPConnection", Recorder)
    monkeypatch.setattr(notifications.http.client, "HTTPSConnection", Recorder)
    for url in ("http://[::1]/h", "https://[fe80::abcd]/h"):
        with pytest.raises(notifications.NotificationError):
            notifications._send_webhook(url, _event())
    # http.client would read the last ":" group of an unbracketed host as a port.
    assert seen == [("::1", 80), ("fe80::abcd", 443)]


@pytest.mark.asyncio
async def test_a_burst_to_a_healthy_destination_arrives_complete_and_in_order(
        client, rec, monkeypatch):
    await _add(client)
    got = []
    monkeypatch.setitem(
        notifications.TRANSPORTS, "webhook", lambda url, event: got.append(event.title))
    for burst in range(20):  # the order used to differ from run to run
        got.clear()
        for i in range(5):
            notifications.notify(notifications.Event("job_failed", f"e{i}", "b"))
        await notifications.drain()
        assert got == [f"e{i}" for i in range(5)], f"burst {burst}"


@pytest.mark.asyncio
async def test_deleting_a_destination_drops_its_queued_messages(client, rec, monkeypatch):
    dest = await _add(client)
    release = threading.Event()
    got = []

    def transport(url, event):
        release.wait(2)
        got.append(event.title)

    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", transport)
    for i in range(3):
        notifications.notify(notifications.Event("job_failed", f"e{i}", "b"))
    await asyncio.sleep(0.1)  # e0 in flight, e1 and e2 queued
    await client.delete(dest["id"])
    release.set()
    await notifications.drain()
    assert got == ["e0"]
    assert not notifications._queues and not notifications._workers


@pytest.mark.asyncio
async def test_queued_messages_use_the_url_current_when_they_are_sent(
        client, rec, monkeypatch):
    dest = await _add(client, "https://old.example/x")
    release = threading.Event()
    urls = []

    def transport(url, event):
        release.wait(2)
        urls.append(url)

    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", transport)
    for i in range(2):
        notifications.notify(notifications.Event("job_failed", f"e{i}", "b"))
    await asyncio.sleep(0.1)
    await client.edit(dest["id"], url="https://new.example/x")
    release.set()
    await notifications.drain()
    assert urls == ["https://old.example/x", "https://new.example/x"]
    assert not notifications._queues and not notifications._workers


@pytest.mark.asyncio
async def test_a_platform_a_sweep_did_not_inspect_keeps_its_reported_error(client, rec):
    await _add(client)
    err = {"platform_id": 7, "error": "tool_not_ready"}
    other = {"platform_id": 8}
    notifications.sweep_finished({"queued": 0, "errors": [err], "platforms": [other]})
    # A later sweep never reached platform 7 (filtered, or the cap stopped early).
    notifications.sweep_finished({"queued": 0, "errors": [], "platforms": [other]})
    notifications.sweep_finished({"queued": 0, "errors": [err], "platforms": [other]})
    await notifications.drain()
    assert len(rec.sent) == 1
    # Checked and clean: forgotten, so a recurrence is news.
    notifications.sweep_finished(
        {"queued": 0, "errors": [], "platforms": [{"platform_id": 7}]})
    notifications.sweep_finished({"queued": 0, "errors": [err], "platforms": [other]})
    await notifications.drain()
    assert len(rec.sent) == 2


# --- the real webhook transport (local server, no third party) -------------


def _serve(status=200, location=None):
    import json as _json
    from http.server import BaseHTTPRequestHandler, HTTPServer

    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(_json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, got


def test_webhook_posts_json_and_reports_http_errors():
    srv, got = _serve()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/hook"
        notifications._send_webhook(url, notifications.Event("job_failed", "T", "B", {"a": 1}))
        assert got == [{"event": "job_failed", "title": "T", "message": "B", "data": {"a": 1}}]
    finally:
        srv.shutdown()
    bad, _ = _serve(status=500)
    try:
        with pytest.raises(notifications.NotificationError, match="HTTP 500"):
            notifications._send_webhook(
                f"http://127.0.0.1:{bad.server_port}/secret-token", _event())
    finally:
        bad.shutdown()


def test_unreachable_webhook_error_does_not_leak_the_url():
    with pytest.raises(notifications.NotificationError) as exc:
        notifications._send_webhook("http://127.0.0.1:9/secret-token", _event())
    assert "secret-token" not in str(exc.value)


def test_apprise_transport_without_the_package_says_so(monkeypatch):
    import builtins
    real = builtins.__import__

    def deny(name, *a, **k):
        if name == "apprise":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)
    with pytest.raises(notifications.NotificationError, match="Apprise is not installed"):
        notifications._send_apprise("discord://a/b", _event())
