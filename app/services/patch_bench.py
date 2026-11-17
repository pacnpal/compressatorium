"""The Patch action (#428): an IPS or BPS patch applied to a cartridge Image.

A person picks one Image, loose or inside a ``.zip``/``.7z``, and one patch
file, picked from a Volume or uploaded to the app's data directory. The job
writes ``Name (Patched).7z`` beside the Image and never changes the Image:

1. **The patch** is read with no symlink anywhere in its path
   (``read_beneath``), under ``CHD_PATCH_MAX_PATCH_SIZE``. A picked patch
   must be inside a Volume; "couldn't tell" refuses it. Its SHA1 goes in the
   Ledger row (``extra_inputs``).
2. **The ROM** is read the same way, under ``CHD_PATCH_MAX_ROM_SIZE``. One in
   an archive is unpacked under the archive limits (``enforce_archive_limits``
   on the whole listing, then ``ArchiveService.extract_file``); an archive
   holding more than one file needs its member named (``archive.zip::Game.gba``).
3. **Disc Images are refused**, by the tool registry's own
   ``disc_extensions()``: the MVP patches single-file cartridge Images.
4. **The engine** (``services.patch_engine``) runs on a detached thread under
   the conversion stall bound. A BPS patch for another ROM is refused by its
   source CRC32, and its output is checked against the target CRC32. An IPS
   patch carries no checksums, so when the ROM isn't Recognized (its SHA1 is
   in no imported DAT and its cached match is no hit) the job finishes with a
   warning rather than a refusal.
5. **The packer** is romz's ``7z a`` (``romz_service.convert``), writing a
   hidden staged archive beside the Image. It is read back and must hold the
   patched ROM exactly (the row's check is ``intact``), then placed under
   ``Name (Patched).7z`` without ever replacing a file: a taken name moves on
   to ``Name (Patched) (2).7z`` and so on.

The job is external (``ConversionMode.PATCH``): it takes no conversion slot,
and its Ledger row is written by the usual terminal listener.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from models import ConversionMode
from services.archive import ArchiveSafetyRefused, archive_service
from services.archive_members import read_archive_members
from services.job_manager import job_manager
from services.patch_engine import PatchError, apply_patch, patch_kind
from services.romz import romz_service
from services.stream_digest import new_hasher
from services.subprocess_runner import (
    ConversionCancelled,
    ReadCancelled,
    Stalled,
    bounded_path_check,
    remove_partial_output,
    remove_partial_tree,
    run_detached_within_stall_bound,
)
from services.tools import registry
from sqlalchemy.exc import MultipleResultsFound
from utils.junk import is_junk_path
from utils.path_utils import (
    lock_boundary_root,
    match_extension,
    place_without_clobbering,
    read_beneath,
    short_temp_file,
    short_temp_name,
    volume_containment,
)

from config import settings

logger = get_logger("patch_bench")

PATCH_EXTENSIONS = (".ips", ".bps")
# The archives a ROM may be patched from. RAR is listed by the browser but
# not here: the MVP takes the two the 7z packer itself writes.
ARCHIVE_EXTENSIONS = (".zip", ".7z")
UPLOAD_FOLDER = "patches"
_CHUNK = 1024 * 1024
# The most numbered names tried before giving up on placing the output.
_MAX_NUMBER = 999
# An upload's id: its own random prefix, then the name it was sent with, made
# safe. No separator can appear, so an id is always one plain file name.
_UPLOAD_ID = re.compile(r"[0-9a-f]{16}-[A-Za-z0-9 ._()\[\]-]{1,200}", re.ASCII)
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()\[\]-]", re.ASCII)


class PatchRefused(Exception):
    """The patch can't go ahead. *status* is the HTTP answer for a route."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _Cancelled(Exception):
    """The job was cancelled before it wrote anything."""


@dataclass(frozen=True)
class _Image:
    # As the person named it: a file, an archive, or ``archive::member``.
    path: str
    # The file on disk: the loose ROM or the archive.
    disk: str
    # The archive member to patch, if named.
    member: str | None

    @property
    def archived(self) -> bool:
        return self.member is not None or _archive_ext(self.disk) is not None


@dataclass(frozen=True)
class _Patch:
    # The path the Ledger names: in a Volume, or the upload's in the data dir.
    path: str
    read: Callable[[int], bytes]


_tasks: set[asyncio.Task] = set()


# ----------------------------------------------------------------------
# uploads
# ----------------------------------------------------------------------


