"""Test doubles for reading a CHD's disc sectors through chdman (#362).

``make_chd_v5`` builds a stand-in CHD: a v5 header in MAME's layout followed
by the CHD's *logical image* stored as-is. It is not a real CHD (a real one is
compressed); it only has to be one that ``serve_extractraw`` can answer
``chdman extractraw -isb START -ib LENGTH`` for, the way the real binary
answers from a real CHD. Tests that fake chdman at
``SubprocessRunner.run_capture`` delegate ``extractraw`` commands here.
"""
from __future__ import annotations

import struct
from pathlib import Path

HEADER_BYTES = 124


def chd_v5_header(
    logical_bytes: int,
    unit_bytes: int,
    *,
    hunk_bytes: int | None = None,
    compressors: tuple[str, str, str, str] = ("lzma", "zlib", "huff", "flac"),
) -> bytes:
    """A 124-byte CHD v5 header in MAME's layout (big-endian)."""
    header = bytearray(HEADER_BYTES)
    header[0:8] = b"MComprHD"
    struct.pack_into(">II", header, 8, HEADER_BYTES, 5)
    for i, name in enumerate(compressors):
        header[16 + 4 * i:20 + 4 * i] = name.encode("ascii").ljust(4, b"\0")[:4]
    struct.pack_into(">QQQ", header, 32, logical_bytes, HEADER_BYTES, 0)
    struct.pack_into(">II", header, 56, hunk_bytes or unit_bytes, unit_bytes)
    return bytes(header)


def make_chd_v5(image: bytes, unit_bytes: int = 2048) -> bytes:
    """A stand-in CHD for *image* (its logical bytes), header first."""
    return chd_v5_header(len(image), unit_bytes) + image


def mode2_frames(cooked: bytes, unit_bytes: int = 2448, header: int = 24) -> bytes:
    """A cooked ISO as CD units: sync + header (+ subheader), 2048 bytes of
    data at *header*, the rest of the frame (and any subcode) zero."""
    out = bytearray()
    for start in range(0, len(cooked), 2048):
        unit = bytearray(unit_bytes)
        unit[0:12] = b"\x00" + b"\xff" * 10 + b"\x00"
        unit[15] = 2 if header == 24 else 1
        unit[header:header + 2048] = cooked[start:start + 2048]
        out += unit
    return bytes(out)


def serve_extractraw(cmd: list[str]) -> tuple[int, bytes, bytes] | None:
    """Answer ``chdman extractraw`` from a ``make_chd_v5`` file; None for any
    other command, so the caller's own fake handles it."""
    if "extractraw" not in cmd:
        return None
    chd = Path(cmd[cmd.index("-i") + 1])
    start = int(cmd[cmd.index("-isb") + 1])
    length = int(cmd[cmd.index("-ib") + 1])
    with open(chd, "rb") as f:
        f.seek(HEADER_BYTES + start)
        data = f.read(length)
    Path(cmd[cmd.index("-o") + 1]).write_bytes(data)
    return 0, b"", b""
