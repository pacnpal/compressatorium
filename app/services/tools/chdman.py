"""ChdmanTool, thin plugin wrapper delegating to ``chdman_service``.

Phase 0 moves no logic: every call forwards to the existing singleton. The
``ModeSpec`` rows formalize the mode metadata that is currently scattered as
prefix checks across the codebase.
"""
from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncGenerator, Sequence
from functools import partial
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from logging_setup import get_logger
from models import CHDInfo, OutputStatus
from services.chdman import CHDMAN_CONVERTIBLE_EXTENSIONS, chdman_service
from services.disc_id import (
    DiscIdStorageAbandoned,
    clear_embedded_disc_id,
    embed_in_chd,
    disc_platform,
    extract_from_source,
    read_embedded_game_id,
)
from services.disk import InsufficientDiskSpace, headroom_scratch_dir
from services.file_hasher import compute_files_sha1_sync
from services.lock_manager import lock_manager
from services.subprocess_runner import ReadCancelled, run_detached
from utils.delete_plan import sheet_track_paths
from utils.disc_sheet import layout_fingerprint
from utils.sheet_text import sheet_track_list

from .base import BaseTool, ContentDigest, ContentDigestUnavailable
from .spec import ModeKind, ModeSpec

logger = get_logger()

# Only CD/DVD images carry a readable disc serial: they hold an ISO 9660
# filesystem with SYSTEM.CNF (PS1/PS2) or PSP_GAME/PARAM.SFO (PSP), or a
# Dreamcast IP.BIN. createraw/createhd (hard-disk images) and createld
# (laserdisc) have no such structure, so disc-ID tagging is scoped to these two.
_DISC_ID_MODES = frozenset({"createcd", "createdvd"})

# chdman's media commands are not interchangeable: `createcd` writes a CD
# track layout and `createdvd` a flat DVD image, so choosing by tool alone --
# which is what a mode with no slugs of its own does -- offers a PS2 catalog
# every one of them, with `createcd` (the first CREATE mode) as the default an
# ISO submission lands on. Splitting the tool's platforms across its modes is
# what makes "narrowed to this platform" mean the right *command*, not just the
# right tool.
#
# Arcade appears in all three disc-shaped sets on purpose: MAME ships CD
# (Naomi/GD-ROM), hard-disk, and raw CHDs alike, and a wrong exclusion is worse
# than a missing one.
_CD_PLATFORMS = frozenset({
    "ps", "psx", "dc", "segacd", "sega-cd", "saturn", "segasaturn", "3do",
    "pcfx", "pc-fx", "neogeocd", "neo-geo-cd", "turbografx-cd",
    "philips-cd-i", "arcade", "mame",
})
_DVD_PLATFORMS = frozenset({"ps2", "psp"})
# Hard-disk and raw CHDs: MAME's own, and nothing a console library holds.
_HD_PLATFORMS = frozenset({"arcade", "mame"})
_LD_PLATFORMS = frozenset({"laseractive"})

