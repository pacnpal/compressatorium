"""Every Dolphin container that needs a block size gets a valid one (#349).

dolphin-tool refuses GCZ, RVZ and WIA without ``-b`` ("Block size must be set
for GCZ/RVZ/WIA", DolphinTool/ConvertCommand.cpp). Only RVZ used to get one, so
every WIA and GCZ job failed at the tool. The block size each mode writes, and
the rule a block size must meet, are ModeSpec data from Dolphin's own
DiscIO/DiscUtils.cpp ``IsDiscImageBlockSizeValid``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from services.tools import registry

from .dolphin_helpers import dolphin_option


@pytest.fixture(name="commands")
def _commands(monkeypatch):
    """The dolphin-tool command of each conversion, captured at its runner."""
    captured: list[list[str]] = []
    service = registry.get("dolphin")._service

    async def capture(cmd, *, input_path, output_path, **kwargs):
        captured.append(list(cmd))
        yield {"progress": 100, "message": "Conversion complete"}

    monkeypatch.setattr(service._runner, "run", capture)
    return captured


async def _convert(tmp_path: Path, mode: str, compression: str | None = None) -> None:
    out = tmp_path / f"Melee{registry.spec(mode).output_ext}"
    async for _ in registry.for_mode(mode).run_convert(
        str(tmp_path / "Melee.iso"), str(out), mode, compression=compression,
    ):
        pass


def test_every_containers_default_block_size_meets_its_rule() -> None:
    from services.tools.base import block_size_problem

    for mode in ("dolphin_rvz", "dolphin_wia", "dolphin_gcz"):
        spec = registry.spec(mode)
        assert spec.block_size, mode
        assert block_size_problem(spec, spec.block_size) is None, mode
    assert registry.spec("dolphin_iso").block_size is None


@pytest.mark.parametrize(("mode", "size", "valid"), [
    # GCZ: a power of two.
    ("dolphin_gcz", 0x8000, True),
    ("dolphin_gcz", 0x400000, True),
    ("dolphin_gcz", 0x18000, False),
    # WIA: at least 2 MiB, and a multiple of it.
    ("dolphin_wia", 0x200000, True),
    ("dolphin_wia", 0x600000, True),
    ("dolphin_wia", 0x20000, False),
    ("dolphin_wia", 0x300000, False),
    # RVZ: at least 32 KiB; a power of two below 2 MiB, a multiple of it above.
    ("dolphin_rvz", 0x8000, True),
    ("dolphin_rvz", 0x200000, True),
    ("dolphin_rvz", 0x600000, True),
    ("dolphin_rvz", 0x4000, False),
    ("dolphin_rvz", 0x30000, False),
    ("dolphin_rvz", 0x300000, False),
])
def test_the_block_size_rule_is_dolphins(mode, size, valid) -> None:
    from services.tools.base import block_size_problem

    problem = block_size_problem(registry.spec(mode), size)
    assert (problem is None) is valid, problem


def test_a_mode_without_a_block_size_refuses_one() -> None:
    from services.tools.base import block_size_problem

    assert block_size_problem(registry.spec("dolphin_iso"), 0x20000)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "compression", "block", "codec"), [
    ("dolphin_wia", None, "2097152", "none"),
    ("dolphin_wia", "lzma:5", "2097152", "lzma"),
    ("dolphin_gcz", None, "131072", None),
    ("dolphin_rvz", None, "131072", "zstd"),
])
async def test_each_container_gets_its_block_size(
    commands, tmp_path, mode, compression, block, codec,
) -> None:
    await _convert(tmp_path, mode, compression)

    (cmd,) = commands
    assert dolphin_option(cmd, "-b") == block
    assert dolphin_option(cmd, "-c") == codec


@pytest.mark.asyncio
async def test_an_iso_takes_no_block_size(commands, tmp_path) -> None:
    await _convert(tmp_path, "dolphin_iso")

    (cmd,) = commands
    assert dolphin_option(cmd, "-b") is None
