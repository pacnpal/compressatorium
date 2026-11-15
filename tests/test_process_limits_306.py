"""Raise the soft open-file limit to the hard limit at startup (#306)."""
from __future__ import annotations

import resource
from pathlib import Path

from utils.process_limits import raise_open_file_limit


def test_soft_limit_is_raised_to_the_hard_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 4096))
    monkeypatch.setattr(resource, "setrlimit", lambda which, lim: calls.append((which, lim)))

    assert raise_open_file_limit() == 4096
    assert calls == [(resource.RLIMIT_NOFILE, (4096, 4096))]


def test_an_unlimited_hard_limit_is_capped(monkeypatch):
    calls = []
    monkeypatch.setattr(
        resource, "getrlimit", lambda which: (1024, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(resource, "setrlimit", lambda which, lim: calls.append(lim))

    raise_open_file_limit()

    assert calls
    assert calls[0][0] != resource.RLIM_INFINITY
    assert calls[0][0] > 1024


def test_a_refusal_is_not_fatal(monkeypatch):
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 4096))

    def refuse(which, lim):
        raise ValueError("not allowed")

    monkeypatch.setattr(resource, "setrlimit", refuse)

    assert raise_open_file_limit() == 256


def test_server_and_cli_raise_it_at_startup():
    app = Path(__file__).resolve().parents[1] / "app"
    assert "raise_open_file_limit()" in (app / "main.py").read_text()
    assert "raise_open_file_limit()" in (app / "cli.py").read_text()
