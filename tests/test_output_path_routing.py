"""Old-vs-new equivalence tests for Phase 2 output-path routing.

Phase 2 routes the two hardcoded output-path resolution sites
(``convert._get_output_path`` and ``job_manager._queue_job_locked``) through
``registry.for_mode(mode).output_path(...)``. These tests lock the
substitution as behavior-preserving for every path reachable via the HTTP API.
"""
from __future__ import annotations

import pytest

from app.models import ConversionMode
from app.services.chdman import chdman_service
from app.services.dolphin_tool import dolphin_tool_service
from app.services.job_manager import JobManager
from app.services.tools import registry
from app.services.z3ds_compress import z3ds_compress_service

# mode -> (legacy service used by the old _get_output_path ladder, sample input).
# Inputs are chosen so extract modes carry a .chd source (so the .chd-stripping
# branch fires) and dolphin/z3ds modes carry a real convertible extension.
ROUTE_MATRIX = [
    ("createcd", chdman_service, "/data/game.cue"),
    ("createdvd", chdman_service, "/data/game.iso"),
    ("createhd", chdman_service, "/data/game.img"),
    ("createraw", chdman_service, "/data/game.img"),
    ("createld", chdman_service, "/data/game.raw"),
    ("extractcd", chdman_service, "/data/game.chd"),
    ("extractdvd", chdman_service, "/data/game.chd"),
    ("extractraw", chdman_service, "/data/game.chd"),
    ("extracthd", chdman_service, "/data/game.chd"),
    ("extractld", chdman_service, "/data/game.chd"),
    ("copy", chdman_service, "/data/game.chd"),
    ("dolphin_rvz", dolphin_tool_service, "/data/game.iso"),
    ("dolphin_iso", dolphin_tool_service, "/data/game.rvz"),
    ("dolphin_wia", dolphin_tool_service, "/data/game.iso"),
    ("dolphin_gcz", dolphin_tool_service, "/data/game.iso"),
    ("z3ds_compress", z3ds_compress_service, "/data/rom.3ds"),
    ("z3ds_decompress", z3ds_compress_service, "/data/rom.zcci"),
]


@pytest.mark.parametrize("output_dir", [None, "/data/out"])
@pytest.mark.parametrize("treat_as_stem", [False, True])
@pytest.mark.parametrize(("mode", "service", "inp"), ROUTE_MATRIX)
def test_route_output_path_matches_legacy_ladder(
    mode, service, inp, output_dir, treat_as_stem,
):
    # Old convert._get_output_path ladder dispatched to the per-tool
    # service.get_output_path_for_mode(...); the registry must match it.
    legacy = service.get_output_path_for_mode(
        mode, inp, output_dir, treat_as_stem=treat_as_stem,
    )
    routed = registry.for_mode(mode).output_path(
        mode, inp, output_dir, treat_as_stem=treat_as_stem,
    )
    assert routed == legacy


@pytest.mark.parametrize("output_dir", [None, "/data/out"])
def test_job_manager_chdman_fallback_equivalence(output_dir):
    # Legacy _queue_job_locked fallback for non-z3ds modes called
    # chdman_service.get_chd_path(...). For create/copy modes the registry's
    # output_path must produce the same .chd path.
    inp = "/data/game.cue"
    legacy = chdman_service.get_chd_path(inp, output_dir)
    routed = registry.for_mode("createcd").output_path("createcd", inp, output_dir)
    assert routed == legacy


@pytest.mark.parametrize("output_dir", [None, "/data/out"])
def test_job_manager_z3ds_fallback_equivalence(output_dir):
    # Legacy _queue_job_locked fallback for z3ds_compress called
    # z3ds_compress_service.get_output_path(...); the registry must match it.
    inp = "/data/rom.cci"
    legacy = z3ds_compress_service.get_output_path(inp, output_dir)
    routed = registry.for_mode("z3ds_compress").output_path(
        "z3ds_compress", inp, output_dir,
    )
    assert routed == legacy


@pytest.mark.parametrize("output_dir", [None, "/data/out"])
def test_extractcd_emits_cue_not_bin(output_dir):
    # Documented edge case: extractcd resolves to a .cue sidecar path.
    routed = registry.for_mode("extractcd").output_path(
        "extractcd", "/data/game.chd", output_dir,
    )
    assert routed.endswith("game.cue")


def test_archive_stem_strips_extension():
    # treat_as_stem=True inputs are synthetic flattened archive-member
    # filenames; the tool strips the extension like a real source, so a
    # ".cue" member yields "disc.chd" (not "disc.cue.chd").
    routed = registry.for_mode("createcd").output_path(
        "createcd", "disc.cue", None, treat_as_stem=True,
    )
    assert routed == "disc.chd"


def test_archive_z3ds_member_maps_output_extension():
    # z3ds output extension is derived from the input; archive members keep
    # their original extension through treat_as_stem so the mapping holds
    # (.3ds -> .z3ds, .cci -> .zcci), regression guard for issue #113.
    assert registry.for_mode("z3ds_compress").output_path(
        "z3ds_compress", "games_rom.3ds", None, treat_as_stem=True,
    ) == "games_rom.z3ds"
    assert registry.for_mode("z3ds_compress").output_path(
        "z3ds_compress", "games_rom.cci", "/out", treat_as_stem=True,
    ) == "/out/games_rom.zcci"


def test_archive_z3ds_decompress_member_reverses_output_extension():
    # Decompress reverses the compress map (.z3ds -> .3ds, .zcci -> .cci),
    # both for on-disk and archive-member (treat_as_stem) inputs.
    assert registry.for_mode("z3ds_decompress").output_path(
        "z3ds_decompress", "games_rom.z3ds", None, treat_as_stem=True,
    ) == "games_rom.3ds"
    assert registry.for_mode("z3ds_decompress").output_path(
        "z3ds_decompress", "games_rom.zcxi", "/out", treat_as_stem=True,
    ) == "/out/games_rom.cxi"


# Guards on the _queue_job_locked output_path fallback for direct service
# callers (the HTTP routes always pass an explicit output_path). These
# preserve rejections the legacy per-tool fallback enforced.


@pytest.mark.asyncio
async def test_z3ds_fallback_rejects_unsupported_extension():
    manager = JobManager()
    with pytest.raises(ValueError, match="Unsupported file extension"):
        await manager.create_job("/data/not-a-rom.txt", ConversionMode.Z3DS_COMPRESS)


@pytest.mark.asyncio
async def test_z3ds_fallback_accepts_supported_extension():
    manager = JobManager()
    job = await manager.create_job("/data/rom.cci", ConversionMode.Z3DS_COMPRESS)
    assert job.output_path == "/data/rom.zcci"


@pytest.mark.asyncio
async def test_z3ds_decompress_fallback_accepts_supported_extension():
    manager = JobManager()
    job = await manager.create_job("/data/rom.zcci", ConversionMode.Z3DS_DECOMPRESS)
    assert job.output_path == "/data/rom.cci"


@pytest.mark.asyncio
async def test_z3ds_decompress_fallback_rejects_unsupported_extension():
    manager = JobManager()
    with pytest.raises(ValueError, match="Unsupported file extension"):
        await manager.create_job("/data/rom.cci", ConversionMode.Z3DS_DECOMPRESS)


@pytest.mark.asyncio
async def test_dolphin_fallback_rejects_output_colliding_with_source():
    # dolphin_iso on a .iso source resolves to the same path; refuse it
    # rather than let dolphin-tool overwrite the source.
    manager = JobManager()
    with pytest.raises(ValueError, match="Output path matches input"):
        await manager.create_job("/data/game.iso", ConversionMode.DOLPHIN_ISO)


@pytest.mark.asyncio
async def test_dolphin_fallback_allows_distinct_output():
    # dolphin_rvz on a .iso source yields a distinct .rvz path; allowed.
    manager = JobManager()
    job = await manager.create_job("/data/game.iso", ConversionMode.DOLPHIN_RVZ)
    assert job.output_path == "/data/game.rvz"
