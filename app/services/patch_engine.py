"""IPS and BPS patches on bytes (#428): source and patch in, target out.

Pure functions, no I/O, so the Patch action (``services.patch_bench``) runs
them on a detached thread under its bounds and the tests run them on
hand-built patches. The format is read from the patch's own header, never
from its file name.

* **IPS**: ``PATCH``, then records until ``EOF``. A record is a 3-byte offset
  and a 2-byte size, then that many bytes; a size of 0 is an RLE record (a
  2-byte run and the one byte it repeats). A record past the end grows the
  ROM, the gap filled with zeros. Three bytes after ``EOF`` are the truncation
  extension: the target's final length. IPS carries no checksums, so nothing
  proves the patch fits the ROM; the caller warns when the ROM isn't
  Recognized.
* **BPS**: ``BPS1``, the source, target and metadata sizes as varints, the
  metadata, then actions until the 12-byte footer: SourceRead (the source's
  bytes at the same offset), TargetRead (bytes from the patch), SourceCopy and
  TargetCopy (from a cursor each moves by a signed varint; TargetCopy may read
  what it just wrote, which is how a run is encoded). The footer is the
  source, target and patch CRC32s, little-endian. All three are checked: the
  patch's before anything else, the source's (and size) before applying, the
  target's (and size) after.

``max_target`` bounds the output, so neither a BPS header claiming a huge
target nor an IPS record far past the end can allocate more than the caller
allows.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

IPS_MAGIC = b"PATCH"
IPS_EOF = b"EOF"
BPS_MAGIC = b"BPS1"
_BPS_FOOTER = 12

SOURCE_READ, TARGET_READ, SOURCE_COPY, TARGET_COPY = range(4)


class PatchError(ValueError):
    """The patch could not be applied. The message says why, for a person."""


class PatchFormatError(PatchError):
    """The patch is damaged, truncated or not an IPS or BPS patch."""


class PatchTooLarge(PatchError):
    """The output would be larger than the caller's limit."""


class SourceMismatch(PatchError):
    """A BPS patch made for another ROM: its source size or CRC32 differs."""


class TargetMismatch(PatchError):
    """A BPS output that misses the size or CRC32 the patch promises."""


@dataclass(frozen=True)
class PatchResult:
    target: bytes
    # "ips" or "bps".
    kind: str
    # Whether the patch's own checksums proved the source and the output
    # (BPS). False for IPS, which has none.
    checked: bool


def patch_kind(patch: bytes) -> str:
    """``"ips"`` or ``"bps"``, from the patch's header."""
    if patch.startswith(BPS_MAGIC):
        return "bps"
    if patch.startswith(IPS_MAGIC):
        return "ips"
    raise PatchFormatError("This isn't an IPS or BPS patch (its header is neither)")


def apply_patch(source: bytes, patch: bytes, *, max_target: int) -> PatchResult:
    """Apply *patch* to *source*; the result's ``target`` is the patched ROM."""
    if patch_kind(patch) == "bps":
        return PatchResult(_apply_bps(source, patch, max_target), "bps", True)
    return PatchResult(_apply_ips(source, patch, max_target), "ips", False)


# ----------------------------------------------------------------------
# IPS
# ----------------------------------------------------------------------


def _grow(out: bytearray, end: int, max_target: int) -> None:
    if end > max_target:
        raise PatchTooLarge(
            f"The patched ROM would be {end} bytes, over the limit of {max_target}",
        )
    if end > len(out):
        out.extend(bytes(end - len(out)))


def _apply_ips(source: bytes, patch: bytes, max_target: int) -> bytes:
    if len(source) > max_target:
        raise PatchTooLarge(f"The ROM is over the limit of {max_target} bytes")
    out = bytearray(source)
    pos = len(IPS_MAGIC)
    end = len(patch)
    while True:
        if pos + 3 > end:
            raise PatchFormatError("The IPS patch ends without its EOF marker")
        head = patch[pos:pos + 3]
        rest = end - pos - 3
        # "EOF" is also the offset 0x454F46. Only the end marker can be
        # followed by nothing or by the 3-byte truncation; a record there
        # needs at least its size and the final marker after it.
        if head == IPS_EOF and rest in (0, 3):
            if rest == 3:
                length = int.from_bytes(patch[pos + 3:pos + 6], "big")
                del out[length:]
            return bytes(out)
        offset = int.from_bytes(head, "big")
        pos += 3
        if pos + 2 > end:
            raise PatchFormatError("The IPS patch is cut off inside a record")
        size = int.from_bytes(patch[pos:pos + 2], "big")
        pos += 2
        if size:
            data = patch[pos:pos + size]
            if len(data) != size:
                raise PatchFormatError("The IPS patch is cut off inside a record")
            pos += size
            _grow(out, offset + size, max_target)
            out[offset:offset + size] = data
            continue
        if pos + 3 > end:
            raise PatchFormatError("The IPS patch is cut off inside an RLE record")
        run = int.from_bytes(patch[pos:pos + 2], "big")
        value = patch[pos + 2]
        pos += 3
        if run == 0:
            raise PatchFormatError("The IPS patch has an RLE record of no bytes")
        _grow(out, offset + run, max_target)
        out[offset:offset + run] = bytes([value]) * run


