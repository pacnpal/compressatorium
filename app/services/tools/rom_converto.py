"""RomConvertoTool, thin plugin wrapper delegating to ``rom_converto_service``.

One tool that owns several unrelated modes, because rom-converto is one binary
that converts several unrelated formats (#420): Xbox 360 ISO -> ZAR, PSP PBP
-> ISO, and 3DS CIA -> CCI and CCI -> CIA. Each mode names its own Platform
(``ModeSpec.platform_slugs``), so a mode is only offered on its own system;
the tool's ``platform_slugs`` is their union.

* **Content, not extension, decides the source.** Every Xbox 360 image is an
  ``.iso``, so ``converts_path`` probes for an Xbox 360 XDVDFS volume; a
  ``.pbp`` must hold a PSP game (not a PS1 Classic), a ``.cia`` a CIA header
  and a ``.cci``/``.3ds`` an NCSD header. Each probe is cached per path, size
  and mtime.
* **ZAR: verifiable, never deletable.** ``xenon verify`` decodes every block
  and checks its hash, but a ZAR holds the disc's files, not the disc, and
  nothing turns it back into an ISO. The mode says so through
  ``ModeSpec.source_kept_reason``.
* **PBP -> ISO and CIA <-> CCI: deletable on the Recognized proof (#423).**
  Their outputs can't be Round-tripped either (a converted CIA is unsigned
  with a zero title key), so ``ModeSpec.delete_proof`` is ``RECOGNIZED``: the
  Source goes only when the output's SHA-1 is in a DAT or Hasheous. Their
  container check reads the output's structure in Python. Only the ZAR's
  check is a Verify row action in the listing (``verifies_path``); the others
  run inside a job, and a later check goes back to this tool by the mode the
  stored check names.
* **Not an Auto default.** Auto keeps Xbox 360 on the XISO trim, PSP on CSO
  and 3DS on Z3DS; these modes are picked by hand or by a Rule.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from models import OutputStatus, RomConvertoInfo
from services.lock_manager import lock_manager
from services.rom_converto import (
    CCI_INPUT_EXTENSIONS,
    CCI_TO_CIA_MODE,
    CIA_INPUT_EXTENSIONS,
    CIA_TO_CCI_MODE,
    PBP_INPUT_EXTENSIONS,
    PBP_MODE,
    VERIFY_EXTENSIONS,
    ZAR_INPUT_EXTENSIONS,
    ZAR_MODE,
    ZAR_OUTPUT_EXTENSION,
    ZAR_SOURCE_KEPT_REASON,
    ZAR_VERIFY_EXTENSIONS,
    RomConvertoError,
    rom_converto_service,
)
from utils.path_utils import match_extension

from .base import BaseTool
from .spec import DeleteProof, ModeKind, ModeSpec

_3DS_SLUGS = frozenset({"3ds", "n3ds", "new-nintendo-3ds"})


def _recognized_mode(
    mode: str, label: str, group: str, output_ext: str,
    inputs: frozenset[str], slugs: frozenset[str],
) -> ModeSpec:
    """A PSP or 3DS mode (#423): deletable only on the Recognized proof."""
    return ModeSpec(
        mode=mode,
        tool_id="rom_converto",
        kind=ModeKind.COMPRESS,
        label=label,
        group=group,
        output_ext=output_ext,
        input_extensions=inputs,
        supports_delete_on_verify=True,
        delete_proof=DeleteProof.RECOGNIZED,
        # Not from archives: the content probe needs the real file.
        allows_archive_input=False,
        platform_slugs=slugs,
        auto_target=False,
    )


class RomConvertoTool(BaseTool):
    id = "rom_converto"
    display_name = "rom-converto"
    # The union of every mode's Platform; each mode names its own below.
    platform_slugs = frozenset({"xbox360", "psp"}) | _3DS_SLUGS
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
        _recognized_mode(
            PBP_MODE, "PSP PBP → ISO", "psp", ".iso", PBP_INPUT_EXTENSIONS,
            frozenset({"psp"}),
        ),
        _recognized_mode(
            CIA_TO_CCI_MODE, "3DS CIA → CCI", "ctr", ".cci", CIA_INPUT_EXTENSIONS,
            _3DS_SLUGS,
        ),
        _recognized_mode(
            CCI_TO_CIA_MODE, "3DS CCI → CIA", "ctr", ".cia", CCI_INPUT_EXTENSIONS,
            _3DS_SLUGS,
        ),
    )
    output_extensions = frozenset(ZAR_VERIFY_EXTENSIONS | {".iso", ".cci", ".cia"})
    verify_extensions = frozenset(VERIFY_EXTENSIONS)
    # `xenon info` describes a ZAR only: the sources, and the ISO/CCI/CIA it
    # writes, are plain images the other tools describe. A file rom-converto
    # refuses is a 422.
    info_extensions = ZAR_VERIFY_EXTENSIONS
    info_file_errors = (RomConvertoError,)

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
        return self._service.is_source(path)

    def verifies_path(self, path: str) -> bool:
        # The listing's Verify row action: a ZAR only. An ISO, CIA or CCI is
        # checked inside the job that wrote it; offering this tool's structure
        # check on every PS2 ISO and 3DS ROM in a listing would be noise.
        return match_extension(path, ZAR_VERIFY_EXTENSIONS) is not None

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        if from_archive:
            return None
        if (
            match_extension(input_path, ZAR_INPUT_EXTENSIONS) is not None
            and not self._service.is_zar_source(input_path)
        ):
            # A PS2 or PSP `.iso` is not this tool's to badge.
            return None
        candidate = self._service.detect_output_path(input_path)
        if candidate is None:
            return None
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
