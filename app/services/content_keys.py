"""Content keys for the duplicate finder (#388).

A content key says which disc a file holds, whatever container it is in. The
MVP has one kind, ``raw:<sha1>``: the SHA1 of the raw disc image.

* An ``.iso`` or ``.bin`` *is* the raw image, so its key is its own SHA1. That
  is also what groups exact copies of one.
* Any other file asks the tool that verifies it for :meth:`raw_image_sha1`,
  which reads the tool's labelled ``embedded_hashes``: a Dolphin container's
  rebuilt disc SHA1, a DVD or hard-disk CHD's data SHA1. A CHD's overall SHA1
  is never read (its label is not a raw image label).
* Every other format (CD CHDs, CSO/ZSO/DAX, NSZ, WUX, Z3DS, sheets, archives)
  gets no key: stored as None, so the run does not ask again, and reported as
  not checked.

A key is stored per file path with the file's size and mtime when it was read
(``content_keys``). A row whose size or mtime no longer match the file says
nothing about it: the file changed, so it is read again. Nothing here groups,
moves or deletes a file; :mod:`services.duplicates` groups.

**Bounds.** Every read is off the event loop and bounded for a slow Volume. An
image's own SHA1 is read through ``open_reporting_reads`` under
``run_detached_within_stall_bound(report_progress=True)``: a big image on a slow
but moving mount keeps going, a read that stops moving ends at the stall bound.
A tool's hash is bounded by the tool's ``verify_timeout``, as a Verify is. A
stat is bounded by the same stall bound.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from logging_setup import get_logger
from services import db as _db
from services.stream_digest import new_hasher
from services.subprocess_runner import (
    ProgressAbandoned,
    ReadCancelled,
    Stalled,
    StorageAbandoned,
    run_detached_within_stall_bound,
    until_cancelled,
)
from services.tools import registry
from services.tools.base import EmbeddedHashUnavailable
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from utils.path_utils import READ_CHUNK, match_extension, open_reporting_reads

logger = get_logger("content_keys")

# The one key kind of the MVP: the SHA1 of the raw disc image.
RAW = "raw"
# Files whose bytes are the raw image itself, so their own SHA1 is the key.
RAW_IMAGE_EXTENSIONS = (".iso", ".bin")
# How long a read may go without progress before it costs that file only.
# None: the shared stall bound (``conversion_stall_bound``), as a conversion's
# own reads use.
STALL_SECONDS: float | None = None


class KeyUnavailable(Exception):
    """The file's key could not be read this time (a stall, an I/O error, a tool
    that failed). Nothing is stored; the next run asks again."""


@dataclass(frozen=True)
class Identity:
    """What a stored key is tied to: the file's size and mtime."""

    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Stored:
    """A stored key that still describes its file. ``key`` None: no key for this format."""

    key: str | None
    container: str


def raw_key(sha1: str) -> str:
    return f"{RAW}:{sha1.strip().lower()}"


def _extensions() -> tuple[str, ...]:
    return tuple(set(registry.convertible_extensions()) | set(registry.output_extensions())
                 | set(registry.verify_extensions()))


def container_of(path: str) -> str:
    """The file's container as the app names it: its longest declared extension.

    ``Game.nkit.iso`` is ``.nkit.iso``, not ``.iso``: a compound extension a
    tool declares wins, so an NKit image is never read as a raw image.
    """
    name = os.path.basename(path)
    return (match_extension(name, _extensions()) or os.path.splitext(name)[1]).lower()


def is_raw_image(path: str) -> bool:
    return container_of(path) in RAW_IMAGE_EXTENSIONS


def key_reads_whole_content(path: str) -> bool:
    """Whether *path*'s key was computed from its whole content (#389).

    An ``.iso``/``.bin`` is hashed here, every byte; a tool's raw hash counts
    when the tool rebuilds the image to compute it
    (``raw_image_hash_rebuilds_content``). A hash read from a header does not:
    it is what the file claims, not what it decodes to. Names only, no disk.
    """
    if is_raw_image(path):
        return True
    tool = registry.tool_for_verify(path)
    return bool(tool is not None and getattr(tool, "raw_image_hash_rebuilds_content", False))


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("content_keys: db.init_engine() has not run")
    return _db.SessionLocal()


def _norm(path: str) -> str:
    return os.path.normpath(path)


def identity_of(path: str) -> Identity:
    """The file's size and mtime. Blocking; raises what ``os.stat`` raises."""
    st = os.stat(path)
    return Identity(size=int(st.st_size), mtime_ns=int(st.st_mtime_ns))


