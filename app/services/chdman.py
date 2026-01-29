import asyncio
import re
import os
import logging
import time
import threading
import shutil
from pathlib import Path
from typing import AsyncGenerator, Optional, List
from fastapi.concurrency import run_in_threadpool

from config import settings


CONVERTIBLE_EXTENSIONS = {".gdi", ".iso", ".cue", ".bin"}

logger = logging.getLogger("chd.chdman")


class ConversionCancelled(Exception):
    """Raised when a conversion is cancelled before completion."""


class ChdmanService:
    """Wrapper for chdman binary."""

    def __init__(self):
        self.chdman_path = settings.chdman_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()

    def _build_command(
        self,
        mode: str,
        input_path: str,
        output_path: str,
        compression: Optional[str] = None,
    ) -> list[str]:
        cmd = [self.chdman_path, mode, "-f", "-i", input_path, "-o", output_path]
        if mode == "createdvd":
            # Insert -hs 2048 after mode for PSP compatibility
            cmd = [
                self.chdman_path,
                mode,
                "-hs",
                "2048",
                "-f",
                "-i",
                input_path,
                "-o",
                output_path,
            ]

        if compression and mode in {
            "createcd",
            "createdvd",
            "createraw",
            "createhd",
            "createld",
            "copy",
        }:
            cmd = cmd[:2] + ["-c", compression] + cmd[2:]

        if (
            settings.chdman_ioprio_class is not None
            and settings.chdman_ioprio_level is not None
        ):
            ionice = shutil.which("ionice")
            if ionice:
                cmd = [
                    ionice,
                    "-c",
                    str(settings.chdman_ioprio_class),
                    "-n",
                    str(settings.chdman_ioprio_level),
                ] + cmd
            elif logger.isEnabledFor(logging.DEBUG):
                logger.debug("ionice not found; skipping I/O priority settings")

        return cmd

    def _track_pid(self, pid: int):
        with self._pid_lock:
            self._active_pids.add(pid)

    def _untrack_pid(self, pid: int):
        with self._pid_lock:
            self._active_pids.discard(pid)

    def active_pids(self) -> list[int]:
        with self._pid_lock:
            return list(self._active_pids)

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "createcd",
        compression: Optional[str] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Run chdman conversion and yield progress updates.

        Args:
            input_path: Path to input file (GDI, ISO, CUE)
            output_path: Path for output CHD file
            mode: "createcd" or "createdvd"

        Yields:
            dict: {"progress": int, "message": str}
        """
        # Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir:  # Empty string means output is in current directory, no need to create
            await run_in_threadpool(os.makedirs, output_dir, exist_ok=True)

        cmd = self._build_command(
            mode, input_path, output_path, compression=compression
        )

        def _preexec():
            if settings.chdman_nice is not None:
                try:
                    os.nice(settings.chdman_nice)
                except OSError:
                    pass

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=_preexec if os.name == "posix" else None,
        )
        self._track_pid(process.pid)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Starting chdman pid=%s cmd=%s", process.pid, " ".join(cmd))

        stall_timeout = max(0, int(getattr(settings, "progress_timeout", 0) or 0))
        last_progress_value = 0
        last_output_size: Optional[int] = None
        last_activity_at = time.monotonic()

        cancelled_by_request = False
        cancel_task = None
        if cancel_event:

            async def _cancel_watcher():
                nonlocal cancelled_by_request
                await cancel_event.wait()
                if process.returncode is not None:
                    return
                cancelled_by_request = True
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Cancelling chdman pid=%s", process.pid)
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Killing chdman pid=%s after timeout", process.pid)
                    process.kill()

            cancel_task = asyncio.create_task(_cancel_watcher())

        buffer = ""
        output_lines: List[str] = []
        last_output_at = time.monotonic()
        last_idle_log = last_output_at
        stall_error: Optional[str] = None

        def _update_output_activity(now: float):
            nonlocal last_output_size, last_activity_at
            if not output_path:
                return
            try:
                if not os.path.exists(output_path):
                    return
                size = os.path.getsize(output_path)
            except OSError:
                return
            if last_output_size is None:
                last_output_size = size
                last_activity_at = now
                return
            if size > last_output_size:
                last_output_size = size
                last_activity_at = now

        async def _check_stall(now: float) -> bool:
            nonlocal stall_error
            if stall_timeout <= 0:
                return False
            _update_output_activity(now)
            if now - last_activity_at < stall_timeout:
                return False
            stall_error = (
                "Conversion stalled: no progress increase or output growth for "
                f"{stall_timeout}s (progress={last_progress_value}%, output_size={last_output_size})"
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "chdman pid=%s stalled for %.1fs (progress=%s output_size=%s)",
                    process.pid,
                    now - last_activity_at,
                    last_progress_value,
                    last_output_size,
                )
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
            return True

        while True:
            try:
                chunk = await asyncio.wait_for(process.stdout.read(100), timeout=2)
            except asyncio.TimeoutError:
                if cancel_event and cancel_event.is_set():
                    break
                now = time.monotonic()
                idle_for = now - last_output_at
                if (
                    logger.isEnabledFor(logging.DEBUG)
                    and idle_for >= 30
                    and now - last_idle_log >= 30
                ):
                    logger.debug("chdman pid=%s idle for %.1fs", process.pid, idle_for)
                    last_idle_log = now
                if await _check_stall(now):
                    break
                continue
            if not chunk:
                break

            buffer += chunk.decode("utf-8", errors="replace")
            last_output_at = time.monotonic()

            # Process complete lines and progress updates
            while "\r" in buffer or "\n" in buffer:
                # Handle carriage returns (progress updates)
                if "\r" in buffer:
                    parts = buffer.split("\r")
                    for part in parts[:-1]:
                        if part.strip():
                            line = part.strip()
                            if line:
                                if not output_lines or output_lines[-1] != line:
                                    output_lines.append(line)
                                    if len(output_lines) > 30:
                                        output_lines.pop(0)
                            now = time.monotonic()
                            progress = self._parse_progress(line)
                            if progress > last_progress_value:
                                last_progress_value = progress
                                last_activity_at = now
                            yield {"progress": progress, "message": line}
                    buffer = parts[-1]
                # Handle newlines
                elif "\n" in buffer:
                    parts = buffer.split("\n")
                    for part in parts[:-1]:
                        line = part.strip()
                        if line:
                            if not output_lines or output_lines[-1] != line:
                                output_lines.append(line)
                                if len(output_lines) > 30:
                                    output_lines.pop(0)
                            now = time.monotonic()
                            progress = self._parse_progress(line)
                            if progress > last_progress_value:
                                last_progress_value = progress
                                last_activity_at = now
                            yield {"progress": progress, "message": line}
                    buffer = parts[-1]
            if await _check_stall(time.monotonic()):
                break

        # Process any remaining buffer
        if buffer.strip():
            line = buffer.strip()
            if not output_lines or output_lines[-1] != line:
                output_lines.append(line)
                if len(output_lines) > 30:
                    output_lines.pop(0)
            now = time.monotonic()
            progress = self._parse_progress(line)
            if progress > last_progress_value:
                last_progress_value = progress
                last_activity_at = now
            yield {"progress": progress, "message": line}
            await _check_stall(time.monotonic())

        await process.wait()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("chdman pid=%s exit=%s", process.pid, process.returncode)
        self._untrack_pid(process.pid)

        if cancel_task:
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass

        if stall_error:
            raise RuntimeError(stall_error)

        if cancelled_by_request:
            raise ConversionCancelled("Conversion cancelled")

        if process.returncode != 0:
            tail = "\n".join(output_lines[-6:])
            if tail:
                raise RuntimeError(
                    f"chdman failed with return code {process.returncode}."
                    f"\nLast output:\n{tail}"
                )
            raise RuntimeError(f"chdman failed with return code {process.returncode}")

        yield {"progress": 100, "message": "Conversion complete"}

    async def info(self, chd_path: str) -> dict:
        """Get information about a CHD file."""
        process = await asyncio.create_subprocess_exec(
            self.chdman_path,
            "info",
            "-i",
            chd_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timeout = max(0, int(getattr(settings, "chdman_info_timeout", 0) or 0))
        try:
            if timeout:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            else:
                stdout, stderr = await process.communicate()
        except asyncio.TimeoutError:
            await self._terminate_process(process)
            raise RuntimeError(f"chdman info timed out after {timeout}s")

        if process.returncode != 0:
            raise RuntimeError(
                stderr.decode() or f"chdman info failed with code {process.returncode}"
            )

        return self._parse_info(stdout.decode())

    async def verify(self, chd_path: str) -> dict:
        """
        Verify the integrity of a CHD file.

        Returns:
            dict: {"valid": bool, "message": str}
        """
        final = {"valid": False, "message": "CHD verification failed"}
        async for update in self.verify_stream(chd_path):
            if update.get("type") in ("complete", "error"):
                final = update
        return {
            "valid": bool(final.get("valid", False)),
            "message": final.get("message") or "CHD verification failed",
        }

    async def verify_stream(self, chd_path: str) -> AsyncGenerator[dict, None]:
        process = await asyncio.create_subprocess_exec(
            self.chdman_path,
            "verify",
            "-i",
            chd_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._track_pid(process.pid)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Starting chdman verify pid=%s path=%s", process.pid, chd_path)

        output_lines = []
        buffer = ""
        last_output_at = time.monotonic()
        start = last_output_at
        stall_timeout = max(
            0, int(getattr(settings, "verify_progress_timeout", 0) or 0)
        )
        overall_timeout = max(
            0, int(getattr(settings, "chdman_verify_timeout", 0) or 0)
        )
        timeout_error = None

        async def _check_timeouts(now: float) -> bool:
            nonlocal timeout_error
            if overall_timeout > 0 and now - start >= overall_timeout:
                timeout_error = f"Verification timed out after {overall_timeout}s"
                await self._terminate_process(process)
                return True
            if stall_timeout > 0 and now - last_output_at >= stall_timeout:
                timeout_error = (
                    "Verification stalled: no output for "
                    f"{stall_timeout}s"
                )
                await self._terminate_process(process)
                return True
            return False

        while True:
            try:
                chunk = await asyncio.wait_for(process.stdout.read(100), timeout=2)
            except asyncio.TimeoutError:
                if await _check_timeouts(time.monotonic()):
                    break
                continue
            if not chunk:
                break

            buffer += chunk.decode("utf-8", errors="replace")
            last_output_at = time.monotonic()

            while "\r" in buffer or "\n" in buffer:
                if "\r" in buffer:
                    parts = buffer.split("\r")
                    for part in parts[:-1]:
                        line = part.strip()
                        if line:
                            output_lines.append(line)
                            progress = self._parse_progress(line)
                            yield {
                                "type": "progress",
                                "progress": progress,
                                "message": line,
                            }
                    buffer = parts[-1]
                elif "\n" in buffer:
                    parts = buffer.split("\n")
                    for part in parts[:-1]:
                        line = part.strip()
                        if line:
                            output_lines.append(line)
                            progress = self._parse_progress(line)
                            yield {
                                "type": "progress",
                                "progress": progress,
                                "message": line,
                            }
                    buffer = parts[-1]
            if await _check_timeouts(time.monotonic()):
                break

        if buffer.strip():
            line = buffer.strip()
            output_lines.append(line)
            progress = self._parse_progress(line)
            yield {"type": "progress", "progress": progress, "message": line}

        await process.wait()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "chdman verify pid=%s exit=%s", process.pid, process.returncode
            )
        self._untrack_pid(process.pid)

        if timeout_error:
            yield {"type": "error", "valid": False, "message": timeout_error}
            return

        output_lines = output_lines[-20:]
        output = "\n".join(output_lines).strip()
        if process.returncode == 0:
            yield {
                "type": "complete",
                "valid": True,
                "message": "CHD file verified successfully",
            }
        else:
            yield {
                "type": "error",
                "valid": False,
                "message": output or "CHD verification failed",
            }

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        try:
            if process.returncode is not None:
                return
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            # The process has already exited or no longer exists; nothing left to terminate.
            pass

    def _parse_progress(self, line: str) -> int:
        """Parse chdman output for progress percentage."""
        # chdman outputs: "Compressing, 45.2% complete..."
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if match:
            return min(99, int(float(match.group(1))))
        return 0

    def _parse_info(self, output: str) -> dict:
        """Parse chdman info output into structured data."""
        info = {"raw_data": output}
        metadata_lines = []

        # Parse key-value pairs
        for line in output.split("\n"):
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                value = value.strip()
                info[key] = value
                if key == "metadata":
                    metadata_lines.append(value)

        if metadata_lines:
            info["metadata_lines"] = metadata_lines

        return info

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible to CHD."""
        ext = Path(filename).suffix.lower()
        return ext in CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_chd_path(
        input_path: str,
        output_dir: Optional[str] = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Get the output CHD path for an input file or stem."""
        input_p = Path(input_path)
        chd_name = input_p.name + ".chd" if treat_as_stem else input_p.stem + ".chd"

        if output_dir:
            return str(Path(output_dir) / chd_name)
        else:
            return str(input_p.parent / chd_name)

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: Optional[str] = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        input_p = Path(input_path)
        stem = input_p.name if treat_as_stem else input_p.stem

        if mode.startswith("extract") and not treat_as_stem:
            name = input_p.name
            if name.lower().endswith(".chd"):
                stem = name[:-4]

        if mode == "copy":
            filename = f"{stem}_copy.chd"
        elif mode in {"createcd", "createdvd", "createraw", "createhd", "createld"}:
            filename = f"{stem}.chd"
        elif mode == "extractcd":
            filename = stem if stem.lower().endswith(".cue") else f"{stem}.cue"
        elif mode == "extractdvd":
            filename = stem if stem.lower().endswith(".iso") else f"{stem}.iso"
        elif mode in {"extractraw", "extracthd"}:
            filename = stem if stem.lower().endswith(".raw") else f"{stem}.raw"
        elif mode == "extractld":
            filename = stem if stem.lower().endswith(".avi") else f"{stem}.avi"
        else:
            filename = f"{stem}.out"

        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)


chdman_service = ChdmanService()
