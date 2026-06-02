"""Z3dsTool, thin plugin wrapper delegating to ``z3ds_compress_service``.

``z3ds_compress_service.info`` is synchronous; the async contract is satisfied
by running it in a threadpool.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from models import OutputStatus, Z3DSInfo
from services.lock_manager import lock_manager
from services.z3ds_compress import (
    Z3DS_CONVERTIBLE_EXTENSIONS,
    Z3DS_OUTPUT_FORMATS,
    z3ds_compress_service,
)

from .base import BaseTool
from .spec import ModeKind, ModeSpec

_Z3DS_OUTPUTS = frozenset(Z3DS_OUTPUT_FORMATS.values())


class Z3dsTool(BaseTool):
    id = "z3ds"
    display_name = "3DS"
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
            allows_archive_input=True,
        ),
    )
    output_extensions = _Z3DS_OUTPUTS
    verify_extensions = _Z3DS_OUTPUTS

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = z3ds_compress_service

    def detect_output(self, input_path: str) -> OutputStatus | None:
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
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        return self._service.convert(
            input_path, output_path, mode,
            compression=compression, cancel_event=cancel_event,
        )

    async def verify(self, path: str) -> dict:
        return await self._service.verify(path)

    def verify_stream(self, path: str) -> AsyncGenerator[dict, None]:
        return self._service.verify_stream(path)

    async def info(self, path: str) -> dict:
        return await run_in_threadpool(self._service.info, path)

    def info_model(self, raw: dict, path: str) -> Z3DSInfo:
        # Direct indexing is intentional: Z3DSInfo's fields are required and
        # z3ds_compress_service.info() always populates them (mirrors the
        # /z3ds-info route). Unlike CHDInfo/DolphinDiscInfo (all-optional),
        # a bare .get() here would inject None for required fields.
        return Z3DSInfo(
            file=raw["file"],
            size=raw["size"],
            size_display=raw["size_display"],
            format=raw.get("format"),
            extension=raw["extension"],
            compressed=raw["compressed"],
            compression_type=raw.get("compression_type"),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