def stored(identities: dict[str, Identity]) -> dict[str, Stored]:
    """The stored keys that still describe their files, by path. Blocking.

    A row for another size or mtime is left out: the file changed since its
    key was read.
    """
    if not identities:
        return {}
    by_norm = {_norm(path): path for path in identities}
    norms = list(by_norm)
    out: dict[str, Stored] = {}
    with _session() as session:
        # Chunked to stay under SQLite's bind-parameter limit.
        for start in range(0, len(norms), 900):
            rows = session.scalars(
                select(_db.ContentKeyRecord).where(
                    _db.ContentKeyRecord.path.in_(norms[start:start + 900]),
                ),
            ).all()
            for row in rows:
                path = by_norm[row.path]
                ident = identities[path]
                if row.size == ident.size and row.mtime_ns == ident.mtime_ns:
                    out[path] = Stored(key=row.key, container=row.container)
    return out


def store(path: str, identity: Identity, key: str | None, container: str) -> None:
    """Store *path*'s key, tied to *identity*. Blocking."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    values = {
        "size": identity.size, "mtime_ns": identity.mtime_ns, "key": key,
        "container": container, "computed_at": now,
    }
    with _session() as session:
        session.execute(
            sqlite_insert(_db.ContentKeyRecord)
            .values(path=_norm(path), **values)
            .on_conflict_do_update(index_elements=["path"], set_=values),
        )
        session.commit()


def _sha1_of(path: str, *, progress: Callable[..., None]) -> str:
    """The file's SHA1, reporting every read. Blocking: run stall-bounded."""
    hasher = new_hasher("sha1")
    with open_reporting_reads(path, progress) as fh:
        while chunk := fh.read(READ_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


async def read_identity(path: str, *, cancel_event: asyncio.Event | None = None) -> Identity:
    """:func:`identity_of`, off the loop and bounded. Raises :class:`KeyUnavailable`."""
    try:
        return await run_detached_within_stall_bound(
            identity_of, path, what=f"stat {os.path.basename(path)}",
            cancel_event=cancel_event, stall=STALL_SECONDS,
        )
    except (OSError, Stalled, ProgressAbandoned) as exc:
        raise KeyUnavailable(f"could not stat {os.path.basename(path)}: {exc}") from exc


async def build(path: str, *, cancel_event: asyncio.Event | None = None) -> str | None:
    """Read *path*'s content key: ``raw:<sha1>``, or None for a format with none.

    Raises :class:`KeyUnavailable` when it could not be read this time, and
    lets ``ReadCancelled`` through for a cancel.
    """
    name = os.path.basename(path)
    if is_raw_image(path):
        try:
            sha1 = await run_detached_within_stall_bound(
                _sha1_of, path, what=f"hashing {name}", cancel_event=cancel_event,
                stall=STALL_SECONDS, report_progress=True,
            )
        except (OSError, Stalled, ProgressAbandoned) as exc:
            raise KeyUnavailable(f"could not read {name}: {exc}") from exc
        return raw_key(sha1)
    tool = registry.tool_for_verify(path)
    if tool is None:
        return None
    try:
        # Each tool call raced against the run's Cancel (`until_cancelled`): a
        # tool that waits for a lane, or a child under its own long bound,
        # ends when the run is cancelled, not when its timeout runs out.
        bound = await until_cancelled(
            tool.verify_timeout(path, cancel_event=cancel_event), cancel_event,
        )
        sha1 = await until_cancelled(
            asyncio.wait_for(
                tool.raw_image_sha1(path, cancel_event=cancel_event), timeout=bound or None,
            ),
            cancel_event,
        )
    # A cancel (`ReadCancelled`) is none of these, so it propagates as is.
    except (EmbeddedHashUnavailable, OSError, RuntimeError, asyncio.TimeoutError) as exc:
        _raise_if_cancelled(cancel_event)
        if isinstance(exc, StorageAbandoned):
            # A child outlived SIGKILL: not this file's key, the storage's
            # state. The run decides (it stops), never treated as one file.
            raise
        raise KeyUnavailable(f"{tool.id} could not hash {name}: {exc}") from exc
    # A tool that ends early on a cancel answers with nothing: no verdict.
    _raise_if_cancelled(cancel_event)
    return raw_key(sha1) if sha1 else None


def _raise_if_cancelled(cancel_event: asyncio.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ReadCancelled("Read cancelled")