# mode -> (label, the platforms that mode's media command actually serves,
#          what the command can actually read, whether Auto may default to it).
#
# Manual selection still offers every create mode on all four source
# extensions (``input_extensions``); the read set is what Auto checks. chdman
# ``createcd`` parses a track sheet (``.cue``/``.gdi``) or a single-track
# ``.iso``; a bare ``.bin`` has no table of contents, so the ``.cue`` beside it
# is the Image. ``createdvd`` copies a flat 2048-byte image, so only ``.iso``.
# The hard-disk, raw and laserdisc commands are never a default: they claim
# the arcade Platforms, but a CD image in an ``arcade`` folder is a CD.
#
# The last column is the Platforms a mode is an Auto target for without being
# offered for them by hand. Some PS2 titles shipped on CD-ROM and are kept as
# cue/bin; only createcd reads those. Claiming ``ps2`` in ``platform_slugs``
# instead would make createcd, the first create mode, the default of every
# platform-narrowed PS2 panel and rule. Auto still sends a PS2 ``.iso`` to
# createdvd, the smaller reader. PSP (UMD), PS3 and the Nintendo and Xbox discs
# are never CDs chdman reads, so ``ps2`` is the only one.
_CD_READS = frozenset({".cue", ".gdi", ".iso"})
# Track sheets: the probe reads the tracks they name (``header_reads``).
_SHEET_EXTS = frozenset({".cue", ".gdi"})
_DVD_READS = frozenset({".iso"})
_NONE: frozenset[str] = frozenset()
_CREATE_MODES = {
    "createraw": ("Create Raw", _HD_PLATFORMS, _NONE, False, _NONE),
    "createhd": ("Create HD", _HD_PLATFORMS, _NONE, False, _NONE),
    "createcd": ("Create CD", _CD_PLATFORMS, _CD_READS, True, frozenset({"ps2"})),
    "createdvd": ("Create DVD", _DVD_PLATFORMS, _DVD_READS, True, _NONE),
    "createld": ("Create LD", _LD_PLATFORMS, _NONE, False, _NONE),
}
# mode -> (label, primary output ext, companion exts). extractcd is the only
# multi-file extract: chdman writes the .cue track sheet plus a sibling .bin
# data track, so its companion set is (".bin",). The rest produce one file.
_EXTRACT_MODES = {
    "extractraw": ("Extract Raw", ".raw", (), _HD_PLATFORMS),
    "extracthd": ("Extract HD", ".raw", (), _HD_PLATFORMS),
    "extractcd": ("Extract CD", ".cue", (".bin",), _CD_PLATFORMS),
    "extractdvd": ("Extract DVD", ".iso", (), _DVD_PLATFORMS),
    "extractld": ("Extract LD", ".avi", (), _LD_PLATFORMS),
}
_CHD = frozenset({".chd"})

# The Round-trip check (#300) compares the image a CHD decodes to with its
# Source image. One digest kind covers every shape: SHA1 of the image's bytes as
# files on disk, track files laid end to end in sheet order. A plain .iso/.bin
# is its own one file; a DVD/HD CHD's data SHA1 is that hash of its image; a CD
# CHD is extracted and its track files hashed the same way as a Source cue.
IMAGE_DATA_SHA1_KIND = "image_data_sha1"
# The modes whose Source and output both have that digest.
_ROUND_TRIP_MODES = frozenset({"createcd", "createdvd"})
_SHEET_EXTS = frozenset({".cue", ".gdi"})
_RAW_IMAGE_EXTS = frozenset({".iso", ".bin"})
# CHD metadata tags (MAME src/lib/util/chd.h), read strictly from the
# `Metadata: Tag='XXXX'` lines `chdman info` prints. DVD and hard-disk CHDs
# store the image bytes as-is, so their data SHA1 is the image's SHA1. CD CHDs
# store 2448-byte frames padded per track, so theirs never is.
_IMAGE_TAGS = frozenset({"DVD ", "GDDD"})
_CD_TAGS = frozenset({"CHCD", "CHTR", "CHT2"})
_GDROM_TAGS = frozenset({"CHGT", "CHGD"})
_TAG_RE = re.compile(r"^\s*Metadata:\s*Tag='(.{4})'", re.MULTILINE)
_SHA1_RE = re.compile(r"[0-9a-fA-F]{40}")


