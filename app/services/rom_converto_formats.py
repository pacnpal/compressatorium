"""The PSP and 3DS containers rom-converto reads and writes (#423), read in Python.

Two jobs, both header reads of a few hundred bytes, never the whole file:

* **Is this file a source this mode takes?** (:func:`source_problem`) A PBP
  whose ``DATA.PSAR`` holds a PSP game (``NPUMDIMG``; a PS1 Classic holds
  ``PSISOIMG``, which ``psp to-iso`` doesn't convert), a CIA by its archive
  header, a CCI by its ``NCSD`` magic. The extension says which question to
  ask; the content answers it, so a renamed file isn't sent to the converter.
* **Is this output whole?** (:func:`output_problem`) The container check of a
  PBP -> ISO or CIA <-> CCI job, what Intact means for these outputs: the
  structure parses, and every byte it declares is in the file (an ISO's
  volume, a CCI's partitions, a CIA's sections). rom-converto 0.24.0 has no
  check of its own for these formats (``ctr verify`` judges signatures, which
  a converted CIA never has), so a cut-off copy is what this catches. It
  says nothing about the content; the Recognized proof does that.

Every function is blocking; callers run it off the event loop and bounded.
A read error propagates as ``OSError``.
"""
from __future__ import annotations

import os
import struct

from utils.path_utils import match_extension

PBP_EXTENSIONS = frozenset({".pbp"})
ISO_EXTENSIONS = frozenset({".iso"})
CIA_EXTENSIONS = frozenset({".cia"})
# A CCI is stored as `.cci` or, more often, `.3ds`; `ctr convert` reads both.
CCI_EXTENSIONS = frozenset({".cci", ".3ds"})

_PBP_MAGIC = b"\0PBP"
_PSP_PSAR_MAGIC = b"NPUMDIMG"
_SECTOR = 2048
_PVD_OFFSET = 16 * _SECTOR
_MEDIA_UNIT = 0x200
_NCSD_MAGIC = b"NCSD"
_CIA_HEADER_SIZE = 0x2020


def _read(path: str, offset: int, size: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(size)


def _align64(n: int) -> int:
    return (n + 63) & ~63


def _pbp_problem(path: str) -> str | None:
    head = _read(path, 0, 0x28)
    if len(head) < 0x28 or head[:4] != _PBP_MAGIC:
        return "not a PBP (no PBP header)"
    psar_offset = struct.unpack_from("<I", head, 0x24)[0]
    magic = _read(path, psar_offset, len(_PSP_PSAR_MAGIC))
    if magic != _PSP_PSAR_MAGIC:
        return (
            "not a PSP game PBP (its DATA.PSAR doesn't hold a UMD image; a PS1 "
            "Classic or an update can't become an ISO)"
        )
    return None


def _cia_layout(path: str) -> tuple[int | None, str | None]:
    """``(declared size, None)`` for a CIA, or ``(None, problem)``."""
    head = _read(path, 0, 0x20)
    if len(head) < 0x20:
        return None, "not a CIA (too short for its header)"
    header_size, _type, _version, cert, ticket, tmd, meta, content = struct.unpack(
        "<IHHIIIIQ", head,
    )
    if header_size != _CIA_HEADER_SIZE:
        return None, "not a CIA (no CIA archive header)"
    if not content:
        return None, "not a CIA (it declares no content)"
    declared = sum(_align64(n) for n in (header_size, cert, ticket, tmd, content, meta))
    return declared, None


def _cci_layout(path: str) -> tuple[int | None, str | None]:
    """``(end of the last partition, None)`` for a CCI, or ``(None, problem)``.

    The partitions, not the header's image size: a trimmed CCI (``--trim``)
    stops after its last partition, short of the card size the header names.
    """
    head = _read(path, 0, _MEDIA_UNIT)
    if len(head) < _MEDIA_UNIT or head[0x100:0x104] != _NCSD_MAGIC:
        return None, "not a CCI (no NCSD header)"
    ends = [
        (offset + size) * _MEDIA_UNIT
        for offset, size in struct.iter_unpack("<II", head[0x120:0x160])
        if size
    ]
    if not ends:
        return None, "not a CCI (it declares no partitions)"
    return max(ends), None


def _iso_layout(path: str) -> tuple[int | None, str | None]:
    """``(volume size, None)`` for an ISO 9660 image, or ``(None, problem)``."""
    pvd = _read(path, _PVD_OFFSET, _SECTOR)
    if len(pvd) < 0x84 or pvd[0] != 1 or pvd[1:6] != b"CD001":
        return None, "not an ISO 9660 image (no primary volume descriptor)"
    sectors = struct.unpack_from("<I", pvd, 0x50)[0]
    block = struct.unpack_from("<H", pvd, 0x80)[0]
    if not sectors or not block:
        return None, "not an ISO 9660 image (its volume has no size)"
    return sectors * block, None


def source_problem(path: str) -> str | None:
    """Why *path* is not a source rom-converto converts here, or None when it is.

    Chosen by extension: a ``.pbp`` must hold a PSP game, a ``.cia`` a CIA,
    a ``.cci``/``.3ds`` a CCI. Any other extension is not a PSP or 3DS source
    (the Xbox 360 ``.iso`` has its own probe).
    """
    if match_extension(path, PBP_EXTENSIONS):
        return _pbp_problem(path)
    if match_extension(path, CIA_EXTENSIONS):
        return _cia_layout(path)[1]
    if match_extension(path, CCI_EXTENSIONS):
        return _cci_layout(path)[1]
    return "not a PBP, CIA or CCI"


def output_problem(path: str) -> str | None:
    """Why the output at *path* is not Intact, or None when it is."""
    if match_extension(path, ISO_EXTENSIONS):
        declared, problem = _iso_layout(path)
        what = "volume"
    elif match_extension(path, CIA_EXTENSIONS):
        declared, problem = _cia_layout(path)
        what = "sections"
    elif match_extension(path, CCI_EXTENSIONS):
        declared, problem = _cci_layout(path)
        what = "partitions"
    else:
        return "not an ISO, CIA or CCI"
    if problem is not None:
        return problem
    size = os.path.getsize(path)
    if declared is not None and size < declared:
        return (
            f"truncated: its {what} need {declared} bytes, the file has {size}"
        )
    return None
