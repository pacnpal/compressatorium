"""Mode metadata for the tool plugin registry.

A ``ModeSpec`` carries everything the dispatch sites need to know about a
conversion mode without branching on tool identity. Each ``ModeSpec.mode``
equals a ``ConversionMode`` value (the validated wire type in ``models.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ModeKind(str, Enum):
    CREATE = "create"      # source -> compressed container
    EXTRACT = "extract"    # compressed container -> source
    COPY = "copy"          # recompress in place
    COMPRESS = "compress"  # generic one-shot compressor (z3ds-style)


@dataclass(frozen=True)
class ModeSpec:
    mode: str                       # wire value, == ConversionMode value
    tool_id: str                    # "chdman" | "dolphin" | "z3ds"
    kind: ModeKind
    label: str                      # UI label
    group: str                      # UI group id
    output_ext: str | None          # ".chd"/".rvz"/None when input-ext-mapped
    input_extensions: frozenset[str]
    supports_compression: bool = False
    supports_compression_level: bool = False  # dolphin rvz/wia only
    supports_delete_on_verify: bool = False
    allows_archive_input: bool = False         # chdman create modes only
    # Surface this mode's inputs as members when browsing INTO an archive,
    # WITHOUT making them convertible in place (romz single-ROM archives: the
    # user wants to see/verify the ROM, but recompressing it would be recursive).
    lists_archive_members: bool = False
