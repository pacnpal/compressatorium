"""The patch engine (#428): IPS and BPS on bytes.

Pure bytes in, bytes out: source bytes and patch bytes give target bytes.
Every patch here is built by hand from the published formats, so each test
says exactly which record or action it exercises:

* IPS: ``PATCH``, records of a 3-byte offset, a 2-byte size and the data, an
  RLE record (size 0, then a 2-byte run and one byte), ``EOF``, and the
  truncation extension (a 3-byte length after ``EOF``).
* BPS: ``BPS1``, the source, target and metadata sizes as varints, the four
  actions (SourceRead, TargetRead, SourceCopy, TargetCopy), then the source,
  target and patch CRC32s, little-endian.
"""

from __future__ import annotations

import struct
import zlib

import pytest
from services import patch_engine
from services.patch_engine import (
    PatchFormatError,
    PatchTooLarge,
    SourceMismatch,
    TargetMismatch,
    apply_patch,
)

# ----------------------------------------------------------------------
# hand-built patches
# ----------------------------------------------------------------------


def _ips(*records: bytes, truncate: int | None = None) -> bytes:
    out = b"PATCH" + b"".join(records) + b"EOF"
    if truncate is not None:
        out += truncate.to_bytes(3, "big")
    return out


def _record(offset: int, data: bytes) -> bytes:
    return offset.to_bytes(3, "big") + len(data).to_bytes(2, "big") + data


def _rle(offset: int, count: int, value: int) -> bytes:
    return offset.to_bytes(3, "big") + b"\x00\x00" + count.to_bytes(2, "big") + bytes([value])


def _varint(value: int) -> bytes:
    """BPS's number encoding (each byte carries 7 bits; the last has bit 7 set)."""
    out = bytearray()
    while True:
        x = value & 0x7F
        value >>= 7
        if value == 0:
            out.append(0x80 | x)
            return bytes(out)
        out.append(x)
        value -= 1


def _signed(delta: int) -> bytes:
    return _varint((abs(delta) << 1) | (1 if delta < 0 else 0))


SOURCE_READ, TARGET_READ, SOURCE_COPY, TARGET_COPY = range(4)


def _action(kind: int, length: int) -> bytes:
    return _varint(((length - 1) << 2) | kind)


def _bps(
    source: bytes, target: bytes, actions: bytes, *, metadata: bytes = b"",
    source_crc: int | None = None, target_crc: int | None = None,
    declared_source: int | None = None, declared_target: int | None = None,
) -> bytes:
    body = (
        b"BPS1"
        + _varint(len(source) if declared_source is None else declared_source)
        + _varint(len(target) if declared_target is None else declared_target)
        + _varint(len(metadata)) + metadata + actions
    )
    body += struct.pack("<I", zlib.crc32(source) if source_crc is None else source_crc)
    body += struct.pack("<I", zlib.crc32(target) if target_crc is None else target_crc)
    return body + struct.pack("<I", zlib.crc32(body))


LIMIT = 1 << 20

# ----------------------------------------------------------------------
# IPS
# ----------------------------------------------------------------------


def test_ips_records_overwrite_bytes_at_their_offsets() -> None:
    source = bytes(range(16))
    patch = _ips(_record(2, b"\xAA\xBB"), _record(10, b"\xCC"))
    result = apply_patch(source, patch, max_target=LIMIT)
    expected = bytearray(source)
    expected[2:4] = b"\xAA\xBB"
    expected[10] = 0xCC
    assert result.target == bytes(expected)
    assert result.kind == "ips"
    # IPS carries no checksums: nothing proves the patch fits this ROM.
    assert result.checked is False


def test_an_ips_rle_record_repeats_one_byte() -> None:
    source = b"\x00" * 12
    result = apply_patch(source, _ips(_rle(3, 5, 0x7E)), max_target=LIMIT)
    assert result.target == b"\x00" * 3 + b"\x7E" * 5 + b"\x00" * 4


