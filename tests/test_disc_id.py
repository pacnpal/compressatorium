"""
Tests for the disc_id extraction service.

Covers:
  - ISO 9660 source-file parsing (PS2, PSP, PS1)
  - Dreamcast IP.BIN parsing
  - PSP PARAM.SFO binary parsing
  - Dreamcast GDI track parsing
  - BIN sector-stream adapter
  - CD CHD sector extraction (Mode 1 and Mode 2 Form 1)
  - CHD extraction stubs (dumpmeta / companion-file paths)
"""

from __future__ import annotations

import io
import asyncio
import struct
from unittest.mock import patch

import pytest

from app.services.disc_id import (
    _BinSectorStream,
    _dumpmeta_raw,
    _extract_cue,
    _extract_gdi,
    _extract_iso,
    _normalize_ps_serial,
    _parse_ipbin,
    _parse_param_sfo,
    _parse_system_cnf,
    extract_from_chd,
    extract_from_source,
)

from ._chd_fakes import make_chd_v5 as _make_chd_v5
from ._chd_fakes import mode2_frames, serve_extractraw


@pytest.fixture(autouse=True)
def _volume(tmp_path, monkeypatch):
    """``tmp_path`` is the configured Volume: sheet tracks resolve only inside one."""
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))


# ---------------------------------------------------------------------------
# Helpers to build minimal disc images in memory
# ---------------------------------------------------------------------------

SECTOR = 2048
PVD_SECTOR = 16
PVD_MAGIC = b"\x01CD001\x01"


def _make_iso(files: dict[str, bytes]) -> bytes:
    """
    Build a minimal ISO 9660 image with the given {path: content} mapping.
    Supports one level of sub-directory (e.g. "PSP_GAME/PARAM.SFO").
    Returns raw bytes (2048-byte sectors).
    """
    # Allocate a mutable bytearray, grow lazily.
    sectors: list[bytearray] = [bytearray(SECTOR) for _ in range(20)]

    def ensure(n: int):
        while len(sectors) <= n:
            sectors.append(bytearray(SECTOR))

    def write_sector(idx: int, data: bytes):
        ensure(idx)
        sectors[idx][: len(data)] = data

    def dir_record(name: str, lba: int, size: int, is_dir: bool = False) -> bytes:
        name_enc = name.encode("ascii")
        name_len = len(name_enc)
        rec_len = 33 + name_len
        if rec_len % 2:
            rec_len += 1
        r = bytearray(rec_len)
        r[0] = rec_len
        # LBA (little-endian 4 bytes at offset 2)
        struct.pack_into("<I", r, 2, lba)
        struct.pack_into(">I", r, 6, lba)
        # Size (little-endian 4 bytes at offset 10)
        struct.pack_into("<I", r, 10, size)
        struct.pack_into(">I", r, 14, size)
        r[25] = 0x02 if is_dir else 0x00
        r[32] = name_len
        r[33 : 33 + name_len] = name_enc
        return bytes(r)

    # Place directories / files starting at sector 20
    next_sector = 20

    # Collect top-level names and sub-dirs
    top_files: dict[str, bytes] = {}
    sub_dirs: dict[str, dict[str, bytes]] = {}

    for path, content in files.items():
        parts = path.split("/")
        if len(parts) == 1:
            top_files[parts[0]] = content
        elif len(parts) == 2:
            sub_dirs.setdefault(parts[0], {})[parts[1]] = content

    # Write sub-directory sectors first so we know their LBAs
    sub_dir_lbas: dict[str, tuple[int, int]] = {}
    for dname, dfiles in sub_dirs.items():
        dir_start = next_sector
        dir_data = bytearray()
        # dot entry
        dot = dir_record("\x00", dir_start, 0, is_dir=True)
        dir_data.extend(dot)
        dotdot = dir_record("\x01", 19, 0, is_dir=True)
        dir_data.extend(dotdot)
        for fname, fcontent in dfiles.items():
            flba = next_sector + 100 + list(dfiles.keys()).index(fname)
            dir_data.extend(dir_record(fname.upper(), flba, len(fcontent)))
        ensure(dir_start)
        sectors[dir_start][: len(dir_data)] = dir_data
        next_sector += 1

        # Write file content
        for i, (fname, fcontent) in enumerate(dfiles.items()):
            flba = dir_start + 100 + i
            ensure(flba)
            sectors[flba][: len(fcontent)] = fcontent
            next_sector = max(next_sector, flba + 1)

        sub_dir_lbas[dname] = (dir_start, len(dir_data))

    # Write root directory (sector 19)
    root_data = bytearray()
    dot = dir_record("\x00", 19, 0, is_dir=True)
    root_data.extend(dot)
    dotdot = dir_record("\x01", 19, 0, is_dir=True)
    root_data.extend(dotdot)

    file_lba_map: dict[str, int] = {}
    for fname, fcontent in top_files.items():
        flba = next_sector
        next_sector += 1
        file_lba_map[fname] = flba
        root_data.extend(dir_record(fname.upper(), flba, len(fcontent)))

    for dname, (dlba, dsize) in sub_dir_lbas.items():
        root_data.extend(dir_record(dname.upper(), dlba, dsize, is_dir=True))

    ensure(19)
    sectors[19][: len(root_data)] = root_data

    # Write top-level files
    for fname, fcontent in top_files.items():
        flba = file_lba_map[fname]
        ensure(flba)
        sectors[flba][: len(fcontent)] = fcontent

    # Build PVD at sector 16
    pvd = bytearray(SECTOR)
    pvd[:7] = b"\x01CD001\x01"
    # Root directory record at offset 156 (34 bytes)
    root_rec = bytearray(34)
    struct.pack_into("<I", root_rec, 2, 19)   # root LBA
    struct.pack_into(">I", root_rec, 6, 19)
    struct.pack_into("<I", root_rec, 10, len(root_data))
    struct.pack_into(">I", root_rec, 14, len(root_data))
    root_rec[25] = 0x02  # directory flag
    root_rec[32] = 1
    root_rec[33] = 0x00  # name = \x00 (self)
    pvd[156 : 156 + 34] = root_rec
    write_sector(16, bytes(pvd))

    # Assemble all sectors into a flat bytes object
    total = max(len(sectors), next_sector + 1)
    ensure(total - 1)
    return b"".join(bytes(s) for s in sectors)


