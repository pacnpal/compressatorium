"""Async file-level SHA1 computation for DAT hash matching."""

from __future__ import annotations

import hashlib

from fastapi.concurrency import run_in_threadpool

_CHUNK_SIZE = 65536  # 64KB


async def compute_file_sha1(file_path: str) -> str:
    """Compute SHA1 hash of an entire file.

    Runs in a thread pool to avoid blocking the event loop.
    Returns lowercase hex digest.
    """
    return await run_in_threadpool(_sha1_sync, file_path)


def compute_file_sha1_sync(file_path: str, progress=None) -> str:
    """Blocking SHA-1 for callers that bring their own offload strategy.

    ``compute_file_sha1`` hands the work to the shared threadpool, which is the
    wrong home for a whole-file read of a path on storage that may stop
    answering (AGENTS.md: such a read must never occupy a pooled worker). A
    caller that runs this through ``run_detached`` gets the same digest on a
    throwaway thread instead. *progress*, when given, is called after each
    chunk read: a caller that runs it through ``run_detached_within_stall_bound``
    (``report_progress=True``) is stall-bounded and cancellable (#432).
    """
    return _sha1_sync(file_path, progress=progress)


def compute_files_sha1_sync(file_paths: list[str]) -> str:
    """Blocking SHA-1 of several files read end to end, in the order given.

    One digest for an image stored as several files (a cue's track files).
    Same offload rule as :func:`compute_file_sha1_sync`. SHA-1 because the
    Round-trip compares it with a CHD header's data SHA-1.
    """
    return _sha1_sync(*file_paths)


def _sha1_sync(*file_paths: str, progress=None) -> str:
    """Synchronous SHA1 computation (several files: read end to end)."""
    h = hashlib.sha1(usedforsecurity=False)  # nosec # nosemgrep: insecure-hash-algorithm-sha1  # pylint: disable=unexpected-keyword-arg
    for file_path in file_paths:
        _update_from_file(h, file_path, progress)
    return h.hexdigest()


def _update_from_file(h, file_path: str, progress=None) -> None:
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            if progress is not None:
                progress(file_path)
