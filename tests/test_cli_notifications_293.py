"""`compressatorium run-rules` sends the sweep summary a server would (#293).

The CLI's job runner registers the same notification listener the server does
(one shared function) and drains the sends before the loop closes, so a pass
that converts an Image reports it once, before the process exits.
"""
from __future__ import annotations

import asyncio

import pytest
from services import notifications

from . import test_cli_run_rules_307 as rr
from .test_cli_run_rules_307 import RVZ_RULE, _cli, _iso, _store_rules

# The 307 fixtures, under their own names.
env = pytest.fixture(name="env")(rr._env.__wrapped__ if hasattr(rr._env, "__wrapped__") else rr._env)
dolphin = pytest.fixture(name="dolphin")(rr._dolphin.__wrapped__ if hasattr(rr._dolphin, "__wrapped__") else rr._dolphin)


def _save_destination(url: str = "https://hooks.example/t") -> None:
    from services.preferences_store import preferences_store

    asyncio.run(preferences_store.put(notifications.PREF_KEY, {"destinations": [
        {"id": "d1", "kind": "webhook", "url": url, "label": ""}]}))


def test_a_run_rules_pass_sends_one_sweep_summary_before_exit(env, dolphin, capsys, monkeypatch):
    sent: list[notifications.Event] = []
    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", lambda url, ev: sent.append(ev))
    notifications._reset_for_tests()
    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    _save_destination()

    code, _lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 0
    assert [e.kind for e in sent] == ["sweep_finished"]
    assert sent[0].data["queued"] == 1 and sent[0].data["completed"] == 1


def test_a_failed_job_in_a_pass_is_inside_the_one_summary(env, dolphin, capsys, monkeypatch):
    sent: list[notifications.Event] = []
    monkeypatch.setitem(notifications.TRANSPORTS, "webhook", lambda url, ev: sent.append(ev))
    notifications._reset_for_tests()
    dolphin["fail"] = True
    _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})
    _save_destination()

    code, _lines, _err = _cli(capsys, "run-rules", "--json")

    assert code == 1
    assert [e.kind for e in sent] == ["sweep_finished"]
    assert sent[0].data["failed"] == 1
