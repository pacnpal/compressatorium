"""
Disc ID / Title extractor for game disc images.

Supports extracting game serial numbers and titles from:
  - ISO 9660 source files (.iso) used for PS2, PSP, and PS1
  - Dreamcast GDI images (.gdi + track files)
  - CHD files via chdman dumpmeta (reads embedded GAME/NAME tags or GDRO IP.BIN)

Extraction strategy (in priority order):
  1. Read our embedded GAME / NAME tags written by addmeta at conversion time.
  2. For CD CHDs: parse the Dreamcast GDRO (IP.BIN) metadata standard tag.
  3. Look for a companion source file (.iso / .gdi) next to the CHD.
  4. Return None if nothing is found.

Source-file extraction:
  - PS2 / PS1 .iso: ISO 9660 → root/SYSTEM.CNF → BOOT2= / BOOT= line.
  - PSP .iso:       ISO 9660 → /PSP_GAME/PARAM.SFO → DISC_ID + TITLE keys.
  - Dreamcast .gdi: reads Track01.bin IP.BIN header (product number + title).

Retroactive tagging (ensure_disc_id_embedded):
  Existing CHDs that were created before conversion-time tagging was added can
  be back-filled with GAME / NAME tags by calling ensure_disc_id_embedded().
  It checks for an existing GAME tag first (fast, no modification) and only
  runs GDRO / companion-file extraction + chdman addmeta when the tag is absent.
  This is called automatically during every metadata scan.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("chd.disc_id")

# ---------------------------------------------------------------------------
# CHD addmeta / dumpmeta tag names used by this application
# ---------------------------------------------------------------------------
TAG_GAME = "GAME"  # game / disc serial number (text)
TAG_NAME = "NAME"  # game / disc title         (text)

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
    r"BOOT2\s*=\s*cdrom0?[:\\\/]+([A-Z0-9_]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_BOOT_RE = re.compile(
    r"BOOT\s*=\s*cdrom[:\\\/]+([A-Z0-9_]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

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

def _read_pvd(f) -> Optional[bytes]:
    """Return the 2048-byte PVD sector from a seekable ISO 9660 stream, or None."""
    try:
        f.seek(_PVD_SECTOR * _SECTOR_SIZE)
        pvd = f.read(_SECTOR_SIZE)
    except OSError:
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


def _list_dir(f, lba: int, size: int) -> list[tuple[str, int, int, bool]]:
    """
    Yield (name, lba, size, is_dir) for each entry in an ISO 9660 directory.
    Skips the '.' and '..' self-referencing entries.
    """
    try:
        f.seek(lba * _SECTOR_SIZE)
        data = f.read(size)
    except OSError:
        return []

    entries: list[tuple[str, int, int, bool]] = []
    pos = 0
    while pos < len(data):
        rec_len = data[pos] if pos < len(data) else 0
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
                pass

        pos += rec_len
    return entries


def _find_file(f, root_lba: int, root_size: int, path_parts: list[str]) -> Optional[bytes]:
    """
    Walk an ISO 9660 directory tree and return the raw bytes of the file at
    *path_parts* (e.g. ["PSP_GAME", "PARAM.SFO"]), or None if not found.
    """
    cur_lba, cur_size = root_lba, root_size
    for i, part in enumerate(path_parts):
        entries = _list_dir(f, cur_lba, cur_size)
        hit = next((e for e in entries if e[0] == part.upper()), None)
        if hit is None:
            return None
        name, lba, size, is_dir = hit
        if i == len(path_parts) - 1:
            # Final component — read and return the file
            try:
                f.seek(lba * _SECTOR_SIZE)
                return f.read(size)
            except OSError:
                return None
        if not is_dir:
            return None
        cur_lba, cur_size = lba, size
    return None


# ===========================================================================
# Format-specific parsers
# ===========================================================================

def _parse_system_cnf(data: bytes) -> dict:
    """Parse SYSTEM.CNF and return a dict with game_id and platform."""
    try:
        text = data.decode("ascii", errors="replace")
    except Exception:
        return {}
    # PS2
    m = _BOOT2_RE.search(text)
    if m:
        raw = m.group(1).rstrip(";")
        return {"game_id": raw, "platform": "ps2"}
    # PS1
    m = _BOOT_RE.search(text)
    if m:
        raw = m.group(1).rstrip(";")
        return {"game_id": raw, "platform": "ps1"}
    return {}


def _parse_param_sfo(data: bytes) -> dict:
    """
    Parse a PSP PARAM.SFO binary blob and return game_id, title, platform.
    SFO format (little-endian):
      0x00  4   magic "\x00PSF"
      0x04  2   version minor
      0x06  2   version major
      0x08  4   key_table_offset
      0x0C  4   data_table_offset
      0x10  4   num_entries
    Then num_entries × 16-byte index entries:
      0x00  2   key_offset (from key_table_offset)
      0x02  2   data_format  (0x0204 = utf8 string)
      0x04  4   data_len
      0x08  4   data_max_len
      0x0C  4   data_offset (from data_table_offset)
    """
    if len(data) < 20 or data[:4] != _SFO_MAGIC:
        return {}
    try:
        key_table_off = struct.unpack_from("<I", data, 8)[0]
        data_table_off = struct.unpack_from("<I", data, 12)[0]
        num_entries = struct.unpack_from("<I", data, 16)[0]

        result: dict = {}
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
            val = data[val_start:val_end].rstrip(b"\x00").decode("utf-8", errors="replace")

            if key == "DISC_ID":
                result["game_id"] = val
            elif key == "TITLE":
                result["title"] = val

        if result:
            result["platform"] = "psp"
        return result
    except Exception:
        return {}


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
# Public synchronous extractor: source files
# ===========================================================================

def extract_from_source(path: str) -> Optional[dict]:
    """
    Extract game ID (and optionally title) from a source disc image file.

    Supported formats:
      .iso  → PS2 (SYSTEM.CNF/BOOT2), PS1 (SYSTEM.CNF/BOOT),
               PSP (PSP_GAME/PARAM.SFO)
      .gdi  → Dreamcast (reads the first data track for IP.BIN)
      .bin  → Tries ISO 9660 with 2352-byte sector layout (Mode 2 / raw)
      .cue  → Follows the DATA track .bin reference

    Returns a dict with at least ``game_id`` and ``platform`` keys, or None.
    """
    ext = Path(path).suffix.lower()
    if ext == ".iso":
        return _extract_iso(path)
    if ext == ".gdi":
        return _extract_gdi(path)
    if ext == ".cue":
        return _extract_cue(path)
    if ext == ".bin":
        return _extract_bin(path)
    return None


def _extract_iso(path: str) -> Optional[dict]:
    """ISO 9660 extraction (2048-byte sectors — PS2, PSP, PS1 DVD-based)."""
    try:
        with open(path, "rb") as f:
            pvd = _read_pvd(f)
            if not pvd:
                return None
            root_lba, root_size = _pvd_root_dir(pvd)

            # PS2 / PS1 — SYSTEM.CNF in root
            cnf = _find_file(f, root_lba, root_size, ["SYSTEM.CNF"])
            if cnf:
                result = _parse_system_cnf(cnf)
                if result.get("game_id"):
                    return result

            # PSP — /PSP_GAME/PARAM.SFO
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


def _extract_cue(path: str) -> Optional[dict]:
    """
    Parse a .cue sheet, find the first DATA track's BIN file, and extract.
    """
    try:
        cue_dir = Path(path).parent
        with open(path, encoding="latin-1") as cue:
            for line in cue:
                line = line.strip()
                if line.upper().startswith("FILE "):
                    # FILE "track.bin" BINARY
                    parts = line.split('"')
                    if len(parts) >= 2:
                        bin_path = cue_dir / parts[1]
                        if bin_path.exists():
                            result = _extract_bin(str(bin_path))
                            if result:
                                return result
                        break
    except Exception as e:
        logger.debug("disc_id: CUE extraction failed for %s: %s", path, e)
    return None


def _extract_gdi(path: str) -> Optional[dict]:
    """
    Parse a Dreamcast .gdi file and extract game ID from the first track's
    IP.BIN header.  The .gdi format is a plain-text index:
        <num_tracks>
        <track_num> <start_lba> <track_type> <sector_size> <filename> <UNKNOWN>
    Track 1 is always the first data area (IP.BIN).
    """
    try:
        gdi_dir = Path(path).parent
        with open(path, encoding="latin-1") as f:
            lines = [l.strip() for l in f if l.strip()]

        for line in lines[1:]:  # skip track count on first line
            parts = line.split()
            if len(parts) < 5:
                continue
            track_num = parts[0]
            sector_size = int(parts[3]) if parts[3].isdigit() else 2352
            filename = parts[4].strip('"')
            bin_path = gdi_dir / filename
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

async def extract_from_chd(chd_path: str, chdman_path: str = "chdman") -> Optional[dict]:
    """
    Extract game ID / title from an existing CHD file.

    Strategy (in order):
      1. Read our custom GAME / NAME tags (written by addmeta at conversion).
      2. Read the Dreamcast GDRO IP.BIN tag (standard CD CHD tag).
      3. Look for a companion source file (.iso / .gdi / .bin / .cue) beside
         the CHD and extract from that.

    Returns a dict with at least ``game_id`` and ``platform`` keys, or None.
    """
    # --- Strategy 1: our embedded GAME tag -----------------------------------
    result = await _dumpmeta_text(chd_path, TAG_GAME, chdman_path)
    if result:
        game_id = result.strip()
        if game_id:
            name_result = await _dumpmeta_text(chd_path, TAG_NAME, chdman_path)
            title = (name_result or "").strip() or None
            out: dict = {"game_id": game_id}
            if title:
                out["title"] = title
            return out

    # --- Strategy 2: Dreamcast GDRO (IP.BIN) ---------------------------------
    gdro = await _dumpmeta_bin(chd_path, "GDRO", chdman_path)
    if gdro:
        parsed = _parse_ipbin(gdro)
        if parsed.get("game_id"):
            return parsed

    # --- Strategy 3: companion source file -----------------------------------
    chd_p = Path(chd_path)
    for ext in (".iso", ".gdi", ".cue", ".bin"):
        candidate = chd_p.with_suffix(ext)
        if candidate.exists():
            res = extract_from_source(str(candidate))
            if res and res.get("game_id"):
                logger.debug(
                    "disc_id: found game_id from companion %s: %s",
                    candidate.name,
                    res,
                )
                return res

    return None


async def embed_in_chd(
    chd_path: str,
    game_id: str,
    title: Optional[str] = None,
    chdman_path: str = "chdman",
) -> bool:
    """
    Embed *game_id* (and optionally *title*) into a CHD file using
    ``chdman addmeta``.  Returns True on success, False on failure.

    Tags written:
      GAME — the disc serial / game ID (e.g. "SLUS_20312", "ULES00135",
             "MK-51034")
      NAME — the human-readable game title (optional)
    """
    ok = await _addmeta_text(chd_path, TAG_GAME, game_id, chdman_path)
    if not ok:
        return False
    if title:
        # NAME tag is best-effort; don't fail the whole operation if it errors
        await _addmeta_text(chd_path, TAG_NAME, title, chdman_path)
    return True


async def ensure_disc_id_embedded(
    chd_path: str,
    chdman_path: str = "chdman",
) -> Optional[dict]:
    """
    Ensure a CHD file has GAME / NAME metadata tags embedded, back-filling
    existing CHDs that were created before conversion-time tagging was added.

    Algorithm (standards-compliant — all tags follow the 4-char MAME CHD
    metadata format and are written via ``chdman addmeta``):

      1. Fast-path: read the GAME tag.  If it is already present, return the
         existing info without touching the file.
      2. Try the standard Dreamcast GDRO (IP.BIN) tag that chdman embeds for
         GDI-sourced CHDs.  Parse product number + title from the IP.BIN
         binary, then embed GAME / NAME via addmeta.
      3. Scan for a companion source file (.iso / .gdi / .cue / .bin) next to
         the CHD, extract the disc serial, then embed GAME / NAME.
      4. Return None if no identity information can be found.

    Returns a dict with at least ``game_id`` on success, or None if the disc
    ID could not be determined.
    """
    # --- Fast path: GAME tag already present ---------------------------------
    existing = await _dumpmeta_text(chd_path, TAG_GAME, chdman_path)
    if existing and existing.strip():
        game_id = existing.strip()
        name_raw = await _dumpmeta_text(chd_path, TAG_NAME, chdman_path)
        title = (name_raw or "").strip() or None
        out: dict = {"game_id": game_id}
        if title:
            out["title"] = title
        return out

    # --- Strategy 2: standard Dreamcast GDRO tag -----------------------------
    gdro = await _dumpmeta_bin(chd_path, "GDRO", chdman_path)
    if gdro:
        parsed = _parse_ipbin(gdro)
        if parsed.get("game_id"):
            logger.debug(
                "disc_id: embedding game_id=%r from GDRO in %s",
                parsed["game_id"],
                chd_path,
            )
            await embed_in_chd(
                chd_path, parsed["game_id"], parsed.get("title"), chdman_path
            )
            return parsed

    # --- Strategy 3: companion source file -----------------------------------
    chd_p = Path(chd_path)
    for ext in (".iso", ".gdi", ".cue", ".bin"):
        candidate = chd_p.with_suffix(ext)
        if candidate.exists():
            res = extract_from_source(str(candidate))
            if res and res.get("game_id"):
                logger.debug(
                    "disc_id: embedding game_id=%r from companion %s in %s",
                    res["game_id"],
                    candidate.name,
                    chd_path,
                )
                await embed_in_chd(
                    chd_path, res["game_id"], res.get("title"), chdman_path
                )
                return res

    return None


# ===========================================================================
# Private chdman subprocess helpers
# ===========================================================================

async def _addmeta_text(
    chd_path: str, tag: str, value: str, chdman_path: str
) -> bool:
    """Write a text metadata tag to *chd_path* using chdman addmeta."""
    try:
        proc = await asyncio.create_subprocess_exec(
            chdman_path,
            "addmeta",
            "-i", chd_path,
            "-t", tag,
            "-vt", value,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "disc_id: addmeta tag=%s failed (rc=%s): %s",
                tag,
                proc.returncode,
                stderr.decode(errors="replace").strip(),
            )
            return False
        return True
    except Exception as e:
        logger.warning("disc_id: addmeta tag=%s error: %s", tag, e)
        return False


async def _dumpmeta_text(
    chd_path: str, tag: str, chdman_path: str
) -> Optional[str]:
    """Read a text metadata tag from *chd_path* via chdman dumpmeta."""
    raw = await _dumpmeta_raw(chd_path, tag, chdman_path, suffix=".txt")
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


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
        proc = await asyncio.create_subprocess_exec(
            chdman_path,
            "dumpmeta",
            "-i", chd_path,
            "-t", tag,
            "-o", tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0:
            return None
        with open(tmp_path, "rb") as f:
            return f.read()
    except Exception as e:
        logger.debug("disc_id: dumpmeta tag=%s error: %s", tag, e)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
