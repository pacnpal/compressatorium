"""Mode metadata for the tool plugin registry.

A ``ModeSpec`` carries everything the dispatch sites need to know about a
conversion mode without branching on tool identity. Each ``ModeSpec.mode``
equals a ``ConversionMode`` value (the validated wire type in ``models.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ``InputKind`` lives in ``models`` so ``ConversionJob`` can type its field
# against it without importing the ``services.tools`` package (which imports
# ``models`` to build the registry — the reverse import would be a cycle). It is
# re-exported here so ``from services.tools.spec import InputKind`` and the
# ``ModeSpec.input_kinds`` default keep working unchanged.
from models import InputKind


class DeleteProof(str, Enum):
    """The check that lets a delete-source job remove its Source (#423).

    ``ROUND_TRIP``, the default: the container check, then the Round-trip when
    the mode supports it (``supports_round_trip``) and the job did not take the
    container-only opt-out. ``RECOGNIZED``, for a mode whose output can't be
    Round-tripped against its Source (PBP -> ISO, CIA <-> CCI): the container
    check, then the output's SHA-1 must be in a local DAT or Hasheous. A miss
    keeps the Source and the job still completes. The container-only opt-out
    never applies to it. Read through ``ToolRegistry.mode_delete_proof`` and
    ``round_trip.delete_check_level``, never by branching on the tool.
    """

    ROUND_TRIP = "round_trip"
    RECOGNIZED = "recognized"


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
    # The tool refuses this mode without a codec (dolphin-tool's RVZ and WIA):
    # a job that chose none runs with the tool's `default_compression`, through
    # `ToolPlugin.compression_for`. A mode that works without one (chdman picks
    # its own best codecs) leaves it False and is sent nothing.
    requires_codec: bool = False
    # The codecs this mode accepts, when the tool restricts them per mode
    # (dolphin-tool refuses zstd in a WIA container); empty for no opinion.
    # The routes refuse any other, and so does the plugin before its tool runs.
    codecs: frozenset[str] = frozenset()
    # This mode's default codec, when it differs from the tool's
    # (`ToolPlugin.default_compression`): RVZ and WIA each have their own.
    # `ToolPlugin.compression_for` prefers it.
    default_compression: str | None = None
    # Each codec's valid level range and default level, as
    # ``(codec, min, max, default)``, for a tool that bounds levels per codec
    # (dolphin-tool: zstd 1-22, bzip2/lzma/lzma2 1-9). A codec not listed
    # takes no level of its own. Read through :meth:`level_range`.
    codec_levels: tuple[tuple[str, int, int, int], ...] = ()
    # The block size this mode writes, for a tool that needs one (dolphin-tool
    # refuses GCZ, RVZ and WIA without `-b`); None when the mode takes none.
    block_size: int | None = None
    # The rule a block size must meet, as ``(minimum, multiple_from)``: at
    # least *minimum*, a power of two below *multiple_from* (everywhere when
    # it is None), and a multiple of *multiple_from* from there up. Checked by
    # ``tools.base.block_size_problem``.
    block_size_rule: tuple[int, int | None] | None = None
    supports_delete_on_verify: bool = False
    # Whether the tool can verify this mode's product *without* the question of
    # deleting anything. Implied by `supports_delete_on_verify` (deleting needs
    # a verify first), so only a mode that can be checked but must never delete
    # its source declares it: makeps3iso's folder->iso reads PARAM.SFO back out
    # of the ISO it built, while deleting a curated game folder on the strength
    # of that is not something to offer. Gating "verify each result" on the
    # delete flag made that check unreachable for exactly those modes.
    supports_verify: bool = False
    # Whether a delete-source job in this mode can be Round-tripped: the tool's
    # ``content_digest`` covers both this mode's inputs and its output, so the
    # job compares the two before deleting the Source. False keeps the
    # container check alone, and such a job says Intact (#298).
    supports_round_trip: bool = False
    # Why this mode always keeps its Source, in the user's words, for a mode
    # whose delete-on-verify is off on principle rather than for want of a
    # check (ZAR stores files, not the disc, and nothing rebuilds the ISO).
    # Only meaningful with ``supports_delete_on_verify`` False. The convert
    # panel, the Rule editor, the refusing 400 and the CLI all show it, so the
    # user learns why the option isn't there. Empty says nothing.
    source_kept_reason: str = ""
    # Which check lets a delete-source job in this mode remove its Source
    # (:class:`DeleteProof`). Only meaningful with ``supports_delete_on_verify``.
    # A RECOGNIZED mode never sets ``supports_round_trip``.
    delete_proof: DeleteProof = DeleteProof.ROUND_TRIP
    allows_archive_input: bool = False         # chdman create modes only
    # Sibling outputs this mode writes beside its primary output_path, expressed
    # as suffix swaps off the primary (e.g. extractcd's ``.cue`` -> ``.bin``
    # data-track sidecar). Read by ``BaseTool.companion_outputs`` so conflict
    # detection, overwrite cleanup, size accounting and in-use tracking enumerate
    # companions from one place instead of re-hardcoding the suffix per consumer.
    # Modes whose companion set is dynamic (makeps3iso's size-dependent split
    # parts) leave this empty and override ``companion_outputs`` instead.
    companion_exts: tuple[str, ...] = ()
    # Default keeps every existing mode FILE-based (zero behavior change). A
    # directory mode (folder->iso) overrides to {InputKind.DIRECTORY}.
    input_kinds: frozenset[InputKind] = frozenset({InputKind.FILE})
    # Platforms this MODE serves, when they differ from its tool's. Empty means
    # "ask the tool", which is right for every single-system tool: chdman's
    # modes are all as PS2/PSX/Dreamcast as chdman is. It exists for composite
    # modes, where the tool is a shell and each mode belongs to a different
    # system -- the chain tool declares no platforms of its own, so without
    # this its GameCube NKit->RVZ mode was offered on PS2 and its PS2 CSO->CHD
    # mode on GameCube, and a saved rule would convert a disc to a format for
    # the wrong console.
    platform_slugs: frozenset[str] = frozenset()
    # The inputs this mode's command can actually read, when that is narrower
    # than ``input_extensions`` (which gates manual selection and stays as it
    # was). Empty means "every input extension". chdman is the case: all five
    # create modes accept the same four extensions by hand, but ``createdvd``
    # reads only a flat 2048-byte ``.iso`` and ``createcd`` only a track sheet
    # or an ``.iso``. Auto asks ``ToolRegistry.mode_reads_extensions`` so it
    # never sends a file to a command that cannot read it. Must be a subset of
    # ``input_extensions``.
    reads_extensions: frozenset[str] = frozenset()
    # Whether Auto may pick this mode as a Platform's default target. False for
    # commands that are never the right default for an Image whose Platform
    # merely matches: chdman's hard-disk / raw / laserdisc commands claim the
    # arcade Platforms, but a CD image in an ``arcade`` folder is a CD.
    auto_target: bool = True
    # Platforms this mode may be the Auto target for on top of
    # ``platform_slugs``, without being offered for them by hand. createcd
    # declares ``ps2``: a PS2 CD-ROM title is a plain Mode 2 CD that only
    # createcd reads as a track sheet, but a platform-narrowed manual panel
    # must keep landing a PS2 catalog on createdvd (see chdman.py).
    auto_platform_slugs: frozenset[str] = frozenset()
    # Whether an unattended Rule may run this mode at all. True for every mode
    # whose only question is termination, which the registry derives
    # (`mode_is_automatable`). False opts a mode out by policy: it is meant to
    # be run by hand (the eXoDOS repack writes a second, playable copy of every
    # game beside its Source, which a Rule over a RomM folder would multiply).
    automatable: bool = True

    def level_range(self, codec: str) -> tuple[int, int, int] | None:
        """``(min, max, default)`` level for *codec*, or None when it takes none."""
        return _level_range(self.codec_levels, codec)

    def takes_no_level(self, codec: str | None) -> bool:
        """Whether the codec a job runs with takes no level (#363).

        That codec is *codec*, or the mode's ``default_compression`` when blank.
        Only a mode with per-codec levels (``codec_levels``) can say no: there,
        a codec it lists no range for (Dolphin's ``none``) takes none. A mode
        without them (nsz) answers False, and the tool's own range applies.
        """
        return _takes_no_level(self, codec)


def _takes_no_level(spec: ModeSpec | ChainSpec, codec: str | None) -> bool:
    effective = codec or spec.default_compression
    return bool(spec.codec_levels and effective) and spec.level_range(effective) is None


def _level_range(
    codec_levels: tuple[tuple[str, int, int, int], ...], codec: str,
) -> tuple[int, int, int] | None:
    for name, low, high, default in codec_levels:
        if name == codec:
            return low, high, default
    return None


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
    # Structural parity with ModeSpec: `ToolPlugin.compression_for` reads these.
    requires_codec: bool = False
    codecs: frozenset[str] = frozenset()
    default_compression: str | None = None
    codec_levels: tuple[tuple[str, int, int, int], ...] = ()
    # Structural parity with ModeSpec: each step's own mode names its block size.
    block_size: int | None = None
    block_size_rule: tuple[int, int | None] | None = None
    supports_delete_on_verify: bool = True   # of the ORIGINAL source, gated on final verify
    # Structural parity with ModeSpec: a chain's final step verifies the
    # output, and its delete flag already implies that, so this stays False and
    # `registry.mode_supports_verify` reads them together.
    supports_verify: bool = False
    # Structural parity with ModeSpec. A chain that sets it compares the first
    # step's decoded Source with the final output (ChainTool.content_digest).
    supports_round_trip: bool = False
    # Structural parity with ModeSpec.source_kept_reason / delete_proof.
    source_kept_reason: str = ""
    delete_proof: DeleteProof = DeleteProof.ROUND_TRIP
    allows_archive_input: bool = True        # == step 1's
    input_kinds: frozenset[InputKind] = field(
        default_factory=lambda: frozenset({InputKind.FILE})
    )
    # Structural parity with ModeSpec.companion_exts so BaseTool.companion_outputs
    # works for chain modes too (cso_to_chd's single .chd has no companion).
    companion_exts: tuple[str, ...] = ()
    # Structural parity with ModeSpec.platform_slugs -- and the reason that
    # field exists. The chain tool is a shell with no platform of its own, so
    # each composite mode has to name its own system or it is offered on every
    # one of them.
    platform_slugs: frozenset[str] = frozenset()
    # Structural parity with ModeSpec.reads_extensions / auto_target /
    # auto_platform_slugs.
    reads_extensions: frozenset[str] = frozenset()
    auto_target: bool = True
    auto_platform_slugs: frozenset[str] = frozenset()
    # Structural parity with ModeSpec.automatable.
    automatable: bool = True

    def level_range(self, codec: str) -> tuple[int, int, int] | None:
        """Structural parity with :meth:`ModeSpec.level_range`."""
        return _level_range(self.codec_levels, codec)

    def takes_no_level(self, codec: str | None) -> bool:
        """Structural parity with :meth:`ModeSpec.takes_no_level`."""
        return _takes_no_level(self, codec)
