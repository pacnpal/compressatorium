"""Decode maxcso's containers (CSO v1/v2, ZSO, DAX) back to the ISO they hold.

Used by the Round-trip check (#301) to hash what a container decodes to with no
temp file: ``maxcso --decompress`` cannot write to a pipe (it writes each block
at an explicit file offset, which a pipe refuses), so the image is decoded here
and hashed as it streams. The layouts follow maxcso's ``README_CSO.md`` and its
reader (``src/input.cpp``):

* **CSO v1** (``CISO``, version 0/1): 24-byte header (magic, header size,
  ``u64`` image size, ``u32`` block size, version, index shift), then
  ``ceil(size / block) + 1`` ``u32`` index entries. An entry's low 31 bits
  shifted left by the index shift are the block's offset; the next entry's
  offset ends it. The high bit marks a block stored uncompressed; the rest are
  raw deflate (window bits -15).
* **CSO v2** (``CISO``, version 2): the same, but a block whose stored length is
  at least the block size is uncompressed, and the high bit picks LZ4 over
  deflate for the others.
* **ZSO** (``ZISO``): the CSO v1 layout with LZ4 blocks instead of deflate.
* **DAX** (``DAX\\0``): 32-byte header (magic, ``u32`` image size, version,
  NC-area count), ``u32`` offsets and ``u16`` lengths per 8 KiB frame, then
  (version 1) the NC areas, frames stored uncompressed. The rest are zlib
  streams (window bits 15).

A stored block may carry alignment padding after its data, so each block is
cut to the bytes it should decode to, and LZ4 stops once the block is full
(maxcso uses ``LZ4_decompress_safe_partial`` for the same reason).

Any structural problem raises ``ValueError``. A decoder bug can only make a good
container hash differently from its Source, which keeps the Source: it cannot
produce the Source's bytes from a container that does not hold them.
"""
from __future__ import annotations

import os
import struct
import threading
import zlib
from array import array
from collections.abc import Iterator

_HIGH = 0x80000000
_LOW = 0x7FFFFFFF
DAX_FRAME_SIZE = 0x2000
# The header bounds maxcso's reader enforces (src/input.cpp, src/cso.h): a
# block of one 2 KiB sector up to 256 KiB, and a sector-aligned image size. The
# block size is also required to be a power of two, which maxcso assumes when
# it derives the block shift.
SECTOR_SIZE = 0x800
MAX_BLOCK_SIZE = 0x40000
_MAX_SHIFT = 31
# Bounds the index a header can make this read: a PSP UMD or PS2 DVD is under
# 9 GiB, so 64 GiB is generous. At 2 KiB blocks that is 32 Mi index entries,
# 128 MiB as an array('I'); the table must also fit in the file.
_MAX_IMAGE = 64 << 30
_SPAN = 4 << 20  # bytes of blocks per read, so 2 KiB blocks are not read one by one


