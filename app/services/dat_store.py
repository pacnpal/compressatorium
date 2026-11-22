"""SQLite-backed DAT store + match cache.

Public API is identical to the legacy JSON-backed implementation so that
route code and tests require no changes to their call sites.  Internally
every method dispatches to a short SQLAlchemy query executed in a
thread pool (matches the existing ``run_in_threadpool`` pattern).
"""

from __future__ import annotations

from logging_setup import get_logger
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, delete, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services import db as _db
from services.dat_parser import parse_dat
from services.dat_ranking import (
    FAMILIES, RankedMatch, apply_ranking, dat_family, image_kind,
    is_unrankable_local_hit, ranked_match,
)

logger = get_logger("dat_store")

#: Transient key on a match dict: the candidate ``(sha1, match_type)`` pairs
#: the verdict was reached from. Two guards at the write boundary read it --
#: ``DATStore._local_index_now_covers`` (did a DAT import land mid-lookup?) and
#: ``DATStore._proves_content_changed`` (is this a different file now?) -- and
#: it is stripped before the row is persisted, being a check input rather than
#: part of the cached record.
#:
#: Typed rather than a bare hash list because the second guard has to compare
#: within the stored row's own hash domain, and a bare hash cannot say which
#: domain it belongs to.
CANDIDATE_HASHES_KEY = "candidate_hashes"

#: Transient key on a match dict: the local-index generation the verdict was
#: decided against (:meth:`DATStore.index_generation`), read before the match's
#: first look at the index. The write boundary refuses a *local* verdict whose
#: generation is no longer current, because a DAT change committed between the
#: decision and the write (issue #492); ``None`` means the match started while a
#: change was in flight, so it cannot be current either. Stripped before the
#: row is persisted, like :data:`CANDIDATE_HASHES_KEY`.
#:
#: A match dict without the key makes no claim about the index and is written
#: as before. Every match the routes compute carries it.
INDEX_GENERATION_KEY = "index_generation"

#: The ``preferences`` row holding the shared half of the generation: a counter
#: every local-index change moves inside its own transaction, so a change that
#: another process commits (the CLI's ``dat import``/``dat sync``) retires the
#: server's stamps too (CodeRabbit PR #527).
SHARED_GENERATION_KEY = "dat_index_generation"
_BUMP_SHARED_GENERATION = text(
    "INSERT INTO preferences (key, value, updated_at) VALUES (:key, '1', '') "
    "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
)
_READ_SHARED_GENERATION = text("SELECT value FROM preferences WHERE key = :key")


def recomputed_hash_in(match: dict, match_type: str) -> str | None:
    """The hash *match* recomputed in ``match_type``'s domain, or None.

    Module level, not a method: two callers need it and they are the two ends
    of the same rule. ``DATStore._proves_content_changed`` uses it before
    overwriting a remote hit, and ``routes.dat.drop_if_content_changed`` uses
    it before deleting a row a non-cacheable result left behind. Both are
    answering "did this file change?", and both have to compare within the
    domain the row was recorded in -- a CHD stored against its embedded
    ``chd_sha1`` versus a rescan's container ``file_sha1`` differ for a file
    nobody touched.

    The typed candidates the route attaches (:data:`CANDIDATE_HASHES_KEY`) are
    the general answer; the ``file_sha1`` fallback covers a result that carries
    only the file-level hash with no candidates alongside it.
    """
    for sha1, kind in match.get(CANDIDATE_HASHES_KEY) or ():
        if kind == match_type:
            return sha1
    if match_type == "file_sha1":
        return match.get("file_hash")
    return None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# How a DAT arrived (``dats.transport``). Only PLAIN_HTTP is set aside, by
# the Recognized delete proof (services.recognized).
MANUAL, HTTPS, PLAIN_HTTP = "manual", "https", "http"
TRANSPORTS = (MANUAL, HTTPS, PLAIN_HTTP)


def _dat_info(header: dict, entries: list[dict], transport: str) -> dict:
    """The ``dats`` row for a freshly parsed DAT (a new id, its family, and
    how it arrived)."""
    if transport not in TRANSPORTS:
        raise ValueError(f"unknown DAT transport {transport!r}")
    return {
        "transport": transport,
        "id": str(uuid.uuid4())[:8],
        "name": header.get("name", "Unknown DAT"),
        "description": header.get("description", ""),
        "version": header.get("version", ""),
        "imported_at": _utcnow_iso(),
        "file_count": len(entries),
        "family": dat_family(
            header.get("name"), header.get("description"), header.get("homepage"),
            header.get("url"), header.get("author"),
        ),
    }


def _hash_rows(entries: list[dict], dat_id: str) -> list[dict[str, Any]]:
    """One ``dat_hashes`` row per SHA1 and per MD5 of each entry."""
    rows: list[dict[str, Any]] = []
    for entry in entries:
        for hash_type in ("sha1", "md5"):
            if entry.get(hash_type):
                rows.append({
                    "hash": entry[hash_type],
                    "hash_type": hash_type,
                    "dat_id": dat_id,
                    "game_name": entry["game_name"],
                    "rom_name": entry["rom_name"],
                    "size": entry["size"],
                })
    return rows


def _import_result(dat_info: dict, added: int) -> dict:
    return {
        "id": dat_info["id"],
        "name": dat_info["name"],
        "version": dat_info["version"],
        "family": dat_info["family"],
        "file_count": dat_info["file_count"],
        "hashes_added": added,
        "message": f"Imported {dat_info['file_count']} entries from {dat_info['name']}",
    }


def _remote_hit_clause():
    """SQL for "this row is a remote (Hasheous) hit"."""
    return and_(
        _db.DATMatch.matched.is_(True),
        func.coalesce(_db.DATMatch.payload["source"].as_string(), "") == "hasheous",
    )


