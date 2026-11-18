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
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from services import db as _db

PLATFORM_CONFLICT = "platform_conflict"
UNSORTED = "unsorted"
# What a folder Library's walk reconciles. The walk sees every Image, so a
# Finding of these kinds it does not see again is gone.
KINDS = (PLATFORM_CONFLICT, UNSORTED)
# A check Rule found the Image is not Intact (#305). A check sees a few Images
# a night, never the whole Library, so these are written one Image at a time
# (`observe`, `resolve`) and never by `reconcile`.
NOT_INTACT = "not_intact"
# A Rule's delete step kept the Source for a reader, and every later sweep
# found one again until it gave up (#329). Written one Image at a time, like
# NOT_INTACT, and cleared once the Source is gone.
SOURCE_KEPT = "source_kept"
# Two or more Images of one Library hold the same disc (#388). One Finding per
# group, keyed by the content key (``raw:<sha1>``) in place of an Image key;
# its cause lists the copies (:data:`COPIES`). Written by a find-duplicates run
# (`services.duplicates`), which reconciles this kind alone, and settled by
# :func:`clear_gone` once fewer than two copies are left.
DUPLICATE = "duplicate"
# A RomM swap the app stopped part way that startup recovery could not finish
# (#392): the ROM, the step, where the Source and the output are, and what to
# do by hand. Owned by the RomM Library, one per journal row (image key
# ``romm-swap-<row id>``), written and cleared by
# ``services.romm.swap_recovery`` alone, which settles it once the row is.
ROMM_SWAP = "romm_swap"
# Equally ranked DATs give an Image different names, so rename to DAT name
# leaves it as it is (#425). Keyed by the Image's path; its cause lists the
# disagreeing matches (``matches``). Written one Image at a time by the rename
# preview, and cleared by ``services.dat_rename.settle_conflicts`` once the
# ranking names it (or it is no longer matched).
RENAME_CONFLICT = "rename_conflict"
# A file an Inbox could not take into its Library (#431), each kind saying
# why: its Platform couldn't be worked out, its header and folder disagree,
# or intake failed (no placement folder, a failed conversion or copy, an
# output name already taken). Owned by the Inbox's Library, keyed
# ``inbox:<id>/<path in the Inbox>``. Written one file at a time by
# ``services.inbox.intake`` and cleared when the file is taken or gone.
INBOX_UNSORTED = "inbox_unsorted"
INBOX_CONFLICT = "inbox_conflict"
INBOX_FAILED = "inbox_failed"
INBOX_KINDS = (INBOX_UNSORTED, INBOX_CONFLICT, INBOX_FAILED)
# A ``duplicate`` Finding's ``cause`` key: the copies, each ``{"image_key",
# "path", "container", "size"}``.
COPIES = "copies"

# The owner of a Finding that belongs to no Library: a hand-started job's
# part-deleted Source. No Library can have the empty id, so this owner never
# meets one. No Library's removal or RomM cleanup drops its rows, nothing asks
# whether its Library is current (there is none to be removed), and no sweep
# settles them: the Findings view does, as it loads (`clear_gone`). Dismissed
# like any other.
NO_LIBRARY = ""
NO_LIBRARY_NAME = "Started by hand"
# A Finding's ``cause`` key listing the files a started delete left behind.
# When it is there, the Finding is settled once every one of them is gone.
REMAINING = "remaining"

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


def started_delete(
    image_key: str, *, source: str | None, output: str | None, why: str,
    left: list[str],
) -> Observed:
    """The ``source_kept`` Finding for a started delete that will not be
    finished (#329): the Source is part deleted and *left* names the files
    still there, so it settles once they are gone (:func:`clear_gone`). One
    shape for every producer: a sweep giving up, a decision invalidated, a
    hand-started job. The Source is its path (where Open folder goes); the
    files left are all in ``cause[REMAINING]``."""
    left = [str(path) for path in left] or [str(source or "")]
    return Observed(
        kind=SOURCE_KEPT, image_key=str(image_key), path=str(source or left[0]),
        reason=f"Source part deleted and kept: {why}; {len(left)} of its files remain",
        cause={"source": source, "output": output, REMAINING: left},
    )


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
    counts = {"added": 0, "changed": 0, "cleared": 0, "kept": 0}
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
            counts[_upsert(session, library_id, row, item, stamp)] += 1
        for key, item in seen.items():
            if key not in stored:
                counts[_upsert(session, library_id, None, item, stamp)] += 1
        session.commit()
    counts.pop("kept", None)
    return counts


