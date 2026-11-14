"""Notifications (#293): webhook and Apprise destinations, sweep and failed-job messages."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(notification_routes.router, prefix="/api")
    return TestClient(app)


def _job(status=JobStatus.FAILED, jid="j1", name="Game.iso", **kw) -> ConversionJob:
    now = datetime.now(timezone.utc)
    return ConversionJob(
        id=jid, file_path=f"/data/ps2/{name}", filename=name,
        mode=ConversionMode.CREATEDVD, status=status, created_at=now, completed_at=now,
        error_message=kw.pop("error_message", "boom"), **kw,
    )


def _add(client, url="https://hooks.example/abc?token=SECRET", kind="webhook", label="Phone"):
    r = client.post("/api/notifications/destinations",
                    json={"kind": kind, "url": url, "label": label})
    assert r.status_code == 200, r.text
    return r.json()


def _event():
    return notifications.Event("job_failed", "t", "b")


# --- destinations -------------------------------------------------------


@pytest.mark.asyncio
async def test_add_edit_remove_and_never_return_the_url(client, rec):
    dest = _add(client)
    assert "url" not in dest and "SECRET" not in str(dest)
    listing = client.get("/api/notifications").json()
    assert [d["label"] for d in listing["destinations"]] == ["Phone"]
    assert "SECRET" not in str(listing)
    assert {e["key"] for e in listing["events"]} >= {"sweep_finished", "job_failed"}

    edited = client.put(f"/api/notifications/destinations/{dest['id']}",
                        json={"label": "Tablet"}).json()
    assert edited["label"] == "Tablet" and "url" not in edited
    # Omitting the url keeps the stored one.
    await notifications.deliver(_event())
    assert rec.sent[-1][1] == "https://hooks.example/abc?token=SECRET"

    client.put(f"/api/notifications/destinations/{dest['id']}",
               json={"url": "https://other.example/x"})
    await notifications.deliver(_event())
    assert rec.sent[-1][1] == "https://other.example/x"

    assert client.delete(f"/api/notifications/destinations/{dest['id']}").status_code == 200
    assert client.get("/api/notifications").json()["destinations"] == []
    assert client.delete(f"/api/notifications/destinations/{dest['id']}").status_code == 404


@pytest.mark.parametrize("url", ["", "ftp://x/y", "file:///etc/passwd", "not a url"])
def test_webhook_url_must_be_http(client, rec, url):
    r = client.post("/api/notifications/destinations", json={"kind": "webhook", "url": url})
    assert r.status_code in (400, 422)


def test_apprise_is_refused_with_a_clear_message_when_not_installed(client, monkeypatch):
    monkeypatch.setattr(notifications, "apprise_available", lambda: False)
    r = client.post("/api/notifications/destinations",
                    json={"kind": "apprise", "url": "discord://id/token"})
    assert r.status_code == 400 and "Apprise is not installed" in r.json()["detail"]
    assert client.get("/api/notifications").json()["apprise_available"] is False
    # Webhooks still work.
    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", lambda u, e: None)
    _add(client)


def test_test_button_reports_success_and_error(client, rec):
    dest = _add(client)
    ok = client.post(f"/api/notifications/destinations/{dest['id']}/test").json()
    assert ok == {"ok": True, "error": None}
    assert rec.sent[-1][2].kind == "test"

    rec.fail_with = RuntimeError("HTTP 500")
    bad = client.post(f"/api/notifications/destinations/{dest['id']}/test").json()
    assert bad["ok"] is False and "HTTP 500" in bad["error"]

    # An unsaved draft can be tested too.
    rec.fail_with = None
    draft = client.post("/api/notifications/test",
                        json={"kind": "webhook", "url": "https://x.example/h"}).json()
    assert draft["ok"] is True and rec.sent[-1][1] == "https://x.example/h"


# --- sending --------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_sent_without_a_destination(rec):
    await notifications.on_job_finished(_job())
    await notifications.deliver(_event())
    await notifications.drain()
    assert rec.sent == []


@pytest.mark.asyncio
async def test_a_failing_destination_does_not_stop_the_others(client, rec, caplog):
    _add(client, "https://a.example/1", label="A")
    _add(client, "discord://x/y", kind="apprise", label="B")
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
    _add(client)
    gate = threading.Event()
    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", lambda u, e: gate.wait(5))
    monkeypatch.setattr(notifications, "SEND_TIMEOUT_S", 0.05)
    started = asyncio.get_running_loop().time()
    await notifications.deliver(_event())
    assert asyncio.get_running_loop().time() - started < 1
    gate.set()


@pytest.mark.asyncio
async def test_notify_returns_at_once_and_sends_in_the_background(client, rec):
    _add(client)
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
    _add(client)
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
    _add(client)
    jobs = [_job(JobStatus.COMPLETED, f"s{i}", f"G{i}.iso") for i in range(2)]
    bad = _job(JobStatus.FAILED, "sf", "Bad.iso", error_message="disc read error")
    notifications.sweep_jobs_queued(jobs + [bad])
    notifications.sweep_finished({"queued": 3, "errors": [
        {"platform_id": 9, "error": "queue_failed"}]})
    for j in jobs:
        j.input_bytes, j.output_size, j.deleted_paths = 1000, 400, ["/x"]
        await notifications.on_job_finished(j)
    await notifications.drain()
    assert rec.sent == []  # still waiting on the last job
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
    _add(client)
    notifications.sweep_finished({"queued": 0, "errors": []})
    notifications.sweep_finished({"queued": 0, "errors": []}, dry_run=True)
    await notifications.drain()
    assert rec.sent == []


@pytest.mark.asyncio
async def test_sweep_with_only_queue_errors_still_reports(client, rec):
    _add(client)
    notifications.sweep_finished({"queued": 0, "errors": [
        {"platform_id": 1, "error": "sources_unreadable"}]})
    await notifications.drain()
    assert len(rec.sent) == 1 and "sources_unreadable" in rec.sent[0][2].body


@pytest.mark.asyncio
async def test_job_that_finishes_before_the_sweep_closes_is_still_counted(client, rec):
    _add(client)
    job = _job(JobStatus.FAILED, "early", "Early.iso")
    notifications.sweep_jobs_queued([job])
    await notifications.on_job_finished(job)  # ends before the sweep returns
    await notifications.drain()
    assert rec.sent == []  # a sweep job never gets a lone failure message
    notifications.sweep_finished({"queued": 1, "errors": []})
    await notifications.drain()
    assert len(rec.sent) == 1 and rec.sent[0][2].data["failed"] == 1


def test_events_are_a_shared_registry():
    notifications.register_event("check_failed", "A check failed")
    assert "check_failed" in {e["key"] for e in notifications.event_list()}


# --- the real webhook transport (local server, no third party) -------------


def _serve(status=200):
    import json as _json
    from http.server import BaseHTTPRequestHandler, HTTPServer

    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(_json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
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