# ---------------------------------------------------------------------------
# _normalize_ps_serial (PCSX2-compatible ExecutablePathToSerial)
# ---------------------------------------------------------------------------

def test_normalize_ps_serial_ps2_from_full_path():
    """Full BOOT2 path → canonical PCSX2 form."""
    assert _normalize_ps_serial("cdrom0:\\SLUS_203.12;1") == "SLUS-20312"


def test_normalize_ps_serial_ps2_filename_only():
    """Filename already stripped → same normalization."""
    assert _normalize_ps_serial("SLUS_203.12") == "SLUS-20312"


def test_normalize_ps_serial_ps1():
    assert _normalize_ps_serial("cdrom:\\SLPS_123.45;1") == "SLPS-12345"


def test_normalize_ps_serial_already_dash():
    """If the separator is already a dash, still normalizes correctly."""
    assert _normalize_ps_serial("SCES-503.08") == "SCES-50308"


def test_normalize_ps_serial_no_dot_rejected():
    """Serial without the canonical dot is rejected (non-standard format)."""
    assert _normalize_ps_serial("SLUS_20312") is None


def test_normalize_ps_serial_empty():
    assert _normalize_ps_serial("") is None


def test_normalize_ps_serial_garbage():
    assert _normalize_ps_serial("not_a_serial") is None


# ---------------------------------------------------------------------------
# _parse_system_cnf, now returns normalized serials
# ---------------------------------------------------------------------------

def test_parse_system_cnf_ps2():
    # Realistic PS2 SYSTEM.CNF, serial includes the canonical dot
    data = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\n"
    result = _parse_system_cnf(data)
    assert result["game_id"] == "SLUS-20312"
    assert result["platform"] == "ps2"


