"""ExodosTool, thin plugin wrapper delegating to ``exodos_service``.

Repacks an eXoDOS game zip (``.zip``) into a file RomM's in-browser DOS player
can start on its own (``.dosz``). See ``services/exodos.py`` for the layout and
why.

* **Content, not extension, decides the source.** ``.zip`` is also romz's
  archive extension, so ``converts_path`` reads the zip's directory: one game
  folder that has a ``dosbox.conf`` template (found beside the zip or above
  it) or holds a DOS program. Cached per (path, size, mtime).
* **Always ready.** Nothing to install and nothing to set (#513): the
  template tree is found per zip, and ``EXODOS_DOS_DIR`` is only an override.
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
from models import BasicFileInfo
from services.exodos import (
    EXODOS,
    INPUT_EXTENSIONS,
    OUTPUT_EXTENSION,
    exodos_service,
)

from .base import BaseTool
from .spec import ModeKind, ModeSpec


class ExodosTool(BaseTool):
    id = "exodos"
    platform_slugs = frozenset({EXODOS.platform_slug})
    display_name = "eXoDOS"
    # File info is the size and the extension's format; nothing is run.
    info_needs_tool = False
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
            # Nor an Auto target: a `dos` folder's zips are left alone by Auto
            # and `compressatorium convert --auto`, with a reason (#294).
            auto_target=False,
        ),
    )
    output_extensions = frozenset({OUTPUT_EXTENSION})
    verify_extensions = frozenset({OUTPUT_EXTENSION})

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = exodos_service

    async def is_ready(self) -> bool:
        # Pure Python with no binary, and no setting to wait for: a game with
        # no template found is still repacked, and says so (#513).
        return True

    async def verify_is_ready(self) -> bool:
        # Checking a .dosz reads only the .dosz (CRCs, names, the root conf).
        return True

    def converts_path(self, path: str) -> bool:
        return self._service.is_repackable(path)

    # No detect_output override: a .zip is listed as an archive row, and the
    # listing never asks a tool for an archive row's outputs, so an "output
    # exists" badge here could not be shown. BaseTool's None stands.

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
