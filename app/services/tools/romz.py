"""RomzTool, thin plugin wrapper delegating to ``romz_service``.

Packs handheld ROM dumps (GB/GBC/GBA/NDS) into ``.7z``/``.zip`` archives and
extracts them back. ``romz_service.info`` is synchronous; the async contract is
satisfied by running it in a threadpool.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from models import OutputStatus, RomzInfo
from services.lock_manager import lock_manager
from services.romz import (
    ROMZ_ARCHIVE_EXTENSIONS,
    ROMZ_COMPRESS_EXTENSIONS,
    ROMZ_OUTPUT_BY_MODE,
    romz_service,
)

from .base import BaseTool
from .spec import ModeKind, ModeSpec

# The compress-direction badge prefers .7z (the default product), then .zip.
_PRIMARY_OUTPUT_EXT = ".7z"


class RomzTool(BaseTool):
    id = "romz"
    display_name = "Handheld ROM"
    modes = (
        ModeSpec(
            mode="romz_7z",
            tool_id="romz",
            kind=ModeKind.COMPRESS,
            label="Compress ROM → 7z",
            group="romz",
            output_ext=".7z",
            input_extensions=frozenset(ROMZ_COMPRESS_EXTENSIONS),
            # The UI sends an effort preset (fast|default|max), no numeric level.
            supports_compression=True,
            supports_delete_on_verify=True,
            # ROMs are compressed loose, not from inside another archive (that
            # would be recursive), so archive members are never offered here.
            allows_archive_input=False,
        ),
        ModeSpec(
            mode="romz_zip",
            tool_id="romz",
            kind=ModeKind.COMPRESS,
            label="Compress ROM → zip",
            group="romz",
            output_ext=".zip",
            input_extensions=frozenset(ROMZ_COMPRESS_EXTENSIONS),
            supports_compression=True,
            supports_delete_on_verify=True,
            allows_archive_input=False,
        ),
        ModeSpec(
            mode="romz_extract",
            tool_id="romz",
            kind=ModeKind.EXTRACT,
            label="Extract ROM ← 7z/zip",
            group="romz",
            output_ext=None,  # restored from the single archived member
            input_extensions=frozenset(ROMZ_ARCHIVE_EXTENSIONS),
            # No delete-on-verify: the output is a raw ROM, which is not in
            # verify_extensions (we verify archives), so we can't confirm the
            # output before deleting the source.
            supports_delete_on_verify=False,
            allows_archive_input=False,
        ),
    )
    # Everything the tool can produce (both directions), for output badges and
    # the registry-driven library scan.
    output_extensions = frozenset(
        set(ROMZ_OUTPUT_BY_MODE.values()) | ROMZ_COMPRESS_EXTENSIONS,
    )
    # `7z t` validates the archives this tool writes.
    verify_extensions = frozenset(ROMZ_ARCHIVE_EXTENSIONS)

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = romz_service

    def detect_output(self, input_path: str) -> OutputStatus | None:
        # Compress direction only: badge "the .7z/.zip already exists" next to a
        # ROM source. Extract-direction badging is out of scope.
        source = Path(input_path)
        if source.suffix.lower() not in ROMZ_COMPRESS_EXTENSIONS:
            return None
        # Output names preserve the ROM extension (Game.gba -> Game.gba.7z), so
        # the candidate is the full filename plus the archive suffix.
        for ext in (_PRIMARY_OUTPUT_EXT, ".zip"):
            candidate = f"{input_path}{ext}"
            file_exists, is_converting = lock_manager.check_file_status(candidate)
            if file_exists or is_converting:
                return OutputStatus(
                    tool_id=self.id,
                    exists=file_exists,
                    ready=file_exists and not is_converting,
                    path=candidate,
                )
        return None

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

    def info_model(self, raw: dict, path: str) -> RomzInfo:
        return RomzInfo(
            file=raw["file"],
            size=raw["size"],
            size_display=raw["size_display"],
            format=raw.get("format"),
            extension=raw["extension"],
            compressed=raw["compressed"],
            compression_type=raw.get("compression_type"),
            contained_name=raw.get("contained_name"),
            original_size=raw.get("original_size"),
            ratio=raw.get("ratio"),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
