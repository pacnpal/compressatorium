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
from services.chdman import ConversionCancelled
from services.timeout_policy import compute_progress_stall_timeout

NKIT2_CONVERTIBLE_EXTENSIONS = {".iso", ".gcz", ".wia", ".rvz", ".wbfs"}

logger = logging.getLogger("chd.nkit2")


class NKit2Service:
    """Wrapper for NKit2 binary (Redump-compatible RVZ conversion)."""

    def __init__(self):
        self.nkit2_path = settings.nkit2_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()

    def is_available(self) -> bool:
        """Check if the NKit2 binary exists and is executable."""
        return os.path.isfile(self.nkit2_path) and os.access(
            self.nkit2_path, os.X_OK,
        )

    def _build_convert_command(
        self,
        input_path: str,
        output_path: str,
    ) -> list[str]:
        """Build NKit2 conversion command for Redump-compatible RVZ output.

        Uses fixed settings: rvz:zstd:19:128k to match MAME Redump DATs.
        The trailing :16 is a thread-count hint; it does not affect the
        output format or compression parameters.
        The output directory is derived from output_path.
        """
        output_dir = os.path.dirname(output_path)

        cmd = [
            self.nkit2_path,
            input_path,
            "-task", "convert",
            "-cfg", "n",
            "-wii:convert", "rvz:zstd:19:128k:16",
            "-gc:convert", "rvz:zstd:19:128k:16",
        ]
        if output_dir:
            cmd.extend(["-out", output_dir])

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
        mode: str = "nkit2_rvz",
        compression: str | None = None,  # Unused - NKit2 uses fixed Redump settings
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run NKit2 conversion and yield progress updates."""
        from fastapi.concurrency import run_in_threadpool

        output_dir = os.path.dirname(output_path)
        if output_dir:
            await run_in_threadpool(os.makedirs, output_dir, exist_ok=True)

        # Snapshot RVZ files that already exist in the output directory so
        # the post-conversion fallback only considers newly-created files.
        stem = Path(input_path).stem
        _output_dir = Path(output_dir) if output_dir else Path(".")

        def _snapshot_existing_rvz() -> set[Path]:
            return {
                p.resolve()
                for p in _output_dir.glob(f"{stem}*.rvz")
                if p.is_file()
            }

        pre_existing_rvz: set[Path] = await run_in_threadpool(_snapshot_existing_rvz)

        cmd = self._build_convert_command(input_path, output_path)

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
                "Starting nkit2 pid=%s cmd=%s", process.pid, " ".join(cmd),
            )

        stall_timeout = compute_progress_stall_timeout(
            input_path=input_path,
            base_timeout=getattr(settings, "progress_timeout", 0),
            timeout_per_gib=getattr(settings, "progress_timeout_per_gib", 0),
            timeout_cap=getattr(settings, "progress_timeout_cap", 0),
        )
        last_progress_value = 0
        last_output_size: int | None = None
        last_activity_at = time.monotonic()
        start = last_activity_at
        last_heartbeat_at = start

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
                    logger.debug("Cancelling nkit2 pid=%s", process.pid)
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
                now = time.monotonic()
                if await _check_stall(now):
                    break
                if now - last_heartbeat_at >= 2:
                    elapsed = int(now - start)
                    yield {
                        "progress": last_progress_value,
                        "message": f"Converting... ({elapsed}s)",
                    }
                    last_heartbeat_at = now
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
                            if progress is not None and progress > last_progress_value:
                                last_progress_value = progress
                                last_activity_at = now
                            yield {
                                "progress": (
                                    progress
                                    if progress is not None
                                    else last_progress_value
                                ),
                                "message": line,
                            }
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
                            if progress is not None and progress > last_progress_value:
                                last_progress_value = progress
                                last_activity_at = now
                            yield {
                                "progress": (
                                    progress
                                    if progress is not None
                                    else last_progress_value
                                ),
                                "message": line,
                            }
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
            if progress is not None and progress > last_progress_value:
                last_progress_value = progress
                last_activity_at = now
            yield {
                "progress": (
                    progress if progress is not None else last_progress_value
                ),
                "message": line,
            }

        await process.wait()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "nkit2 pid=%s exit=%s", process.pid, process.returncode,
            )
        self._untrack_pid(process.pid)

        if cancel_task:
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                logger.debug("cancel_task was cancelled after nkit2 process completion")

        if stall_error:
            raise RuntimeError(stall_error)

        if cancelled_by_request:
            raise ConversionCancelled("Conversion cancelled")

        if process.returncode != 0:
            tail = "\n".join(output_lines[-6:])
            if tail:
                raise RuntimeError(
                    f"nkit2 failed with return code "
                    f"{process.returncode}.\nLast output:\n{tail}",
                )
            raise RuntimeError(
                f"nkit2 failed with return code {process.returncode}",
            )

        # NKit2 may name the output differently; if our expected output_path
        # doesn't exist, look for .rvz files in the output directory.
        # Only consider files that were NOT present before the conversion
        # started (using the pre-snapshot), then pick the newest one.
        if not os.path.exists(output_path):
            output_path_resolved = Path(output_path).resolve()
            candidates = [
                candidate
                for candidate in _output_dir.glob(f"{stem}*.rvz")
                if (
                    candidate.is_file()
                    and candidate.resolve() != output_path_resolved
                    and candidate.resolve() not in pre_existing_rvz
                )
            ]
            if candidates:
                candidate = max(
                    candidates,
                    key=lambda p: (p.stat().st_mtime_ns, str(p)),
                )
                os.rename(str(candidate), output_path)

        if not os.path.exists(output_path):
            raise RuntimeError(
                "NKit2 exited successfully but produced no output file at "
                f"{output_path!r}"
            )

        yield {"progress": 100, "message": "Conversion complete"}

    def _parse_progress(self, line: str) -> int | None:
        """Parse NKit2 output for progress percentage."""
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if match:
            return min(99, int(float(match.group(1))))
        return None

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible by NKit2."""
        lower = filename.lower()
        if lower.endswith(".nkit.iso"):
            return True
        ext = Path(filename).suffix.lower()
        return ext in NKIT2_CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Get the output path for NKit2 RVZ conversion."""
        input_p = Path(input_path)
        stem = input_p.name if treat_as_stem else input_p.stem
        filename = f"{stem}.rvz"

        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)


nkit2_service = NKit2Service()
