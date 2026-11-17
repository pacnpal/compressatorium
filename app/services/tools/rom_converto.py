"""RomConvertoTool, thin plugin wrapper delegating to ``rom_converto_service``.

One tool that will own several unrelated modes, because rom-converto is one
binary that converts several unrelated formats (#420): Xbox 360 ISO -> ZAR
now, PSP PBP -> ISO and 3DS CIA <-> CCI later. Each mode names its own
Platform (``ModeSpec.platform_slugs``), so a mode is only offered on its own
system; the tool's ``platform_slugs`` is their union.

Xbox 360 ISO -> ZAR, the first mode:

* **Content, not extension, decides the source**, as for the xiso tool: every
  Xbox 360 image is an ``.iso``, so ``converts_path`` probes for an Xbox 360
  XDVDFS volume (cached per path, size and mtime). An original Xbox image is
  not offered: ZAR is what Xenia mounts.
* **Verifiable, never deletable.** ``xenon verify`` decodes every block and
  checks its hash, but a ZAR holds the disc's files, not the disc, and
  nothing turns it back into an ISO, so there is no Round-trip against the
  Source. The mode says so through ``ModeSpec.source_kept_reason``.
* **Not an Auto default.** Auto keeps Xbox 360 on the XISO trim; ZAR is picked
  by hand or by a Rule.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from models import OutputStatus, RomConvertoInfo
from services.lock_manager import lock_manager
from services.rom_converto import (
    ZAR_INPUT_EXTENSIONS,
    ZAR_MODE,
    ZAR_OUTPUT_EXTENSION,
    ZAR_SOURCE_KEPT_REASON,
    ZAR_VERIFY_EXTENSIONS,
    rom_converto_service,
)

from .base import BaseTool
from .spec import ModeKind, ModeSpec


class RomConvertoTool(BaseTool):
    id = "rom_converto"
    display_name = "rom-converto"
    policy_owner = "rom_converto"
    # The union of every mode's Platform; each mode names its own below.
    platform_slugs = frozenset({"xbox360"})
    modes = (
        ModeSpec(
            mode=ZAR_MODE,
            tool_id="rom_converto",
            kind=ModeKind.COMPRESS,
            label="Xbox 360 ISO → ZAR",
            group="xenon",
            output_ext=ZAR_OUTPUT_EXTENSION,
            input_extensions=ZAR_INPUT_EXTENSIONS,
            supports_verify=True,
            supports_delete_on_verify=False,
            source_kept_reason=ZAR_SOURCE_KEPT_REASON,
            # Not from archives: the content probe needs the real file, and a
            # 7-8 GB disc image is not something to inflate into scratch space
            # just to find out it is a PS2 game.
            allows_archive_input=False,
            platform_slugs=frozenset({"xbox360"}),
            auto_target=False,
        ),
    )
    output_extensions = frozenset(ZAR_VERIFY_EXTENSIONS)
    verify_extensions = frozenset(ZAR_VERIFY_EXTENSIONS)

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = rom_converto_service
        self._not_ready_reason = super().not_ready_reason

    @property
    def not_ready_reason(self) -> str:
        """The last readiness answer's reason: missing, not executable, or broken."""
        return self._not_ready_reason

    async def is_ready(self) -> bool:
        # A stat (detached, inside the service) and, once per binary, a
        # `--version` run: a binary that is there but won't run is not ready.
        ready, reason = await self._service.readiness()
        if not ready:
            self._not_ready_reason = reason
        return ready

    def converts_path(self, path: str) -> bool:
        return self._service.is_zar_source(path)

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        if from_archive or not self._service.is_zar_source(input_path):
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
        return await self._service.info(path)

    def info_model(self, raw: dict, path: str) -> RomConvertoInfo:
        return RomConvertoInfo(
            **self._basic_info_fields(raw),
            file_count=raw.get("file_count"),
            directory_count=raw.get("directory_count"),
            logical_size=raw.get("logical_size"),
            logical_size_display=raw.get("logical_size_display"),
            blocks=raw.get("blocks"),
            boot_executable=raw.get("boot_executable"),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
