"""Parser for Logiqx XML DAT files (MAME Redump format)."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger("chd.dat_parser")

_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def parse_dat(xml_content: str) -> tuple[dict, list[dict]]:
    """Parse a Logiqx XML DAT file.

    Uses iterparse for memory efficiency on large DAT files.

    Returns:
        (header_info, entries) where header_info has name/description/version
        and entries is a list of dicts with game_name, rom_name, size, sha1, md5.
    """
    header: dict = {}
    entries: list[dict] = []

    try:
        # Use a parser that disables external entities to prevent XXE attacks
        parser = ET.XMLParser()
        context = ET.iterparse(
            _string_to_file(xml_content), events=("end",), parser=parser,
        )

        for event, elem in context:
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

            elif tag == "game" or tag == "machine":
                game_name = elem.get("name", "").strip()
                for child in elem:
                    child_tag = _strip_ns(child.tag)
                    if child_tag == "description" and child.text and not game_name:
                        game_name = child.text.strip()
                    elif child_tag == "rom":
                        entry = _parse_rom_element(child, game_name)
                        if entry:
                            entries.append(entry)
                elem.clear()

    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML DAT file: {exc}") from exc

    if not header.get("name"):
        header["name"] = "Unknown DAT"

    return header, entries


def _parse_rom_element(elem: ET.Element, game_name: str) -> dict | None:
    """Extract hash info from a <rom> element."""
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


def _string_to_file(content: str):
    """Convert a string to a file-like object for iterparse."""
    import io
    return io.BytesIO(content.encode("utf-8"))
