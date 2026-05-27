"""Old-vs-new equivalence tests for Phase 2 output-path routing.

Phase 2 routes the two hardcoded output-path resolution sites
(``convert._get_output_path`` and ``job_manager._queue_job_locked``) through
``registry.for_mode(mode).output_path(...)``. These tests lock the
substitution as behavior-preserving for every path reachable via the HTTP API.
"""
from __future__ import annotations

import pytest

from app.services.chdman import chdman_service
from app.services.dolphin_tool import dolphin_tool_service
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


def test_archive_stem_keeps_full_name_as_stem():
    # Documented edge case: treat_as_stem=True keeps the member's full name
    # (no extension stripping) as the output stem.
    routed = registry.for_mode("createcd").output_path(
        "createcd", "disc.cue", None, treat_as_stem=True,
    )
    assert routed == "disc.cue.chd"
