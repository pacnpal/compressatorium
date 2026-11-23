"""Redacting a destination's URL leaves the app's own words readable (#567).

``_redact`` replaces every 4+ character piece of the URL wherever it appears,
so a webhook at ``http://ha:8123/api/webhook/<id>`` turned "Could not reach the
webhook: Connection refused" into "Could not reach the <redacted>: ...". The
module's own messages now say which of their text came from elsewhere, and
only that is redacted; a message passed whole (a transport someone else
wrote) is still redacted whole.
"""
from __future__ import annotations

import logging

import pytest
from services import notifications

from .test_notifications import Api, Recorder, _engine  # noqa: F401  (autouse fixture)

_HOOK = "https://ha.example:8123/api/webhook/TOKEN123abc"


@pytest.fixture
def client():
    return Api()


@pytest.fixture
def failing(monkeypatch):
    rec = Recorder()
    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", rec.make("webhook"))
    return rec


@pytest.mark.asyncio
async def test_the_app_s_own_words_survive_a_url_that_repeats_them(client, failing):
    failing.fail_with = notifications.NotificationError.said(
        "Could not reach the webhook: {reason}", reason="Connection refused",
    )

    out = await client.test_draft("webhook", _HOOK)

    assert out == {"ok": False, "error": "Could not reach the webhook: Connection refused"}


@pytest.mark.asyncio
async def test_a_foreign_part_is_still_redacted(client, failing):
    failing.fail_with = notifications.NotificationError.said(
        "Could not reach the webhook: {reason}", reason="no route to TOKEN123abc",
    )

    out = await client.test_draft("webhook", _HOOK)

    assert "TOKEN123abc" not in out["error"]
    assert out["error"].startswith("Could not reach the webhook: ")


@pytest.mark.asyncio
async def test_a_message_passed_whole_is_redacted_whole(client, failing):
    failing.fail_with = notifications.NotificationError(f"cannot POST {_HOOK} (webhook)")

    out = await client.test_draft("webhook", _HOOK)

    assert "TOKEN123abc" not in out["error"] and "8123" not in out["error"]


@pytest.mark.asyncio
async def test_the_log_keeps_the_app_s_words_too(client, failing, caplog):
    status, _ = await client.add("webhook", _HOOK)
    assert status == 200
    failing.fail_with = notifications.NotificationError.said(
        "The webhook answered HTTP {status}", status=404,
    )
    caplog.set_level(logging.WARNING)

    await notifications.deliver(notifications.Event("job_failed", "t", "b"))

    assert "The webhook answered HTTP 404" in caplog.text
    assert "TOKEN123abc" not in caplog.text


def test_every_message_this_module_raises_names_its_foreign_parts():
    """Its own raises go through ``said``, and none formats foreign text into
    the template (an f-string would)."""
    import inspect
    import re

    source = inspect.getsource(notifications)
    assert "raise NotificationError(" not in source
    assert not re.search(r"NotificationError\.said\(\s*f[\"']", source)


def test_a_part_keeps_its_format_spec():
    """Codex on #581: the parts were turned into strings before the template
    was applied again, so ``{limit:g}`` raised ValueError on a str and a
    timed-out Test answered 500. Each part is formatted with its spec first,
    then redacted."""
    err = notifications.NotificationError.said("Timed out after {limit:g}s", limit=10.0)
    assert err.redacted(_HOOK) == "Timed out after 10s"
    assert str(err) == "Timed out after 10s"
    tagged = notifications.NotificationError.said("got {what!r}", what="TOKEN123abc")
    assert "TOKEN123abc" not in tagged.redacted(_HOOK)


@pytest.mark.asyncio
async def test_a_timed_out_test_answers_with_the_reason(client, failing):
    failing.fail_with = notifications.NotificationError.said(
        "Timed out after {limit:g}s", limit=notifications.SEND_TIMEOUT_S,
    )

    out = await client.test_draft("webhook", _HOOK)

    assert out["ok"] is False and out["error"].startswith("Timed out after ")
