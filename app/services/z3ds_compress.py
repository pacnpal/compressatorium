import asyncio
import contextlib
import logging
import os
import shutil
import struct
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import aiofiles
from config import settings
from fastapi.concurrency import run_in_threadpool
from services.chdman import ConversionCancelled
from services.timeout_policy import compute_progress_stall_timeout

Z3DS_CONVERTIBLE_EXTENSIONS = {".cci", ".cia", ".3ds"}

Z3DS_OUTPUT_FORMATS = {
    ".cci": ".zcci",
    ".cia": ".zcia",
    ".3ds": ".z3ds",
}

logger = logging.getLogger("chd.z3ds_compress")


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

    @staticmethod
    async def _get_verify_payload_offset(file_path: str) -> int:
        """Return the byte offset where the seekable zstd payload begins."""

        def _read_offset() -> int:
            with open(file_path, "rb") as fh:
                header = fh.read(0x20)

            if len(header) < 0x20:
                raise ValueError("Invalid Z3DS file: header is too short")

            magic, _underlying_magic, _version, _reserved, header_size, metadata_size, _compressed_size, _uncompressed_size = struct.unpack(
                "<4s4sBBHIQQ",
                header,
            )
            if magic != b"Z3DS":
                raise ValueError("Invalid Z3DS file: missing Z3DS header")

            payload_offset = int(header_size) + int(metadata_size)
            file_size = os.path.getsize(file_path)
            if payload_offset <= 0 or payload_offset >= file_size:
                raise ValueError("Invalid Z3DS file: payload offset is out of range")
            return payload_offset

        return await run_in_threadpool(_read_offset)

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
        try:
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
            if process.stdout is None:
                raise RuntimeError("z3ds_compressor stdout is not available")

            self._track_pid(process.pid)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Starting z3ds_compressor pid=%s cmd=%s",
                    process.pid,
                    " ".join(cmd),
                )

            stall_timeout = compute_progress_stall_timeout(
                input_path=input_path,
                base_timeout=getattr(settings, "progress_timeout", 0),
                timeout_per_gib=getattr(settings, "progress_timeout_per_gib", 0),
                timeout_cap=getattr(settings, "progress_timeout_cap", 0),
            )
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

            output_lines: list[str] = []
            buffer = ""

            def _record_line(raw: str) -> None:
                line = raw.strip()
                if not line:
                    return
                if not output_lines or output_lines[-1] != line:
                    output_lines.append(line)
                    if len(output_lines) > 30:
                        output_lines.pop(0)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("z3ds_compressor output: %s", line)

            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(process.stdout.read(256), timeout=2.0)
                    except asyncio.TimeoutError:
                        chunk = None
                    
                    if chunk == b"":
                        break

                    if chunk:
                        buffer += chunk.decode("utf-8", errors="replace")
                        last_activity_at = time.monotonic()

                        while True:
                            sep_positions = [i for i in (buffer.find("\r"), buffer.find("\n")) if i >= 0]
                            if not sep_positions:
                                break
                            sep_index = min(sep_positions)
                            _record_line(buffer[:sep_index])
                            buffer = buffer[sep_index + 1:]
                            last_activity_at = time.monotonic()

                    # Check output file size for progress
                    if os.path.exists(output_path):
                        try:
                            current_size = os.path.getsize(output_path)
                            if last_output_size is None or current_size != last_output_size:
                                last_output_size = current_size
                                last_activity_at = time.monotonic()
                                # Estimate progress based on typical 50% compression ratio
                                # The 0.5 factor below is a heuristic based on typical compression
                                # ratios (~50% of original size) observed for supported 3DS/CCI/CIA
                                # ROMs when using this tool. It is used *only* for progress
                                # estimation; the actual compression ratio can be higher or lower
                                # depending on the specific ROM and compression settings. If you
                                # consistently see progress jump from a low value directly to 100%,
                                # or stay high for too long, consider adjusting this factor (e.g.
                                # to 0.3 for stronger compression or 0.7 for weaker compression),
                                # or making it configurable. Deviations from the assumed ratio
                                # affect only the perceived smoothness/accuracy of the progress
                                # bar, not the correctness of the compression itself.
                                if os.path.exists(input_path):
                                    input_size = os.path.getsize(input_path)
                                    if input_size > 0:
                                        # Expect output to be ~50% of input
                                        expected_output_size = input_size * 0.5
                                        progress = min(95, int((current_size / expected_output_size) * 90 + 5))
                                        yield {
                                            "progress": progress,
                                            "message": f"Compressing... ({current_size // (1024*1024)} MB)",
                                        }
                        except OSError:
                            pass

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
                            raise TimeoutError(
                                f"Compression stalled (no progress for {stall_timeout}s)",
                            )

                await process.wait()

                if buffer.strip():
                    _record_line(buffer)

            finally:
                if cancel_task:
                    cancel_task.cancel()
                    try:
                        await cancel_task
                    except asyncio.CancelledError:
                        pass
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()
                self._untrack_pid(process.pid)

            if cancelled_by_request:
                # Clean up partial output
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except OSError:
                        pass
                raise ConversionCancelled("Compression cancelled by user")

            if process.returncode != 0:
                error_msg = "\n".join(output_lines[-10:]) if output_lines else "Unknown error"
                raise RuntimeError(
                    f"z3ds_compressor failed with exit code {process.returncode}: {error_msg}",
                )

            yield {"progress": 100, "message": "3DS compression complete"}

        except Exception as e:
            logger.exception("Error in z3ds_compress.convert: %s", e)
            raise

    def info(self, file_path: str) -> dict:
        """Get basic information about a 3DS ROM file.
        
        Since z3ds_compressor doesn't provide metadata extraction, this method
        returns basic file system information: size, format, compression status.
        
        Note: This is a synchronous method. Callers should wrap with run_in_threadpool
        if calling from async context.
        
        Args:
            file_path: Path to .cci, .cia, .3ds, .zcci, .zcia, or .z3ds file
            
        Returns:
            dict with file info (file, size, format, compressed, etc.)
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
            
        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()
        
        # Determine format and compression status
        is_compressed = ext in {".zcci", ".zcia", ".z3ds"}
        base_format = None
        if ext in {".cci", ".zcci"}:
            base_format = "CCI (Cart Image)"
        elif ext in {".cia", ".zcia"}:
            base_format = "CIA (Installable Archive)"
        elif ext in {".3ds", ".z3ds"}:
            base_format = "3DS (Cart Image)"
        
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


    async def verify(self, file_path: str) -> dict:
        """Verify the integrity of a compressed 3DS file.

        Since z3ds_compressor lacks a native verify mode, we perform basic checks:
        1. File exists
        2. File is not empty
        3. File has correct extension
        
        Returns:
            dict: {"valid": bool, "message": str}
        """
        final = {"valid": False, "message": "Verification failed"}
        async for update in self.verify_stream(file_path):
            if update.get("type") in ("complete", "error"):
                final = update
        return {
            "valid": bool(final.get("valid", False)),
            "message": final.get("message") or "Verification failed",
        }

    async def verify_stream(self, file_path: str) -> AsyncGenerator[dict, None]:
        """Stream verification progress.
        
        For now, this is a simulated verification since we rely on checks that
        are seemingly instantaneous. We add a small delay to ensure the UI
        has time to register the 'verifying' state.
        """
        if not os.path.exists(file_path):
            yield {
                "type": "error", 
                "valid": False, 
                "message": "File not found"
            }
            return

        if os.path.getsize(file_path) == 0:
            yield {
                "type": "error", 
                "valid": False, 
                "message": "File is empty"
            }
            return

        ext = Path(file_path).suffix.lower()
        if ext not in {".zcci", ".zcia", ".z3ds"}:
             yield {
                "type": "error", 
                "valid": False, 
                "message": f"Invalid extension: {ext}"
            }
             return

        # Perform deep verification using zstd -t.
        # Container metadata length is variable, so compute the payload offset
        # from the on-disk Z3DS header fields.

        try:
            zstd_path = shutil.which("zstd")
            if not zstd_path:
                yield {
                    "type": "error",
                    "valid": False,
                    "message": "zstd not found; full integrity verification is unavailable",
                }
                return

            payload_offset = await self._get_verify_payload_offset(file_path)

            # Start zstd -t process reading from stdin
            process = await asyncio.create_subprocess_exec(
                zstd_path,
                "-t",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._track_pid(process.pid)

            try:
                yield {"type": "progress", "progress": 0, "message": "Verifying integrity..."}

                # Stream file to zstd process from the computed payload offset.
                chunk_size = 1024 * 1024  # 1MB chunks
                try:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Starting z3ds verification stream for %s (offset=%d)",
                            file_path,
                            payload_offset,
                        )

                    async with aiofiles.open(file_path, "rb") as f:
                        await f.seek(payload_offset)

                        while True:
                            chunk = await f.read(chunk_size)
                            if not chunk:
                                break
                            try:
                                if process.stdin is None:
                                    raise RuntimeError("zstd stdin is unavailable")
                                process.stdin.write(chunk)
                                await process.stdin.drain()
                            except BrokenPipeError:
                                # zstd closed stdin early due to integrity failure.
                                break

                    if process.stdin is not None:
                        process.stdin.close()
                        await process.stdin.wait_closed()

                except Exception as stream_err:
                    logger.error("Error streaming to zstd: %s", stream_err)
                    raise

                # Wait for process to finish
                _stdout, stderr = await process.communicate()

                if process.returncode == 0:
                    yield {"type": "progress", "progress": 100, "message": "Integrity check passed"}
                    yield {
                        "type": "complete",
                        "valid": True,
                        "message": "File verified successfully"
                    }
                else:
                    stderr_text = stderr.decode("utf-8", errors="replace").strip()
                    yield {
                        "type": "error",
                        "valid": False,
                        "message": f"Integrity check failed: {stderr_text}"
                    }
            finally:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()
                self._untrack_pid(process.pid)

        except Exception as e:
            logger.exception("Error during 3DS verification: %s", e)
            yield {
                "type": "error", 
                "valid": False, 
                "message": f"Verification error: {str(e)}"
            }

# Global service instance
z3ds_compress_service = Z3DSCompressService()
