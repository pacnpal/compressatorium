"""The IPS/BPS engine (#428) on bytes: hand-built patches and fixed vectors.

The fixed vectors are written byte by byte from the formats' specs (IPS's
record layout; byuu's BPS spec, CRC32s included), so they pin the format
independently of the small encoders below.
"""

from __future__ import annotations

import zlib

import pytest
from services.patch_engine import (
    PatchError,
    apply_bps,
    apply_ips,
    apply_patch,
    patch_format,
)


def _crc(data: bytes) -> bytes:
    return (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "little")


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        x = value & 0x7F
        value >>= 7
        if value == 0:
            out.append(0x80 | x)
            return bytes(out)
        out.append(x)
        value -= 1


def _bps(source: bytes, target: bytes, actions: bytes, metadata: bytes = b"") -> bytes:
    body = (
        b"BPS1" + _varint(len(source)) + _varint(len(target))
        + _varint(len(metadata)) + metadata + actions + _crc(source) + _crc(target)
    )
    return body + _crc(body)


def _action(command: int, length: int, extra: bytes = b"") -> bytes:
    return _varint(((length - 1) << 2) | command) + extra


def _delta(n: int) -> bytes:
    return _varint((abs(n) << 1) | (1 if n < 0 else 0))


# --- fixed vectors ---------------------------------------------------------

IPS_VECTOR = bytes.fromhex(
    "5041544348"          # PATCH
    "000001" "0002" "5859"  # offset 1, 2 bytes: "XY"
    "000008" "0000" "0004" "5a"  # RLE at 8: 4 x "Z"
    "454f46"              # EOF
    "00000a"              # truncate to 10 bytes
)

# source "Hello" -> "Hello World!!!!lo": SourceRead 5, TargetRead " World",
# TargetRead "!", TargetCopy 3 from 11 (overlapping), SourceCopy 2 from 3.
BPS_VECTOR = bytes.fromhex(
    "42505331859180909520576f726c6481218b968686"
    "8289d1f7" "146064f3" "55cecfd3"
)


def test_ips_vector_records_rle_and_truncation() -> None:
    assert apply_ips(b"0123456789ABCDEF", IPS_VECTOR) == b"0XY34567ZZ"


def test_bps_vector_every_action() -> None:
    assert apply_bps(b"Hello", BPS_VECTOR) == b"Hello World!!!!lo"


def test_the_format_is_told_by_magic() -> None:
    assert patch_format(IPS_VECTOR) == "ips"
    assert patch_format(BPS_VECTOR) == "bps"
    assert apply_patch(b"Hello", BPS_VECTOR) == b"Hello World!!!!lo"
    with pytest.raises(PatchError):
        apply_patch(b"Hello", b"UPS1....")


# --- IPS -------------------------------------------------------------------


def test_ips_record_past_the_end_grows_the_target() -> None:
    patch = b"PATCH" + b"\x00\x00\x06" + b"\x00\x02" + b"!!" + b"EOF"
    assert apply_ips(b"abcd", patch) == b"abcd\x00\x00!!"


def test_ips_rle_past_the_end_grows_the_target() -> None:
    patch = b"PATCH" + b"\x00\x00\x02" + b"\x00\x00" + b"\x00\x03" + b"z" + b"EOF"
    assert apply_ips(b"abcd", patch) == b"abzzz"


def test_ips_without_eof_or_mid_record_is_refused() -> None:
    with pytest.raises(PatchError, match="part way"):
        apply_ips(b"abcd", b"PATCH\x00\x00\x01\x00\x05ab")
    with pytest.raises(PatchError, match="part way"):
        apply_ips(b"abcd", b"PATCH\x00\x00\x01\x00\x01a")
    with pytest.raises(PatchError, match="after EOF"):
        apply_ips(b"abcd", b"PATCHEOF\x00")


# --- BPS -------------------------------------------------------------------


