"""Parser for Logiqx XML DAT files (MAME Redump format)."""

from __future__ import annotations

import io
from logging_setup import get_logger
import os
import re

import defusedxml.ElementTree as ET  # noqa: N817
from defusedxml.common import DefusedXmlException

logger = get_logger("dat_parser")

_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def parse_dat(source: str) -> tuple[dict, list[dict]]:
    """Parse a Logiqx XML DAT file from a file path or XML string.

    Accepts either a filesystem path (preferred — enables true iterparse
    streaming from disk) or a raw XML string.  Passing a path avoids loading
    the entire document into memory before parsing begins.

    defusedxml is used to prevent XXE (XML External Entity) and related XML
    injection attacks from untrusted DAT uploads.

    Returns:
        (header_info, entries) where header_info has name/description/version
        and entries is a list of dicts with game_name, rom_name, size, sha1, md5.
    """
    header: dict = {}
    entries: list[dict] = []

    try:
        if os.path.isfile(source):
            # Stream directly from disk — no in-memory copy of the XML.
            context = ET.iterparse(source, events=("end",))
        else:
            # Fallback: treat source as a raw XML string.
            context = ET.iterparse(
                io.BytesIO(source.encode("utf-8")), events=("end",),
            )

        for _event, elem in context:
            tag = _strip_ns(elem.tag)

            if tag == "header":
                for child in elem:
                    child_tag = _strip_ns(child.tag)
                    if child_tag == "name" and child.text:
                        header["name"] = child.text.strip()
                    elif child_tag == "description" and child.text:
                        header["description"] = child.text.strip()
                    elif child_tag == "version" and child.text:
                        header["version"] = child.text.strip()
                elem.clear()

            elif tag in ("game", "machine", "software"):
                game_name = elem.get("name", "").strip()
                # Prefer <description> text for the human-readable name when
                # present (softlist entries typically carry the full title in
                # <description> while the short-id is in name=""). Fall back
                # to the name attribute.
                for child in elem:
                    if _strip_ns(child.tag) == "description" and child.text:
                        game_name = child.text.strip()
                        break
                # Walk ALL hash-carrying descendants. Logiqx datafiles place
                # <rom> as direct children; MAME softlist DATs nest them inside
                # <part><dataarea><rom>; and CD-based softlists (Amiga CD,
                # Amiga CD32, Bandai Pippin, Konami FireBeat, etc.) carry their
                # track hashes in <disk> elements under <part><diskarea>.
                # iter() handles all three shapes.
                for rom in elem.iter():
                    if _strip_ns(rom.tag) not in ("rom", "disk"):
                        continue
                    entry = _parse_rom_element(rom, game_name)
                    if entry:
                        entries.append(entry)
                elem.clear()

    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML DAT file: {exc}") from exc
    except DefusedXmlException as exc:
        raise ValueError(f"DAT file contains forbidden XML content: {exc}") from exc

    if not header.get("name"):
        header["name"] = "Unknown DAT"

    return header, entries


def _parse_rom_element(elem: ET.Element, game_name: str) -> dict | None:
    """Extract hash info from a <rom> or <disk> element."""
    rom_name = elem.get("name", "").strip()
    sha1 = (elem.get("sha1") or "").strip().lower()
    md5 = (elem.get("md5") or "").strip().lower()
    size_str = elem.get("size", "0")

    try:
        size = int(size_str)
    except (ValueError, TypeError):
        size = 0

    # Must have at least one valid hash
    has_sha1 = bool(_SHA1_RE.match(sha1))
    has_md5 = bool(_MD5_RE.match(md5))

    if not has_sha1 and not has_md5:
        return None

    return {
        "game_name": game_name or rom_name,
        "rom_name": rom_name,
        "size": size,
        "sha1": sha1 if has_sha1 else "",
        "md5": md5 if has_md5 else "",
    }


def _strip_ns(tag: str) -> str:
    """Remove XML namespace prefix from a tag."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag
