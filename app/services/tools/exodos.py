"""ExodosTool, thin plugin wrapper delegating to ``exodos_service``.

Repacks an eXoDOS game zip (``.zip``) into a file RomM's in-browser DOS player
can start on its own (``.dosz``). See ``services/exodos.py`` for the layout and
why.

* **Content, not extension, decides the source.** ``.zip`` is also romz's
  archive extension, so ``converts_path`` reads the zip's directory: one game
  folder whose template is in ``EXODOS_DOS_DIR``. Cached per (path, size,
  mtime).
* **Gated like nsz.** No template tree, no tool: ``is_ready`` is false and the
  sidebar hides it.
* **Verifiable, never deletable.** The product differs from its Source by
  design, so verify proves it reads back, not that it holds the Source; and the
  Source is the operator's eXoDOS install. ``supports_verify`` without
  ``supports_delete_on_verify``, the makeps3iso and xiso precedent.
* **No subprocess.** Everything runs in Python, so there are no pids to report.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from fastapi.concurrency import run_in_threadpool
from models import BasicFileInfo, OutputStatus
from services.exodos import (
    EXODOS,
    INPUT_EXTENSIONS,
    OUTPUT_EXTENSION,
    exodos_service,
)
from services.lock_manager import lock_manager
from services.subprocess_runner import run_detached

from .base import BaseTool
from .spec import ModeKind, ModeSpec


class ExodosTool(BaseTool):
    id = "exodos"
    platform_slugs = frozenset({EXODOS.platform_slug})
    policy_owner = "exodos"
    display_name = "eXoDOS"
    modes = (
        ModeSpec(
            mode="exodos_repack",
            tool_id="exodos",
            # A new file of a different kind beside the Source, like xiso's
            # trim. Not COPY, whose output may share its source's path.
            kind=ModeKind.COMPRESS,
            label="Repack for RomM",
            group="exodos",
            output_ext=OUTPUT_EXTENSION,
            input_extensions=INPUT_EXTENSIONS,
            supports_verify=True,
            supports_delete_on_verify=False,
            # The repack reads the zip's own directory and the template tree
            # beside it; a zip inside another archive has neither.
            allows_archive_input=False,
            # Manual only: a Rule over RomM's dos folder would write a second
            # copy of every game beside it, and its verify step would hide the
            # conf warning nobody is there to read.
            automatable=False,
        ),
    )
    output_extensions = frozenset({OUTPUT_EXTENSION})
    verify_extensions = frozenset({OUTPUT_EXTENSION})

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = exodos_service

    async def is_ready(self) -> bool:
        # Detached, not pooled: the probe stats a directory that may sit on a
        # mount that has stopped answering (see ToolPlugin.is_ready).
        return bool(await run_detached(self._service.is_ready))

    def converts_path(self, path: str) -> bool:
        return self._service.is_repackable(path)

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        if from_archive or not self._service.is_repackable(input_path):
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
        split: bool = False,
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

    def info_model(self, raw: dict, path: str) -> BasicFileInfo:
        return BasicFileInfo(**self._basic_info_fields(raw))

    def active_pids(self) -> list[int]:
        return []
