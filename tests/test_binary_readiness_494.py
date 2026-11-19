"""A tool whose binary is missing or won't run is not ready (#494).

Only nsz, jwud, rom-converto and exodos asked whether they could run. Every
other tool that spawns a program from a configured path (chdman, dolphin-tool,
z3ds_compressor, maxcso, 7z, makeps3iso, extract-xiso, nkit2iso) kept
``BaseTool.is_ready()``'s True, so ``GET /api/tools`` offered it with its
program gone and the File info gate (#488) never fired: an uncached CHD's info
ran a chdman that wasn't there and answered 500.

A plugin now declares the programs it runs (``required_binaries``, and
``verify_required_binaries`` where a check needs less or something else), and
``BaseTool`` probes them through one shared, detached, cached probe
(``services.tools.binaries``).
"""
from __future__ import annotations

import asyncio
import os
import stat
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from services.tools import not_ready_message, registry

from app.routes import info as info_routes

# Tool id -> the service attribute holding the path its program runs from.
_BINARY_TOOLS = {
    "chdman": "chdman_path",
    "dolphin": "dolphin_tool_path",
    "z3ds": "z3ds_compressor_path",
    "cso": "maxcso_path",
    "romz": "sevenzip_path",
    "makeps3iso": "makeps3iso_path",
    "xiso": "extract_xiso_path",
    "nkit": "nkit2iso_path",
    "jwud": "jwudtool_path",
}


def _executable(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _not_executable(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def _missing(path):
    return path


def _point(monkeypatch, tool_id: str, path) -> None:
    monkeypatch.setattr(
        registry.get(tool_id)._service, _BINARY_TOOLS[tool_id], str(path),
    )


@pytest.fixture(name="bins")
def _bins(tmp_path, monkeypatch):
    """Every binary tool pointed at a program that is there and runs."""
    folder = tmp_path / "bin"
    folder.mkdir()
    for tool_id in _BINARY_TOOLS:
        _point(monkeypatch, tool_id, _executable(folder / tool_id))
    return folder


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch):
    root = tmp_path / "vol"
    root.mkdir()
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(root))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(root))
    return root


_BROKEN = [("missing", _missing), ("not executable", _not_executable)]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", sorted(_BINARY_TOOLS))
@pytest.mark.parametrize(("state", "make"), _BROKEN, ids=[s for s, _m in _BROKEN])
async def test_the_tool_list_drops_a_tool_whose_binary_cannot_run(
    bins, monkeypatch, tool_id, state, make,
):
    bad = make(bins / f"{tool_id}-broken")
    _point(monkeypatch, tool_id, bad)

    tools = await info_routes.list_tools()

    assert tool_id in tools["unavailable"]
    assert tool_id not in tools["available"]
    tool = registry.get(tool_id)
    message = not_ready_message(tool)
    assert str(bad) in message
    assert ("not executable" in message) == (state == "not executable")


@pytest.mark.asyncio
async def test_the_tool_list_keeps_every_tool_whose_binary_is_there(bins):
    tools = await info_routes.list_tools()

    assert set(_BINARY_TOOLS) <= set(tools["available"])


@pytest.mark.asyncio
async def test_a_directory_where_the_binary_should_be_is_not_ready(bins, monkeypatch):
    folder = bins / "chdman-dir"
    folder.mkdir()
    _point(monkeypatch, "chdman", folder)

    assert await registry.tool_is_ready(registry.get("chdman")) is False


@pytest.mark.asyncio
async def test_a_bare_name_is_looked_up_on_path(bins, tmp_path, monkeypatch):
    on_path = tmp_path / "path"
    on_path.mkdir()
    monkeypatch.setenv("PATH", str(on_path))
    _point(monkeypatch, "romz", "fake7z-494")
    tool = registry.get("romz")

    assert await registry.tool_is_ready(tool) is False
    assert "fake7z-494" in not_ready_message(tool)

    _executable(on_path / "fake7z-494")
    _forget()
    assert await registry.tool_is_ready(tool) is True


# --- File info: 503 with the plugin's reason, not a 500 from the failed run ---

_INFO = [
    ("get_chd_info", "chdman", "Game.chd", b"MComprHD" + b"\0" * 120),
    ("get_dolphin_info", "dolphin", "Game.rvz", b"RVZ\x01" + b"\0" * 60),
]


def _refuse_reads(monkeypatch) -> None:
    async def refuse(*_args, **_kwargs):
        raise AssertionError("the route ran its tool although the tool isn't ready")

    monkeypatch.setattr(info_routes.chdman_service, "info", refuse)
    monkeypatch.setattr(info_routes.dolphin_tool_service, "header", refuse)


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "tool_id", "name", "data"), _INFO, ids=["chd", "dolphin"])
@pytest.mark.parametrize(("state", "make"), _BROKEN, ids=[s for s, _m in _BROKEN])
async def test_file_info_answers_503_when_the_binary_cannot_run(
    bins, volume, monkeypatch, route, tool_id, name, data, state, make,
):
    bad = make(bins / f"{tool_id}-broken")
    _point(monkeypatch, tool_id, bad)
    _refuse_reads(monkeypatch)
    src = volume / name
    src.write_bytes(data)

    with pytest.raises(HTTPException) as caught:
        await getattr(info_routes, route)(path=str(src))

    assert caught.value.status_code == 503
    tool = registry.get(tool_id)
    assert caught.value.detail == not_ready_message(tool, purpose="info")
    assert str(bad) in caught.value.detail
    assert ("not executable" in caught.value.detail) == (state == "not executable")


