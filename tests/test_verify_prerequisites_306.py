"""A check that could not run is no verdict on the file (#306, PR review).

Missing prerequisites (nsz without prod.keys) and a verifier that failed to
launch or raised are reported ``inconclusive``, so a caller holding a stored
check (the CLI's verify) keeps it.
"""
from __future__ import annotations

import asyncio

from services.maxcso import MaxcsoService
from services.nsz import NszService
from services.subprocess_runner import collect_verify


def test_nsz_without_keys_is_inconclusive(tmp_path, monkeypatch):
    nsz = tmp_path / "Game.nsz"
    nsz.write_bytes(b"x" * 64)
    service = NszService()
    monkeypatch.setattr(service, "resolved_keys_file", lambda: None)

    result = asyncio.run(collect_verify(
        service.verify_stream(str(nsz)), fallback_message="failed",
    ))

    assert result["valid"] is False
    assert result.get("inconclusive") is True
    assert "prod.keys" in result["message"]


def test_a_verifier_that_cannot_launch_is_inconclusive(tmp_path, monkeypatch):
    cso = tmp_path / "Game.cso"
    cso.write_bytes(b"x" * 64)
    service = MaxcsoService()

    async def no_binary(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "maxcso")
        yield  # pragma: no cover

    monkeypatch.setattr(service._runner, "capture_verify", no_binary)

    result = asyncio.run(collect_verify(
        service.verify_stream(str(cso)), fallback_message="failed",
    ))

    assert result["valid"] is False
    assert result.get("inconclusive") is True
