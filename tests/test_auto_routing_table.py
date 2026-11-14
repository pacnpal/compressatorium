"""The whole Auto routing table in one place.

For every Platform a tool declares, and every extension a mode serving it can
read, the mode Auto picks. ``test_the_table_covers_every_routable_pair`` fails
when a tool, mode, Platform or read set changes the set of pairs, so this file
has to be updated (and read) whenever routing changes.

Rows worth knowing about:

- ``ps2 .cue`` goes to ``createcd``: a PS2 CD-ROM title is a plain Mode 2 CD.
  ``ps2 .iso`` stays on ``createdvd``, the smallest reader that claims it.
- The cartridge Platforms all reach ``romz_7z`` for every ROM extension: romz
  is one format-agnostic mode, so a ``.gba`` in a ``gb`` folder is packed as it
  is.
- ``ngc``/``wii`` ``.rvz`` goes to ``dolphin_rvz`` (an RVZ re-encoded as RVZ).
  Without an output directory the job is skipped as "output exists".
- ``laseractive`` and ``ps3`` have no rows: ``createld`` is never an Auto
  default, and ``folder_to_iso`` takes a folder, not a file.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from services import platform_detect
from services.platform_detect import canonical_slug, default_mode
from services.tools import registry

from .test_xiso_service import build_image

CD = {".cue": "createcd", ".gdi": "createcd", ".iso": "createcd"}
CSO_TO_CHD = {".cso": "cso_to_chd", ".zso": "cso_to_chd", ".dax": "cso_to_chd"}
DOLPHIN = {
    ".iso": "dolphin_rvz", ".gcz": "dolphin_rvz", ".wia": "dolphin_rvz",
    ".rvz": "dolphin_rvz", ".wbfs": "dolphin_rvz",
    ".nkit.iso": "nkit_to_rvz", ".nkit.gcz": "nkit_to_rvz",
}
Z3DS = dict.fromkeys((".3ds", ".3dsx", ".cci", ".cia", ".cxi"), "z3ds_compress")
ROM = dict.fromkeys(
    (
        ".a26", ".a78", ".d64", ".gb", ".gba", ".gbc", ".gen", ".gg", ".lnx",
        ".md", ".n64", ".nds", ".nes", ".ngc", ".ngp", ".prg", ".sfc", ".smc",
        ".smd", ".sms", ".t64", ".v64", ".vb", ".ws", ".wsc", ".z64",
    ),
    "romz_7z",
)

ROUTES: dict[str, dict[str, str]] = {
    # Sony
    "ps": CD,
    "ps2": {".cue": "createcd", ".gdi": "createcd", ".iso": "createdvd", **CSO_TO_CHD},
    "psp": {".iso": "createdvd", **CSO_TO_CHD},
    # Sega, NEC, SNK, Philips, Panasonic, arcade: CD images
    "dc": CD, "saturn": CD, "segacd": CD, "3do": CD, "pc-fx": CD,
    "neo-geo-cd": CD, "turbografx-cd": CD, "philips-cd-i": CD, "arcade": CD,
    # Nintendo discs and cards
    "ngc": DOLPHIN, "wii": DOLPHIN,
    "wiiu": {".wud": "jwud_compress"},
    "switch": {".nsp": "nsz_compress", ".xci": "nsz_compress"},
    "3ds": Z3DS, "new-nintendo-3ds": Z3DS,
    # Microsoft
    "xbox": {".iso": "xiso_rewrite"}, "xbox360": {".iso": "xiso_rewrite"},
    # Cartridges and home computers
    **{
        slug: ROM
        for slug in (
            "gb", "gbc", "gba", "nds", "nes", "snes", "n64", "virtualboy",
            "sms", "genesis-slash-megadrive", "gamegear", "wonderswan",
            "neo-geo-pocket", "lynx", "c64", "atari2600", "atari7800",
        )
    },
}

ROWS = sorted(
    (platform, ext, mode)
    for platform, exts in ROUTES.items()
    for ext, mode in exts.items()
)


def _routable_pairs() -> set[tuple[str, str]]:
    """Every (Platform, extension) some Auto-eligible mode claims and reads."""
    pairs = set()
    for spec in registry.mode_specs():
        if not spec.auto_target or spec.kind.value not in ("create", "compress"):
            continue
        claimed = set(registry.mode_platform_slugs(spec.mode)) | set(
            spec.auto_platform_slugs,
        )
        for slug in claimed:
            for ext in registry.mode_reads_extensions(spec.mode):
                pairs.add((canonical_slug(slug), ext))
    return pairs


def test_the_table_covers_every_routable_pair():
    assert {(platform, ext) for platform, ext, _mode in ROWS} == _routable_pairs()


@pytest.fixture(autouse=True)
def _fresh_cache():
    platform_detect.clear_cache()
    yield


@pytest.mark.parametrize(("platform", "ext", "mode"), ROWS)
def test_auto_target(tmp_path: Path, platform, ext, mode):
    path = tmp_path / f"game{ext}"
    if platform.startswith("xbox"):
        # The Xbox tool only converts a file that really holds an Xbox volume.
        build_image(path, layout="XGD2")
    else:
        path.write_bytes(b"\0" * 64)

    assert default_mode(str(path), platform, registry) == mode