def test_parse_system_cnf_ps2_backslash_variants():
    for line, expected in (
        (b"BOOT2=cdrom0:\\SLUS_203.12;1", "SLUS-20312"),
        (b"BOOT2 = cdrom0:/SLUS_203.12;1", "SLUS-20312"),
        (b"BOOT2=cdrom:\\SLUS_203.12;1", "SLUS-20312"),
    ):
        result = _parse_system_cnf(line)
        assert result.get("game_id") == expected, f"failed for: {line}"


def test_parse_system_cnf_ps1():
    data = b"BOOT = cdrom:\\SLUS_123.45;1\n"
    result = _parse_system_cnf(data)
    assert result["game_id"] == "SLUS-12345"
    assert result["platform"] == "ps1"


def test_parse_system_cnf_empty():
    result = _parse_system_cnf(b"VMODE = NTSC\n")
    assert result == {}


# ---------------------------------------------------------------------------
# _parse_param_sfo
# ---------------------------------------------------------------------------

def _make_param_sfo(fields: dict[str, str]) -> bytes:
    """Build a minimal PARAM.SFO binary for the given string fields."""
    # Build key table and data table
    keys = list(fields.keys())
    values = list(fields.values())

    key_table = bytearray()
    key_offsets = []
    for k in keys:
        key_offsets.append(len(key_table))
        key_table.extend(k.encode("ascii") + b"\x00")

    data_table = bytearray()
    data_offsets = []
    data_lens = []
    for v in values:
        data_offsets.append(len(data_table))
        enc = v.encode("utf-8") + b"\x00"
        data_lens.append(len(enc))
        data_table.extend(enc)

    num_entries = len(keys)
    header_size = 20
    index_size = num_entries * 16
    key_table_offset = header_size + index_size
    data_table_offset = key_table_offset + len(key_table)

    header = struct.pack(
        "<4sHHIII",
        b"\x00PSF",
        0x01,
        0x01,
        key_table_offset,
        data_table_offset,
        num_entries,
    )

    index = bytearray()
    for i, (k, v) in enumerate(zip(keys, values)):
        index.extend(
            struct.pack(
                "<HHIII",
                key_offsets[i],  # key_offset
                0x0204,          # fmt = UTF8
                data_lens[i],    # data_len
                data_lens[i],    # data_max_len
                data_offsets[i], # data_offset
            )
        )

    return header + bytes(index) + bytes(key_table) + bytes(data_table)


def test_parse_param_sfo_basic():
    sfo = _make_param_sfo({"DISC_ID": "ULES00135", "TITLE": "Patapon"})
    result = _parse_param_sfo(sfo)
    assert result["game_id"] == "ULES00135"
    assert result["title"] == "Patapon"
    assert result["platform"] == "psp"


def test_parse_param_sfo_missing_title():
    sfo = _make_param_sfo({"DISC_ID": "UCUS98744"})
    result = _parse_param_sfo(sfo)
    assert result["game_id"] == "UCUS98744"
    assert "title" not in result


def test_parse_param_sfo_bad_magic():
    result = _parse_param_sfo(b"BADD" + b"\x00" * 100)
    assert result == {}


def test_parse_param_sfo_too_short():
    result = _parse_param_sfo(b"\x00PSF")
    assert result == {}


# ---------------------------------------------------------------------------
# _parse_ipbin
# ---------------------------------------------------------------------------

def _make_ipbin(product_number: str = "MK-51034  ", title: str = "TEST GAME") -> bytes:
    data = bytearray(0x200)
    data[0x00 : 0x10] = b"SEGA SEGAKATANA "
    data[0x10 : 0x20] = b"SEGA ENTERPRISES"
    pn = product_number.ljust(10).encode("ascii")[:10]
    data[0x40 : 0x4A] = pn
    t = title.ljust(128).encode("ascii")[:128]
    data[0x80 : 0x100] = t
    return bytes(data)


def test_parse_ipbin_basic():
    data = _make_ipbin("MK-51034  ", "DEAD OR ALIVE 2 HARDCORE")
    result = _parse_ipbin(data)
    assert result["game_id"] == "MK-51034"
    assert "DEAD OR ALIVE" in result["title"]
    assert result["platform"] == "dreamcast"


