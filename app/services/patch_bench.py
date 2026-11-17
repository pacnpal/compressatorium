"""The patch bench (#428): apply an IPS or BPS patch to a cartridge Image.

A person picks one Image (loose, or a member of a ``.zip``/``.7z``, or a
``.zip``/``.7z`` holding one file) and one patch file (on a Volume, or
uploaded into ``<data_dir>/patches``). The job writes ``Name (Patched).7z``
beside the original (``Name (Patched) (2).7z`` when that is taken) and never
touches the original. Nothing here downloads a patch or runs unattended.

* **What is refused before the job starts** (:func:`check_request`): an Image
  outside the Volumes, a disc Image by the registry's own disc extensions
  (``registry.disc_extensions``), an archive holding more than one file, a
  patch that isn't ``.ips``/``.bps``, sits outside the Volumes and the upload
  folder, or is over ``CHD_PATCH_MAX_SIZE``.
* **The job** (``ConversionMode.PATCH``, an external job with a Ledger row):
  reads the patch, reads the Image (an archive member through the archive
  service's own extract, so its size limits apply), applies the patch
  (``services.patch_engine``), and packs the result with romz's 7z packer.
  Every blocking step runs on a detached thread under the stall bound
  (``run_detached_within_stall_bound``). A BPS patch for another ROM fails
  the job; an IPS patch on an Image that isn't Recognized finishes with a
  warning. The Ledger row lists the patch file and its SHA1 in
  ``extra_inputs``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from logging_setup import get_logger
from models import ConversionMode
from services import patch_engine
from services.archive import archive_service
from services.archive_members import read_archive_members
from services.concurrency_manager import concurrency_manager
from services.dat_store import dat_store
from services.job_manager import job_manager
from services.romz import romz_service
from services.subprocess_runner import (
    ConversionCancelled,
    ReadCancelled,
    remove_partial_output,
    remove_partial_tree,
    run_detached_within_stall_bound,
)
from services.tools import registry
from utils.junk import is_junk_path
from utils.path_utils import is_within_configured_volumes

from config import settings

logger = get_logger("patch_bench")

PATCH_EXTENSIONS = (".ips", ".bps")
_ARCHIVES = (".zip", ".7z")
# ponytail: the Image is read whole; 1 GiB covers every cartridge. Stream it
# if a bigger one ever needs patching.
MAX_IMAGE_BYTES = patch_engine.MAX_TARGET_BYTES

_tasks: set[asyncio.Task] = set()


class PatchRefused(Exception):
    """The patch may not start: *status* is the HTTP answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def uploads_dir() -> str:
    return os.path.join(settings.data_dir, "patches")


def too_big() -> str:
    return f"The patch is over CHD_PATCH_MAX_SIZE ({settings.patch_max_size} bytes)"


def _in_uploads(path: str) -> bool:
    root = os.path.realpath(uploads_dir())
    return os.path.realpath(path).startswith(root + os.sep)


def check_request(image_path: str, patch_path: str) -> dict:
    """The plan for one patch, or :class:`PatchRefused`. Blocking (stats)."""
    archive, member = (image_path.split("::", 1) + [None])[:2]
    if not is_within_configured_volumes(archive) or not os.path.isfile(archive):
        raise PatchRefused(404, "The Image isn't a file on a Volume")
    if member is None and archive.lower().endswith(_ARCHIVES):
        files = [
            m.name for m in read_archive_members(archive)
            if not m.is_dir and not is_junk_path(m.name)
        ]
        if len(files) != 1:
            raise PatchRefused(400, "The archive holds more than one file; pick the ROM inside it")
        member = files[0]
    if member is not None and not archive.lower().endswith(_ARCHIVES):
        raise PatchRefused(400, "Only an Image in a .zip or .7z can be patched from inside it")
    rom_name = os.path.basename(member or archive)
    if rom_name.lower().endswith(registry.disc_extensions()):
        raise PatchRefused(
            400, f"{rom_name} is a disc Image; only cartridge Images can be patched",
        )
    if not patch_path.lower().endswith(PATCH_EXTENSIONS):
        raise PatchRefused(400, "The patch must be an .ips or .bps file")
    if not (is_within_configured_volumes(patch_path, treat_archives=False)
            or _in_uploads(patch_path)) or not os.path.isfile(patch_path):
        raise PatchRefused(404, "The patch isn't a file on a Volume or an uploaded one")
    if os.path.getsize(patch_path) > settings.patch_max_size:
        raise PatchRefused(400, too_big())
    return {
        "image": image_path, "archive": archive, "member": member,
        "rom_name": rom_name, "patch": patch_path,
    }


