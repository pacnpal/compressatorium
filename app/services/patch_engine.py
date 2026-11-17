"""IPS and BPS patching on bytes (#428): source bytes + patch bytes -> target bytes.

Pure functions, no I/O, so the patch bench (``services/patch_bench.py``) runs
them on a detached thread and the tests run them on hand-built patches.

* **IPS**: ``PATCH``, then records (3-byte offset, 2-byte size, data; a size
  of 0 is an RLE record: 2-byte count, 1 value byte) up to ``EOF``, then an
  optional 3-byte size the target is truncated to. IPS carries no checksum,
  so nothing here can tell whether it fits the source.
* **BPS** (byuu's format): ``BPS1``, the source, target and metadata sizes as
  varints, the metadata, the actions (SourceRead, TargetRead, SourceCopy,
  TargetCopy), then the source, target and patch CRC32s. The patch CRC is
  checked first, then the source size and CRC (a patch made for another
  ROM is refused), and the target is checked against its size and CRC.
"""

from __future__ import annotations

import zlib

IPS_MAGIC = b"PATCH"
BPS_MAGIC = b"BPS1"
_IPS_EOF = b"EOF"


class PatchError(ValueError):
    """The patch is damaged, of an unknown kind, or doesn't fit the source."""


# A cartridge ROM is never this big; the same ceiling the bench reads Images under.
MAX_TARGET_BYTES = 1024 * 1024 * 1024
# No size, offset or length in a real patch comes near this.
_MAX_VARINT = 1 << 64


def patch_format(patch: bytes) -> str | None:
    """``"ips"`` or ``"bps"`` by the patch's own magic, else None."""
    if patch.startswith(BPS_MAGIC):
        return "bps"
    if patch.startswith(IPS_MAGIC):
        return "ips"
    return None


def apply_patch(source: bytes, patch: bytes) -> bytes:
    """The target *patch* makes from *source*; raises :class:`PatchError`."""
    kind = patch_format(patch)
    if kind == "bps":
        return apply_bps(source, patch)
    if kind == "ips":
        return apply_ips(source, patch)
    raise PatchError("Not an IPS or BPS patch")


def apply_ips(source: bytes, patch: bytes) -> bytes:
    if not patch.startswith(IPS_MAGIC):
        raise PatchError("Not an IPS patch")
    target = bytearray(source)
    pos = len(IPS_MAGIC)

    def take(n: int) -> bytes:
        nonlocal pos
        chunk = patch[pos:pos + n]
        if len(chunk) != n:
            raise PatchError("The IPS patch ends part way through a record")
        pos += n
        return chunk

    while True:
        head = take(3)
        if head == _IPS_EOF:
            break
        offset = int.from_bytes(head, "big")
        size = int.from_bytes(take(2), "big")
        if size:
            data = take(size)
        else:
            count = int.from_bytes(take(2), "big")
            data = take(1) * count
        end = offset + len(data)
        if end > len(target):
            target.extend(bytes(end - len(target)))
        target[offset:end] = data
    rest = patch[pos:]
    if len(rest) == 3:
        del target[int.from_bytes(rest, "big"):]
    elif rest:
        raise PatchError("The IPS patch has unexpected bytes after EOF")
    return bytes(target)


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def apply_bps(source: bytes, patch: bytes) -> bytes:
    if not patch.startswith(BPS_MAGIC) or len(patch) < len(BPS_MAGIC) + 3 + 12:
        raise PatchError("Not a BPS patch")
    footer = len(patch) - 12
    want_source, want_target, want_patch = (
        int.from_bytes(patch[footer + i:footer + i + 4], "little") for i in (0, 4, 8)
    )
    if _crc(patch[:-4]) != want_patch:
        raise PatchError("The BPS patch is damaged (its own CRC32 doesn't match)")
    pos = len(BPS_MAGIC)

    def varint() -> int:
        nonlocal pos
        value, shift = 0, 1
        while True:
            if pos >= footer:
                raise PatchError("The BPS patch ends part way through a number")
            byte = patch[pos]
            pos += 1
            value += (byte & 0x7F) * shift
            if byte & 0x80:
                return value
            shift <<= 7
            value += shift
            if value > _MAX_VARINT:
                # Refuse it here: a run of continuation bytes would otherwise
                # build an ever-larger integer, quadratic in its length.
                raise PatchError("The BPS patch has a number too large to be real")

    source_size, target_size, metadata_size = varint(), varint(), varint()
    if metadata_size > footer - pos:
        raise PatchError("The BPS patch ends part way through its metadata")
    pos += metadata_size
    if len(source) != source_size:
        raise PatchError(
            f"This BPS patch is for a {source_size}-byte ROM; this one is "
            f"{len(source)} bytes",
        )
    if _crc(source) != want_source:
        raise PatchError(
            f"This BPS patch is for another ROM: it expects CRC32 {want_source:08x}, "
            f"this one is {_crc(source):08x}",
        )
    if target_size > MAX_TARGET_BYTES:
        raise PatchError(
            f"This BPS patch makes a {target_size}-byte ROM, over the "
            f"{MAX_TARGET_BYTES}-byte limit",
        )
    target = bytearray(target_size)
    out = source_rel = target_rel = 0
    while pos < footer:
        data = varint()
        command, length = data & 3, (data >> 2) + 1
        if out + length > target_size:
            raise PatchError("The BPS patch writes past the end of its target")
        if command == 0:  # SourceRead
            if out + length > source_size:
                raise PatchError("The BPS patch reads past the end of its source")
            target[out:out + length] = source[out:out + length]
        elif command == 1:  # TargetRead
            if pos + length > footer:
                raise PatchError("The BPS patch ends part way through its data")
            target[out:out + length] = patch[pos:pos + length]
            pos += length
        else:
            delta = varint()
            delta = -(delta >> 1) if delta & 1 else delta >> 1
            if command == 2:  # SourceCopy
                source_rel += delta
                if source_rel < 0 or source_rel + length > source_size:
                    raise PatchError("The BPS patch copies from outside its source")
                target[out:out + length] = source[source_rel:source_rel + length]
                source_rel += length
            else:  # TargetCopy: may overlap the bytes it writes
                target_rel += delta
                if target_rel < 0 or target_rel >= out:
                    raise PatchError("The BPS patch copies from outside its target")
                # An overlapping copy repeats the `out - target_rel` bytes before
                # the cursor, so build the run in one go rather than a byte loop.
                run = target[target_rel:target_rel + min(length, out - target_rel)]
                if len(run) < length:
                    run = run * -(-length // len(run))
                target[out:out + length] = run[:length]
                target_rel += length
        out += length
    if out != target_size:
        raise PatchError("The BPS patch ends before its target is complete")
    if _crc(target) != want_target:
        raise PatchError(
            f"The patched ROM doesn't match the patch's target CRC32 {want_target:08x}",
        )
    return bytes(target)
