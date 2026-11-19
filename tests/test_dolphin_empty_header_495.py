"""A dolphin-tool header read that yields nothing is a failed read (#495).

For a junk ``.rvz``, ``dolphin-tool header`` exits 0 with no output. That used
to parse into an empty dict, so File info answered 200 with every field null.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from services.dolphin_tool import DolphinToolService

from app.routes import info as info_routes


def _fake_dolphin(tmp_path, body: str) -> DolphinToolService:
    script = tmp_path / "fake_dolphin.py"
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(0o755)
    service = DolphinToolService()
    service.dolphin_tool_path = str(script)
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "print('   ')\nprint('no colon here')\n"])
async def test_header_with_no_parsed_fields_raises(tmp_path, body):
    service = _fake_dolphin(tmp_path, body)
    with pytest.raises(RuntimeError, match="could not read a header"):
        await service.header(str(tmp_path / "bad.rvz"))


@pytest.mark.asyncio
async def test_header_with_fields_still_parses(tmp_path):
    service = _fake_dolphin(tmp_path, "print('Game ID: GALE01')\n")
    info = await service.header(str(tmp_path / "ok.rvz"))
    assert info["game_id"] == "GALE01"


@pytest.mark.asyncio
async def test_dolphin_info_route_is_not_200_all_null_for_empty_header(
    tmp_path, monkeypatch
):
    bad = tmp_path / "bad.rvz"
    bad.write_bytes(b"\x00junk" * 100)
    service = _fake_dolphin(tmp_path, "")
    monkeypatch.setattr(info_routes, "dolphin_tool_service", service)

    async def ready(_tool_id):
        return None

    monkeypatch.setattr(info_routes, "_require_info_ready", ready)
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    with pytest.raises(HTTPException) as exc_info:
        await info_routes.get_dolphin_info(path=str(bad))
    assert exc_info.value.status_code == 500
    assert "could not read a header" in exc_info.value.detail
