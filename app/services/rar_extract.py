"""Extract a multi-volume RAR set to the one file inside it, with ``unar`` (#476).

Usenet downloads arrive as RAR sets: ``Game.rar`` + ``Game.r00`` … (the old
naming) or ``Game.part01.rar`` … (the new one), in RAR4 or RAR5. The
``rar_extract`` mode takes the set's first volume and writes the single file
the set was made to carry beside it, under that file's own name.

Planning reads only the set's headers, in Python (rarfile, through the shared
``services.archive_members``): the volumes, every member's name, size, CRC and
kind, and whether a password is needed. A set is refused before anything is
written unless it holds exactly one payload file (release sidecars and junk
never count, ``utils.junk.is_archive_sidecar``), no member path is absolute or
has a ``..``, no member is a link or a device, no password is needed, and
every volume rarfile reads is one the set's naming names (so the locks and the
delete take the whole set). Multi-file sets, such as a ``.cue`` with its
``.bin`` tracks, are not supported.

``unar`` (The Unarchiver, from Debian main) does the extraction, into a private
folder beside the output: it reads RAR5 and multi-volume sets, which the
image's ``unrar-free`` can't, and checks every CRC as it goes. The payload is
then checked again here (a plain file, the size and CRC32 the set records) and
moved onto the output path without replacing anything
(``services.output_placement``). A missing volume, a CRC error, a truncated
set or a taken name fails the job and keeps every volume, with nothing left
behind.

The checks after the conversion: ``verify`` reads the extracted file end to
end (the container check; the file has no container), and ``content_digest``
is the file's size and CRC32 on both sides, read from the set's headers for
the Source and hashed for the output (the Round-trip).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import stat
import threading
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

from logging_setup import get_logger
from services.archive import (
    ArchiveSafetyRefused,
    archive_service,
    reject_member_traversal,
)
from services.archive_members import (
    IncompleteArchive,
    later_volume_of,
    rar_later_volumes,
    read_rar_set,
)
from services.disk import ensure_headroom
from services.output_placement import OutputPlacement
from services.stream_digest import hash_files
from services.subprocess_runner import (
    ConversionCancelled,
    ReadCancelled,
    SubprocessRunner,
    collect_verify,
    remove_partial_tree,
    run_detached,
    run_detached_within_stall_bound,
    verify_exception_event,
    verify_preflight,
)
from utils.junk import is_archive_sidecar

from config import settings

RAR_EXTRACT_MODE = "rar_extract"
RAR_INPUT_EXTENSIONS = frozenset({".rar"})
# The SubprocessRunner owner: COMPRESSATORIUM_RAR_* overrides the shared policy.
_OWNER = "rar"
# The private extraction folder, hidden beside the output on its volume.
_WORK_PREFIX = ".rar-extract-"
_PRIVATE_DIR_MODE = 0o700
# How many names a refusal lists before "...".
_NAMES_SHOWN = 4

logger = get_logger("rar_extract")


class RarSetRefused(ValueError):
    """A RAR set this mode won't extract; the message says why and names
    what was found."""


@dataclass(frozen=True)
class RarPlan:
    """What extracting a set will do, read from its headers."""

    payload: str              # the member's path in the set
    size: int
    crc: int | None           # None when the set records no CRC32 (RAR5 BLAKE2)
    unpacked: int             # every file's size: what the work folder holds
    volumes: tuple[str, ...]  # every volume, first volume first


def _shown(names: list[str]) -> str:
    listed = ", ".join(names[:_NAMES_SHOWN])
    return listed + (", ..." if len(names) > _NAMES_SHOWN else "")


def _read(first_volume: str):
    """The set's headers, or a refusal saying why they can't be read."""
    name = os.path.basename(first_volume)
    try:
        return read_rar_set(first_volume)
    except IncompleteArchive as exc:
        raise RarSetRefused(
            f"{name}: a volume of the set is missing, truncated or unreadable ({exc})",
        ) from exc
    except Exception as exc:  # any rarfile or OS error: fail closed
        logger.debug("Could not read the RAR set %s", first_volume, exc_info=True)
        raise RarSetRefused(f"{name} can't be read as a RAR set: {exc}") from exc


def plan_set(first_volume: str) -> RarPlan:
    """Plan extracting the set *first_volume* leads, or refuse it.

    Raises :class:`RarSetRefused` with the reason, naming what was found. Reads
    headers only. Blocking: run it detached.
    """
    name = os.path.basename(first_volume)
    first = later_volume_of(first_volume)
    if first is not None:
        raise RarSetRefused(
            f"{name} is a later volume of a RAR set; select its first volume, "
            f"{os.path.basename(first)}",
        )
    listing = _read(first_volume)
    members = listing.members
    if listing.encrypted or any(m.is_encrypted for m in members):
        raise RarSetRefused(f"{name} is encrypted; a password-protected set can't be extracted")
    if not members:
        raise RarSetRefused(f"{name} holds no files")
    for member in members:
        try:
            reject_member_traversal(member.name)
        except ArchiveSafetyRefused as exc:
            raise RarSetRefused(
                f"{name} has an unsafe member path ({member.name}): an absolute "
                "path or a .. could write outside the extraction folder",
            ) from exc
    special = [m.name for m in members if not m.is_dir and not m.is_plain_file]
    if special:
        raise RarSetRefused(
            f"{name} holds a link or special file ({_shown(special)}); only plain "
            "files are extracted",
        )
    try:
        archive_service.enforce_archive_limits([(m.name, m.size) for m in members])
    except ArchiveSafetyRefused as exc:
        raise RarSetRefused(f"{name} is over the archive limits: {exc.rule}") from exc
    files = [m for m in members if not m.is_dir]
    payload = [m for m in files if not is_archive_sidecar(m.name)]
    if not payload:
        raise RarSetRefused(
            f"{name} holds no file to extract, only sidecars or junk "
            f"({_shown([m.name for m in files])})",
        )
    if len(payload) > 1:
        raise RarSetRefused(
            f"{name} holds {len(payload)} files ({_shown([m.name for m in payload])}); "
            "this mode extracts a set with exactly one file besides its .nfo/.sfv/"
            ".txt/.url sidecars. Multi-file sets, such as a .cue with its .bin "
            "tracks, are not supported",
        )
    # Every volume the set is read from must be one its naming names: those
    # are what the job locks and what deleting the Source removes.
    folder = os.path.dirname(first_volume)
    named = {os.path.basename(p) for p in [first_volume, *rar_later_volumes(first_volume)]}
    read = [os.path.basename(v) for v in listing.volumes]
    stray = [v for v in read if v not in named]
    if stray:
        raise RarSetRefused(
            f"{name} reads volume(s) not named like the rest of the set "
            f"({_shown(stray)}), so they would not be kept or deleted with it",
        )
    (only,) = payload
    return RarPlan(
        payload=only.name,
        size=only.size,
        crc=only.crc,
        unpacked=sum(m.size for m in files),
        volumes=tuple(os.path.join(folder, v) for v in read),
    )


def set_stem(first_volume: str) -> str:
    """The set's name without its volume suffix (``Game.part01.rar`` and
    ``Game.rar`` are both ``Game``): the output name of last resort."""
    name = os.path.basename(first_volume)
    stem = Path(name).stem
    lowered = stem.lower()
    cut = lowered.rfind(".part")
    if cut > 0 and lowered[cut + 5:].isdigit():
        return stem[:cut]
    return stem


def _make_work_dir(work_dir: str, abandoned: threading.Event) -> None:
    """Create the private work folder; undo it if the caller gave up meanwhile."""
    os.makedirs(os.path.dirname(work_dir), exist_ok=True)
    os.mkdir(work_dir, _PRIVATE_DIR_MODE)
    if abandoned.is_set():
        shutil.rmtree(work_dir, ignore_errors=True)


def _check_product(work_dir: str, produced: str, size: int) -> None:
    """The extracted payload is a plain file of the planned size, reached
    with no link anywhere below the work folder. Blocking."""
    current = work_dir
    st = None
    for part in os.path.relpath(produced, work_dir).split(os.sep):
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise RuntimeError(
                f"unar finished without writing {os.path.basename(produced)}",
            ) from None
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"unar wrote {part} as a link; nothing was kept")
    if st is None or not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"unar did not write {os.path.basename(produced)} as a file")
    if st.st_size != size:
        raise RuntimeError(
            f"unar wrote {st.st_size} bytes of {os.path.basename(produced)}, not the "
            f"{size} the set records; nothing was kept",
        )


class RarExtractService:
    """``unar`` over a planned RAR set."""

    def __init__(self) -> None:
        self.unar_path = settings.unar_path
        self._runner = SubprocessRunner(owner=_OWNER)

    plan = staticmethod(plan_set)

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    def abandoned_pids(self) -> list[int]:
        return self._runner.abandoned_pids()

    def _build_command(self, first_volume: str, work_dir: str) -> list[str]:
        # -D: no containing folder of unar's own, so members land at their
        # paths under the work folder. -nr: never unpack an archive found
        # inside. -k skip: no AppleDouble resource-fork files. unar takes no
        # "--", so both paths are absolute: a name starting with "-" can't
        # read as an option. Its stdin is /dev/null (the runner), so it never
        # waits for a password; an encrypted set was refused when planned.
        return [
            self.unar_path, "-o", os.path.abspath(work_dir), "-D", "-nr", "-k", "skip",
            os.path.abspath(first_volume),
        ]

    # ----- output paths -----------------------------------------------------

    @staticmethod
    def get_output_path(input_path: str, output_dir: str | None = None) -> str:
        """The payload's own name in *output_dir* (default: the set's folder).

        Falls back to the set's name for a set that can't be planned, so a
        caller can still compute a path; planning refuses such a set before a
        job is queued, and the conversion plans again.
        """
        try:
            filename = os.path.basename(plan_set(input_path).payload)
        except RarSetRefused:
            filename = set_stem(input_path)
        folder = output_dir or os.path.dirname(input_path)
        return os.path.join(folder, filename)

    @staticmethod
    def expected_output_size(input_path: str) -> int | None:
        """Every file's unpacked size (sidecars land in the work folder too),
        from the headers; None for a set that can't be planned. Blocking."""
        try:
            return plan_set(input_path).unpacked
        except RarSetRefused:
            return None

    # ----- convert ----------------------------------------------------------

    async def convert(
        self,
        input_path: str,
        output_path: str,
        mode: str = RAR_EXTRACT_MODE,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Extract the set's one file onto *output_path*, yielding progress.

        Plans again first (the set may have changed since the job was
        queued), so a refused set fails here with its reason before anything
        is written. Never replaces a file at *output_path*.
        """
        if mode != RAR_EXTRACT_MODE:
            raise ValueError(f"Unsupported rar mode: {mode}")
        try:
            plan = await run_detached_within_stall_bound(
                plan_set, input_path,
                what="Reading the RAR set's headers", cancel_event=cancel_event,
            )
        except ReadCancelled as exc:
            raise ConversionCancelled("Conversion cancelled") from exc
        member = os.path.basename(plan.payload)
        yield {
            "progress": 1,
            "message": f"Extracting {member} from {len(plan.volumes)} volume(s)...",
        }

        work_dir = os.path.join(
            os.path.dirname(os.path.abspath(output_path)),
            f"{_WORK_PREFIX}{secrets.token_hex(6)}",
        )
        produced = os.path.join(work_dir, *plan.payload.split("/"))
        abandoned = threading.Event()
        # The final move, safe to give up on: never over an existing file, and
        # a move that lands after the job gave up removes its own product.
        placement = OutputPlacement(output_path, label="extracted file")
        try:
            # Every file the set unpacks lands in the work folder on the
            # output's volume (sidecars too), so that is the room it needs,
            # from the headers, before unar writes a byte.
            await run_detached_within_stall_bound(
                ensure_headroom, [(work_dir, plan.unpacked)],
                what="Checking free space for the extraction", cancel_event=cancel_event,
            )
            await run_detached_within_stall_bound(
                _make_work_dir, work_dir, abandoned,
                what="Preparing the extraction folder", cancel_event=cancel_event,
            )
            async for update in self._runner.run(
                self._build_command(input_path, work_dir),
                input_path=input_path,
                # The work folder, not the payload's path: the runner creates
                # its output's parent folder before the spawn, and unar extracts
                # a member folder that already exists under another name
                # (``vols`` becomes ``vols-1``). `_check_product` then says
                # whether the payload is where the plan put it.
                output_path=work_dir,
                output_growth_paths=lambda: [produced],
                parse_progress=lambda _line: None,  # unar prints no percentage
                initial_progress=1,
                cancel_event=cancel_event,
                fail_label="unar",
                complete_message=f"Extracted {member}",
            ):
                if update.get("progress", 0) >= 100:
                    # Hold the bar short of done until the file is checked and placed.
                    update = {**update, "progress": 95}
                yield update
            await run_detached_within_stall_bound(
                _check_product, work_dir, produced, plan.size,
                what="Checking the extracted file", cancel_event=cancel_event,
            )
            if plan.crc is not None:
                # unar checked every CRC as it read; this checks the bytes that
                # actually landed, so the file kept is the one the set records.
                crc = await hash_files([produced], algorithm="crc32", cancel_event=cancel_event)
                if int(crc, 16) != plan.crc:
                    raise RuntimeError(
                        f"The extracted {member} does not match the CRC32 the set "
                        "records for it; nothing was kept",
                    )
            yield {"progress": 98, "message": f"Placing {member}..."}
            await run_detached_within_stall_bound(
                placement.place, produced,
                what="Placing the extracted file", cancel_event=cancel_event,
            )
        except (
            ConversionCancelled, ReadCancelled, RuntimeError, ValueError, OSError,
            asyncio.CancelledError, GeneratorExit,
        ) as exc:
            # unar writes inside the work folder only; the output path is
            # touched by the final move alone, and swept only while it is
            # still the file that move placed. The volumes are never written.
            await placement.abandon_and_sweep()
            if isinstance(exc, ReadCancelled):
                raise ConversionCancelled("Conversion cancelled") from exc
            raise
        finally:
            # On the output volume, which may be what the run just died on:
            # the shared bounded sweep. Flag the setup first, so a setup a
            # cancel abandoned removes what it creates after this sweep.
            abandoned.set()
            await remove_partial_tree(work_dir, label="rar extract dir")
        yield {"progress": 100, "message": f"Extracted {member}"}

    # ----- checks -----------------------------------------------------------

    async def verify(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> dict:
        return await collect_verify(
            self.verify_stream(path, cancel_event=cancel_event),
            fallback_message="Verification failed",
        )

    async def verify_stream(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict, None]:
        """The container check of an extracted file: it reads end to end.

        The file has no container of its own, so this is all its own check can
        say. What proves its content is the CRC32 the conversion compared with
        the set's headers before placing it, and the Round-trip a delete-source
        job runs (:meth:`content_digest`).
        """
        own = frozenset({os.path.splitext(path)[1].lower()})
        problem, _size = await verify_preflight(path, own, cancel_event=cancel_event)
        if problem is not None:
            yield problem
            return
        yield {"type": "progress", "progress": 0, "message": "Reading the file..."}
        try:
            await hash_files([path], algorithm="crc32", cancel_event=cancel_event)
        except ReadCancelled:
            yield {
                "type": "error", "valid": False, "cancelled": True,
                "message": "Verification cancelled",
            }
            return
        except OSError as exc:  # unreadable: no verdict (#306)
            yield verify_exception_event(exc, prefix="Cannot read file")
            return
        yield {
            "type": "complete", "progress": 100, "valid": True,
            "message": "File is Intact",
        }

    async def payload_digest(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> tuple[int, int] | None:
        """``(size, crc32)`` of what *path* holds: a set's payload from its
        headers, a loose file hashed. None for a set with no CRC32 recorded.
        Raises :class:`RarSetRefused` for a set that can't be planned."""
        if Path(path).suffix.lower() in RAR_INPUT_EXTENSIONS:
            plan = await run_detached(plan_set, path, cancel_event=cancel_event)
            return None if plan.crc is None else (plan.size, plan.crc)
        crc = await hash_files([path], algorithm="crc32", cancel_event=cancel_event)
        size = await run_detached(os.path.getsize, path, cancel_event=cancel_event)
        return int(size), int(crc, 16)

    # ----- info -------------------------------------------------------------

    @staticmethod
    def info(file_path: str) -> dict:
        """The set's first volume, and the file it holds when it plans.
        Synchronous (a header read); wrap callers in a threadpool."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        size = os.path.getsize(file_path)
        contained = original = None
        volumes = 1
        try:
            plan = plan_set(file_path)
        except RarSetRefused as exc:
            logger.debug("rar info: %s", exc)
        else:
            contained = os.path.basename(plan.payload)
            original = plan.size
            volumes = len(plan.volumes)
        size_mb = size / (1024 * 1024)
        return {
            "file": file_path,
            "size": size,
            "size_display": f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB",
            "format": "RAR set",
            "extension": Path(file_path).suffix.lower(),
            "compressed": True,
            "compression_type": "RAR",
            "contained_name": contained,
            "original_size": original,
            "volumes": volumes,
        }


rar_service = RarExtractService()
