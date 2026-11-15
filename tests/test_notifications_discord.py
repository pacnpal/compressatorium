"""Discord-compatible webhook destinations (#293): a native kind, no Apprise."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from routes import notifications as notification_routes
from services import db as _db
from services import notifications

DISCORD_URL = "https://discord.com/api/webhooks/123456789/SECRETTOKEN-abc_DEF"


@pytest.fixture(autouse=True)
def _engine(db_path, reset_db_engine):
    _db.init_engine(db_path)
    notifications._reset_for_tests()
    yield
    reset_db_engine()


def _event(kind="sweep_finished", title="Sweep finished", body="b", **data):
    return notifications.Event(kind, title, body, data)


# --- payload ---------------------------------------------------------------


def test_payload_shape_and_empty_mention_list():
    ev = _event(queued=3, completed=3, failed=0, saved_bytes=1200, failures=[])
    payload = notifications.discord_payload(ev)
    assert payload["username"] == "Compressatorium"
    assert payload["content"] == "Sweep finished"
    assert payload["allowed_mentions"] == {"parse": []}
    embed = payload["embeds"][0]
    assert embed["title"] == "Sweep finished" and embed["description"] == "b"
    assert embed["timestamp"].endswith("+00:00")
    names = {f["name"] for f in embed["fields"]}
    assert {"Queued", "Completed", "Failed", "Saved bytes"} <= names
    assert "Failures" not in names  # only scalar values become fields
    json.dumps(payload)


def test_colours_follow_the_outcome():
    ok = notifications.discord_payload(_event(failed=0))["embeds"][0]["color"]
    bad_sweep = notifications.discord_payload(_event(failed=2))["embeds"][0]["color"]
    bad_job = notifications.discord_payload(_event("job_failed", "x"))["embeds"][0]["color"]
    other = notifications.discord_payload(_event("test", "t"))["embeds"][0]["color"]
    assert bad_sweep == bad_job and ok != bad_sweep and other not in (ok, bad_sweep)


def test_limits_are_enforced_by_truncating_with_an_ellipsis():
    ev = notifications.Event(
        "job_failed", "T" * 5000, "D" * 9000,
        {f"k{i}": "V" * 3000 for i in range(30)},
    )
    payload = notifications.discord_payload(ev)
    embed = payload["embeds"][0]
    assert len(payload["content"]) <= 2000 and payload["content"].endswith("…")
    assert len(embed["title"]) <= 256
    assert len(embed["description"]) <= 4096
    assert len(embed["fields"]) <= 10
    assert all(len(f["value"]) <= 1024 and len(f["name"]) <= 256 for f in embed["fields"])
    total = len(embed["title"]) + len(embed["description"]) + sum(
        len(f["name"]) + len(f["value"]) for f in embed["fields"])
    assert total <= 6000


def test_an_everyone_ping_in_a_filename_is_not_parsed():
    ev = _event("job_failed", "Job failed: @everyone.iso", "<@&123> boom")
    assert notifications.discord_payload(ev)["allowed_mentions"] == {"parse": []}


def test_the_generic_webhook_payload_is_unchanged():
    ev = _event("job_failed", "T", "B", a=1)
    assert notifications.generic_payload(ev) == {
        "event": "job_failed", "title": "T", "message": "B", "data": {"a": 1}}


# --- the kind registry -----------------------------------------------------


def test_discord_is_a_registered_kind_with_one_transport():
    assert "discord" in notifications.KINDS
    assert "discord" in notifications.TRANSPORTS
    kinds = {k["value"]: k for k in notifications.kind_list()}
    assert set(kinds) == set(notifications.KINDS)
    assert all(k["label"] and k["placeholder"] for k in kinds.values())


@pytest.mark.parametrize("url", [
    "http://discord.com/api/webhooks/1/x", "ftp://x/y", "https://h/a b", "", "https://",
])
def test_discord_requires_a_clean_https_url(url):
    with pytest.raises(notifications.NotificationError):
        notifications.validate("discord", url)


def test_discord_accepts_any_https_service_that_speaks_the_format():
    assert notifications.validate("discord", DISCORD_URL) == DISCORD_URL
    assert notifications.validate("discord", "https://chat.example.org/hooks/x/discord")


def test_the_hint_shows_only_the_host():
    dest = {"id": "d", "kind": "discord", "url": DISCORD_URL, "label": ""}
    view = notifications.public_view(dest)
    assert view["hint"] == "discord.com" and "SECRETTOKEN" not in str(view)


# --- sending ---------------------------------------------------------------


def _serve(status=200, headers=None, drip=False):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    got: list[dict] = []
    stop = threading.Event()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if drip:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            while drip and not stop.is_set():
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.1)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, got, stop


def test_the_discord_transport_posts_the_discord_payload_and_204_is_success():
    srv, got, _ = _serve(status=204)
    try:
        notifications.TRANSPORTS["discord"](
            f"http://127.0.0.1:{srv.server_port}/api/webhooks/1/tok", _event("job_failed", "J"))
    finally:
        srv.shutdown()
    assert got[0]["embeds"][0]["title"] == "J" and got[0]["allowed_mentions"] == {"parse": []}


def test_a_429_names_the_rate_limit_and_the_retry_after_without_retrying():
    srv, got, _ = _serve(status=429, headers={"Retry-After": "7"})
    try:
        with pytest.raises(notifications.NotificationError) as exc:
            notifications.TRANSPORTS["discord"](
                f"http://127.0.0.1:{srv.server_port}/api/webhooks/1/tok", _event())
    finally:
        srv.shutdown()
    text = str(exc.value)
    assert "429" in text and "rate" in text.lower() and "7" in text
    assert len(got) == 1  # no retry loop


@pytest.mark.parametrize("kind", ["webhook", "discord"])
@pytest.mark.asyncio
async def test_a_dripping_body_cannot_outlive_the_deadline_for_either_kind(
        kind, monkeypatch):
    from services.preferences_store import preferences_store

    srv, got, stop = _serve(drip=True)
    monkeypatch.setattr(notifications, "SEND_TIMEOUT_S", 0.6)
    await preferences_store.put(notifications.PREF_KEY, {"destinations": [
        {"id": "d1", "kind": kind, "url": f"http://127.0.0.1:{srv.server_port}/h", "label": ""}]})
    try:
        started = time.monotonic()
        await notifications.deliver(_event("job_failed", "x"))
        assert time.monotonic() - started < 1.5
        await notifications.deliver(_event("job_failed", "y"))  # not left busy
        assert len(got) == 2
    finally:
        stop.set()
        srv.shutdown()


# --- secrets ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_discord_token_never_reaches_a_log_or_the_test_response(caplog):
    def boom(url, event):
        raise notifications.NotificationError(f"cannot POST {url}")

    notifications.TRANSPORTS["discord"] = boom
    await notifications.add_destination("discord", DISCORD_URL, "Server")
    await notifications.deliver(_event())
    result = await notifications.test_destination(kind="discord", url=DISCORD_URL)
    for text in (caplog.text, str(result)):
        assert "SECRETTOKEN" not in text and "123456789" not in text


@pytest.mark.asyncio
async def test_the_api_lists_the_kinds_and_adds_a_discord_destination():
    listing = await notification_routes.get_notifications()
    assert {k["value"] for k in listing["kinds"]} == set(notifications.KINDS)
    body = notification_routes.NewDestination(kind="discord", url=DISCORD_URL, label="Hook")
    view = await notification_routes.add_destination(body)
    assert view["kind"] == "discord" and view["hint"] == "discord.com"
    assert "SECRETTOKEN" not in str(await notification_routes.get_notifications())


# --- the Settings form -----------------------------------------------------


def test_the_settings_form_offers_all_three_kinds_from_the_server_list():
    src = Path(__file__).resolve().parents[1] / "src/lib/components/views"
    text = (src / "NotificationSettings.svelte").read_text()
    # Registry-driven: the picker is built from the kinds the API returns.
    assert "kinds" in text and "placeholder" in text
    labels = {k["label"] for k in notifications.kind_list()}
    assert {"Webhook (generic JSON)", "Discord webhook", "Apprise"} <= labels


# --- a stuck transport cannot hold the process at exit ----------------------


def test_a_transport_that_never_returns_does_not_hold_the_process_at_exit(tmp_path):
    """Executor workers are joined at interpreter shutdown, so a send that never
    returns (a slow-drip Apprise server) used to hang a CLI command after its
    work was done. Transports now run on daemon threads."""
    import subprocess
    import sys
    import textwrap

    script = tmp_path / "send_and_exit.py"
    script.write_text(textwrap.dedent("""
        import asyncio, sys, threading
        sys.path.insert(0, sys.argv[2])
        from services import db, notifications

        db.init_engine(sys.argv[1])
        notifications.SEND_TIMEOUT_S = 0.5
        notifications.TRANSPORTS["webhook"] = lambda url, ev: threading.Event().wait()

        async def main():
            await notifications.add_destination("webhook", "https://h.example/x", None)
            notifications.notify(notifications.Event("job_failed", "t", "b"))
            await notifications.drain(timeout=2)

        asyncio.run(main())
        print("done")
    """))
    app_dir = Path(__file__).resolve().parents[1] / "app"
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-I", str(script), str(tmp_path / "db.sqlite"), str(app_dir)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "done" in done.stdout
    assert time.monotonic() - started < 10