@pytest.mark.asyncio
async def test_chd_info_is_ready_when_chdman_is_there(bins):
    assert await registry.tool_is_ready(registry.get("chdman"), purpose="info") is True


@pytest.mark.asyncio
async def test_dolphin_info_runs_dolphin_tool_when_it_is_there(bins, volume, monkeypatch):
    src = volume / "Game.rvz"
    src.write_bytes(b"RVZ\x01" + b"\0" * 60)
    read = AsyncMock(return_value={"game_id": "GALE01", "raw_data": ""})
    monkeypatch.setattr(info_routes.dolphin_tool_service, "header", read)

    result = await info_routes.get_dolphin_info(path=str(src))

    assert result.game_id == "GALE01"
    read.assert_awaited()


# --- A check needs what it runs, which is not always the converter ----------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", ["chdman", "dolphin", "cso", "romz", "xiso"])
async def test_a_check_that_runs_the_binary_is_not_ready_without_it(
    bins, monkeypatch, tool_id,
):
    _point(monkeypatch, tool_id, bins / "nothing-here")

    assert await registry.tool_is_ready(registry.get(tool_id), purpose="verify") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", ["makeps3iso", "jwud"])
async def test_a_check_done_in_python_stays_ready_without_the_binary(
    bins, monkeypatch, tool_id,
):
    _point(monkeypatch, tool_id, bins / "nothing-here")
    tool = registry.get(tool_id)

    assert await registry.tool_is_ready(tool) is False
    assert await registry.tool_is_ready(tool, purpose="verify") is True


@pytest.mark.asyncio
async def test_the_3ds_check_needs_zstd_not_the_compressor(bins, tmp_path, monkeypatch):
    on_path = tmp_path / "path"
    on_path.mkdir()
    monkeypatch.setenv("PATH", str(on_path))
    _point(monkeypatch, "z3ds", bins / "nothing-here")
    tool = registry.get("z3ds")

    assert await registry.tool_is_ready(tool) is False
    assert await registry.tool_is_ready(tool, purpose="verify") is False
    assert "zstd" in not_ready_message(tool, purpose="verify")

    _executable(on_path / "zstd")
    _forget()
    assert await registry.tool_is_ready(tool, purpose="verify") is True
    # The compressor is still missing: converting stays unavailable.
    assert await registry.tool_is_ready(tool) is False
    assert "z3ds_compressor was not found" in not_ready_message(tool)


# --- The shared probe: detached, cached per path, one in flight -------------

def _forget() -> None:
    from services.tools import binaries

    binaries.forget_binary_probes()


@pytest.mark.asyncio
async def test_the_probe_runs_detached(bins, monkeypatch):
    from services.tools import binaries

    detached: list[str] = []
    real = binaries.run_detached

    async def spy(func, *args, **kwargs):
        detached.append(args[0] if args else "")
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(binaries, "run_detached", spy)
    _point(monkeypatch, "chdman", _executable(bins / "chdman-detached"))

    assert await registry.tool_is_ready(registry.get("chdman")) is True
    assert str(bins / "chdman-detached") in detached


@pytest.mark.asyncio
async def test_an_answer_is_reused_until_it_expires(bins, monkeypatch):
    from services.tools import binaries

    clock = [1000.0]
    monkeypatch.setattr(binaries, "_now", lambda: clock[0])
    calls: list[str] = []
    real = binaries._inspect

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(binaries, "_inspect", counting)
    program = _executable(bins / "chdman-cached")
    _point(monkeypatch, "chdman", program)
    tool = registry.get("chdman")

    assert await registry.tool_is_ready(tool) is True
    os.unlink(program)
    # Within the window: the tool list does not stat it again.
    assert await registry.tool_is_ready(tool) is True
    assert calls == [str(program)]

    clock[0] += binaries.BINARY_PROBE_TTL_SECONDS + 1
    # Past it: a binary that went away is noticed.
    assert await registry.tool_is_ready(tool) is False
    assert calls == [str(program), str(program)]

    _executable(program)
    clock[0] += binaries.BINARY_PROBE_TTL_SECONDS + 1
    # And one that comes back.
    assert await registry.tool_is_ready(tool) is True


@pytest.mark.asyncio
async def test_concurrent_probes_of_one_path_share_one_stat(bins, monkeypatch):
    from services.tools import binaries

    _forget()
    calls: list[str] = []
    real = binaries._inspect

    def slow(path):
        calls.append(path)
        import time
        time.sleep(0.1)
        return real(path)

    monkeypatch.setattr(binaries, "_inspect", slow)
    program = _executable(bins / "chdman-shared")
    _point(monkeypatch, "chdman", program)
    tool = registry.get("chdman")

    answers = await asyncio.gather(*(registry.tool_is_ready(tool) for _ in range(5)))

    assert answers == [True] * 5
    assert calls == [str(program)]
