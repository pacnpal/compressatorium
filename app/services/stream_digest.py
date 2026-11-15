"""Hash a decoded byte stream, with no temp file (the Round-trip check, #301).

A tool whose container a Python decoder can read (maxcso's CSO/ZSO/DAX, a WUX)
yields the decoded image in chunks; :func:`hash_stream` hashes them on a
detached thread. A tool whose decoder is a child process uses
``SubprocessRunner.hash_stdout`` instead. Either way nothing is written to disk.

The thread is detached (``run_detached``), never a pooled worker: it reads a
whole image from storage that may stop answering. And unlike a plain detached
read it is *stopped* on a cancel or an outer deadline: the producer gets a
``threading.Event`` and the loop checks it between chunks, so pressing Cancel
does not leave a thread decoding the rest of a 25 GB image unattended.
"""
from __future__ import annotations

import hashlib
import threading
import zlib
from collections.abc import Callable, Iterable, Iterator

from services.subprocess_runner import ReadCancelled, run_detached

# What a producer is: given the stop flag, yield the decoded image in order.
Producer = Callable[[threading.Event], Iterable[bytes]]

_CHUNK = 1 << 20  # 1 MiB per read


class _Crc32:
    """``hashlib``-shaped CRC32, so a CRC is one more ``algorithm`` name."""

    def __init__(self) -> None:
        self._crc = 0

    def update(self, data: bytes) -> None:
        self._crc = zlib.crc32(data, self._crc)

    def hexdigest(self) -> str:
        return f"{self._crc:08x}"


def new_hasher(algorithm: str):
    """A hasher with ``update``/``hexdigest``: ``sha1``, ``sha256`` or ``crc32``."""
    if algorithm == "crc32":
        return _Crc32()
    # SHA-1 only where the other side of a comparison is a SHA-1 the app does
    # not choose (a CHD header's data SHA-1: maxcso and cso_to_chd). It is a
    # content fingerprint, never a security check, and is declared so.
    return hashlib.new(algorithm, usedforsecurity=False)


async def hash_stream(
    produce: Producer,
    *,
    algorithm: str = "sha1",
    cancel_event=None,
) -> str:
    """Hex digest of everything ``produce`` yields, hashed on a detached thread.

    Raises :class:`ReadCancelled` when ``cancel_event`` fires. On that, on an
    outer deadline, or on any other exit the stop flag is set, so the producer
    ends at its next chunk. Exceptions the producer raises (a corrupt
    container, an I/O error) propagate.
    """
    stop = threading.Event()

    def _work() -> str:
        hasher = new_hasher(algorithm)
        for chunk in produce(stop):
            if stop.is_set():
                raise ReadCancelled("Read cancelled")
            hasher.update(chunk)
        # A producer ends early, without raising, once it sees the flag; what
        # it yielded until then is a prefix, never the image's digest.
        if stop.is_set():
            raise ReadCancelled("Read cancelled")
        return hasher.hexdigest()

    try:
        return await run_detached(_work, cancel_event=cancel_event)
    finally:
        stop.set()


def file_chunks(paths: list[str], *, offset: int = 0) -> Producer:
    """A producer reading ``paths`` end to end (``offset`` into the first)."""

    def produce(stop: threading.Event) -> Iterator[bytes]:
        skip = offset
        for path in paths:
            with open(path, "rb") as fh:
                if skip:
                    fh.seek(skip)
                    skip = 0
                while not stop.is_set():
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    yield chunk

    return produce


async def content_digest_of(
    produce: Producer,
    *,
    kind: str,
    algorithm: str = "sha1",
    cancel_event=None,
):
    """A ``ContentDigest`` of ``kind`` over what ``produce`` yields.

    The common tail of every streamed digest: a decoder's ``ValueError`` (a
    corrupt or truncated container) and an ``OSError`` become
    ``ContentDigestUnavailable``, so the Source is kept. A cancel is left to
    propagate as :class:`ReadCancelled`; the Round-trip reads the event itself.
    """
    from services.tools.base import ContentDigest, ContentDigestUnavailable

    try:
        value = await hash_stream(
            produce, algorithm=algorithm, cancel_event=cancel_event,
        )
    except (ValueError, OSError) as e:
        raise ContentDigestUnavailable(str(e)) from e
    return ContentDigest(kind=kind, value=value)


async def hash_files(
    paths: list[str], *, algorithm: str = "sha1", cancel_event=None,
) -> str:
    """Digest of ``paths`` read end to end, in the order given."""
    return await hash_stream(
        file_chunks(paths), algorithm=algorithm, cancel_event=cancel_event,
    )
