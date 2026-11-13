"""extract-xiso service: trim an Xbox / Xbox 360 disc image to an XISO.

A full ("Redump-style") Xbox disc image carries far more than the game: a video
partition, a security sector region, and a game partition padded out to the
full disc size. ``extract-xiso -r`` (XboxDev/extract-xiso, BSD-style license)
reads the XDVDFS filesystem out of the game partition and writes a fresh image
holding only that filesystem, laid out contiguously. The result is the "XISO"
that xemu, Xenia (and its Android ports), and modded consoles load directly,
typically 20-40% smaller than the source.

One direction only: an XISO cannot be turned back into the original disc image
(the dropped partitions are gone), so there is no "expand" mode, and this tool
never offers to delete the source after a verify (see the plugin docstring).

**The source is never handed to extract-xiso directly.** Its rewrite mode
unconditionally renames its input to ``<input>.old`` before reading it, and on a
failure it does not rename it back — the one behaviour a library-wide converter
can least afford. So the service gives the binary a *symlink* to the source in a
private scratch directory: ``rename(2)`` on a symlink renames the link, never
its target, so the user's file is only ever opened for reading. The rewritten
image lands in a hidden work directory beside the final output and is moved into
place (same filesystem, so atomically) only once the binary has exited cleanly.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import struct
import tempfile
import threading
import uuid
from collections.abc import AsyncGenerator
from functools import lru_cache
from pathlib import Path

from config import settings
from logging_setup import get_logger
from services.subprocess_runner import (
    ConversionCancelled,
    ReadCancelled,
    SubprocessRunner,
    collect_verify,
    ioprio_prefix,
    nice_prefix,
    remove_partial_output,
    remove_partial_tree,
    run_detached,
    run_detached_within_stall_bound,
    verify_preflight,
)
from utils.path_utils import match_extension

_OWNER = "xiso"
# The scratch link dir and the hidden work dir hold paths into the user's
# library, so only the service user may list them (what ``mkdtemp`` gives).
_PRIVATE_DIR_MODE = 0o700

# Every Xbox / Xbox 360 disc image is an ``.iso``; content decides whether a
# given ``.iso`` is one (see ``is_convertible``). CHDMAN, Dolphin and maxcso
# claim the same suffix for their own systems, and the user picks the tool.
XISO_CONVERTIBLE_EXTENSIONS = {".iso"}

# The product. Compound, like NKit's ``.nkit.iso``: the trailing ``.iso`` keeps
# every emulator and dashboard happy, while ``.xiso`` (a) stops the output from
# ever being named after its own source and (b) gives the verify route a claim
# that cannot hijack the plain ``.iso`` rows the other tools own.
XISO_OUTPUT_EXTENSION = ".xiso.iso"
XISO_VERIFY_EXTENSIONS = {XISO_OUTPUT_EXTENSION}

# --- XDVDFS layout -------------------------------------------------------
#
# These are the constants extract-xiso itself probes (extract-xiso.c,
# ``verify_xiso``). The filesystem's volume descriptor sits 0x10000 bytes into
# the game partition; where the game partition starts depends on the disc
# generation. An already-trimmed XISO has the game partition at offset 0.
_SECTOR = 2048
_HEADER_OFFSET = 0x10000
_MAGIC = b"MICROSOFT*XBOX*MEDIA"
_LAYOUTS: tuple[tuple[int, str, str | None], ...] = (
    # (game partition offset, layout label, console when the layout implies it)
    (0x00000000, "XISO", None),
    (0x0FD90000, "XGD2", "Xbox 360"),
    (0x02080000, "XGD3", "Xbox 360"),
    (0x18300000, "XGD1", "Xbox"),
)
# extract-xiso stamps the images it writes with this tag (followed by its
# version) and skips rewriting any image that already carries it. Absolute file
# offset, matching XISO_OPTIMIZED_TAG_OFFSET / _LENGTH_MIN upstream.
_OPTIMIZED_TAG_OFFSET = 31337
_OPTIMIZED_TAG = b"in!xiso"

_ATTR_DIRECTORY = 0x10
_ENTRY_HEADER = struct.Struct("<HHIIBB")  # left, right, sector, size, attr, name_len

# Hard ceilings on what a directory walk will believe. Every number below comes
# out of the file, so every number is bounded before it drives a read or a
# recursion: a crafted image must produce a clean "not an Xbox image", never a
# multi-gigabyte allocation or a stack overflow. Real discs sit orders of
# magnitude under each (Halo 3: 167 entries, deepest directory 3 levels).
_MAX_DIR_TABLE = 64 * 1024 * 1024
_MAX_ENTRIES = 1_000_000
_MAX_DEPTH = 64

# One progress line per file written: ``adding /maps/a.map (5000000 bytes) [OK]``.
# Directories report ``(0 bytes)`` and contribute nothing, which is correct.
_ADDING_RE = re.compile(r"^adding .* \((\d+) bytes\) \[OK\]\s*$")
# Printed (with exit 0, and no output written) when the input is already one.
_ALREADY_OPTIMIZED = "is already optimized"

logger = get_logger("extract_xiso")


class XisoImageError(ValueError):
    """``path`` is not a readable Xbox / Xbox 360 disc image."""


def _format_size(size: int) -> str:
    size_mb = size / (1024 * 1024)
    return f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"


def detect_layout(path: str) -> tuple[int, str, str | None]:
    """Locate the XDVDFS game partition of ``path``.

    Returns ``(partition_offset, layout, console_or_None)``. Blocking (up to
    four 20-byte reads); call it off the event loop. Raises
    :class:`XisoImageError` when no layout matches — which is the honest answer
    for every PSP, PS2, GameCube or plain data ``.iso``.
    """
    file_size = os.path.getsize(path)
    with open(path, "rb") as handle:
        for offset, layout, console in _LAYOUTS:
            probe = offset + _HEADER_OFFSET
            if probe + len(_MAGIC) > file_size:
                continue
            handle.seek(probe)
            if handle.read(len(_MAGIC)) == _MAGIC:
                return offset, layout, console
    raise XisoImageError("Not an Xbox / Xbox 360 disc image (no XDVDFS volume found)")


def is_optimized(path: str) -> bool:
    """Whether ``path`` already carries extract-xiso's rewrite tag."""
    with open(path, "rb") as handle:
        handle.seek(_OPTIMIZED_TAG_OFFSET)
        return handle.read(len(_OPTIMIZED_TAG)) == _OPTIMIZED_TAG


