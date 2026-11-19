"""A tool whose binary is missing or not executable is not ready (#494).

``BaseTool.is_ready`` probes ``binary_path``, so every tool that runs an
external program (chdman, dolphin, maxcso, z3ds, makeps3iso, romz, nkit2iso,
xiso) drops out of ``GET /api/tools`` and its info route answers 503 instead
of a 500 from a failed spawn.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from services.tools import not_ready_message, registry

from app.routes import info as info_routes

pytestmark = pytest.mark.real_binaries

BINARY_TOOLS = [
    "chdman", "dolphin", "cso", "z3ds", "makeps3iso", "romz", "nkit", "xiso",
]


def _missing(tmp_path):
    return str(tmp_path / "nope")


def _not_executable(tmp_path):
    path = tmp_path / "tool"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o644)
    return str(path)


def _executable(tmp_path):
    path = tmp_path / "tool"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", BINARY_TOOLS)
@pytest.mark.parametrize(
    ("make", "says"), [(_missing, "was not found"), (_not_executable, "is not executable")],
)
async def test_a_broken_binary_drops_the_tool_from_the_list(
    tmp_path, monkeypatch, tool_id, make, says,
):
    tool = registry.get(tool_id)
    monkeypatch.setattr(tool, "binary_path", make(tmp_path))

    listed = await info_routes.list_tools()

    assert tool_id in listed["unavailable"]
    assert tool_id not in listed["available"]
    assert says in not_ready_message(tool)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", BINARY_TOOLS)
async def test_an_executable_binary_is_ready(tmp_path, monkeypatch, tool_id):
    tool = registry.get(tool_id)
    monkeypatch.setattr(tool, "binary_path", _executable(tmp_path))

    assert await registry.tool_is_ready(tool) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_missing, _not_executable])
async def test_chd_info_answers_503_when_chdman_is_broken(tmp_path, monkeypatch, make):
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    tool = registry.get("chdman")
    monkeypatch.setattr(tool, "binary_path", make(tmp_path))
    src = tmp_path / "Game.chd"
    src.write_bytes(b"MComprHD" + b"\0" * 120)

    with pytest.raises(HTTPException) as caught:
        await info_routes.get_chd_info(path=str(src))

    assert caught.value.status_code == 503
    assert caught.value.detail == not_ready_message(tool)


@pytest.mark.asyncio
async def test_makeps3iso_still_verifies_without_its_binary(tmp_path, monkeypatch):
    # Its check reads TITLE_ID back in Python.
    tool = registry.get("makeps3iso")
    monkeypatch.setattr(tool, "binary_path", _missing(tmp_path))

    assert await registry.tool_is_ready(tool) is False
    assert await registry.tool_is_ready(tool, purpose="verify") is True
