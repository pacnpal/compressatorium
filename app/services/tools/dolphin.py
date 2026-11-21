"""DolphinTool, thin plugin wrapper delegating to ``dolphin_tool_service``."""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from config import settings
from models import DolphinDiscInfo, OutputStatus
from services.dolphin_tool import (
    DOLPHIN_CONVERTIBLE_EXTENSIONS,
    DOLPHIN_OUTPUT_FORMATS,
    dolphin_tool_service,
)
from services.disc_id import nintendo_disc_platform
from services.file_hasher import compute_file_sha1_sync
from services.lock_manager import lock_manager
from services.subprocess_runner import ReadCancelled, run_detached
from services.workload_limiter import workload_limiter

from .base import (
    BaseTool,
    ContentDigest,
    ContentDigestUnavailable,
    EmbeddedHashUnavailable,
)
from .binaries import RequiredBinary
from .spec import ModeKind, ModeSpec

# The digest kind every Dolphin mode produces and compares: SHA1 of the whole
# GameCube/Wii disc image, the same hash Redump indexes.
DISC_SHA1_KIND = "gc_wii_disc_sha1"

# The codecs dolphin-tool accepts in each container. From its
# Source/Core/DolphinTool/ConvertCommand.cpp: a WIA container refuses zstd and
# an RVZ container refuses purge ("Compression type is not supported for the
# container format"), and its `--compression` choices leave purge out entirely.
CONTAINER_CODECS = {
    "rvz": frozenset({"none", "zstd", "bzip2", "lzma", "lzma2"}),
    "wia": frozenset({"none", "bzip2", "lzma", "lzma2"}),
}
# Each container's default, as Dolphin's own convert dialog picks it
# (Source/Core/DolphinQt/ConvertDialog.cpp, OnFormatChanged): RVZ selects
# Zstandard ("recommended"); WIA selects nothing, so it stays on its first
# entry, No Compression.
CONTAINER_DEFAULTS = {"rvz": "zstd", "wia": "none"}
# Each codec's valid level range and default level, as (min, max, default);
# None for a codec that takes no level. The ranges are Dolphin's
# (Source/Core/DiscIO/WIABlob.cpp, GetAllowedCompressionLevels: bzip2, lzma
# and lzma2 1-9; zstd 1 to ZSTD_maxCLevel(), 22, in the GUI range), and
# DolphinTool/ConvertCommand.cpp refuses any other level ("Compression level
# not in acceptable range") and any codec but none without one. zstd keeps
# this app's 19 (it matches MAME's Redump sets); the others take Dolphin's own
# convert-dialog default, 5 (DolphinQt/ConvertDialog.cpp).
CODEC_LEVELS: dict[str, tuple[int, int, int] | None] = {
    "none": None,
    "zstd": (1, 22, 19),
    "bzip2": (1, 9, 5),
    "lzma": (1, 9, 5),
    "lzma2": (1, 9, 5),
}


# Each container's block size (`-b`), which dolphin-tool requires for GCZ, RVZ
# and WIA ("Block size must be set for GCZ/RVZ/WIA",
# DolphinTool/ConvertCommand.cpp), as (default, (minimum, multiple_from)) for
# ModeSpec.block_size / block_size_rule. The rules are DiscIO/DiscUtils.cpp's
# IsDiscImageBlockSizeValid: GCZ a power of two; WIA at least
# WIA_MIN_BLOCK_SIZE (2 MiB) and a multiple of it; RVZ at least
# RVZ_MIN_BLOCK_SIZE (32 KiB), a power of two below RVZ_BIG_BLOCK_SIZE_LCM
# (2 MiB) and a multiple of it above. The defaults are Dolphin's convert
# dialog's (DolphinQt/ConvertDialog.cpp): GCZ_RVZ_PREFERRED_BLOCK_SIZE (128 KiB)
# for GCZ and RVZ, and WIA's only offer, WIA_MIN_BLOCK_SIZE.
CONTAINER_BLOCK_SIZES: dict[str, tuple[int, tuple[int, int | None]]] = {
    "gcz": (0x20000, (1, None)),
    "rvz": (0x20000, (0x8000, 0x200000)),
    "wia": (0x200000, (0x200000, 0x200000)),
}


def _codec_levels(codecs: frozenset[str]) -> tuple[tuple[str, int, int, int], ...]:
    """``ModeSpec.codec_levels`` for *codecs*, from :data:`CODEC_LEVELS`."""
    return tuple(
        (codec, *CODEC_LEVELS[codec]) for codec in sorted(codecs) if CODEC_LEVELS[codec]
    )


def container_of(mode: str) -> str:
    """The container a Dolphin mode writes (``rvz``, ``wia``, ...)."""
    return DOLPHIN_OUTPUT_FORMATS[mode][0]


def _block_size(mode: str) -> tuple[int | None, tuple[int, int | None] | None]:
    """``(ModeSpec.block_size, block_size_rule)`` for *mode*; both None for a
    container that takes no block size (ISO)."""
    return CONTAINER_BLOCK_SIZES.get(container_of(mode), (None, None))


