"""Mode metadata for the tool plugin registry.

A ``ModeSpec`` carries everything the dispatch sites need to know about a
conversion mode without branching on tool identity. Each ``ModeSpec.mode``
equals a ``ConversionMode`` value (the validated wire type in ``models.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ModeKind(str, Enum):
    CREATE = "create"      # source -> compressed container
    EXTRACT = "extract"    # compressed container -> source
    COPY = "copy"          # recompress in place
    COMPRESS = "compress"  # generic one-shot compressor (z3ds-style)


class InputKind(str, Enum):
    """The unit of work a mode consumes.

    Everything in the codebase historically keyed input off
    ``Path(filename).suffix``, which assumes a file. A directory has no
    suffix, so a mode that packages a folder (makeps3iso folder->iso) declares
    ``DIRECTORY`` and relies on a detector predicate
    (``ToolPlugin.accepts_directory``) instead of an extension match.
    """

    FILE = "file"
    DIRECTORY = "directory"


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
    # Default keeps every existing mode FILE-based (zero behavior change). A
    # directory mode (folder->iso) overrides to {InputKind.DIRECTORY}.
    input_kinds: frozenset[InputKind] = frozenset({InputKind.FILE})


@dataclass(frozen=True)
class ChainStep:
    """One step of a :class:`ChainSpec`: a sub-mode run by an existing tool."""

    tool_id: str          # owner of this step's mode ("cso", "chdman")
    mode: str             # the sub-mode wire value ("cso_decompress", "createdvd")
    weight: float         # progress weight; normalized across steps at runtime
    output_ratio: float   # est. (this step's output size) / (chain input size)


@dataclass(frozen=True)
class ChainSpec:
    """A composite mode that runs an ordered pipeline of existing modes.

    Structurally a superset of :class:`ModeSpec`, so every registry / route /
    job_manager consumer that reads a spec's fields (``mode``, ``tool_id``,
    ``kind``, ``output_ext``, ``input_extensions``, ``supports_*``,
    ``allows_archive_input``, ``input_kinds``) works unchanged. The chain
    extras (``steps``, ``intermediate_exts``, ``verify_step``) are only read by
    the synthetic ``ChainTool`` that orchestrates the pipeline.
    """

    mode: str                       # composite wire value, e.g. "cso_to_chd"
    tool_id: str                    # synthetic owner ("chain")
    kind: ModeKind
    label: str
    group: str
    output_ext: str | None          # final output extension (".chd")
    input_extensions: frozenset[str]  # == step 1's accepted inputs
    steps: tuple[ChainStep, ...]    # ordered: first owns input, last owns output
    intermediate_exts: tuple[str, ...] = ()   # one per non-final step (".iso",)
    verify_step: int = -1           # index of the step whose tool verifies the final output
    supports_compression: bool = False
    supports_compression_level: bool = False
    supports_delete_on_verify: bool = True   # of the ORIGINAL source, gated on final verify
    allows_archive_input: bool = True        # == step 1's
    input_kinds: frozenset[InputKind] = field(
        default_factory=lambda: frozenset({InputKind.FILE})
    )
