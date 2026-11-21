"""Notifications (#523): per-destination events, a success event, signed webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from models import ConversionJob, ConversionMode, JobStatus
from routes import notifications as routes
from services import db as _db
from services import notifications


@pytest.fixture(autouse=True)
def _engine(db_path, reset_db_engine):
    _db.init_engine(db_path)
    notifications._reset_for_tests()
    yield
    reset_db_engine()


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, notifications.Event, str | None]] = []

    def send(url, event, secret=None):
        out.append((url, event, secret))

    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", send)
    monkeypatch.setitem(notifications.TRANSPORTS, "apprise", send)
    monkeypatch.setattr(notifications, "apprise_available", lambda: True)
    return out


def _job(status, jid="j1") -> ConversionJob:
    now = datetime.now(timezone.utc)
    return ConversionJob(
        id=jid, file_path="/data/ps2/Game.iso", filename="Game.iso",
        mode=ConversionMode.CREATEDVD, status=status, created_at=now, completed_at=now,
        error_message="boom" if status == JobStatus.FAILED else None,
    )


async def _add(url, kind="webhook", **body):
    return await routes.add_destination(routes.NewDestination(kind=kind, url=url, **body))


@pytest.mark.asyncio
async def test_each_destination_gets_only_the_events_it_chose(sent):
    await _add("https://a.example/hook", events=["job_failed"])
    await _add("https://b.example/hook", events=["sweep_finished"])
    await notifications.on_job_finished(_job(JobStatus.FAILED))
    await notifications.drain()
    assert [url for url, _, _ in sent] == ["https://a.example/hook"]


@pytest.mark.asyncio
async def test_a_destination_that_never_chose_gets_the_defaults(sent):
    dest = await _add("https://a.example/hook")
    keys = {e["key"] for e in notifications.event_list() if e["default"]}
    assert set(dest["events"]) == keys
    assert "job_failed" in keys and "job_succeeded" not in keys


@pytest.mark.asyncio
async def test_an_unknown_event_is_refused():
    with pytest.raises(Exception) as exc:
        await _add("https://a.example/hook", events=["nope"])
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_events_can_be_changed_later(sent):
    dest = await _add("https://a.example/hook", events=["sweep_finished"])
    edited = await routes.edit_destination(
        dest["id"], routes.DestinationEdit(events=["job_failed"]))
    assert edited["events"] == ["job_failed"]
    await notifications.on_job_finished(_job(JobStatus.FAILED))
    await notifications.drain()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_job_you_started_that_succeeds_sends_a_success_message(sent):
    await _add("https://a.example/hook", events=["job_succeeded"])
    await notifications.on_job_finished(_job(JobStatus.COMPLETED))
    await notifications.on_job_finished(_job(JobStatus.FAILED, jid="j2"))
    await notifications.drain()
    assert len(sent) == 1
    event = sent[0][1]
    assert event.kind == "job_succeeded" and "Game.iso" in event.title


@pytest.mark.asyncio
async def test_a_sweeps_job_that_succeeds_is_not_sent_alone(sent):
    await _add("https://a.example/hook", events=["job_succeeded"])
    notifications.sweep_owner()
    job = _job(JobStatus.PROCESSING)
    notifications.sweep_jobs_queued([job])
    job.status = JobStatus.COMPLETED
    await notifications.on_job_finished(job)
    await notifications.drain()
    assert sent == []


@pytest.mark.asyncio
async def test_the_secret_is_never_returned_and_reaches_the_transport(sent):
    dest = await _add("https://a.example/hook", secret="s3cret", events=["job_failed"])
    assert dest["signed"] is True and "s3cret" not in json.dumps(dest)
    listed = await routes.get_notifications()
    assert "s3cret" not in json.dumps(listed)
    await notifications.on_job_finished(_job(JobStatus.FAILED))
    await notifications.drain()
    assert sent[0][2] == "s3cret"
    # Omitted keeps it; "" clears it.
    kept = await routes.edit_destination(dest["id"], routes.DestinationEdit(label="x"))
    assert kept["signed"] is True
    cleared = await routes.edit_destination(dest["id"], routes.DestinationEdit(secret=""))
    assert cleared["signed"] is False


@pytest.mark.asyncio
async def test_only_the_generic_webhook_takes_a_secret(sent):
    with pytest.raises(Exception) as exc:
        await _add("discord://id/token", kind="apprise", secret="s3cret")
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_the_test_button_signs_with_the_saved_secret(sent):
    dest = await _add("https://a.example/hook", secret="s3cret")
    result = await routes.test_saved_destination(dest["id"])
    assert result["ok"] and sent[-1][2] == "s3cret"


def test_the_webhook_body_is_signed_with_hmac_sha256():
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            got["sig"] = self.headers.get(notifications.SIGNATURE_HEADER)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/hook"
        notifications._send_webhook(url, notifications.Event("job_failed", "T", "B"), "k")
        want = hmac.new(b"k", got["body"], hashlib.sha256).hexdigest()
        assert got["sig"] == f"sha256={want}"
        notifications._send_webhook(url, notifications.Event("job_failed", "T", "B"))
        assert got["sig"] is None
    finally:
        srv.shutdown()
