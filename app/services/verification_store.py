"""SQLite-backed verification store.

Records which CHD/Dolphin image files have been successfully integrity-
verified.  Public API matches the legacy JSON store 1:1.
"""

from __future__ import annotations

from logging_setup import get_logger
import os
from collections.abc import Callable
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


# What a ``verify`` result carries when it reached no verdict on the file: it
# could not run or read it (``error``), was cut short (``cancelled``,
# ``abandoned``, ``inconclusive``), outran its bound (``timed_out``, the verify
# routes' own timeout), or would not check it here (``uncheckable``). The file
# is not known bad, so what was stored stays.
_NO_VERDICT = ("cancelled", "abandoned", "inconclusive", "timed_out", "uncheckable")


def check_verdict(result: dict) -> bool | None:
    """What a ``verify`` result says of the file: True it passed, False it is
    bad (a real verdict), None no verdict."""
    if result.get("valid"):
        return True
    if "error" in result or any(result.get(flag) for flag in _NO_VERDICT):
        return None
    return False


def cannot_check(result: dict) -> bool:
    """Whether a ``verify`` result says the file could not be checked here:
    refused by this app's own rules (``uncheckable``) or not checkable this
    time (``inconclusive``: a file the verifier could not open, a stall, a
    missing prerequisite). No verdict either way, so it is neither Intact nor
    failed (#370). The one reading for the web's batch verify and the CLI."""
    return bool(result.get("uncheckable") or result.get("inconclusive"))


async def record_check(store: "VerificationStore", path: str, result: dict) -> bool | None:
    """Record a plain re-check of *path* in *store*, as every re-check does
    (#351): a pass refreshes the record (``refresh_check``), a real verdict
    that it is bad marks it failed (``mark_failed``), so no badge, lookup or
    delete warning keeps presenting it as checked, and no verdict leaves the
    record as it was. Returns the verdict (:func:`check_verdict`)."""
    verdict = check_verdict(result)
    if verdict is True:
        await store.refresh_check(path)
    elif verdict is False:
        await store.mark_failed(path)
    return verdict


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

    def clear_blocking(self, chd_path: str) -> None:
        """:meth:`clear`, blocking, for a caller that bounds it on its own
        detached thread (a retried delete, #329)."""
        self._clear_sync(chd_path)

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

    def move_blocking(self, old_path: str, new_path: str) -> None:
        """:meth:`move`, blocking, for a caller that bounds it on its own
        detached thread (placing a staged output, #398)."""
        self._move_sync(old_path, new_path)

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

    def get_record_blocking(self, chd_path: str) -> dict[str, str | None] | None:
        """:meth:`get_record`, blocking, for a caller that bounds it on its own
        detached thread (a retried delete, #329)."""
        return self._get_record_sync(chd_path)

    def records_blocking(self, paths: list[str]) -> dict[str, dict]:
        """The passing check records of *paths*, by path, in one query. Blocking.

        Looked up by each path as given (normalized, not resolved): for a
        caller that must not touch the disk (the Findings view, #389) and whose
        paths are already the resolved ones a Library lists. A path with no
        record is left out; a failed one is kept, ``check_passed`` ``failed``,
        so a caller can tell "failed" from "never checked".
        """
        by_norm = {os.path.normpath(path): path for path in paths}
        out: dict[str, dict] = {}
        norms = list(by_norm)
        with self._session() as session:
            for start in range(0, len(norms), 900):
                rows = session.query(_db.Verification).filter(
                    _db.Verification.chd_path.in_(norms[start:start + 900]),
                ).all()
                for row in rows:
                    out[by_norm[row.chd_path]] = {
                        "verified_at": row.verified_at,
                        "produced_meta": row.produced_meta,
                        "check_passed": row.check_passed,
                    }
        return out

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

    def all_records_blocking(self) -> list[dict[str, str | None]]:
        """:meth:`all_records`, for a caller already off the event loop."""
        return self._all_records_sync()

    def _prune_missing_sync(self, progress: Callable[..., None] | None = None) -> int:
        """Drop the records whose output is confirmed gone; how many.

        The registry's presence rule, not a bare exists(): a split output's
        check is stored under its logical name (`Game.iso`), which is never
        on disk while its parts are (#305). And only a confirmed absence
        (``registry.output_missing``): a file whose check can't answer, on a
        Volume that stalls or errors, keeps its record (#373). ``progress``
        is told each record, for a stall-bounded caller; the paths are read
        and the rows deleted in separate sessions, so no session is held open
        while a Volume is asked. Blocking.
        """
        # Imported here, as the registry pulls in every tool.
        from services.tools import registry

        tick = progress or (lambda *_args: None)
        with self._session() as session:
            paths = list(session.scalars(select(_db.Verification.chd_path)).all())
        missing = []
        for path in paths:
            tick(path)
            if registry.output_missing(path):
                missing.append(path)
        tick()  # a caller that gave up stops here, before deleting anything
        if not missing:
            return 0
        with self._session() as session:
            # Chunk to stay under SQLite's bind-parameter limit (default 999).
            chunk_size = 900
            for i in range(0, len(missing), chunk_size):
                batch = missing[i:i + chunk_size]
                session.execute(
                    delete(_db.Verification).where(_db.Verification.chd_path.in_(batch))
                )
            session.commit()
        return len(missing)

    def prune_missing_blocking(self, progress: Callable[..., None] | None = None) -> int:
        """:meth:`_prune_missing_sync`, for a stall-bounded caller (#373)."""
        return self._prune_missing_sync(progress)


verification_store = VerificationStore()
