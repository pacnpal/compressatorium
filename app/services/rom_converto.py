"""Service wrapper for rom-converto (DevYukine/rom-converto), the converter RomM ships.

One binary that converts several unrelated formats. This app runs the version
RomM pins, v0.24.0, and uses it only for what no other tool here does: Xbox 360
ISO -> ZAR (Xenia's ZArchive, #422), PSP PBP -> ISO and 3DS CIA <-> CCI (#423).
One table (``_CALLS``) says how each mode runs; every mode goes through the
same ``convert``: a private work folder, then a move into place.

For the PSP and 3DS modes (#423): ``psp to-iso IN OUT`` and ``ctr convert IN
OUT [--trim]`` (the direction comes from the input's extension, so the work
folder product is named ``.3ds`` or ``.cia``; the CCI is placed as ``.cci``,
trimmed). rom-converto carries the 3DS common keys and takes no key or seed
file; an ``Error:`` line about keys or a seed is said plainly
(:data:`KEYS_MESSAGE`). Their outputs have no rom-converto check, so the
container check reads their structure in Python (``rom_converto_formats``).

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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from logging_setup import get_logger
from services import rom_converto_formats as formats
from services.extract_xiso import XISO_VERIFY_EXTENSIONS, probe_console
from services.output_placement import OutputPlacement
from services.subprocess_runner import (
    RunnerService,
    ConversionCancelled,
    ReadCancelled,
    SubprocessRunner,
    files_in_folder,
    info_timeout,
    remove_partial_tree,
    run_detached,
    run_detached_within_stall_bound,
    verify_exception_event,
    verify_preflight,
)
from utils.path_utils import (
    match_extension,
)
from utils.sizes import format_size

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

# PSP PBP -> ISO and 3DS CIA <-> CCI (#423). Their outputs can't be
# Round-tripped against the Source (a converted CIA is unsigned with a zero
# title key, and nothing turns an ISO back into its PBP), so the modes use the
# Recognized proof (``ModeSpec.delete_proof``).
PBP_MODE = "pbp_to_iso"
CIA_TO_CCI_MODE = "cia_to_cci"
CCI_TO_CIA_MODE = "cci_to_cia"
PBP_INPUT_EXTENSIONS = formats.PBP_EXTENSIONS
CIA_INPUT_EXTENSIONS = formats.CIA_EXTENSIONS
CCI_INPUT_EXTENSIONS = formats.CCI_EXTENSIONS
# The extensions this app writes the outputs under, and checks them as: ISO for
# PBP, CIA, and CCI as `.cci` (what RomM's `cci` target writes). Never `.3ds`:
# that is only rom-converto's name in the work folder, and claiming it would
# make every `.3ds` ROM look like an output with a check record to clear.
CONTAINER_VERIFY_EXTENSIONS = frozenset({".iso", ".cia", ".cci"})
VERIFY_EXTENSIONS = ZAR_VERIFY_EXTENSIONS | CONTAINER_VERIFY_EXTENSIONS


@dataclass(frozen=True)
class _ModeCall:
    """How one mode runs: the subcommand, the product's name in the work
    folder, and what to call the format in messages."""

    command: tuple[str, ...]
    inputs: frozenset[str]
    output_ext: str
    work_ext: str  # the name rom-converto writes in the work folder
    label: str
    source_label: str  # what the Source must be, for a refusal
    start: str
    done: str
    extra: tuple[str, ...] = ()


_CALLS: dict[str, _ModeCall] = {
    ZAR_MODE: _ModeCall(
        ("xenon", "compress"), ZAR_INPUT_EXTENSIONS, ZAR_OUTPUT_EXTENSION,
        ZAR_OUTPUT_EXTENSION, "ZAR", "an Xbox 360 disc image",
        "Starting ZAR compression...", "Compressed Xbox 360 image to ZAR",
    ),
    PBP_MODE: _ModeCall(
        ("psp", "to-iso"), PBP_INPUT_EXTENSIONS, ".iso", ".iso", "ISO",
        "a PSP game PBP", "Starting PBP to ISO...", "Converted PSP PBP to ISO",
    ),
    CIA_TO_CCI_MODE: _ModeCall(
        # `.3ds` in the work folder is the name the spike ran; the output is
        # placed as `.cci`. Trimmed: a padded CCI is the card's full size
        # (128 MiB for a 1 MiB homebrew), and emulators read a trimmed one.
        ("ctr", "convert"), CIA_INPUT_EXTENSIONS, ".cci", ".3ds", "CCI", "a CIA",
        "Starting CIA to CCI...", "Converted 3DS CIA to CCI", extra=("--trim",),
    ),
    CCI_TO_CIA_MODE: _ModeCall(
        ("ctr", "convert"), CCI_INPUT_EXTENSIONS, ".cia", ".cia", "CIA", "a CCI",
        "Starting CCI to CIA...", "Converted 3DS CCI to CIA",
    ),
}

# An `Error:` line about keys or seeds (#423): rom-converto 0.24.0 carries its
# common keys and takes no key or seed file, so an input that needs one it
# doesn't have can only fail. The spike saw none; the message is ready for it.
_KEYS_ERROR = re.compile(r"\b(keys?|seed(db)?|decrypt\w*|title ?key|crypto)\b", re.IGNORECASE)
KEYS_MESSAGE = (
    "it needs keys or a seed this app doesn't have. The app never downloads or "
    "ships keys or seeds, and rom-converto has only its own built-in common keys"
)

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



def _env() -> dict[str, str]:
    return {**os.environ, "NO_COLOR": "1"}


def _failure_message(error: str) -> str:
    """A failed run's message from rom-converto's first ``Error:`` line, saying
    plainly when the input needs keys or a seed this app doesn't have."""
    if _KEYS_ERROR.search(error):
        return f"rom-converto could not convert this file: {KEYS_MESSAGE}. It said: {error}"
    return f"rom-converto failed: {error}"


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


