"""Tests for the PS3 folder->iso source detector (``services.ps3``).

Phase 2 scaffolding: the directory analogue of an extension check. The key
behavior is that the disc/JB layout (a ``PS3_GAME/`` root) is required, and a
bare installed/HDD game-data folder (a ``PARAM.SFO`` under a TITLEID with no
``PS3_GAME``) is rejected — otherwise such folders would be advertised for
``folder_to_iso`` and fail late.
"""
from __future__ import annotations

import struct

from app.services.ps3 import is_ps3_iso_source, ps3_title_id
from app.services.tools import registry


def _make_sfo(pairs: list[tuple[str, str]]) -> bytes:
    keys_blob = b""
    key_offsets = []
    for key, _ in pairs:
        key_offsets.append(len(keys_blob))
        keys_blob += key.encode("ascii") + b"\x00"
    data_blob = b""
    data_meta = []
    for _, value in pairs:
        encoded = value.encode("utf-8") + b"\x00"
        data_meta.append((len(data_blob), len(encoded)))
        data_blob += encoded
    num = len(pairs)
    index_size = num * 16
    key_table_off = 20 + index_size
    data_table_off = key_table_off + len(keys_blob)
    header = struct.pack("<4sHH", b"\x00PSF", 1, 1)
    header += struct.pack("<III", key_table_off, data_table_off, num)
    index = b""
    for key_off, (data_off, data_len) in zip(key_offsets, data_meta):
        index += struct.pack("<HHIII", key_off, 0x0204, data_len, data_len, data_off)
    return header + index + keys_blob + data_blob


def test_disc_rip_folder_accepted(tmp_path):
    root = tmp_path / "MyGame"
    (root / "PS3_GAME").mkdir(parents=True)
    (root / "PS3_DISC.SFB").write_bytes(b"\x00")
    assert is_ps3_iso_source(str(root)) is True


def test_jb_game_folder_accepted(tmp_path):
    root = tmp_path / "MyGame"
    game = root / "PS3_GAME"
    game.mkdir(parents=True)
    (game / "PARAM.SFO").write_bytes(_make_sfo([("TITLE_ID", "BLES00000")]))
    assert is_ps3_iso_source(str(root)) is True


def test_installed_game_data_folder_rejected(tmp_path):
    # A TITLEID directory with only a PARAM.SFO (no PS3_GAME root) is an
    # installed/HDD layout makeps3iso can't package — must NOT be advertised.
    root = tmp_path / "BLES00000"
    root.mkdir()
    (root / "PARAM.SFO").write_bytes(_make_sfo([("TITLE_ID", "BLES00000")]))
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
        _make_sfo([("TITLE_ID", "BLES01807"), ("TITLE", "Some Game")])
    )
    assert ps3_title_id(str(root)) == "BLES01807"


def test_registry_directory_seam_present(tmp_path):
    # The seam exists; no directory tool is registered yet (makeps3iso is
    # deferred), so a valid PS3 folder resolves to no tool for now.
    root = tmp_path / "MyGame"
    (root / "PS3_GAME").mkdir(parents=True)
    (root / "PS3_DISC.SFB").write_bytes(b"\x00")
    assert registry.tools_for_directory(str(root)) == []