def test_an_ips_record_past_the_end_grows_the_rom() -> None:
    source = b"\x11" * 4
    result = apply_patch(source, _ips(_record(6, b"\x22\x22")), max_target=LIMIT)
    # The gap is filled with zeros, as every IPS patcher does.
    assert result.target == b"\x11" * 4 + b"\x00\x00" + b"\x22\x22"


def test_the_ips_truncation_extension_cuts_the_rom_short() -> None:
    source = bytes(range(32))
    result = apply_patch(source, _ips(_record(0, b"\xFF"), truncate=8), max_target=LIMIT)
    assert result.target == b"\xFF" + bytes(range(1, 8))


def test_an_ips_record_at_the_offset_spelling_eof_is_a_record_not_the_end() -> None:
    """0x454F46 spells ``EOF``: a record there is told apart from the end
    marker by what follows it, so a patch that writes there still applies."""
    source = b"\x00" * 0x454F46
    patch = _ips(_record(0x454F46, b"\x01\x02"))
    result = apply_patch(source, patch, max_target=0x454F46 + 2)
    assert result.target[-2:] == b"\x01\x02"
    assert len(result.target) == 0x454F46 + 2


@pytest.mark.parametrize(
    "patch",
    [
        b"PATCX" + _record(0, b"\x01") + b"EOF",  # not an IPS header
        b"PATCH" + _record(0, b"\x01"),  # no EOF
        b"PATCH" + (0).to_bytes(3, "big") + (4).to_bytes(2, "big") + b"\x01",  # short data
        b"PATCH" + _rle(0, 0, 1) + b"EOF",  # an RLE run of nothing
        b"PATCH" + b"EOF" + b"\x00\x01",  # a truncation of two bytes, not three
    ],
)
def test_a_malformed_ips_patch_is_refused(patch: bytes) -> None:
    with pytest.raises(PatchFormatError):
        apply_patch(b"\x00" * 8, patch, max_target=LIMIT)


def test_an_ips_patch_that_would_grow_past_the_limit_is_refused() -> None:
    with pytest.raises(PatchTooLarge):
        apply_patch(b"\x00" * 4, _ips(_record(100, b"\x01")), max_target=64)


# ----------------------------------------------------------------------
# BPS
# ----------------------------------------------------------------------


def test_bps_source_read_and_target_read() -> None:
    source = b"HELLO WORLD"
    target = b"HELLO THERE"
    actions = _action(SOURCE_READ, 6) + _action(TARGET_READ, 5) + b"THERE"
    result = apply_patch(source, _bps(source, target, actions), max_target=LIMIT)
    assert result.target == target
    assert result.kind == "bps"
    assert result.checked is True


def test_bps_source_copy_moves_its_cursor_both_ways() -> None:
    source = b"ABCDEFGHIJ"
    target = b"HIJABCDE"
    # Forward to 7 for "HIJ" (cursor ends at 10), then back 10 for "ABCDE".
    actions = (
        _action(SOURCE_COPY, 3) + _signed(7)
        + _action(SOURCE_COPY, 5) + _signed(-10)
    )
    result = apply_patch(source, _bps(source, target, actions), max_target=LIMIT)
    assert result.target == target


def test_bps_target_copy_repeats_what_it_has_written() -> None:
    """TargetCopy reads the target as it grows, so a copy from one byte back
    over eight bytes is a run: the classic overlapping case."""
    source = b""
    target = b"AB" + b"B" * 8 + b"ABBB"
    actions = (
        _action(TARGET_READ, 2) + b"AB"
        + _action(TARGET_COPY, 8) + _signed(1)  # from offset 1, overlapping
        + _action(TARGET_COPY, 4) + _signed(-9)  # back to 0
    )
    result = apply_patch(source, _bps(source, target, actions), max_target=LIMIT)
    assert result.target == target


