"""Characterization tests for the tool plugin registry (design Phase 0).

These lock the *current* behavior (mode -> tool, capability flags, output
paths) into tests before later phases move dispatch logic onto the registry.
"""
from __future__ import annotations

import pytest

from app.models import ConversionMode
from app.routes import convert as convert_routes
from app.services.chdman import (
    CHDMAN_CONVERTIBLE_EXTENSIONS,
    chdman_service,
)
from app.services.dolphin_tool import (
    DOLPHIN_CONVERTIBLE_EXTENSIONS,
    dolphin_tool_service,
)
from app.services.tools import registry
from app.services.tools.registry import ToolRegistry
from app.services.tools.spec import ModeKind, ModeSpec
from app.services.nsz import (
    NSZ_COMPRESS_EXTENSIONS,
    NSZ_DECOMPRESS_EXTENSIONS,
    nsz_service,
)
from app.services.z3ds_compress import (
    Z3DS_CONVERTIBLE_EXTENSIONS,
    z3ds_compress_service,
)

# ConversionMode values that are NOT conversion modes, handled by the
# external job API, not by any of the three conversion services.
EXTERNAL_MODES = {ConversionMode.METADATA_SCAN, ConversionMode.DAT_MATCH}
CONVERSION_MODES = [m for m in ConversionMode if m not in EXTERNAL_MODES]


def _legacy_tool_for_mode(mode: str) -> str:
    """Replicates the dispatch ladder at job_manager.py:1444."""
    if mode.startswith("dolphin_"):
        return "dolphin"
    if mode == ConversionMode.Z3DS_COMPRESS.value:
        return "z3ds"
    if mode.startswith("nsz_"):
        return "nsz"
    return "chdman"


def test_every_conversion_mode_resolves_to_exactly_one_tool():
    resolved = {m.value: registry.for_mode(m.value).id for m in CONVERSION_MODES}
    assert len(resolved) == 18
    # Each registered mode is owned by exactly one tool (no duplicates).
    assert sorted(s.mode for s in registry.mode_specs()) == sorted(resolved)


def test_external_modes_are_not_registered():
    for mode in EXTERNAL_MODES:
        with pytest.raises(KeyError):
            registry.for_mode(mode.value)


@pytest.mark.parametrize("mode", [m.value for m in CONVERSION_MODES])
def test_mode_to_tool_matches_legacy_ladder(mode):
    assert registry.for_mode(mode).id == _legacy_tool_for_mode(mode)


@pytest.mark.parametrize("mode", [m.value for m in CONVERSION_MODES])
def test_supports_delete_on_verify_parity(mode):
    assert (
        registry.spec(mode).supports_delete_on_verify
        == convert_routes.supports_delete_on_verify(mode)
    )


def test_kind_classification():
    spec = registry.spec
    assert spec("createcd").kind is ModeKind.CREATE
    assert spec("extractcd").kind is ModeKind.EXTRACT
    assert spec("copy").kind is ModeKind.COPY
    assert spec("z3ds_compress").kind is ModeKind.COMPRESS
    # dolphin compress vs extract
    assert spec("dolphin_rvz").kind is ModeKind.COMPRESS
    assert spec("dolphin_iso").kind is ModeKind.EXTRACT
    # nsz compress vs decompress
    assert spec("nsz_compress").kind is ModeKind.COMPRESS
    assert spec("nsz_decompress").kind is ModeKind.EXTRACT


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        # chdman create/copy accept -c codec selection; extract does not.
        ("createcd", True),
        ("createdvd", True),
        ("copy", True),
        ("extractcd", False),
        # dolphin rvz/wia take a compression codec (+ level); gcz/iso don't.
        ("dolphin_rvz", True),
        ("dolphin_wia", True),
        ("dolphin_gcz", False),
        ("dolphin_iso", False),
        ("z3ds_compress", False),
    ],
)
def test_supports_compression_matches_current_behavior(mode, expected):
    assert registry.spec(mode).supports_compression is expected


def test_compression_level_only_for_dolphin_rvz_wia():
    # Dolphin RVZ/WIA and Switch compress expose a numeric level.
    for mode in ("dolphin_rvz", "dolphin_wia", "nsz_compress"):
        assert registry.spec(mode).supports_compression_level is True
    for mode in ("createcd", "copy", "dolphin_gcz", "dolphin_iso", "z3ds_compress",
                 "nsz_decompress"):
        assert registry.spec(mode).supports_compression_level is False


def test_convertible_extensions_match_service_constants():
    expected = (
        set(CHDMAN_CONVERTIBLE_EXTENSIONS)
        | set(DOLPHIN_CONVERTIBLE_EXTENSIONS)
        | set(Z3DS_CONVERTIBLE_EXTENSIONS)
        | set(NSZ_COMPRESS_EXTENSIONS)
        | set(NSZ_DECOMPRESS_EXTENSIONS)
    )
    assert set(registry.convertible_extensions()) == expected


