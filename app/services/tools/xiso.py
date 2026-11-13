"""XisoTool, thin plugin wrapper delegating to ``extract_xiso_service``.

Trims an Xbox / Xbox 360 disc image (``.iso``) to an XISO (``.xiso.iso``).

Three choices here that differ from a plain suffix-matched compressor:

* **Content, not extension, decides the source.** Every Xbox image is an
  ``.iso``, and so is every PSP, PS2, GameCube and CD/DVD image the other tools
  already own. ``converts_path`` therefore probes for an XDVDFS volume at the
  partition offsets extract-xiso itself checks, so the Xbox tool is only offered
  on Xbox discs and never on an image this tool already trimmed. The probe is a
  few 20-byte reads, cached per (path, size, mtime).
* **The product is compound** (``.xiso.iso``, cf. NKit's ``.nkit.iso``), so its
  verify claim cannot hijack the plain ``.iso`` rows other tools own, and the
  output can never be named after its own source.
* **Verifiable, never deletable.** Verify walks the XISO's directory tables and
  has extract-xiso list it — enough to catch a truncated or corrupt product,
  but XDVDFS carries no checksums, so it is not a comparison with the source.
  Trimming also discards partitions that cannot be rebuilt. Deleting a
  Redump-verified source on that basis is not something to offer, so the mode
  sets ``supports_verify`` without ``supports_delete_on_verify`` (the
  makeps3iso precedent).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from fastapi.concurrency import run_in_threadpool
from models import OutputStatus, XisoInfo
from services.extract_xiso import (
    XISO_CONVERTIBLE_EXTENSIONS,
    XISO_OUTPUT_EXTENSION,
    XISO_VERIFY_EXTENSIONS,
    extract_xiso_service,
)
from services.lock_manager import lock_manager

from .base import BaseTool
from .spec import ModeKind, ModeSpec


class XisoTool(BaseTool):
    id = "xiso"
    # RomM's slugs for the two consoles this serves.
    platform_slugs = frozenset({"xbox", "xbox360"})
    policy_owner = "xiso"
    display_name = "Xbox"
    modes = (
        ModeSpec(
            mode="xiso_rewrite",
            tool_id="xiso",
            kind=ModeKind.COMPRESS,
            label="Trim to XISO",
            group="xiso",
            output_ext=XISO_OUTPUT_EXTENSION,
            input_extensions=frozenset(XISO_CONVERTIBLE_EXTENSIONS),
            supports_verify=True,
            supports_delete_on_verify=False,
            # Not from archives: the content probe needs the real file, and a
            # 7-8 GB disc image is not something to inflate into scratch space
            # just to discover it is a PSP game.
            allows_archive_input=False,
        ),
    )
    output_extensions = frozenset(XISO_VERIFY_EXTENSIONS)
    verify_extensions = frozenset(XISO_VERIFY_EXTENSIONS)

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = extract_xiso_service

    def converts_path(self, path: str) -> bool:
        return self._service.is_convertible(path)

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        if from_archive or not self._service.is_convertible(input_path):
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

    def info_model(self, raw: dict, path: str) -> XisoInfo:
        return XisoInfo(
            **self._basic_info_fields(raw),
            console=raw.get("console"),
            layout=raw.get("layout"),
            file_count=raw.get("file_count"),
            data_size=raw.get("data_size"),
            data_size_display=raw.get("data_size_display"),
            data_ratio=raw.get("data_ratio"),
            has_system_update=raw.get("has_system_update"),
            trimmed=raw.get("trimmed"),
            optimized=raw.get("optimized"),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
