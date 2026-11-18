"""
Disc ID / Title extractor for game disc images.

Supports extracting game serial numbers and titles from:
  - CHD files: reads disc sectors directly via a minimal CHD v5 reader
    (the same mechanism as libchdr used by PCSX2 / AetherSX2 / NetherSX2)
  - ISO 9660 source files (.iso) used for PS2, PSP, and PS1
  - Dreamcast GDI images (.gdi + track files)
  - CHD files via chdman dumpmeta (the standard Dreamcast GDRO IP.BIN tag)

Serial normalization matches PCSX2's ExecutablePathToSerial:
  SYSTEM.CNF "BOOT2 = cdrom0:\\SLUS_203.12;1"  →  "SLUS-20312"
Emulator frontends and game database scrapers key on this normalized form.

Extraction strategy for CHDs (in priority order):
  1. Read disc sectors directly from the CHD (via a minimal CHD v5 reader):
       DVD CHDs (unit_bytes=2048): plain 2048-byte ISO sectors.
       CD CHDs (unit_bytes=2352/2448): Mode 1 and Mode 2 Form 1 framing are
       both probed automatically, so PS1 BIN/CUE-sourced CHDs are supported.
  2. For CD CHDs: parse the Dreamcast GDRO (IP.BIN) metadata standard tag.
  3. Look for a companion source file (.iso / .gdi / .cue / .bin) next to the CHD.
  4. Return None if nothing is found.

Source-file extraction:
  - PS2 / PS1 .iso: ISO 9660 → root/SYSTEM.CNF → BOOT2= / BOOT= line.
  - PSP .iso:       ISO 9660 → /PSP_GAME/PARAM.SFO → DISC_ID + TITLE keys.
  - Dreamcast .gdi: reads Track01.bin IP.BIN header (product number + title).

Nothing here writes to a CHD (#352). chdman opens a compressed CHD read-only
(MAME ``chd.cpp``: ``m_allow_writes = !compressed()``), so ``chdman addmeta``
fails on every CHD this app makes. The disc ID a conversion reads from its
Source is recorded in the app's database (``chd_metadata_store``) instead, and
the library scan stores what ``extract_from_chd`` finds the same way.
"""

from __future__ import annotations

import asyncio
import logging
from logging_setup import get_logger
import os
import re
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from services.subprocess_runner import (
    DeadlineExpired,
    ProbeCapacityExceeded,
    ReadCancelled,
    StorageAbandoned,
    abandonment_checkpoint,
    ioprio_prefix,
    nice_prefix,
    run_detached_bounded,
)
from utils.sheet_text import cue_tracks, gdi_tracks, sheet_readings, sheet_tracks

logger = get_logger("disc_id")

# Resource limits for CHD metadata/disc-ID probing. CHD files live in user
# volumes, so treat header/map fields and chdman output as untrusted input.
_MAX_DUMPMETA_BYTES = 1 * 1024 * 1024


class DiscIdStorageAbandoned(StorageAbandoned):
    """A chdman child for this CHD outlived SIGKILL and is still running.

    Distinct from "no tag found" because the two demand opposite behaviour: the
    latter invites a write, the former forbids touching the file at all. Raised
    rather than folded into the ``None`` these helpers otherwise return, since
    every caller acts on that ``None`` immediately -- before any enclosing
    ``collect_abandonment()`` sink is inspected (issue #268).
    """


def _abort_if_abandoned(abandoned: list, action: str, chd_path: str) -> None:
    """Raise if ``action`` on ``chd_path`` left a chdman child running.

    Called by the three capture helpers rather than by their callers, because
    every one of them reduces the outcome to a bare ``None``/``False`` that means
    "no such tag" or "nothing to delete" -- and there are a dozen call sites, each
    of which answers that by reading or writing the same CHD again. Guarding the
    callers one at a time is how you miss one; the ambiguity has to be impossible
    at the point the value is produced (issue #268).
    """
    if abandoned:
        raise DiscIdStorageAbandoned(
            f"{action} on {chd_path} left {', '.join(abandoned)} stuck on "
            "unresponsive storage; it is still running, so touching the file "
            "again would race it"
        )


_DUMPMETA_TIMEOUT_SECONDS = 15
# The bound on one blocking disc-ID read of a CHD or its companion file
# (``bounded_disc_read``). The reads are small (a few hunks, a header), so a
# Volume that takes this long is not answering.
_DISC_READ_TIMEOUT_SECONDS = 30.0


def _chdman_runner():
    """The chdman service's ``SubprocessRunner``, imported lazily.

    These helpers shell out to chdman, so they belong on that tool's runner:
    one PID set ``active_pids()`` fully describes, one nice/ionice policy, and
    one bounded teardown instead of three hand-rolled spawns whose waits after
    ``kill()`` were unbounded -- ``dumpmeta`` in particular could hang the
    library scan's Phase 2 outright on unresponsive storage. Going through
    ``run_capture`` also reports an unkillable child into any open
    ``collect_abandonment()`` sink, which is what lets the scan stop instead of
    stranding one per file (issue #268). The import is deferred to keep this
    module free of a load-order dependency on the service singletons.
    """
    from services.chdman import chdman_service
    return chdman_service.runner


def _chdman_cmd(chdman_path: str, *args: str) -> list[str]:
    """chdman argv with the tool priority policy applied as command wrappers.

    Wrappers, never ``run_capture``'s ``preexec_fn``: this process is
    multithreaded, and forking a Python callable from a multithreaded parent can
    deadlock the child before it ``exec()``s -- and a child wedged there never
    returns from ``create_subprocess_exec``, so the bound these calls exist to
    get would never be applied. ``nice``/``ionice`` only exec. The hand-rolled
    spawns this replaces used no ``preexec_fn`` either.
    """
    owner = _chdman_runner().owner
    return nice_prefix(owner) + ioprio_prefix(owner) + [chdman_path, *args]

# ---------------------------------------------------------------------------
# ISO 9660 constants
# ---------------------------------------------------------------------------
_SECTOR_SIZE = 2048
_PVD_SECTOR = 16
_PVD_MAGIC = b"\x01CD001\x01"

# Raw CD sector header sizes (bytes prepended before user-data in .bin files)
_RAW_SECTOR_HEADER_SIZE_MODE1 = 16   # Mode 1 / CD-ROM XA sector header
_RAW_SECTOR_HEADER_SIZE_MODE2 = 24   # Mode 2 Form 1 sector header