def test_bps_source_copy_moves_backwards_and_forwards() -> None:
    source = b"0123456789"
    target = b"789" + b"012" + b"56"
    actions = (
        _action(2, 3, _delta(7))     # 7..9, rel -> 10
        + _action(2, 3, _delta(-10))  # 0..2, rel -> 3
        + _action(2, 2, _delta(2))    # 5..6
    )
    assert apply_bps(source, _bps(source, target, actions)) == target


def test_bps_target_copy_repeats_a_run() -> None:
    source = b""
    target = b"ab" + b"ababab"
    actions = _action(1, 2, b"ab") + _action(3, 6, _delta(0))
    assert apply_bps(source, _bps(source, target, actions, metadata=b"<x/>")) == target


def test_bps_source_crc_mismatch_is_refused() -> None:
    patch = _bps(b"Hello", b"Hello", _action(0, 5))
    with pytest.raises(PatchError, match="another ROM"):
        apply_bps(b"Jello", patch)


def test_bps_source_size_mismatch_is_refused() -> None:
    patch = _bps(b"Hello", b"Hello", _action(0, 5))
    with pytest.raises(PatchError, match="5-byte ROM"):
        apply_bps(b"Hello!", patch)


def test_bps_target_crc_mismatch_is_refused() -> None:
    """The patch's own CRC holds; only the target CRC it carries is wrong."""
    source = b"Hello"
    body = (
        b"BPS1" + _varint(5) + _varint(5) + _varint(0) + _action(0, 5)
        + _crc(source) + _crc(b"Jello")
    )
    with pytest.raises(PatchError, match="target CRC32"):
        apply_bps(source, body + _crc(body))


def test_bps_patch_crc_mismatch_is_refused() -> None:
    damaged = bytearray(BPS_VECTOR)
    damaged[10] ^= 0xFF
    with pytest.raises(PatchError, match="damaged"):
        apply_bps(b"Hello", bytes(damaged))


def test_bps_reads_outside_bounds_are_refused() -> None:
    with pytest.raises(PatchError, match="outside its source"):
        apply_bps(b"abc", _bps(b"abc", b"abcd", _action(2, 4, _delta(0))))
    with pytest.raises(PatchError, match="outside its target"):
        apply_bps(b"abc", _bps(b"abc", b"ab", _action(3, 2, _delta(0))))
    with pytest.raises(PatchError, match="before its target is complete"):
        apply_bps(b"abc", _bps(b"abc", b"abcd", _action(0, 3)))


def test_bps_target_over_the_limit_is_refused_before_allocating() -> None:
    """A tiny patch can declare a huge target; refuse it before the bytearray."""
    import services.patch_engine as engine

    source = b"\x00"
    body = (
        b"BPS1" + _varint(len(source)) + _varint(engine.MAX_TARGET_BYTES + 1)
        + _varint(0) + _crc(source) + _crc(b"")
    )
    with pytest.raises(PatchError, match="limit"):
        apply_bps(source, body + _crc(body))


def test_bps_endless_number_is_refused_early() -> None:
    """A size field of continuation bytes is refused, not parsed to the end."""
    body = b"BPS1" + b"\x00" * 100_000 + b"\x80" + _crc(b"") + _crc(b"")
    with pytest.raises(PatchError, match="too large"):
        apply_bps(b"", body + _crc(body))


def test_bps_metadata_longer_than_the_patch_is_refused() -> None:
    source = b"A"
    body = (
        b"BPS1" + _varint(1) + _varint(0) + _varint(127) + _crc(source) + _crc(b"")
    )
    with pytest.raises(PatchError, match="metadata"):
        apply_bps(source, body + _crc(body))


def test_bps_long_target_copy_run_is_built_in_bulk() -> None:
    """One byte, then a 4 MiB TargetCopy of it: fast, and byte-exact."""
    import time

    size = 4 * 1024 * 1024
    target = b"\x00" * size
    actions = _action(1, 1, b"\x00") + _action(3, size - 1, _delta(0))
    patch = _bps(b"", target, actions)
    started = time.monotonic()
    assert apply_bps(b"", patch) == target
    assert time.monotonic() - started < 1.0
