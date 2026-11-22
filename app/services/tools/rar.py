"""RarTool: a multi-volume RAR set to the one file inside it (#476).

A thin plugin over ``rar_service``. Its own tool rather than a romz mode:
readiness is per tool, so a missing ``unar`` hides this tool alone and never
the handheld 7z one.

A set is one Source of several files. Only its first volume converts
(:meth:`converts_path`); every later volume is a source companion
(:meth:`source_companions`), so listing, the locks, the delete plan and
deleting the Source all take the whole set.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from models import RarInfo
from services.archive_members import RarVolumesUnknown, later_volume_of, rar_set_volumes
from services.rar_extract import (
    RAR_EXTRACT_MODE,
    RAR_INPUT_EXTENSIONS,
    RarSetRefused,
    plan_set,
    rar_service,
)
from services.subprocess_runner import ReadCancelled

from .base import BaseTool, CompanionsUnknown, ContentDigest, ContentDigestUnavailable
from .binaries import RequiredBinary
from .spec import ModeKind, ModeSpec

# Size and CRC32 of the one file: read from the set's headers for the Source,
# hashed for the extracted output.
FILE_CRC32_KIND = "file_size_crc32"


class RarTool(BaseTool):
    id = "rar"
    display_name = "RAR sets"
    # File info reads the set's headers in Python; unar isn't run.
    info_needs_tool = False
    modes = (
        ModeSpec(
            mode=RAR_EXTRACT_MODE,
            tool_id="rar",
            kind=ModeKind.EXTRACT,
            label="Extract RAR set → file",
            group="rar",
            output_ext=None,  # the one file's own name
            input_extensions=RAR_INPUT_EXTENSIONS,
            # The conversion compares the bytes it wrote with the CRC32 the
            # set records before keeping them, and the Round-trip compares
            # them again before the Source goes.
            supports_delete_on_verify=True,
            supports_round_trip=True,
            # The file has no container: its own check (read in full) says
            # nothing about its content, so a delete never skips the Round-trip.
            round_trip_required=True,
            # The set is the input: its volumes are files on disk, never
            # members of another archive.
            allows_archive_input=False,
            # A download to unpack, not a Platform's format: never Auto's pick.
            auto_target=False,
        ),
    )
    # The output can be any file at all, so no extension marks it as this
    # tool's, and there is no Verify row action: a set is checked by
    # extracting it, and the extracted file by the job that wrote it.
    output_extensions = frozenset()
    verify_extensions = frozenset()

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = rar_service

    @property
    def required_binaries(self) -> tuple[RequiredBinary, ...]:
        # The extractor every conversion runs (#494).
        return (RequiredBinary("unar", self._service.unar_path, "UNAR_PATH"),)

    @property
    def verify_required_binaries(self) -> tuple[RequiredBinary, ...]:
        # The check reads the extracted file in Python; unar isn't run.
        return ()

    def converts_path(self, path: str) -> bool:
        # The first volume, and only a set that plans: the route then names
        # what it found through `convert_refusal`.
        return self.convert_refusal(path) is None

    def convert_refusal(self, path: str) -> str | None:
        if not super().converts_path(path):
            return f"{Path(path).name} is not a RAR set's first volume (.rar)"
        try:
            plan_set(path)
        except RarSetRefused as exc:
            return str(exc)
        return None

    def source_companions(self, path: str) -> list[str]:
        # The later volumes of the set the path leads, as its own headers
        # read them: a file merely named like a volume (a stray part4, an
        # unrelated .r00 beside a single-volume .rar) is never a companion,
        # so no lock, delete plan or delete-source takes it. Names never
        # decide the other way either: a set whose part2 / .r00 is missing
        # names no later volume, yet its part1 says more follow. So every
        # .rar that is not a later volume has its headers read (bounded,
        # cached against the set's files); a lone .rar reads its own once.
        # Headers that can't be read whole or in time raise CompanionsUnknown:
        # a listing then shows none, and anything that deletes refuses.
        if Path(path).suffix.lower() not in RAR_INPUT_EXTENSIONS:
            return []
        if later_volume_of(path) is not None:
            return []
        try:
            return rar_set_volumes(path)[1:]
        except RarVolumesUnknown as exc:
            raise CompanionsUnknown(*exc.args) from exc

    def output_path(
        self,
        mode: str,
        input_path: str,
        output_dir: str | None = None,
        *,
        treat_as_stem: bool = False,
    ) -> str:
        return self._service.get_output_path(input_path, output_dir)

    def expected_output_size(self, input_path: str, mode: str) -> int | None:
        return self._service.expected_output_size(input_path)

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
        # A fixed extraction: no compression, no split.
        return self._service.convert(
            input_path, output_path, mode, cancel_event=cancel_event,
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

    def info_model(self, raw: dict, path: str) -> RarInfo:
        return RarInfo(
            **self._basic_info_fields(raw),
            contained_name=raw.get("contained_name"),
            original_size=raw.get("original_size"),
            volumes=raw.get("volumes") or 1,
        )

    async def content_digest(
        self,
        path: str,
        *,
        cancel_event: asyncio.Event | None = None,
        within: Sequence[str] | None = None,  # reads no sheet, so unused
    ) -> ContentDigest | None:
        try:
            found = await self._service.payload_digest(path, cancel_event=cancel_event)
        except ReadCancelled:
            raise
        except Exception as exc:  # a set that no longer plans, an unreadable file
            raise ContentDigestUnavailable(
                f"could not read {Path(path).name}: {exc}",
            ) from exc
        if found is None:
            raise ContentDigestUnavailable(
                f"{Path(path).name} records no CRC32 for its file to compare with",
            )
        size, crc = found
        return ContentDigest(kind=FILE_CRC32_KIND, value=f"{size}:{crc:08x}")

    def active_pids(self) -> list[int]:
        return self._service.active_pids()