# ---------------------------------------------------------------------------
# Regex patterns for SYSTEM.CNF parsing
# ---------------------------------------------------------------------------
_BOOT2_RE = re.compile(
    r"BOOT2\s*=\s*cdrom0?[:\\\/]+([A-Z0-9_.]+)",
    re.IGNORECASE,
)
_BOOT_RE = re.compile(
    r"BOOT\s*=\s*cdrom[:\\\/]+([A-Z0-9_.]+)",
    re.IGNORECASE,
)

# PS1/PS2 disc serial pattern: 4 letters + _ or - + 3 digits + . + 2 digits
# Matches the format validated by PCSX2's ExecutablePathToSerial.
_PS_SERIAL_RE = re.compile(r"^[A-Z]{4}[_-][0-9]{3}\.[0-9]{2}", re.ASCII)

# ---------------------------------------------------------------------------
# SFO (PARAM.SFO) constants
# ---------------------------------------------------------------------------
_SFO_MAGIC = b"\x00PSF"
_SFO_FMT_UTF8 = 0x0204

# ---------------------------------------------------------------------------
# Dreamcast IP.BIN offsets (all values ASCII, null/space-padded)
# Based on the official Dreamcast Bootstrap Specification:
#   Hardware ID   : 0x00 – 0x0F  (16 bytes, "SEGA SEGAKATANA ")
#   Maker ID      : 0x10 – 0x1F  (16 bytes, "SEGA ENTERPRISES")
#   Product Number: 0x40 – 0x49  (10 bytes, e.g. "MK-51034  ")
#   Version       : 0x4A – 0x4F  ( 6 bytes, e.g. "V1.001")
#   Release Date  : 0x50 – 0x57  ( 8 bytes, "20011115")
#   Publisher     : 0x60 – 0x6F  (16 bytes)
#   Game Title    : 0x80 – 0xFF  (128 bytes)
# ---------------------------------------------------------------------------
_DC_HWID_OFF = 0x00
_DC_HWID_LEN = 16
_DC_PROD_OFF = 0x40
_DC_PROD_LEN = 10
_DC_VER_OFF = 0x4A
_DC_VER_LEN = 6
_DC_TITLE_OFF = 0x80
_DC_TITLE_LEN = 128
_DC_HWID_NEEDLE = b"SEGA SEGAKATANA"


# ===========================================================================
# Low-level ISO 9660 helpers
# ===========================================================================

# Every length these helpers read comes out of the image (the PVD's root
# directory size, a directory record's extent size), so every read is bounded
# before it happens: by this parser limit, and by what the image actually holds
# past the offset. A directory record table is a few KiB on a real disc and the
# files read through here (SYSTEM.CNF, PARAM.SFO) are smaller still, so 1 MiB
# leaves wide headroom. A size outside either bound is a parse failure, which
# callers already treat as "no header": a planted image claiming 0xffffffff
# bytes costs nothing, instead of a 4 GiB allocation.
_MAX_ISO_READ = 1024 * 1024


def _stream_size(f) -> int | None:
    """Logical size of an ISO 9660 stream in bytes, when it can be known."""
    size = getattr(f, "logical_size", None)
    if size is not None:
        return size
    try:
        return os.fstat(f.fileno()).st_size
    except (AttributeError, OSError, ValueError):
        return None


def _bounded_read(f, offset: int, size: int, *, strict: bool = False) -> bytes | None:
    """``size`` bytes at ``offset``, or None when the image cannot hold them.

    Refuses a size above ``_MAX_ISO_READ`` or running past the end of the
    image, without reading anything. ``strict`` raises an I/O error instead of
    answering None (#306: a verify must tell "cannot read" from "not there").
    """
    if size < 0 or size > _MAX_ISO_READ:
        return None
    total = _stream_size(f)
    if total is not None and offset + size > total:
        return None
    try:
        f.seek(offset)
        return f.read(size)
    except OSError:
        if strict:
            raise
        return None


def _read_pvd(f, *, strict: bool = False) -> bytes | None:
    """Return the 2048-byte PVD sector from a seekable ISO 9660 stream, or None.

    ``strict`` raises an I/O error instead of answering None.
    """
    try:
        f.seek(_PVD_SECTOR * _SECTOR_SIZE)
        pvd = f.read(_SECTOR_SIZE)
    except OSError:
        if strict:
            raise
        return None
    if len(pvd) < 190 or pvd[:7] != _PVD_MAGIC:
        return None
    return pvd


def _pvd_root_dir(pvd: bytes) -> tuple[int, int]:
    """Return (lba, size) of the root directory from a PVD."""
    root = pvd[156:190]
    lba = struct.unpack_from("<I", root, 2)[0]
    size = struct.unpack_from("<I", root, 10)[0]
    return lba, size


