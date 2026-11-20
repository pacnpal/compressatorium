"""The methods every tool service shares live once, on RunnerService (#543)."""
from __future__ import annotations

import pytest

from services import (
    chdman, dolphin_tool, extract_xiso, jwudtool, makeps3iso, maxcso, nkit2iso, nsz,
    rar_extract, rom_converto, romz, z3ds_compress,
)
from services.subprocess_runner import RunnerService

SERVICES = [
    chdman.ChdmanService, dolphin_tool.DolphinToolService,
    extract_xiso.ExtractXisoService, jwudtool.JwudToolService,
    makeps3iso.MakePs3IsoService, maxcso.MaxcsoService, nkit2iso.Nkit2IsoService,
    nsz.NszService, rar_extract.RarExtractService, rom_converto.RomConvertoService,
    romz.RomzService, z3ds_compress.Z3DSCompressService,
]


@pytest.mark.parametrize("cls", SERVICES, ids=lambda c: c.__name__)
def test_each_service_shares_the_runner_methods(cls):
    assert issubclass(cls, RunnerService)
    for name in ("active_pids", "abandoned_pids"):
        assert name not in vars(cls), f"{cls.__name__} redefines {name}"
    if "verify_stream" in vars(cls):
        # Only makeps3iso has a verify of its own (a PARAM.SFO readback).
        assert ("verify" in vars(cls)) == (cls is makeps3iso.MakePs3IsoService)


@pytest.mark.asyncio
async def test_verify_drains_the_stream_with_the_services_fallback():
    class _Quiet(RunnerService):
        verify_fallback_message = "CHD verification failed"

        async def verify_stream(self, path, *, cancel_event=None):
            yield {"type": "progress", "progress": 0, "message": path}

    assert (await _Quiet().verify("x"))["message"] == "CHD verification failed"
    assert chdman.ChdmanService.verify_fallback_message == "CHD verification failed"
    assert dolphin_tool.DolphinToolService.verify_fallback_message == "Disc verification failed"