def uploads_dir() -> Path:
    """Where an uploaded patch is kept, so its Ledger path stays valid."""
    return Path(settings.data_dir) / UPLOAD_FOLDER


def _sha1(data: bytes) -> str:
    """The SHA1 a DAT names a file by: an identity, not a security check."""
    hasher = new_hasher("sha1")
    hasher.update(data)
    return hasher.hexdigest()


def _limit(name: str) -> int:
    return max(0, int(getattr(settings, name, 0) or 0))


def _upload_id(name: str, ext: str) -> str:
    """``<16 hex>-<safe stem><ext>``: always one plain name, always ending in
    the patch's extension, whatever the upload was called."""
    base = os.path.basename(name.replace("\\", "/"))
    stem = _UNSAFE.sub("_", base[: len(base) - len(ext)])[-150:].lstrip(".") or "patch"
    return f"{secrets.token_hex(8)}-{stem}{ext.lower()}"


async def save_upload(name: str, read: Callable[[int], Awaitable[bytes]]) -> dict:
    """Keep an uploaded patch in the data directory: ``{upload_id, name, size, sha1}``.

    Read in chunks and given up at ``CHD_PATCH_MAX_PATCH_SIZE`` (413), so an
    oversized upload never lands. Refused (400) unless it is named ``.ips`` or
    ``.bps`` and its header says it is one.
    """
    ext = match_extension(name, PATCH_EXTENSIONS)
    if ext is None:
        raise PatchRefused(400, "Upload an .ips or .bps patch")
    folder = uploads_dir()
    cap = _limit("patch_max_patch_size")
    await run_in_threadpool(folder.mkdir, parents=True, exist_ok=True)
    fd, staged = await run_in_threadpool(short_temp_file, str(folder), prefix=".upload-")
    digest = new_hasher("sha1")
    head = b""
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await read(_CHUNK):
                size += len(chunk)
                if cap and size > cap:
                    raise PatchRefused(
                        413, f"The patch is over the CHD_PATCH_MAX_PATCH_SIZE limit ({cap} bytes)",
                    )
                if len(head) < 8:
                    head += chunk[:8]
                digest.update(chunk)
                await run_in_threadpool(out.write, chunk)
        try:
            patch_kind(head)
        except PatchError as exc:
            raise PatchRefused(400, str(exc)) from None
        upload_id = _upload_id(name, ext)
        await run_in_threadpool(place_without_clobbering, staged, str(folder / upload_id))
    except BaseException:
        await remove_partial_output(staged, label="patch upload")
        raise
    return {
        "upload_id": upload_id, "name": os.path.basename(name), "size": size,
        "sha1": digest.hexdigest(),
    }


# ----------------------------------------------------------------------
# starting a job
# ----------------------------------------------------------------------


def _archive_ext(path: str) -> str | None:
    return match_extension(path, ARCHIVE_EXTENSIONS)


def _refuse_disc(name: str) -> None:
    """A disc Image, by the registry's own disc extensions, can't be patched yet."""
    if match_extension(name, registry.disc_extensions()) is not None:
        raise PatchRefused(
            400,
            f"{os.path.basename(name)} is a disc Image; patching takes cartridge "
            "Images only for now",
        )


def _image(image_path: str) -> _Image:
    if "::" in image_path:
        disk, member = image_path.split("::", 1)
        if _archive_ext(disk) is None:
            raise PatchRefused(400, "Only a ROM inside a .zip or .7z can be patched")
        try:
            archive_service.member_segments(member)
        except ValueError as exc:
            raise PatchRefused(400, f"That archive member can't be read: {exc}") from None
        _refuse_disc(member)
        return _Image(image_path, disk, member)
    if archive_service.is_archive(image_path) and _archive_ext(image_path) is None:
        raise PatchRefused(400, "Only a ROM inside a .zip or .7z can be patched")
    _refuse_disc(image_path)
    return _Image(image_path, image_path, None)


async def _require_inside(path: str, what: str) -> None:
    """Fail closed: only a path the Volumes are known to hold."""
    try:
        inside = await bounded_path_check(volume_containment, path)
    except asyncio.TimeoutError:
        raise PatchRefused(503, f"{what} couldn't be checked: its Volume isn't answering") from None
    if inside is not True:
        raise PatchRefused(400, f"{what} isn't inside a Volume")


