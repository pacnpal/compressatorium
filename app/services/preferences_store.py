"""SQLite-backed preferences store.

A generic key/value store for app preferences (UI layout widths, etc.).
Each key holds an arbitrary JSON object.  Mirrors the session-handling
pattern of the other stores so it drops into the same engine lifecycle.
"""

from __future__ import annotations

from logging_setup import get_logger
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services import db as _db

logger = get_logger("preferences_store")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PreferencesStore:
    """Persists arbitrary JSON preference blobs keyed by a string."""

    def __init__(
        self,
        store_path: str | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        if session_factory is not None:
            self._session_factory = session_factory
            self._own_engine: Engine | None = None
        elif store_path is not None:
            self._own_engine = _db.make_engine(str(store_path))
            _db.Base.metadata.create_all(self._own_engine)
            self._session_factory = sessionmaker(
                bind=self._own_engine, expire_on_commit=False, future=True,
            )
        else:
            self._own_engine = None
            self._session_factory = None

    def _session(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        if _db.SessionLocal is None:
            raise RuntimeError(
                "PreferencesStore: db.SessionLocal not initialized, call "
                "db.init_engine() before using the store.",
            )
        return _db.SessionLocal()

    # ------------------------------------------------------------------

    def _get_sync(self, key: str) -> dict | None:
        with self._session() as session:
            row = session.get(_db.Preference, key)
            if row is None or row.value is None:
                return None
            # SQLAlchemy decodes the JSON column to a fresh object per
            # query, so returning it directly is safe (no shared state)
            # and avoids coercing a non-dict blob through dict().
            return row.value

    async def get(self, key: str) -> dict | None:
        """Return the stored JSON object for *key*, or None if unset."""
        return await run_in_threadpool(self._get_sync, key)

    def _put_sync(self, key: str, value: dict) -> dict:
        stmt = sqlite_insert(_db.Preference).values(
            key=key,
            value=value,
            updated_at=_utcnow_iso(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value": stmt.excluded.value,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        with self._session() as session:
            session.execute(stmt)
            session.commit()
        return value

    async def put(self, key: str, value: dict) -> dict:
        """Upsert *value* under *key*, stamping updated_at. Returns value."""
        return await run_in_threadpool(self._put_sync, key, value)


preferences_store = PreferencesStore()
