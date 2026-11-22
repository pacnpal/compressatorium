"""The methods every tool service shares live once, on RunnerService (#543)."""
from __future__ import annotations

import pytest

from services import (
    chdman, dolphin_tool, extract_xiso, jwudtool, makeps3iso, maxcso, nkit2iso, nsz,
    rar_extract, rom_converto, romz, z3ds_compress,
)
from services.subprocess_runner import RunnerService, VerifyingRunnerService

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
    if issubclass(cls, VerifyingRunnerService):
        # A streaming service defines its stream and inherits the drain.
        assert "verify_stream" in vars(cls) and "verify" not in vars(cls)


NOT_STREAMING = (makeps3iso.MakePs3IsoService, nkit2iso.Nkit2IsoService)


@pytest.mark.parametrize("cls", SERVICES, ids=lambda c: c.__name__)
def test_only_a_streaming_service_has_a_verify_stream(cls):
    """makeps3iso keeps its own verify (a PARAM.SFO readback) and nkit2iso
    has none: neither inherits an abstract stream it never defines."""
    streaming = cls not in NOT_STREAMING
    assert issubclass(cls, VerifyingRunnerService) == streaming
    assert hasattr(cls, "verify_stream") == streaming
    assert not hasattr(nkit2iso.Nkit2IsoService, "verify")
    assert "verify" in vars(makeps3iso.MakePs3IsoService)


@pytest.mark.asyncio
async def test_verify_drains_the_stream_with_the_services_fallback():
    class _Quiet(VerifyingRunnerService):
        verify_fallback_message = "CHD verification failed"

        async def verify_stream(self, path, *, cancel_event=None):
            yield {"type": "progress", "progress": 0, "message": path}

    assert (await _Quiet().verify("x"))["message"] == "CHD verification failed"
    assert chdman.ChdmanService.verify_fallback_message == "CHD verification failed"
    assert dolphin_tool.DolphinToolService.verify_fallback_message == "Disc verification failed"


@pytest.mark.asyncio
async def test_run_capture_feeds_a_file_from_an_offset_on_stdin(tmp_path):
    """The runner owns a child fed on stdin (z3ds's ``zstd -t``, #543)."""
    from services.subprocess_runner import SubprocessRunner

    src = tmp_path / "payload.bin"
    src.write_bytes(b"HDRthe payload")
    code, out, _err = await SubprocessRunner(owner="test").run_capture(
        ["cat"], stdin_path=str(src), stdin_offset=3,
    )
    assert (code, out) == (0, b"the payload")


def test_z3ds_spawns_nothing_outside_the_runner():
    import inspect

    assert "create_subprocess_exec" not in inspect.getsource(z3ds_compress)
