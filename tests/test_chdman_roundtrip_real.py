"""Real chdman Round-trip tests (#300).

Opt-in: runs the actual ``chdman`` through the app's own code on a real disc
image: convert to CHD, then the Round-trip check between the Source and the
CHD. Each test SKIPS (never fails) unless the binary and its sample are
present, so CI and normal ``pytest`` runs stay green.

Supply them with env vars:
  - ``CHDMAN`` (optional): path to the binary; defaults to the configured
    ``CHDMAN_PATH`` or ``chdman`` on ``PATH``.
  - ``CHDMAN_ROUNDTRIP_DVD_ISO``: a real DVD ``.iso`` (PS2/PSP), for createdvd.
  - ``CHDMAN_ROUNDTRIP_CD_CUE``: a real CD ``.cue`` with its track files, for
    createcd.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from services.round_trip import round_trip
from services.tools import registry

from config import settings


def _binary() -> str | None:
    for candidate in (os.environ.get("CHDMAN"), settings.chdman_path, "chdman"):
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return shutil.which(candidate) or candidate
    return None


def _sample(env: str) -> Path | None:
    value = os.environ.get(env)
    path = Path(value) if value else None
    return path if path and path.is_file() else None


async def _convert_and_round_trip(tmp_path, monkeypatch, env: str, mode: str):
    binary, sample = _binary(), _sample(env)
    if binary is None or sample is None:
        pytest.skip(f"set {env} (and CHDMAN) to run")
    tool = registry.get("chdman")
    monkeypatch.setattr(tool._service, "chdman_path", binary)
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path / "scratch"))

    chd = tmp_path / f"{sample.stem}.chd"
    async for _ in tool.convert(str(sample), str(chd), mode):
        pass
    assert chd.is_file()
    # The job only round-trips an output that passed its container check.
    assert (await tool.verify(str(chd)))["valid"]

    outcome = await round_trip(tool, str(sample), str(chd))
    assert outcome.passed, outcome.message
    scratch = tmp_path / "scratch"
    assert not scratch.exists() or os.listdir(scratch) == []


@pytest.mark.asyncio
async def test_real_dvd_iso_round_trips_through_createdvd(tmp_path, monkeypatch):
    await _convert_and_round_trip(
        tmp_path, monkeypatch, "CHDMAN_ROUNDTRIP_DVD_ISO", "createdvd",
    )


@pytest.mark.asyncio
async def test_real_cd_cue_round_trips_through_createcd(tmp_path, monkeypatch):
    await _convert_and_round_trip(
        tmp_path, monkeypatch, "CHDMAN_ROUNDTRIP_CD_CUE", "createcd",
    )