def _build_modes() -> list[ModeSpec]:
    modes: list[ModeSpec] = [
        ModeSpec(
            mode=mode,
            tool_id="chdman",
            kind=ModeKind.CREATE,
            label=label,
            group="create",
            output_ext=".chd",
            input_extensions=frozenset(CHDMAN_CONVERTIBLE_EXTENSIONS),
            supports_compression=True,
            supports_delete_on_verify=True,
            supports_round_trip=mode in _ROUND_TRIP_MODES,
            allows_archive_input=True,
            platform_slugs=slugs,
            reads_extensions=reads,
            auto_target=auto_target,
            auto_platform_slugs=auto_slugs,
        )
        for mode, (label, slugs, reads, auto_target, auto_slugs) in _CREATE_MODES.items()
    ]
    modes += [
        ModeSpec(
            mode=mode,
            tool_id="chdman",
            kind=ModeKind.EXTRACT,
            label=label,
            group="extract",
            output_ext=ext,
            input_extensions=_CHD,
            # A .chd packed in an archive can be pulled out and decompressed
            # back to its disc image — the job pipeline extracts the member to a
            # temp file before chdman runs, same as the create direction. (copy
            # stays off below: recompressing a .chd straight out of an archive
            # is a pointless round trip.)
            allows_archive_input=True,
            companion_exts=companions,
            platform_slugs=slugs,
        )
        for mode, (label, ext, companions, slugs) in _EXTRACT_MODES.items()
    ]
    modes.append(
        ModeSpec(
            mode="copy",
            tool_id="chdman",
            kind=ModeKind.COPY,
            label="Copy / Recompress",
            group="copy",
            output_ext=".chd",
            input_extensions=_CHD,
            supports_compression=True,
            supports_delete_on_verify=True,
        )
    )
    return modes


