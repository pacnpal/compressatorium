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
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services import db as _db
from services.dat_parser import parse_dat
from services.dat_ranking import (
    FAMILIES, RankedMatch, apply_ranking, dat_family, image_kind,
    is_unrankable_local_hit, ranked_match,
)
from utils.path_utils import path_presence

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


def _dat_info(header: dict, entries: list[dict]) -> dict:
    """The ``dats`` row for a freshly parsed DAT (a new id, its family)."""
    return {
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

    def _import_dat_sync(self, source: str) -> tuple[dict, dict]:
        """Parse *source* and insert all entries into the DB in one transaction."""
        header, entries = parse_dat(source)
        dat_info = _dat_info(header, entries)
        hash_rows = _hash_rows(entries, dat_info["id"])
        with self._session() as session:
            self._insert_dat(session, dat_info, hash_rows)
            # Importing a new DAT invalidates the match cache (a
            # previously-"unmatched" file may now match, or a previously
            # matched file may now match against a different DAT) -- except
            # for remote hits, which the local DATs did not produce.
            self._invalidate_dat_derived_matches(session)
            session.commit()
        return dat_info, _import_result(dat_info, len(hash_rows))

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

    async def import_dat(self, source: str) -> dict:
        """Parse and import a DAT file (path or XML string)."""
        _info, result = await run_in_threadpool(self._import_dat_sync, source)
        return result

    async def import_dat_no_persist(self, source: str) -> dict:
        """Stage a DAT in memory; call :meth:`persist` to atomically commit all staged DATs."""
        def _stage() -> tuple[dict, dict]:
            header, entries = parse_dat(source)
            dat_info = _dat_info(header, entries)
            hash_rows = _hash_rows(entries, dat_info["id"])
            with self._pending_lock:
                self._pending_imports.append((dat_info, hash_rows))
            return dat_info, _import_result(dat_info, len(hash_rows))

        _info, result = await run_in_threadpool(_stage)
        return result

    def _persist_sync(self) -> None:
        """Commit all staged DATs in a single transaction and clear the staging area."""
        with self._pending_lock:
            pending = list(self._pending_imports)
            self._pending_imports = []
        if not pending:
            return
        with self._session() as session:
            for dat_info, hash_rows in pending:
                self._insert_dat(session, dat_info, hash_rows)
            self._invalidate_dat_derived_matches(session)
            session.commit()

    async def persist(self) -> None:
        """Commit all DATs staged by :meth:`import_dat_no_persist` in a single transaction."""
        await run_in_threadpool(self._persist_sync)

    async def discard_pending(self) -> None:
        """Discard all staged DATs without writing them to the database."""
        with self._pending_lock:
            self._pending_imports = []

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def _delete_dat_sync(self, dat_id: str) -> bool:
        with self._session() as session:
            row = session.get(_db.DAT, dat_id)
            if row is None:
                return False
            # Matches don't cascade (FK is SET NULL), drop them
            # explicitly so the behaviour matches the legacy store.
            session.execute(delete(_db.DATMatch).where(_db.DATMatch.dat_id == dat_id))
            # Hashes cascade via FK ON DELETE CASCADE.
            session.delete(row)
            session.commit()
        return True

    async def delete_dat(self, dat_id: str) -> bool:
        return await run_in_threadpool(self._delete_dat_sync, dat_id)

    def _delete_dats_bulk_sync(self, dat_ids: list[str]) -> int:
        if not dat_ids:
            return 0
        # Chunk to stay under SQLite's bind-parameter limit (default 999).
        chunk_size = 900
        with self._session() as session:
            existing: list[str] = []
            for i in range(0, len(dat_ids), chunk_size):
                chunk = dat_ids[i:i + chunk_size]
                existing.extend(session.scalars(
                    select(_db.DAT.id).where(_db.DAT.id.in_(chunk))
                ).all())
            if not existing:
                return 0
            for i in range(0, len(existing), chunk_size):
                chunk = existing[i:i + chunk_size]
                session.execute(delete(_db.DATMatch).where(_db.DATMatch.dat_id.in_(chunk)))
                session.execute(delete(_db.DAT).where(_db.DAT.id.in_(chunk)))
            session.commit()
            return len(existing)

    async def delete_dats_bulk(self, dat_ids: list[str]) -> int:
        return await run_in_threadpool(self._delete_dats_bulk_sync, list(dat_ids))

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
        ``matches`` lists every DAT's record, each with its DAT's name and
        family.
        """
        with self._session() as session:
            rows = session.execute(
                select(_db.DATHash, _db.DAT.name, _db.DAT.family)
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
                }
                for row, dat_name, family in rows
            ]
            return {**matches[0], "matches": matches}

    def lookup_sha1(self, sha1: str) -> dict | None:
        return self._lookup_hash(sha1, "sha1")

    def lookup_md5(self, md5: str) -> dict | None:
        return self._lookup_hash(md5, "md5")

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
    # ``_upsert_match_sync``, so the FK proxy called that row remote, preserved
    # it through every later import, and went on serving an identity from a DAT
    # the operator had removed. ``coalesce`` because SQL NULL is not False: a
    # payload with no ``source`` must compare unequal, not unknown, or the
    # NOT below would spare exactly the rows it is meant to drop.
    def _invalidate_dat_derived_matches(self, session) -> None:
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
        """
        session.execute(delete(_db.DATMatch).where(~_remote_hit_clause()))

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
        """
        if not (match.get("checked_remote") or match.get("source") == "hasheous"):
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

    @staticmethod
    def _persistable_payload(match: dict) -> dict:
        """The cached payload: the match minus the transient check inputs.

        ``candidate_hashes`` exists to be revalidated at the write boundary,
        not to be served back to the UI, so it is dropped in the one place
        both writers build their payload.
        """
        payload = dict(match)
        payload.pop(CANDIDATE_HASHES_KEY, None)
        return payload

    def _upsert_match_sync(self, file_path: str, match: dict) -> None:
        normalized = self._normalize(file_path)
        with self._session() as session:
            dat_id = match.get("dat_id")
            # Guard against dat_id referencing a non-existent DAT (e.g.,
            # deleted between match and persist).  Null it out rather
            # than violate the FK.
            if dat_id is not None:
                if session.get(_db.DAT, dat_id) is None:
                    dat_id = None
            if self._local_index_now_covers(session, match):
                # A DAT covering this hash landed while the match was running.
                # Leave the path uncached so the next one resolves it locally.
                return
            payload = self._persistable_payload(match)
            existing = session.get(_db.DATMatch, normalized)
            if self._would_downgrade_remote_hit(existing, match):
                # The post-sync rematch re-runs every previously-matched path.
                # With Hasheous off, a path whose only identity came from the
                # remote source misses locally and would be written back as
                # "unmatched", undoing the preservation above -- so the
                # selective invalidation alone was not enough end to end.
                #
                # Refused here rather than at each caller: the batch route, the
                # background job and the scan all write through this one place.
                # A file that genuinely changed is removed by
                # routes.dat.drop_if_content_changed(), which deletes rather
                # than downgrading, and a miss recorded while the remote source
                # *was* consulted carries `checked_remote` and is allowed
                # through.
                return
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

    async def set_match(self, file_path: str, match: dict) -> None:
        await run_in_threadpool(self._upsert_match_sync, file_path, match)

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

    def _set_matches_batch_sync(self, matches: dict[str, dict]) -> None:
        if not matches:
            return
        normalized_matches = {
            self._normalize(raw_path): match
            for raw_path, match in matches.items()
        }

        # Resolve valid dat_ids once and prefetch existing match rows for the
        # whole batch to avoid an N+1 session.get() loop.
        with self._session() as session:
            valid_dat_ids = set(session.scalars(select(_db.DAT.id)).all())
            # Prefetch existing rows in chunks to stay under SQLite's
            # 999 bind-parameter limit.
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
                if self._local_index_now_covers(session, match):
                    # Same rule as _upsert_match_sync.
                    continue
                payload = self._persistable_payload(match)
                existing = existing_matches.get(normalized)
                if self._would_downgrade_remote_hit(existing, match):
                    # Same rule as _upsert_match_sync. This path updated rows
                    # unconditionally, so a concurrent batch match could still
                    # erase a remote hit the single-path writer would refuse to
                    # touch -- and the guard's comment claimed the batch route
                    # wrote through it, which was simply untrue.
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

    async def set_matches_batch(self, matches: dict[str, dict]) -> None:
        await run_in_threadpool(self._set_matches_batch_sync, dict(matches))

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

    def _prune_missing_sync(self, progress: Callable[..., None] | None = None) -> int:
        """Drop the match entries whose file is confirmed gone; how many.

        Only a confirmed absence (``path_presence(...) is False``): a file on
        a Volume that stalls or errors keeps its entry (#373). ``progress`` is
        told each entry, for a stall-bounded caller; no session is held open
        while a Volume is asked. Blocking.
        """
        tick = progress or (lambda *_args: None)
        with self._session() as session:
            paths = list(session.scalars(select(_db.DATMatch.path)).all())
        missing = []
        for path in paths:
            tick(path)
            if path_presence(path) is False:
                missing.append(path)
        tick()  # a caller that gave up stops here, before deleting anything
        if not missing:
            return 0
        with self._session() as session:
            # Chunk to stay under SQLite's bind-parameter limit (default 999).
            chunk_size = 900
            for i in range(0, len(missing), chunk_size):
                chunk = missing[i:i + chunk_size]
                session.execute(delete(_db.DATMatch).where(_db.DATMatch.path.in_(chunk)))
            session.commit()
        return len(missing)

    def prune_missing_blocking(self, progress: Callable[..., None] | None = None) -> int:
        """:meth:`_prune_missing_sync`, for a stall-bounded caller (#373)."""
        return self._prune_missing_sync(progress)


dat_store = DATStore()