def test_parse_ipbin_wrong_hwid():
    data = bytearray(_make_ipbin())
    data[0x00 : 0x10] = b"NOTADREAMCAST   "
    result = _parse_ipbin(bytes(data))
    assert result == {}


def test_parse_ipbin_too_short():
    result = _parse_ipbin(b"SEGA SEGAKATANA " + b"\x00" * 10)
    assert result == {}


# ---------------------------------------------------------------------------
# ISO extraction from a real in-memory ISO
# ---------------------------------------------------------------------------

def test_extract_iso_ps2(tmp_path):
    # Use the canonical PS2 serial format with dot (SLUS_XXX.YY)
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\nVER = 1.00\n"
    iso_bytes = _make_iso({"SYSTEM.CNF": cnf})
    iso_path = tmp_path / "game.iso"
    iso_path.write_bytes(iso_bytes)
    result = _extract_iso(str(iso_path))
    assert result is not None
    assert result["game_id"] == "SLUS-20312"
    assert result["platform"] == "ps2"


def test_extract_iso_psp(tmp_path):
    sfo = _make_param_sfo({"DISC_ID": "ULES00135", "TITLE": "Patapon"})
    iso_bytes = _make_iso({"PSP_GAME/PARAM.SFO": sfo})
    iso_path = tmp_path / "game.iso"
    iso_path.write_bytes(iso_bytes)
    result = _extract_iso(str(iso_path))
    assert result is not None
    assert result["game_id"] == "ULES00135"
    assert result["title"] == "Patapon"
    assert result["platform"] == "psp"


def test_extract_iso_no_match(tmp_path):
    iso_bytes = _make_iso({"README.TXT": b"nothing here"})
    iso_path = tmp_path / "game.iso"
    iso_path.write_bytes(iso_bytes)
    result = _extract_iso(str(iso_path))
    assert result is None


def test_extract_iso_not_iso9660(tmp_path):
    iso_path = tmp_path / "game.iso"
    iso_path.write_bytes(b"\x00" * (34 * 2048))
    result = _extract_iso(str(iso_path))
    assert result is None


# ---------------------------------------------------------------------------
# extract_from_source dispatch
# ---------------------------------------------------------------------------

def test_extract_from_source_iso(tmp_path):
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"
    iso_bytes = _make_iso({"SYSTEM.CNF": cnf})
    p = tmp_path / "game.iso"
    p.write_bytes(iso_bytes)
    result = extract_from_source(str(p))
    assert result and result["game_id"] == "SLUS-20312"


def test_extract_from_source_unknown_ext(tmp_path):
    p = tmp_path / "game.z64"
    p.write_bytes(b"\x80\x37\x12\x40" + b"\x00" * 100)
    result = extract_from_source(str(p))
    assert result is None


def _make_mode1_bin(iso_bytes: bytes) -> bytes:
    """
    Wrap a flat ISO 9660 image (2048-byte sectors) in Mode 1 2352-byte sectors.

    Each physical sector = 16-byte header + 2048-byte payload + 288-byte ECC pad.
    This produces a BIN that ``_extract_bin`` can parse (it probes for the PVD at
    sector 16 with a 16-byte offset).
    """
    SECTOR = 2352
    HEADER = 16  # Mode 1
    DATA = 2048
    TAIL = SECTOR - HEADER - DATA  # 288
    num = (len(iso_bytes) + DATA - 1) // DATA
    buf = bytearray()
    for i in range(num):
        buf += b"\x00" * HEADER
        chunk = iso_bytes[i * DATA : (i + 1) * DATA]
        buf += chunk.ljust(DATA, b"\x00")
        buf += b"\x00" * TAIL
    return bytes(buf)


# ---------------------------------------------------------------------------
# CUE extraction
# ---------------------------------------------------------------------------

def test_extract_cue_ps1(tmp_path):
    """CUE sheet pointing to a Mode 1 BIN with a PS1 SYSTEM.CNF is extracted."""
    cnf = b"BOOT = cdrom:\\SLPS_123.45;1\n"
    bin_path = tmp_path / "track01.bin"
    bin_path.write_bytes(_make_mode1_bin(_make_iso({"SYSTEM.CNF": cnf})))

    cue_path = tmp_path / "game.cue"
    cue_path.write_text(
        f'FILE "{bin_path.name}" BINARY\n'
        "  TRACK 01 MODE1/2352\n"
        "    INDEX 01 00:00:00\n"
    )

    result = _extract_cue(str(cue_path))
    assert result is not None
    assert result["game_id"] == "SLPS-12345"


