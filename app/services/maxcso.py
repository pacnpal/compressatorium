"""Wrapper for the ``maxcso`` tool (https://github.com/unknownbrackets/maxcso).

maxcso losslessly compresses PSP/PS2 ``.iso`` disc images into ``.cso``/``.zso``
(and decompresses them back). PPSSPP and PCSX2 read CSO/ZSO directly, so this is
a native, emulator-friendly compression target that needs no keys.

Three modes:

- ``cso_compress``   ``.iso`` -> ``.cso``  (``maxcso <in> -o <out>``; cso1 default)
- ``zso_compress``   ``.iso`` -> ``.zso``  (``maxcso --format=zso <in> -o <out>``)
- ``cso_decompress`` ``.cso``/``.zso``/``.dax`` -> ``.iso`` (``maxcso --decompress``)

maxcso writes the output file directly (``-o``), so unlike nsz there is no work
dir to manage. Progress is estimated from the growth of the output file (maxcso
draws a TTY progress bar that goes silent on a pipe), like the z3ds service.
Verification runs ``maxcso --crc`` which decompresses the whole file and logs
its CRC32, ignoring output; a clean exit means the container decompresses
intact (the analog of nsz ``-V`` / z3ds ``zstd -t``).
"""
import asyncio
import contextlib
import logging
import os
import shutil
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from logging_setup import get_logger
from services.chdman import ConversionCancelled
from services.timeout_policy import compute_progress_stall_timeout

# Compress takes a raw .iso; decompress takes any maxcso-produced container.
MAXCSO_COMPRESS_EXTENSIONS = {".iso"}
MAXCSO_DECOMPRESS_EXTENSIONS = {".cso", ".zso", ".dax"}

# Output extension is decided by the mode, not the input extension (an .iso can
# become either .cso or .zso), so the map is keyed by mode rather than suffix.
MAXCSO_OUTPUT_BY_MODE = {
    "cso_compress": ".cso",
    "zso_compress": ".zso",
    "cso_decompress": ".iso",
}

# Rough output:input size ratios, used only to smooth the progress bar.
_COMPRESS_RATIO = 0.5    # compressed output is ~50% of the source
_DECOMPRESS_RATIO = 2.0  # decompressed .iso is ~2x the compressed source

logger = get_logger("maxcso")


