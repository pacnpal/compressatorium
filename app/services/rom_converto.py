"""Service wrapper for rom-converto (DevYukine/rom-converto), the converter RomM ships.

One binary that converts several unrelated formats. This app runs the version
RomM pins, v0.24.0, and uses it only for what no other tool here does. The
first mode is Xbox 360 ISO -> ZAR (Xenia's ZArchive); PSP PBP -> ISO and 3DS
CIA <-> CCI come later under the same plugin (#420).

What the spike (#421) recorded about v0.24.0, and what this module does about
it:

* **Everything goes to stderr, nothing to stdout, and there is no progress
  without a terminal.** The shared runner merges stderr into the stream it
  reads, and progress comes from the product growing on disk.
* **It writes its product under a hidden temp name** (``.Game.zar.<random>.tmp``)
  beside the requested output and renames it at the end, so the output path
  itself never grows. The service runs it into a private work folder beside the
  output and hands the runner :func:`files_in_folder` over that folder as the
  growth signal, so a long, quiet ZAR job is not killed by the no-progress
  bound. The finished file is then moved into place, never over anything.
* ``--no-update-check`` keeps every run off the network, ``--no-cache`` keeps
  it from reading or writing its persistent hash cache (a verify must decode
  the file, never answer from a cache), and ``NO_COLOR=1`` keeps ANSI codes out
  of the messages. Failures print ``Error: ...`` and exit 1; a usage error
  exits 2.
* **A ZAR can't be decoded back to the ISO.** It stores the disc's files, not
  its sectors, and v0.24.0 has no ZAR -> ISO command. So there is no
  Round-trip, and the mode keeps its Source (``ModeSpec.source_kept_reason``).
  ``xenon verify`` decodes every block and checks its hash, which is the
  output's own check.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import threading
import uuid
from collections.abc import AsyncGenerator
from functools import lru_cache
from pathlib import Path

from logging_setup import get_logger
from services.extract_xiso import XISO_VERIFY_EXTENSIONS, probe_console
from services.output_placement import OutputPlacement
from services.subprocess_runner import (
    ConversionCancelled,
    ReadCancelled,
    SubprocessRunner,
    collect_verify,
    files_in_folder,
    info_timeout,
    ioprio_prefix,
    nice_prefix,
    remove_partial_tree,
    run_detached,
    run_detached_within_stall_bound,
    verify_exception_event,
    verify_preflight,
)
from utils.path_utils import (
    match_extension,
)

from config import settings

_OWNER = "rom_converto"
logger = get_logger("rom_converto")

# The release this app pins, the one RomM ships (spike #421).
ROM_CONVERTO_VERSION = "0.24.0"

ZAR_MODE = "zar_compress"
# Every Xbox 360 disc image is an ``.iso``, like PSP, PS2 and GameCube images,
# so content decides (``is_zar_source``). A trimmed ``.xiso.iso`` is one too.
ZAR_INPUT_EXTENSIONS = frozenset({".iso"})
ZAR_OUTPUT_EXTENSION = ".zar"
ZAR_VERIFY_EXTENSIONS = frozenset({ZAR_OUTPUT_EXTENSION})

# Why the ZAR mode never deletes its Source (ruling 1 on #422, spike #421).
ZAR_SOURCE_KEPT_REASON = (
    "A ZAR holds the disc's files, not the disc itself, and rom-converto "
    f"{ROM_CONVERTO_VERSION} can't turn a ZAR back into an ISO, so the output "
    "can't be Round-tripped against the ISO. The Source ISO is always kept."
)

# How long ``rom-converto --version`` may take before the binary counts as
# broken. Inside the registry's own readiness bound (READY_PROBE_SECONDS).
_VERSION_TIMEOUT = 10.0
_PRIVATE_DIR_MODE = 0o700
_ERROR_PREFIX = "Error:"
_WARN_PREFIX = "[WARN]"
# ``  Files:             3`` in ``xenon info``'s Container block.
_INFO_LINE = re.compile(r"^\s+([^:]+):\s+(.+?)\s*$")


class RomConvertoError(ValueError):
    """rom-converto refused a file (its own ``Error:`` line is the message)."""


def _format_size(size: int) -> str:
    size_mb = size / (1024 * 1024)
    return f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"


def _env() -> dict[str, str]:
    return {**os.environ, "NO_COLOR": "1"}


def _first_error(output: str) -> str | None:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith(_ERROR_PREFIX):
            return line
    return None


@lru_cache(maxsize=4096)
def _probe_xbox360(path: str, size: int, mtime_ns: int) -> bool:
    """Whether ``path`` holds an Xbox 360 XDVDFS volume, cached on (path, size, mtime).

    ``size`` and ``mtime_ns`` are only cache keys. A read error propagates,
    and ``lru_cache`` stores no exception, so an outage is never remembered as
    "not an Xbox 360 image".
    """
    return probe_console(path) == "Xbox 360"


class RomConvertoService:
    """Wrapper for the ``rom-converto`` binary."""

    def __init__(self) -> None:
        self.binary_path = settings.rom_converto_path
        self._runner = SubprocessRunner(owner=_OWNER)
        # (binary path, size, mtime) -> (ready, reason): the readiness probe
        # runs the binary once per file, not on every GET /api/tools.
        self._readiness: tuple[tuple, bool, str] | None = None

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    def abandoned_pids(self) -> list[int]:
        """Children that outlived SIGKILL; see ``SubprocessRunner``."""
        return self._runner.abandoned_pids()

    def _command(self, *args: str) -> list[str]:
        """``rom-converto --no-update-check --no-cache <args>``.

        ``nice``/``ionice`` are command wrappers (``nice_via_wrapper``), never a
        ``preexec_fn``: forking a Python callable in this multithreaded process
        can deadlock the child before exec.
        """
        return nice_prefix(_OWNER) + ioprio_prefix(_OWNER) + [
            self.binary_path, "--no-update-check", "--no-cache", *args,
        ]

    # --- readiness ------------------------------------------------------------

    def forget_readiness(self) -> None:
        """Drop the cached readiness answer (a new binary path, in tests)."""
        self._readiness = None

    def _resolved_binary(self) -> str | None:
        path = self.binary_path
        if os.sep in path:
            return path if os.path.isfile(path) else None
        return shutil.which(path)

    def _stat_binary(self) -> tuple[str, tuple] | None:
        resolved = self._resolved_binary()
        if resolved is None:
            return None
        st = os.stat(resolved)
        return resolved, (resolved, st.st_size, st.st_mtime_ns, os.access(resolved, os.X_OK))

    async def readiness(self) -> tuple[bool, str]:
        """``(ready, reason)``: the binary is there and answers ``--version``.

        A missing binary and one that won't run (wrong architecture, a broken
        download) are both not ready, each with its reason. Cached on the
        binary's size and mtime, so only the stat repeats.
        """
        try:
            found = await run_detached(self._stat_binary)
        except OSError as exc:
            return False, f"rom-converto at {self.binary_path} could not be read: {exc}"
        if found is None:
            return False, (
                f"rom-converto was not found at {self.binary_path} "
                "(set ROM_CONVERTO_PATH to where it is)"
            )
        resolved, key = found
        cached = self._readiness
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        if not key[3]:
            ready, reason = False, f"rom-converto at {resolved} is not executable"
        else:
            ready, reason = await self._probe_version(resolved)
        self._readiness = (key, ready, reason)
        return ready, reason

    async def _probe_version(self, resolved: str) -> tuple[bool, str]:
        try:
            # nice_via_wrapper: no preexec_fn, which can deadlock the child
            # before exec in this threaded process (and before the timeout runs).
            returncode, stdout, _ = await self._runner.run_capture(
                [resolved, "--version"], timeout=_VERSION_TIMEOUT,
                stderr_to_stdout=True, nice_via_wrapper=True, env=_env(),
            )
        except OSError as exc:
            return False, f"rom-converto at {resolved} did not run: {exc}"
        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        if returncode is None:
            return False, (
                f"rom-converto at {resolved} did not run: no answer to --version "
                f"within {int(_VERSION_TIMEOUT)}s"
            )
        if returncode != 0 or not output.startswith("rom-converto"):
            said = output.splitlines()[0] if output else f"exit code {returncode}"
            return False, f"rom-converto at {resolved} did not run: {said}"
        return True, ""

    # --- source / output naming ---------------------------------------------

    @staticmethod
    def is_zar_source(path: str) -> bool:
        """Whether ``path`` is an Xbox 360 disc image (full or trimmed).

        Extension first (cheap), then content: an XDVDFS volume whose layout
        or boot executable says Xbox 360. An original Xbox image is not one:
        ZAR is what Xenia mounts. Never raises.
        """
        if match_extension(path, ZAR_INPUT_EXTENSIONS) is None:
            return False
        try:
            st = os.stat(path)
            return _probe_xbox360(path, st.st_size, st.st_mtime_ns)
        except OSError:
            return False

    @staticmethod
    def _output_stem(name: str) -> str:
        """``Game.iso`` -> ``Game`` (and the xiso tool's ``Game.xiso.iso`` -> ``Game``)."""
        matched = match_extension(name, set(XISO_VERIFY_EXTENSIONS) | ZAR_INPUT_EXTENSIONS)
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
        filename = f"{self._output_stem(input_p.name)}{ZAR_OUTPUT_EXTENSION}"
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
        if mode != ZAR_MODE:
            raise ValueError(f"Unsupported rom-converto mode: {mode}")
        return self.get_output_path(input_path, output_dir, treat_as_stem=treat_as_stem)

    # --- convert ------------------------------------------------------------

    @staticmethod
    def _make_work_dir(work_dir: str, abandoned: threading.Event) -> None:
        """Create the private work folder; undo it if the caller gave up meanwhile."""
        os.makedirs(os.path.dirname(work_dir), exist_ok=True)
        os.mkdir(work_dir, _PRIVATE_DIR_MODE)
        if abandoned.is_set():
            shutil.rmtree(work_dir, ignore_errors=True)

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = ZAR_MODE,
        *,
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Compress an Xbox 360 image to a ZAR at ``output_path``, yielding progress."""
        if mode != ZAR_MODE:
            raise ValueError(f"Unsupported rom-converto mode: {mode}")
        # Re-checked: the file may have changed since planning. Bounded like
        # every read before the subprocess (whose stall watchdog isn't up yet).
        try:
            is_source = await run_detached_within_stall_bound(
                self.is_zar_source, input_path,
                what="Reading the Xbox 360 image header", cancel_event=cancel_event,
            )
        except ReadCancelled as exc:
            raise ConversionCancelled("Conversion cancelled") from exc
        if not is_source:
            raise ValueError(f"Not an Xbox 360 disc image: {input_path}")

        yield {"progress": 1, "message": "Starting ZAR compression..."}
        out_dir = os.path.dirname(os.path.abspath(output_path))
        # Named up front so the sweep below always knows it, even when creating
        # it is abandoned half-way. Hidden, beside the output, so the final move
        # is a same-filesystem rename.
        work_dir = os.path.join(out_dir, f".rom-converto-{uuid.uuid4().hex[:12]}")
        produced = os.path.join(work_dir, os.path.basename(output_path))
        abandoned = threading.Event()
        # The final move, safe to give up on (services.output_placement).
        placement = OutputPlacement(output_path, label="ZAR")
        errors: list[str] = []

        def parse_progress(line: str) -> int | None:
            # Records rom-converto's first Error: line. It prints no
            # percentage, so this never returns one (None): the runner's
            # growth fallback drives the bar.
            stripped = line.strip()
            if stripped.startswith(_ERROR_PREFIX) and not errors:
                errors.append(stripped)

        try:
            await run_detached_within_stall_bound(
                self._make_work_dir, work_dir, abandoned,
                what="Preparing the rom-converto work directory",
                cancel_event=cancel_event,
            )
            try:
                async for update in self._runner.run(
                    self._command("xenon", "compress", input_path, produced),
                    input_path=input_path,
                    output_path=produced,
                    parse_progress=parse_progress,
                    initial_progress=1,
                    cancel_event=cancel_event,
                    fail_label="rom-converto",
                    complete_message="ZAR written",
                    # rom-converto names its file itself while it writes, so the
                    # whole work folder is the growth signal (ruling 2, #422).
                    output_growth_paths=files_in_folder(work_dir),
                    mode=mode,
                    nice_via_wrapper=True,
                    env=_env(),
                    require_output=True,
                ):
                    if update.get("progress", 0) >= 100:
                        # Hold the bar short of done until the file is in place.
                        update = {**update, "progress": 98}
                    if update.get("message", "").startswith(_WARN_PREFIX):
                        # rom-converto's caveat about its own output (a ZAR with
                        # no default.xex at its root, which Xenia won't mount).
                        update = {**update, "warning": True}
                    yield update
            except RuntimeError as exc:
                if errors and "failed with return code" in str(exc):
                    raise RuntimeError(f"rom-converto failed: {errors[0]}") from exc
                raise
            await run_detached_within_stall_bound(
                placement.place, produced,
                what="Placing the ZAR output", cancel_event=cancel_event,
            )
        except (
            ConversionCancelled, ReadCancelled, RuntimeError,
            asyncio.CancelledError, GeneratorExit,
        ) as exc:
            # Swept only if the move completed and the path is still its file.
            await placement.abandon_and_sweep()
            if isinstance(exc, ReadCancelled):
                raise ConversionCancelled("Conversion cancelled") from exc
            raise
        finally:
            abandoned.set()
            await remove_partial_tree(work_dir, label="rom-converto work dir")

        yield {"progress": 100, "message": "Compressed Xbox 360 image to ZAR"}

    # --- verify -------------------------------------------------------------

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
        """``rom-converto xenon verify``: decode every block and check its hash."""
        if match_extension(file_path, ZAR_VERIFY_EXTENSIONS) is None:
            yield {
                "type": "error", "valid": False,
                "message": f"Not a ZAR ({ZAR_OUTPUT_EXTENSION})",
            }
            return
        problem, _size = await verify_preflight(
            file_path, ZAR_VERIFY_EXTENSIONS, cancel_event=cancel_event,
        )
        if problem is not None:
            yield problem
            return
        try:
            async for update in self._runner.capture_verify(
                self._command("xenon", "verify", file_path),
                path=file_path,
                success_message="ZAR is Intact: every block decoded and matched its hash",
                cancel_event=cancel_event,
                nice_via_wrapper=True,
                env=_env(),
                start_message="Decoding every ZAR block...",
            ):
                yield update
        except Exception as exc:
            logger.exception("Error during ZAR verification of %s", file_path)
            yield verify_exception_event(exc)

    # --- info ---------------------------------------------------------------

    async def info(self, file_path: str) -> dict:
        """Describe a ZAR with ``rom-converto xenon info``.

        Raises :class:`RomConvertoError` when rom-converto refuses the file,
        and ``RuntimeError`` when it didn't answer in time.
        """
        if match_extension(file_path, ZAR_VERIFY_EXTENSIONS) is None:
            raise RomConvertoError(f"Not a ZAR ({ZAR_OUTPUT_EXTENSION})")
        size = await run_detached(os.path.getsize, file_path)
        timeout = info_timeout(_OWNER)
        returncode, stdout, _ = await self._runner.run_capture(
            self._command("xenon", "info", file_path),
            timeout=timeout or None, stderr_to_stdout=True,
            nice_via_wrapper=True, env=_env(),
        )
        output = (stdout or b"").decode("utf-8", errors="replace")
        if returncode is None:
            raise RuntimeError(f"rom-converto did not answer within {timeout}s")
        if returncode != 0:
            raise RomConvertoError(_first_error(output) or f"exit code {returncode}")
        fields = self._container_fields(output)
        logical = _int_or_none(fields.get("logical bytes"))
        boot = fields.get("default.xex")
        return {
            "file": file_path,
            "size": size,
            "size_display": _format_size(size),
            "format": fields.get("format") or "ZAR (ZArchive)",
            "extension": ZAR_OUTPUT_EXTENSION,
            "compressed": True,
            "compression_type": "ZArchive (zstd)",
            "file_count": _int_or_none(fields.get("files")),
            "directory_count": _int_or_none(fields.get("directories")),
            "logical_size": logical,
            "logical_size_display": _format_size(logical) if logical is not None else None,
            "blocks": _int_or_none(fields.get("blocks")),
            "boot_executable": None if boot is None else boot == "present",
        }

    @staticmethod
    def _container_fields(output: str) -> dict[str, str]:
        """The ``key: value`` lines of ``xenon info``'s Container block."""
        fields: dict[str, str] = {}
        inside = False
        for line in output.splitlines():
            if line.strip() == "Container:":
                inside = True
                continue
            if inside and line and not line[0].isspace():
                break
            match = _INFO_LINE.match(line) if inside else None
            if match:
                fields[match.group(1).strip().lower()] = match.group(2)
        return fields


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


# Global service instance
rom_converto_service = RomConvertoService()