async def start(plan: dict) -> dict:
    """Start the patch job for a checked *plan*; ``{job_id}``."""
    job = job_manager.create_external_job(
        f"Patch {plan['rom_name']}", ConversionMode.PATCH, message="Reading the patch",
    )
    task = asyncio.get_running_loop().create_task(_run(job.id, plan))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"job_id": job.id}


async def drain() -> None:
    """Wait for every running patch job (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def _read_patch(path: str) -> bytes:
    with open(path, "rb") as fh:
        data = fh.read(settings.patch_max_size + 1)
    if len(data) > settings.patch_max_size:
        raise patch_engine.PatchError(too_big())
    return data


def _read_image(plan: dict) -> bytes:
    path, temp_dir = plan["archive"], None
    if plan["member"] is not None:
        path, temp_dir = archive_service.extract_file(plan["archive"], plan["member"])
    try:
        if os.path.getsize(path) > MAX_IMAGE_BYTES:
            raise patch_engine.PatchError("The Image is over 1 GiB; too big to patch")
        return Path(path).read_bytes()
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _recognized(image: str, source: bytes) -> bool:
    """Recognized: a cached DAT/Hasheous hit, or the bytes' SHA1 in a local DAT."""
    if (dat_store.get_match(image) or {}).get("matched"):
        return True
    # A content id, not security.
    digest = hashlib.sha1(source, usedforsecurity=False)  # nosec # nosemgrep
    return dat_store.lookup_sha1(digest.hexdigest()) is not None


def _name(stem: str, n: int) -> str:
    return f"{stem} (Patched)" if n == 1 else f"{stem} (Patched) ({n})"


def _free_name(folder: str, stem: str) -> str:
    n = 1
    while os.path.lexists(os.path.join(folder, _name(stem, n) + ".7z")):
        n += 1
    return _name(stem, n)


def _workspace() -> str:
    os.makedirs(settings.temp_dir, exist_ok=True)
    return tempfile.mkdtemp(prefix="patch-", dir=settings.temp_dir)


def _write_rom(work: str, target: bytes, name: str) -> str:
    path = os.path.join(work, name)
    Path(path).write_bytes(target)
    return path