def _list_dir(
    f, lba: int, size: int, *, strict: bool = False,
) -> list[tuple[str, int, int, bool]]:
    """
    Yield (name, lba, size, is_dir) for each entry in an ISO 9660 directory.
    Skips the '.' and '..' self-referencing entries. ``strict`` raises an I/O
    error instead of answering an empty listing.
    """
    data = _bounded_read(f, lba * _SECTOR_SIZE, size, strict=strict)
    if data is None:
        return []

    entries: list[tuple[str, int, int, bool]] = []
    pos = 0
    while pos < len(data):
        rec_len = data[pos]
        if rec_len == 0:
            # Advance to next sector boundary
            next_pos = ((pos // _SECTOR_SIZE) + 1) * _SECTOR_SIZE
            if next_pos >= len(data):
                break
            pos = next_pos
            continue
        if pos + rec_len > len(data):
            break

        rec = data[pos : pos + rec_len]
        if len(rec) < 34:
            pos += rec_len
            continue

        flags = rec[25]
        name_len = rec[32]
        if 33 + name_len > len(rec):
            pos += rec_len
            continue

        name_raw = rec[33 : 33 + name_len]
        if name_raw not in (b"\x00", b"\x01"):
            try:
                name = name_raw.decode("ascii", errors="replace").upper()
                if ";" in name:
                    name = name[: name.index(";")]
                entry_lba = struct.unpack_from("<I", rec, 2)[0]
                entry_size = struct.unpack_from("<I", rec, 10)[0]
                is_dir = bool(flags & 0x02)
                entries.append((name, entry_lba, entry_size, is_dir))
            except Exception:
                logger.debug(
                    "Skipping malformed ISO 9660 directory entry at offset %d",
                    pos,
                    exc_info=True,
                )

        pos += rec_len
    return entries


def read_iso_file(
    iso_path: str, path_parts: list[str], *, strict: bool = False,
) -> Optional[bytes]:
    """Read one file's bytes from an ISO 9660 image by path, or ``None``.

    The shared, format-only ISO 9660 path lookup: open the image, walk the PVD
    root directory to ``path_parts`` (e.g. ``["PS3_GAME", "PARAM.SFO"]``), and
    return the file bytes. Callers that need a specific embedded file from a
    built ISO (PS3 ``TITLE_ID`` readback in ``services.ps3``) reuse this rather
    than re-implementing the ISO 9660 walk, per the repo's "modularity is key"
    rule. Returns ``None`` for a non-ISO / unreadable image or a missing path.
    ``strict`` raises an I/O error instead, for a caller that has to tell "the
    image does not hold it" from "the image could not be read" (a verify).
    """
    try:
        with open(iso_path, "rb") as f:
            pvd = _read_pvd(f, strict=strict)
            if not pvd:
                return None
            root_lba, root_size = _pvd_root_dir(pvd)
            return _find_file(f, root_lba, root_size, path_parts, strict=strict)
    except OSError:
        if strict:
            raise
        return None


def _find_file(
    f, root_lba: int, root_size: int, path_parts: list[str], *, strict: bool = False,
) -> Optional[bytes]:
    """
    Walk an ISO 9660 directory tree and return the raw bytes of the file at
    *path_parts* (e.g. ["PSP_GAME", "PARAM.SFO"]), or None if not found.
    """
    cur_lba, cur_size = root_lba, root_size
    for i, part in enumerate(path_parts):
        entries = _list_dir(f, cur_lba, cur_size, strict=strict)
        hit = next((e for e in entries if e[0] == part.upper()), None)
        if hit is None:
            return None
        _name, lba, size, is_dir = hit
        if i == len(path_parts) - 1:
            # Final component, read and return the file (bounded, see
            # _MAX_ISO_READ: None when its recorded size is implausible).
            return _bounded_read(f, lba * _SECTOR_SIZE, size, strict=strict)
        if not is_dir:
            return None
        cur_lba, cur_size = lba, size
    return None


# ===========================================================================
# Format-specific parsers
# ===========================================================================

def _normalize_ps_serial(raw: str) -> Optional[str]:
    """
    Normalize a PlayStation disc serial to the canonical form used by PCSX2,
    AetherSX2, and NetherSX2.

    Mirrors PCSX2's ``ExecutablePathToSerial`` function:
      1. Strip the path prefix up to and including the last \\ (or : if no \\).
      2. Strip the ;N version suffix.
      3. Validate against the PS1/PS2 serial pattern:
         4 letters + (_ or -) + 3 digits + . + 2 digits.
      4. Remove the dot, replace _ with -.

    Examples::
      cdrom0:\\SLUS_203.12;1  →  SLUS-20312
      SCES_503.08             →  SCES-50308
      SLPS_123.45;2           →  SLPS-12345
      SLUS_20312              →  None  (no dot, non-standard, rejected)
    """
    s = raw.strip().upper()
    # Strip path prefix (everything up to and including the last \\ or :)
    bslash = s.rfind("\\")
    if bslash >= 0:
        s = s[bslash + 1:]
    else:
        colon = s.rfind(":")
        if colon >= 0:
            s = s[colon + 1:]
    # Strip ;N version suffix
    semi = s.find(";")
    if semi >= 0:
        s = s[:semi]
    s = s.strip()
    # Validate: must match the PS1/PS2 serial format ????_???.??
    if not _PS_SERIAL_RE.match(s):
        return None
    # Remove the dot, replace _ with -
    return s.replace(".", "").replace("_", "-")


def _parse_system_cnf(data: bytes) -> dict:
    """Parse SYSTEM.CNF and return a dict with game_id and platform."""
    try:
        text = data.decode("ascii", errors="replace")
    except Exception:
        return {}
    # PS2
    m = _BOOT2_RE.search(text)
    if m:
        normalized = _normalize_ps_serial(m.group(1))
        if normalized:
            return {"game_id": normalized, "platform": "ps2"}
    # PS1
    m = _BOOT_RE.search(text)
    if m:
        normalized = _normalize_ps_serial(m.group(1))
        if normalized:
            return {"game_id": normalized, "platform": "ps1"}
    return {}


def parse_sfo_keys(data: bytes) -> dict[str, str]:
    """Return every UTF-8 string key of a PSF (PARAM.SFO) blob.

    The shared, format-only PARAM.SFO table reader. Both the PSP/CHD tagging
    path (``_parse_param_sfo`` below) and PS3 folder detection
    (``services.ps3``) build on this, so the binary table parsing lives in one
    place (per the repo's "modularity is key" rule) rather than being
    copy-pasted per platform.

    SFO format (little-endian):
      0x00  4   magic "\\x00PSF"
      0x08  4   key_table_offset
      0x0C  4   data_table_offset
      0x10  4   num_entries
    Then num_entries x 16-byte index entries:
      0x00  2   key_offset (from key_table_offset)
      0x02  2   data_format  (0x0204 = utf8 string)
      0x04  4   data_len
      0x0C  4   data_offset (from data_table_offset)
    """
    if len(data) < 20 or data[:4] != _SFO_MAGIC:
        return {}
    try:
        key_table_off = struct.unpack_from("<I", data, 8)[0]
        data_table_off = struct.unpack_from("<I", data, 12)[0]
        num_entries = struct.unpack_from("<I", data, 16)[0]
    except struct.error:
        return {}

    out: dict[str, str] = {}
    for i in range(num_entries):
        entry_off = 20 + i * 16
        if entry_off + 16 > len(data):
            break
        key_off = struct.unpack_from("<H", data, entry_off)[0]
        fmt = struct.unpack_from("<H", data, entry_off + 2)[0]
        data_len = struct.unpack_from("<I", data, entry_off + 4)[0]
        val_off = struct.unpack_from("<I", data, entry_off + 12)[0]

        key_start = key_table_off + key_off
        key_end = data.find(b"\x00", key_start)
        if key_end < 0:
            continue
        key = data[key_start:key_end].decode("ascii", errors="replace")
        if fmt != _SFO_FMT_UTF8:
            continue
        val_start = data_table_off + val_off
        val_end = val_start + data_len
        if val_end > len(data):
            continue
        out[key] = data[val_start:val_end].rstrip(b"\x00").decode(
            "utf-8", errors="replace",
        )
    return out


def _parse_param_sfo(data: bytes) -> dict:
    """Parse a PSP PARAM.SFO blob into game_id, title, platform."""
    keys = parse_sfo_keys(data)
    result: dict = {}
    if "DISC_ID" in keys:
        result["game_id"] = keys["DISC_ID"]
    if "TITLE" in keys:
        result["title"] = keys["TITLE"]
    if result:
        result["platform"] = "psp"
    return result


def _parse_ipbin(data: bytes) -> dict:
    """
    Parse a Dreamcast IP.BIN binary (from the GDRO CHD metadata tag or from
    the first track of a GDI image) and return game_id, title, platform.
    """
    if len(data) < _DC_PROD_OFF + _DC_PROD_LEN:
        return {}
    try:
        hwid = data[_DC_HWID_OFF : _DC_HWID_OFF + _DC_HWID_LEN]
        if _DC_HWID_NEEDLE not in hwid:
            return {}
        prod_raw = data[_DC_PROD_OFF : _DC_PROD_OFF + _DC_PROD_LEN]
        game_id = prod_raw.decode("ascii", errors="replace").strip()

        title = ""
        if len(data) >= _DC_TITLE_OFF + _DC_TITLE_LEN:
            title_raw = data[_DC_TITLE_OFF : _DC_TITLE_OFF + _DC_TITLE_LEN]
            title = title_raw.decode("ascii", errors="replace").strip()

        result: dict = {}
        if game_id:
            result["game_id"] = game_id
        if title:
            result["title"] = title
        if result:
            result["platform"] = "dreamcast"
        return result
    except Exception:
        return {}


# ===========================================================================
# Platform headers: which console a disc belongs to, from a few bytes
# ===========================================================================
#
# These answer "whose disc is this" and nothing else. The names they return are
# this module's own vocabulary (the same one ``_parse_system_cnf`` already uses:
# "ps1", "ps2", "psp", "dreamcast"); ``services.platform_detect`` maps them onto
# Platform slugs.

# GameCube / Wii disc header magic numbers (big-endian u32). The same values
# nkit2iso reads, because an NKit image keeps the original disc header.
GAMECUBE_MAGIC = 0xC2339F3D   # at 0x1C
WII_MAGIC = 0x5D1C9EA3        # at 0x18

# Sega sector-0 system ids, at the start of the first data sector.
_SEGA_SECTOR0_IDS = (
    (b"SEGA SEGASATURN", "saturn"),
    (b"SEGADISCSYSTEM", "segacd"),
    (_DC_HWID_NEEDLE, "dreamcast"),
)
# Where sector 0's user data starts: a cooked 2048-byte image, a raw Mode 1
# track (16-byte sync + header), a raw Mode 2 Form 1 track (24 bytes).
_SECTOR0_OFFSETS = (0, _RAW_SECTOR_HEADER_SIZE_MODE1, _RAW_SECTOR_HEADER_SIZE_MODE2)


def nintendo_disc_platform(path: str) -> str | None:
    """``"gamecube"`` or ``"wii"`` from a raw GC/Wii disc header, else None."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x20)
    except OSError:
        return None
    if len(head) < 0x20:
        return None
    if struct.unpack_from(">I", head, 0x18)[0] == WII_MAGIC:
        return "wii"
    if struct.unpack_from(">I", head, 0x1C)[0] == GAMECUBE_MAGIC:
        return "gamecube"
    return None


def _cue_track_paths(path: str, within=None) -> list[Path]:
    """Every track file a cue sheet names, resolved beside the sheet.

    Decoded the shared way (``utils.sheet_text``: UTF-8 with a BOM stripped,
    cp1252 when not UTF-8, Latin-1 last); the first spelling that exists on
    disk wins, and a track none of whose spellings exists keeps the first.
    ``within`` names the trusted roots (``utils.sheet_text.resolve_track``);
    None means the configured Volumes.
    """
    return sheet_tracks(path, cue_tracks, within=within) or []


def _cue_data_file(path: str, within=None) -> str | None:
    """The first ``FILE`` a cue sheet names, when it exists beside the sheet."""
    tracks = _cue_track_paths(path, within)
    if not tracks:
        return None
    return str(tracks[0]) if tracks[0].is_file() else None


def sega_sector0_platform(path: str, *, within=None) -> str | None:
    """Saturn, Sega CD or Dreamcast from the system id at the start of sector 0.

    Reads one sector. A ``.cue`` is followed to its first data file, resolved
    within *within* (the configured Volumes when None).
    """
    if Path(path).suffix.lower() == ".cue":
        resolved = _cue_data_file(path, within)
        if resolved is None:
            return None
        path = resolved
    try:
        with open(path, "rb") as f:
            head = f.read(_RAW_SECTOR_HEADER_SIZE_MODE2 + 0x20)
    except OSError:
        return None
    for offset in _SECTOR0_OFFSETS:
        window = head[offset:offset + 0x10]
        for needle, platform in _SEGA_SECTOR0_IDS:
            if window.startswith(needle):
                return platform
    return None


def disc_platform(path: str, *, within=None) -> str | None:
    """The console of a CD/DVD source image, from its own header, or None.

    PS1 / PS2 through ``SYSTEM.CNF``, PSP through ``PARAM.SFO`` and Dreamcast
    through a ``.gdi``'s ``IP.BIN`` (all the existing extractor), then the Sega
    sector-0 ids (Saturn, Sega CD, a Dreamcast track read directly).
    *within*: the trusted roots a sheet's tracks resolve under (an Inbox's
    unpack folder, #432); None, the default, is the configured Volumes.
    """
    found = extract_from_source(path, within=within)
    if found and found.get("platform"):
        return found["platform"]
    return sega_sector0_platform(path, within=within)


# ===========================================================================
# Public synchronous extractor: source files
# ===========================================================================

def extract_from_source(path: str, *, within=None) -> Optional[dict]:
    """
    Extract game ID (and optionally title) from a source disc image file.

    Supported formats:
      .iso  → PS2 (SYSTEM.CNF/BOOT2), PS1 (SYSTEM.CNF/BOOT),
               PSP (PSP_GAME/PARAM.SFO)
      .gdi  → Dreamcast (reads the first data track for IP.BIN)
      .bin  → Tries ISO 9660 with 2352-byte sector layout (Mode 2 / raw)
      .cue  → Follows the DATA track .bin reference

    Returns a dict with at least ``game_id`` and ``platform`` keys, or None.

    ``within`` is passed to the track-sheet readers: the roots a .cue/.gdi's
    tracks must resolve under. None (the default) means the configured
    Volumes; the job worker passes the archive extraction dir it created.
    """
    ext = Path(path).suffix.lower()
    if ext == ".iso":
        return _extract_iso(path)
    if ext == ".gdi":
        return _extract_gdi(path, within=within)
    if ext == ".cue":
        return _extract_cue(path, within=within)
    if ext == ".bin":
        return _extract_bin(path)
    return None


def _extract_iso(path: str) -> Optional[dict]:
    """ISO 9660 extraction (2048-byte sectors, PS2, PSP, PS1 DVD-based)."""
    try:
        with open(path, "rb") as f:
            pvd = _read_pvd(f)
            if not pvd:
                return None
            root_lba, root_size = _pvd_root_dir(pvd)

            # PS2 / PS1, SYSTEM.CNF in root
            cnf = _find_file(f, root_lba, root_size, ["SYSTEM.CNF"])
            if cnf:
                result = _parse_system_cnf(cnf)
                if result.get("game_id"):
                    return result

            # PSP, /PSP_GAME/PARAM.SFO
            sfo = _find_file(f, root_lba, root_size, ["PSP_GAME", "PARAM.SFO"])
            if sfo:
                result = _parse_param_sfo(sfo)
                if result.get("game_id"):
                    return result

    except Exception as e:
        logger.debug("disc_id: ISO extraction failed for %s: %s", path, e)
    return None


def _extract_bin(path: str) -> Optional[dict]:
    """
    Try to extract from a raw BIN file.
    Dreamcast / PS1 discs use 2352-byte sectors (Mode 1/2).  The user-data
    for a Mode 1 sector starts at byte 16; for Mode 2 Form 1 at byte 24.
    We try the ISO 9660 PVD by probing both offsets.
    """
    try:
        with open(path, "rb") as f:
            # Try Mode 2 XA (24-byte header) then Mode 1 (16-byte header)
            for header_size in (_RAW_SECTOR_HEADER_SIZE_MODE2, _RAW_SECTOR_HEADER_SIZE_MODE1):
                sector_size = 2352
                pvd_offset = _PVD_SECTOR * sector_size + header_size
                try:
                    f.seek(pvd_offset)
                    pvd_candidate = f.read(_SECTOR_SIZE)
                except OSError:
                    continue
                if len(pvd_candidate) >= 7 and pvd_candidate[:7] == _PVD_MAGIC:
                    # Wrap as a virtual seekable stream that maps logical sectors
                    # to physical sectors with the given header size.
                    stream = _BinSectorStream(f, sector_size, header_size)
                    pvd = _read_pvd(stream)
                    if pvd:
                        root_lba, root_size = _pvd_root_dir(pvd)
                        cnf = _find_file(stream, root_lba, root_size, ["SYSTEM.CNF"])
                        if cnf:
                            result = _parse_system_cnf(cnf)
                            if result.get("game_id"):
                                return result
    except Exception as e:
        logger.debug("disc_id: BIN extraction failed for %s: %s", path, e)
    return None


class _BinSectorStream:
    """
    Adapts a raw-sector BIN file (2352-byte physical sectors) to look like a
    2048-byte logical ISO 9660 stream so the standard _read_pvd / _find_file
    helpers can operate on it transparently.
    """

    def __init__(self, f, sector_size: int = 2352, header_size: int = 24):
        self._f = f
        self._sector_size = sector_size
        self._header_size = header_size
        self._pos = 0  # logical position

    @property
    def logical_size(self) -> int | None:
        """Bytes of 2048-byte user data the raw track holds (for read bounds)."""
        try:
            physical = os.fstat(self._f.fileno()).st_size
        except (AttributeError, OSError, ValueError):
            return None
        return (physical // self._sector_size) * _SECTOR_SIZE

    def seek(self, pos: int) -> None:
        self._pos = pos

    def read(self, size: int) -> bytes:
        result = bytearray()
        remaining = size
        pos = self._pos
        while remaining > 0:
            logical_sector = pos // _SECTOR_SIZE
            offset_in_sector = pos % _SECTOR_SIZE
            physical_offset = (
                logical_sector * self._sector_size
                + self._header_size
                + offset_in_sector
            )
            chunk = min(remaining, _SECTOR_SIZE - offset_in_sector)
            try:
                self._f.seek(physical_offset)
                data = self._f.read(chunk)
            except OSError:
                break
            if not data:
                break
            result.extend(data)
            pos += len(data)
            remaining -= len(data)
        self._pos = pos
        return bytes(result)


# ===========================================================================
# CHD disc sectors, read through chdman (#362)
# ===========================================================================
# A compressed CHD's map and hunks (lzma, zlib, huffman, flac, and the CD codecs
# cdlz/cdzl/cdfl) are chdman's business, not this module's: the few sectors the
# ISO 9660 walk needs are read with `chdman extractraw` over a byte range of
# the CHD's logical image. Only the v5 header is parsed here, with MAME's
# layout (src/lib/util/chd.h), for the unit size and the logical size.

_CHD_MAGIC = b"MComprHD"
_CHD_V5_HEADER_BYTES = 124
_CHD_UNIT_DVD = 2048
_CHD_UNITS_CD = (2352, 2448)   # sector, or sector + 96 bytes of subcode
# One chdman read covers at most what one ISO walk read may ask for
# (``_MAX_ISO_READ`` of user data), and a walk is a handful of reads: the
# volume descriptor, the root directory, a subdirectory, the file.
_MAX_CHD_READ_UNITS = _MAX_ISO_READ // _SECTOR_SIZE
_MAX_CHD_READS = 8


@dataclass(frozen=True)
class ChdHeader:
    """The CHD v5 header fields the disc-ID reader uses (MAME's layout)."""

    version: int
    compressors: tuple[str, str, str, str]
    logical_bytes: int
    map_offset: int
    meta_offset: int
    hunk_bytes: int
    unit_bytes: int


def parse_chd_header(raw: bytes) -> Optional[ChdHeader]:
    """The CHD v5 header in *raw*, or None when it is not one.

    MAME's v5 layout, big-endian: magic at 0, header length 8, version 12,
    compressors[4] 16, logical bytes 32, map offset 40, meta offset 48, hunk
    bytes 56, unit bytes 60, raw SHA1 64, SHA1 84, parent SHA1 104. The map
    of a compressed CHD is itself compressed; nothing here reads it.
    """
    if len(raw) < _CHD_V5_HEADER_BYTES or raw[:8] != _CHD_MAGIC:
        return None
    length, version = struct.unpack_from(">II", raw, 8)
    if version != 5 or length < _CHD_V5_HEADER_BYTES:
        return None
    compressors = tuple(
        raw[16 + 4 * i:20 + 4 * i].rstrip(b"\0").decode("ascii", "replace")
        for i in range(4)
    )
    logical_bytes, map_offset, meta_offset = struct.unpack_from(">QQQ", raw, 32)
    hunk_bytes, unit_bytes = struct.unpack_from(">II", raw, 56)
    return ChdHeader(
        version, compressors, logical_bytes, map_offset, meta_offset,
        hunk_bytes, unit_bytes,
    )


def read_chd_header(path: str) -> Optional[ChdHeader]:
    """The v5 header of the CHD at *path* (blocking: read it through
    ``bounded_disc_read``)."""
    with open(path, "rb") as f:
        return parse_chd_header(f.read(_CHD_V5_HEADER_BYTES))


class _NeedSectors(Exception):
    """The ISO walk asked for logical sectors that are not fetched yet."""

    def __init__(self, first: int, count: int) -> None:
        super().__init__(f"sectors {first}..{first + count - 1}")
        self.first = first
        self.count = count


class _ChdSectorCache:
    """A seekable view of a CHD's ISO 9660 data, filled by chdman reads.

    ``units`` holds raw CHD units by logical sector number (shared between the
    Mode 2 and Mode 1 attempts on a CD); a sector's 2048 bytes of user data
    start at ``data_offset`` in its unit (0 for a DVD, 24 for Mode 2 Form 1,
    16 for Mode 1). A read touching a sector not fetched yet raises
    ``_NeedSectors`` for the run, and the caller fetches it and walks again.
    ``logical_size`` lets ``_bounded_read`` refuse a read past the image.
    """

    def __init__(self, units: dict[int, bytes], sectors: int, data_offset: int) -> None:
        self._units = units
        self._sectors = sectors
        self._data_offset = data_offset
        self._pos = 0
        self.logical_size = sectors * _SECTOR_SIZE

    def seek(self, pos: int) -> None:
        self._pos = pos

    def read(self, size: int) -> bytes:
        if size <= 0:
            return b""
        first = self._pos // _SECTOR_SIZE
        last = min((self._pos + size - 1) // _SECTOR_SIZE, self._sectors - 1)
        if first > last:
            return b""
        missing = [n for n in range(first, last + 1) if n not in self._units]
        if missing:
            raise _NeedSectors(missing[0], missing[-1] - missing[0] + 1)
        data = b"".join(
            self._units[n][self._data_offset:self._data_offset + _SECTOR_SIZE]
            for n in range(first, last + 1)
        )
        start = self._pos - first * _SECTOR_SIZE
        out = data[start:start + size]
        self._pos += len(out)
        return out


def _identify_iso(stream) -> Optional[dict]:
    """The disc ID in an ISO 9660 stream: SYSTEM.CNF (PS1/PS2), then
    PSP_GAME/PARAM.SFO (PSP). None when there is no volume descriptor or no
    disc ID."""
    pvd = _read_pvd(stream)
    if not pvd:
        return None
    root_lba, root_size = _pvd_root_dir(pvd)
    cnf = _find_file(stream, root_lba, root_size, ["SYSTEM.CNF"])
    if cnf:
        result = _parse_system_cnf(cnf)
        if result.get("game_id"):
            return result
    sfo = _find_file(stream, root_lba, root_size, ["PSP_GAME", "PARAM.SFO"])
    if sfo:
        result = _parse_param_sfo(sfo)
        if result.get("game_id"):
            return result
    return None


async def _read_chd_units(
    chd_path: str,
    chdman_path: str,
    first: int,
    count: int,
    unit_bytes: int,
    cancel_event: asyncio.Event | None,
) -> Optional[bytes]:
    """*count* raw units of the CHD from unit *first*, via ``chdman extractraw``.

    Through chdman's shared runner, bounded by ``_DISC_READ_TIMEOUT_SECONDS``
    and raced against *cancel_event*, like the GDRO ``dumpmeta`` read: a child
    that outlived SIGKILL raises ``DiscIdStorageAbandoned`` (into the open
    abandonment sink), a timed-out one raises it too (the storage is not
    answering), a cancel raises ``ReadCancelled``. A chdman failure (a damaged
    CHD) is a warning and None.
    """
    name = os.path.basename(chd_path)
    with tempfile.TemporaryDirectory(prefix="cmptr-discid-") as tmp:
        out = os.path.join(tmp, "units.bin")
        with abandonment_checkpoint() as abandoned:
            returncode, _, stderr = await _chdman_runner().run_capture(
                _chdman_cmd(
                    chdman_path, "extractraw", "-f", "-i", chd_path, "-o", out,
                    "-isb", str(first * unit_bytes), "-ib", str(count * unit_bytes),
                ),
                timeout=_DISC_READ_TIMEOUT_SECONDS,
                cancel_event=cancel_event,
                nice_via_wrapper=True,
            )
        _abort_if_abandoned(abandoned, "chdman extractraw", chd_path)
        if returncode is None:
            if cancel_event is not None and cancel_event.is_set():
                raise ReadCancelled("Read cancelled")
            logger.warning(
                "disc_id: reading %s with chdman did not answer in %gs; its disc ID "
                "is skipped", chd_path, _DISC_READ_TIMEOUT_SECONDS,
            )
            raise DiscIdStorageAbandoned(
                f"reading {name} did not answer in {_DISC_READ_TIMEOUT_SECONDS:g}s; "
                "the storage is not responding"
            )
        if returncode != 0:
            logger.warning(
                "disc_id: chdman extractraw failed for %s (rc=%s): %s",
                chd_path, returncode, stderr.decode(errors="replace").strip(),
            )
            return None
        with open(out, "rb") as f:
            return f.read(count * unit_bytes)


async def _extract_from_chd_sectors(
    chd_path: str,
    chdman_path: str,
    *,
    cancel_event: asyncio.Event | None = None,
) -> Optional[dict]:
    """The disc ID from a CHD's own sectors, like libchdr-based emulators.

    Reads the v5 header (bounded) for the unit size, then walks the CHD's
    ISO 9660 filesystem to SYSTEM.CNF or PARAM.SFO, fetching only the sectors
    the walk asks for through chdman (``_read_chd_units``). A DVD CHD's units
    are plain 2048-byte sectors; a CD CHD's are 2352 or 2448 bytes, probed as
    Mode 2 Form 1 (data at 24) and then Mode 1 (data at 16). Returns the disc
    ID dict, or None. Storage that does not answer raises
    ``DiscIdStorageAbandoned``; a cancel raises ``ReadCancelled``.
    """
    try:
        header = await bounded_disc_read(
            read_chd_header, chd_path, path=chd_path, cancel_event=cancel_event,
        )
    except OSError as exc:
        logger.debug("disc_id: cannot read the CHD header of %s: %s", chd_path, exc)
        return None
    if header is None:
        logger.debug("disc_id: %s is not a CHD v5 file", chd_path)
        return None
    if header.unit_bytes == _CHD_UNIT_DVD:
        offsets = [0]
    elif header.unit_bytes in _CHD_UNITS_CD:
        offsets = [_RAW_SECTOR_HEADER_SIZE_MODE2, _RAW_SECTOR_HEADER_SIZE_MODE1]
    else:
        logger.debug(
            "disc_id: %s has unit_bytes=%d, not a CD or DVD", chd_path, header.unit_bytes,
        )
        return None
    sectors = header.logical_bytes // header.unit_bytes
    units: dict[int, bytes] = {}
    reads = 0
    for data_offset in offsets:
        cache = _ChdSectorCache(units, sectors, data_offset)
        while True:
            try:
                result = _identify_iso(cache)
                break
            except _NeedSectors as need:
                if reads >= _MAX_CHD_READS:
                    logger.debug("disc_id: gave up walking %s after %d reads", chd_path, reads)
                    return None
                reads += 1
                count = min(need.count, _MAX_CHD_READ_UNITS)
                data = await _read_chd_units(
                    chd_path, chdman_path, need.first, count, header.unit_bytes,
                    cancel_event,
                )
                if data is None:
                    return None
                for i in range(count):
                    units[need.first + i] = data[
                        i * header.unit_bytes:(i + 1) * header.unit_bytes
                    ]
        if result:
            logger.debug(
                "disc_id: game_id=%r from the sectors of %s (data offset %d)",
                result["game_id"], chd_path, data_offset,
            )
            return result
    return None


def _extract_cue(path: str, within=None) -> Optional[dict]:
    """
    Parse a .cue sheet, find the first DATA track's BIN file, and extract.
    """
    try:
        for bin_path in _cue_track_paths(path, within):
            if bin_path.exists():
                result = _extract_bin(str(bin_path))
                if result:
                    return result
                break
    except Exception as e:
        logger.debug("disc_id: CUE extraction failed for %s: %s", path, e)
    return None


def _extract_gdi(path: str, within=None) -> Optional[dict]:
    """
    Parse a Dreamcast .gdi file and extract game ID from the first track's
    IP.BIN header.  The .gdi format is a plain-text index:
        <num_tracks>
        <track_num> <start_lba> <track_type> <sector_size> <filename> <UNKNOWN>
    Track 1 is always the first data area (IP.BIN).
    """
    try:
        # The shared sheet parsing (utils.sheet_text): quoted names keep their
        # spaces, and the first spelling of a name that exists on disk wins.
        files = sheet_tracks(
            path, lambda text: [t.name for t in gdi_tracks(text)], within=within,
        )
        if not files:
            return None   # unreadable, or a track refused: say nothing
        entries = gdi_tracks(sheet_readings(path)[0])
        for track, bin_path in zip(entries, files):
            track_num = track.number
            sector_size = track.sector_size or 2352
            if not bin_path.exists():
                continue
            if track_num == "1":
                # Track 1 is the IP.BIN area (first 16 sectors at sector 0)
                try:
                    with open(bin_path, "rb") as bf:
                        # IP.BIN header at start of track; skip raw-sector
                        # header if sector_size > 2048
                        offset = _RAW_SECTOR_HEADER_SIZE_MODE1 if sector_size == 2352 else 0
                        bf.seek(offset)
                        data = bf.read(0x200)
                    result = _parse_ipbin(data)
                    if result.get("game_id"):
                        return result
                except OSError:
                    pass
                break  # only check track 1
    except Exception as e:
        logger.debug("disc_id: GDI extraction failed for %s: %s", path, e)
    return None


# ===========================================================================
# Public async extractor: CHD files
# ===========================================================================

async def extract_from_chd(
    chd_path: str,
    chdman_path: str = "chdman",
    *,
    cancel_event: asyncio.Event | None = None,
) -> Optional[dict]:
    """
    Extract game ID / title from an existing CHD file.

    Strategy (in order):
      1. Read disc sectors directly from the CHD via _CHDReader:
         walks the ISO 9660 filesystem to find SYSTEM.CNF (PS2/PS1) or
         PSP_GAME/PARAM.SFO (PSP).  Handles both DVD CHDs (2048-byte sectors)
         and CD CHDs (2352/2448-byte sectors with Mode 1 or Mode 2 Form 1
         framing), the same approach used by libchdr-based emulators.
      2. Read the Dreamcast GDRO IP.BIN tag (standard CD CHD tag).
      3. Look for a companion source file (.iso / .gdi / .bin / .cue) beside
         the CHD and extract from that.

    There is no app-written GAME/NAME tag to read first: chdman cannot write
    one into a compressed CHD (#352). Returns a dict containing at least
    ``game_id`` when successful; it may also include ``title`` and/or
    ``platform``. Returns None if no ID could be found.

    Every file read is bounded and raced against *cancel_event*
    (``bounded_disc_read``): storage that does not answer raises
    ``DiscIdStorageAbandoned`` and a cancel raises ``ReadCancelled``, so the
    caller never hangs on it and never caches an outcome for a file it did not
    read.
    """
    # --- Strategy 1: read disc sectors directly (like PCSX2 / AetherSX2) ----
    # For DVD CHDs (unit_bytes=2048) this reads SYSTEM.CNF / PARAM.SFO from
    # the CHD's own ISO 9660 filesystem without any extraction pass.
    disc_result = await _extract_from_chd_sectors(
        chd_path, chdman_path, cancel_event=cancel_event,
    )
    if disc_result and disc_result.get("game_id"):
        logger.debug(
            "disc_id: Strategy 1 (CHD sectors) found game_id=%r in %s",
            disc_result["game_id"],
            chd_path,
        )
        return disc_result
    logger.debug("disc_id: Strategy 1 (CHD sectors) found no game_id in %s", chd_path)

    # --- Strategy 2: Dreamcast GDRO (IP.BIN) ---------------------------------
    gdro = await _dumpmeta_bin(chd_path, "GDRO", chdman_path)
    if gdro:
        parsed = _parse_ipbin(gdro)
        if parsed.get("game_id"):
            logger.debug(
                "disc_id: Strategy 2 (GDRO/IP.BIN) found game_id=%r in %s",
                parsed["game_id"],
                chd_path,
            )
            return parsed
        logger.debug(
            "disc_id: Strategy 2 (GDRO/IP.BIN) tag present but no valid game_id in %s",
            chd_path,
        )
    else:
        logger.debug("disc_id: Strategy 2 (GDRO) tag not found in %s", chd_path)

    # --- Strategy 3: companion source file -----------------------------------
    res = await bounded_disc_read(
        _companion_disc_id, chd_path, path=chd_path, cancel_event=cancel_event,
    )
    if res:
        return res
    logger.debug("disc_id: Strategy 3 (companion file) found no game_id for %s", chd_path)

    logger.debug("disc_id: all strategies exhausted, no game_id found for %s", chd_path)
    return None


def _companion_disc_id(chd_path: str) -> Optional[dict]:
    """The disc ID of a Source file left beside the CHD (blocking)."""
    chd_p = Path(chd_path)
    for ext in (".iso", ".gdi", ".cue", ".bin"):
        candidate = chd_p.with_suffix(ext)
        if candidate.exists():
            res = extract_from_source(str(candidate))
            if res and res.get("game_id"):
                logger.debug(
                    "disc_id: Strategy 3 (companion %s) found game_id=%r in %s",
                    candidate.name, res["game_id"], chd_path,
                )
                return res
    return None


async def bounded_disc_read(
    func, *args, path: str, cancel_event: asyncio.Event | None,
):
    """Run a blocking disc-ID read of *path* off the loop, bounded and cancellable.

    The one seam for disc-ID reads: ``extract_from_chd`` (library scan,
    ``/api/info``) and ``ChdmanTool.post_convert`` both read through it.
    Through ``run_detached_bounded``: a disposable thread, at most
    ``_DISC_READ_TIMEOUT_SECONDS``, raced against *cancel_event*. A Volume that
    answers a cached ``stat`` and then blocks every read holds neither the loop,
    a shared pool worker, the scan nor its lane. Past the bound (or with the
    probe ceiling full) the thread is written off as abandoned storage: a
    warning names the file and ``DiscIdStorageAbandoned`` is raised, so no
    caller caches "nothing found" for a file it never read, and a scan stops its
    pass (#268). A cancel raises ``ReadCancelled``.
    """
    try:
        return await run_detached_bounded(
            func, *args, timeout=_DISC_READ_TIMEOUT_SECONDS, cancel_event=cancel_event,
        )
    except (DeadlineExpired, ProbeCapacityExceeded) as exc:
        logger.warning(
            "disc_id: reading %s did not answer in %gs; its disc ID is skipped",
            path, _DISC_READ_TIMEOUT_SECONDS,
        )
        raise DiscIdStorageAbandoned(
            f"reading {os.path.basename(path)} did not answer in "
            f"{_DISC_READ_TIMEOUT_SECONDS:g}s; the storage is not responding"
        ) from exc


# ===========================================================================
# Private chdman subprocess helpers
# ===========================================================================

async def _dumpmeta_bin(
    chd_path: str, tag: str, chdman_path: str
) -> Optional[bytes]:
    """Read a binary metadata tag from *chd_path* via chdman dumpmeta."""
    return await _dumpmeta_raw(chd_path, tag, chdman_path, suffix=".bin")


async def _dumpmeta_raw(
    chd_path: str, tag: str, chdman_path: str, suffix: str = ".bin"
) -> Optional[bytes]:
    """Run ``chdman dumpmeta`` and return the output bytes, or None on failure."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    tmp.close()
    try:
        with abandonment_checkpoint() as abandoned:
            returncode, _, stderr = await _chdman_runner().run_capture(
                _chdman_cmd(chdman_path, "dumpmeta", "-i", chd_path, "-t", tag, "-o", tmp_path),
                timeout=_DUMPMETA_TIMEOUT_SECONDS,
                nice_via_wrapper=True,
            )
        # Before the result is reduced to False/None below, which would read as
        # "tag absent" / "nothing to delete" and invite the next read or write.
        _abort_if_abandoned(abandoned, "chdman dumpmeta", chd_path)
        if returncode is None:
            # The shared teardown already bounded the kill; the old path killed
            # the child and then waited for it with no limit, which is what
            # could hang a scan on a dead mount.
            logger.debug(
                "disc_id: dumpmeta tag=%s timed out after %ss in %s",
                tag,
                _DUMPMETA_TIMEOUT_SECONDS,
                chd_path,
            )
            return None
        if returncode != 0:
            if logger.isEnabledFor(logging.DEBUG):
                stderr_text = stderr.decode(errors="replace").strip()
                logger.debug(
                    "disc_id: dumpmeta tag=%s not found or failed (rc=%d) in %s: %s",
                    tag,
                    returncode,
                    chd_path,
                    stderr_text,
                )
            return None
        if os.path.getsize(tmp_path) > _MAX_DUMPMETA_BYTES:
            logger.debug(
                "disc_id: dumpmeta tag=%s exceeded %d bytes in %s",
                tag,
                _MAX_DUMPMETA_BYTES,
                chd_path,
            )
            return None
        with open(tmp_path, "rb") as f:
            return f.read(_MAX_DUMPMETA_BYTES + 1)
    except DiscIdStorageAbandoned:
        raise
    except Exception as e:
        logger.debug("disc_id: dumpmeta tag=%s error: %s", tag, e)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
