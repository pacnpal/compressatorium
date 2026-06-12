"""Tests for the PS3 folder->iso source detector (``services.ps3``).

Phase 2 scaffolding: the directory analogue of an extension check. The key
behavior is that the disc/JB layout (a ``PS3_GAME/`` root) is required, and a
bare installed/HDD game-data folder (a ``PARAM.SFO`` under a TITLEID with no
``PS3_GAME``) is rejected — otherwise such folders would be advertised for
``folder_to_iso`` and fail late.
"""
from __future__ import annotations

from app.services.ps3 import is_ps3_iso_source, ps3_title_id
from app.services.tools import registry

from .ps3_helpers import make_sfo


def test_disc_rip_folder_accepted(tmp_path):
    root = tmp_path / "MyGame"
    (root / "PS3_GAME").mkdir(parents=True)
    (root / "PS3_DISC.SFB").write_bytes(b"\x00")
    assert is_ps3_iso_source(str(root)) is True


def test_jb_game_folder_accepted(tmp_path):
    root = tmp_path / "MyGame"
    game = root / "PS3_GAME"
    game.mkdir(parents=True)
    (game / "PARAM.SFO").write_bytes(make_sfo([("TITLE_ID", "BLES00000")]))
    assert is_ps3_iso_source(str(root)) is True


def test_installed_game_data_folder_rejected(tmp_path):
    # A TITLEID directory with only a PARAM.SFO (no PS3_GAME root) is an
    # installed/HDD layout makeps3iso can't package — must NOT be advertised.
    root = tmp_path / "BLES00000"
    root.mkdir()
    (root / "PARAM.SFO").write_bytes(make_sfo([("TITLE_ID", "BLES00000")]))
    assert is_ps3_iso_source(str(root)) is False


def test_plain_folder_and_file_rejected(tmp_path):
    plain = tmp_path / "random"
    plain.mkdir()
    assert is_ps3_iso_source(str(plain)) is False

    a_file = tmp_path / "game.iso"
    a_file.write_bytes(b"x")
    assert is_ps3_iso_source(str(a_file)) is False


def test_title_id_readback(tmp_path):
    root = tmp_path / "MyGame"
    game = root / "PS3_GAME"
    game.mkdir(parents=True)
    (game / "PARAM.SFO").write_bytes(
        make_sfo([("TITLE_ID", "BLES01807"), ("TITLE", "Some Game")])
    )
    assert ps3_title_id(str(root)) == "BLES01807"


def test_registry_directory_resolves_makeps3iso(tmp_path):
    # A valid PS3 disc/JB folder resolves to the makeps3iso tool via the
    # directory seam; an arbitrary folder resolves to no tool.
    root = tmp_path / "MyGame"
    (root / "PS3_GAME").mkdir(parents=True)
    (root / "PS3_DISC.SFB").write_bytes(b"\x00")
    tools = registry.tools_for_directory(str(root))
    assert [t.id for t in tools] == ["makeps3iso"]

    plain = tmp_path / "random"
    plain.mkdir()
    assert registry.tools_for_directory(str(plain)) == []