def test_extract_cue_bin_missing(tmp_path):
    """CUE sheet whose BIN file is absent returns None without raising."""
    cue_path = tmp_path / "game.cue"
    cue_path.write_text('FILE "nonexistent.bin" BINARY\n  TRACK 01 MODE1/2352\n')

    result = _extract_cue(str(cue_path))
    assert result is None


def test_extract_cue_fallback_to_second_file(tmp_path):
    """When the first FILE entry is missing, the second FILE entry is tried."""
    cnf = b"BOOT = cdrom:\\SLPS_123.45;1\n"
    bin_path = tmp_path / "track02.bin"
    bin_path.write_bytes(_make_mode1_bin(_make_iso({"SYSTEM.CNF": cnf})))

    cue_path = tmp_path / "game.cue"
    cue_path.write_text(
        'FILE "nonexistent.bin" BINARY\n'
        "  TRACK 01 MODE1/2352\n"
        f'FILE "{bin_path.name}" BINARY\n'
        "  TRACK 02 MODE1/2352\n"
        "    INDEX 01 00:00:00\n"
    )

    result = _extract_cue(str(cue_path))
    assert result is not None
    assert result["game_id"] == "SLPS-12345"


def test_extract_from_source_cue(tmp_path):
    """extract_from_source dispatches to _extract_cue for .cue files."""
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"
    bin_path = tmp_path / "track01.bin"
    bin_path.write_bytes(_make_mode1_bin(_make_iso({"SYSTEM.CNF": cnf})))

    cue_path = tmp_path / "game.cue"
    cue_path.write_text(
        f'FILE "{bin_path.name}" BINARY\n'
        "  TRACK 01 MODE1/2352\n"
        "    INDEX 01 00:00:00\n"
    )

    result = extract_from_source(str(cue_path))
    assert result is not None
    assert result["game_id"] == "SLUS-20312"


# ---------------------------------------------------------------------------
# GDI extraction
# ---------------------------------------------------------------------------

def test_extract_gdi(tmp_path):
    ipbin = _make_ipbin("T-1209N   ", "SONIC ADVENTURE")
    # Build a minimal track file (2352-byte sectors, raw header = 16 bytes)
    # IP.BIN is at physical byte offset 16 (16-byte sector header)
    track_data = b"\x00" * 16 + ipbin  # minimal: one sector worth
    track1 = tmp_path / "track01.bin"
    track1.write_bytes(track_data)
    gdi = tmp_path / "game.gdi"
    gdi.write_text(
        "3\n"
        f"1 0 4 2352 {track1.name} 0\n"
        "2 300 0 2352 track02.raw 0\n"
        "3 45000 4 2048 track03.bin 0\n"
    )
    result = _extract_gdi(str(gdi))
    assert result is not None
    assert result["game_id"] == "T-1209N"
    assert "SONIC ADVENTURE" in result["title"]
    assert result["platform"] == "dreamcast"


def test_extract_gdi_missing_track(tmp_path):
    gdi = tmp_path / "game.gdi"
    gdi.write_text("1\n1 0 4 2352 nonexistent.bin 0\n")
    result = _extract_gdi(str(gdi))
    assert result is None


# ---------------------------------------------------------------------------
# _BinSectorStream
# ---------------------------------------------------------------------------

def test_bin_sector_stream_read():
    # Build a fake 2352-byte-sector image: first 16 bytes = raw header, then 2048 bytes data
    payload = b"A" * 2048
    raw_sector = b"\x00" * 16 + payload  # sector 0
    stream = _BinSectorStream(io.BytesIO(raw_sector * 10), sector_size=2352, header_size=16)
    stream.seek(0)
    data = stream.read(2048)
    assert data == payload


