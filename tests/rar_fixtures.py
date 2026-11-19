"""The rar tool's test sets (#476), copied out of ``tests/fixtures/rar``.

The checked-in archives are rarfile's own test files (see the README there).
Their volume sets hold two ``.txt`` members, which are release sidecars to the
extractor, so a test that needs a payload copies a set and renames a member in
place (:func:`rename_member`): the new name has the same length, the header
CRCs are recomputed, and the stored data is untouched.
"""
from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "rar"

# Each set's volumes, first volume first.
RAR5_SET = ("rar5-vols.part1.rar", "rar5-vols.part2.rar", "rar5-vols.part3.rar")
RAR4_SET = ("rar3-old.rar", "rar3-old.r00", "rar3-old.r01")
BIG = "vols/bigfile.txt"
SMALL = "vols/smallfile.txt"
BIG_SIZE = 205000
BIG_CRC = 0x509AD74C

_RAR5_SIG = b"Rar!\x1a\x07\x01\x00"
_RAR4_SIG = b"Rar!\x1a\x07\x00"


def copy_set(names: tuple[str, ...], folder: Path) -> list[Path]:
    """Copy a set's volumes into *folder*; the copies, first volume first."""
    folder.mkdir(parents=True, exist_ok=True)
    copies = []
    for name in names:
        target = folder / name
        shutil.copyfile(FIXTURES / name, target)
        copies.append(target)
    return copies


def payload_set(names: tuple[str, ...], folder: Path, *, as_name: str = "vols/bigfile.gba"):
    """A copy of a volume set whose big member is renamed to *as_name*, so it
    holds one payload and one ``.txt`` sidecar."""
    volumes = copy_set(names, folder)
    rename_member(volumes, BIG, as_name)
    return volumes


def _vint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _rename_rar5(data: bytearray, old: bytes, new: bytes) -> None:
    pos = len(_RAR5_SIG)
    while pos + 4 < len(data):
        size, body = _vint(data, pos + 4)
        end = body + size
        kind, cursor = _vint(data, body)
        flags, cursor = _vint(data, cursor)
        if flags & 0x1:
            _extra, cursor = _vint(data, cursor)
        data_size = 0
        if flags & 0x2:
            data_size, cursor = _vint(data, cursor)
        header = bytes(data[body:end])
        if old in header:
            data[body:end] = header.replace(old, new)
            struct.pack_into("<I", data, pos, zlib.crc32(bytes(data[pos + 4:end])))
        if kind == 5:  # end of archive
            return
        pos = end + data_size


def _rename_rar4(data: bytearray, old: bytes, new: bytes) -> None:
    pos = len(_RAR4_SIG)
    while pos + 7 <= len(data):
        kind, flags, head_size = struct.unpack_from("<BHH", data, pos + 2)
        add = 0
        if flags & 0x8000:
            add = struct.unpack_from("<I", data, pos + 7)[0]
            if kind == 0x74 and flags & 0x100:
                add += struct.unpack_from("<I", data, pos + 32)[0] << 32
        header = bytes(data[pos:pos + head_size])
        if old in header:
            data[pos:pos + head_size] = header.replace(old, new)
            crc = zlib.crc32(bytes(data[pos + 2:pos + head_size])) & 0xFFFF
            struct.pack_into("<H", data, pos, crc)
        if kind == 0x7B:  # end of archive
            return
        pos += head_size + add


def rename_member(volumes: list[Path], old: str, new: str) -> None:
    """Rename member *old* to *new* (same length) in every volume's headers."""
    old_b, new_b = old.encode(), new.encode()
    if len(old_b) != len(new_b):
        raise ValueError("the new name must have the old one's length")
    for volume in volumes:
        data = bytearray(volume.read_bytes())
        if data.startswith(_RAR5_SIG):
            _rename_rar5(data, old_b, new_b)
        elif data.startswith(_RAR4_SIG):
            # RAR 2.9 stores the folder separator as a backslash.
            _rename_rar4(data, old_b.replace(b"/", b"\\"), new_b.replace(b"/", b"\\"))
        else:
            raise ValueError(f"{volume} is not a RAR volume")
        volume.write_bytes(bytes(data))


if __name__ == "__main__":
    # `python tests/rar_fixtures.py DIR`: a payload copy of each set in
    # DIR/rar5 and DIR/rar4, for a manual run of rar_extract (the PR smoke).
    import sys

    dest = Path(sys.argv[1])
    payload_set(RAR5_SET, dest / "rar5")
    payload_set(RAR4_SET, dest / "rar4")
    print(f"wrote {dest / 'rar5'} and {dest / 'rar4'}")
