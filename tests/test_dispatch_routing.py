"""Delegation-parity tests for the convert/verify dispatch routed through the
tool registry in ``job_manager._process_job`` (design Phase 3).

The real CLIs cannot run in the sandbox, so these assert that the *selection*
is correct: ``registry.for_mode(mode).convert`` / ``.verify`` reach the same
underlying service the legacy dispatch ladders would have chosen.  Each tool
delegates to its service via the wrapper's ``_service`` attribute, so patching
``_service`` there observes exactly what ``_process_job`` would dispatch to.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import ConversionMode
from app.services.tools import registry

EXTERNAL_MODES = {ConversionMode.METADATA_SCAN, ConversionMode.DAT_MATCH}
CONVERSION_MODES = [m.value for m in ConversionMode if m not in EXTERNAL_MODES]


def _legacy_dispatch_id(mode: str) -> str:
    """Replicates the convert/verify dispatch ladders formerly in
    ``_process_job`` (job_manager.py:1464 / :1576 / :1611).  Both ladders make
    the same tool selection, differing only in the progress message."""
    if mode.startswith("dolphin_"):
        return "dolphin"
    if mode == ConversionMode.Z3DS_COMPRESS.value:
        return "z3ds"
    if mode.startswith("nsz_"):
        return "nsz"
    if mode.startswith(("cso_", "zso_")):
        return "cso"
    return "chdman"


@pytest.mark.parametrize("mode", CONVERSION_MODES)
def test_verify_dispatch_matches_legacy_ladder(mode, monkeypatch):
    called: dict[str, str] = {}

    def _record(tool_id):
        async def _verify(path):
            called["id"] = tool_id
            return {"valid": True, "message": "ok"}

        return _verify

    for tool_id in ("chdman", "dolphin", "z3ds", "nsz", "cso"):
        monkeypatch.setattr(
            registry.get(tool_id)._service, "verify", _record(tool_id)
        )

    result = asyncio.run(registry.for_mode(mode).verify("/data/out"))

    assert result == {"valid": True, "message": "ok"}
    assert called["id"] == _legacy_dispatch_id(mode)


@pytest.mark.parametrize("mode", CONVERSION_MODES)
def test_convert_dispatch_matches_legacy_ladder(mode, monkeypatch):
    called: dict[str, str] = {}

    def _record(tool_id):
        def _convert(input_path, output_path, mode_, *, compression=None,
                     cancel_event=None):
            called["id"] = tool_id

            async def _gen():
                yield {"progress": 100, "message": "done"}

            return _gen()

        return _convert

    for tool_id in ("chdman", "dolphin", "z3ds", "nsz", "cso"):
        monkeypatch.setattr(
            registry.get(tool_id)._service, "convert", _record(tool_id)
        )

    async def _drain():
        return [
            u
            async for u in registry.for_mode(mode).convert(
                "/data/in", "/data/out", mode, compression=None, cancel_event=None
            )
        ]

    updates = asyncio.run(_drain())

    assert updates == [{"progress": 100, "message": "done"}]
    assert called["id"] == _legacy_dispatch_id(mode)


def test_external_modes_never_resolve():
    for mode in EXTERNAL_MODES:
        with pytest.raises(KeyError):
            registry.for_mode(mode.value)
