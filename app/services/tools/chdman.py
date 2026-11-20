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
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from logging_setup import get_logger
from models import CHDInfo, OutputStatus
from services.chdman import CHDMAN_CONVERTIBLE_EXTENSIONS, chdman_service
from services.disc_id import (
    DiscIdStorageAbandoned,
    bounded_disc_read,
    disc_platform,
    extract_from_source,
)
from services.disk import InsufficientDiskSpace, headroom_scratch_dir
from services.file_hasher import compute_files_sha1_sync
from services.lock_manager import lock_manager
from services.subprocess_runner import ReadCancelled, StorageAbandoned, run_detached
from utils.disc_sheet import read_disc_sheet
from utils.sheet_text import sheet_track_list

from .base import (
    IMAGE_DATA_SHA1_KIND,
    BaseTool,
    ContentDigest,
    ContentDigestUnavailable,
    EmbeddedHashUnavailable,
)
from .binaries import RequiredBinary
from .spec import ModeKind, ModeSpec

logger = get_logger()

# Only CD/DVD images carry a readable disc serial: they hold an ISO 9660
# filesystem with SYSTEM.CNF (PS1/PS2) or PSP_GAME/PARAM.SFO (PSP), or a
# Dreamcast IP.BIN. createraw/createhd (hard-disk images) and createld
# (laserdisc) have no such structure, so disc-ID recording is scoped to these two.
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
# What chdman compresses with when a create job sends no ``-c`` (its default
# compression lists in chdman.cpp): CD images use the CD codecs, hard disk,
# raw and DVD use the general list, laserdisc A/V Huffman.
_CD_DEFAULT_CODECS = ("cdlz", "cdzl", "cdfl")
_GENERAL_DEFAULT_CODECS = ("lzma", "zlib", "huff", "flac")
_IMPLICIT_CODECS = {
    "createraw": _GENERAL_DEFAULT_CODECS,
    "createhd": _GENERAL_DEFAULT_CODECS,
    "createcd": _CD_DEFAULT_CODECS,
    "createdvd": _GENERAL_DEFAULT_CODECS,
    "createld": ("avhu",),
}
# The codecs each create mode takes with ``-c`` (#471), the shared
# ``compression_problem`` refuses the rest before a job exists. chdman parses
# any four-letter codec it knows (``parse_compression``, so ``lzma2`` is an
# "Invalid compressor" everywhere), but a CD codec refuses a hunk that is not
# whole 2448-byte CD frames (``chd_cd_compressor``: CODEC_ERROR), and only
# createcd writes those: createdvd sends ``-hs 2048``, createhd and createraw
# (``-us 512``) default to 4096. avhu encodes the A/V frames only createld
# writes. The general codecs compress any bytes, so every mode takes them,
# and ``none`` stores the hunks as they are.
_GENERAL_CODECS = frozenset({"none", "zlib", "zstd", "lzma", "huff", "flac"})
_CD_CODECS = frozenset({"cdzl", "cdzs", "cdlz", "cdfl"})
_CREATE_CODECS = {
    "createraw": _GENERAL_CODECS,
    "createhd": _GENERAL_CODECS,
    "createcd": _GENERAL_CODECS | _CD_CODECS,
    "createdvd": _GENERAL_CODECS,
    "createld": _GENERAL_CODECS | {"avhu"},
}
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
# The kind (IMAGE_DATA_SHA1_KIND) lives in base.py, shared with other tools.
# The modes whose Source and output both have that digest: CD and DVD (#300),
# hard disk, raw and CHD-to-CHD copy (#301). A raw or HD CHD's data SHA1 is the
# SHA1 of the input bytes; a copy keeps the Source CHD's data.
_ROUND_TRIP_MODES = frozenset({"createcd", "createdvd", "createhd", "createraw", "copy"})
# Create LD has no comparable digest: its Source is an AVI, the CHD's data SHA1
# covers chdman's A/V hunk encoding, and extractld does not rebuild the AVI byte
# for byte. Deleting the AVI on the container check alone is not offered (#301).
_NO_DELETE_SOURCE_MODES = frozenset({"createld"})
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