# mode -> (label, output_ext, kind, supports_compression, supports_level).
# A mode with a codec picker is RVZ or WIA, which dolphin-tool refuses without
# `-c`: those two require a codec (`ModeSpec.requires_codec`), take one at a
# time (`single_codec`), and take their container's codecs and default from
# the tables above. The other two take none, and say why.
_MODES = {
    "dolphin_rvz": ("Dolphin RVZ", ".rvz", ModeKind.COMPRESS, True, True),
    "dolphin_wia": ("Dolphin WIA", ".wia", ModeKind.COMPRESS, True, True),
    "dolphin_gcz": ("Dolphin GCZ", ".gcz", ModeKind.COMPRESS, False, False),
    "dolphin_iso": ("Dolphin ISO", ".iso", ModeKind.EXTRACT, False, False),
}
_NO_COMPRESSION_REASONS = {
    "dolphin_gcz": "GCZ uses fixed internal compression",
    "dolphin_iso": "Compression not applicable for ISO extraction",
}

# Ordered, de-duplicated output extensions; iteration order decides which
# sibling wins when several candidates exist.
_DOLPHIN_OUTPUT_EXTENSIONS = tuple(
    dict.fromkeys(ext for _, ext in DOLPHIN_OUTPUT_FORMATS.values()),
)


def _build_modes() -> list[ModeSpec]:
    return [
        ModeSpec(
            mode=mode,
            tool_id="dolphin",
            kind=kind,
            label=label,
            group="dolphin",
            output_ext=ext,
            input_extensions=frozenset(DOLPHIN_CONVERTIBLE_EXTENSIONS),
            supports_compression=supports_compression,
            supports_compression_level=supports_level,
            requires_codec=supports_compression,
            single_codec=supports_compression,
            no_compression_reason=_NO_COMPRESSION_REASONS.get(mode, ""),
            codecs=(
                CONTAINER_CODECS[container_of(mode)] if supports_compression
                else frozenset()
            ),
            default_compression=(
                CONTAINER_DEFAULTS[container_of(mode)] if supports_compression else None
            ),
            codec_levels=(
                _codec_levels(CONTAINER_CODECS[container_of(mode)])
                if supports_compression else ()
            ),
            block_size=_block_size(mode)[0],
            block_size_rule=_block_size(mode)[1],
            supports_delete_on_verify=True,
            # Every input and output extension has a disc-SHA1 content digest.
            supports_round_trip=True,
            allows_archive_input=True,
        )
        for mode, (label, ext, kind, supports_compression, supports_level)
        in _MODES.items()
    ]


