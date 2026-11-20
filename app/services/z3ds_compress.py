import asyncio
from logging_setup import get_logger
import os
import shutil
import struct
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.chdman import ConversionCancelled
from services.subprocess_runner import (
    RunnerService,
    SubprocessRunner,
    ReadCancelled,
    remove_partial_output,
    run_detached,
    verify_preflight,
    ToolUnavailable,
    verify_exception_event,
)
from utils.sizes import format_size

# Compress inputs (raw 3DS ROMs). The upstream fork
# (https://github.com/pacnpal/z3ds_compress) added .cxi/.3dsx alongside the
# original .cci/.cia/.3ds.
Z3DS_CONVERTIBLE_EXTENSIONS = {".cci", ".cia", ".3ds", ".cxi", ".3dsx"}

# Compress map: raw ROM extension -> compressed (Z3DS) extension.
Z3DS_OUTPUT_FORMATS = {
    ".cci": ".zcci",
    ".cia": ".zcia",
    ".3ds": ".z3ds",
    ".cxi": ".zcxi",
    ".3dsx": ".z3dsx",
}

# Decompress inputs (compressed Z3DS containers) and the reverse extension map.
# The fork made 3DS round-trippable: it auto-detects direction from the "Z3DS"
# magic header and exposes -c/-d to force it (we always pass the explicit flag).
Z3DS_DECOMPRESS_EXTENSIONS = {".zcci", ".zcia", ".z3ds", ".zcxi", ".z3dsx"}

Z3DS_DECOMPRESS_FORMATS = {
    ".zcci": ".cci",
    ".zcia": ".cia",
    ".z3ds": ".3ds",
    ".zcxi": ".cxi",
    ".z3dsx": ".3dsx",
}

logger = get_logger("z3ds_compress")


