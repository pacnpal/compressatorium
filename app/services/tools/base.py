"""The plugin contract (``ToolPlugin``) and a minimal shared base.

Phase 0 keeps ``BaseTool`` deliberately small: it only hoists the
mode-lookup and derived-extension helpers that every tool needs. The shared
``SubprocessRunner`` orchestration and the ``verify()``-wraps-``verify_stream()``
default described in the design land in a later phase, once real logic is moved
out of the existing service singletons.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from logging_setup import get_logger
from pydantic import BaseModel

from models import OutputStatus
from services.subprocess_runner import resolve_verify_timeout, run_detached
from utils.path_utils import match_extension

from .spec import ModeSpec

logger = get_logger("tools.base")


def compression_problem(spec: ModeSpec, compression: str | None) -> str | None:
    """Why *spec*'s mode refuses *compression*, or None when it takes it.

    A codec outside ``spec.codecs`` (when the mode names its codecs), a level
    for a codec that takes none (``spec.takes_no_level``, #363), or a level
    outside that codec's range (``spec.level_range``). The routes answer 400
    with it, and the plugin refuses with it before its tool runs.
    """
    for part in (compression or "").split(","):
        codec, _, level = part.partition(":")
        if spec.codecs and codec and codec not in spec.codecs:
            return (
                f"{spec.label} can't use {codec} compression; it takes "
                f"{', '.join(sorted(spec.codecs))}"
            )
        if level and spec.takes_no_level(codec):
            return (
                f"{spec.label} {codec or spec.default_compression} compression "
                "takes no level"
            )
        levels = spec.level_range(codec) if codec else None
        if levels and level:
            low, high, _default = levels
            if not level.isdigit() or not low <= int(level) <= high:
                return (
                    f"{spec.label} {codec} takes levels {low}-{high}; "
                    f"{level} is out of range"
                )
    return None


def without_unusable_codecs(
    spec: ModeSpec, compression: str | None,
) -> tuple[str | None, list[str]]:
    """*compression* less the codecs *spec*'s mode can't use, and those codecs.

    The one drop rule for a saved compression (#471, #474): an Auto target's
    saved preference (``checked_compression``) and a stored Rule
    (``normalize_rule``) keep the codecs ``compression_problem`` would take and
    lose the rest. None when nothing is left, which runs the tool's own codecs.
    A mode that names no codecs keeps everything. Parts are trimmed; a part
    with only a level (``":19"``) is kept.
    """
    parts = [p.strip() for p in (compression or "").split(",") if p.strip()]
    kept: list[str] = []
    dropped: list[str] = []
    for part in parts:
        codec = part.partition(":")[0]
        if spec.codecs and codec and codec not in spec.codecs:
            dropped.append(codec)
        else:
            kept.append(part)
    return ",".join(kept) or None, dropped


def block_size_problem(spec: ModeSpec, size: int) -> str | None:
    """Why *spec*'s mode refuses block size *size*, or None when it takes it.

    Reads ``spec.block_size_rule`` (Dolphin's ``IsDiscImageBlockSizeValid``
    for its containers). A mode with no rule takes no block size at all.
    """
    if not spec.block_size_rule:
        return f"{spec.label} takes no block size"
    minimum, multiple_from = spec.block_size_rule
    power_of_two = size > 0 and size & (size - 1) == 0
    if multiple_from and size >= multiple_from:
        valid = size % multiple_from == 0
    else:
        valid = power_of_two
    if size < minimum or not valid:
        rule = "a power of two" + (
            f" below {multiple_from} and a multiple of it from there"
            if multiple_from else ""
        )
        return f"{spec.label} takes a block size of at least {minimum}, {rule}; {size} isn't"
    return None


def held_file_path(fd: int) -> str | None:
    """A path that names the file *fd* is open on, for a tool to read. Blocking.

    ``/proc/<pid>/fd/<fd>`` (Linux, the app's container): it reopens the very
    inode the descriptor holds, whatever is renamed into its old name, and a
    tool's child process can open it too. Checked by file identity (device and
    inode) against the descriptor. None where there is no such path: a caller
    that must read the held file then refuses rather than read the name.
    """
    candidate = f"/proc/{os.getpid()}/fd/{fd}"
    try:
        held, named = os.fstat(fd), os.stat(candidate)
    except OSError:
        return None
    if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
        return None
    return candidate


class EmbeddedHashUnavailable(Exception):
    """A tool could not derive its embedded content hash for a file.

    Raised by ``embedded_hashes`` when the tool *should* be able to report a
    content hash for this file type but the attempt failed transiently (e.g.
    ``dolphin-tool verify`` timed out / exited non-zero / the binary is
    missing). The DAT-match path treats this as a non-cacheable error rather
    than collapsing to a meaningless file-level hash and caching a false
    "unmatched" result. Tools that legitimately have no embedded hash for a
    file (so a file-level SHA1 fallback is correct) return ``[]`` instead.
    """


@dataclass(frozen=True)
class ContentDigest:
    """A hash of the image content a file decodes to (the Round-trip check).

    ``kind`` names the shape of what was hashed (``"gc_wii_disc_sha1"``: SHA1
    of the whole GameCube/Wii disc image). Two digests are only comparable
    when their kinds agree: a CD's track hash and a disc-image hash can never
    prove anything about each other, so :meth:`same_content` refuses to answer
    rather than report a mismatch.
    """

    kind: str
    value: str
    # What to tell the user when this digest does not match the other side,
    # for a tool that knows a mismatch is expected rather than a sign of
    # damage (nsz rebuilding an XCI). Not part of the comparison.
    mismatch_reason: str = field(default="", compare=False)

    def same_content(self, other: ContentDigest) -> bool:
        if self.kind != other.kind:
            raise ValueError(
                f"cannot compare a {self.kind} digest with a {other.kind} digest",
            )
        return self.value == other.value


# The digest kind for "SHA1 of the image's bytes", shared by every tool whose
# Source is a plain image file and whose output decodes back to one (chdman
# DVD, maxcso, z3ds, JWUDTool). One kind is what lets a chain compare a CSO's
# decoded image with a CHD's data SHA1 (#301).
IMAGE_DATA_SHA1_KIND = "image_data_sha1"
# The same, SHA-256, for a tool whose two sides are both digests it makes
# itself (z3ds, JWUDTool): no outside SHA-1 to match, so SHA-1 is not used.
IMAGE_DATA_SHA256_KIND = "image_data_sha256"


class ContentDigestUnavailable(Exception):
    """A tool should have a content digest for this file but could not get one.

    The Round-trip check treats it as "not proven": the Source is kept. Distinct
    from ``content_digest`` returning ``None``, which means the tool has no
    digest for this kind of file at all.
    """


@runtime_checkable
class ToolPlugin(Protocol):
    id: str
    display_name: str
    binary_path: str
    modes: Sequence[ModeSpec]
    input_extensions: frozenset[str]    # convertible-from
    output_extensions: frozenset[str]   # produced ("output exists" badges + scan discovery)
    verify_extensions: frozenset[str]   # accepted by verify()
    # Extensions of this tool's files that are one disc of a multi-disc game on
    # their own (a .cue sheet, a .chd, an .iso), so an .m3u can list them. Empty
    # for a tool with no such files. Read by ToolRegistry.disc_extensions().
    disc_extensions: frozenset[str]
    # When the tool's embedded_hashes are *exhaustive*: a miss of every
    # reported content hash means the file definitively isn't in the DAT, so
    # the DAT matcher must NOT fall back to a file-level SHA1 of the container
    # (which can't be indexed anyway and would re-read the whole file). True
    # only for formats whose container bytes never appear in a DAT, e.g.
    # Dolphin's recompressed RVZ/WIA/GCZ. False (default) for formats whose own
    # file SHA1 may legitimately be indexed (e.g. a CHD-container DAT).
    embedded_hash_is_exhaustive: bool
    # The ``embedded_hashes`` labels whose value is the SHA1 of the raw disc
    # image the file holds (#388): what the duplicate finder's ``raw:`` key is
    # read from (:meth:`raw_image_sha1`). Empty for a tool with no such hash
    # (CSO, NSZ, WUX, Z3DS). Never a label that folds in anything else, such
    # as a CHD's overall ``chd_sha1``.
    raw_image_hash_labels: frozenset[str]
    # Whether that hash is computed by rebuilding the whole image from the
    # file's payload (dolphin-tool verify), so equal keys prove this file
    # decodes to the image. False when it is read from a header (a CHD's data
    # SHA1): equal keys then say only what the header claims, and the file is
    # trusted as a copy to keep only once a check has passed (#389).
    raw_image_hash_rebuilds_content: bool
    # Library-manager platform slugs whose media this tool handles, empty for
    # "no opinion". Part of the contract, not just BaseTool's default:
    # `ToolRegistry.narrow_to_platform` reads it off every registered tool, so
    # an implementation that satisfies the protocol without it would fail there
    # rather than at registration. See BaseTool.platform_slugs for the semantics.
    platform_slugs: frozenset[str]
    # The codec a job of this tool runs with when it needs one and none was
    # chosen (#345): a mode that requires a codec, or a level set with the
    # codec on "tool default". Read by :meth:`compression_for`. None means
    # "this tool names no default" -- see BaseTool.default_compression.
    default_compression: str | None
    # What to tell the user when ``is_ready()`` is False: why this tool cannot
    # run here. Auto shows it beside an Image it skipped for that reason.
    not_ready_reason: str
    # The same for ``verify_is_ready()``.
    verify_not_ready_reason: str
    # Whether this tool's ``info()`` runs its program (#488). True: the File
    # info route asks ``is_ready()`` first and, when it answers False, says
    # ``not_ready_message(tool)`` with a 503 instead of failing with a 500.
    # False: the read is done in Python (a header, an archive listing, the
    # file's size) and answers whether or not the tool could convert here.
    # Asked through ``ToolRegistry.tool_is_ready(tool, purpose="info")``.
    info_needs_tool: bool

    def spec(self, mode: str) -> ModeSpec:
        """Return the ModeSpec for a mode this tool owns."""

    def compression_for(self, mode: str, compression: str | None) -> str | None:
        """The compression a job of *mode* runs with, given what it chose (#345).

        Every job reaches its tool through this, whoever queued it (Auto, a
        single job, the CLI, a Rule), so the tool's default applies in one
        place. *compression* is ``"codec"``, ``"codec:level"``, ``":level"``
        (a level with the codec on "tool default"), or None/"" for nothing
        chosen. A chosen codec is returned as it is.
        """

    def output_path(
        self,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Resolve the output path for a conversion."""

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        """Detect an existing sibling output this tool could produce.

        Returns ``None`` when the tool cannot produce an output for this
        input or no output is present (neither finished nor mid-conversion).

        ``from_archive`` marks a *synthesised* path: the location an archive
        member would occupy once extracted, which does not exist yet. It is the
        same distinction ``output_path``'s ``treat_as_stem`` draws, and it
        matters wherever a tool's output name depends on the input's
        surroundings rather than on the input alone — a member never brings its
        siblings with it, so it can only ever produce a single-file output. Most
        tools swap a suffix and can ignore it.
        """

    def accepts_directory(self, path: str) -> bool:
        """Whether this tool can take ``path`` (a directory) as its input unit.

        The directory analogue of ``ext in input_extensions``. Default
        ``False``; a tool with an ``InputKind.DIRECTORY`` mode (makeps3iso
        folder->iso) overrides this to run its source-layout detector. May do
        disk I/O — call it off the event loop.
        """

    def converts_path(self, path: str) -> bool:
        """Whether this tool can convert a concrete file *on its own*.

        The input-side mirror of ``verifies_path``: the default is the shared
        whole-filename match against ``input_extensions`` (so a compound
        extension resolves), but a tool whose source is really a
        *set* of files overrides it so only the primary member is offered.
        JWUDTool's split Wii U dumps are the case — ``game_part1.wud`` through
        ``game_part12.wud`` are one disc image, and selecting part 7 alone can
        only fail. Listing code calls it so ``FileEntry.convertible_by`` marks
        the secondary members non-convertible while leaving them visible.
        May do disk I/O (it looks for the sibling primary) — it runs inside the
        threadpool scan.
        """

    def delete_on_verify_is_safe(self, mode: str, compression: str | None) -> bool:
        """Whether deleting the source after ``verify()`` is safe for this job.

        ``ModeSpec.supports_delete_on_verify`` says the *mode* can offer it;
        this says the *job* can, given its settings. It exists because a tool's
        verify step is not always a full content check: jwud's is a structural
        WUX walk (the format carries no checksums), which is only backed by a
        real byte-for-byte comparison because JWUDTool runs one during the
        conversion — and the job can turn that off with ``-noVerify``. Deleting
        a 25 GB source on the strength of a geometry check alone would risk the
        only copy, so jwud returns ``False`` for that combination and the route
        rejects it.

        Default ``True``: every other tool's verify reads the whole output.
        """

    def source_companions(self, path: str) -> list[str]:
        """Sibling files this input consumes *besides* ``path`` itself.

        The input-side mirror of ``companion_outputs``: a source that is really
        several files on disk (a split Wii U dump's ``game_part2.wud`` …
        ``game_part12.wud``) reports the rest of the set here, so
        delete-on-verify removes the whole source rather than orphaning the
        parts it didn't name. ``path`` itself is never included, and a tool
        whose inputs are single files returns ``[]``.

        Note ``.cue``/``.gdi`` track files are *not* routed through this hook
        today: they're parsed out of the file's contents by
        ``utils.delete_plan``, with its own unsafe-reference handling, rather
        than derived from naming. Folding those into this seam is the obvious
        next consolidation.
        """

    def header_platform(self, path: str, *, within=None) -> str | None:
        """The Platform this tool reads from ``path``'s own header, or None.

        How a tool recognises its Images. ``services.platform_detect`` asks
        every tool whose ``input_extensions`` match the file and maps the
        answer onto a Platform slug, so a new console is taught to detection
        by its tool, not by a central list. None means "not mine, or I cannot
        tell", never a guess. Reads a few bytes from disk; call it off the
        event loop.

        *within*: the trusted roots a track sheet's tracks resolve under, for
        a sheet outside the Volumes (an Inbox's unpack folder, #432). None,
        the default, is the configured Volumes. A tool whose answer reads no
        sheet ignores it.
        """

    def header_reads(self, path: str) -> list[str] | None:
        """Other files ``header_platform(path)`` reads, besides ``path``.

        A track sheet's answer comes from the tracks it names, so detection
        keys its cache on their size and mtime too, and does not cache an
        answer while one is missing. None means "cannot tell" (an unreadable
        sheet), which is never cached either. Default empty.
        """

    def claims_check(self, path: str) -> bool:
        """Whether a verify run must check ``path`` with this tool (#306).

        Wider than :meth:`verifies_path`, which gates the listing's Verify/Info
        row-actions: a file this tool would recognise if it were intact (a
        truncated ``.gba.zip``) is claimed, so the check runs and reports the
        damage instead of the file going unchecked. Defaults to
        ``verifies_path``.
        """

    def verifies_path(self, path: str) -> bool:
        """Whether this tool's verify/info applies to a concrete file.

        The per-file refinement of ``verify_extensions``: the default is a
        plain extension match, but a tool that claims a broad container
        extension yet only handles a narrow subset (romz claims ``.7z``/
        ``.zip`` but only verifies single-ROM archives it produced) overrides
        this with per-file inspection. Listing code calls it so the frontend
        gates the Verify/Info row-actions on what the tool can actually
        handle, not on extension alone.
        """

    def scannable_path(self, path: str) -> bool:
        """Whether the library metadata / DAT-match scan should read this file.

        ``scannable_extensions`` picks the walk by *type*; this vetoes an
        individual path within it. Default ``True`` — override only when a name
        matching a produced extension is provably not a standalone artifact, so
        hashing it can only ever waste I/O. Any tool answering ``False`` drops
        the path from the walk, since a file no tool considers scannable is one
        no DAT can match. It runs once per candidate across the whole library,
        so keep it cheap — a stat or two at most, never a read.
        """

    def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None = None,
        split: bool = False,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run a conversion, yielding ``{"progress", "message"}`` updates.

        ``split`` is honored only by tools whose mode declares it (makeps3iso
        folder->iso, ``-s`` 4 GB FAT32 split); other tools accept and ignore it,
        exactly as they do ``compression`` they don't use.

        Callers go through :meth:`run_convert`, which resolves *compression*
        first; ``convert`` receives what the job runs with.
        """

    def run_convert(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None = None,
        split: bool = False,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """:meth:`convert` with *compression* resolved by :meth:`compression_for`.

        The one way into a conversion: the job runner and every chain step call
        this, so a step a chain passes no codec (nkit_to_rvz's RVZ step) still
        gets its mode's default (CodeRabbit 4581672840).
        """

    async def verify(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        """Verify an output file; returns ``{"valid", "message"}``.

        ``cancel_event`` is the same event ``convert()`` takes, and it must be
        honored the same way: a job cancelled during the delete-on-verify verify
        stage has to stop the verifier, not wait it out. A run that ended
        because the event fired returns ``"cancelled": True`` alongside
        ``valid=False`` — it proved nothing, so no caller may record it as a
        verification failure (issue #266).

        Every implementation is also **bounded**: it ends within
        ``ToolPlugin.verify_timeout(path)`` whether or not it is cancelled.
        """

    def verify_stream(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Verify with streaming progress updates.

        Yields ``{"type": "progress"|"complete"|"error", ...}``; the terminal
        event carries ``valid`` and, on cancellation, ``cancelled``.
        """

    async def verify_timeout(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> int:
        """Wall-clock bound for verifying ``path``, in seconds (0 = unbounded).

        The tool's own verify already enforces this; it is exposed so the job
        pipeline can apply the *same* number as an outer backstop without
        knowing which knob a given tool reads. That matters because a tool whose
        verify never spawns a subprocess (jwud's container walk, makeps3iso's
        PARAM.SFO readback) has nothing for a subprocess timeout to bound, and
        because the bound is per-tool tunable — resolving it centrally with a
        shared default would silently override a per-tool override.
        """

    async def info(self, path: str) -> dict:
        """Return raw tool info for a file."""

    def info_model(self, raw: dict, path: str) -> BaseModel:
        """Map raw info into the typed API model."""

    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]:
        """Report verifiable hashes embedded in / derivable from ``path``.

        Each entry is ``(sha1_hex, match_type)``: a content SHA1 the file
        carries (CHD header / data SHA1, Dolphin disc SHA1, ...) plus a
        label describing where it came from. The DAT-match fast path tries
        these against the imported DATs before falling back to a full
        file-level SHA1. Return an empty list when the tool has no cheap or
        format-meaningful hash to offer (file-level SHA1 fallback is correct).

        Raise :class:`EmbeddedHashUnavailable` when the tool *should* yield a
        hash for this file type but the attempt failed transiently, so the
        caller skips the (meaningless) file-level fallback and does not cache
        a false negative.

        ``cancel_event`` lets a background job (metadata scan / DAT match)
        abort an expensive derivation (e.g. ``dolphin-tool verify``) promptly
        instead of blocking until the current file finishes.
        """

    async def raw_image_sha1(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> str | None:
        """The SHA1 of the raw disc image ``path`` holds, for the duplicate finder (#388).

        Read from :meth:`embedded_hashes`: the hash under one of
        :attr:`raw_image_hash_labels`. ``None`` when this file has no such hash
        (a format with no raw image hash, a CD CHD): it gets no content key
        and is reported as not checked. Raises
        :class:`EmbeddedHashUnavailable` when it should have one but could not
        get it this time; the caller stores nothing and asks again next run.
        The caller bounds the call with ``verify_timeout(path)``.
        """

    async def content_sha1(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
        fd: int | None = None,
    ) -> str | None:
        """The raw image SHA1 of what ``path`` holds now, from a full pass (#389).

        Never a cache and never a header alone: the tool reads the whole
        payload (dolphin-tool's verify rebuilds the disc; chdman's verify
        recomputes the data SHA1 and the header must agree). ``None`` when this
        tool has no full-content hash for the file, which then cannot be the
        copy a duplicate delete keeps. Raises :class:`EmbeddedHashUnavailable`
        when the pass did not give one. The caller bounds the call.

        With *fd* (the caller holds the file open), the pass reads that file,
        through :func:`held_file_path`, never the name, so nothing renamed into
        the name meanwhile is what gets hashed; with no such path it raises
        :class:`EmbeddedHashUnavailable` rather than read the name. *path* still
        decides the format (its extension).
        """

    async def content_digest(
        self,
        path: str,
        *,
        cancel_event: asyncio.Event | None = None,
        within: Sequence[str] | None = None,
    ) -> ContentDigest | None:
        """Hash of the image content ``path`` decodes to, for the Round-trip check.

        Called on both sides of a conversion whose mode declares
        ``ModeSpec.supports_round_trip``: once on the Source and once on the
        output. The job deletes the Source only when the two digests have the
        same ``kind`` and value. A claim about content, never container bytes:
        an ``.iso`` and the ``.rvz`` made from it have different file hashes but
        the same digest.

        Return ``None`` when this tool has no digest for this kind of file.
        Raise :class:`ContentDigestUnavailable` when it should have one but the
        attempt failed; the Source is then kept. A cancelled ``cancel_event``
        may surface as either; the caller checks the event itself. The caller
        bounds the call with ``verify_timeout(path)``.

        ``within`` is the trusted root(s) a sheet's track references may
        resolve under, as in ``post_convert``: the job's archive-extraction
        folder for an archive-member Source, ``None`` (the configured Volumes)
        otherwise. A tool that extracts into its own scratch folder passes that
        folder to its own sheet reads. A tool that reads no sheet ignores it.
        """

    async def verify_is_ready(self) -> bool:
        """Whether this tool can *check* a file here (#306).

        Readiness is per operation: a check can need less than a conversion
        (jwud's WUX check is pure Python and needs no JWUDTool or Java) or
        something else (z3ds's check needs ``zstd``). Asked through
        :meth:`ToolRegistry.tool_is_ready` with ``purpose="verify"``, so the
        same bound applies. Defaults to ``is_ready()``.
        """

    async def source_problem(
        self,
        path: str,
        *,
        cancel_event: asyncio.Event | None = None,
        within: Sequence[str] | None = None,
    ) -> str | None:
        """Why the Round-trip must not trust ``path`` as a Source, or None.

        Asked once, before any digest, under the same bound. A Source whose
        format carries its own checksum (a CHD for chdman Copy) is checked
        with it first: a Source that is not Intact would compare its damage
        with an output that faithfully copied it. Return a reason to keep the
        Source without comparing; ``BaseTool`` returns None.

        ``within`` is the same trusted root(s) ``content_digest`` gets for the
        Source: an override that reads a sheet, a track or an archive member
        passes it to that read and fails closed when the read is refused.
        """

    async def is_ready(self) -> bool:
        """Whether this tool's runtime prerequisites are met.

        A tool that cannot run at all in the current deployment — a missing
        user-supplied key file, an absent optional binary — reports ``False``
        so ``GET /api/tools`` lists it as unavailable and the frontend hides
        it entirely, rather than offering a converter that can only fail at
        job time. ``BaseTool``'s default checks that ``binary_path`` exists and
        is executable (#494); a tool with no binary is ready.

        Async because a readiness probe may touch the disk (nsz searches the
        configured volumes for ``prod.keys``); implementations that block
        should hop to a threadpool rather than stall the event loop.
        
        **Filesystem work here goes through ``run_detached``, never
        ``run_in_threadpool``.** Callers all fan out over the whole registry
        (``GET /api/tools``, the RomM platform list, every sweep) and bound the
        wait through :meth:`ToolRegistry.tool_is_ready` -- but a bound can only
        abandon the awaiter, never the OS thread. A probe wedged on an
        unresponsive mount therefore keeps whatever thread it was given, and
        one taken from the shared pool is never handed back, so repeated probes
        retire the pool a worker at a time until unrelated offloads have
        nowhere to run. A detached thread is disposable and costs nothing.
        """

    def active_pids(self) -> list[int]:
        """Return PIDs of in-flight subprocesses for this tool."""

    async def post_convert(
        self, input_path: str, output_path: str, mode: str,
        *, within: Sequence[str] | None = None,
    ) -> None:
        """Optional post-processing hook after a successful conversion.

        ``within`` is the trusted root(s) an archive-extracted source lives
        under (the job worker's extraction dir). None means the source is in a
        configured Volume.
        """

    def overwrite_targets(self, output_path: str, mode: str) -> list[str]:
        """Every path to clear before an **authorized** overwrite, primary first.

        The superset of ``companion_outputs``: it includes ``output_path``
        itself, and it enumerates artifacts a prior *failed* run may have left
        behind even when they can't coexist with a finished output (a
        makeps3iso ``-s`` build interrupted mid-split leaves both the
        not-yet-renamed base and some numbered parts). ``companion_outputs``
        describes a *successful* output set and is the wrong list to delete
        from; this one describes everything the overwrite must sweep.

        The job pipeline calls this instead of branching on tool identity, and
        rejects the whole overwrite if any entry exists as a non-file, so an
        override must list paths, never remove them.
        """

    def expected_output_size(self, input_path: str, mode: str) -> int | None:
        """Bytes this mode will write for ``input_path``, if cheaply knowable.

        Only for *preflight sizing* — the chain's disk-headroom check, which
        must hold source + full intermediate + partial final at once and cannot
        estimate the intermediate from a ratio when a container's shrink factor
        varies by an order of magnitude (a scrubbed Wii NKit, a highly
        compressed CSO). Return ``None`` when the answer isn't in a header the
        tool can read for free; callers fall back to ``ChainStep.output_ratio``.
        Never a correctness input, so an unreadable file is ``None``, not an
        error. Blocking (a small header read) — call it off the event loop.
        """

    def verify_target(self, output_path: str, mode: str) -> str | None:
        """The file to verify for a finished job, or None when there is none.

        Usually ``output_path`` itself. It differs where a mode's real product
        is not at the path the job planned: a split makeps3iso build writes
        ``<iso>.0``/``.1``/… and no bare ``.iso``, so verifying the planned path
        checks a file that does not exist and fails a conversion that worked.
        Asking the tool keeps that per-format knowledge in the plugin instead of
        the job runner.
        """

    def companion_outputs(self, output_path: str, mode: str) -> list[str]:
        """Sibling output paths this mode writes beside ``output_path``.

        A mode that produces more than one file (extractcd's ``.cue`` + ``.bin``
        sidecar, a split makeps3iso ``.iso.0``/``.1``/…) returns the *extra*
        paths here so conflict detection, overwrite cleanup, size accounting and
        in-use tracking enumerate them from one place instead of each re-encoding
        the per-mode suffix. ``output_path`` itself is never included.
        """


class BaseTool:
    """Shared helpers so concrete tools stay tiny."""

    id: str
    display_name: str
    modes: Sequence[ModeSpec] = ()
    output_extensions: frozenset[str] = frozenset()
    verify_extensions: frozenset[str] = frozenset()
    disc_extensions: frozenset[str] = frozenset()
    # The ``SubprocessRunner`` owner name this tool's service runs under, which
    # is what the shared nice/ioprio/timeout policy keys its per-tool overrides
    # on (``COMPRESSATORIUM_<OWNER>_*``). It is *not* always ``id``: the plugin
    # id is the routing/UI name ("dolphin", "cso") while the owner is the
    # historical service name ("dolphin_tool", "maxcso"). Declared here so
    # policy lookups stay registry-driven instead of re-deriving the mapping at
    # each call site. None means "shared defaults only".
    policy_owner: str | None = None
    # Default False: a tool whose container file SHA1 might be DAT-indexed
    # still falls back to a file-level hash after an embedded-hash miss.
    embedded_hash_is_exhaustive: bool = False
    # Default: no labelled hash is a raw disc image's SHA1 (#388).
    raw_image_hash_labels: frozenset[str] = frozenset()
    # Default: a raw image hash, where there is one, is read, not rebuilt.
    raw_image_hash_rebuilds_content: bool = False
    # Library-manager platform slugs (RomM's vocabulary, which is IGDB's) whose
    # media this tool handles. Purely *narrowing*: an extension match still
    # decides convertibility, but when a library manager tells us the platform,
    # this disambiguates what the extension cannot. A bare ``.iso`` is accepted
    # by chdman, dolphin and maxcso alike; only the platform says whether it is
    # a GameCube disc (RVZ) or a PS2 disc (CHD/CSO).
    #
    # Declared per tool, so adding a tool stays a one-module change and the
    # mapping never becomes an if-ladder in a route. Empty means "no platform
    # opinion" — such a tool is never filtered out, so an unknown or new RomM
    # slug degrades to today's extension-only behaviour rather than to an empty
    # list.
    platform_slugs: frozenset[str] = frozenset()
    # The codec to name when the operator asks for a compression *level* but
    # leaves the codec on "tool default".
    #
    # The level is not a separate argument anywhere in the pipeline: it rides
    # on the compression string as ``"codec:level"``, so a level with no codec
    # serialises as ``":19"``. Whether that is meaningful is a per-tool fact.
    # For a mode that offers no codec choice at all (nsz, whose dropdown picks
    # a solid/block *layout*) the empty part reads as "tool default" and the
    # level is honoured. For a mode that does offer codecs it is not a default
    # but a blank: dolphin-tool receives ``-c "" -l 19`` and refuses the job.
    # Declaring the default here lets the automation resolve it instead, per
    # tool, rather than an if-ladder keyed on tool identity at the call site.
    # Mirrors `defaultCompression` in `src/lib/tools/registry.js`, which seeds
    # the same choice in the manual picker.
    default_compression: str | None = None
    # Why ``is_ready()`` answered False, in the user's words. Tools gated on a
    # user-supplied file or an optional runtime say which one; the default
    # names the binary the shared probe found missing (#494).
    not_ready_reason: str = "its program is not available on this server"
    # Default True: an info read that runs the program needs it to be ready.
    # A tool whose ``info()`` reads the file in Python sets False, so its File
    # info keeps answering when the program is missing (#488).
    info_needs_tool: bool = True

    @property
    def verify_not_ready_reason(self) -> str:
        """Why ``verify_is_ready()`` answered False; the convert reason by default."""
        return self.not_ready_reason

    def __init__(self, binary_path: str) -> None:
        self.binary_path = binary_path

    @property
    def input_extensions(self) -> frozenset[str]:
        if not self.modes:
            return frozenset()
        return frozenset().union(*(m.input_extensions for m in self.modes))

    def spec(self, mode: str) -> ModeSpec:
        for m in self.modes:
            if m.mode == mode:
                return m
        raise KeyError(mode)

    def compression_for(self, mode: str, compression: str | None) -> str | None:
        # Only a mode with a codec picker can be missing a codec. Where a mode
        # has none (nsz picks a solid/block *layout*) the empty part of ":18"
        # reads as the tool's own layout and is kept.
        spec = self.spec(mode)
        codec, _, level = (compression or "").partition(":")
        if level and spec.takes_no_level(codec):
            # A level for a codec that takes none (WIA's default, none): the
            # routes refuse it, so this is a job queued before they did. It
            # runs as the codec alone, which is what dolphin-tool would do.
            logger.warning(
                "%s: %s takes no compression level; ignoring %s",
                mode, codec or spec.default_compression, level,
            )
            return codec or spec.default_compression
        if codec and not level and spec.level_range(codec):
            # A chosen codec with no level gets that codec's own default level
            # (dolphin-tool needs one, and its range depends on the codec).
            return f"{codec}:{spec.level_range(codec)[2]}"
        if codec or not spec.supports_compression:
            return compression
        if not (spec.requires_codec or level):
            # Nothing chosen and nothing needed: the tool picks (chdman's own
            # default is better than naming one).
            return compression or None
        # The mode's own default first (RVZ and WIA differ), then the tool's.
        default = spec.default_compression or self.default_compression
        if not default:
            if level:
                # A level cannot be sent without a codec, and inventing one
                # would convert with a setting nobody chose: drop it, say so.
                logger.warning(
                    "%s needs a codec alongside a compression level and declares no "
                    "default; ignoring the level", mode,
                )
            return None
        if level:
            return f"{default}:{level}"
        levels = spec.level_range(default)
        return f"{default}:{levels[2]}" if levels else default

    def run_convert(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None = None,
        split: bool = False,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        # compression_for is idempotent on what it returns, so a job resolved
        # at queue time (_queue_job_locked) resolves to the same value here.
        return self.convert(
            input_path, output_path, mode,
            compression=self.compression_for(mode, compression),
            split=split, cancel_event=cancel_event,
        )

    def refuse_unsupported_compression(self, mode: str, compression: str | None) -> None:
        """Raise ``ValueError`` for what *mode* refuses: a codec outside
        ``ModeSpec.codecs``, or a level outside that codec's range. Nothing is
        clamped. A no-op when the mode names no codecs, or nothing was chosen.
        """
        problem = compression_problem(self.spec(mode), compression)
        if problem:
            raise ValueError(problem)

    @staticmethod
    def _basic_info_fields(raw: dict) -> dict:
        """The shared ``BasicFileInfo`` fields pulled from a tool's raw info dict.

        The five "what is this file" tools (z3ds, nsz, cso, romz, makeps3iso)
        build their info model from this, so the identical ``raw[...] -> field``
        mapping lives in one place. Required fields are indexed directly (the
        services always populate them); the optional pair uses ``.get`` so a
        missing key becomes ``None`` rather than a KeyError.
        """
        return {
            "file": raw["file"],
            "size": raw["size"],
            "size_display": raw["size_display"],
            "format": raw.get("format"),
            "extension": raw["extension"],
            "compressed": raw["compressed"],
            "compression_type": raw.get("compression_type"),
        }

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        return None

    def accepts_directory(self, path: str) -> bool:
        # Default: file-only tool. Directory-input tools (makeps3iso) override.
        return False

    def converts_path(self, path: str) -> bool:
        # Default: the same whole-filename `input_extensions` match the listing
        # used before this hook existed, so a compound extension still resolves
        # (nkit2iso's .nkit.iso, whose Path.suffix is the generic .iso). Tools
        # whose source is a *set* of files (jwud's split Wii U dumps) override it
        # so only the primary is offered; every other tool inherits the match.
        return match_extension(path, self.input_extensions) is not None

    def source_companions(self, path: str) -> list[str]:
        # Default: a single-file source consumes nothing else.
        return []

    def delete_on_verify_is_safe(self, mode: str, compression: str | None) -> bool:
        # Default: this tool's verify() reads the whole output, so passing it
        # is enough to justify removing the source. Only a tool whose verify
        # is weaker than its conversion-time check (jwud) overrides this.
        return True

    async def verify_timeout(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> int:
        # Default: the shared size-scaled verify bound, resolved for this tool's
        # policy owner so a per-tool COMPRESSATORIUM_<OWNER>_VERIFY_TIMEOUT
        # override applies here exactly as it does inside the tool's own verify.
        # Sizing the file is itself a stat, so the cancel event is raced against
        # it: this runs before the verify that would otherwise observe it.
        return await resolve_verify_timeout(
            path, self.policy_owner, cancel_event=cancel_event,
        )

    async def prime_embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Make :meth:`embedded_hashes` able to answer for *path*, a file no
        scan has seen (an Inbox's bound copy, #432): read and cache what it
        reads, as the library scan would. Default: nothing to prime. Raises
        ``ReadCancelled`` on a cancel; any other failure leaves the match to
        fall back to the file's own hash."""
        return None

    def header_platform(self, path: str, *, within=None) -> str | None:
        # Default: no header probe. Tools that can tell their consoles apart
        # from the image bytes (chdman, dolphin, xiso) override it.
        return None

    def header_reads(self, path: str) -> list[str] | None:
        # Default: the probe reads only the file itself.
        return []

    def claims_check(self, path: str) -> bool:
        # Default: the listing's own answer.
        return self.verifies_path(path)

    def verifies_path(self, path: str) -> bool:
        # Default: a declared-extension match (suffix-based, so a compound
        # extension resolves — see utils.path_utils.match_extension). Tools that
        # over-claim a container extension (e.g. romz on .7z/.zip) override with
        # per-file inspection.
        return match_extension(path, self.verify_extensions) is not None

    def scannable_path(self, path: str) -> bool:
        # Default: every path the extension walk found is worth scanning, the
        # rule the scan used before this hook existed. Only a tool that knows a
        # name cannot be a standalone artifact (a jwud split part) vetoes one.
        return True

    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]:
        # Default: no embedded hash; callers fall back to file-level SHA1.
        return []

    async def raw_image_sha1(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> str | None:
        # Default: read the label, never guess. A tool with no raw label has
        # no raw image hash at all, and is not asked for its hashes.
        if not self.raw_image_hash_labels:
            return None
        found = {
            sha1.strip().lower()
            for sha1, label in await self.embedded_hashes(path, cancel_event=cancel_event)
            if label in self.raw_image_hash_labels and sha1
        }
        if len(found) != 1:
            # None came back this time (over a size cap, a verify that printed
            # nothing), or two that disagree: no key now, and none stored.
            raise EmbeddedHashUnavailable(
                f"no single raw image hash for {Path(path).name} ({len(found)} found)",
            )
        return found.pop()

    async def content_sha1(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
        fd: int | None = None,
    ) -> str | None:
        # The binding to the held file lives here, once for every tool (Codex
        # 4593632748): each tool's own pass (`_content_sha1`) reads *target*.
        target = path
        if fd is not None:
            from services.subprocess_runner import run_detached_bounded

            target = await run_detached_bounded(held_file_path, fd, timeout=30.0)
            if target is None:
                raise EmbeddedHashUnavailable(
                    f"no path here names the held {Path(path).name}, so it cannot "
                    "be read in full without reading its name instead",
                )
        return await self._content_sha1(path, target, cancel_event=cancel_event)

    async def _content_sha1(
        self, path: str, target: str, *, cancel_event: asyncio.Event | None = None,
    ) -> str | None:
        """:meth:`content_sha1`'s pass: *path* names the format, *target* is
        what is read. Default: a tool whose raw hash rebuilds the image
        computes it afresh on every call (no cache), so that is a full pass;
        any other has none."""
        if not self.raw_image_hash_rebuilds_content:
            return None
        return await self.raw_image_sha1(target, cancel_event=cancel_event)

    async def source_problem(
        self,
        path: str,
        *,
        cancel_event: asyncio.Event | None = None,
        within: Sequence[str] | None = None,
    ) -> str | None:
        # Default: a Source carries no checksum of its own to check first.
        return None

    async def content_digest(
        self,
        path: str,
        *,
        cancel_event: asyncio.Event | None = None,
        within: Sequence[str] | None = None,
    ) -> ContentDigest | None:
        # Default: no content digest. Such a tool's modes leave
        # ModeSpec.supports_round_trip False, so their jobs keep the container
        # check and say Intact.
        return None

    async def is_ready(self) -> bool:
        # Default: the configured binary is there and executable (#494). A
        # tool with no binary (``""``) has nothing to check. Tools gated on
        # something else override this (nsz / prod.keys). Detached, per
        # ``ToolPlugin.is_ready``: a stat on a dead mount must not hold a
        # pooled worker.
        if not self.binary_path:
            return True
        problem = await run_detached(binary_problem, self.binary_path)
        if problem is not None:
            self.not_ready_reason = problem
        return problem is None

    async def verify_is_ready(self) -> bool:
        # Default: a check needs what a conversion needs.
        return await self.is_ready()

    def expected_output_size(self, input_path: str, mode: str) -> int | None:
        # Default: unknown. Tools whose source header states the output size
        # (maxcso's CSO/ZSO/DAX, nkit2iso's NKit) override it so the chain
        # preflight sizes the intermediate exactly instead of by ratio.
        return None

    async def post_convert(
        self, input_path: str, output_path: str, mode: str,
        *, within: Sequence[str] | None = None,
    ) -> None:
        return None

    def verify_target(self, output_path: str, mode: str) -> str | None:
        """Default: the job's own output path, which is what nearly every mode
        produces. Overridden where the product can live elsewhere."""
        return output_path

    def companion_outputs(self, output_path: str, mode: str) -> list[str]:
        # Default: derive the sibling paths from the mode's ``companion_exts``
        # as suffix swaps off ``output_path`` (e.g. extractcd ``.cue`` -> the
        # ``.bin`` data track). Pure path math, no disk I/O, returned regardless
        # of on-disk existence — callers guard existence/lock as needed. Tools
        # whose companion set is dynamic (makeps3iso's size-dependent split
        # parts) override this with their own disk-aware probe.
        exts = self.spec(mode).companion_exts
        return [str(Path(output_path).with_suffix(ext)) for ext in exts]

    def overwrite_targets(self, output_path: str, mode: str) -> list[str]:
        # Default: the finished output set — the primary plus its companions.
        # Correct for every mode whose failed runs leave nothing a successful
        # run wouldn't. makeps3iso overrides (a mid-split failure can leave the
        # base *and* numbered parts, which never coexist on success).
        return [output_path, *self.companion_outputs(output_path, mode)]


def binary_problem(path: str) -> str | None:
    """Why the binary at *path* can't run, or None. Blocking: a stat."""
    resolved = path if os.sep in path else shutil.which(path)
    if resolved is None or not os.path.isfile(resolved):
        return f"{path} was not found"
    if not os.access(resolved, os.X_OK):
        return f"{resolved} is not executable"
    return None


def not_ready_message(tool: ToolPlugin, *, purpose: str = "convert") -> str:
    """Why *tool* can't run here, as every caller says it (#484).

    ``"<display name> isn't ready: <reason>"``, the reason being the plugin's
    own: ``verify_not_ready_reason`` for *purpose* ``"verify"`` (as
    :meth:`ToolRegistry.tool_is_ready` takes it), else ``not_ready_reason``
    (``"convert"``, and ``"info"``, which asks the same ``is_ready()``).
    One place, so the CLI, Auto's plan, the Rules sweep and the File info
    routes say the same thing.

    Read it after the readiness check that answered False: a plugin may learn
    its reason while it probes (rom-converto keeps the last probe's). Reading
    it is an attribute read, never I/O, so it is safe on the event loop. A
    reason that can't be read, or is empty, gives the base default rather
    than raising into the caller.
    """
    try:
        reason = (
            tool.verify_not_ready_reason if purpose == "verify" else tool.not_ready_reason
        )
    except Exception:  # a broken reason must not fail the caller that asked
        logger.warning("could not read %s's not-ready reason", tool.id, exc_info=True)
        reason = None
    if not isinstance(reason, str) or not reason.strip():
        reason = BaseTool.not_ready_reason
    return f"{getattr(tool, 'display_name', None) or tool.id} isn't ready: {reason}"