def _upsert(
    session, library_id: str, row: _db.FindingRecord | None, item: Observed, stamp: str,
) -> str:
    """Write *item* over *row* (None: a new Finding). Returns added, changed or kept."""
    if row is None:
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
        return "added"
    outcome = "kept"
    fingerprint = item.fingerprint
    if row.fingerprint != fingerprint:
        outcome = "changed"
        row.fingerprint = fingerprint
        row.cause = dict(item.cause)
        # The cause was seen to change, so the person decides again: a
        # cause that later changes back to the dismissed one stays shown.
        _dismissals(session, library_id, row.image_key, row.kind).delete()
    row.reason = item.reason
    row.path = item.path
    row.last_seen = stamp
    return outcome


def observe(
    library_id: str, item: Observed, *, current: Callable[[], bool] | None = None,
    now: str | None = None, fingerprint: str | None = None,
) -> str | None:
    """Upsert one Finding, on the same terms as :func:`reconcile`. Blocking.

    For a producer that sees one Image at a time (a check Rule). Returns
    ``added``, ``changed`` or ``kept``; None when *current* answered False.
    With *fingerprint* (the cause the caller read, #389), nothing is written
    unless the stored row still has it: a newer cause written meanwhile wins.
    """
    with _write_lock, _session() as session:
        if current is not None and not current():
            return None
        row = session.query(_db.FindingRecord).filter_by(
            library_id=library_id, image_key=item.image_key, kind=item.kind,
        ).first()
        if fingerprint is not None and (row is None or row.fingerprint != fingerprint):
            return None
        outcome = _upsert(session, library_id, row, item, now or _now())
        session.commit()
    return outcome


def resolve(
    library_id: str, image_key: str, kind: str, *,
    current: Callable[[], bool] | None = None, fingerprint: str | None = None,
) -> bool:
    """Clear one Finding: its cause went away (a later check passed). Blocking.

    Its dismissal is kept, as :func:`reconcile` keeps one for a row it drops.
    True when a row was removed. With *fingerprint*, only a row that still
    has that cause is removed (#389).
    """
    with _write_lock, _session() as session:
        if current is not None and not current():
            return False
        query = session.query(_db.FindingRecord).filter_by(
            library_id=library_id, image_key=image_key, kind=kind,
        )
        if fingerprint is not None:
            query = query.filter(_db.FindingRecord.fingerprint == fingerprint)
        removed = query.delete()
        session.commit()
    return bool(removed)


def clear_gone(
    library_id: str, kind: str, *, current: Callable[[], bool] | None = None,
) -> int:
    """Clear *library_id*'s Findings of *kind* whose file is gone. Blocking.

    For a kind no walk reconciles (``NOT_INTACT``): a converted or deleted
    Image is never listed or checked again, so its Finding would otherwise
    stay for good. Gone needs evidence the storage is there: the file is
    missing while its folder lists other entries. A share that is not mounted
    is a missing folder or an empty mount point, and clears nothing; neither
    does an Image deleted from a folder it was alone in (dismiss that one).
    A Finding whose cause lists the files a delete left (:data:`REMAINING`)
    is cleared once every one of those is gone, by the same evidence (its
    Source's first file may be the one already removed). A duplicate group
    (:data:`COPIES`) is cleared once fewer than two of its copies are left.
    Dismissals are kept, as :func:`resolve` keeps them. Returns how many were
    cleared.

    The files are looked at outside the write lock, so the delete is a
    compare-and-delete (CodeRabbit 4580360075): only a row still carrying the
    id and cause fingerprint that was looked at goes. One a producer updated
    meanwhile (a new cause, another list of files left) stays.
    """
    with _session() as session:
        rows = session.query(_db.FindingRecord).filter_by(
            library_id=library_id, kind=kind,
        ).all()
        candidates = [(row.id, row.fingerprint, row.path, dict(row.cause or {})) for row in rows]
    gone = [
        (row_id, fingerprint) for row_id, fingerprint, path, cause in candidates
        if _settled(path, cause)
    ]
    if not gone:
        return 0
    with _write_lock, _session() as session:
        if current is not None and not current():
            return 0
        cleared = 0
        for row_id, fingerprint in gone:
            cleared += session.query(_db.FindingRecord).filter(
                _db.FindingRecord.id == row_id,
                _db.FindingRecord.library_id == library_id,
                _db.FindingRecord.kind == kind,
                _db.FindingRecord.fingerprint == fingerprint,
            ).delete(synchronize_session=False)
        session.commit()
    return cleared


