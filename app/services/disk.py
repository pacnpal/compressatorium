"""Shared free-space (headroom) preflight for multi-step / large conversions.

A single-step convert holds one output at a time, so the codebase never needed
a disk check. A chain (cso -> iso -> chd) holds the source, the full
intermediate, and the partial final at once, so it can exhaust a volume mid-run.
``ensure_headroom`` is the shared seam any such job calls before it starts.

It is **multi-volume aware**: Compressatorium routinely has the per-job work dir
(intermediates) and the selected output dir (final) on different filesystems, so
requirements are grouped by mount (``st_dev``) and each mount is checked once.
The output dir may not exist yet (``SubprocessRunner`` mkdirs it later), so the
mount is resolved from the nearest existing ancestor of each target.
"""
from __future__ import annotations

import os
import shutil


class InsufficientDiskSpace(RuntimeError):
    """Raised by :func:`ensure_headroom` when a target volume lacks space."""


def _nearest_existing(path: str) -> str:
    """Walk up ``path`` until an existing directory is found.

    A target dir may not exist yet (created later by the subprocess runner);
    ``shutil.disk_usage`` / ``os.stat`` report the same filesystem for any path
    on that mount, so the nearest existing ancestor is a safe stand-in.
    """
    candidate = os.path.abspath(path)
    while True:
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return candidate  # reached the root
        candidate = parent


def ensure_headroom(
    targets: list[tuple[str, int]],
    *,
    margin_bytes: int = 0,
) -> None:
    """Verify each distinct mount can hold the bytes targeted at it.

    Parameters
    ----------
    targets:
        ``(path, required_bytes)`` pairs. ``path`` is where bytes will be
        written (work dir, output dir); it need not exist yet. Requirements
        for paths on the same mount are summed so a shared filesystem is not
        double-spent.
    margin_bytes:
        Extra free space to keep beyond the summed requirement, per mount.

    Raises
    ------
    InsufficientDiskSpace
        If any mount's free space is below ``required + margin``.
    """
    # dev -> [anchor_path, summed_required_bytes]
    by_dev: dict[int, list] = {}
    for path, required in targets:
        anchor = _nearest_existing(path)
        try:
            dev = os.stat(anchor).st_dev
        except OSError:
            # Can't stat the anchor (race/permission); skip rather than block.
            continue
        entry = by_dev.setdefault(dev, [anchor, 0])
        entry[1] += max(0, int(required))

    for anchor, required in by_dev.values():
        try:
            free = shutil.disk_usage(anchor).free
        except OSError:
            continue
        need = required + max(0, int(margin_bytes))
        if free < need:
            raise InsufficientDiskSpace(
                f"Not enough free space on the volume holding {anchor!r}: "
                f"need ~{need:,} bytes, have {free:,} bytes free"
            )
