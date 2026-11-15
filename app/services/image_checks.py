"""Each Image's last check time, for check Rules (#305). Blocking; call off the loop.

A check Rule picks the stalest Images first: those with no row, then the oldest
of ``checked_at`` and ``attempted_at`` (:func:`order_stamps`). A pass writes
``intact`` and a fail ``not_intact`` with a check time. An inconclusive check
writes only an attempt time (:func:`record_attempt`): it is no check time and
says nothing about the Image, but it sends the Image behind the others, so one
that never reaches a verdict cannot take every night's budget.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from services import db as _db
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

INTACT = "intact"
NOT_INTACT = "not_intact"

# Serialises a write against forgetting its Library, so a check that outlives
# its Library cannot write after that Library's rows are dropped.
_write_lock = threading.Lock()


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("image_checks: db.init_engine() has not run")
    return _db.SessionLocal()


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def last_checked(library_id: str) -> dict[str, str]:
    """``{image key: checked_at}`` for every Image of *library_id* ever checked."""
    with _session() as session:
        rows = session.query(_db.ImageCheckRecord).filter_by(library_id=library_id).all()
        return {row.image_key: row.checked_at for row in rows if row.checked_at}


def order_stamps(library_id: str) -> dict[str, str]:
    """``{image key: the later of its check and attempt times}``: what picks order on."""
    with _session() as session:
        rows = session.query(_db.ImageCheckRecord).filter_by(library_id=library_id).all()
        return {
            row.image_key: max(s for s in (row.checked_at, row.attempted_at) if s)
            for row in rows if row.checked_at or row.attempted_at
        }


def record_attempt(
    library_id: str, image_key: str, *, at: str | None = None,
    current: Callable[[], bool] | None = None,
) -> bool:
    """Store an inconclusive check of one Image: its attempt time, nothing else."""
    stamp = at or now()
    with _write_lock, _session() as session:
        if current is not None and not current():
            return False
        session.execute(
            sqlite_insert(_db.ImageCheckRecord)
            .values(library_id=library_id, image_key=image_key, attempted_at=stamp)
            .on_conflict_do_update(
                index_elements=["library_id", "image_key"],
                set_={"attempted_at": stamp},
            )
        )
        session.commit()
    return True


def record(
    library_id: str, image_key: str, result: str, *, at: str | None = None,
    current: Callable[[], bool] | None = None,
) -> bool:
    """Store a check of one Image. *current* False (its Library was removed) writes nothing."""
    stamp = at or now()
    with _write_lock, _session() as session:
        if current is not None and not current():
            return False
        session.execute(
            sqlite_insert(_db.ImageCheckRecord)
            .values(library_id=library_id, image_key=image_key, checked_at=stamp, result=result)
            .on_conflict_do_update(
                index_elements=["library_id", "image_key"],
                set_={"checked_at": stamp, "result": result},
            )
        )
        session.commit()
    return True


def forget_library(library_id: str) -> None:
    """Drop a removed Library's check times."""
    with _write_lock, _session() as session:
        session.query(_db.ImageCheckRecord).filter_by(library_id=library_id).delete()
        session.commit()