class DolphinTool(BaseTool):
    id = "dolphin"
    # Nintendo optical discs. RVZ/WIA/GCZ are Dolphin's own formats.
    platform_slugs = frozenset({"ngc", "gamecube", "wii"})
    # dolphin-tool's own RVZ/WIA default, and what the manual picker seeds.
    default_compression = "zstd"
    policy_owner = "dolphin_tool"
    display_name = "Dolphin"
    modes = _build_modes()
    output_extensions = frozenset({".rvz", ".wia", ".gcz", ".iso"})
    verify_extensions = frozenset(DOLPHIN_CONVERTIBLE_EXTENSIONS)
    # Each of these is one GameCube / Wii disc, so an .m3u can list them.
    disc_extensions = frozenset({".iso", ".gcz", ".wia", ".rvz", ".wbfs"})
    # The compressed/container formats (.rvz/.wia/.gcz/.wbfs) are recompressed
    # by dolphin-tool, so their on-disk bytes never appear in a DAT — only the
    # reconstructed disc SHA1 does. A disc-hash miss is therefore definitive;
    # don't fall back to a (meaningless) file-level hash of the container.
    embedded_hash_is_exhaustive = True
    # The rebuilt disc SHA1 is the raw disc image's SHA1, so a container's
    # duplicate-finder key matches the .iso it was made from (#388).
    raw_image_hash_labels = frozenset({"dolphin_disc_sha1"})
    # `dolphin-tool verify` rebuilds the whole disc to hash it (#389).
    raw_image_hash_rebuilds_content = True

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = dolphin_tool_service

    @property
    def required_binaries(self) -> tuple[RequiredBinary, ...]:
        # The program every conversion runs, read where the service spawns it
        # (#494): missing or not executable, and the tool isn't ready.
        return (RequiredBinary(
            "dolphin-tool", self._service.dolphin_tool_path, "DOLPHIN_TOOL_PATH",
        ),)

    def header_platform(self, path: str, *, within=None) -> str | None:
        # The magic numbers in a raw disc header. Dolphin's own containers
        # (RVZ/WIA/GCZ) hold that header compressed, so they answer None.
        return nintendo_disc_platform(path)

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        source = Path(input_path)
        source_ext = source.suffix.lower()
        if source_ext not in self.input_extensions:
            return None

        candidate_paths: list[str] = []
        if source_ext in _DOLPHIN_OUTPUT_EXTENSIONS and source_ext != ".iso":
            candidate_paths.append(str(source))
        for output_ext in _DOLPHIN_OUTPUT_EXTENSIONS:
            if output_ext == source_ext:
                continue
            candidate_paths.append(str(source.with_suffix(output_ext)))

        converting_path: str | None = None
        for candidate_path in candidate_paths:
            file_exists, is_converting = lock_manager.check_file_status(candidate_path)
            if file_exists:
                return OutputStatus(
                    tool_id=self.id,
                    exists=True,
                    ready=not is_converting,
                    path=candidate_path,
                )
            if is_converting and converting_path is None:
                converting_path = candidate_path

        if converting_path:
            return OutputStatus(
                tool_id=self.id, exists=False, ready=False, path=converting_path,
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
        split: bool = False,  # noqa: ARG002 - split applies only to makeps3iso
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        # A codec the container refuses (a Rule saved before the routes
        # checked) fails here, with the reason, before dolphin-tool runs.
        self.refuse_unsupported_compression(mode, compression)
        # The container's block size comes from the mode, so every job, chain
        # step and CLI run (all through run_convert) sends the same `-b`.
        return self._service.convert(
            input_path, output_path, mode,
            compression=compression, block_size=self.spec(mode).block_size,
            cancel_event=cancel_event,
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
        return await self._service.header(path)

    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]:
        # A plain .iso is already the raw redump image, its file-level SHA1
        # *is* the disc hash, so skip the expensive verify pass and let the
        # caller fall back to cheap file hashing. The compressed/container
        # formats (.rvz/.wia/.gcz/.wbfs) must be reconstructed by dolphin-tool
        # before hashing, so only those go through verify.
        if Path(path).suffix.lower() == ".iso":
            return []
        # Respect the operator-configured match size cap: dolphin-tool verify
        # reconstructs and hashes the entire disc, which is exactly the
        # expensive work MATCH_MAX_FILE_SIZE exists to bound. Over-cap files
        # report no hash so the caller's size-cap handling (file-level path)
        # short-circuits instead of paying for a full verify. CHD's hook reads
        # cached hashes and is intentionally not capped.
        size_cap = max(0, int(getattr(settings, "match_max_file_size", 0) or 0))
        if size_cap > 0:
            try:
                size_bytes = await run_in_threadpool(os.path.getsize, path)
            except OSError as e:
                # Couldn't stat the file: for an exhaustive tool this is an
                # inability to derive the disc hash, not "no embedded hash".
                # Surface it as non-cacheable so the caller doesn't fall back
                # to a (meaningless) container SHA1 and cache a false negative.
                raise EmbeddedHashUnavailable(
                    f"could not stat {path}: {e}",
                ) from e
            if size_bytes > size_cap:
                # Over the cap: report no embedded hash and let the caller's
                # own size-cap handling return a non-cacheable "file too large"
                # rather than paying for a full-disc verify.
                return []
        # Gate the verify under the "match" workload lane so MAX_MATCH_CONCURRENCY
        # bounds how many full-disc reconstructions run at once, even across a
        # metadata scan and concurrent /dat/match requests.
        try:
            async with await workload_limiter.acquire("match", cancel_event=cancel_event):
                hashes = await self._service.disc_hashes(
                    path, cancel_event=cancel_event,
                )
        except Exception as e:
            # Spawn failure (e.g. binary missing) or unexpected error: treat as
            # a transient inability to derive the hash, not "no hash exists".
            raise EmbeddedHashUnavailable(
                f"dolphin-tool verify failed for {path}: {e}",
            ) from e
        if not hashes:
            # A compressed Dolphin container must yield a disc hash; an empty
            # result means verify failed / produced nothing. Signal a
            # non-cacheable miss rather than collapsing to the container's
            # (meaningless-against-Redump) file SHA1 and caching it.
            raise EmbeddedHashUnavailable(
                f"dolphin-tool verify produced no hash for {path}",
            )
        return [(h, "dolphin_disc_sha1") for h in hashes]

    async def content_digest(
        self,
        path: str,
        *,
        cancel_event: asyncio.Event | None = None,
        within: Sequence[str] | None = None,  # reads no sheet, so unused
    ) -> ContentDigest | None:
        # Both sides of every Dolphin mode reduce to the SHA1 of the whole disc
        # image. A plain .iso *is* that image, so it is hashed directly, with no
        # subprocess; the containers are rebuilt and hashed by
        # `dolphin-tool verify --algorithm sha1`.
        ext = Path(path).suffix.lower()
        if ext not in self.verify_extensions:
            return None
        if ext == ".iso":
            try:
                value = await run_detached(
                    compute_file_sha1_sync, path, cancel_event=cancel_event,
                )
            except (OSError, ReadCancelled) as e:
                raise ContentDigestUnavailable(f"could not hash {path}: {e}") from e
        else:
            value = await self._service.disc_sha1(path, cancel_event=cancel_event)
            if value is None:
                raise ContentDigestUnavailable(
                    f"dolphin-tool verify gave no disc SHA1 for {path}",
                )
        return ContentDigest(kind=DISC_SHA1_KIND, value=str(value).lower())

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
