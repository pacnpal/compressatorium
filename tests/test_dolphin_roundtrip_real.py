"""Real Dolphin Round-trip test (#298).

Opt-in: runs the actual ``dolphin-tool`` through the app's own code on a real
GameCube/Wii ISO: convert to RVZ, then the Round-trip check between the ISO
and the RVZ. It SKIPS (never fails) unless both are present, so CI and normal
``pytest`` runs stay green.

Supply them with env vars:
  - ``DOLPHIN_TOOL`` (optional): path to the binary; defaults to the configured
    ``DOLPHIN_TOOL_PATH`` or ``dolphin-tool`` on ``PATH``.
  - ``DOLPHIN_ROUNDTRIP_ISO``: a real GameCube/Wii ``.iso``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from config import settings
from services.round_trip import round_trip
from services.tools import registry


def _binary() -> str | None:
    for candidate in (os.environ.get("DOLPHIN_TOOL"), settings.dolphin_tool_path,
                      "dolphin-tool"):
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return shutil.which(candidate) or candidate
    return None


def _sample() -> Path | None:
    env = os.environ.get("DOLPHIN_ROUNDTRIP_ISO")
    path = Path(env) if env else None
    return path if path and path.is_file() else None


@pytest.mark.asyncio
async def test_real_iso_to_rvz_round_trips(tmp_path, monkeypatch):
    binary, sample = _binary(), _sample()
    if binary is None or sample is None:
        pytest.skip("set DOLPHIN_ROUNDTRIP_ISO (and DOLPHIN_TOOL) to run")
    tool = registry.get("dolphin")
    monkeypatch.setattr(tool._service, "dolphin_tool_path", binary)

    rvz = tmp_path / f"{sample.stem}.rvz"
    async for _ in tool.convert(str(sample), str(rvz), "dolphin_rvz",
                                compression="zstd:5"):
        pass
    assert rvz.is_file()

    outcome = await round_trip(tool, str(sample), str(rvz))
    assert outcome.passed, outcome.message
