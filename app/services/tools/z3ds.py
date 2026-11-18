"""Z3dsTool, thin plugin wrapper delegating to ``z3ds_compress_service``.

``z3ds_compress_service.info`` is synchronous; the async contract is satisfied
by running it in a threadpool.
"""
from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from models import OutputStatus, Z3DSInfo
from services.lock_manager import lock_manager
from services.stream_digest import content_digest_of, file_chunks
from services.subprocess_runner import run_detached
from services.z3ds_compress import (
    Z3DS_CONVERTIBLE_EXTENSIONS,
    Z3DS_DECOMPRESS_EXTENSIONS,
    Z3DS_DECOMPRESS_FORMATS,
    Z3DS_OUTPUT_FORMATS,
    z3ds_compress_service,
)

from .base import (
    IMAGE_DATA_SHA256_KIND,
    BaseTool,
    ContentDigest,
    ContentDigestUnavailable,
)
from .spec import ModeKind, ModeSpec

# Compressed containers: the compress outputs == the decompress inputs == the
# verify-class files. The raw ROMs the decompress direction produces are tracked
# as outputs too (for "output exists" badges / library-scan discovery).
_Z3DS_COMPRESSED = frozenset(Z3DS_OUTPUT_FORMATS.values())
_Z3DS_DECOMPRESSED = frozenset(Z3DS_DECOMPRESS_FORMATS.values())


class Z3dsTool(BaseTool):
    id = "z3ds"
    platform_slugs = frozenset({"3ds", "n3ds", "new-nintendo-3ds"})
    policy_owner = "z3ds"
    display_name = "3DS"
    # File info is the size and the extension's format; z3ds_compressor isn't run.
    info_needs_tool = False
    modes = (
        ModeSpec(
            mode="z3ds_compress",
            tool_id="z3ds",
            kind=ModeKind.COMPRESS,
            label="Compress 3DS",
            group="z3ds",
            output_ext=None,  # mapped from the input extension
            input_extensions=frozenset(Z3DS_CONVERTIBLE_EXTENSIONS),
            supports_delete_on_verify=True,
            # The ROM is hashed as a file; the container's payload is decoded
            # by zstd to a pipe and hashed as it streams (content_digest).
            supports_round_trip=True,
            allows_archive_input=True,
        ),
        ModeSpec(
            mode="z3ds_decompress",
            tool_id="z3ds",
            kind=ModeKind.EXTRACT,
            label="Decompress 3DS",
            group="z3ds",
            output_ext=None,  # mapped (reversed) from the input extension
            input_extensions=frozenset(Z3DS_DECOMPRESS_EXTENSIONS),
            # No delete-on-verify: the output is a raw ROM, which is not in
            # verify_extensions (we only verify the compressed containers), so we
            # can't confirm the output before deleting the source.
            supports_delete_on_verify=False,
            allows_archive_input=True,
        ),
    )
    output_extensions = _Z3DS_COMPRESSED | _Z3DS_DECOMPRESSED
    verify_extensions = _Z3DS_COMPRESSED

    @property
    def verify_not_ready_reason(self) -> str:
        return "zstd is not installed, so 3DS files cannot be checked"

    async def verify_is_ready(self) -> bool:
        # The check streams the payload through `zstd -t` (#306). Detached: a
        # PATH lookup stats directories, and readiness callers fan out.
        return await run_detached(shutil.which, "zstd") is not None

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = z3ds_compress_service

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        # Compress direction only: badge "the .zcci/.z3ds/... already exists"
        # next to a raw ROM source. Decompress-direction badging is out of scope
        # (mirrors maxcso); the job pipeline's check_output_conflicts still
        # guards against overwriting an existing restored ROM.
        source = Path(input_path)
        expected_ext = Z3DS_OUTPUT_FORMATS.get(source.suffix.lower())
        if not expected_ext:
            return None
        candidate = str(source.with_suffix(expected_ext))
        file_exists, is_converting = lock_manager.check_file_status(candidate)
        if not (file_exists or is_converting):
            return None
        return OutputStatus(
            tool_id=self.id,
            exists=file_exists,
            ready=file_exists and not is_converting,
            path=candidate,
        )

    def output_path(
        self,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        return self._service.get_output_path_for_mode(
            mode, input_path, output_dir, treat_as_stem=treat_as_stem,
        )

    def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None = None,
        split: bool = False,  # noqa: ARG002 - split applies only to makeps3iso
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        return self._service.convert(
            input_path, output_path, mode,
            compression=compression, cancel_event=cancel_event,
        )

    async def verify(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        return await self._service.verify(path, cancel_event=cancel_event)

    def verify_stream(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        return self._service.verify_stream(path, cancel_event=cancel_event)

    async def info(self, path: str) -> dict:
        return await run_in_threadpool(self._service.info, path)

    def info_model(self, raw: dict, path: str) -> Z3DSInfo:
        return Z3DSInfo(**self._basic_info_fields(raw))

    async def content_digest(
        self,
        path: str,
        *,
        cancel_event: asyncio.Event | None = None,
        within: Sequence[str] | None = None,  # reads no sheet, so unused
    ) -> ContentDigest | None:
        # Both sides are the SHA-256 of the raw ROM: the ROM file itself, or
        # what a Z3DS container's zstd payload decodes to (no temp file, #301).
        ext = Path(path).suffix.lower()
        if ext in Z3DS_CONVERTIBLE_EXTENSIONS:
            return await content_digest_of(
                file_chunks([path]), kind=IMAGE_DATA_SHA256_KIND,
                algorithm="sha256", cancel_event=cancel_event,
            )
        if ext not in Z3DS_DECOMPRESS_EXTENSIONS:
            return None
        try:
            value = await self._service.decoded_sha256(path, cancel_event=cancel_event)
        except (RuntimeError, ValueError, OSError) as e:
            # ReadCancelled is none of these, so a cancel propagates untouched.
            raise ContentDigestUnavailable(str(e)) from e
        return ContentDigest(kind=IMAGE_DATA_SHA256_KIND, value=value)

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