def test_bin_sector_stream_cross_sector():
    # Read across two sectors
    payload0 = b"A" * 2048
    payload1 = b"B" * 2048
    raw = b"\x00" * 16 + payload0 + b"\x00" * 16 + payload1
    stream = _BinSectorStream(io.BytesIO(raw), sector_size=2352, header_size=16)
    stream.seek(2040)  # 8 bytes before end of sector 0
    data = stream.read(16)  # 8 from sector 0 + 8 from sector 1
    assert data == b"A" * 8 + b"B" * 8


def test_bin_sector_stream_sector1_read():
    """Verify that reading starting at logical sector 1 maps to the correct physical position."""
    header = b"\x00" * 16
    padding = b"\x00" * (2352 - 16 - 2048)  # trailing ECC bytes in a 2352-byte sector
    payload0 = b"A" * 2048
    payload1 = b"B" * 2048
    raw = header + payload0 + padding + header + payload1 + padding
    assert len(raw) == 2 * 2352
    stream = _BinSectorStream(io.BytesIO(raw), sector_size=2352, header_size=16)
    # Seek to start of logical sector 1 (2048 bytes in logical space)
    stream.seek(2048)
    data = stream.read(2048)
    assert data == payload1


# ---------------------------------------------------------------------------
# _extract_from_chd_sectors: a CHD's sectors, read through chdman (#362)
# ---------------------------------------------------------------------------

@pytest.fixture(name="fake_extractraw")
def _fake_extractraw(monkeypatch):
    """chdman's runner answers ``extractraw`` from a ``_make_chd_v5`` file."""
    from services.chdman import chdman_service

    commands: list[list[str]] = []

    async def fake(cmd, **kwargs):
        commands.append(list(cmd))
        answer = serve_extractraw(cmd)
        return answer if answer is not None else (1, b"", b"not served")

    monkeypatch.setattr(chdman_service.runner, "run_capture", fake)
    return commands


async def _sectors(path):
    from services.disc_id import _extract_from_chd_sectors as extract

    return await extract(str(path), "chdman")


