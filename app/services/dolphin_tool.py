import asyncio
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings

DOLPHIN_CONVERTIBLE_EXTENSIONS = {".iso", ".gcz", ".wia", ".rvz", ".wbfs"}

DOLPHIN_OUTPUT_FORMATS = {
    "dolphin_rvz": ("rvz", ".rvz"),
    "dolphin_wia": ("wia", ".wia"),
    "dolphin_gcz": ("gcz", ".gcz"),
    "dolphin_iso": ("iso", ".iso"),
}

logger = logging.getLogger("chd.dolphin_tool")


class DolphinToolService:
    """Wrapper for dolphin-tool binary."""

    def __init__(self):
        self.dolphin_tool_path = settings.dolphin_tool_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()

    def _build_convert_command(
        self,
        mode: str,
        input_path: str,
        output_path: str,
        compression: str | None = None,
    ) -> list[str]:
        fmt_name, _ = DOLPHIN_OUTPUT_FORMATS.get(mode, ("rvz", ".rvz"))

        cmd = [
            self.dolphin_tool_path,
            "convert",
            "-i", input_path,
            "-o", output_path,
            "-f", fmt_name,
        ]

        if fmt_name == "rvz":
            cmd.extend(["-b", "131072"])

        if compression and fmt_name in ("rvz", "wia"):
            if "," in compression:
                raise ValueError(
                    "dolphin-tool supports a single compression codec at a time",
                )
            codec = compression
            level = None
            if ":" in compression:
                codec, level = compression.split(":", 1)
            cmd.extend(["-c", codec])
            if level:
                cmd.extend(["-l", level])

        if (
            settings.chdman_ioprio_class is not None
            and settings.chdman_ioprio_level is not None
        ):
            ionice = shutil.which("ionice")
            if ionice:
                cmd = [
                    ionice,
                    "-c", str(settings.chdman_ioprio_class),
                    "-n", str(settings.chdman_ioprio_level),
                ] + cmd

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
        mode: str = "dolphin_rvz",
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run dolphin-tool conversion and yield progress updates."""
        from fastapi.concurrency import run_in_threadpool

        output_dir = os.path.dirname(output_path)
        if output_dir:
            await run_in_threadpool(os.makedirs, output_dir, exist_ok=True)

        cmd = self._build_convert_command(
            mode, input_path, output_path, compression=compression,
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
            logger.debug(
                "Starting dolphin-tool pid=%s cmd=%s",
                process.pid, " ".join(cmd),
            )

        stall_timeout = max(
            0, int(getattr(settings, "progress_timeout", 0) or 0),
        )
        last_progress_value = 0
        last_output_size: int | None = None
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
                    logger.debug(
                        "Cancelling dolphin-tool pid=%s", process.pid,
                    )
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()

            cancel_task = asyncio.create_task(_cancel_watcher())

        buffer = ""
        output_lines: list[str] = []
        stall_error: str | None = None

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
                "Conversion stalled: no progress increase or output growth "
                f"for {stall_timeout}s (progress={last_progress_value}%, "
                f"output_size={last_output_size})"
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
                chunk = await asyncio.wait_for(
                    process.stdout.read(100), timeout=2,
                )
            except asyncio.TimeoutError:
                if cancel_event and cancel_event.is_set():
                    break
                if await _check_stall(time.monotonic()):
                    break
                continue
            if not chunk:
                break

            buffer += chunk.decode("utf-8", errors="replace")

            while "\r" in buffer or "\n" in buffer:
                if "\r" in buffer:
                    parts = buffer.split("\r")
                    for part in parts[:-1]:
                        if part.strip():
                            line = part.strip()
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

        await process.wait()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "dolphin-tool pid=%s exit=%s",
                process.pid, process.returncode,
            )
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
            from services.chdman import ConversionCancelled
            raise ConversionCancelled("Conversion cancelled")

        if process.returncode != 0:
            tail = "\n".join(output_lines[-6:])
            if tail:
                raise RuntimeError(
                    f"dolphin-tool failed with return code "
                    f"{process.returncode}.\nLast output:\n{tail}",
                )
            raise RuntimeError(
                f"dolphin-tool failed with return code {process.returncode}",
            )

        yield {"progress": 100, "message": "Conversion complete"}

    async def header(self, path: str) -> dict:
        """Get header information about a disc image."""
        process = await asyncio.create_subprocess_exec(
            self.dolphin_tool_path,
            "header",
            "-i", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timeout = max(
            0, int(getattr(settings, "chdman_info_timeout", 0) or 0),
        )
        try:
            if timeout:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout,
                )
            else:
                stdout, stderr = await process.communicate()
        except asyncio.TimeoutError:
            await self._terminate_process(process)
            raise RuntimeError(
                f"dolphin-tool header timed out after {timeout}s",
            )

        if process.returncode != 0:
            raise RuntimeError(
                stderr.decode()
                or f"dolphin-tool header failed with code {process.returncode}",
            )

        return self._parse_header(stdout.decode())

    async def verify(self, path: str) -> dict:
        """Verify the integrity of a disc image."""
        final = {"valid": False, "message": "Disc verification failed"}
        async for update in self.verify_stream(path):
            if update.get("type") in ("complete", "error"):
                final = update
        return {
            "valid": bool(final.get("valid", False)),
            "message": final.get("message") or "Disc verification failed",
        }

    async def verify_stream(self, path: str) -> AsyncGenerator[dict, None]:
        """Stream disc image verification progress."""
        process = await asyncio.create_subprocess_exec(
            self.dolphin_tool_path,
            "verify",
            "-i", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._track_pid(process.pid)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Starting dolphin-tool verify pid=%s path=%s",
                process.pid, path,
            )

        output_lines = []
        buffer = ""
        last_output_at = time.monotonic()
        start = last_output_at
        stall_timeout = max(
            0, int(getattr(settings, "verify_progress_timeout", 0) or 0),
        )
        overall_timeout = max(
            0, int(getattr(settings, "chdman_verify_timeout", 0) or 0),
        )
        timeout_error = None

        async def _check_timeouts(now: float) -> bool:
            nonlocal timeout_error
            if overall_timeout > 0 and now - start >= overall_timeout:
                timeout_error = (
                    f"Verification timed out after {overall_timeout}s"
                )
                await self._terminate_process(process)
                return True
            if stall_timeout > 0 and now - last_output_at >= stall_timeout:
                timeout_error = (
                    f"Verification stalled: no output for {stall_timeout}s"
                )
                await self._terminate_process(process)
                return True
            return False

        while True:
            try:
                chunk = await asyncio.wait_for(
                    process.stdout.read(100), timeout=2,
                )
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
            yield {
                "type": "progress",
                "progress": progress,
                "message": line,
            }

        await process.wait()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "dolphin-tool verify pid=%s exit=%s",
                process.pid, process.returncode,
            )
        self._untrack_pid(process.pid)

        if timeout_error:
            yield {
                "type": "error",
                "valid": False,
                "message": timeout_error,
            }
            return

        output_lines = output_lines[-20:]
        output = "\n".join(output_lines).strip()
        if process.returncode == 0:
            yield {
                "type": "complete",
                "valid": True,
                "message": "Disc image verified successfully",
            }
        else:
            yield {
                "type": "error",
                "valid": False,
                "message": output or "Disc verification failed",
            }

    @staticmethod
    async def _terminate_process(
        process: asyncio.subprocess.Process,
    ) -> None:
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
            pass

    def _parse_progress(self, line: str) -> int:
        """Parse dolphin-tool output for progress percentage."""
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if match:
            return min(99, int(float(match.group(1))))
        return 0

    def _parse_header(self, output: str) -> dict:
        """Parse dolphin-tool header output into structured data."""
        info = {"raw_data": output}
        for line in output.split("\n"):
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                value = value.strip()
                info[key] = value
        return info

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible by dolphin-tool."""
        ext = Path(filename).suffix.lower()
        return ext in DOLPHIN_CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Get the output path for a dolphin-tool conversion."""
        input_p = Path(input_path)
        stem = input_p.name if treat_as_stem else input_p.stem
        _, ext = DOLPHIN_OUTPUT_FORMATS.get(mode, ("rvz", ".rvz"))
        filename = f"{stem}{ext}"

        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)


dolphin_tool_service = DolphinToolService()