class MaxcsoService:
    """Wrapper for the maxcso binary."""

    def __init__(self):
        self.maxcso_path = settings.maxcso_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()

    # ----- command ----------------------------------------------------------

    def _build_command(
        self, input_path: str, output_path: str, mode: str,
    ) -> list[str]:
        if mode not in MAXCSO_OUTPUT_BY_MODE:
            raise ValueError(f"Unsupported maxcso mode: {mode}")
        cmd = [self.maxcso_path]
        if mode == "cso_decompress":
            cmd.append("--decompress")
        elif mode == "zso_compress":
            cmd += ["--format=zso"]
        # cso_compress uses the default cso1 format (no flag).
        cmd += [input_path, "-o", output_path]

        # Apply nice/ionice via command wrappers, NOT preexec_fn: forking a
        # Python callable in a multithreaded app (this one uses threadpools) can
        # deadlock the child before exec. `nice`/`ionice` are exec-only.
        prefix: list[str] = []
        if settings.chdman_nice is not None:
            nice = shutil.which("nice")
            if nice:
                prefix += [nice, "-n", str(settings.chdman_nice)]
        if (
            settings.chdman_ioprio_class is not None
            and settings.chdman_ioprio_level is not None
        ):
            ionice = shutil.which("ionice")
            if ionice:
                prefix += [
                    ionice,
                    "-c", str(settings.chdman_ioprio_class),
                    "-n", str(settings.chdman_ioprio_level),
                ]
        return prefix + cmd

    def _track_pid(self, pid: int):
        with self._pid_lock:
            self._active_pids.add(pid)

    def _untrack_pid(self, pid: int):
        with self._pid_lock:
            self._active_pids.discard(pid)

    def active_pids(self) -> list[int]:
        with self._pid_lock:
            return list(self._active_pids)

    # ----- output paths -----------------------------------------------------

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Output path for a maxcso mode.

        The output extension is fixed by the mode (``cso_compress`` -> ``.cso``,
        ``zso_compress`` -> ``.zso``, ``cso_decompress`` -> ``.iso``), so this
        ignores the input suffix. ``treat_as_stem`` is accepted for interface
        parity with the other tools; archive members arrive as flattened
        filenames that keep their original extension, and only the stem is used
        here, so no separate branch is needed.
        """
        output_ext = MAXCSO_OUTPUT_BY_MODE.get(mode)
        if output_ext is None:
            raise ValueError(f"Unsupported maxcso mode: {mode}")
        input_p = Path(input_path)
        filename = f"{input_p.stem}{output_ext}"
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    # ----- convert ----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "cso_compress",
        *,
        compression: str | None = None,  # unused: format is chosen by the mode
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        decompress = mode == "cso_decompress"
        verb = "decompression" if decompress else "compression"
        try:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                await asyncio.to_thread(os.makedirs, output_dir, exist_ok=True)

            cmd = self._build_command(input_path, output_path, mode)

            # cmd is built from validated settings paths (no shell interpretation);
            # args are a fixed list, never shell-expanded. nice/ionice are applied
            # as command wrappers in _build_command, so there's no preexec_fn.
            process = await asyncio.create_subprocess_exec(  # nosemgrep
                cmd[0], *cmd[1:],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if process.stdout is None:
                raise RuntimeError("maxcso stdout is not available")

            self._track_pid(process.pid)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Starting maxcso pid=%s cmd=%s", process.pid, " ".join(cmd))

            stall_timeout = compute_progress_stall_timeout(
                input_path=input_path,
                base_timeout=getattr(settings, "progress_timeout", 0),
                timeout_per_gib=getattr(settings, "progress_timeout_per_gib", 0),
                timeout_cap=getattr(settings, "progress_timeout_cap", 0),
            )
            try:
                input_size = os.path.getsize(input_path)
            except OSError:
                input_size = 0
            ratio = _DECOMPRESS_RATIO if decompress else _COMPRESS_RATIO
            expected_size = max(1, int(input_size * ratio))

            last_output_size: int | None = None
            last_activity_at = time.monotonic()

            cancelled_by_request = False
            cancel_task = None
            if cancel_event:

                async def _cancel_watcher():
                    nonlocal cancelled_by_request
                    await cancel_event.wait()
                    cancelled_by_request = True
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                    except ProcessLookupError:
                        pass

                cancel_task = asyncio.create_task(_cancel_watcher())

            output_tail: list[str] = []

            try:
                yield {"progress": 1, "message": f"Starting CSO {verb}..."}

                while True:
                    try:
                        chunk = await asyncio.wait_for(process.stdout.read(256), timeout=2.0)
                    except asyncio.TimeoutError:
                        chunk = None

                    if chunk == b"":
                        break

                    if chunk:
                        text = chunk.decode("utf-8", errors="replace").strip()
                        if text:
                            output_tail.append(text)
                            if len(output_tail) > 30:
                                output_tail.pop(0)
                        last_activity_at = time.monotonic()

                    # Estimate progress from the growing output file.
                    if os.path.exists(output_path):
                        try:
                            current = os.path.getsize(output_path)
                        except OSError:
                            current = None
                        if current is not None and current != last_output_size:
                            last_output_size = current
                            last_activity_at = time.monotonic()
                            pct = min(95, max(1, int(current / expected_size * 90) + 5))
                            yield {
                                "progress": pct,
                                "message": f"Working... ({current // (1024 * 1024)} MB)",
                            }

                    if process.returncode is not None:
                        break

                    if stall_timeout > 0 and (time.monotonic() - last_activity_at) > stall_timeout:
                        logger.warning(
                            "maxcso pid=%s stalled (no progress for %ds), killing",
                            process.pid, stall_timeout,
                        )
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        raise TimeoutError(
                            f"Conversion stalled (no progress for {stall_timeout}s)",
                        )

                await process.wait()
            finally:
                if cancel_task:
                    cancel_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_task
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()
                self._untrack_pid(process.pid)

            if cancelled_by_request:
                if os.path.exists(output_path):
                    with contextlib.suppress(OSError):
                        os.remove(output_path)
                raise ConversionCancelled("Conversion cancelled by user")

            if process.returncode != 0:
                tail = "\n".join(output_tail[-10:]) if output_tail else "Unknown error"
                raise RuntimeError(
                    f"maxcso failed with exit code {process.returncode}: {tail}",
                )

            yield {"progress": 100, "message": f"CSO {verb} complete"}

        except ConversionCancelled as e:
            logger.info("maxcso conversion cancelled: %s", e)
            raise
        except Exception as e:
            # Any failure (nonzero exit, stall timeout, etc.) leaves a partial
            # output on disk because maxcso writes straight to output_path.
            # Remove it so a retry isn't blocked by, or silently trusts, a
            # truncated file. (The cancel path above already cleans up.)
            if os.path.exists(output_path):
                with contextlib.suppress(OSError):
                    os.remove(output_path)
            logger.exception("Error in maxcso.convert: %s", e)
            raise

    # ----- info -------------------------------------------------------------

    def info(self, file_path: str) -> dict:
        """Filesystem-level info (size, format label, compression state). maxcso
        exposes no offline metadata dump. Synchronous; wrap callers in a
        threadpool."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()

        is_compressed = ext in MAXCSO_DECOMPRESS_EXTENSIONS
        base_format = {
            ".iso": "ISO (disc image)",
            ".cso": "CSO (compressed ISO)",
            ".zso": "ZSO (lz4-compressed ISO)",
            ".dax": "DAX (compressed ISO)",
        }.get(ext)
        compression_type = {
            ".cso": "CSO (deflate)",
            ".zso": "ZSO (lz4)",
            ".dax": "DAX",
        }.get(ext)

        size_mb = file_size / (1024 * 1024)
        size_display = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

        return {
            "file": file_path,
            "size": file_size,
            "size_display": size_display,
            "format": base_format,
            "extension": ext,
            "compressed": is_compressed,
            "compression_type": compression_type,
        }

    @staticmethod
    def is_convertible(filename: str) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in MAXCSO_COMPRESS_EXTENSIONS or ext in MAXCSO_DECOMPRESS_EXTENSIONS

    # ----- verify -----------------------------------------------------------

    async def verify(self, file_path: str) -> dict:
        final = {"valid": False, "message": "Verification failed"}
        async for update in self.verify_stream(file_path):
            if update.get("type") in ("complete", "error"):
                final = update
        return {
            "valid": bool(final.get("valid", False)),
            "message": final.get("message") or "Verification failed",
        }

    async def verify_stream(self, file_path: str) -> AsyncGenerator[dict, None]:
        """Verify a compressed CSO/ZSO/DAX by running ``maxcso --crc`` on it.

        ``--crc`` reads and decompresses the whole container, logging its CRC32
        and ignoring output, so a clean (exit 0) run proves the file decompresses
        intact end to end.
        """
        if not os.path.exists(file_path):
            yield {"type": "error", "valid": False, "message": "File not found"}
            return
        try:
            is_empty = os.path.getsize(file_path) == 0
        except OSError as e:
            yield {"type": "error", "valid": False, "message": f"Error reading file: {e}"}
            return
        if is_empty:
            yield {"type": "error", "valid": False, "message": "File is empty"}
            return
        ext = Path(file_path).suffix.lower()
        if ext not in MAXCSO_DECOMPRESS_EXTENSIONS:
            yield {"type": "error", "valid": False, "message": f"Invalid extension: {ext}"}
            return

        try:
            process = await asyncio.create_subprocess_exec(  # nosemgrep
                self.maxcso_path, "--crc", file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._track_pid(process.pid)
            try:
                yield {"type": "progress", "progress": 0, "message": "Verifying integrity..."}
                stdout, _ = await process.communicate()
                output = (stdout or b"").decode("utf-8", errors="replace").strip()
                if process.returncode == 0:
                    yield {
                        "type": "progress",
                        "progress": 100,
                        "message": "Integrity check passed",
                    }
                    yield {
                        "type": "complete",
                        "valid": True,
                        "message": "File verified successfully",
                    }
                else:
                    tail = (
                        "\n".join(output.splitlines()[-5:])
                        if output else "verification failed"
                    )
                    yield {
                        "type": "error",
                        "valid": False,
                        "message": f"Integrity check failed: {tail}",
                    }
            finally:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()
                self._untrack_pid(process.pid)
        except Exception as e:
            logger.exception("Error during CSO verification: %s", e)
            yield {"type": "error", "valid": False, "message": f"Verification error: {e}"}


# Global service instance
maxcso_service = MaxcsoService()