def _read_in_volume(path: str, max_bytes: int) -> bytes:
    """*path*'s bytes, read with no symlink anywhere below its Volume root.

    Checked again here, on the resolved path, so a link swapped in after the
    route's check can't take the read out of the Volumes.
    """
    real = os.path.realpath(path)
    root = lock_boundary_root(real) if volume_containment(real) is True else None
    if root is None:
        raise PatchRefused(400, f"{os.path.basename(path)} isn't inside a Volume")
    relative = os.path.relpath(real, root).replace(os.sep, "/")
    return _read_capped(lambda cap: read_beneath(str(root), relative, max_bytes=cap), max_bytes)


def _read_capped(read: Callable[[int], bytes], max_bytes: int) -> bytes:
    try:
        return read(max_bytes or (1 << 62))
    except ValueError as exc:
        raise PatchRefused(400, f"Refused: {exc}") from None


async def _patch_source(patch_path: str | None, upload_id: str | None) -> _Patch:
    if (patch_path is None) == (upload_id is None):
        raise PatchRefused(400, "Name one patch: a file in a Volume or an upload")
    if upload_id is not None:
        if not _UPLOAD_ID.fullmatch(upload_id) or match_extension(
            upload_id, PATCH_EXTENSIONS,
        ) is None:
            raise PatchRefused(400, "That isn't an uploaded patch")
        folder = os.path.realpath(uploads_dir())
        if not await run_in_threadpool(os.path.isfile, os.path.join(folder, upload_id)):
            raise PatchRefused(400, "That uploaded patch isn't here any more; upload it again")
        return _Patch(
            os.path.join(str(uploads_dir()), upload_id),
            lambda cap: _read_capped(
                lambda c: read_beneath(folder, upload_id, max_bytes=c), cap,
            ),
        )
    if "::" in patch_path or match_extension(patch_path, PATCH_EXTENSIONS) is None:
        raise PatchRefused(400, "The patch must be an .ips or .bps file")
    await _require_inside(patch_path, "The patch")
    return _Patch(patch_path, lambda cap: _read_in_volume(patch_path, cap))


async def start(
    image_path: str, *, patch_path: str | None = None, upload_id: str | None = None,
) -> dict:
    """Start patching *image_path* as a background job; ``{job_id}``.

    Only the quick checks here (the request names one Image and one patch,
    neither is a disc Image or outside the Volumes); the reads run in the job.
    """
    image = _image(image_path)
    patch = await _patch_source(patch_path, upload_id)
    await _require_inside(image.disk, "The Image")
    packer = romz_service.sevenzip_path
    if not await run_in_threadpool(shutil.which, packer):
        raise PatchRefused(
            503,
            f"The 7z packer isn't available here ({packer}), "
            "so the patched ROM can't be packed",
        )
    name = os.path.basename(image.member or image.disk)
    job = job_manager.create_external_job(
        f"Patch {name}", ConversionMode.PATCH, message="Reading the patch",
    )
    # The Ledger's Source is the Image, not the external-job placeholder. No
    # in-use check reads an external job's path.
    job.file_path = image.path
    task = asyncio.get_running_loop().create_task(_run(job.id, image, patch))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"job_id": job.id}


