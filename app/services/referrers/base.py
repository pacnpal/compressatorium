"""The Referrer contract (#295).

A Referrer is anything that points at an Image by its path: an ``.m3u``
playlist, later a frontend ``gamelist.xml`` or RomM's ROM. When a conversion
replaces a Source, every Referrer that points at it is moved to the output
before the Source is removed.

Three operations, all synchronous (the job hook runs them off the event loop):

- ``detect(image_path)``: cheap check whether this Referrer could point at the
  Image at all (for ``.m3u``: is there a playlist beside it).
- ``plan(renames)``: the changes a set of ``{old_path: new_path}`` renames
  needs, one ``ReferrerChange`` per file or record. Planning writes nothing,
  so the delete confirmation can show the result.
- ``apply(change)``: write one planned change atomically, keeping a one-time
  backup. Raises ``ReferrerError`` when it can't.
"""

from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from utils.path_utils import place_without_clobbering


class ReferrerError(Exception):
    """A Referrer could not be planned or applied. ``target`` names what failed."""

    def __init__(self, target: str, reason: str):
        super().__init__(f"{target}: {reason}")
        self.target = target
        self.reason = reason


@dataclass(frozen=True)
class ReferrerChange:
    """One planned rewrite of one Referrer target."""

    referrer: str
    target: str
    # (old entry, new entry) pairs, as shown in the delete confirmation.
    edits: tuple[tuple[str, str], ...]
    # Whatever the Referrer needs to apply the change; never sent to clients.
    payload: Any = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict:
        return {
            "referrer": self.referrer,
            "target": self.target,
            "edits": [{"old": old, "new": new} for old, new in self.edits],
        }


class Referrer(ABC):
    """A registry-driven Referrer plugin. ``id`` is stable; it lands in the Ledger."""

    id: str
    label: str

    @abstractmethod
    def detect(self, image_path: str) -> bool:
        """Whether this Referrer could point at the Image at *image_path*."""

    @abstractmethod
    def plan(self, renames: Mapping[str, str]) -> list[ReferrerChange]:
        """The changes *renames* (``{old_path: new_path}``) needs. Writes nothing."""

    @abstractmethod
    def apply(self, change: ReferrerChange) -> None:
        """Write *change* atomically with a one-time backup, or raise ReferrerError."""


def _keep_owner(tmp: str, original: os.stat_result) -> None:
    """Give *tmp* the replaced file's group (and owner, when running as root).

    Readers that reach a shared playlist through its group would lose access
    if the replacement took the writer's group, so a group that can't be kept
    aborts the write.
    """
    uid = original.st_uid if hasattr(os, "geteuid") and os.geteuid() == 0 else -1
    current = os.stat(tmp)
    if current.st_gid == original.st_gid and uid in (-1, current.st_uid):
        return
    try:
        os.chown(tmp, uid, original.st_gid)
    except OSError as exc:
        raise PermissionError(
            exc.errno, f"can't keep its group ({exc.strerror or exc})",
        ) from exc


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Replace *path* with *data* so a crash leaves the old file or the new one.

    Writes a temp file in the same directory, fsyncs it and renames it over
    *path*. An existing file's permission bits and group are kept (its owner
    too when running as root); a group that can't be kept aborts the write.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            original = os.stat(path)
        except FileNotFoundError:
            original = None
        if original is not None:
            _keep_owner(tmp, original)
        os.chmod(tmp, original.st_mode & 0o7777 if original is not None else 0o644)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def write_new_bytes(path: str, data: bytes) -> None:
    """Create *path* holding *data*, or raise ``FileExistsError`` if anything is there.

    The no-overwrite sibling of ``atomic_write_bytes``. It writes and fsyncs a
    temp file in the same directory, then places it with
    ``place_without_clobbering`` (a hard link or a no-replace rename), so the
    existence check and the creation are one atomic step: two writers racing
    for one name have exactly one winner, and a file or symlink already at
    *path* is never touched. The new file has the default ``0o644`` mode.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        place_without_clobbering(tmp, path)
    finally:
        with suppress(OSError):
            os.unlink(tmp)
