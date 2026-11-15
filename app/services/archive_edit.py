"""Rewrite a zip into a new archive: rename, drop or add members, crash-safe.

Shared infrastructure for tools whose product is an edited copy of a zip (the
eXoDOS repack today, the Patch bench later). The Source is only ever read.

* **Members are copied through zipfile's public API.** Each member is read
  (zipfile checks its CRC as it reaches the end) and written back with its own
  compression method and timestamp, so a corrupt Source member fails the copy
  instead of being carried into the product. The member *content* is identical;
  the compressed bytes may differ from the Source's.
* **Nothing that looks finished appears early.** The archive is written under
  a hidden temporary name in the destination directory (``.<name>.partial-*``,
  which ends in neither the product's extension nor ``.zip``), fsynced, and only
  then moved into place with :func:`utils.path_utils.place_without_clobbering`,
  an atomic no-replace move. A crash, a cancel or an error leaves no product,
  and an existing file at the destination is never replaced.
"""
from __future__ import annotations

import os
import tempfile
import time
import zipfile
from collections.abc import Callable, Mapping

from services.subprocess_runner import ConversionCancelled
from utils.path_utils import place_without_clobbering

_CHUNK = 1024 * 1024
# Methods zipfile can write; anything else is re-deflated.
_WRITABLE = frozenset({
    zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA,
})


def _keep(name: str) -> str:
    return name


def _never() -> bool:
    return False


def _plan(
    infos: list[zipfile.ZipInfo],
    rename: Callable[[str], str | None],
    add: Mapping[str, bytes],
) -> list[tuple[zipfile.ZipInfo, str]]:
    """Pair each kept member with its new name, refusing two on one name."""
    plan: list[tuple[zipfile.ZipInfo, str]] = []
    taken: dict[str, str] = {}
    for info in infos:
        new = rename(info.filename)
        if new is None or new in add:
            continue
        if new in taken:
            raise ValueError(
                f"Members {taken[new]!r} and {info.filename!r} would both "
                f"be written as {new!r}",
            )
        taken[new] = info.filename
        plan.append((info, new))
    return plan


def _copy_member(
    src: zipfile.ZipFile,
    dst: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    new_name: str,
    tick: Callable[[int], None],
    is_cancelled: Callable[[], bool],
) -> None:
    out = zipfile.ZipInfo(new_name, date_time=info.date_time)
    out.compress_type = (
        info.compress_type if info.compress_type in _WRITABLE else zipfile.ZIP_DEFLATED
    )
    out.external_attr = info.external_attr
    out.comment = info.comment
    if info.is_dir():
        dst.writestr(out, b"")
        return
    with src.open(info) as reader, dst.open(out, "w", force_zip64=True) as writer:
        while True:
            if is_cancelled():
                raise ConversionCancelled("Conversion cancelled")
            chunk = reader.read(_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
            tick(len(chunk))


def _fsync_dir(directory: str) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def rewrite_zip(
    src: str,
    dst: str,
    *,
    rename: Callable[[str], str | None] = _keep,
    add: Mapping[str, bytes] | None = None,
    is_cancelled: Callable[[], bool] = _never,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Write a copy of the zip at ``src`` to ``dst``, edited.

    ``rename`` maps each member's name to its name in the product, or to
    ``None`` to drop it. ``add`` holds members to write after the copied ones;
    an added name replaces any copied member that would have taken it. Two
    copied members renamed onto one name raise ``ValueError``.

    ``is_cancelled`` is polled between chunks and raises
    :class:`ConversionCancelled`. ``on_progress(done, total)`` reports
    uncompressed bytes copied. Blocking: run it off the event loop.

    Raises ``FileExistsError`` when ``dst`` already exists, before or at the
    moment of placement; it is never replaced.
    """
    add = dict(add or {})
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    directory = os.path.dirname(os.path.abspath(dst))
    fd, tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(dst)}.partial-", dir=directory,
    )
    try:
        with zipfile.ZipFile(src) as source:
            plan = _plan(source.infolist(), rename, add)
            total = sum(info.file_size for info, _ in plan)
            done = 0

            def tick(n: int) -> None:
                nonlocal done
                done += n
                if on_progress is not None:
                    on_progress(done, total)

            with os.fdopen(fd, "wb") as handle:
                fd = -1
                with zipfile.ZipFile(handle, "w", allowZip64=True) as product:
                    for info, new_name in plan:
                        _copy_member(source, product, info, new_name, tick, is_cancelled)
                    for name, data in add.items():
                        product.writestr(
                            zipfile.ZipInfo(name, date_time=_now()), data,
                            compress_type=zipfile.ZIP_DEFLATED,
                        )
                handle.flush()
                os.fsync(handle.fileno())
        if on_progress is not None:
            on_progress(total, total)
        place_without_clobbering(tmp, dst)
        _fsync_dir(directory)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _now() -> tuple[int, int, int, int, int, int]:
    return time.localtime()[:6]


__all__ = ["rewrite_zip"]