async def drain() -> None:
    """Wait for every running patch job (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ----------------------------------------------------------------------
# the job
# ----------------------------------------------------------------------


async def _run(job_id: str, image: _Image, patch: _Patch) -> None:
    try:
        await _patch(job_id, image, patch)
    except (_Cancelled, ReadCancelled, ConversionCancelled):
        await job_manager.finish_external_job_cancelled(
            job_id, message="Cancelled: nothing was written",
        )
    except (PatchRefused, PatchError) as exc:
        await job_manager.finish_external_job(job_id, success=False, error_message=str(exc))
    except ArchiveSafetyRefused as exc:
        await job_manager.finish_external_job(
            job_id, success=False,
            error_message=f"Refused by the archive safety rules: {exc.rule}",
        )
    except Stalled as exc:
        await job_manager.finish_external_job(job_id, success=False, error_message=str(exc))
    except Exception as exc:
        logger.warning("patch_bench: the patch job %s failed", job_id, exc_info=True)
        await job_manager.finish_external_job(
            job_id, success=False, error_message=f"The patch stopped on an error: {exc}",
        )
    else:
        await job_manager.finish_external_job(job_id, success=True)


def _stop_if_cancelled(job_id: str) -> None:
    if job_manager.is_cancelled(job_id):
        raise _Cancelled()


async def _step(job_id: str, progress: int, message: str) -> None:
    _stop_if_cancelled(job_id)
    await job_manager.update_external_job(job_id, progress=progress, message=message)


async def _detached(job_id: str, func: Callable, *args, what: str):
    return await run_detached_within_stall_bound(
        func, *args, what=what, cancel_event=job_manager.get_cancel_event(job_id),
    )


async def _patch(job_id: str, image: _Image, patch: _Patch) -> None:
    job = job_manager.get_job(job_id)
    rom_cap = _limit("patch_max_rom_size")
    patch_bytes = await _detached(
        job_id, patch.read, _limit("patch_max_patch_size"), what="reading the patch",
    )
    job.extra_inputs = [{
        "kind": "patch", "path": patch.path, "sha1": _sha1(patch_bytes),
    }]

    await _step(job_id, 10, "Reading the ROM")
    if image.archived:
        rom_name, source = await _detached(
            job_id, _read_archived, image, rom_cap, what="unpacking the ROM",
        )
    else:
        rom_name = os.path.basename(image.disk)
        source = await _detached(
            job_id, _read_in_volume, image.disk, rom_cap, what="reading the ROM",
        )
    job.input_bytes = len(source)
    job.platform = await run_in_threadpool(_platform, image.path)

    await _step(job_id, 30, "Applying the patch")
    result = await _detached(
        job_id, _apply, source, patch_bytes, rom_cap, what="applying the patch",
    )
    if not result.checked and not await run_in_threadpool(_recognized, image.path, source):
        job.warning = (
            "The ROM isn't Recognized, and an IPS patch carries no checksums, "
            "so nothing shows the patch was made for it"
        )

    await _step(job_id, 50, "Packing the patched ROM")
    stem = PurePosixPath(rom_name).stem
    patched_name = f"{stem} (Patched){PurePosixPath(rom_name).suffix}"
    final = await _pack_and_place(job_id, image, patched_name, stem, result.target)
    job.output_path = final
    job.output_size = await run_in_threadpool(os.path.getsize, final)
    job.check_passed = "intact"
    job.result = {
        "patch_kind": result.kind,
        "checksums_checked": result.checked,
        "output_path": final,
    }
    job.message = f"Wrote {os.path.basename(final)}"


def _apply(source: bytes, patch_bytes: bytes, rom_cap: int):
    return apply_patch(source, patch_bytes, max_target=rom_cap or (1 << 62))


def _read_archived(image: _Image, max_bytes: int) -> tuple[str, bytes]:
    """The ROM inside the archive: ``(member name, bytes)``.

    The whole listing is held to the archive limits before anything is
    unpacked, a symlink or encrypted member refuses it, and the member must be
    a file of known size within ``max_bytes``.
    """
    real = os.path.realpath(image.disk)
    if volume_containment(real) is not True:
        raise PatchRefused(400, f"{os.path.basename(image.disk)} isn't inside a Volume")
    members = read_archive_members(real)
    for m in members:
        if m.is_symlink:
            raise ArchiveSafetyRefused("Archive contains a symlink member", rule="a symlink member")
        if m.is_encrypted:
            raise ArchiveSafetyRefused(
                f"Archive member is encrypted: {m.name}", rule=f"an encrypted member ({m.name})",
            )
    archive_service.enforce_archive_limits([(m.name, int(m.size or 0)) for m in members])
    files = [m for m in members if not m.is_dir and not is_junk_path(m.name)]
    if image.member is None:
        if len(files) != 1:
            raise PatchRefused(
                400,
                f"{os.path.basename(image.disk)} holds "
                + ("no file" if not files else "more than one file")
                + "; open it and pick the ROM inside",
            )
        member = files[0]
        _refuse_disc(member.name)
        try:
            archive_service.member_segments(member.name)
        except ValueError as exc:
            raise PatchRefused(400, f"That archive member can't be read: {exc}") from None
    else:
        named = [m for m in files if m.name == image.member]
        if not named:
            raise PatchRefused(400, f"{image.member} isn't in {os.path.basename(image.disk)}")
        member = named[-1]
    if member.size is None:
        raise PatchRefused(400, f"{member.name}'s size isn't recorded, so its limit can't be held")
    if max_bytes and member.size > max_bytes:
        raise PatchRefused(
            400, f"{member.name} is over the CHD_PATCH_MAX_ROM_SIZE limit ({max_bytes} bytes)",
        )
    extracted, temp_dir = archive_service.extract_file(real, member.name)
    try:
        with open(extracted, "rb") as handle:
            data = handle.read(max_bytes + 1 if max_bytes else -1)
    finally:
        archive_service.cleanup_temp_dir(temp_dir)
    if max_bytes and len(data) > max_bytes:
        raise PatchRefused(400, f"{member.name} is over the CHD_PATCH_MAX_ROM_SIZE limit")
    return os.path.basename(member.name), data


def _recognized(image_path: str, data: bytes) -> bool:
    """Whether a DAT names these bytes, or the cached match for the Image is a
    hit. A store that can't be read counts as not Recognized: the answer only
    decides whether a warning is shown."""
    from services.dat_store import dat_store

    try:
        if dat_store.lookup_sha1(_sha1(data)) is not None:
            return True
    except MultipleResultsFound:
        return True  # named by more than one DAT
    except Exception:
        logger.warning("patch_bench: could not read the DAT store", exc_info=True)
        return False
    try:
        cached = dat_store.get_match(image_path)
    except Exception:
        logger.warning("patch_bench: could not read the match cache", exc_info=True)
        return False
    return bool(cached and cached.get("matched"))


def _platform(image_path: str) -> str | None:
    """The Image's Platform for the Ledger row, as shared detection sees it."""
    from services.platform_detect import detect_platform

    try:
        return detect_platform(image_path).platform
    except Exception:
        logger.debug("patch_bench: no Platform for %s", image_path, exc_info=True)
        return None