def test_bps_metadata_is_skipped() -> None:
    source = b"abc"
    target = b"abd"
    actions = _action(SOURCE_READ, 2) + _action(TARGET_READ, 1) + b"d"
    patch = _bps(source, target, actions, metadata=b"<xml>hi</xml>")
    assert apply_patch(source, patch, max_target=LIMIT).target == target


def test_a_bps_patch_for_another_rom_is_refused_by_its_source_crc() -> None:
    made_for = b"the right rom"
    target = b"the right ROM"
    actions = _action(SOURCE_READ, 10) + _action(TARGET_READ, 3) + b"ROM"
    patch = _bps(made_for, target, actions)
    with pytest.raises(SourceMismatch) as caught:
        apply_patch(b"the wrong rom", patch, max_target=LIMIT)
    assert "isn't the one this patch was made for" in str(caught.value)


def test_a_bps_patch_for_a_rom_of_another_size_is_refused() -> None:
    made_for = b"0123456789"
    patch = _bps(made_for, made_for, _action(SOURCE_READ, 10))
    with pytest.raises(SourceMismatch):
        apply_patch(made_for + b"!", patch, max_target=LIMIT)


def test_a_bps_output_that_misses_the_target_crc_is_refused() -> None:
    source = b"abcdef"
    target = b"abcdef"
    patch = _bps(source, target, _action(SOURCE_READ, 6), target_crc=zlib.crc32(b"other"))
    with pytest.raises(TargetMismatch):
        apply_patch(source, patch, max_target=LIMIT)


def test_a_damaged_bps_patch_is_refused_by_its_own_crc() -> None:
    source = b"abcdef"
    target = b"abcxyz"
    patch = bytearray(_bps(source, target, _action(SOURCE_READ, 3) + _action(TARGET_READ, 3) + b"xyz"))
    patch[-14] ^= 0xFF  # a byte of the actions
    with pytest.raises(PatchFormatError) as caught:
        apply_patch(source, bytes(patch), max_target=LIMIT)
    assert "damaged" in str(caught.value)


def test_a_bps_patch_that_declares_a_huge_target_is_refused_before_it_runs() -> None:
    source = b"abc"
    patch = _bps(source, b"abc", _action(SOURCE_READ, 3), declared_target=1 << 40)
    with pytest.raises(PatchTooLarge):
        apply_patch(source, patch, max_target=LIMIT)


@pytest.mark.parametrize(
    "actions",
    [
        _action(SOURCE_READ, 9),  # reads past the source
        _action(TARGET_READ, 4) + b"ab",  # its data runs out
        _action(SOURCE_COPY, 2) + _signed(-1),  # copies from before the source
        _action(TARGET_COPY, 2) + _signed(0),  # copies what isn't written yet
        _action(TARGET_READ, 3) + b"abc" + _action(TARGET_READ, 3) + b"def",  # writes past the target
    ],
)
def test_a_bps_action_that_reaches_outside_its_buffers_is_refused(actions: bytes) -> None:
    source = b"abcd"
    target = b"abcd"
    with pytest.raises(PatchFormatError):
        apply_patch(source, _bps(source, target, actions), max_target=LIMIT)


def test_a_bps_patch_that_writes_too_little_is_refused() -> None:
    source = b"abcd"
    with pytest.raises(PatchFormatError):
        apply_patch(source, _bps(source, b"abcd", _action(SOURCE_READ, 2)), max_target=LIMIT)


# ----------------------------------------------------------------------
# what a patch is
# ----------------------------------------------------------------------


def test_the_format_is_read_from_the_patch_not_its_name() -> None:
    assert patch_engine.patch_kind(_ips(_record(0, b"\x01"))) == "ips"
    assert patch_engine.patch_kind(_bps(b"a", b"a", _action(SOURCE_READ, 1))) == "bps"
    with pytest.raises(PatchFormatError):
        patch_engine.patch_kind(b"\x00\x01\x02 not a patch")