class DATStore:
    """Persists imported DAT files and hash lookup index."""

    def __init__(
        self,
        store_path: str | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        """Construct a store.

        * ``session_factory``, if provided, use this ``sessionmaker``
          directly.  Preferred in tests that build an isolated engine.
        * ``store_path``, legacy constructor argument.  If given,
          treat it as a SQLite database file and build a private
          engine for this store (isolated from the module-level DB).
          This preserves the test fixture pattern
          ``DATStore(store_path=tmp_path / "dat_store.json")``, the
          path is still unique-per-test, it just happens to be a
          SQLite file now.
        * Neither, use the process-wide ``db.SessionLocal``.
        """
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
            self._session_factory = None  # resolved lazily from db.SessionLocal
        # Staging area for import_dat_no_persist(), committed atomically by persist().
        self._pending_imports: list[tuple[dict, list[dict[str, Any]]]] = []
        # Lock serialises concurrent append (import_dat_no_persist) / read+clear
        # (_persist_sync) / clear (discard_pending) calls from different threads.
        self._pending_lock = threading.Lock()
        # Local-index generation (see index_generation): this process's
        # in-memory half, which also marks a change in flight. The shared
        # half lives in the database (SHARED_GENERATION_KEY).
        self._index_lock = threading.Lock()
        self._index_generation = 0
        self._index_changes_in_flight = 0

    # ------------------------------------------------------------------
    # Local-index generation (issue #492)
    # ------------------------------------------------------------------

    def index_generation(self, session: Session | None = None) -> int | None:
        """Which version of the local DAT index is committed, or ``None``.

        ``None`` while a change to the index (an import, a sync's persist, a
        DAT delete) is in flight: a match that starts then cannot tell whether
        it read the index before or after that change commits. A match stamps
        the value it reads before its first lookup (:data:`INDEX_GENERATION_KEY`)
        and the write boundary compares it with the current one.

        The value is this process's counter plus the shared one in the
        database (:data:`SHARED_GENERATION_KEY`). Both only grow, so the sum
        moves whenever either does: a change committed by another process,
        which this process's counter never sees, still retires every stamp
        read before it. Reads the database (on *session* when given), so call
        it off the event loop.
        """
        with self._index_lock:
            if self._index_changes_in_flight:
                return None
            local = self._index_generation
        if session is not None:
            return local + self._shared_generation(session)
        with self._session() as own:
            return local + self._shared_generation(own)

    @staticmethod
    def _shared_generation(session) -> int:
        value = session.execute(
            _READ_SHARED_GENERATION, {"key": SHARED_GENERATION_KEY},
        ).scalar()
        return int(value) if value is not None else 0

    @staticmethod
    def _bump_shared_generation(session) -> None:
        """Move the shared generation, in the transaction changing the index."""
        session.execute(_BUMP_SHARED_GENERATION, {"key": SHARED_GENERATION_KEY})

    @contextmanager
    def _changing_local_index(self) -> Iterator[None]:
        """Bracket a transaction that changes the local DAT index.

        The generation moves on entry *and* on exit, and reads as ``None`` in
        between. Moving on entry is what makes the write boundary's re-read
        sound: a writer holding SQLite's write lock either sees an unchanged
        generation (so no change has committed since it decided, and none can
        until it commits) or a different one. Moving on exit retires the
        generation a match could have read while the change was in flight.
        The exit runs even if the transaction fails, so a failed import cannot
        leave every later write refused.
        """
        with self._index_lock:
            self._index_generation += 1
            self._index_changes_in_flight += 1
        try:
            yield
        finally:
            with self._index_lock:
                self._index_generation += 1
                self._index_changes_in_flight -= 1

    # ------------------------------------------------------------------
    # Session plumbing
    # ------------------------------------------------------------------

    def _session(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        if _db.SessionLocal is None:
            raise RuntimeError(
                "DATStore: db.SessionLocal not initialized, call "
                "db.init_engine() before using the store.",
            )
        return _db.SessionLocal()

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def _import_dat_sync(
        self, source: str, replace: bool = False, transport: str = MANUAL,
    ) -> tuple[dict, list[str]]:
        """Parse *source* and insert all entries into the DB in one transaction.

        With *replace*, an earlier import of the same DAT (same name and
        family: yesterday's No-Intro pack, last month's Redump zip) goes in
        the same transaction, so re-importing a pack never doubles it (#524).

        Returns the import result and the paths the invalidation dropped.
        """
        header, entries = parse_dat(source)
        dat_info = _dat_info(header, entries, transport)
        hash_rows = _hash_rows(entries, dat_info["id"])
        replaced: list[str] = []
        with self._changing_local_index(), self._session() as session:
            self._insert_dat(session, dat_info, hash_rows)
            if replace:
                old = list(session.scalars(select(_db.DAT.id).where(
                    _db.DAT.name == dat_info["name"],
                    _db.DAT.family == dat_info["family"],
                    _db.DAT.id != dat_info["id"],
                )).all())
                if old:
                    replaced = self._delete_matches_citing(session, old)
                    session.execute(delete(_db.DAT).where(_db.DAT.id.in_(old)))
            # Importing a new DAT invalidates the match cache (a
            # previously-"unmatched" file may now match, or a previously
            # matched file may now match against a different DAT) -- except
            # for remote hits, which the local DATs did not produce.
            dropped = self._invalidate_dat_derived_matches(session)
            self._bump_shared_generation(session)
            session.commit()
        return _import_result(dat_info, len(hash_rows)), [*replaced, *dropped]

    @staticmethod
    def _insert_dat(session, dat_info: dict, hash_rows: list[dict[str, Any]]) -> None:
        """Add one DAT and its hashes. Shared by both import paths.

        A DAT re-imported under the same name and family (a newer version, or
        the MAMERedump sync, which replaces every DAT with a fresh id) takes
        the rank of the one it replaces, so the user's order survives it.
        """
        rank = session.scalars(
            select(_db.DAT.rank).where(
                _db.DAT.name == dat_info["name"],
                _db.DAT.family == dat_info["family"],
                _db.DAT.rank.is_not(None),
            ).order_by(_db.DAT.imported_at.desc()).limit(1)
        ).first()
        session.add(_db.DAT(
            id=dat_info["id"],
            name=dat_info["name"],
            description=dat_info["description"],
            version=dat_info["version"],
            imported_at=dat_info["imported_at"],
            file_count=dat_info["file_count"],
            family=dat_info["family"],
            rank=rank,
            transport=dat_info["transport"],
        ))
        # The multi-row insert bypasses the unit-of-work, so flush the DAT row
        # first or the FK from dat_hashes will fail.
        session.flush()
        # Unique per DAT (#424): another DAT knowing the same hash keeps its
        # own row. A DAT listing one hash twice keeps its last entry, as before.
        chunk_size = 900
        for i in range(0, len(hash_rows), chunk_size):
            stmt = sqlite_insert(_db.DATHash).values(hash_rows[i:i + chunk_size])
            stmt = stmt.on_conflict_do_update(
                index_elements=["hash", "hash_type", "dat_id"],
                set_={
                    "game_name": stmt.excluded.game_name,
                    "rom_name": stmt.excluded.rom_name,
                    "size": stmt.excluded.size,
                },
            )
            session.execute(stmt)
        # The DAT's file list (#426), apart from the hash index: a file
        # whose bytes another file shares keeps its own row here.
        files = {
            (row["game_name"], row["rom_name"]): row["size"] for row in hash_rows
        }
        file_rows = [
            {"dat_id": dat_info["id"], "game_name": game, "rom_name": rom, "size": size}
            for (game, rom), size in files.items()
        ]
        for i in range(0, len(file_rows), chunk_size):
            session.execute(
                sqlite_insert(_db.DATFile).values(file_rows[i:i + chunk_size])
                .on_conflict_do_nothing(),
            )

    async def import_dat(
        self, source: str, *, invalidated: set[str] | None = None, replace: bool = False,
        transport: str = MANUAL,
    ) -> dict:
        """Parse and import a DAT file (path or XML string).

        *transport* records how it arrived (:data:`TRANSPORTS`): a hand import
        is ``manual``; a download says ``https`` or ``http``.

        *invalidated*, when given, receives the paths whose cached verdicts the
        import's invalidation removed, read inside the import's own
        transaction. Those are exactly the files the import owes a rematch
        that the remote hits it preserved do not cover (issue #492): a
        snapshot taken before the import misses a row written between the
        snapshot and the commit.
        """
        result, dropped = await run_in_threadpool(
            self._import_dat_sync, source, replace, transport,
        )
        if invalidated is not None:
            invalidated.update(dropped)
        return result

    async def import_dat_no_persist(self, source: str, *, transport: str = MANUAL) -> dict:
        """Stage a DAT in memory; call :meth:`persist` to atomically commit all staged DATs."""
        def _stage() -> tuple[dict, dict]:
            header, entries = parse_dat(source)
            dat_info = _dat_info(header, entries, transport)
            hash_rows = _hash_rows(entries, dat_info["id"])
            with self._pending_lock:
                self._pending_imports.append((dat_info, hash_rows))
            return dat_info, _import_result(dat_info, len(hash_rows))

        _info, result = await run_in_threadpool(_stage)
        return result

    def _persist_sync(self) -> list[str]:
        """Commit all staged DATs in a single transaction and clear the staging area.

        Returns the paths the invalidation dropped.
        """
        with self._pending_lock:
            pending = list(self._pending_imports)
            self._pending_imports = []
        if not pending:
            return []
        with self._changing_local_index(), self._session() as session:
            for dat_info, hash_rows in pending:
                self._insert_dat(session, dat_info, hash_rows)
            dropped = self._invalidate_dat_derived_matches(session)
            self._bump_shared_generation(session)
            session.commit()
        return dropped

    async def persist(self, *, invalidated: set[str] | None = None) -> None:
        """Commit all DATs staged by :meth:`import_dat_no_persist` in a single transaction.

        *invalidated* works as in :meth:`import_dat`.
        """
        dropped = await run_in_threadpool(self._persist_sync)
        if invalidated is not None:
            invalidated.update(dropped)

    async def discard_pending(self) -> None:
        """Discard all staged DATs without writing them to the database."""
        with self._pending_lock:
            self._pending_imports = []

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    @staticmethod
    def _delete_matches_citing(session, dat_ids: list[str]) -> list[str]:
        """Delete the cached matches that cite any of *dat_ids*; their paths.

        Listed and deleted in one transaction, like
        :meth:`_invalidate_dat_derived_matches`: the files lose a verdict here,
        and another DAT may still know them, so the caller owes them a rematch
        (CodeRabbit on PR #493). A verdict citing an old DAT can be written
        after a sync's persist and before its old DATs are deleted.
        """
        cites = _db.DATMatch.dat_id.in_(dat_ids)
        dropped = list(session.scalars(select(_db.DATMatch.path).where(cites)).all())
        session.execute(delete(_db.DATMatch).where(cites))
        return dropped

    def _delete_dat_sync(self, dat_id: str) -> tuple[bool, list[str]]:
        # A local hit decided against this DAT and written after the delete
        # would name a DAT that is gone, so a delete moves the generation too.
        with self._changing_local_index(), self._session() as session:
            row = session.get(_db.DAT, dat_id)
            if row is None:
                return False, []
            # Matches don't cascade (FK is SET NULL), drop them
            # explicitly so the behaviour matches the legacy store.
            dropped = self._delete_matches_citing(session, [dat_id])
            # Hashes cascade via FK ON DELETE CASCADE.
            session.delete(row)
            self._bump_shared_generation(session)
            session.commit()
        return True, dropped

    def _upgrade_to_https_sync(self, dat_ids: list[str]) -> int:
        with self._session() as session:
            result = session.execute(
                _db.DAT.__table__.update()
                .where(_db.DAT.id.in_(dat_ids), _db.DAT.transport == PLAIN_HTTP)
                .values(transport=HTTPS)
            )
            session.commit()
            return result.rowcount or 0

    async def upgrade_to_https(self, dat_ids: list[str]) -> int:
        """Mark the plain-HTTP DATs among *dat_ids* as fetched over HTTPS, once
        the same bytes came again over HTTPS. Never touches
        an ``https``, ``manual`` or unrecorded row. Returns how many changed."""
        if not dat_ids:
            return 0
        return await run_in_threadpool(self._upgrade_to_https_sync, list(dat_ids))

    async def delete_dat(self, dat_id: str, *, invalidated: set[str] | None = None) -> bool:
        """Delete one DAT. *invalidated* works as in :meth:`import_dat`: it
        receives the paths whose cached matches cited the DAT."""
        deleted, dropped = await run_in_threadpool(self._delete_dat_sync, dat_id)
        if invalidated is not None:
            invalidated.update(dropped)
        return deleted

    def _delete_dats_bulk_sync(self, dat_ids: list[str]) -> tuple[int, list[str]]:
        if not dat_ids:
            return 0, []
        # Chunk to stay under SQLite's bind-parameter limit (default 999).
        chunk_size = 900
        with self._changing_local_index(), self._session() as session:
            existing: list[str] = []
            for i in range(0, len(dat_ids), chunk_size):
                chunk = dat_ids[i:i + chunk_size]
                existing.extend(session.scalars(
                    select(_db.DAT.id).where(_db.DAT.id.in_(chunk))
                ).all())
            if not existing:
                return 0, []
            dropped: list[str] = []
            for i in range(0, len(existing), chunk_size):
                chunk = existing[i:i + chunk_size]
                dropped.extend(self._delete_matches_citing(session, chunk))
                session.execute(delete(_db.DAT).where(_db.DAT.id.in_(chunk)))
            self._bump_shared_generation(session)
            session.commit()
            return len(existing), dropped

    async def delete_dats_bulk(
        self, dat_ids: list[str], *, invalidated: set[str] | None = None,
    ) -> int:
        """Delete DATs by id; how many existed. *invalidated* works as in
        :meth:`delete_dat`."""
        deleted, dropped = await run_in_threadpool(
            self._delete_dats_bulk_sync, list(dat_ids),
        )
        if invalidated is not None:
            invalidated.update(dropped)
        return deleted

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    @staticmethod
    def _dat_dict(r) -> dict:
        return {
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "version": r.version,
            "imported_at": r.imported_at,
            "file_count": r.file_count,
            "family": r.family,
            "rank": r.rank,
            "transport": r.transport,
        }

    def list_dats(self) -> list[dict]:
        with self._session() as session:
            rows = session.scalars(select(_db.DAT)).all()
            return [self._dat_dict(r) for r in rows]

    def get_dat_name(self, dat_id: str) -> str:
        with self._session() as session:
            row = session.get(_db.DAT, dat_id)
            return row.name if row is not None else "Unknown"

    def _lookup_hash(self, hex_hash: str, hash_type: str) -> dict | None:
        """Every DAT's record for *hex_hash*, or None when no DAT knows it.

        The top-level fields are the oldest import's (a stable order, not a
        ranking: :meth:`with_current_ranking` decides which DAT names a file);
        ``matches`` lists every DAT's record, each with its DAT's name, family
        and ``transport`` (read in the same query, so the Recognized proof sees
        how each matching DAT arrived).
        """
        with self._session() as session:
            rows = session.execute(
                select(_db.DATHash, _db.DAT.name, _db.DAT.family, _db.DAT.transport)
                .join(_db.DAT, _db.DAT.id == _db.DATHash.dat_id)
                .where(
                    _db.DATHash.hash == hex_hash.lower(),
                    _db.DATHash.hash_type == hash_type,
                )
                .order_by(_db.DAT.imported_at, _db.DAT.id)
            ).all()
            if not rows:
                return None
            matches = [
                {
                    "dat_id": row.dat_id,
                    "dat_name": dat_name,
                    "family": family,
                    "game_name": row.game_name,
                    "rom_name": row.rom_name,
                    "size": row.size,
                    "transport": transport,
                }
                for row, dat_name, family, transport in rows
            ]
            return {**matches[0], "matches": matches}

    def game_files(self, dat_id: str, game_name: str) -> list[dict]:
        """Every file the DAT *dat_id* lists for *game_name*: ``{rom_name,
        size}``, once per name, in name order. What an Image rename (#426)
        names a sheet's tracks after.

        Read from the DAT's file list (``dat_files``),
        which keeps a file whose bytes another file shares. A DAT imported
        before that list existed has none until it is imported again: its
        hash index is read instead, which may miss such a file, so a caller
        compares the count with what it has. Only files the DAT gives a hash
        for are listed."""
        with self._session() as session:
            rows = session.execute(
                select(_db.DATFile.rom_name, _db.DATFile.size)
                .where(_db.DATFile.dat_id == dat_id, _db.DATFile.game_name == game_name)
            ).all()
            listed = rows or session.execute(
                select(_db.DATFile.rom_name).where(_db.DATFile.dat_id == dat_id).limit(1)
            ).all()
            if not listed:
                rows = session.execute(
                    select(_db.DATHash.rom_name, _db.DATHash.size)
                    .where(_db.DATHash.dat_id == dat_id, _db.DATHash.game_name == game_name)
                    .distinct()
                ).all()
        files: dict[str, int] = {}
        for rom_name, size in rows:
            files.setdefault(rom_name, size)
        return [{"rom_name": name, "size": files[name]} for name in sorted(files)]

    def lookup_sha1(self, sha1: str) -> dict | None:
        return self._lookup_hash(sha1, "sha1")

    # ------------------------------------------------------------------
    # Ranking (#424)
    # ------------------------------------------------------------------

    @staticmethod
    def _ranks(session) -> dict[str, tuple[int | None, str]]:
        """``{dat_id: (rank, family)}`` for every existing DAT."""
        return {
            dat_id: (rank, family)
            for dat_id, rank, family in session.execute(
                select(_db.DAT.id, _db.DAT.rank, _db.DAT.family)
            ).all()
        }

    @staticmethod
    def _ranked(payload: dict, path: str, ranks) -> dict | None:
        if is_unrankable_local_hit(payload):
            return None  # cached before #424: read as uncached, match it again
        if not payload.get("matches"):
            return payload  # nothing to rank; skip the registry lookup
        return apply_ranking(payload, kind=image_kind(path), ranks=ranks)

    def with_current_ranking(self, payload: dict, path: str) -> dict | None:
        """*payload* (a match result for *path*) ranked by the current order.

        Applied when a result is computed and again whenever a cached one is
        read, so reordering DATs takes effect without re-hashing anything.
        ``None`` when every DAT the payload names is gone: there is no local
        hit any more.
        """
        if not payload.get("matches"):
            return payload  # nothing to rank (a miss, a remote hit)
        with self._session() as session:
            return self._ranked(payload, path, self._ranks(session))

    def ranked_match_for_image(self, path: str) -> RankedMatch:
        """Which DAT names the Image at *path*: the top-ranked match, or a conflict.

        The one shared answer to "what is this Image called?" (rename to DAT
        name, the Inbox, the RomM view). Reads the match the app already has
        for *path* and never hashes: ``top`` is ``None`` both when there is no
        match and when equally ranked DATs disagree, which ``conflict`` then
        lists. A Hasheous hit is a single match named by its own record.
        Blocking; call it off the event loop.
        """
        return ranked_match(self.get_match(path))

    def get_ranking(self) -> dict:
        """The DATs in ranked order, and whether the user has set the order.

        Ranked DATs come first in the user's order; any DAT they have not
        placed follows, by family (Redump, No-Intro, TOSEC, MAME, other) and
        then name. Until an order is saved the family defaults apply, which
        put No-Intro first for cartridge Images.
        """
        with self._session() as session:
            rows = session.scalars(select(_db.DAT)).all()
            dats = [self._dat_dict(r) for r in rows]
        family_order = {family: i for i, family in enumerate(FAMILIES)}
        dats.sort(key=lambda d: (
            d["rank"] is None,
            d["rank"] if d["rank"] is not None else 0,
            family_order.get(d["family"], len(FAMILIES)),
            d["name"].lower(),
        ))
        return {"custom": any(d["rank"] is not None for d in dats), "dats": dats}

    def _set_ranking_sync(self, order: list[str]) -> dict:
        if len(set(order)) != len(order):
            raise ValueError("A DAT is listed more than once")
        with self._session() as session:
            known = set(session.scalars(select(_db.DAT.id)).all())
            unknown = [dat_id for dat_id in order if dat_id not in known]
            if unknown:
                raise ValueError(f"Unknown DAT id(s): {', '.join(unknown)}")
            position = {dat_id: i for i, dat_id in enumerate(order)}
            for row in session.scalars(select(_db.DAT)).all():
                row.rank = position.get(row.id)
            session.commit()
        return self.get_ranking()

    async def set_ranking(self, order: list[str]) -> dict:
        """Save the user's order (DAT ids, first wins); unlisted DATs are unranked.

        Raises ``ValueError`` for an unknown or repeated id, saving nothing.
        """
        return await run_in_threadpool(self._set_ranking_sync, list(order))

    async def reset_ranking(self) -> dict:
        """Forget the user's order, so the family defaults apply again."""
        return await run_in_threadpool(self._set_ranking_sync, [])

    # ------------------------------------------------------------------
    # Match cache
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(path: str) -> str:
        return os.path.normpath(path)

    def get_match(self, file_path: str) -> dict | None:
        normalized = self._normalize(file_path)
        with self._session() as session:
            row = session.get(_db.DATMatch, normalized)
            if row is None:
                return None
            return self._ranked(dict(row.payload), normalized, self._ranks(session))

    @staticmethod
    def _would_downgrade_remote_hit(existing, match: dict) -> bool:
        """True when writing *match* would replace a remote hit with a non-answer.

        "Non-answer" is the operative word: an unmatched result only supersedes
        a remote hit if it actually knows better. That means either the remote
        source was consulted this time (``checked_remote``), or the recompute
        can *prove* the file changed underneath the cached row.

        The proof compares like with like. A CHD hit is stored against its
        embedded hash (``chd_sha1``), while a rescan recomputes the container
        ``file_sha1`` -- different domains that differ for an unchanged file,
        so only a stored ``file_sha1`` match can be compared. Without this the
        guard kept a stale badge on a replaced file forever, since cached hits
        are always accepted on read.
        """
        if existing is None or not existing.matched:
            return False
        if (existing.payload or {}).get("source") != "hasheous":
            return False
        if match.get("matched"):
            return False
        if match.get("checked_remote"):
            return False
        return not DATStore._proves_content_changed(existing, match)

    @staticmethod
    def _proves_content_changed(existing, match: dict) -> bool:
        """True only when a recomputed hash in the row's OWN domain contradicts it.

        Comparing across domains is meaningless: a CHD hit is stored against
        its embedded ``chd_sha1`` while a rescan recomputes the container's
        ``file_sha1``, and those differ for a file nobody has touched. That is
        why this used to demand ``existing.match_type == "file_sha1"`` and
        refuse everything else.

        Refusing everything else was too blunt, though. An exhaustive tool
        recomputes the *same* typed hash it matched on -- Dolphin's
        ``dolphin_disc_sha1`` -- so the domains do agree and the comparison is
        sound. Keying on the stored row's own ``match_type`` keeps the
        cross-domain case out while letting that one in, which matters because
        an exhaustive format has no ``file_sha1`` to fall back on: a replaced
        RVZ could never be proven changed, and kept its previous game's badge
        for good.
        """
        if not existing.file_hash or not existing.match_type:
            return False
        recomputed = recomputed_hash_in(match, existing.match_type)
        return bool(recomputed) and recomputed != existing.file_hash

    # A row is a *remote* hit only if it says so. This is the one place the
    # question is answered, and it is answered from the payload the writer
    # recorded -- never inferred from ``dat_id IS NULL``. A local hit whose DAT
    # was deleted between the match and the write has its dangling FK nulled by
    # ``_set_matches_batch_sync``, so the FK proxy called that row remote, preserved
    # it through every later import, and went on serving an identity from a DAT
    # the operator had removed. ``coalesce`` because SQL NULL is not False: a
    # payload with no ``source`` must compare unequal, not unknown, or the
    # NOT below would spare exactly the rows it is meant to drop.
    def _invalidate_dat_derived_matches(self, session) -> list[str]:
        """Drop the part of the match cache the local DATs are responsible for.

        A new/refreshed DAT set can change any verdict it produced, and can
        turn a previous miss into a hit -- so DAT-derived hits and cached
        misses both go. A *remote* (Hasheous) hit owes nothing to the local
        DATs, so it stays: deleting it stranded the badge permanently once the
        provider was switched off, since the recompute would then miss locally
        and cache "unmatched".

        Shared by both import paths on purpose. It lived only in
        ``_persist_sync`` at first, which left ``_import_dat_sync`` (the
        user-uploaded-DAT path) still wiping everything.

        Returns the paths it dropped, listed in the same transaction as the
        delete. The import already holds SQLite's write lock here (its DAT
        rows went in first), so no match can be written between the listing
        and the delete, and the list is exactly the set of files this change
        took a verdict away from (issue #492).
        """
        doomed = ~_remote_hit_clause()
        dropped = list(session.scalars(select(_db.DATMatch.path).where(doomed)).all())
        session.execute(delete(_db.DATMatch).where(doomed))
        return dropped

    @staticmethod
    def _remote_took_part(match: dict) -> bool:
        """True for a verdict the remote source took part in: a remote hit,
        or a miss stamped with the remote server it was checked against."""
        return bool(match.get("checked_remote")) or match.get("source") == "hasheous"

    @staticmethod
    def _local_index_now_covers(session, match: dict) -> bool:
        """True when the local DATs have since learned any candidate hash.

        Read inside the writing transaction, which is the point: the route
        decides local-first, but the decision and the write are separate
        operations, and a DAT import can commit in between -- for a path with
        no prior row the import's rematch snapshot cannot cover it either, so
        the verdict would be written *after* invalidation and then served
        unconditionally. Checking here closes that window at the boundary where
        the write actually happens.

        Applies to every verdict the remote source took part in, over the whole
        candidate set:

        * a stamped **miss** needs it as much as a hit. Restricting the guard
          to hits let a clean remote miss through untouched, and because that
          row is cacheable and its ``checked_remote`` stamp still matches, the
          local match that had just landed stayed hidden until the *next* DAT
          import happened to invalidate it.
        * a hit needs the **other** candidates checked, not only the one that
          matched. A CHD offers up to three hashes and the DAT that landed
          mid-flight may know a different one, in which case a local identity
          exists and the remote answer would outrank it.

        ``candidate_hashes`` is the transient key the route attaches for
        exactly this re-check (see :data:`CANDIDATE_HASHES_KEY`) -- typed
        ``(sha1, match_type)`` pairs, of which only the hashes matter here;
        ``file_hash`` is folded in so a hit stays covered even if that list is
        absent.

        Skipping the write (rather than rewriting the payload) keeps DAT-record
        shape out of the store: the path is simply left uncached, and the next
        match recomputes it local-first. That costs nothing extra remotely,
        because this only fires when the local index *does* know a hash --
        which is also why it re-checks hashes rather than comparing a DAT-index
        generation: a generation would skip the write on any unrelated import
        and send the file back out to the remote source for nothing.

        Local verdicts are the other half (issue #492), and for them the
        generation *is* the right test: see :meth:`_verdict_is_stale`.
        """
        if not DATStore._remote_took_part(match):
            return False
        hashes = [sha1 for sha1, _kind in (match.get(CANDIDATE_HASHES_KEY) or ())]
        file_hash = match.get("file_hash")
        if file_hash and file_hash not in hashes:
            hashes.append(file_hash)
        return any(
            session.execute(
                select(_db.DATHash.hash).where(
                    _db.DATHash.hash == sha1, _db.DATHash.hash_type == "sha1",
                ).limit(1)
            ).first() is not None
            for sha1 in hashes if sha1
        )

    def _verdict_is_stale(self, session, match: dict, generation: int | None) -> bool:
        """True when *match* must not be written: the local index moved under it.

        Called with SQLite's write lock held, so no index change can commit
        until the caller does, and *generation* is the value read under it.
        Two tests, by who took part in the verdict:

        * **remote** verdicts get only the hash re-check of
          :meth:`_local_index_now_covers`, which fires when the local index now
          knows one of the candidate hashes. The generation plays no part, even
          while a change is in flight: a remote answer is not something the
          local index decided, and a refused verdict goes to a *local-only*
          rematch, which cannot ask the remote source again. A remote hit
          refused for an unrelated import would come back as unmatched (Codex
          on PR #493). A change that commits after this write is covered too:
          its invalidation keeps a remote hit, which the post-change listing
          rematches, and drops a remote miss, which it reports.
        * **local** verdicts (a DAT hit, or a miss the remote source was not
          asked about) were decided entirely by the local index, so any change
          to it can change them, and a local-only rematch reproduces them.
          They have to carry the current generation
          (:data:`INDEX_GENERATION_KEY`). A hash re-check is not enough here: a
          local hit would miss a newly imported DAT that also knows the hash and
          outranks it, and a miss with no candidate hashes has nothing to
          re-check. This is issue #492's stale miss, a local-only rematch
          decided against the old index and written after the import that
          would have matched it. ``None`` on either side (the match started, or
          this write landed, while a change was in flight) is never current.

        A local verdict without the generation key makes no claim and is
        written as before.
        """
        if self._remote_took_part(match):
            return self._local_index_now_covers(session, match)
        if INDEX_GENERATION_KEY not in match:
            return False
        stamp = match[INDEX_GENERATION_KEY]
        return stamp is None or stamp != generation

    @staticmethod
    def _persistable_payload(match: dict) -> dict:
        """The cached payload: the match minus the transient check inputs.

        ``candidate_hashes`` and ``index_generation`` exist to be revalidated
        at the write boundary, not to be served back to the UI, so they are
        dropped in the one place the writer builds its payload.
        """
        payload = dict(match)
        payload.pop(CANDIDATE_HASHES_KEY, None)
        payload.pop(INDEX_GENERATION_KEY, None)
        return payload

    async def set_match(self, file_path: str, match: dict) -> list[str]:
        """Cache *match* for *file_path*; the paths it could not cache because
        the local index moved under it (see :meth:`set_matches_batch`)."""
        return await run_in_threadpool(self._set_matches_batch_sync, {file_path: match})

    def _delete_match_sync(self, file_path: str) -> bool:
        normalized = self._normalize(file_path)
        with self._session() as session:
            row = session.get(_db.DATMatch, normalized)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    async def delete_match(self, file_path: str) -> bool:
        """Remove the cached match row for ``file_path`` (no-op if absent).

        Used when a forced recompute produces a non-cacheable result so a
        stale prior match doesn't keep showing in ``/dat/matches/lookup``.
        Returns True when a row was removed.
        """
        return await run_in_threadpool(self._delete_match_sync, file_path)

    def move_match_blocking(self, old_path: str, new_path: str) -> bool:
        """Carry *old_path*'s cached match over to *new_path*, for a file
        renamed without its bytes changing (#425). A row already at
        *new_path* is replaced. True when a row moved. Blocking."""
        old, new = self._normalize(old_path), self._normalize(new_path)
        with self._session() as session:
            row = session.get(_db.DATMatch, old)
            if row is None:
                return False
            payload = dict(row.payload or {})
            if "path" in payload:
                payload["path"] = new
            existing = session.get(_db.DATMatch, new)
            if existing is not None:
                session.delete(existing)
                session.flush()
            session.add(_db.DATMatch(
                path=new, matched=row.matched, dat_id=row.dat_id,
                game_name=row.game_name, rom_name=row.rom_name,
                match_type=row.match_type, file_hash=row.file_hash, payload=payload,
            ))
            session.delete(row)
            session.commit()
            return True

    def _drop_match_if_changed_sync(self, file_path: str, match: dict) -> bool:
        normalized = self._normalize(file_path)
        with self._session() as session:
            row = session.get(_db.DATMatch, normalized)
            if row is None or not self._proves_content_changed(row, match):
                return False
            session.delete(row)
            session.commit()
            return True

    async def drop_match_if_changed(self, file_path: str, match: dict) -> bool:
        """Delete ``file_path``'s cached row only if ``match`` disproves it.

        The predicate is :meth:`_proves_content_changed`, the same one the
        write guard uses -- a recomputed hash in the *stored row's own* domain
        that contradicts it. Sharing it is the point: "did this file change?"
        had grown a second, narrower copy in the route layer, and the two
        answered differently for exhaustive formats.

        Read and delete happen in one session because they are one decision. A
        background match job can persist the replacement file's correct result
        between a separate get and delete -- the ``match`` workload token
        covers hashing, not this cache operation -- and the delete would then
        remove the fresh row it never compared. Returns True when a row was
        removed.
        """
        return await run_in_threadpool(self._drop_match_if_changed_sync, file_path, match)

    def _set_matches_batch_sync(self, matches: dict[str, dict]) -> list[str]:
        """Write *matches*; return the (normalized) paths owed a rematch.

        The one writer: :meth:`set_match` is a batch of one. A path is owed a
        rematch when :meth:`_verdict_is_stale` refuses its verdict. Nothing is
        written for it (an existing row is left as it was), so the caller hands
        it to a rematch rather than keeping a verdict decided against an index
        that is no longer current.

        SQLite's write lock is taken before anything is read, which is what
        makes the checks sound. An import holds that lock from its first DAT
        row to its commit and moves the generation before that
        (:meth:`_changing_local_index`), so from here until this commits the
        committed index cannot change: the hash re-check reads the index the
        row will sit beside, and any change that started earlier shows in the
        generation. It also keeps every other writer out between the prefetch
        and the write. A row an import's invalidation (or a prune) deleted
        after an unlocked prefetch made the flush raise ``StaleDataError``,
        which rolled back the whole batch with no path reported, so another
        path in it fell out of every rematch set (CodeRabbit on PR #493).
        """
        if not matches:
            return []
        normalized_matches = {
            self._normalize(raw_path): match
            for raw_path, match in matches.items()
        }
        owed: list[str] = []

        with self._session() as session:
            # BEGIN IMMEDIATE takes the write lock now instead of at the first
            # INSERT; pysqlite sees the open transaction and adds no BEGIN of
            # its own. Waits out an import under the 30 s busy timeout, as the
            # flush always did.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            generation = self.index_generation(session)
            # Guard against a dat_id naming a DAT that no longer exists (e.g.
            # deleted between match and persist): null it out rather than
            # violate the FK. One query, and existing rows prefetched in
            # chunks (SQLite's 999 bind-parameter limit), to avoid an N+1
            # session.get() loop.
            valid_dat_ids = set(session.scalars(select(_db.DAT.id)).all())
            chunk_size = 900
            norm_keys = list(normalized_matches.keys())
            existing_matches: dict[str, _db.DATMatch] = {}
            for i in range(0, len(norm_keys), chunk_size):
                chunk = norm_keys[i:i + chunk_size]
                stmt = select(_db.DATMatch).where(_db.DATMatch.path.in_(chunk))
                for row in session.scalars(stmt).all():
                    existing_matches[row.path] = row

            for normalized, match in normalized_matches.items():
                dat_id = match.get("dat_id")
                if dat_id is not None and dat_id not in valid_dat_ids:
                    dat_id = None
                if self._verdict_is_stale(session, match, generation):
                    # A DAT change landed while the match was running. Leave
                    # the row alone and report the path, so it is rematched
                    # against the index that is current now.
                    owed.append(normalized)
                    continue
                payload = self._persistable_payload(match)
                existing = existing_matches.get(normalized)
                if self._would_downgrade_remote_hit(existing, match):
                    # The post-sync rematch re-runs every previously-matched
                    # path. With Hasheous off, a path whose only identity came
                    # from the remote source misses locally and would be
                    # written back as "unmatched", undoing the preservation
                    # in _invalidate_dat_derived_matches -- so the selective
                    # invalidation alone was not enough end to end.
                    #
                    # Refused here rather than at each caller: the batch
                    # route, the background job and the scan all write through
                    # this one place. A file that genuinely changed is removed
                    # by routes.dat.drop_if_content_changed(), which deletes
                    # rather than downgrading, and a miss recorded while the
                    # remote source *was* consulted carries `checked_remote`
                    # and is allowed through. Not owed a rematch: the row
                    # already holds the better answer.
                    continue
                if existing is not None:
                    existing.matched = bool(match.get("matched", False))
                    existing.dat_id = dat_id
                    existing.game_name = match.get("game_name")
                    existing.rom_name = match.get("rom_name")
                    existing.match_type = match.get("match_type")
                    existing.file_hash = match.get("file_hash")
                    existing.payload = payload
                else:
                    session.add(_db.DATMatch(
                        path=normalized,
                        matched=bool(match.get("matched", False)),
                        dat_id=dat_id,
                        game_name=match.get("game_name"),
                        rom_name=match.get("rom_name"),
                        match_type=match.get("match_type"),
                        file_hash=match.get("file_hash"),
                        payload=payload,
                    ))
            session.commit()
        return owed

    async def set_matches_batch(self, matches: dict[str, dict]) -> list[str]:
        """Cache each of *matches*; the paths owed a rematch instead.

        A path is owed one when its verdict was decided against a local index
        that has since changed (see :meth:`_set_matches_batch_sync`). Its row
        is left as the change left it, and the caller is expected to hand the
        path to a rematch (``routes.dat.rematch_refused_verdicts``).
        """
        return await run_in_threadpool(self._set_matches_batch_sync, dict(matches))

    def get_matches_batch(self, file_paths: list[str]) -> dict[str, dict | None]:
        if not file_paths:
            return {}
        normalized_map = {p: self._normalize(p) for p in file_paths}
        unique_norms = list(set(normalized_map.values()))
        # Chunk to stay under SQLite's bind-parameter limit (default 999).
        chunk_size = 900
        by_norm: dict[str, dict] = {}
        with self._session() as session:
            ranks = self._ranks(session)
            for i in range(0, len(unique_norms), chunk_size):
                chunk = unique_norms[i:i + chunk_size]
                rows = session.scalars(
                    select(_db.DATMatch).where(_db.DATMatch.path.in_(chunk))
                ).all()
                by_norm.update({
                    r.path: self._ranked(dict(r.payload), r.path, ranks) for r in rows
                })
        return {orig: by_norm.get(norm) for orig, norm in normalized_map.items()}

    # ------------------------------------------------------------------
    # Stats / housekeeping
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        with self._session() as session:
            total_dats = session.query(_db.DAT).count()
            total_sha1 = session.query(_db.DATHash).filter(
                _db.DATHash.hash_type == "sha1"
            ).count()
            total_md5 = session.query(_db.DATHash).filter(
                _db.DATHash.hash_type == "md5"
            ).count()
            matched = session.query(_db.DATMatch).filter(
                _db.DATMatch.matched.is_(True)
            ).count()
            unmatched = session.query(_db.DATMatch).filter(
                _db.DATMatch.matched.is_(False)
            ).count()
            return {
                "total_dats": total_dats,
                "total_sha1_hashes": total_sha1,
                "total_md5_hashes": total_md5,
                "total_matches": matched,
                "total_unmatched": unmatched,
                "total_scanned": matched + unmatched,
            }

    def has_dats(self) -> bool:
        with self._session() as session:
            return session.query(_db.DAT).limit(1).first() is not None

    def has_stale_dats(self) -> bool:
        """Return True if any DAT row has file_count=0.

        Flags pre-parser-upgrade imports that wrote rows with zero entries
        (see PR #49 / self-heal in dat_sync._do_sync). Cheap enough for
        startup, single-row existence check.
        """
        with self._session() as session:
            return session.execute(
                select(_db.DAT.id).where(_db.DAT.file_count == 0).limit(1)
            ).first() is not None

    def list_match_paths(self) -> list[str]:
        """Return every path currently cached in DATMatch.

        Used by dat_sync._do_sync to snapshot scanned paths before the
        post-sync persist() wipes the cache, so a background rematch job
        can re-hash those files against the freshly-imported DAT set.

        Order is undefined; callers that need stable ordering must sort.
        """
        with self._session() as session:
            return list(session.scalars(select(_db.DATMatch.path)).all())


dat_store = DATStore()
