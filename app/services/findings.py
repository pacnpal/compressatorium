"""The Findings store (#304): what needs a person's decision, one row per kind and Image.

Whatever notices something (a folder Library's walk, and later RomM or the
Inbox) hands :func:`reconcile` everything it currently sees for one Library.
Each observed Finding is upserted: the first time it is stored with
``first_seen``; every later time ``last_seen`` moves. A stored Finding of the
same kinds that was not observed again is deleted, because its cause went away
(the Image moved, was mapped, or was fixed).

**Dismiss.** :func:`dismiss` records the cause's fingerprint in
``finding_dismissals``, keyed by Library, Image, kind and fingerprint, and the
Finding is hidden while the cause stays the same. The dismissal is kept apart
from the Finding row, so a row a walk drops for not seeing the Image comes
back dismissed when it returns with the same cause. The fingerprint is a digest of the
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
    # The producer could not look at the Image this time (a file it could not
    # read). Stored only when the Image has no Finding yet; one it already has
    # is kept as it is, as for a folder in `keep_under`.
    only_if_new: bool = False

    @property
    def fingerprint(self) -> str:
        raw = json.dumps({"kind": self.kind, **self.cause}, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class FindingChanged(Exception):
    """The Finding's cause is no longer the one the caller was shown."""


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
    they are, dismissal included: not seen is not gone. An ``only_if_new``
    item does the same for one Image that already has a row.

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
        # Images the producer could not look at this time that already have a
        # row: every row of theirs stays as it is, and nothing new is added.
        held = {row.image_key for row in rows} & {
            item.image_key for item in seen.values() if item.only_if_new
        }
        seen = {k: v for k, v in seen.items() if k[0] not in held}
        for key, row in stored.items():
            item = seen.get(key)
            if row.image_key in held:
                continue
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
                # The cause was seen to change, so the person decides again: a
                # cause that later changes back to the dismissed one stays shown.
                _dismissals(session, library_id, row.image_key, row.kind).delete()
            row.reason = item.reason
            row.path = item.path
            row.last_seen = stamp
        for key, item in seen.items():
            if key in stored:
                continue
            # A dismissal of another cause for this Image and kind is out of
            # date: the cause changed while the Image was out of the listing,
            # so it must not hide a later return of that old cause.
            _dismissals(session, library_id, item.image_key, item.kind).filter(
                _db.FindingDismissalRecord.fingerprint != item.fingerprint,
            ).delete(synchronize_session=False)
            session.add(_db.FindingRecord(
                library_id=library_id, image_key=item.image_key, kind=item.kind,
                path=item.path, reason=item.reason, cause=dict(item.cause),
                fingerprint=item.fingerprint,
                first_seen=stamp, last_seen=stamp,
            ))
            counts["added"] += 1
        session.commit()
    return counts


def _dismissals(session, library_id: str, image_key: str, kind: str):
    return session.query(_db.FindingDismissalRecord).filter_by(
        library_id=library_id, image_key=image_key, kind=kind,
    )


def _dismissed_keys(session) -> set[tuple[str, str, str, str]]:
    return {
        (d.library_id, d.image_key, d.kind, d.fingerprint)
        for d in session.query(_db.FindingDismissalRecord).all()
    }


def _describe(row: _db.FindingRecord, dismissed: set) -> dict[str, Any]:
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
        # Sent back with a dismiss, so it acts only on the cause the user saw.
        "fingerprint": row.fingerprint,
        "dismissed": (row.library_id, row.image_key, row.kind, row.fingerprint) in dismissed,
    }


def list_findings(*, include_dismissed: bool = False) -> list[dict[str, Any]]:
    """Every stored Finding, hiding the dismissed ones unless asked. Blocking."""
    with _session() as session:
        rows = session.query(_db.FindingRecord).order_by(
            _db.FindingRecord.library_id, _db.FindingRecord.image_key, _db.FindingRecord.kind,
        ).all()
        dismissed = _dismissed_keys(session)
        out = [_describe(row, dismissed) for row in rows]
    if include_dismissed:
        return out
    return [row for row in out if not row["dismissed"]]


def count_dismissed() -> int:
    """How many Findings are hidden by a dismiss. Blocking."""
    return sum(1 for row in list_findings(include_dismissed=True) if row["dismissed"])


def _check_shown(row: _db.FindingRecord, fingerprint: str | None) -> None:
    if fingerprint is not None and row.fingerprint != fingerprint:
        raise FindingChanged(f"Finding {row.id} changed since it was listed")


def dismiss(
    finding_id: int, *, fingerprint: str | None = None,
    current: Callable[[str], bool] | None = None,
) -> bool:
    """Hide a Finding until its cause changes. False when there is no such Finding.

    Idempotent (a double click is one dismissal), and under the write lock
    that `reconcile` and `forget_library` take. *current* is asked there with
    the Finding's Library id: False (the Library is being removed) writes
    nothing, so no dismissal is left behind for a removed Library.

    *fingerprint* is the cause the user was shown: when the row's cause is
    another one now, :class:`FindingChanged` is raised and nothing is written.
    """
    with _write_lock, _session() as session:
        row = session.get(_db.FindingRecord, finding_id)
        if row is None:
            return False
        if current is not None and not current(row.library_id):
            return False
        _check_shown(row, fingerprint)
        exists = _dismissals(session, row.library_id, row.image_key, row.kind).filter_by(
            fingerprint=row.fingerprint,
        ).first()
        if exists is None:
            session.add(_db.FindingDismissalRecord(
                library_id=row.library_id, image_key=row.image_key, kind=row.kind,
                fingerprint=row.fingerprint, dismissed_at=_now(),
            ))
        session.commit()
    return True


def undismiss(finding_id: int, *, fingerprint: str | None = None) -> bool:
    """Show a dismissed Finding again. False when there is no such Finding.

    *fingerprint* as for :func:`dismiss`.
    """
    with _write_lock, _session() as session:
        row = session.get(_db.FindingRecord, finding_id)
        if row is None:
            return False
        _check_shown(row, fingerprint)
        _dismissals(session, row.library_id, row.image_key, row.kind).delete()
        session.commit()
    return True


def forget_library(library_id: str) -> None:
    """Drop a removed Library's Findings and dismissals. Blocking.

    Call it after the Library stops answering ``current`` as True: the write
    lock then makes sure no walk of it writes afterwards.
    """
    with _write_lock, _session() as session:
        session.query(_db.FindingRecord).filter_by(library_id=library_id).delete()
        session.query(_db.FindingDismissalRecord).filter_by(library_id=library_id).delete()
        session.commit()