def walk_image(path: str) -> dict:
    """Walk the XDVDFS directory tree of ``path`` without extracting anything.

    Returns ``file_count``, ``data_bytes`` (the sum of every file's size, which
    is what extract-xiso's per-file progress lines add up to), ``max_extent``
    (the furthest byte any file reaches, relative to the start of the file),
    the root executable's name, and whether a ``$SystemUpdate`` folder exists.
    Blocking; call it off the event loop. Raises :class:`XisoImageError` on any
    structural inconsistency.
    """
    offset, layout, console = detect_layout(path)
    file_size = os.path.getsize(path)
    stats = {
        "layout": layout,
        "console": console,
        "partition_offset": offset,
        "file_count": 0,
        "dir_count": 0,
        "data_bytes": 0,
        "max_extent": 0,
        "root_executable": None,
        "has_system_update": False,
    }
    entries_seen = 0
    visited: set[int] = set()

    with open(path, "rb") as handle:
        handle.seek(offset + _HEADER_OFFSET + len(_MAGIC))
        raw = handle.read(8)
        if len(raw) < 8:
            raise XisoImageError("Truncated XDVDFS volume descriptor")
        root_sector, root_size = struct.unpack("<II", raw)
        if not root_sector or not root_size:
            raise XisoImageError("XDVDFS volume contains no files")

        def read_table(sector: int, size: int) -> bytes:
            start = offset + sector * _SECTOR
            if size > _MAX_DIR_TABLE or start + size > file_size:
                raise XisoImageError("Directory table lies outside the image")
            handle.seek(start)
            table = handle.read(size)
            if len(table) < size:
                raise XisoImageError("Truncated directory table")
            return table

        def walk_dir(sector: int, size: int, depth: int, at_root: bool) -> None:
            nonlocal entries_seen
            if depth > _MAX_DEPTH:
                raise XisoImageError("Directory tree is implausibly deep")
            if sector in visited:
                raise XisoImageError("Directory tree contains a cycle")
            visited.add(sector)
            table = read_table(sector, size)
            # The table is a binary tree of entries; left/right are offsets in
            # 4-byte units from the start of the table. Walk it iteratively so
            # a pathological tree shape cannot exhaust the stack.
            pending = [0]
            seen_offsets: set[int] = set()
            # Child directories are walked after this table is released, so
            # only one table is ever held: recursing from inside the loop would
            # keep every ancestor's table alive, up to _MAX_DEPTH x
            # _MAX_DIR_TABLE bytes for a crafted image.
            children: list[tuple[int, int]] = []
            while pending:
                pos = pending.pop()
                if pos in seen_offsets:
                    continue
                seen_offsets.add(pos)
                if pos + _ENTRY_HEADER.size > len(table):
                    raise XisoImageError("Directory entry runs past its table")
                left, right, f_sector, f_size, attr, name_len = (
                    _ENTRY_HEADER.unpack_from(table, pos)
                )
                if left == 0xFFFF:
                    # Unused slot / sector padding: not an entry.
                    continue
                name_end = pos + _ENTRY_HEADER.size + name_len
                if name_end > len(table):
                    raise XisoImageError("Directory entry name runs past its table")
                name = table[pos + _ENTRY_HEADER.size:name_end].decode(
                    "latin-1", errors="replace",
                )
                entries_seen += 1
                if entries_seen > _MAX_ENTRIES:
                    raise XisoImageError("Implausibly many directory entries")
                if left:
                    pending.append(left * 4)
                if right:
                    pending.append(right * 4)

                if attr & _ATTR_DIRECTORY:
                    stats["dir_count"] += 1
                    if at_root and name.lower() == "$systemupdate":
                        stats["has_system_update"] = True
                    if f_size:
                        children.append((f_sector, f_size))
                    continue

                stats["file_count"] += 1
                stats["data_bytes"] += f_size
                extent = offset + f_sector * _SECTOR + f_size
                stats["max_extent"] = max(stats["max_extent"], extent)
                if at_root and name.lower() in {"default.xex", "default.xbe"}:
                    stats["root_executable"] = name
            del table
            for child_sector, child_size in children:
                walk_dir(child_sector, child_size, depth + 1, at_root=False)

        walk_dir(root_sector, root_size, 0, at_root=True)

    if not stats["console"] and stats["root_executable"]:
        # An already-trimmed XISO has no layout to tell the generations apart,
        # but the boot executable does: .xbe is original Xbox, .xex is 360.
        stats["console"] = (
            "Xbox 360" if stats["root_executable"].lower().endswith(".xex") else "Xbox"
        )
    return stats