class ChdmanTool(BaseTool):
    id = "chdman"
    # CD/DVD-based systems whose images CHD is the standard container for.
    # Deliberately excludes GameCube/Wii: those are .iso too, but RVZ is the
    # format for them, and that is exactly the ambiguity the platform resolves.
    # Derived as the union of what the modes claim, so the tool-level answer
    # cannot drift from the per-mode ones. ``copy`` declares nothing and
    # inherits this: recompressing a finished .chd is media-agnostic.
    platform_slugs = (
        _CD_PLATFORMS | _DVD_PLATFORMS | _HD_PLATFORMS | _LD_PLATFORMS
    )
    default_compression = "zlib"
    policy_owner = "chdman"
    display_name = "CHDMAN"
    modes = _build_modes()
    # All extensions chdman produces: .chd from create/copy, plus the extract
    # targets (.cue/.iso/.raw/.avi) and the .bin data-track sidecar that
    # extractcd writes alongside the .cue. The .bin is what Redump-style DATs
    # index (the tiny .cue isn't), so it must be discoverable by the scan.
    # verify only applies to finished CHDs.
    output_extensions = frozenset({".chd", ".cue", ".bin", ".iso", ".raw", ".avi"})
    verify_extensions = frozenset({".chd"})

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = chdman_service

    def header_platform(self, path: str) -> str | None:
        # PS1/PS2/PSP/Dreamcast through the disc-ID parser, Saturn and Sega CD
        # through their sector-0 system ids.
        return disc_platform(path)

    def header_reads(self, path: str) -> list[str] | None:
        # A .cue/.gdi is answered from the tracks it names. None (unknown
        # dependencies, so the answer is not cached) when the sheet is
        # unreadable or any track is refused (sheet_track_list keeps that None
        # apart from "no tracks"): a refused FIFO track later replaced by a
        # real one must be detected afresh.
        if Path(path).suffix.lower() not in _SHEET_EXTS:
            return []
        tracks = sheet_track_list(path)
        return None if tracks is None else [str(track) for track in tracks]

    # chdman's extract/copy modes take .chd input, but a .chd is not a
    # "convertible-from" source in the file listing, only the create sources
    # are. Override the derived union so convertible_extensions() matches
    # CHDMAN_CONVERTIBLE_EXTENSIONS.
    @property
    def input_extensions(self) -> frozenset[str]:
        return frozenset(CHDMAN_CONVERTIBLE_EXTENSIONS)

    def detect_output(
        self, input_path: str, *, from_archive: bool = False,
    ) -> OutputStatus | None:
        if Path(input_path).suffix.lower() not in self.input_extensions:
            return None
        candidate = str(Path(input_path).with_suffix(".chd"))
        file_exists, is_locked = lock_manager.check_file_status(candidate)
        if not (file_exists or is_locked):
            return None
        return OutputStatus(
            tool_id=self.id,
            exists=file_exists,
            ready=file_exists and not is_locked,
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
        split: bool = False,  # noqa: ARG002 - split applies only to makeps3iso
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        return self._service.convert(
            input_path, output_path, mode,
            compression=compression, cancel_event=cancel_event,
        )

    async def post_convert(
        self, input_path: str, output_path: str, mode: str,
        *, within: Sequence[str] | None = None,
    ) -> None:
        """Embed disc-ID GAME/NAME tags into a freshly created CD/DVD CHD.

        This is the single home for conversion-time disc-ID tagging: a direct
        ``createcd`` / ``createdvd`` job reaches it via
        ``job_manager._process_job`` and a ``cso_to_chd`` chain reaches it via
        ``ChainTool``'s final step, so both paths tag identically.

        Only ``createcd`` / ``createdvd`` carry a disc serial, so every other
        chdman mode is a no-op. Best-effort, with one exception: a *tagging*
        failure never fails the job, but a chdman child that outlived SIGKILL
        does (``DiscIdStorageAbandoned``). That is not a fact about the tag, it
        is the output's storage no longer answering -- and every step after this
        hook reads that storage, so continuing buys a hung job instead of an
        untagged one (issue #268).
        Idempotent: if the CHD already carries a GAME tag equal to the source's
        serial the embed is skipped, so re-invoking the hook never appends a
        duplicate tag.

        ``GAME`` = normalized serial (emulator DB lookup key);
        ``NAME`` = human-readable title when available, serial otherwise.

        ``within``: the trusted root an archive-extracted source sits under
        (the worker's extraction dir); None means the configured Volumes. A
        track sheet whose tracks are refused there, or that is unreadable,
        stops tagging with a warning naming the sheet.
        """
        if mode not in _DISC_ID_MODES:
            return
        if Path(output_path).suffix.lower() != ".chd":
            return
        if not await run_in_threadpool(os.path.exists, output_path):
            return
        try:
            disc_info = await run_in_threadpool(
                partial(extract_from_source, input_path, within=within),
            )
            if not (disc_info and disc_info.get("game_id")):
                if Path(input_path).suffix.lower() in _SHEET_EXTS and (
                    await run_in_threadpool(
                        partial(sheet_track_list, input_path, within=within),
                    )
                ) is None:
                    logger.warning(
                        "Disc ID not tagged for %s: its track sheet is unreadable "
                        "or names a track outside the trusted roots", input_path,
                    )
                return
            game_id = disc_info["game_id"]
            existing = await read_embedded_game_id(output_path, self.binary_path)
            if existing == game_id:
                return
            if existing is not None:
                # chdman addmeta appends rather than replaces, so a CHD that
                # already carries a *different* serial must have the stale
                # GAME/NAME stripped first — otherwise the old tag persists and
                # the embed never converges. A freshly created CHD has no tag,
                # so this is skipped in the normal path.
                await clear_embedded_disc_id(output_path, self.binary_path)
            title = disc_info.get("title") or game_id
            embedded = await embed_in_chd(output_path, game_id, title, self.binary_path)
            if embedded:
                logger.info(
                    "Embedded disc ID %r in %s", game_id, Path(output_path).name,
                )
            else:
                logger.debug(
                    "Failed to embed disc ID %r in %s",
                    game_id, Path(output_path).name,
                )
        except DiscIdStorageAbandoned as exc:
            # The one failure that is *not* best-effort. A chdman child is still
            # running against this output, and everything the caller does next
            # touches the same storage -- `_compute_output_size` is an unbounded
            # `run_in_threadpool`, and delete-on-verify starts a verifier -- so
            # swallowing this trades a skipped tag for a hung job, which is
            # #263's original failure. Propagate and let the job fail (#268).
            logger.error("Disc ID embed abandoned for %s: %s", output_path, exc)
            raise
        except Exception as exc:  # best effort; tagging never fails the job
            logger.debug("Disc ID embed skipped for %s: %s", output_path, exc)

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

    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]:
        # CHDs carry an overall SHA1 (header) and a data SHA1 (uncompressed
        # content) in their metadata. Read them from the metadata store
        # (primed by the library scan / a prior /info call) so matching a CHD
        # against a DAT never has to re-hash the whole file. The read is
        # instant, so ``cancel_event`` is accepted for contract parity but
        # unused.
        from services.chd_metadata_store import chd_metadata_store

        # One atomic snapshot (row + current mtime in a single read) instead of
        # get_metadata() + is_stale(), whose two separate reads could see the
        # hashes at one mtime and judge staleness at another.
        metadata = await chd_metadata_store.get_fresh_metadata(path)
        if not metadata:
            # No cached metadata, or the file changed since it was cached so the
            # stored header/data SHA1 describe an older disc. Either way report
            # *no embedded candidates* (rather than raise): chdman is
            # non-exhaustive, so the caller still falls back to a file-level
            # SHA1 of the current .chd, which is valid for any DAT that indexes
            # the container bytes. Only the stale embedded hashes are
            # suppressed, not the whole match.
            return []
        out: list[tuple[str, str]] = []
        sha1 = (metadata.get("sha1") or "").strip().lower()
        if sha1:
            out.append((sha1, "chd_sha1"))
        data_sha1 = (metadata.get("data_sha1") or "").strip().lower()
        if data_sha1:
            out.append((data_sha1, "chd_data_sha1"))
        return out

    async def content_digest(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> ContentDigest | None:
        ext = Path(path).suffix.lower()
        if ext in _RAW_IMAGE_EXTS:
            value = await _hash_files([path], cancel_event)
        elif ext in _SHEET_EXTS:
            value = await _hash_sheet(path, cancel_event)
        elif ext == ".chd":
            value = await self._chd_image_sha1(path, cancel_event)
        else:
            return None
        return ContentDigest(kind=IMAGE_DATA_SHA1_KIND, value=value.lower())

    async def _chd_image_sha1(
        self, path: str, cancel_event: asyncio.Event | None,
    ) -> str:
        """The digest value of the image a CHD decodes to.

        DVD/HD: the header's data SHA1. The job only asks after the container
        check passed, and ``chdman verify`` recomputes exactly that hash from
        the decoded data; ``ChdmanService.verify`` passes only when chdman said
        the raw SHA1 matched (it exits 0 on a mismatch), so the header value is
        proven. CD/GD-ROM: extracted to a scratch dir and digested like a
        Source sheet, track table included (DESIGN §3.3.12 records why it is
        not computed in place). Never the overall SHA1: it folds in metadata.
        """
        try:
            info = await self._service.info(path)
        except Exception as e:  # chdman failed, timed out, or left a child behind
            raise ContentDigestUnavailable(f"chdman info failed: {e}") from e
        tags = set(_TAG_RE.findall(info.get("raw_data", "")))
        if tags & _IMAGE_TAGS:
            data_sha1 = (info.get("data_sha1") or "").strip()
            if not _SHA1_RE.fullmatch(data_sha1):
                raise ContentDigestUnavailable("chdman info gave no data SHA1")
            return data_sha1
        if tags & (_CD_TAGS | _GDROM_TAGS):
            sheet_ext = ".gdi" if tags & _GDROM_TAGS else ".cue"
            return await self._extracted_cd_sha1(
                path, sheet_ext, _logical_bytes(info), cancel_event,
            )
        raise ContentDigestUnavailable(
            f"no CD, DVD or hard-disk metadata in {Path(path).name}",
        )

    async def _extracted_cd_sha1(
        self,
        path: str,
        sheet_ext: str,
        required_bytes: int,
        cancel_event: asyncio.Event | None,
    ) -> str:
        """Extract a CD CHD to a scratch dir and hash its tracks like a Source sheet.

        The dir must pass the shared headroom check first and is removed on
        every exit (``headroom_scratch_dir``), so a failed, cancelled or
        timed-out extract leaves nothing behind.
        """
        try:
            async with headroom_scratch_dir("cmptr-roundtrip-", required_bytes) as work:
                sheet = os.path.join(work, f"image{sheet_ext}")
                if not await self._service.extract_cd(
                    path, sheet, cancel_event=cancel_event,
                ):
                    raise ContentDigestUnavailable("chdman extractcd did not finish")
                return await _hash_sheet(sheet, cancel_event)
        except InsufficientDiskSpace as e:
            raise ContentDigestUnavailable(f"not enough space to extract: {e}") from e

    def info_model(self, raw: dict, path: str) -> CHDInfo:
        return CHDInfo(
            file=path,
            input_file=raw.get("input_file"),
            file_version=raw.get("file_version"),
            logical_size=raw.get("logical_size"),
            hunk_size=raw.get("hunk_size"),
            total_hunks=raw.get("total_hunks"),
            unit_size=raw.get("unit_size"),
            total_units=raw.get("total_units"),
            compression=raw.get("compression"),
            chd_size=raw.get("chd_size"),
            ratio=raw.get("ratio"),
            sha1=raw.get("sha1"),
            data_sha1=raw.get("data_sha1"),
            raw_data=raw.get("raw_data", ""),
            media_type=raw.get("media_type"),
            game_id=raw.get("game_id"),
            title=raw.get("title"),
        )

    def active_pids(self) -> list[int]:
        return self._service.active_pids()


def _logical_bytes(info: dict) -> int:
    """The CHD's logical size, the most a decode can write.

    For a CD it counts 2448-byte frames, so it bounds the extracted track files
    (at most 2352 bytes a frame) from above. When it cannot be read the decode
    cannot be sized, and the headroom check is not skipped for that: the Source
    is kept.
    """
    digits = re.sub(r"\D", "", str(info.get("logical_size") or ""))
    if not digits:
        raise ContentDigestUnavailable("chdman info gave no logical size")
    return int(digits)


async def _hash_files(paths: list[str], cancel_event: asyncio.Event | None) -> str:
    """SHA1 of ``paths`` end to end, on a detached thread (whole-file reads)."""
    try:
        return await run_detached(
            compute_files_sha1_sync, paths, cancel_event=cancel_event,
        )
    except (OSError, ReadCancelled) as e:
        raise ContentDigestUnavailable(f"could not hash the image: {e}") from e


async def _hash_sheet(sheet: str, cancel_event: asyncio.Event | None) -> str:
    """The digest value of a cue/gdi: its track data and its track table.

    ``<sha1 of the track files in sheet order>``, plus ``/<sha1 of the track
    table>`` unless the table is the one chdman infers for a bare image of those
    bytes (``utils.disc_sheet.layout_fingerprint``). So equal values mean the
    same sector bytes *and* the same tracks, types, sector sizes, lengths, index
    offsets and gaps, however the tracks are split into files.
    """
    try:
        tracks = await run_detached(sheet_track_paths, sheet, cancel_event=cancel_event)
        layout = await run_detached(layout_fingerprint, sheet, cancel_event=cancel_event)
    except (OSError, ValueError, ReadCancelled) as e:
        raise ContentDigestUnavailable(f"could not read {Path(sheet).name}: {e}") from e
    if not tracks:
        raise ContentDigestUnavailable(f"{Path(sheet).name} names no track files")
    data_sha1 = await _hash_files(tracks, cancel_event)
    return data_sha1 if layout is None else f"{data_sha1}/{layout}"