def test_tools_for_input_representative():
    assert sorted(t.id for t in registry.tools_for_input("game.iso")) == [
        "chdman",
        "dolphin",
    ]
    assert [t.id for t in registry.tools_for_input("rom.3ds")] == ["z3ds"]
    assert [t.id for t in registry.tools_for_input("game.nsp")] == ["nsz"]
    assert [t.id for t in registry.tools_for_input("game.nsz")] == ["nsz"]
    assert [t.id for t in registry.tools_for_input("disc.gdi")] == ["chdman"]
    # A finished .chd is not a "convertible-from" source in the listing.
    assert registry.tools_for_input("out.chd") == []


def test_tool_for_verify_representative():
    assert registry.tool_for_verify("out.chd").id == "chdman"
    assert registry.tool_for_verify("rom.z3ds").id == "z3ds"
    assert registry.tool_for_verify("disc.rvz").id == "dolphin"
    assert registry.tool_for_verify("game.nsz").id == "nsz"
    assert registry.tool_for_verify("game.xcz").id == "nsz"
    assert registry.tool_for_verify("nope.txt") is None


@pytest.mark.parametrize("output_dir", [None, "/data/out"])
@pytest.mark.parametrize("treat_as_stem", [False, True])
@pytest.mark.parametrize(
    ("mode", "service", "inp"),
    [
        ("createcd", chdman_service, "/data/game.cue"),
        ("createdvd", chdman_service, "/data/game.iso"),
        ("extractcd", chdman_service, "/data/game.chd"),
        ("extractdvd", chdman_service, "/data/game.chd"),
        ("copy", chdman_service, "/data/game.chd"),
        ("dolphin_rvz", dolphin_tool_service, "/data/game.iso"),
        ("dolphin_iso", dolphin_tool_service, "/data/game.rvz"),
        ("z3ds_compress", z3ds_compress_service, "/data/rom.3ds"),
        ("nsz_compress", nsz_service, "/data/game.nsp"),
        ("nsz_compress", nsz_service, "/data/game.xci"),
        ("nsz_decompress", nsz_service, "/data/game.nsz"),
        ("nsz_decompress", nsz_service, "/data/game.xcz"),
    ],
)
def test_output_path_delegation_matches_service(
    mode, service, inp, output_dir, treat_as_stem,
):
    expected = service.get_output_path_for_mode(
        mode, inp, output_dir, treat_as_stem=treat_as_stem,
    )
    actual = registry.for_mode(mode).output_path(
        mode, inp, output_dir, treat_as_stem=treat_as_stem,
    )
    assert actual == expected


def test_duplicate_mode_registration_raises():
    class _StubTool:
        id = "stub"
        modes = (
            ModeSpec(
                mode="createcd",  # collides with chdman
                tool_id="stub",
                kind=ModeKind.CREATE,
                label="Stub",
                group="create",
                output_ext=".chd",
                input_extensions=frozenset({".iso"}),
            ),
        )

    fresh = ToolRegistry()
    fresh.register(registry.get("chdman"))
    with pytest.raises(ValueError, match="duplicate mode createcd"):
        fresh.register(_StubTool())


def test_duplicate_tool_id_registration_raises():
    fresh = ToolRegistry()
    fresh.register(registry.get("chdman"))
    with pytest.raises(ValueError, match="duplicate tool id chdman"):
        fresh.register(registry.get("chdman"))


def _spec(mode: str, tool_id: str = "stub") -> ModeSpec:
    return ModeSpec(
        mode=mode,
        tool_id=tool_id,
        kind=ModeKind.CREATE,
        label="Stub",
        group="create",
        output_ext=".x",
        input_extensions=frozenset({".y"}),
    )


def test_duplicate_mode_within_single_tool_raises():
    class _StubTool:
        id = "stub"
        modes = (_spec("foo"), _spec("foo"))

    with pytest.raises(ValueError, match="duplicate mode foo"):
        ToolRegistry().register(_StubTool())


def test_mode_spec_tool_id_must_match_owner():
    class _StubTool:
        id = "stub"
        modes = (_spec("foo", tool_id="other"),)

    with pytest.raises(ValueError, match="tool_id 'other'"):
        ToolRegistry().register(_StubTool())


@pytest.mark.parametrize("tool_id", ["chdman", "dolphin", "z3ds", "nsz"])
def test_output_extensions_cover_mode_output_exts(tool_id):
    tool = registry.get(tool_id)
    declared = {m.output_ext for m in tool.modes if m.output_ext is not None}
    assert declared <= tool.output_extensions