@lru_cache(maxsize=4096)
def _probe_container(path: str, size: int, mtime_ns: int) -> str | None:
    """:func:`rom_converto_formats.source_problem`, cached on (path, size, mtime).

    As for :func:`_probe_xbox360`, a read error propagates and is never cached.
    """
    return formats.source_problem(path)


def _call(mode: str) -> _ModeCall:
    try:
        return _CALLS[mode]
    except KeyError:
        raise ValueError(f"Unsupported rom-converto mode: {mode}") from None


class RomConvertoService(RunnerService):
    """Wrapper for the ``rom-converto`` binary."""

    def __init__(self) -> None:
        self.binary_path = settings.rom_converto_path
        self._runner = SubprocessRunner(owner=_OWNER)
        # (binary path, size, mtime) -> (ready, reason): the readiness probe
        # runs the binary once per file, not on every GET /api/tools.
        self._readiness: tuple[tuple, bool, str] | None = None

    def _command(self, *args: str) -> list[str]:
        """``rom-converto --no-update-check --no-cache <args>``."""
        return [
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
            # stdout alone: the version line is there, and stderr may carry a
            # priority wrapper's diagnostics, which must not decide readiness.
            returncode, stdout, stderr = await self._runner.run_capture(
                [resolved, "--version"], timeout=_VERSION_TIMEOUT, env=_env(),
            )
        except OSError as exc:
            return False, f"rom-converto at {resolved} did not run: {exc}"
        if returncode is None:
            return False, (
                f"rom-converto at {resolved} did not run: no answer to --version "
                f"within {int(_VERSION_TIMEOUT)}s"
            )
        out = (stdout or b"").decode("utf-8", errors="replace").strip()
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        # The version line on stdout's first line, else anywhere on stderr (a
        # build that prints it there, beside any wrapper diagnostics), and only
        # from a clean exit.
        lines = out.splitlines()[:1] + err.splitlines()
        if returncode == 0 and any(line.startswith("rom-converto") for line in lines):
            return True, ""
        said = (out or err).splitlines()[0] if (out or err) else ""
        if returncode == 0:
            return False, (
                f"rom-converto at {resolved} ran but printed no rom-converto version "
                f"line" + (f": {said}" if said else "")
            )
        return False, f"rom-converto at {resolved} did not run: {said or f'exit code {returncode}'}"

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
    def is_container_source(path: str) -> bool:
        """Whether ``path`` is a PSP game PBP, a CIA or a CCI, by its header.

        The extension says which (``rom_converto_formats.source_problem``).
        Cached on path, size and mtime. Never raises.
        """
        try:
            st = os.stat(path)
            return _probe_container(path, st.st_size, st.st_mtime_ns) is None
        except OSError:
            return False

    def is_source(self, path: str) -> bool:
        """Whether any rom-converto mode converts ``path``: an Xbox 360 ``.iso``,
        or a PBP, CIA or CCI by its content. Never raises."""
        if match_extension(path, ZAR_INPUT_EXTENSIONS) is not None:
            return self.is_zar_source(path)
        return self.is_container_source(path)

    def is_source_for(self, mode: str, path: str) -> bool:
        """Whether ``mode`` converts ``path``: its extension, then its content."""
        call = _CALLS.get(mode)
        if call is None or match_extension(path, call.inputs) is None:
            return False
        if mode == ZAR_MODE:
            return self.is_zar_source(path)
        return self.is_container_source(path)

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
        output_ext: str = ZAR_OUTPUT_EXTENSION,
    ) -> str:
        input_p = Path(input_path)
        filename = f"{self._output_stem(input_p.name)}{output_ext}"
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
            output_ext=_call(mode).output_ext,
        )

    def detect_output_path(self, input_path: str) -> str | None:
        """Where a conversion of ``input_path`` would be written beside it, by
        extension alone (no content read), or None for a file no mode takes.
        For the "output exists" badge."""
        for mode, call in _CALLS.items():
            if match_extension(input_path, call.inputs) is not None:
                return self.get_output_path_for_mode(mode, input_path)
        return None

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
        """Convert ``input_path`` in ``mode`` into ``output_path``, yielding progress.

        Every mode runs the same way: rom-converto writes into a private work
        folder beside the output, and the product is moved into place, never
        over anything.
        """
        call = _call(mode)
        # Re-checked: the file may have changed since planning. Bounded like
        # every read before the subprocess (whose stall watchdog isn't up yet).
        try:
            is_source = await run_detached_within_stall_bound(
                self.is_source_for, mode, input_path,
                what=f"Reading the header of {os.path.basename(input_path)}",
                cancel_event=cancel_event,
            )
        except ReadCancelled as exc:
            raise ConversionCancelled("Conversion cancelled") from exc
        if not is_source:
            raise ValueError(f"Not {call.source_label}: {input_path}")

        yield {"progress": 1, "message": call.start}
        out_dir = os.path.dirname(os.path.abspath(output_path))
        # Named up front so the sweep below always knows it, even when creating
        # it is abandoned half-way. Hidden, beside the output, so the final move
        # is a same-filesystem rename.
        work_dir = os.path.join(out_dir, f".rom-converto-{uuid.uuid4().hex[:12]}")
        stem = os.path.splitext(os.path.basename(output_path))[0]
        produced = os.path.join(work_dir, f"{stem}{call.work_ext}")
        abandoned = threading.Event()
        # The final move, safe to give up on (services.output_placement).
        placement = OutputPlacement(output_path, label=call.label)
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
                    self._command(*call.command, input_path, produced, *call.extra),
                    input_path=input_path,
                    output_path=produced,
                    parse_progress=parse_progress,
                    initial_progress=1,
                    cancel_event=cancel_event,
                    fail_label="rom-converto",
                    complete_message=f"{call.label} written",
                    # rom-converto names its file itself while it writes, so the
                    # whole work folder is the growth signal (ruling 2, #422).
                    output_growth_paths=files_in_folder(work_dir),
                    mode=mode,
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
                    raise RuntimeError(_failure_message(errors[0])) from exc
                raise
            await run_detached_within_stall_bound(
                placement.place, produced,
                what=f"Placing the {call.label} output", cancel_event=cancel_event,
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

        yield {"progress": 100, "message": call.done}

    # --- verify -------------------------------------------------------------

    async def verify_stream(
        self, file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """The output's container check.

        A ZAR: ``rom-converto xenon verify`` decodes every block and checks its
        hash. An ISO, CCI or CIA (#423): its structure is read in Python
        (``rom_converto_formats.output_problem``), since rom-converto has no
        check of its own for them; a cut-off file fails.
        """
        if match_extension(file_path, CONTAINER_VERIFY_EXTENSIONS) is not None:
            async for update in self._container_verify(file_path, cancel_event):
                yield update
            return
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
                env=_env(),
                start_message="Decoding every ZAR block...",
            ):
                yield update
        except Exception as exc:
            logger.exception("Error during ZAR verification of %s", file_path)
            yield verify_exception_event(exc)

    @staticmethod
    async def _container_verify(
        file_path: str, cancel_event: asyncio.Event | None,
    ) -> AsyncGenerator[dict, None]:
        """An ISO, CCI or CIA output's structure: whole, or why not."""
        problem, _size = await verify_preflight(
            file_path, CONTAINER_VERIFY_EXTENSIONS, cancel_event=cancel_event,
        )
        if problem is not None:
            yield problem
            return
        try:
            found = await run_detached_within_stall_bound(
                formats.output_problem, file_path,
                what=f"Reading {os.path.basename(file_path)}", cancel_event=cancel_event,
            )
        except ReadCancelled:
            yield {
                "type": "error", "valid": False, "cancelled": True,
                "message": "Verification cancelled",
            }
            return
        except (OSError, RuntimeError, ValueError) as exc:
            # A file that could not be read, or a read that stalled, is no
            # verdict (the shared rule).
            yield verify_exception_event(exc)
            return
        if found is not None:
            yield {"type": "error", "valid": False, "message": f"Not Intact: {found}"}
            return
        yield {
            "type": "complete", "valid": True,
            "message": "File is Intact: its structure reads back whole",
        }

    # --- info ---------------------------------------------------------------

    async def info(self, file_path: str) -> dict:
        """Describe a ZAR with ``rom-converto xenon info``.

        Raises :class:`RomConvertoError` when rom-converto refuses the file,
        and ``RuntimeError`` when it didn't answer in time.
        """
        if match_extension(file_path, ZAR_VERIFY_EXTENSIONS) is None:
            raise RomConvertoError(f"Not a ZAR ({ZAR_OUTPUT_EXTENSION})")
        size = await run_detached(os.path.getsize, file_path)
        timeout = info_timeout()
        returncode, stdout, _ = await self._runner.run_capture(
            self._command("xenon", "info", file_path),
            timeout=timeout or None, stderr_to_stdout=True, env=_env(),
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
            "size_display": format_size(size),
            "format": fields.get("format") or "ZAR (ZArchive)",
            "extension": ZAR_OUTPUT_EXTENSION,
            "compressed": True,
            "compression_type": "ZArchive (zstd)",
            "file_count": _int_or_none(fields.get("files")),
            "directory_count": _int_or_none(fields.get("directories")),
            "logical_size": logical,
            "logical_size_display": format_size(logical) if logical is not None else None,
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
