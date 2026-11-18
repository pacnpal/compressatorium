"""File info routes say the tool isn't ready instead of a 500 (#488).

Only ``/api/rom-converto-info`` asked whether its tool was ready. The others
ran their read regardless, so a missing binary came back as a 500 "Failed to
read ... info" and the File info modal said no tool could read the file.

Whether a route's read needs its tool is the plugin's fact
(``ToolPlugin.info_needs_tool``), asked through the one shared check
(``ToolRegistry.tool_is_ready(tool, purpose="info")``). A route whose read
runs the binary answers 503 with :func:`services.tools.not_ready_message`; a
route that reads the header in Python answers as usual and never waits on a
readiness probe.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from services.tools import not_ready_message, registry

from app.routes import info as info_routes

from .test_nkit2iso_routes import _nkit_bytes
from .test_xiso_service import build_image


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch):
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    return tmp_path


def _bytes(data: bytes):
    def write(path):
        path.write_bytes(data)
    return write


def _xbox(path):
    build_image(path, layout="XGD2")


# (route, tool id, file name, how to write a file the route accepts)
_NEEDS_TOOL = [
    ("get_chd_info", "chdman", "Game.chd", _bytes(b"MComprHD" + b"\0" * 120)),
    ("get_dolphin_info", "dolphin", "Game.rvz", _bytes(b"RVZ\x01" + b"\0" * 60)),
    ("get_rom_converto_info", "rom_converto", "Game.zar", _bytes(b"\0" * 64)),
]
_READS_WITHOUT_TOOL = [
    ("get_z3ds_info", "z3ds", "Game.3ds", _bytes(b"\0" * 512)),
    ("get_nkit_info", "nkit", "Melee.nkit.iso", _bytes(_nkit_bytes())),
    ("get_xiso_info", "xiso", "Halo 3.iso", _xbox),
    ("get_cso_info", "cso", "Game.iso", _bytes(b"\0" * 2048)),
    ("get_romz_info", "romz", "Game.gba", _bytes(b"\0" * 256)),
    ("get_jwud_info", "jwud", "Game.wud", _bytes(b"\0" * 256)),
    ("get_nsz_info", "nsz", "Game.nsp", _bytes(b"\0" * 256)),
]


def _not_ready(monkeypatch, tool) -> list[str]:
    """Make *tool* report not ready; return the list its probes land in."""
    probed: list[str] = []

    async def is_ready() -> bool:
        probed.append(tool.id)
        return False

    monkeypatch.setattr(tool, "is_ready", is_ready)
    return probed


def _refuse_reads(monkeypatch) -> None:
    """A gated route must not reach its binary at all."""
    async def refuse(*_args, **_kwargs):
        raise AssertionError("the route ran its tool although the tool isn't ready")

    monkeypatch.setattr(info_routes.chdman_service, "info", refuse)
    monkeypatch.setattr(info_routes.dolphin_tool_service, "header", refuse)
    monkeypatch.setattr(info_routes.rom_converto_service, "info", refuse)


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "tool_id", "name", "write"), _NEEDS_TOOL)
async def test_a_route_that_runs_its_tool_says_it_is_not_ready(
    volume, monkeypatch, route, tool_id, name, write,
):
    tool = registry.get(tool_id)
    probed = _not_ready(monkeypatch, tool)
    _refuse_reads(monkeypatch)
    src = volume / name
    write(src)

    with pytest.raises(HTTPException) as caught:
        await getattr(info_routes, route)(path=str(src))

    assert caught.value.status_code == 503
    assert caught.value.detail == not_ready_message(tool)
    assert caught.value.detail.startswith(f"{tool.display_name} isn't ready: ")
    assert probed == [tool_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "tool_id", "name", "write"), _READS_WITHOUT_TOOL)
async def test_a_route_that_reads_the_header_itself_still_answers(
    volume, monkeypatch, route, tool_id, name, write,
):
    tool = registry.get(tool_id)
    probed = _not_ready(monkeypatch, tool)
    src = volume / name
    write(src)

    result = await getattr(info_routes, route)(path=str(src))

    assert result.file == str(src)
    # Not even asked: nsz's probe walks every volume for prod.keys, and a
    # read that never runs the tool has no reason to wait on it.
    assert probed == []


def test_the_contract_says_which_info_reads_need_the_tool():
    assert {tool_id for _r, tool_id, _n, _w in _NEEDS_TOOL} == {
        tool_id for _r, tool_id, _n, _w in _NEEDS_TOOL + _READS_WITHOUT_TOOL
        if registry.get(tool_id).info_needs_tool
    }


@pytest.mark.asyncio
async def test_a_probe_that_never_answers_cannot_hang_the_info_route(volume, monkeypatch):
    tool = registry.get("rom_converto")

    async def hangs() -> bool:
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(tool, "is_ready", hangs)
    _refuse_reads(monkeypatch)
    src = volume / "Game.zar"
    src.write_bytes(b"\0" * 64)

    with (
        patch("services.tools.registry.READY_PROBE_SECONDS", 0.05),
        pytest.raises(HTTPException) as caught,
    ):
        await asyncio.wait_for(info_routes.get_rom_converto_info(path=str(src)), 5)

    assert caught.value.status_code == 503
    assert caught.value.detail == not_ready_message(tool)


@pytest.mark.asyncio
async def test_info_readiness_is_the_convert_probe_for_a_tool_that_needs_it(monkeypatch):
    tool = registry.get("dolphin")
    probed = _not_ready(monkeypatch, tool)

    assert await registry.tool_is_ready(tool, purpose="info") is False
    assert probed == ["dolphin"]
    assert not_ready_message(tool, purpose="info") == not_ready_message(tool)
