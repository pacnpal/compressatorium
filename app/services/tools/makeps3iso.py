"""MakePs3IsoTool: the first directory-as-input tool (PS3 folder -> ISO).

One mode, ``folder_to_iso``, whose unit of work is a **directory** rather than a
file with a suffix. Instead of matching ``ext in input_extensions`` it overrides
``accepts_directory`` to run the ``services.ps3`` source-layout detector, which
is also what ``registry.tools_for_directory`` (and the file-listing annotation)
drive off. Everything else delegates to ``makeps3iso_service``.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator

from fastapi.concurrency import run_in_threadpool

from models import OutputStatus, Ps3IsoInfo
from services import ps3
from services.lock_manager import lock_manager
from services.makeps3iso import makeps3iso_service

from .base import BaseTool
from .spec import InputKind, ModeKind, ModeSpec


class MakePs3IsoTool(BaseTool):
    id = "makeps3iso"
    display_name = "PS3 ISO"
    modes = (
        ModeSpec(
            mode="folder_to_iso",
            tool_id="makeps3iso",
            kind=ModeKind.CREATE,
            label="PS3 Folder → ISO",
            group="makeps3iso",
            output_ext=".iso",
            # Directory-driven: there is no input extension to match, so this is
            # empty and selection runs through accepts_directory() instead.
            input_extensions=frozenset(),
            supports_compression=False,
            # No delete-on-verify: deleting a user-curated decrypted folder is
            # destructive and makeps3iso has no native verify (only the light
            # PARAM.SFO TITLE_ID readback).
            supports_delete_on_verify=False,
            allows_archive_input=False,
            input_kinds=frozenset({InputKind.DIRECTORY}),
        ),
    )
    # Intentionally does NOT advertise ".iso" in output_extensions. That union
    # feeds the global metadata/DAT scan walk (registry.scannable_extensions),
    # and ".iso" is a generic source suffix (raw PS2/PSP/Wii/CD-DVD images) that
    # no tool can verify — advertising it would drag every library ISO into a
    # full-file-SHA1 scan. The produced "<folder>.iso" is surfaced via the
    # directory row's detect_output badge instead, which needs no extension
    # registration. (BaseTool defaults output_extensions to an empty set.)

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = makeps3iso_service

    def accepts_directory(self, path: str) -> bool:
        # The directory analogue of `ext in input_extensions`: require the
        # decrypted PS3 disc/JB layout (a PS3_GAME/ root, plus PS3_DISC.SFB for
        # disc rips). Does disk I/O — callers run it off the event loop.
        return ps3.is_ps3_iso_source(path)

    def detect_output(self, input_path: str) -> OutputStatus | None:
        # Sibling "<folder>.iso" badge next to a convertible PS3 folder. Guard on
        # isdir so the file-listing's per-file detection loop (which calls every
        # tool's detect_output) never fabricates a "<file>.iso" candidate.
        if not os.path.isdir(input_path):
            return None
        candidate = self._service.get_output_path(input_path)
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
        mode: str,  # noqa: ARG002 - single-mode tool
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        return self._service.get_output_path(
            input_path, output_dir, treat_as_stem=treat_as_stem,
        )

    def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str,
        *,
        compression: str | None = None,  # noqa: ARG002 - no compression knob
        split: bool = False,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        return self._service.convert(
            input_path, output_path, mode,
            split=split, cancel_event=cancel_event,
        )

    async def verify(self, path: str) -> dict:
        return await self._service.verify(path)

    async def info(self, path: str) -> dict:
        return await run_in_threadpool(self._service.info, path)

    def info_model(self, raw: dict, path: str) -> Ps3IsoInfo:
        return Ps3IsoInfo(
            file=raw["file"],
            size=raw["size"],
            size_display=raw["size_display"],
            format=raw.get("format"),
            extension=raw["extension"],
            compressed=raw["compressed"],
            compression_type=raw.get("compression_type"),
            title=raw.get("title"),
            title_id=raw.get("title_id"),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