class Z3DSCompressService(RunnerService):
    """Wrapper for z3ds_compressor binary."""

    def __init__(self):
        self.z3ds_compressor_path = settings.z3ds_compressor_path
        # convert() and verify_stream() share the runner's PID set so a single
        # active_pids() sees both; convert() delegates its whole streaming loop
        # to the runner (verify_stream still spawns zstd directly).
        self._runner = SubprocessRunner(owner="z3ds")

    def _build_command(
        self,
        input_path: str,
        output_path: str,
        mode: str = "z3ds_compress",
    ) -> list[str]:
        """Build command for z3ds_compressor.

        The fork auto-detects direction from the "Z3DS" magic header, but we
        always pass an explicit ``-c`` (compress) / ``-d`` (decompress) flag so
        the job's mode, not the file contents, decides the direction. The tool
        takes input and output paths as positional arguments.
        Format: ``z3ds_compressor <-c|-d> <input> <output>``
        """
        flag = "-d" if mode == "z3ds_decompress" else "-c"
        cmd = [
            self.z3ds_compressor_path,
            flag,
            input_path,
            output_path,
        ]
        return cmd

    @staticmethod
    async def _get_verify_payload_offset(
        file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> int:
        """Return the byte offset where the seekable zstd payload begins.

        Read on a detached thread rather than a pooled one: on an unresponsive
        mount this open/read cannot be stopped, only abandoned, and abandoning a
        *shared* worker for every cancelled verify would eventually starve the
        pool the rest of the app offloads to.
        """

        def _read_offset() -> int:
            with open(file_path, "rb") as fh:
                header = fh.read(0x20)

            if len(header) < 0x20:
                raise ValueError("Invalid Z3DS file: header is too short")

            (
                magic,
                _underlying_magic,
                _version,
                _reserved,
                header_size,
                metadata_size,
                _compressed_size,
                _uncompressed_size,
            ) = struct.unpack(
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

        return await run_detached(_read_offset, cancel_event=cancel_event)

    @staticmethod
    def decode_command() -> list[str] | None:
        """The decoder the Round-trip pipes a Z3DS payload through, or None.

        The payload after the header and metadata is seekable zstd: ordinary
        zstd frames holding the ROM, then a skippable seek-table frame that
        ``zstd -d`` passes over. The same binary ``verify`` uses for ``-t``.
        """
        zstd_path = shutil.which("zstd")
        return [zstd_path, "-d", "-c", "-q"] if zstd_path else None

    async def decoded_sha256(
        self, file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> str:
        """SHA-256 of the ROM a Z3DS container decodes to, streamed (#301).

        The payload is fed to ``zstd -dc`` and its stdout hashed as it arrives
        (``SubprocessRunner.hash_stdout``), so no decoded ROM is written. The
        caller bounds the call; a cancel stops zstd and raises ReadCancelled.
        """
        # The PATH lookup stats directories, so it runs off the event loop too.
        cmd = await run_detached(self.decode_command, cancel_event=cancel_event)
        if cmd is None:
            raise RuntimeError("zstd not found; the decoded ROM cannot be hashed")
        offset = await self._get_verify_payload_offset(
            file_path, cancel_event=cancel_event,
        )
        return await self._runner.hash_stdout(
            cmd, algorithm="sha256", stdin_path=file_path, stdin_offset=offset,
            cancel_event=cancel_event,
        )

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
        # `compression` is unused (3DS has no codec/level picker) but kept for
        # interface consistency with chdman/dolphin services. `mode` selects the
        # direction: "z3ds_compress" (default) or "z3ds_decompress".
        mode: str = "z3ds_compress",
        *,
        compression: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run z3ds_compressor on ``input_path``, yielding progress dicts.

        ``mode`` selects the direction (``z3ds_compress`` packs a raw ROM into a
        Z3DS container; ``z3ds_decompress`` restores the original ROM). The
        explicit ``-c``/``-d`` flag is passed so the job's mode, not the file's
        magic header, decides direction. Honors ``cancel_event`` (terminate +
        clean partial output, raising ``ConversionCancelled``) and a stall
        timeout. Yields ``{"progress": int, "message": str}`` and a final 100%.
        """
        decompress = mode == "z3ds_decompress"
        verb = "decompression" if decompress else "compression"
        cmd = self._build_command(input_path, output_path, mode)
        yield {"progress": 5, "message": f"Starting 3DS {verb}..."}

        # Delegate the streaming spawn / stall / cancel / PID loop to the shared
        # runner, which applies nice/ionice (owner "z3ds"). parse_progress is a
        # no-op — there is no parseable percent — so the runner's size-growth
        # fallback drives the bar from the growing output file.
        try:
            async for update in self._runner.run(
                cmd,
                input_path=input_path,
                output_path=output_path,
                parse_progress=lambda _line: None,
                initial_progress=5,
                cancel_event=cancel_event,
                fail_label="z3ds_compressor",
                complete_message=f"3DS {verb} complete",
                mode=mode,
            ):
                yield update
        except (ConversionCancelled, RuntimeError, asyncio.CancelledError, GeneratorExit):
            # z3ds_compressor writes the container in place. These are the
            # abnormal exits the runner can raise after it spawned the child — a
            # mid-run cancel, a non-zero/stall RuntimeError, or a task-cancellation
            # / generator close — so a partial may be on disk. The shared helper
            # removes it under a hard time bound, so a retry isn't blocked by a
            # truncated file and an unresponsive mount can't hold the job (and
            # with it the queue) here. Setup (above this try) and pre-spawn
            # failures are not caught here, so a pre-existing output is never
            # deleted for a conversion that wrote nothing.
            await remove_partial_output(output_path, label="z3ds output")
            raise

    def info(self, file_path: str) -> dict:
        """Get basic information about a 3DS ROM file.

        Since z3ds_compressor doesn't provide metadata extraction, this method
        returns basic file system information: size, format, compression status.

        Note: This is a synchronous method. Callers should wrap with run_in_threadpool
        if calling from async context.

        Args:
            file_path: Path to a raw ROM (.cci/.cia/.3ds/.cxi/.3dsx) or a
                compressed container (.zcci/.zcia/.z3ds/.zcxi/.z3dsx)

        Returns:
            dict with file info (file, size, format, compressed, etc.)
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()

        # Determine format and compression status
        is_compressed = ext in Z3DS_DECOMPRESS_EXTENSIONS
        base_format = None
        if ext in {".cci", ".zcci"}:
            base_format = "CCI (Cart Image)"
        elif ext in {".cia", ".zcia"}:
            base_format = "CIA (Installable Archive)"
        elif ext in {".3ds", ".z3ds"}:
            base_format = "3DS (Cart Image)"
        elif ext in {".cxi", ".zcxi"}:
            base_format = "CXI (Executable Image)"
        elif ext in {".3dsx", ".z3dsx"}:
            base_format = "3DSX (Homebrew)"

        # Format size for display
        size_display = format_size(file_size)

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
        """Get the output path for a z3ds mode.

        Args:
            mode: Conversion mode ("z3ds_compress" or "z3ds_decompress")
            input_path: Path to input file or stem
            output_dir: Optional output directory
            treat_as_stem: If True, treat input_path as stem without extension

        Returns:
            Path for output file

        Note:
            ``treat_as_stem=True`` is used for archive members. The member's
            original extension is preserved in the synthetic filename (see
            ``ArchiveService._output_name_for_member``), so it maps the same
            way as an on-disk file: compress maps .3ds -> .z3ds, .cci -> .zcci,
            etc.; decompress reverses it (.z3ds -> .3ds, .zcci -> .cci). It only
            falls back to a default extension when the input extension is
            missing or unrecognised.
        """
        input_p = Path(input_path)

        # Both branches treat the input as a filename: archive members arrive
        # as flattened filenames that keep their original extension, so the
        # output mapping is identical to the on-disk case.
        stem = input_p.stem
        ext = input_p.suffix.lower()
        if mode == "z3ds_decompress":
            output_ext = Z3DS_DECOMPRESS_FORMATS.get(ext, ".3ds")
        else:
            output_ext = Z3DS_OUTPUT_FORMATS.get(ext, ".zcci")

        filename = f"{stem}{output_ext}"

        if output_dir:
            return str(Path(output_dir) / filename)
        return str(input_p.parent / filename)

    async def verify_stream(
        self, file_path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream verification progress for a compressed 3DS file.

        Performs deep integrity verification by piping the file through `zstd -t`
        to validate the ZStandard compression stream. This ensures the compressed
        data is not corrupted and can be successfully decompressed.
        """
        # Bounded, off the event loop: an unresponsive volume must fail this
        # verify, not freeze every task in the process (see verify_preflight).
        problem, _size = await verify_preflight(
            file_path, Z3DS_DECOMPRESS_EXTENSIONS, cancel_event=cancel_event,
        )
        if problem is not None:
            yield problem
            return

        # Perform deep verification using zstd -t.
        # Container metadata length is variable, so compute the payload offset
        # from the on-disk Z3DS header fields.

        try:
            zstd_path = shutil.which("zstd")
            if not zstd_path:
                yield verify_exception_event(ToolUnavailable(
                    "zstd not found; full integrity verification is unavailable",
                ), prefix="Verification could not run")
                return

            payload_offset = await self._get_verify_payload_offset(
                file_path, cancel_event=cancel_event,
            )
            # The shared one-shot verifier (#543): zstd is fed the payload on
            # stdin, bounded by the size-scaled verify timeout and racing the
            # cancel, and stopped through the runner's TERM -> KILL ladder.
            async for event in self._runner.capture_verify(
                [zstd_path, "-t"], path=file_path, success_message="File is Intact",
                cancel_event=cancel_event, stdin_path=file_path,
                stdin_offset=payload_offset,
            ):
                yield event

        except ReadCancelled:
            # Cancelled while reading the header: no verdict, so report the
            # cancellation rather than a verification error.
            yield {
                "type": "error",
                "valid": False,
                "cancelled": True,
                "message": "Verification cancelled",
            }
        except Exception as e:
            logger.exception("Error during 3DS verification: %s", e)
            yield verify_exception_event(e)


# Global service instance
z3ds_compress_service = Z3DSCompressService()