@lru_cache(maxsize=4096)
def _probe_convertible(path: str, size: int, mtime_ns: int) -> bool:
    """Content check behind ``is_convertible``, cached on (path, size, mtime).

    The listing calls ``converts_path`` for every ``.iso`` it shows, so this
    keeps a re-browse of a large library to one stat per file. ``size`` and
    ``mtime_ns`` are only cache keys: a replaced file re-probes.
    """
    try:
        detect_layout(path)
        return not is_optimized(path)
    except (XisoImageError, OSError):
        return False


class ExtractXisoService:
    """Wrapper for the ``extract-xiso`` binary."""

    def __init__(self) -> None:
        self.extract_xiso_path = settings.extract_xiso_path
        self._runner = SubprocessRunner(owner=_OWNER)

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    def abandoned_pids(self) -> list[int]:
        """Children that outlived SIGKILL; see ``SubprocessRunner``."""
        return self._runner.abandoned_pids()

    # --- source / output naming -----------------------------------------

    @staticmethod
    def is_convertible(path: str) -> bool:
        """Whether ``path`` is an untrimmed Xbox / Xbox 360 image.

        Extension first (cheap), then content: an XDVDFS volume at one of the
        known partition offsets, and no extract-xiso tag (an image this tool
        already wrote would only be skipped by the binary). Never raises.
        """
        if match_extension(path, XISO_CONVERTIBLE_EXTENSIONS) is None:
            return False
        if match_extension(path, XISO_VERIFY_EXTENSIONS) is not None:
            return False
        try:
            st = os.stat(path)
        except OSError:
            return False
        return _probe_convertible(path, st.st_size, st.st_mtime_ns)

    @staticmethod
    def _output_stem(name: str) -> str:
        """``Game.iso`` -> ``Game`` (and ``Game.xiso.iso`` -> ``Game``)."""
        matched = match_extension(
            name, XISO_VERIFY_EXTENSIONS | XISO_CONVERTIBLE_EXTENSIONS,
        )
        if matched is not None:
            return name[: -len(matched)]
        return str(Path(name).with_suffix(""))

    def get_output_path(
        self,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        input_p = Path(input_path)
        filename = f"{self._output_stem(input_p.name)}{XISO_OUTPUT_EXTENSION}"
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    def get_output_path_for_mode(
        self,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        return self.get_output_path(
            input_path, output_dir, treat_as_stem=treat_as_stem,
        )

    # --- info ---------------------------------------------------------------

    def info(self, file_path: str) -> dict:
        """Describe an Xbox / Xbox 360 image from its own filesystem.

        Synchronous (a header probe plus a directory walk — kilobytes, not the
        image); the plugin threadpools it. Raises :class:`XisoImageError` for a
        file that is not an Xbox image.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        file_size = os.path.getsize(file_path)
        stats = walk_image(file_path)
        trimmed = stats["layout"] == "XISO"
        optimized = trimmed and is_optimized(file_path)
        data = stats["data_bytes"]
        return {
            "file": file_path,
            "size": file_size,
            "size_display": _format_size(file_size),
            "format": (
                f"XISO ({stats['console']})" if trimmed and stats["console"]
                else "XISO" if trimmed
                else f"{stats['console']} disc image ({stats['layout']})"
            ),
            "extension": (
                match_extension(file_path, XISO_VERIFY_EXTENSIONS)
                or Path(file_path).suffix.lower()
            ),
            "compressed": trimmed,
            "compression_type": "Trimmed (XISO)" if trimmed else None,
            "console": stats["console"],
            "layout": stats["layout"],
            "file_count": stats["file_count"],
            "data_size": data,
            "data_size_display": _format_size(data),
            # Share of the image that is game data. On a full disc image this
            # is roughly what trimming keeps; on an XISO it is near 100%.
            "data_ratio": f"{data / file_size * 100:.1f}%" if file_size else None,
            "has_system_update": stats["has_system_update"],
            "trimmed": trimmed,
            "optimized": optimized,
        }

    # --- convert ------------------------------------------------------------

    def _build_command(self, source_link: str, out_dir: str) -> list[str]:
        """``extract-xiso -r -d <out_dir> <source_link>``.

        Options must precede the image path: extract-xiso stops parsing options
        at the first operand, so a trailing ``-d`` is silently ignored and the
        rewrite lands in the current directory instead. ``-D`` (delete the
        source) is never passed. ``nice`` is a command wrapper rather than a
        ``preexec_fn`` (the runner is told so via ``nice_via_wrapper``): this
        process runs detached reader threads, and forking a Python callable in
        a multithreaded process can deadlock the child before exec.
        """
        return nice_prefix(_OWNER) + ioprio_prefix(_OWNER) + [
            self.extract_xiso_path, "-r", "-d", out_dir, source_link,
        ]

    @staticmethod
    def _workspace_paths(output_path: str) -> tuple[str, str]:
        """Choose ``(link_dir, out_dir)`` before anything is created.

        Chosen up front, in the caller, so the caller can always sweep them --
        even when the setup that creates them fails half-way or is abandoned
        on a cancel. The scratch link dir lives under the app's temp dir (a
        container-local filesystem, where symlinks always work); the work dir
        sits beside the final output, hidden like nsz's / JWUDTool's
        ``.nsz-*`` / ``.jwud-*`` dirs, so the last step is a same-filesystem
        rename, never a multi-gigabyte copy.
        """
        token = uuid.uuid4().hex[:12]
        base = settings.temp_dir or tempfile.gettempdir()
        out_parent = os.path.dirname(os.path.abspath(output_path))
        return (
            os.path.join(base, f"xiso_src_{token}"),
            os.path.join(out_parent, f".xiso-{token}"),
        )

    @staticmethod
    def _remove_workspace(link_dir: str, out_dir: str) -> None:
        for directory in (link_dir, out_dir):
            shutil.rmtree(directory, ignore_errors=True)

    @classmethod
    def _prepare_workspace(
        cls,
        input_path: str,
        link_dir: str,
        out_dir: str,
        abandoned: threading.Event,
    ) -> str:
        """Create the scratch symlink and the work dir; return the link path.

        All or nothing: any failure removes whatever this call already created
        before re-raising. Blocking, so the caller runs it detached, and if the
        caller gives up on it (a cancel) it sets ``abandoned``: a call that
        finishes after that undoes its own work, since the caller's sweep may
        already have run.
        """
        # extract-xiso derives the output name by stripping ".iso", so the link
        # must end in it; a fixed name also keeps odd characters in the user's
        # filename away from the binary entirely.
        source_link = os.path.join(link_dir, "source.iso")
        try:
            os.makedirs(os.path.dirname(link_dir), exist_ok=True)
            # 0o700, as ``mkdtemp`` would make them: the names are chosen up
            # front (see `_workspace_paths`), so the mode is set here instead.
            os.mkdir(link_dir, _PRIVATE_DIR_MODE)
            os.symlink(os.path.abspath(input_path), source_link)
            os.makedirs(os.path.dirname(out_dir), exist_ok=True)
            os.mkdir(out_dir, _PRIVATE_DIR_MODE)
        except BaseException:
            cls._remove_workspace(link_dir, out_dir)
            raise
        if abandoned.is_set():
            cls._remove_workspace(link_dir, out_dir)
        return source_link

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "xiso_rewrite",
        *,
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Trim ``input_path`` to an XISO at ``output_path``, yielding progress.

        Progress is real, not estimated: the directory walk gives the total game
        data up front, and extract-xiso prints one ``adding … (N bytes) [OK]``
        line per file as it writes. Honors ``cancel_event`` and the shared stall
        timeout through ``SubprocessRunner.run``.
        """
        # Re-checked here because the file may have changed since planning; off
        # the event loop, like every other read of the source volume. These
        # reads run before the subprocess's stall watchdog exists, so each is
        # bounded by the same no-progress timeout, and a cancel during any of
        # them is reported as a cancel, not a failed read.
        try:
            convertible = await run_detached_within_stall_bound(
                self.is_convertible, input_path,
                what="Reading the Xbox image header", cancel_event=cancel_event,
            )
        except ReadCancelled as exc:
            raise ConversionCancelled("Conversion cancelled") from exc
        if not convertible:
            raise ValueError(
                f"Not an untrimmed Xbox / Xbox 360 disc image: {input_path}",
            )

        yield {"progress": 1, "message": "Reading the Xbox filesystem..."}
        try:
            stats = await run_detached_within_stall_bound(
                walk_image, input_path,
                what="Reading the Xbox filesystem", cancel_event=cancel_event,
            )
        except ReadCancelled as exc:
            raise ConversionCancelled("Conversion cancelled") from exc
        total = max(int(stats["data_bytes"]), 1)
        label = f"{stats['console'] or 'Xbox'} {stats['layout']}"

        link_dir, out_dir = self._workspace_paths(output_path)
        abandoned = threading.Event()
        # Set by the worker thread itself, immediately before it calls
        # os.replace, so it is true only once this job has really begun
        # writing output_path. A flag set here before the await could not tell
        # a rename that started from a cancel that landed first.
        renaming = threading.Event()
        written = 0

        def place_output(src: str) -> None:
            renaming.set()
            os.replace(src, output_path)

        def parse_progress(line: str) -> int | None:
            nonlocal written
            match = _ADDING_RE.match(line.strip())
            if not match:
                return None
            written += int(match.group(1))
            # 2..98: the walk was the first slice, the move into place the last.
            return 2 + min(96, written * 96 // total)

        try:
            source_link = await run_detached_within_stall_bound(
                self._prepare_workspace, input_path, link_dir, out_dir, abandoned,
                what="Preparing the XISO work directory", cancel_event=cancel_event,
            )
            rewritten = os.path.join(out_dir, "source.iso")
            cmd = self._build_command(source_link, out_dir)
            yield {"progress": 2, "message": f"Trimming {label} image to XISO..."}

            async for update in self._runner.run(
                cmd,
                input_path=input_path,
                output_path=rewritten,
                parse_progress=parse_progress,
                initial_progress=2,
                cancel_event=cancel_event,
                fail_label="extract-xiso",
                complete_message="XISO written",
                require_output=True,
                mode=mode,
                nice_via_wrapper=True,
            ):
                if update.get("progress", 0) >= 100:
                    # Hold the bar short of done until the file is in place.
                    update = {**update, "progress": 98}
                yield update

            # Bounded like the reads before the subprocess: the child has exited,
            # so its stall watchdog no longer covers this step, and the output
            # volume is the one most likely to have just failed.
            await run_detached_within_stall_bound(
                place_output, rewritten,
                what="Placing the XISO output", cancel_event=cancel_event,
            )
        except (
            ConversionCancelled, ReadCancelled, RuntimeError,
            asyncio.CancelledError, GeneratorExit,
        ) as exc:
            # The rewrite only ever writes inside out_dir; output_path is touched
            # by the final rename alone, so it only needs sweeping if that rename
            # actually started (an abandoned rename may still land after the job
            # reports cancelled). Until then output_path was never written, and
            # anything already there is not this job's to delete, including on
            # a cancel that arrives before the rename's thread runs.
            # The source is never written at all.
            if renaming.is_set():
                await remove_partial_output(output_path, label="xiso output")
            if isinstance(exc, ReadCancelled):
                raise ConversionCancelled("Conversion cancelled") from exc
            if isinstance(exc, RuntimeError) and _ALREADY_OPTIMIZED in str(exc):
                # Exit 0 with nothing written: the binary's own skip. The
                # convertibility probe filters these out, so reaching here means
                # the file changed between planning and running.
                raise RuntimeError(
                    "Image is already an optimized XISO; nothing to trim",
                ) from exc
            raise
        finally:
            # The shared bounded sweep nsz and JWUDTool use for their work dirs:
            # out_dir sits on the output volume, which may be exactly what the
            # run just died on, so a wedged rmtree leaves a stray hidden dir
            # rather than a job that never finalises. Flag the setup first: if a
            # cancel abandoned it mid-way, it removes what it creates after this
            # sweep has run.
            abandoned.set()
            await remove_partial_tree(out_dir, label="xiso work dir")
            await remove_partial_tree(link_dir, label="xiso source link dir")

        yield {"progress": 100, "message": f"Trimmed {label} image to XISO"}

    # --- verify -------------------------------------------------------------

    @staticmethod
    def _structural_check(path: str) -> dict:
        """Walk the XISO and check every file lies inside it.

        The failure this catches is the common one — a truncated copy — and it
        reads only the directory tables. It is *not* a content check (XDVDFS
        carries no checksums), which is why the mode never offers to delete
        its source on the strength of it.
        """
        stats = walk_image(path)
        if stats["layout"] != "XISO":
            raise XisoImageError(
                f"Not a trimmed XISO (found a full {stats['layout']} disc layout)",
            )
        size = os.path.getsize(path)
        if stats["max_extent"] > size:
            raise XisoImageError(
                f"Image is truncated: files extend to {stats['max_extent']} bytes "
                f"but the file is {size}",
            )
        return stats

    async def verify(
        self, file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        return await collect_verify(
            self.verify_stream(file_path, cancel_event=cancel_event),
            fallback_message="Verification failed",
        )

    async def verify_stream(
        self, file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Structural check in Python, then ``extract-xiso -l`` must parse it."""
        if match_extension(file_path, XISO_VERIFY_EXTENSIONS) is None:
            yield {
                "type": "error", "valid": False,
                "message": f"Not an XISO produced by this tool ({XISO_OUTPUT_EXTENSION})",
            }
            return
        problem, _size = await verify_preflight(
            file_path, XISO_CONVERTIBLE_EXTENSIONS, cancel_event=cancel_event,
        )
        if problem is not None:
            yield problem
            return

        yield {"type": "progress", "progress": 0, "message": "Checking XISO structure..."}
        try:
            stats = await run_detached(
                self._structural_check, file_path, cancel_event=cancel_event,
            )
        except ReadCancelled:
            yield {
                "type": "error", "valid": False, "cancelled": True,
                "message": "Verification cancelled",
            }
            return
        except (XisoImageError, OSError) as exc:
            yield {"type": "error", "valid": False, "message": f"Invalid XISO: {exc}"}
            return

        # nice/ionice as command wrappers, never preexec_fn (the maxcso / chdman /
        # dolphin verify convention): forking a Python callable in this
        # multithreaded process can deadlock the child before exec. Passing
        # nice_via_wrapper also stops capture_verify adding a second ionice.
        cmd = (
            nice_prefix(_OWNER) + ioprio_prefix(_OWNER)
            + [self.extract_xiso_path, "-q", "-l", file_path]
        )
        try:
            async for update in self._runner.capture_verify(
                cmd,
                path=file_path,
                nice_via_wrapper=True,
                success_message=(
                    f"XISO structure verified ({stats['file_count']} files, "
                    f"{_format_size(stats['data_bytes'])} of game data)"
                ),
                cancel_event=cancel_event,
                start_message="Listing XISO contents...",
            ):
                yield update
        except Exception as exc:
            logger.exception("Error during XISO verification: %s", exc)
            yield {"type": "error", "valid": False, "message": f"Verification error: {exc}"}


# Global service instance
extract_xiso_service = ExtractXisoService()