@pytest.mark.asyncio
async def test_extract_from_chd_sectors_ps2(tmp_path, fake_extractraw):
    iso = _make_iso({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"})
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(_make_chd_v5(iso))

    result = await _sectors(chd_path)

    assert result is not None
    assert result["game_id"] == "SLUS-20312"
    assert all("extractraw" in cmd for cmd in fake_extractraw)


@pytest.mark.asyncio
async def test_extract_from_chd_sectors_psp(tmp_path, fake_extractraw):
    sfo = _make_param_sfo({"DISC_ID": "ULES00135", "TITLE": "Patapon"})
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(_make_chd_v5(_make_iso({"PSP_GAME/PARAM.SFO": sfo})))

    result = await _sectors(chd_path)

    assert result is not None
    assert result["game_id"] == "ULES00135"
    assert result["title"] == "Patapon"


@pytest.mark.asyncio
@pytest.mark.parametrize(("unit_bytes", "header"), [
    (2352, 16), (2352, 24), (2448, 16), (2448, 24),
], ids=["2352-mode1", "2352-mode2", "2448-mode1", "2448-mode2"])
async def test_extract_from_chd_sectors_ps1_cd(
    tmp_path, fake_extractraw, unit_bytes, header,
):
    cooked = _make_iso({"SYSTEM.CNF": b"BOOT = cdrom:\\SCUS_941.63;1\n"})
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(
        _make_chd_v5(mode2_frames(cooked, unit_bytes, header), unit_bytes=unit_bytes),
    )

    result = await _sectors(chd_path)

    assert result is not None
    assert result["game_id"] == "SCUS-94163"


@pytest.mark.asyncio
async def test_extract_from_chd_sectors_cd_no_recognizable_content(
    tmp_path, fake_extractraw,
):
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(_make_chd_v5(b"\xab" * (2352 * 40), unit_bytes=2352))

    assert await _sectors(chd_path) is None


@pytest.mark.asyncio
async def test_extract_from_chd_sectors_fake_chd_returns_none(tmp_path, fake_extractraw):
    p = tmp_path / "fake.chd"
    p.write_bytes(b"not a chd" * 20)

    assert await _sectors(p) is None
    assert fake_extractraw == []      # not a CHD: chdman is never asked


@pytest.mark.asyncio
async def test_a_failed_chdman_read_is_no_disc_id(tmp_path, monkeypatch, caplog):
    import logging

    from services.chdman import chdman_service

    async def damaged(cmd, **kwargs):
        return 1, b"", b"Error reading CHD file: Decompression error"

    monkeypatch.setattr(chdman_service.runner, "run_capture", damaged)
    chd_path = tmp_path / "game.chd"
    chd_path.write_bytes(_make_chd_v5(_make_iso({"SYSTEM.CNF": b"x"})))

    with caplog.at_level(logging.WARNING):
        assert await _sectors(chd_path) is None
    assert any("game.chd" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING)


def test_dumpmeta_raw_times_out(tmp_path, monkeypatch):
    """A dumpmeta that timed out read nothing, so it is not "no tag" (#490).

    run_capture bounds the read and reports the timeout as a None returncode;
    answering None here would be cached as "no disc ID" for good.
    """
    from services.chdman import chdman_service

    from app.services.disc_id import DiscIdReadFailed

    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")

    async def timed_out(cmd, **kwargs):
        assert kwargs["timeout"] > 0
        return None, b"", b""

    monkeypatch.setattr(chdman_service.runner, "run_capture", timed_out)

    with pytest.raises(DiscIdReadFailed):
        asyncio.run(_dumpmeta_raw(str(chd), "GDRO", "chdman"))


@pytest.mark.asyncio
async def test_extract_from_chd_gdro_fallback(tmp_path):
    """No GAME tag → fall back to GDRO IP.BIN parsing."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    ipbin = _make_ipbin("MK-51034  ", "DEAD OR ALIVE")

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        if tag == "GDRO":
            return ipbin
        return None

    with (
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
    ):
        result = await extract_from_chd(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "MK-51034"
    assert result["platform"] == "dreamcast"


@pytest.mark.asyncio
async def test_extract_from_chd_companion_iso_fallback(tmp_path):
    """No tags, no GDRO → look for companion ISO → normalized serial returned."""
    # Use the canonical PS2 format with the dot (SLUS_XXX.YY)
    cnf = b"BOOT2 = cdrom0:\\SLUS_209.99;1\n"
    iso_bytes = _make_iso({"SYSTEM.CNF": cnf})
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")
    iso = tmp_path / "game.iso"
    iso.write_bytes(iso_bytes)

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    with (
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
    ):
        result = await extract_from_chd(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "SLUS-20999"


@pytest.mark.asyncio
async def test_extract_from_chd_none_when_nothing(tmp_path):
    """All strategies fail → None."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake")

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    with (
        patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin),
    ):
        result = await extract_from_chd(str(chd), "chdman")

    assert result is None


@pytest.mark.asyncio
async def test_extract_from_chd_reads_the_serial_from_chd_sectors(tmp_path, fake_extractraw):
    """No GDRO, no companion: SYSTEM.CNF read from the CHD's own sectors."""
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\n"
    chd = tmp_path / "game.chd"
    chd.write_bytes(_make_chd_v5(_make_iso({"SYSTEM.CNF": cnf})))

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    with patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin):
        result = await extract_from_chd(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "SLUS-20312"


@pytest.mark.asyncio
async def test_extract_from_chd_psp_companion_keeps_its_title(tmp_path):
    """A PSP companion ISO gives the serial and the PARAM.SFO title."""
    sfo = _make_param_sfo({"DISC_ID": "ULES00135", "TITLE": "Patapon"})
    chd = tmp_path / "patapon.chd"
    chd.write_bytes(b"fake")
    (tmp_path / "patapon.iso").write_bytes(_make_iso({"PSP_GAME/PARAM.SFO": sfo}))

    async def fake_dumpmeta_bin(chd_path, tag, chdman_path):
        return None

    with patch("app.services.disc_id._dumpmeta_bin", side_effect=fake_dumpmeta_bin):
        result = await extract_from_chd(str(chd), "chdman")

    assert result is not None
    assert result["game_id"] == "ULES00135"
    assert result["title"] == "Patapon"
