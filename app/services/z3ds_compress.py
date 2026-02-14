import asyncio
import logging
import os
import shutil
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from fastapi.concurrency import run_in_threadpool

Z3DS_CONVERTIBLE_EXTENSIONS = {".cci", ".cia"}

Z3DS_OUTPUT_FORMATS = {
    ".cci": ".zcci",
    ".cia": ".zcia",
}

logger = logging.getLogger("z3ds_compress")


class Z3DSCompressService:
    """Wrapper for z3ds_compressor binary."""

    def __init__(self):
        self.z3ds_compressor_path = settings.z3ds_compressor_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()

    def _build_command(
        self,
        input_path: str,
        output_path: str,
    ) -> list[str]:
        """Build command for z3ds_compressor.
        
        The tool takes input and output paths as arguments.
        Format: z3ds_compressor <input> <output>
        """
        cmd = [
            self.z3ds_compressor_path,
            input_path,
            output_path,
        ]

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

    def get_output_path(self, input_path: str, output_dir: str | None = None) -> str:
        """Calculate output path for a 3DS file.
        
        Args:
            input_path: Path to input .cci or .cia file
            output_dir: Optional output directory. If None, uses same directory as input.
            
        Returns:
            Path for output .zcci or .zcia file
        """
        input_file = Path(input_path)
        ext = input_file.suffix.lower()
        
        if ext not in Z3DS_OUTPUT_FORMATS:
            raise ValueError(f"Unsupported file extension: {ext}")
        
        output_ext = Z3DS_OUTPUT_FORMATS[ext]
        output_name = input_file.stem + output_ext
        
        if output_dir:
            return str(Path(output_dir) / output_name)
        return str(input_file.parent / output_name)

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = "z3ds_compress",  # Unused but required for interface consistency with chdman/dolphin services
        compression: str | None = None,  # Unused but required for interface consistency
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run z3ds_compressor and yield progress updates.

        Args:
            input_path: Path to input file (.cci or .cia)
            output_path: Path for output file (.zcci or .zcia)
            cancel_event: Optional event to signal cancellation

        Yields:
            dict: {"progress": int, "message": str}
        """
        # Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir:
            await run_in_threadpool(os.makedirs, output_dir, exist_ok=True)

        cmd = self._build_command(input_path, output_path)

        def _preexec():
            if settings.chdman_nice is not None:
                try:
                    os.nice(settings.chdman_nice)
                except OSError:
                    pass

        # cmd is built from validated settings paths (no shell interpretation);
        # create_subprocess_exec passes args directly without shell expansion.
        process = await asyncio.create_subprocess_exec(
            cmd[0], *cmd[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=_preexec if os.name == "posix" else None,
        )
        self._track_pid(process.pid)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Starting z3ds_compressor pid=%s cmd=%s", process.pid, " ".join(cmd))

        stall_timeout = max(0, int(getattr(settings, "progress_timeout", 0) or 0))
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

        yield {"progress": 5, "message": "Starting 3DS compression..."}

        output_lines = []
        try:
            while True:
                try:
                    line_bytes = await asyncio.wait_for(
                        process.stdout.readline(), timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    line_bytes = None

                if line_bytes:
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if line:
                        output_lines.append(line)
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("z3ds_compressor output: %s", line)
                        last_activity_at = time.monotonic()

                # Check output file size for progress
                if os.path.exists(output_path):
                    try:
                        current_size = os.path.getsize(output_path)
                        if last_output_size is None or current_size != last_output_size:
                            last_output_size = current_size
                            last_activity_at = time.monotonic()
                            # Estimate progress based on typical 50% compression ratio
                            if os.path.exists(input_path):
                                input_size = os.path.getsize(input_path)
                                if input_size > 0:
                                    # Expect output to be ~50% of input
                                    expected_output_size = input_size * 0.5
                                    progress = min(95, int((current_size / expected_output_size) * 90 + 5))
                                    yield {"progress": progress, "message": f"Compressing... ({current_size // (1024*1024)} MB)"}
                    except OSError:
                        pass

                # Check if process has finished
                if process.returncode is not None:
                    break

                # Check for stalls
                if stall_timeout > 0:
                    elapsed_since_activity = time.monotonic() - last_activity_at
                    if elapsed_since_activity > stall_timeout:
                        logger.warning(
                            "z3ds_compressor pid=%s stalled (no progress for %ds), killing",
                            process.pid, int(elapsed_since_activity),
                        )
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        raise TimeoutError(f"Compression stalled (no progress for {stall_timeout}s)")

            # Wait for process to finish
            await process.wait()

        finally:
            self._untrack_pid(process.pid)
            if cancel_task:
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass

        if cancelled_by_request:
            # Clean up partial output
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            raise asyncio.CancelledError("Compression cancelled by user")

        if process.returncode != 0:
            error_msg = "\n".join(output_lines[-10:]) if output_lines else "Unknown error"
            raise RuntimeError(f"z3ds_compressor failed with exit code {process.returncode}: {error_msg}")

        yield {"progress": 100, "message": "3DS compression complete"}

    def info(self, file_path: str) -> dict:
        """Get basic information about a 3DS ROM file.
        
        Since z3ds_compressor doesn't provide metadata extraction, this method
        returns basic file system information: size, format, compression status.
        
        Note: This is a synchronous method. Callers should wrap with run_in_threadpool
        if calling from async context.
        
        Args:
            file_path: Path to .cci, .cia, .zcci, or .zcia file
            
        Returns:
            dict with file info (file, size, format, compressed, etc.)
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
            
        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()
        
        # Determine format and compression status
        is_compressed = ext in {".zcci", ".zcia"}
        base_format = None
        if ext in {".cci", ".zcci"}:
            base_format = "CCI (Cart Image)"
        elif ext in {".cia", ".zcia"}:
            base_format = "CIA (Installable Archive)"
        
        # Format size for display
        size_mb = file_size / (1024 * 1024)
        size_display = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"
        
        return {
            "file": file_path,
            "size": file_size,
            "size_display": size_display,
            "format": base_format,
            "extension": ext,
            "compressed": is_compressed,
            "compression_type": "Seekable ZStandard" if is_compressed else None,
        }

    @staticmethod
    def is_convertible(filename: str) -> bool:
        """Check if a file is convertible by z3ds_compress.
        
        Args:
            filename: Name of the file to check
            
        Returns:
            True if the file has a .cci or .cia extension
        """
        ext = Path(filename).suffix.lower()
        return ext in Z3DS_CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_output_path_for_mode(
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        """Get the output path for z3ds_compress mode.
        
        Args:
            mode: Conversion mode (should be "z3ds_compress")
            input_path: Path to input file or stem
            output_dir: Optional output directory
            treat_as_stem: If True, treat input_path as stem without extension
            
        Returns:
            Path for output file
            
        Note:
            When treat_as_stem=True (archive members), the method defaults to .zcci
            output since we cannot determine the original extension from the stem alone.
            However, z3ds_compress mode blocks archive inputs, so this case should not occur.
        """
        input_p = Path(input_path)
        
        if treat_as_stem:
            # input_path is just a stem (no extension)
            # Default to .zcci since we can't determine .cci vs .cia from stem alone
            # Note: This case should not occur as archives are blocked for z3ds mode
            stem = input_p.name
            output_ext = ".zcci"
        else:
            # Normal case: extract stem and map extension
            stem = input_p.stem
            ext = input_p.suffix.lower()
            
            # Map input extension to output extension
            if ext in Z3DS_OUTPUT_FORMATS:
                output_ext = Z3DS_OUTPUT_FORMATS[ext]
            else:
                # Default to .zcci if extension unknown
                output_ext = ".zcci"
            
        filename = f"{stem}{output_ext}"
        
        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)


# Global service instance
z3ds_compress_service = Z3DSCompressService()