def lz4_block_decompress(src: bytes, size: int) -> bytes:
    """Decode one raw LZ4 block, stopping once ``size`` bytes are out."""
    dst = bytearray()
    i, n = 0, len(src)
    while i < n and len(dst) < size:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                if i >= n:
                    raise ValueError("LZ4 literal length runs past the block")
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if i + lit > n:
            raise ValueError("LZ4 literals run past the block")
        dst += src[i:i + lit]
        i += lit
        if len(dst) >= size or i >= n:
            break
        if i + 2 > n:
            raise ValueError("LZ4 match offset runs past the block")
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(dst):
            raise ValueError("LZ4 match offset is out of range")
        length = token & 15
        if length == 15:
            while True:
                if i >= n:
                    raise ValueError("LZ4 match length runs past the block")
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4
        start = len(dst) - offset
        if length <= offset:
            dst += dst[start:start + length]
        else:  # overlapping: the last `offset` bytes repeat
            pattern = bytes(dst[start:])
            dst += (pattern * (length // offset + 1))[:length]
    if len(dst) < size:
        raise ValueError("LZ4 block decodes short")
    return bytes(dst[:size])


def _inflate(data: bytes, size: int, wbits: int) -> bytes:
    try:
        out = zlib.decompressobj(wbits).decompress(data, size)
    except zlib.error as e:
        raise ValueError(f"deflate block is corrupt: {e}") from e
    if len(out) < size:
        raise ValueError("deflate block decodes short")
    return out


def iter_decoded(path: str, stop: threading.Event | None = None) -> Iterator[bytes]:
    """Yield the image ``path`` (a CSO/ZSO/DAX) decodes to, in order."""
    with open(path, "rb") as fh:
        magic = fh.read(4)
        fh.seek(0)
        if magic in (b"CISO", b"ZISO"):
            yield from _iter_ciso(fh, stop)
        elif magic == b"DAX\x00":
            yield from _iter_dax(fh, stop)
        else:
            raise ValueError("not a CSO, ZSO or DAX container")


def _read_exact(fh, offset: int, length: int) -> bytes:
    fh.seek(offset)
    data = fh.read(length)
    if len(data) != length:
        raise ValueError("container is truncated")
    return data


def _iter_ciso(fh, stop) -> Iterator[bytes]:
    head = fh.read(24)
    if len(head) < 24:
        raise ValueError("container header is truncated")
    magic, _hsize, total, block, version, shift = struct.unpack("<4sIQIBB", head[:22])
    zso = magic == b"ZISO"
    if (
        version > 2
        or not SECTOR_SIZE <= block <= MAX_BLOCK_SIZE
        or block & (block - 1)
        or not 0 < total <= _MAX_IMAGE
        or total % SECTOR_SIZE
        or shift > _MAX_SHIFT
    ):
        raise ValueError("container header is not one maxcso reads")
    v2 = version == 2 and not zso
    nblocks = (total + block - 1) // block
    file_size = os.fstat(fh.fileno()).st_size
    if 24 + (nblocks + 1) * 4 > file_size:
        raise ValueError("container is truncated")
    index = array("I")
    index.frombytes(_read_exact(fh, 24, (nblocks + 1) * 4))
    if array("I").itemsize != 4:  # pragma: no cover - every supported platform
        raise ValueError("unsupported platform")
    if struct.pack("<I", 1) != struct.pack("=I", 1):  # pragma: no cover - big endian
        index.byteswap()
    batch = max(1, _SPAN // block)
    for first in range(0, nblocks, batch):
        if stop is not None and stop.is_set():
            return
        last = min(first + batch, nblocks)
        lo = (index[first] & _LOW) << shift
        hi = (index[last] & _LOW) << shift
        if hi < lo:
            raise ValueError("block index is not increasing")
        if hi > file_size:
            raise ValueError("container is truncated")
        span = _read_exact(fh, lo, hi - lo)
        out = bytearray()
        for i in range(first, last):
            start = ((index[i] & _LOW) << shift) - lo
            end = ((index[i + 1] & _LOW) << shift) - lo
            if end < start:
                raise ValueError("block index is not increasing")
            data = span[start:end]
            want = min(block, total - i * block)
            flag = index[i] & _HIGH
            if v2:
                if len(data) >= block:
                    out += _plain(data, want)
                elif flag:
                    out += lz4_block_decompress(data, want)
                else:
                    out += _inflate(data, want, -15)
            elif flag:
                out += _plain(data, want)
            elif zso:
                out += lz4_block_decompress(data, want)
            else:
                out += _inflate(data, want, -15)
        yield bytes(out)


def _plain(data: bytes, want: int) -> bytes:
    if len(data) < want:
        raise ValueError("uncompressed block is short")
    return data[:want]


def _iter_dax(fh, stop) -> Iterator[bytes]:
    head = fh.read(32)
    if len(head) < 32:
        raise ValueError("container header is truncated")
    _magic, total, version, nc_count = struct.unpack("<4sIII", head[:16])
    if version > 1 or not 0 < total <= _MAX_IMAGE:
        raise ValueError("container header is not valid")
    frames = (total + DAX_FRAME_SIZE - 1) // DAX_FRAME_SIZE
    nareas = nc_count if version >= 1 else 0
    if nareas > frames:
        raise ValueError("container header is not valid")
    table = _read_exact(fh, 32, frames * 6 + nareas * 8)
    offsets = struct.unpack_from(f"<{frames}I", table, 0)
    lengths = struct.unpack_from(f"<{frames}H", table, frames * 4)
    uncompressed = [False] * frames
    for a in range(nareas):
        start, count = struct.unpack_from("<II", table, frames * 6 + a * 8)
        for frame in range(start, min(start + count, frames)):
            uncompressed[frame] = True
    for i in range(frames):
        if stop is not None and stop.is_set():
            return
        want = min(DAX_FRAME_SIZE, total - i * DAX_FRAME_SIZE)
        data = _read_exact(fh, offsets[i], lengths[i])
        yield _plain(data, want) if uncompressed[i] else _inflate(data, want, 15)
