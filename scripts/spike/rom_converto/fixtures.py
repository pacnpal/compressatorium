"""Synthetic inputs for the rom-converto spike (#421).

Everything here is built from seeded random bytes or hand-made headers. Nothing
is a commercial game image, and no key or seed is used or shipped.
"""

import hashlib
import struct

SECTOR = 2048
MAGIC = b"MICROSOFT*XBOX*MEDIA"
# Partition bases rom-converto probes for an Xbox 360 image.
BASE_TRIMMED = 0x0
BASE_XGD2 = 0x0FD90000
BASE_XGD3 = 0x02080000


def rand_bytes(size, seed):
    """Deterministic filler: a SHA-256 counter stream, so runs are repeatable."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


def _entry_size(name):
    return (14 + len(name.encode("latin-1")) + 3) & ~3


def dir_table(entries):
    """Pack (name, start_sector, size) files into one XDVDFS directory table.

    The table is a binary search tree whose child links are offsets in 4-byte
    units. Names are sorted case-insensitively and split at the middle, so the
    tree is balanced. Unused tail bytes are 0xFF, as on a real disc.
    """
    entries = sorted(entries, key=lambda e: e[0].lower())
    offsets = {}
    order = []

    def place(lo, hi):
        if lo >= hi:
            return None
        mid = (lo + hi) // 2
        order.append(mid)
        return mid, place(lo, mid), place(mid + 1, hi)

    tree = place(0, len(entries))
    cursor = 0
    for i in order:
        offsets[i] = cursor
        cursor += _entry_size(entries[i][0])

    blobs = {}

    def emit(node):
        if node is None:
            return
        i, left, right = node
        name, sector, size = entries[i]
        raw = name.encode("latin-1")
        rec = struct.pack(
            "<HHIIBB",
            offsets[left[0]] // 4 if left else 0,
            offsets[right[0]] // 4 if right else 0,
            sector,
            size,
            0x20,
            len(raw),
        )
        rec += raw
        blobs[i] = rec + b"\xff" * (_entry_size(name) - len(rec))
        emit(left)
        emit(right)

    emit(tree)
    out = b"".join(blobs[i] for i in order)
    return out + b"\xff" * ((-len(out)) % SECTOR)


def build_xdvdfs(files, base=BASE_TRIMMED):
    """A small XDVDFS image holding `files` ({root-level name: bytes}).

    A non-zero `base` puts the volume at that offset, the way a full XGD disc
    dump does (the gap before it is zeros and stays sparse on disk).
    """
    root_sector = 33
    table_sectors = len(dir_table([(n, 0, len(d)) for n, d in files.items()])) // SECTOR
    next_sector = root_sector + table_sectors
    entries, payload = [], []
    for name, data in files.items():
        entries.append((name, next_sector, len(data)))
        payload.append((next_sector, data))
        next_sector += (len(data) + SECTOR - 1) // SECTOR
    table = dir_table(entries)
    img = bytearray(base + next_sector * SECTOR)
    vd = bytearray(SECTOR)
    vd[0:20] = MAGIC
    struct.pack_into("<II", vd, 20, root_sector, len(table))
    vd[0x7EC : 0x7EC + 20] = MAGIC
    img[base + 32 * SECTOR : base + 33 * SECTOR] = vd
    start = base + root_sector * SECTOR
    img[start : start + len(table)] = table
    for sector, data in payload:
        img[base + sector * SECTOR : base + sector * SECTOR + len(data)] = data
    return bytes(img)


def xbox360_files(seed=1):
    """A tiny file set. default.xex is random bytes, not a real XEX."""
    return {
        "default.xex": rand_bytes(5000, seed),
        "a.bin": rand_bytes(70000, seed + 1),
        "readme.txt": b"synthetic spike file\n" * 10,
    }


def build_pbp(psar=b"", title="SPIKE"):
    """A hand-made EBOOT.PBP: header, a PARAM.SFO and a junk PSAR.

    The PSAR is not an NPUMDIMG, so `psp to-iso` has to refuse it. A real PSN
    PBP can't be built without a game's own encrypted image.
    """
    values = [(b"TITLE", title.encode() + b"\x00"), (b"DISC_ID", b"NPUZ00000\x00")]
    idx = key_table = data_table = b""
    for key, value in values:
        padded = (len(value) + 3) & ~3
        idx += struct.pack("<HHIII", len(key_table), 0x0204, len(value), padded, len(data_table))
        key_table += key + b"\x00"
        data_table += value + b"\x00" * (padded - len(value))
    key_table += b"\x00" * ((-len(key_table)) % 4)
    key_start = 20 + len(idx)
    sfo = (
        b"\x00PSF"
        + struct.pack("<IIII", 0x101, key_start, key_start + len(key_table), len(values))
        + idx
        + key_table
        + data_table
    )
    sections = [sfo, b"", b"", b"", b"", b"", b"", psar]
    offsets, off = [], 0x28
    for section in sections:
        offsets.append(off)
        off += len(section)
    header = (
        b"\x00PBP" + struct.pack("<I", 0x10000) + b"".join(struct.pack("<I", o) for o in offsets)
    )
    return header + b"".join(sections)