def _publish(packed: str, folder: str, stem: str, first: str) -> str:
    """Give *packed* a free ``(Patched)`` name in *folder*, never replacing a file."""
    name = first
    while True:
        final = os.path.join(folder, name + ".7z")
        try:
            os.link(packed, final)
        except FileExistsError:
            name = _free_name(folder, stem)
            continue
        except OSError:
            # A filesystem without hard links: claim the name with an exclusive
            # create, so a concurrent job can't take it, then move onto our claim.
            try:
                os.close(os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            except FileExistsError:
                name = _free_name(folder, stem)
                continue
            try:
                os.replace(packed, final)
            except OSError:
                # Don't leave the empty claim looking like a published archive.
                with contextlib.suppress(OSError):
                    os.unlink(final)
                raise
            return final
        os.unlink(packed)
        return final


async def _step(func, *args, what: str, job_id: str):
    return await run_detached_within_stall_bound(
        func, *args, what=what, cancel_event=job_manager.get_cancel_event(job_id),
    )


async def _run(job_id: str, plan: dict) -> None:
    job = job_manager.get_job(job_id)
    rom_dir = packed = None
    try:
        # A patch reads a whole ROM and runs 7z, so it takes a conversion slot
        # like any conversion and honours MAX_CONCURRENT_JOBS.
        await job_manager.update_external_job(job_id, progress=0, message="Waiting for a slot")
        if not await concurrency_manager.acquire(
            job_id, cancel_event=job_manager.get_cancel_event(job_id),
        ):
            raise ConversionCancelled()
        patch = await _step(_read_patch, plan["patch"], what="reading the patch", job_id=job_id)
        kind = patch_engine.patch_format(patch)
        if kind is None:
            raise patch_engine.PatchError("Not an IPS or BPS patch")
        digest = hashlib.sha1(patch, usedforsecurity=False)  # nosec # nosemgrep
        job.extra_inputs = [{"path": plan["patch"], "sha1": digest.hexdigest()}]
        await job_manager.update_external_job(job_id, progress=10, message="Reading the Image")
        source = await _step(_read_image, plan, what="reading the Image", job_id=job_id)
        job.input_bytes = len(source)
        if kind == "ips" and not await _step(
            _recognized, plan["image"], source, what="looking the Image up", job_id=job_id,
        ):
            job.warning = (
                "The Image isn't Recognized, so this IPS patch may not be for it "
                "(IPS has no checksum to tell)"
            )
        await job_manager.update_external_job(job_id, progress=30, message="Applying the patch")
        target = await _step(
            patch_engine.apply_patch, source, patch, what="applying the patch", job_id=job_id,
        )
        stem, ext = os.path.splitext(plan["rom_name"])
        folder = os.path.dirname(plan["archive"])
        name = await _step(_free_name, folder, stem, what="naming the output", job_id=job_id)
        # The workspace is owned before the write starts, so a write that is
        # abandoned (cancel, stall) is still removed by the finally below.
        rom_dir = await _step(_workspace, what="making a workspace", job_id=job_id)
        rom = await _step(
            _write_rom, rom_dir, target, name + ext, what="writing the ROM", job_id=job_id,
        )
        await job_manager.update_external_job(job_id, progress=60, message="Packing the 7z")
        packed = os.path.join(folder, f".{name}.{uuid.uuid4().hex[:8]}.partial.7z")
        async for _ in romz_service.convert(
            rom, packed, "romz_7z", cancel_event=job_manager.get_cancel_event(job_id),
        ):
            pass
        final = await _step(
            _publish, packed, folder, stem, name, what="naming the output", job_id=job_id,
        )
        job.output_path = final
        job.output_size = await _step(
            os.path.getsize, final, what="sizing the output", job_id=job_id,
        )
        job.message = f"Wrote {os.path.basename(final)}"
        job.file_path = plan["image"]
        await job_manager.finish_external_job(job_id, success=True)
    except (ReadCancelled, ConversionCancelled):
        job.file_path = plan["image"]
        await job_manager.finish_external_job_cancelled(
            job_id, message="Cancelled: nothing was written",
        )
    except (ValueError, OSError, RuntimeError) as exc:
        # PatchError and ArchiveSafetyRefused are ValueErrors; Stalled and
        # 7z's own failure are RuntimeErrors. Each message says what went wrong.
        job.file_path = plan["image"]
        await job_manager.finish_external_job(job_id, success=False, error_message=str(exc))
    except Exception:
        logger.warning("patch_bench: job %s failed", job_id, exc_info=True)
        job.file_path = plan["image"]
        await job_manager.finish_external_job(
            job_id, success=False, error_message="The patch stopped on an error",
        )
    finally:
        concurrency_manager.release(job_id)
        if rom_dir is not None:
            await remove_partial_tree(rom_dir, label="patched ROM")
        if packed is not None and job.output_path is None:
            await remove_partial_output(packed, label="partial patched 7z")
