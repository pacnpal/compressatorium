"""What a Library's records are, forgotten in one call (#388).

A Library's Findings (with their dismissals), its last find-duplicates run and
its check times are all keyed by the Library's id and its Image keys. They go
together when those keys stop naming the same Images: a folder Library is
removed, or RomM is pointed at another instance or library (whose ROM ids name
other games). Both paths call :func:`forget`, so a new record kind is added
here once rather than to two lists that can drift. Blocking.
"""

from __future__ import annotations


def forget(library_id: str) -> None:
    """Drop every record kept for *library_id*'s Images. Blocking.

    The caller has already moved the Library's generation (removed it, or
    bumped it for RomM), so a run in flight writes nothing afterwards.
    """
    from services import duplicates, findings, image_checks

    findings.forget_library(library_id)
    duplicates.forget_library(library_id)
    image_checks.forget_library(library_id)