def _work_dir() -> str:
    base = settings.temp_dir or str(Path(settings.data_dir) / "temp")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="patch_", dir=base)


def _write(path: str, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)


def _read_back(archive: str, member: str, expected: bytes, work: str) -> None:
    """The packed archive must hold exactly one member, the patched ROM.

    Read whole with py7zr, the reader the app lists archives with. Not through
    the archive limits: this is the app's own output, already within
    ``CHD_PATCH_MAX_ROM_SIZE``.
    """
    import py7zr

    out = tempfile.mkdtemp(prefix="readback_", dir=work)
    try:
        with py7zr.SevenZipFile(archive, "r") as packed:
            names = packed.getnames()
            if names != [member]:
                raise PatchRefused(500, f"The packed archive holds {names}, not {member}")
            packed.extract(path=out, targets=[member])
        with open(os.path.join(out, member), "rb") as handle:
            same = handle.read(len(expected) + 1) == expected
    finally:
        shutil.rmtree(out, ignore_errors=True)
    if not same:
        raise PatchRefused(
            500, "The packed archive didn't read back as the patched ROM, so it wasn't kept",
        )


def _place(staged: str, folder: str, stem: str) -> str:
    """Move *staged* to ``<stem> (Patched).7z``, or the first free numbered
    name, never replacing a file."""
    for number in range(1, _MAX_NUMBER + 1):
        suffix = "" if number == 1 else f" ({number})"
        target = os.path.join(folder, f"{stem} (Patched){suffix}.7z")
        try:
            place_without_clobbering(staged, target)
        except FileExistsError:
            continue
        return target
    raise PatchRefused(409, f"Every name up to {stem} (Patched) ({_MAX_NUMBER}).7z is taken")


async def _pack_and_place(
    job_id: str, image: _Image, patched_name: str, stem: str, target: bytes,
) -> str:
    folder = os.path.dirname(image.disk)
    staged = os.path.join(folder, short_temp_name(".patch-", ".7z"))
    work = await run_in_threadpool(_work_dir)
    try:
        rom_file = os.path.join(work, patched_name)
        await _detached(job_id, _write, rom_file, target, what="writing the patched ROM")
        async for update in romz_service.convert(
            rom_file, staged, mode=ConversionMode.ROMZ_7Z.value,
            cancel_event=job_manager.get_cancel_event(job_id),
        ):
            progress = update.get("progress")
            if isinstance(progress, (int, float)):
                await job_manager.update_external_job(
                    job_id, progress=50 + int(progress * 0.4),
                    message=update.get("message") or "Packing the patched ROM",
                )
        await _step(job_id, 90, "Reading the archive back")
        await _detached(
            job_id, _read_back, staged, patched_name, target, work,
            what="reading the archive back",
        )
        _stop_if_cancelled(job_id)
        return await _detached(job_id, _place, staged, folder, stem, what="placing the archive")
    finally:
        await remove_partial_output(staged, label="staged patch archive")
        await remove_partial_tree(work, label="patch work directory")
