"""Each Image's last check time, for check Rules (#305). Blocking; call off the loop.

A check Rule picks the stalest Images first: those with no row, then the oldest
``checked_at``. A pass writes ``intact`` and a fail ``not_intact``; an
inconclusive check writes nothing, so the Image stays as stale as it was.
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
        return {row.image_key: row.checked_at for row in rows}


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
