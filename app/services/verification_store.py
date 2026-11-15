"""SQLite-backed verification store.

Records which CHD/Dolphin image files have been successfully integrity-
verified.  Public API matches the legacy JSON store 1:1.
"""

from __future__ import annotations

from logging_setup import get_logger
import os
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services import db as _db

logger = get_logger("verification_store")


# ``check_passed`` of a record whose last check failed (#305). The row stays so
# the provenance (the producing mode and tool in ``produced_meta``, the Source)
# survives the failure, but every read that means "this file passed" skips it:
# no Intact badge, no re-run fast path, no ``--since`` skip.
FAILED = "failed"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class VerificationStore:
    """Persists disc/image verification results across application restarts."""

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
                "VerificationStore: db.SessionLocal not initialized, call "
                "db.init_engine() before using the store.",
            )
        return _db.SessionLocal()

    @staticmethod
    def _normalize(path: str) -> str:
        return os.path.realpath(path)

    # ------------------------------------------------------------------

    def _mark_sync(
        self,
        chd_path: str,
        source_path: str | None,
        produced_meta: dict | None = None,
        check: str = "intact",
        keep_provenance: bool = False,
    ) -> None:
        normalized = self._normalize(chd_path)
        normalized_source = self._normalize(source_path) if source_path else None
        stmt = sqlite_insert(_db.Verification).values(
            chd_path=normalized,
            source_path=normalized_source,
            verified_at=_utcnow_iso(),
            produced_meta=produced_meta,
            check_passed=check,
        )
        updates = {
            "verified_at": stmt.excluded.verified_at,
            "check_passed": stmt.excluded.check_passed,
        }
        # A conversion's record replaces what was there, None included, so an
        # output rebuilt at the same path never inherits an old mode. A plain
        # re-check (``refresh_check``) keeps the mode that made the file
        # (which the CLI routes the next check by) and its Source (which the
        # re-run fast path reads): refreshing a check never erases provenance
        # (#306).
        if not keep_provenance:
            updates["produced_meta"] = stmt.excluded.produced_meta
            updates["source_path"] = stmt.excluded.source_path
        stmt = stmt.on_conflict_do_update(index_elements=["chd_path"], set_=updates)
        with self._session() as session:
            session.execute(stmt)
            session.commit()

    async def mark_verified(
        self,
        chd_path: str,
        *,
        source_path: str | None = None,
        produced_meta: dict | None = None,
        check: str = "intact",
    ) -> None:
        """Record that ``chd_path`` passed a check.

        ``check`` names which one (#298): ``"intact"``, its tool's container
        check (every manual Verify), or ``"round_tripped"``, its content digest
        equalled its Source's at conversion time. This is a conversion's
        record: it replaces any stored provenance, ``None`` included. A plain
        re-check uses :meth:`refresh_check`.
        """
        await run_in_threadpool(
            self._mark_sync, chd_path, source_path, produced_meta, check,
        )

    async def refresh_check(self, chd_path: str, *, check: str = "intact") -> None:
        """Record that ``chd_path`` passed a plain re-check (#306).

        Updates when and which check passed, and keeps the stored
        ``produced_meta`` and ``source_path``: refreshing a check never erases
        what made the file. A file with no record gets a plain one.
        """
        await run_in_threadpool(
            self._mark_sync, chd_path, None, None, check, True,
        )

    def _mark_failed_sync(self, chd_path: str) -> None:
        normalized = self._normalize(chd_path)
        with self._session() as session:
            row = session.get(_db.Verification, normalized)
            if row is not None:
                row.check_passed = FAILED
                session.commit()

    async def mark_failed(self, chd_path: str) -> None:
        """Record that ``chd_path`` failed a check (#305).

        It is no longer verified (:meth:`is_verified`, :meth:`get_record` and
        :meth:`all_records` skip it), but ``produced_meta`` and ``source_path``
        are kept, as :meth:`refresh_check` keeps them: a check that fails
        never erases what made the file, so the next check still goes to the
        tool that made it, and a later pass (:meth:`refresh_check`) restores
        the record. A file with no record stays without one.
        """
        await run_in_threadpool(self._mark_failed_sync, chd_path)

    def _clear_sync(self, chd_path: str) -> None:
        normalized = self._normalize(chd_path)
        with self._session() as session:
            session.execute(
                delete(_db.Verification).where(_db.Verification.chd_path == normalized)
            )
            session.commit()

    async def clear(self, chd_path: str) -> None:
        await run_in_threadpool(self._clear_sync, chd_path)

    def _move_sync(self, old_path: str, new_path: str) -> None:
        old_normalized = self._normalize(old_path)
        new_normalized = self._normalize(new_path)
        with self._session() as session:
            old = session.get(_db.Verification, old_normalized)
            if old is None:
                return
            # Preserve source_path/verified_at AND produced_meta across the
            # rename. Dropping produced_meta would leave the renamed record
            # verified but ineligible for the re-run fast path (a same-filesystem
            # rename keeps the output's inode/size/mtime, so the recorded output
            # fingerprint still matches at the new path).
            source_path = old.source_path
            verified_at = old.verified_at
            produced_meta = old.produced_meta
            check_passed = old.check_passed
            session.delete(old)
            session.flush()
            # Upsert under the new key.
            existing = session.get(_db.Verification, new_normalized)
            if existing is not None:
                existing.source_path = source_path
                existing.verified_at = verified_at
                existing.produced_meta = produced_meta
                existing.check_passed = check_passed
            else:
                session.add(_db.Verification(
                    chd_path=new_normalized,
                    source_path=source_path,
                    verified_at=verified_at,
                    produced_meta=produced_meta,
                    check_passed=check_passed,
                ))
            session.commit()

    async def move(self, old_path: str, new_path: str) -> None:
        await run_in_threadpool(self._move_sync, old_path, new_path)

    # ------------------------------------------------------------------

    def _is_verified_sync(self, chd_path: str) -> bool:
        normalized = self._normalize(chd_path)
        with self._session() as session:
            row = session.get(_db.Verification, normalized)
            return row is not None and row.check_passed != FAILED

    async def is_verified(self, chd_path: str) -> bool:
        return await run_in_threadpool(self._is_verified_sync, chd_path)

    def _get_record_sync(self, chd_path: str) -> dict[str, str | None] | None:
        normalized = self._normalize(chd_path)
        with self._session() as session:
            row = session.get(_db.Verification, normalized)
            if row is None or row.check_passed == FAILED:
                return None
            return {
                "chd_path": row.chd_path,
                "source_path": row.source_path,
                "verified_at": row.verified_at,
                "produced_meta": row.produced_meta,
                "check_passed": row.check_passed,
            }

    async def get_record(self, chd_path: str) -> dict[str, str | None] | None:
        return await run_in_threadpool(self._get_record_sync, chd_path)

    def produced_meta_blocking(self, chd_path: str) -> dict | None:
        """What made ``chd_path``, passing or not: its ``produced_meta``. Blocking.

        Kept through a failed check (:meth:`mark_failed`), so a check Rule
        still routes the file to the tool that made it (#305).
        """
        normalized = self._normalize(chd_path)
        with self._session() as session:
            row = session.get(_db.Verification, normalized)
            return row.produced_meta if row is not None else None

    async def all_provenance(self) -> list[dict[str, object]]:
        """Every row's path and ``produced_meta``, failed ones included (#306).

        For discovery and routing only: a file whose last check failed is still
        found from a folder run and goes back to the tool that made it. Reads
        that mean "this passed" (``all_records``, ``get_record``) skip a failed
        row, and ``--since`` and the re-run fast path stay on those.
        """
        return await run_in_threadpool(self._all_provenance_sync)

    def _all_provenance_sync(self) -> list[dict[str, object]]:
        with self._session() as session:
            rows = session.scalars(select(_db.Verification)).all()
            return [
                {"chd_path": r.chd_path, "produced_meta": r.produced_meta}
                for r in rows
            ]

    async def all_records(self) -> list[dict[str, str | None]]:
        return await run_in_threadpool(self._all_records_sync)

    def _all_records_sync(self) -> list[dict[str, str | None]]:
        """Synchronous fetch of every verification record (internal seam for tests)."""
        with self._session() as session:
            rows = session.scalars(
                select(_db.Verification).where(_db.Verification.check_passed != FAILED),
            ).all()
            return [
                {
                    "chd_path": r.chd_path,
                    "source_path": r.source_path,
                    "verified_at": r.verified_at,
                    "check_passed": r.check_passed,
                }
                for r in rows
            ]

    def _prune_missing_sync(self) -> int:
        # The registry's presence rule, not a bare exists(): a split output's
        # check is stored under its logical name (`Game.iso`), which is never
        # on disk while its parts are (#305). Imported here, as the registry
        # pulls in every tool.
        from services.tools import registry

        with self._session() as session:
            paths = session.scalars(select(_db.Verification.chd_path)).all()
            missing = [p for p in paths if not registry.output_exists(p)]
            if not missing:
                return 0
            # Chunk to stay under SQLite's bind-parameter limit (default 999).
            chunk_size = 900
            for i in range(0, len(missing), chunk_size):
                batch = missing[i:i + chunk_size]
                session.execute(
                    delete(_db.Verification).where(_db.Verification.chd_path.in_(batch))
                )
            session.commit()
            return len(missing)

    async def prune_missing(self) -> int:
        return await run_in_threadpool(self._prune_missing_sync)


verification_store = VerificationStore()