def _holds_image(info: dict) -> bool | None:
    """Whether a CHD's data is a flat image, from its ``chdman info``.

    True for a DVD or hard-disk CHD, or one with no Metadata lines at all (a
    createraw CHD): its data SHA1 is the SHA1 of the image's bytes. False for a
    CD or GD-ROM CHD, whose data is frames padded per track. None when the
    Metadata lines are there but name no tag this reads: not proven either way.
    """
    tags = set(_TAG_RE.findall(info.get("raw_data", "") or ""))
    if tags & _IMAGE_TAGS or not (tags or info.get("metadata_lines")):
        return True
    if tags & (_CD_TAGS | _GDROM_TAGS):
        return False
    return None


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
            codecs=_CREATE_CODECS[mode],
            implicit_codecs=_IMPLICIT_CODECS[mode],
            supports_delete_on_verify=mode not in _NO_DELETE_SOURCE_MODES,
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
            implicit_codecs_unknown=True,
            supports_delete_on_verify=True,
            # Source and output are both CHDs, digested the same way.
            supports_round_trip=True,
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
    # One disc each. A .bin is a track of a disc, never the disc itself.
    disc_extensions = frozenset({".cue", ".gdi", ".iso", ".chd"})
    # The data SHA1, and only for a DVD or hard-disk CHD (#388,
    # `raw_image_sha1`). Never `chd_sha1`: the overall SHA1 folds in the
    # metadata hashes, so it never equals a raw image's.
    raw_image_hash_labels = frozenset({"chd_data_sha1"})

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self._service = chdman_service

    @property
    def required_binaries(self) -> tuple[RequiredBinary, ...]:
        # The program every conversion runs, read where the service spawns it
        # (#494): missing or not executable, and the tool isn't ready.
        return (RequiredBinary("chdman", self._service.chdman_path, "CHDMAN_PATH"),)

    def header_platform(self, path: str, *, within=None) -> str | None:
        # PS1/PS2/PSP/Dreamcast through the disc-ID parser, Saturn and Sega CD
        # through their sector-0 system ids. A sheet's tracks resolve within
        # *within* when given (an Inbox's unpack folder, #432).
        return disc_platform(path, within=within)

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
        """Record the Source's disc ID for a freshly created CD/DVD CHD.

        The single home for conversion-time disc IDs: a direct ``createcd`` /
        ``createdvd`` job reaches it via ``job_manager._process_job`` and a
        ``cso_to_chd`` chain via ``ChainTool``'s final step.

        The serial (and title, when the Source has one) read from the Source is
        stored for the output in ``chd_metadata_store``, the cache ``/api/info``
        and the library scan read, and the output is marked as checked so the
        scan does not read it again. Nothing is written into the CHD: chdman
        opens a compressed CHD read-only, so ``chdman addmeta`` fails on every
        CHD this app makes (#352). No chdman process runs here.

        Only ``createcd`` / ``createdvd`` carry a disc serial, so every other
        chdman mode is a no-op. Best effort about the disc ID, not about
        storage: a read or database failure never fails the job and is logged
        as a warning naming the file (without a record, the scan and
        ``/api/info`` read the ID from the CHD's own sectors later). Storage
        that stops answering is different: every step after this hook reads
        the same files, so ``DiscIdStorageAbandoned`` propagates and the job
        manager fails the job instead of hanging it (#268).

        ``within``: the trusted root an archive-extracted source sits under
        (the worker's extraction dir); None means the configured Volumes. A
        track sheet whose tracks are refused there, or that is unreadable,
        records nothing, with a warning naming the sheet.
        """
        if mode not in _DISC_ID_MODES:
            return
        if Path(output_path).suffix.lower() != ".chd":
            return
        try:
            # One detached read through the shared disc-ID seam (#352): a
            # Source or output Volume that stops answering raises
            # DiscIdStorageAbandoned (not caught here) instead of holding the
            # job and the queue slot behind it.
            outcome, disc_info = await bounded_disc_read(
                _read_source_disc_id, input_path, output_path, within,
                path=input_path, cancel_event=None,
            )
        except (OSError, ValueError):
            # An ordinary read failure: best effort, but never silent.
            logger.warning(
                "Disc ID not read from %s for %s", input_path, Path(output_path).name,
                exc_info=True,
            )
            return
        if outcome == "sheet refused":
            logger.warning(
                "Disc ID not recorded for %s: its track sheet is unreadable "
                "or names a track outside the trusted roots", input_path,
            )
        if outcome != "found":
            return
        await _record_disc_id(output_path, disc_info["game_id"], disc_info.get("title"))

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

    async def prime_embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> None:
        # A CHD no scan has seen (an Inbox's bound copy, #432): `chdman info`
        # read once, under its own bound and the
        # caller's cancel, and the store primed for that path, as a scan or
        # raw_image_sha1 would.
        if Path(path).suffix.lower() != ".chd":
            return
        from services.chd_metadata_store import chd_metadata_store

        if await chd_metadata_store.get_fresh_metadata(path):
            return
        try:
            await chd_metadata_store.read_and_cache(
                path, lambda: self._service.info(path, cancel_event=cancel_event),
            )
        except (ReadCancelled, StorageAbandoned):
            raise
        except Exception:
            logger.info("chdman info failed for %s; matching by its own hash", path)

    async def raw_image_sha1(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> str | None:
        # Only a DVD or hard-disk CHD's data SHA1 is its image's SHA1 (checked
        # on a real createdvd CHD, tests/test_duplicates_388_real.py). A CD
        # CHD's covers 2448-byte frames padded per track, so it gets no key.
        # The labelled hash is read from the metadata store; a CHD it has no
        # current row for is read once with `chdman info` (the header only,
        # under its own bound) and the store primed, as a scan would.
        if Path(path).suffix.lower() != ".chd":
            return None
        from services.chd_metadata_store import chd_metadata_store

        # A row must match the file's size and st_mtime_ns (one written before
        # they were recorded is stale): a key stored against the new identity
        # must never come from the file it replaced.
        metadata = await chd_metadata_store.get_fresh_metadata(path)
        if not metadata:
            try:
                metadata, stored = await chd_metadata_store.read_and_cache(
                    path, lambda: self._service.info(path, cancel_event=cancel_event),
                )
            except Exception as e:  # chdman failed or timed out
                if isinstance(e, (ReadCancelled, StorageAbandoned)):
                    # A cancel, or a child that outlived SIGKILL (the storage
                    # is not answering): no failure of this file, and the
                    # caller decides whether to go on.
                    raise
                raise EmbeddedHashUnavailable(f"chdman info failed: {e}") from e
            if stored is None:
                raise EmbeddedHashUnavailable(f"{Path(path).name} changed while it was read")
        if not _holds_image(metadata):
            return None
        return await super().raw_image_sha1(path, cancel_event=cancel_event)

    async def _content_sha1(
        self, path: str, target: str, *, cancel_event: asyncio.Event | None = None,
    ) -> str | None:
        # A full pass for a DVD or hard-disk CHD (#389): `chdman verify`
        # decodes every hunk and recomputes the data SHA1, passing only when
        # it equals the header's; that header value, read fresh (no cache), is
        # then the image's SHA1. A CD CHD has no raw image hash at all. *path*
        # says it is a CHD; *target* (the held file) is what chdman reads.
        if Path(path).suffix.lower() != ".chd":
            return None
        result = await self.verify(target, cancel_event=cancel_event)
        if not result.get("valid"):
            raise EmbeddedHashUnavailable(
                f"{Path(path).name} did not pass chdman verify "
                f"({result.get('message') or 'no verdict'})",
            )
        try:
            info = await self._service.info(target, cancel_event=cancel_event)
        except Exception as e:  # chdman failed or timed out
            if isinstance(e, (ReadCancelled, StorageAbandoned)):
                raise  # a cancel or a stuck child: the caller decides
            raise EmbeddedHashUnavailable(f"chdman info failed: {e}") from e
        if not _holds_image(info):
            return None
        data_sha1 = (info.get("data_sha1") or "").strip().lower()
        if not _SHA1_RE.fullmatch(data_sha1):
            raise EmbeddedHashUnavailable("chdman info gave no data SHA1")
        return data_sha1

    async def content_digest(
        self,
        path: str,
        *,
        cancel_event: asyncio.Event | None = None,
        within: Sequence[str] | None = None,
    ) -> ContentDigest | None:
        # ``within`` is for the Source side: a sheet's tracks must resolve under
        # it (the job's extraction folder for an archive member, the Volumes
        # when None). A CHD's own extract is read under its scratch dir instead.
        ext = Path(path).suffix.lower()
        if ext in _RAW_IMAGE_EXTS:
            value = await _hash_files([path], cancel_event)
        elif ext in _SHEET_EXTS:
            value = await _hash_sheet(path, cancel_event, within)
        elif ext == ".chd":
            value = await self._chd_image_sha1(path, cancel_event)
        else:
            return None
        return ContentDigest(kind=IMAGE_DATA_SHA1_KIND, value=value.lower())

    async def source_problem(
        self,
        path: str,
        *,
        cancel_event: asyncio.Event | None = None,
        within: Sequence[str] | None = None,  # verifies the CHD file; reads no sheet
    ) -> str | None:
        # A CHD Source (Copy) is only a claim until chdman verify recomputes
        # its data SHA1: a damaged Source must not be compared, and deleted, as
        # if it were good. An image Source has nothing of its own to check.
        if Path(path).suffix.lower() != ".chd":
            return None
        result = await self.verify(path, cancel_event=cancel_event)
        if result.get("valid") or result.get("cancelled"):
            return None
        return f"the Source CHD is not Intact ({result.get('message') or 'verify failed'})"

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
            info = await self._service.info(path, cancel_event=cancel_event)
        except Exception as e:  # chdman failed, timed out, or left a child behind
            raise ContentDigestUnavailable(f"chdman info failed: {e}") from e
        holds_image = _holds_image(info)
        # DVD/HD, or no Metadata lines at all (a createraw CHD): the data SHA1
        # is the SHA1 of the image's bytes, proven by the container check
        # before this. Metadata lines the tag reader cannot parse are not "no
        # metadata": they fall through to "not proven" below.
        if holds_image:
            data_sha1 = (info.get("data_sha1") or "").strip()
            if not _SHA1_RE.fullmatch(data_sha1):
                raise ContentDigestUnavailable("chdman info gave no data SHA1")
            return data_sha1
        if holds_image is False:
            tags = set(_TAG_RE.findall(info.get("raw_data", "")))
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
                # The scratch dir is outside the Volumes; it is the one root
                # this sheet's tracks may resolve under.
                return await _hash_sheet(sheet, cancel_event, within=[work])
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


def _read_source_disc_id(
    input_path: str, output_path: str, within: Sequence[str] | None,
) -> tuple[str, dict | None]:
    """``post_convert``'s blocking reads, in one place to bound them together.

    Returns ``(outcome, disc_info)``: ``"no output"`` when the CHD is not
    there, ``"found"`` with the Source's disc ID, ``"sheet refused"`` when a
    track sheet could not be read under ``within``, otherwise ``"none"``.
    """
    if not os.path.exists(output_path):
        return "no output", None
    disc_info = extract_from_source(input_path, within=within)
    if disc_info and disc_info.get("game_id"):
        return "found", disc_info
    if Path(input_path).suffix.lower() in _SHEET_EXTS and (
        sheet_track_list(input_path, within=within) is None
    ):
        return "sheet refused", None
    return "none", None


async def _record_disc_id(output_path: str, game_id: str, title: str | None) -> None:
    """Store a converted CHD's disc ID in the app's database, or say why not.

    Marks the output as checked too, so the library scan's disc-ID phase skips
    it. A database failure is a warning naming the output and the serial, never
    a failed job: the scan reads the ID from the CHD later. The output's own
    stat not answering (``mark_disc_id_checked``'s bounded probe) is not that:
    the output's storage has stopped answering, so ``DiscIdStorageAbandoned``
    is raised for the job to fail on.
    """
    from services.chd_metadata_store import chd_metadata_store

    name = Path(output_path).name
    try:
        await chd_metadata_store.update_disc_id_info(output_path, game_id, title)
        await chd_metadata_store.mark_disc_id_checked(output_path)
    except asyncio.TimeoutError as exc:
        # Before the OSError arm: asyncio.TimeoutError is the built-in
        # TimeoutError, an OSError.
        logger.error("Recording the disc ID of %s: its storage did not answer", name)
        raise DiscIdStorageAbandoned(
            f"recording the disc ID of {name}: the output's storage did not "
            "answer, so the job stops before reading it again"
        ) from exc
    except (SQLAlchemyError, RuntimeError, OSError):
        logger.warning(
            "Disc ID %r not recorded for %s; the library scan will read it from "
            "the CHD", game_id, name, exc_info=True,
        )
        return
    logger.info("Recorded disc ID %r for %s", game_id, name)


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


async def _hash_sheet(
    sheet: str,
    cancel_event: asyncio.Event | None,
    within: Sequence[str] | None,
) -> str:
    """The digest value of a cue/gdi: its track data and its track table.

    ``<sha1 of the track files in sheet order>``, plus ``/<sha1 of the track
    table>`` unless the table is the one chdman infers for a bare image of those
    bytes (``utils.disc_sheet.read_disc_sheet``). So equal values mean the same
    sector bytes *and* the same tracks, types, sector sizes, lengths, index
    offsets and gaps, however the tracks are split into files.

    The sheet is read through ``utils.sheet_text`` with ``within`` as the
    trusted roots its tracks may resolve under; anything it refuses is
    unavailable, so the Source is kept.
    """
    try:
        disc = await run_detached(
            read_disc_sheet, sheet, within=within, cancel_event=cancel_event,
        )
    except (OSError, ValueError, ReadCancelled) as e:
        raise ContentDigestUnavailable(f"could not read {Path(sheet).name}: {e}") from e
    data_sha1 = await _hash_files(disc.tracks, cancel_event)
    return data_sha1 if disc.layout is None else f"{data_sha1}/{disc.layout}"