# ----------------------------------------------------------------------
# BPS
# ----------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes, end: int) -> None:
        self.data, self.pos, self.end = data, 0, end

    def byte(self) -> int:
        if self.pos >= self.end:
            raise PatchFormatError("The BPS patch is cut off")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def take(self, count: int) -> bytes:
        if self.pos + count > self.end:
            raise PatchFormatError("The BPS patch is cut off")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def number(self) -> int:
        value, shift = 0, 1
        while True:
            x = self.byte()
            value += (x & 0x7F) * shift
            if x & 0x80:
                return value
            shift <<= 7
            value += shift
            if shift > 1 << 70:
                raise PatchFormatError("The BPS patch has a number too long to read")

    def signed(self) -> int:
        value = self.number()
        return -(value >> 1) if value & 1 else value >> 1


def _apply_bps(source: bytes, patch: bytes, max_target: int) -> bytes:
    if len(patch) < len(BPS_MAGIC) + 3 + _BPS_FOOTER:
        raise PatchFormatError("The BPS patch is too short to be one")
    footer = len(patch) - _BPS_FOOTER
    source_crc, target_crc, patch_crc = struct.unpack("<III", patch[footer:])
    if zlib.crc32(patch[:-4]) != patch_crc:
        raise PatchFormatError("The BPS patch is damaged (its own CRC32 doesn't match)")

    reader = _Reader(patch, footer)
    reader.pos = len(BPS_MAGIC)
    source_size = reader.number()
    target_size = reader.number()
    reader.take(reader.number())  # metadata, not needed to apply
    if target_size > max_target:
        raise PatchTooLarge(
            f"The patched ROM would be {target_size} bytes, over the limit of {max_target}",
        )
    if source_size != len(source) or zlib.crc32(source) != source_crc:
        raise SourceMismatch(
            "This ROM isn't the one this patch was made for (its size or CRC32 "
            "differs from what the patch expects)",
        )

    target = bytearray(target_size)
    written = 0
    source_cursor = target_cursor = 0
    while reader.pos < footer:
        data = reader.number()
        kind, length = data & 3, (data >> 2) + 1
        if written + length > target_size:
            raise PatchFormatError("The BPS patch writes past the end of its target")
        if kind == SOURCE_READ:
            if written + length > len(source):
                raise PatchFormatError("The BPS patch reads past the end of the ROM")
            target[written:written + length] = source[written:written + length]
        elif kind == TARGET_READ:
            target[written:written + length] = reader.take(length)
        elif kind == SOURCE_COPY:
            source_cursor += reader.signed()
            if source_cursor < 0 or source_cursor + length > len(source):
                raise PatchFormatError("The BPS patch copies from outside the ROM")
            target[written:written + length] = source[source_cursor:source_cursor + length]
            source_cursor += length
        else:
            target_cursor += reader.signed()
            if target_cursor < 0 or target_cursor >= written:
                raise PatchFormatError("The BPS patch copies from outside what it has written")
            _target_copy(target, target_cursor, written, length)
            target_cursor += length
        written += length

    if written != target_size:
        raise PatchFormatError("The BPS patch ends before its target is complete")
    if zlib.crc32(target) != target_crc:
        raise TargetMismatch(
            "The patched ROM doesn't match the CRC32 the patch promises, so it wasn't kept",
        )
    return bytes(target)


def _target_copy(target: bytearray, start: int, at: int, length: int) -> None:
    """Copy *length* bytes from *start* to *at* in *target*, reading bytes
    this copy itself writes when the two overlap (a repeating pattern)."""
    gap = at - start
    if gap >= length:
        target[at:at + length] = target[start:start + length]
        return
    pattern = bytes(target[start:at])
    repeats = -(-length // gap)
    target[at:at + length] = (pattern * repeats)[:length]
