"""Shared PS3 folder / PARAM.SFO fixture builders for the PS3 test suite.

Centralised here so ``test_ps3_detector`` and ``test_makeps3iso`` don't drift
apart or duplicate the SFO-table encoding (mirroring the DB suite's conftest
sharing). The makeps3iso folder->iso seam keys off the disc/JB layout, so the
builders produce exactly that.
"""
from __future__ import annotations

import struct
from pathlib import Path


def make_sfo(pairs: list[tuple[str, str]]) -> bytes:
    """Encode ``(key, value)`` string pairs as a minimal PSF/PARAM.SFO blob."""
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
    for key_off, (data_off, data_len) in zip(key_offsets, data_meta, strict=True):
        index += struct.pack("<HHIII", key_off, 0x0204, data_len, data_len, data_off)
    return header + index + keys_blob + data_blob


def make_ps3_folder(root: Path) -> Path:
    """Create a minimal valid PS3 disc/JB folder (PS3_GAME/ root + PS3_DISC.SFB)."""
    game = root / "PS3_GAME"
    game.mkdir(parents=True)
    (game / "PARAM.SFO").write_bytes(
        make_sfo([("TITLE_ID", "BLES01807"), ("TITLE", "Some Game")])
    )
    (root / "PS3_DISC.SFB").write_bytes(b"\x00")
    return root
