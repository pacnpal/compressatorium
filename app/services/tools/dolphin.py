"""DolphinTool — thin plugin wrapper delegating to ``dolphin_tool_service``."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from models import DolphinDiscInfo
from services.dolphin_tool import (
    DOLPHIN_CONVERTIBLE_EXTENSIONS,
    dolphin_tool_service,
)

from .base import BaseTool
from .spec import ModeKind, ModeSpec

# mode -> (label, output_ext, kind, supports_compression, supports_level)
_MODES = {
    "dolphin_rvz": ("Dolphin RVZ", ".rvz", ModeKind.COMPRESS, True, True),
    "dolphin_wia": ("Dolphin WIA", ".wia", ModeKind.COMPRESS, True, True),
    "dolphin_gcz": ("Dolphin GCZ", ".gcz", ModeKind.COMPRESS, False, False),
    "dolphin_iso": ("Dolphin ISO", ".iso", ModeKind.EXTRACT, False, False),
}


def _build_modes() -> list[ModeSpec]:
    return [
        ModeSpec(
            mode=mode,
            tool_id="dolphin",
            kind=kind,
            label=label,
            group="dolphin",
            output_ext=ext,
            input_extensions=DOLPHIN_CONVERTIBLE_EXTENSIONS,
            supports_compression=supports_compression,
            supports_compression_level=supports_level,
            supports_delete_on_verify=True,
        )
        for mode, (label, ext, kind, supports_compression, supports_level)
        in _MODES.items()
    ]


class DolphinTool(BaseTool):
    id = "dolphin"
    display_name = "Dolphin"
    modes = _build_modes()
    output_extensions = frozenset({".rvz", ".wia", ".gcz", ".iso"})
    verify_extensions = DOLPHIN_CONVERTIBLE_EXTENSIONS

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = dolphin_tool_service

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
        return await self._service.header(path)

    def info_model(self, raw: dict, path: str) -> DolphinDiscInfo:
        return DolphinDiscInfo(
            file=path,
            game_id=raw.get("game_id"),
            game_name=(
                raw.get("game_name")
                or raw.get("internal_name")
                or raw.get("name")
            ),
            title_id=raw.get("title_id"),
            disc_number=raw.get("disc_number") or raw.get("disc"),
            revision=raw.get("revision"),
            region=raw.get("region"),
            country=raw.get("country"),
            format=raw.get("format"),
            compression=raw.get("compression") or raw.get("compression_method"),
            compression_level=raw.get("compression_level"),
            block_size=raw.get("block_size"),
            file_size=raw.get("file_size"),
            raw_data=raw.get("raw_data", ""),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
