"""Write tiny CSO v1/v2, ZSO and DAX files for the Round-trip tests (#301).

Laid out exactly as maxcso's README_CSO.md and ``src/input.cpp`` read them, so
the decoder under test meets the formats, not this module's assumptions about
them. LZ4 blocks are hand-built (literals plus one overlapping match), since
the image has no ``lz4`` package.
"""
from __future__ import annotations

import struct
import zlib

_HIGH = 0x80000000


def _raw_deflate(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def lz4_block(data: bytes) -> bytes:
    """A valid LZ4 block for ``data``: one overlapping match when the data
    opens with a repeated byte, the rest as literals."""
    out = bytearray()
    run = 1
    while run < len(data) and data[run] == data[0] and run < 200:
        run += 1
    if run >= 8 and len(data) > run + 5:
        # 1 literal, then a match of (run - 1) at offset 1 (overlapping copy).
        match = run - 1 - 4
        token = (1 << 4) | min(match, 15)
        out.append(token)
        out += data[:1]
        out += struct.pack("<H", 1)
        if match >= 15:
            rest = match - 15
            while rest >= 255:
                out.append(255)
                rest -= 255
            out.append(rest)
        literals = data[run:]
    else:
        literals = data
    n = len(literals)
    out.append(min(n, 15) << 4)
    if n >= 15:
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out += literals
    return bytes(out)


def write_ciso(path, image: bytes, *, magic=b"CISO", version=1, block=2048,
               shift=0, lz4=False, plain_blocks=()) -> None:
    """CSO v1 (``version`` 0/1), CSO v2 (``version`` 2) or ZSO (``magic`` ZISO)."""
    nblocks = (len(image) + block - 1) // block
    header_size = 24
    data_start = header_size + (nblocks + 1) * 4
    align = 1 << shift
    data_start = (data_start + align - 1) // align * align
    body = bytearray()
    index = []
    for i in range(nblocks):
        chunk = image[i * block:(i + 1) * block]
        if i in plain_blocks:
            stored, flag = chunk, (_HIGH if version != 2 else 0)
        elif magic == b"ZISO":
            stored, flag = lz4_block(chunk), 0
        elif version == 2 and lz4:
            stored, flag = lz4_block(chunk), _HIGH
        else:
            stored, flag = _raw_deflate(chunk), 0
        if version == 2 and len(stored) >= block:
            # v2 reads any block this long as uncompressed, so maxcso
            # stores an incompressible block as-is.
            stored, flag = chunk, 0
        pos = data_start + len(body)
        index.append(((pos >> shift) & 0x7FFFFFFF) | flag)
        body += stored
        while (data_start + len(body)) % align:
            body.append(0)
    index.append((data_start + len(body)) >> shift)
    head = struct.pack("<4sIQIBBH", magic, header_size, len(image), block, version, shift, 0)
    table = struct.pack(f"<{len(index)}I", *index)
    pad = b"\0" * (data_start - header_size - len(table))
    path.write_bytes(head + table + pad + bytes(body))


DAX_FRAME = 0x2000


def write_dax(path, image: bytes, *, nc_frames=()) -> None:
    nframes = (len(image) + DAX_FRAME - 1) // DAX_FRAME
    areas = [(f, 1) for f in nc_frames]
    head = struct.pack("<4sIII4I", b"DAX\0", len(image), 1, len(areas), 0, 0, 0, 0)
    table_size = nframes * 4 + nframes * 2 + len(areas) * 8
    pos = len(head) + table_size
    offsets, lengths, body = [], [], bytearray()
    for i in range(nframes):
        chunk = image[i * DAX_FRAME:(i + 1) * DAX_FRAME]
        stored = chunk if i in nc_frames else zlib.compress(chunk, 9)
        offsets.append(pos + len(body))
        lengths.append(len(stored))
        body += stored
    table = (
        struct.pack(f"<{nframes}I", *offsets)
        + struct.pack(f"<{nframes}H", *lengths)
        + b"".join(struct.pack("<II", s, c) for s, c in areas)
    )
    path.write_bytes(head + table + bytes(body))
