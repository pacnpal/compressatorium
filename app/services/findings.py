"""The Findings store (#304): what needs a person's decision, one row per kind and Image.

Whatever notices something (a folder Library's walk, and later RomM or the
Inbox) hands :func:`reconcile` everything it currently sees for one Library.
Each observed Finding is upserted: the first time it is stored with
``first_seen``; every later time ``last_seen`` moves. A stored Finding of the
same kinds that was not observed again is deleted, because its cause went away
(the Image moved, was mapped, or was fixed).

**Dismiss.** :func:`dismiss` records the cause's fingerprint, and the Finding
is hidden while the cause stays the same. The fingerprint is a digest of the
structured cause (the header, the folder, the mapping), not of the reason's
wording, so a new header answer brings the Finding back and a reworded message
does not.

Nothing here, nor anything that reads it, moves or deletes an Image.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from services import db as _db

PLATFORM_CONFLICT = "platform_conflict"
UNSORTED = "unsorted"
KINDS = (PLATFORM_CONFLICT, UNSORTED)

# Serialises a producer's write against forgetting its Library, so a walk that
# outlives its Library cannot write after that Library's rows are dropped.
_write_lock = threading.Lock()


@dataclass(frozen=True)
class Observed:
    """One Finding as its producer sees it right now."""

    kind: str
    image_key: str
    path: str
    reason: str
    # What the Finding is about, structured. Its digest is the fingerprint.
    cause: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        raw = json.dumps({"kind": self.kind, **self.cause}, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("findings: db.init_engine() has not run")
    return _db.SessionLocal()


def _under(key: str, prefixes: tuple[str, ...]) -> bool:
    return any(key == p or key.startswith(f"{p}/") for p in prefixes)


def reconcile(
    library_id: str, observed: Iterable[Observed], *,
    kinds: Iterable[str] = KINDS, now: str | None = None,
    keep_under: Iterable[str] = (),
    current: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Make *library_id*'s stored Findings of *kinds* match *observed*. Blocking.

    *keep_under* names the Image-key folders the producer could not look in
    this time (a subfolder that did not list). Rows under them are left as
    they are, dismissal included: not seen is not gone.

    *current* is asked under the store's write lock, just before writing:
    False (the producer's Library was removed while it worked) writes nothing.

    Returns how many were added, updated (the cause changed) and cleared.
    """
    kinds = tuple(kinds)
    unseen = tuple(p.strip("/") for p in keep_under if p.strip("/"))
    stamp = now or _now()
    seen: dict[tuple[str, str], Observed] = {}
    for item in observed:
        if item.kind in kinds:
            seen[(item.image_key, item.kind)] = item
    counts = {"added": 0, "changed": 0, "cleared": 0}
    with _write_lock, _session() as session:
        if current is not None and not current():
            return counts
        rows = session.query(_db.FindingRecord).filter(
            _db.FindingRecord.library_id == library_id,
            _db.FindingRecord.kind.in_(kinds),
        ).all()
        stored = {(row.image_key, row.kind): row for row in rows}
        for key, row in stored.items():
            item = seen.get(key)
            if item is None and _under(row.image_key, unseen):
                continue
            if item is None:
                session.delete(row)
                counts["cleared"] += 1
                continue
            fingerprint = item.fingerprint
            if row.fingerprint != fingerprint:
                counts["changed"] += 1
                row.fingerprint = fingerprint
                row.cause = dict(item.cause)
                # The cause changed, so the person decides again: a cause
                # that later changes back to the dismissed one stays shown.
                row.dismissed_fingerprint = None
            row.reason = item.reason
            row.path = item.path
            row.last_seen = stamp
        for key, item in seen.items():
            if key in stored:
                continue
            session.add(_db.FindingRecord(
                library_id=library_id, image_key=item.image_key, kind=item.kind,
                path=item.path, reason=item.reason, cause=dict(item.cause),
                fingerprint=item.fingerprint, dismissed_fingerprint=None,
                first_seen=stamp, last_seen=stamp,
            ))
            counts["added"] += 1
        session.commit()
    return counts


def _describe(row: _db.FindingRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "library_id": row.library_id,
        "image_key": row.image_key,
        "kind": row.kind,
        "path": row.path,
        "reason": row.reason,
        "cause": dict(row.cause or {}),
        "first_seen": row.first_seen,
        "last_seen": row.last_seen,
        "dismissed": row.dismissed_fingerprint == row.fingerprint,
    }


def list_findings(*, include_dismissed: bool = False) -> list[dict[str, Any]]:
    """Every stored Finding, hiding the dismissed ones unless asked. Blocking."""
    with _session() as session:
        rows = session.query(_db.FindingRecord).order_by(
            _db.FindingRecord.library_id, _db.FindingRecord.image_key, _db.FindingRecord.kind,
        ).all()
        out = [_describe(row) for row in rows]
    if include_dismissed:
        return out
    return [row for row in out if not row["dismissed"]]


def count_dismissed() -> int:
    """How many Findings are hidden by a dismiss. Blocking."""
    with _session() as session:
        return session.query(_db.FindingRecord).filter(
            _db.FindingRecord.dismissed_fingerprint == _db.FindingRecord.fingerprint,
        ).count()


def dismiss(finding_id: int) -> bool:
    """Hide a Finding until its cause changes. False when there is no such Finding."""
    with _session() as session:
        row = session.get(_db.FindingRecord, finding_id)
        if row is None:
            return False
        row.dismissed_fingerprint = row.fingerprint
        session.commit()
    return True


def undismiss(finding_id: int) -> bool:
    """Show a dismissed Finding again. False when there is no such Finding."""
    with _session() as session:
        row = session.get(_db.FindingRecord, finding_id)
        if row is None:
            return False
        row.dismissed_fingerprint = None
        session.commit()
    return True


def forget_library(library_id: str) -> None:
    """Drop a removed Library's Findings. Blocking.

    Call it after the Library stops answering ``current`` as True: the write
    lock then makes sure no walk of it writes afterwards.
    """
    with _write_lock, _session() as session:
        session.query(_db.FindingRecord).filter_by(library_id=library_id).delete()
        session.commit()