def _settled(path: str, cause: dict[str, Any]) -> bool:
    """Whether a Finding's files say its cause went away.

    A duplicate group (:data:`COPIES`) is settled once fewer than two of its
    copies are left; a started delete (:data:`REMAINING`) once every file it
    left is gone; anything else once its own file is gone.
    """
    copies = cause.get(COPIES)
    if copies:
        left = [c for c in copies if not _is_gone(str((c or {}).get("path") or ""))]
        return len(left) < 2
    return all(_is_gone(p) for p in list(cause.get(REMAINING) or ()) or [path])


def _is_gone(path: str) -> bool:
    """*path* is missing while its folder lists other entries."""
    return not os.path.lexists(path) and _lists_entries(os.path.dirname(path))


def _lists_entries(folder: str) -> bool:
    """Whether *folder* is a directory with anything in it."""
    try:
        with os.scandir(folder) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def stored_of(library_id: str, kind: str) -> list[dict[str, Any]]:
    """*library_id*'s stored Findings of *kind*: ``{"image_key", "cause"}``. Blocking."""
    with _session() as session:
        rows = session.query(_db.FindingRecord).filter_by(
            library_id=library_id, kind=kind,
        ).all()
        return [{"image_key": row.image_key, "cause": dict(row.cause or {})} for row in rows]


def forget_kind(library_id: str, kind: str) -> None:
    """Drop *library_id*'s Findings of *kind* and their dismissals. Blocking.

    For a Library whose Image keys now name other Images (RomM pointed at
    another instance reuses ROM ids).
    """
    with _write_lock, _session() as session:
        session.query(_db.FindingRecord).filter_by(library_id=library_id, kind=kind).delete()
        session.query(_db.FindingDismissalRecord).filter_by(
            library_id=library_id, kind=kind,
        ).delete()
        session.commit()


def forget_under(library_id: str, prefix: str, kinds: Iterable[str]) -> None:
    """Drop *library_id*'s Findings of *kinds* keyed under *prefix* (``prefix/...``),
    and their dismissals. Blocking. For a removed Inbox (#431), whose Findings
    live under its Library."""
    kinds = tuple(kinds)
    pattern = f"{prefix}/%"
    with _write_lock, _session() as session:
        for model in (_db.FindingRecord, _db.FindingDismissalRecord):
            session.query(model).filter(
                model.library_id == library_id,
                model.kind.in_(kinds),
                model.image_key.like(pattern),
            ).delete(synchronize_session=False)
        session.commit()


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


def get_finding(finding_id: int) -> dict[str, Any] | None:
    """One stored Finding as :func:`list_findings` describes it, or None. Blocking."""
    with _session() as session:
        row = session.get(_db.FindingRecord, finding_id)
        if row is None:
            return None
        return _describe(row, _dismissed_keys(session))


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
    nothing, so no dismissal is left behind for a removed Library. A
    :data:`NO_LIBRARY` Finding has no Library to be removed, so it is not asked.

    *fingerprint* is the cause the user was shown: when the row's cause is
    another one now, :class:`FindingChanged` is raised and nothing is written.
    """
    with _write_lock, _session() as session:
        row = session.get(_db.FindingRecord, finding_id)
        if row is None:
            return False
        if (
            current is not None and row.library_id != NO_LIBRARY
            and not current(row.library_id)
        ):
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
