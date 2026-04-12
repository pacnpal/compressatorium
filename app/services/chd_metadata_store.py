"""SQLite-backed CHD metadata cache.

Caches ``chdman info`` output (and disc-ID information) per CHD file
with mtime-based invalidation.  Public API is unchanged from the
previous JSON implementation; internals now dispatch to SQLAlchemy.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services import db as _db

logger = logging.getLogger("chd.chd_metadata_store")


# =============================================================================
# MEDIA TYPE DETECTION - Single Source of Truth
# =============================================================================
# Tag patterns that identify DVD media
DVD_TAG_PATTERNS = frozenset(["DVD", "DVD-VIDEO", "DVD-ROM"])

# Tag patterns that identify CD media (checked after DVD patterns)
CD_TAG_PATTERNS = frozenset(["CD", "CD-ROM", "CDROM", "GDROM", "GD-ROM"])

# Metadata prefixes that identify CD media (e.g., CHCD, CHT2, CHTR)
CD_METADATA_PREFIXES = ("CHCD", "CHT2", "CHTR", "CHT")

# CD-specific compression codecs (fallback detection)
CD_COMPRESSION_CODECS = frozenset(["cdzl", "cdzs", "cdlz", "cdfl"])
# =============================================================================


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CHDMetadataStore:
    """Persists CHD metadata across application restarts."""

    def __init__(
        self,
        store_path: Optional[str] = None,
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
                "CHDMetadataStore: db.SessionLocal not initialized — call "
                "db.init_engine() before using the store.",
            )
        return _db.SessionLocal()

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.realpath(path)

    # ------------------------------------------------------------------
    # Media-type extraction (unchanged)
    # ------------------------------------------------------------------

    @staticmethod
    def extract_media_type(info: dict) -> Optional[str]:
        """Extract media type (dvd/cd) from CHD metadata."""
        raw_data = info.get("raw_data", "")

        tag_values: set[str] = set()
        metadata_tags: set[str] = set()

        for line in raw_data.splitlines() if isinstance(raw_data, str) else []:
            if not line:
                continue
            for match in re.finditer(r"Tag\s*[:=]\s*([^,\s]+)", line, re.IGNORECASE):
                tag_value = match.group(1).strip().strip("'\"").upper()
                tag_values.add(tag_value)
            meta_match = re.search(r"^\s*Metadata:\s*([^,]+)", line, re.IGNORECASE)
            if meta_match:
                metadata_tags.add(meta_match.group(1).strip().upper())

        metadata_lines = info.get("metadata_lines")
        if isinstance(metadata_lines, list):
            for entry in metadata_lines:
                if not isinstance(entry, str):
                    continue
                metadata_tags.add(entry.strip().upper())

        def matches_dvd(value: str) -> bool:
            normalized = value.upper()
            return any(pat in normalized for pat in DVD_TAG_PATTERNS)

        def matches_cd(value: str) -> bool:
            normalized = re.sub(r"[^A-Z0-9]", "", value.upper())
            if any(pat.replace("-", "") in normalized for pat in CD_TAG_PATTERNS):
                return True
            if any(normalized.startswith(prefix) for prefix in CD_METADATA_PREFIXES):
                return True
            return False

        for tag_value in tag_values:
            if matches_dvd(tag_value):
                return "dvd"
        for tag_value in tag_values:
            if matches_cd(tag_value):
                return "cd"

        for meta_value in metadata_tags:
            if matches_dvd(meta_value):
                return "dvd"
        for meta_value in metadata_tags:
            if any(meta_value.startswith(prefix) for prefix in CD_METADATA_PREFIXES):
                return "cd"
            if matches_cd(meta_value):
                return "cd"

        metadata = info.get("metadata", "")
        if isinstance(metadata, str):
            metadata_upper = metadata.upper()
            if any(pat in metadata_upper for pat in DVD_TAG_PATTERNS):
                return "dvd"
            if any(pat.replace("-", "") in metadata_upper for pat in CD_TAG_PATTERNS):
                return "cd"
            if any(metadata_upper.startswith(prefix) for prefix in CD_METADATA_PREFIXES):
                return "cd"

        compression = info.get("compression", "")
        if isinstance(compression, str):
            if any(codec in compression.lower() for codec in CD_COMPRESSION_CODECS):
                return "cd"

        return None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _get_sync(self, chd_path: str) -> Optional[_db.CHDMetadata]:
        normalized = self._normalize_path(chd_path)
        with self._session() as session:
            return session.get(_db.CHDMetadata, normalized)

    async def get_metadata(self, chd_path: str) -> Optional[dict]:
        row = await run_in_threadpool(self._get_sync, chd_path)
        return None if row is None else row.metadata_json

    async def get_media_type(self, chd_path: str) -> Optional[str]:
        row = await run_in_threadpool(self._get_sync, chd_path)
        return None if row is None else row.media_type

    async def get_full_info(self, chd_path: str) -> tuple[Optional[dict], Optional[str]]:
        row = await run_in_threadpool(self._get_sync, chd_path)
        if row is None:
            return None, None
        return row.metadata_json, row.media_type

    async def get_disc_id_info(self, chd_path: str) -> tuple[Optional[str], Optional[str]]:
        row = await run_in_threadpool(self._get_sync, chd_path)
        if row is None:
            return None, None
        return row.game_id, row.title

    async def is_disc_id_checked(self, chd_path: str) -> bool:
        normalized = self._normalize_path(chd_path)
        try:
            current_mtime = await run_in_threadpool(os.path.getmtime, normalized)
        except OSError:
            return False
        row = await run_in_threadpool(self._get_sync, chd_path)
        if row is None or not row.disc_id_checked:
            return False
        return row.disc_id_checked_mtime == current_mtime

    async def is_stale(self, chd_path: str) -> bool:
        normalized = self._normalize_path(chd_path)
        try:
            current_mtime = await run_in_threadpool(os.path.getmtime, normalized)
        except OSError:
            # File missing / unreadable → treat as stale.
            return True
        row = await run_in_threadpool(self._get_sync, chd_path)
        if row is None or row.mtime is None:
            return True
        return current_mtime != row.mtime

    async def get_batch(self, chd_paths: list) -> Dict[str, dict]:
        """Bulk fetch: returns ``{path: {"media_type": str|None, "cached": True}}``
        for every cached entry.  Un-cached paths are simply absent."""
        if not chd_paths:
            return {}
        norm_map = {
            p: await run_in_threadpool(self._normalize_path, p) for p in chd_paths
        }
        unique_norms = list(set(norm_map.values()))

        def _fetch() -> dict[str, str | None]:
            with self._session() as session:
                rows = session.scalars(
                    select(_db.CHDMetadata).where(_db.CHDMetadata.chd_path.in_(unique_norms))
                ).all()
                return {r.chd_path: r.media_type for r in rows}

        by_norm = await run_in_threadpool(_fetch)
        return {
            orig: {"media_type": by_norm[norm], "cached": True}
            for orig, norm in norm_map.items()
            if norm in by_norm
        }

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _set_metadata_sync(self, chd_path: str, info: dict) -> dict:
        normalized = self._normalize_path(chd_path)
        try:
            mtime = os.path.getmtime(normalized)
        except OSError:
            mtime = None

        media_type = self.extract_media_type(info)
        now = _utcnow_iso()

        with self._session() as session:
            stmt = sqlite_insert(_db.CHDMetadata).values(
                chd_path=normalized,
                metadata_json=info,
                media_type=media_type,
                mtime=mtime,
                cached_at=now,
                disc_id_checked=False,
            )
            # ON CONFLICT: update only the Phase-1 fields; intentionally
            # preserve disc_id_checked, disc_id_checked_mtime, game_id, title
            # set by Phase 2 so they survive a Phase-1 metadata refresh.
            # Note: use SQL column names in set_ and excluded (attribute name
            # "metadata_json" maps to SQL column "metadata").
            stmt = stmt.on_conflict_do_update(
                index_elements=["chd_path"],
                set_={
                    "metadata": stmt.excluded.metadata,
                    "media_type": stmt.excluded.media_type,
                    "mtime": stmt.excluded.mtime,
                    "cached_at": stmt.excluded.cached_at,
                },
            )
            session.execute(stmt)
            session.commit()
            row = session.get(_db.CHDMetadata, normalized)
            if row is None:
                raise RuntimeError(
                    f"CHD metadata row missing after upsert for {normalized!r}; "
                    "this should never happen"
                )
            return {
                "chd_path": row.chd_path,
                "metadata": row.metadata_json,
                "media_type": row.media_type,
                "mtime": row.mtime,
                "cached_at": row.cached_at,
                "disc_id_checked": row.disc_id_checked,
                "disc_id_checked_mtime": row.disc_id_checked_mtime,
                "game_id": row.game_id,
                "title": row.title,
            }

    async def set_metadata(self, chd_path: str, info: dict, persist: bool = True) -> dict:
        """Cache metadata for a CHD file.

        ``persist`` is kept for interface parity; SQLite always commits
        per call so the flag is effectively a no-op (the old JSON code
        used it to batch writes).
        """
        _ = persist
        return await run_in_threadpool(self._set_metadata_sync, chd_path, info)

    def _mark_disc_id_checked_sync(self, chd_path: str) -> None:
        normalized = self._normalize_path(chd_path)
        try:
            current_mtime: float | None = os.path.getmtime(normalized)
        except OSError:
            current_mtime = None
        with self._session() as session:
            existing = session.get(_db.CHDMetadata, normalized)
            if existing is None:
                session.add(_db.CHDMetadata(
                    chd_path=normalized,
                    disc_id_checked=True,
                    disc_id_checked_mtime=current_mtime,
                ))
            else:
                existing.disc_id_checked = True
                existing.disc_id_checked_mtime = current_mtime
            session.commit()

    async def mark_disc_id_checked(self, chd_path: str) -> None:
        await run_in_threadpool(self._mark_disc_id_checked_sync, chd_path)

    def _update_disc_id_info_sync(
        self, chd_path: str, game_id: Optional[str], title: Optional[str],
    ) -> None:
        normalized = self._normalize_path(chd_path)
        with self._session() as session:
            existing = session.get(_db.CHDMetadata, normalized)
            if existing is None:
                session.add(_db.CHDMetadata(
                    chd_path=normalized,
                    game_id=game_id,
                    title=title,
                ))
            else:
                existing.game_id = game_id
                existing.title = title
            session.commit()

    async def update_disc_id_info(
        self, chd_path: str, game_id: Optional[str], title: Optional[str],
        persist: bool = True,
    ) -> None:
        """Store disc-ID info. ``persist`` is a no-op — kept for interface parity."""
        _ = persist
        await run_in_threadpool(self._update_disc_id_info_sync, chd_path, game_id, title)

    def _clear_sync(self, chd_path: str) -> None:
        normalized = self._normalize_path(chd_path)
        with self._session() as session:
            session.execute(
                delete(_db.CHDMetadata).where(_db.CHDMetadata.chd_path == normalized)
            )
            session.commit()

    async def clear(self, chd_path: str) -> None:
        await run_in_threadpool(self._clear_sync, chd_path)

    def _move_sync(self, old_path: str, new_path: str) -> None:
        old_normalized = self._normalize_path(old_path)
        new_normalized = self._normalize_path(new_path)
        try:
            new_mtime: float | None = os.path.getmtime(new_normalized)
        except OSError:
            new_mtime = None

        with self._session() as session:
            old = session.get(_db.CHDMetadata, old_normalized)
            if old is None:
                return
            # Copy fields, drop old, insert new.  Using ``session.delete``
            # + ``add`` rather than UPDATE so PK change is explicit.
            payload = {
                "chd_path": new_normalized,
                "metadata_json": old.metadata_json,
                "media_type": old.media_type,
                "mtime": new_mtime,
                "cached_at": old.cached_at,
                "disc_id_checked": old.disc_id_checked,
                "disc_id_checked_mtime": old.disc_id_checked_mtime,
                "game_id": old.game_id,
                "title": old.title,
            }
            session.delete(old)
            session.flush()
            existing_new = session.get(_db.CHDMetadata, new_normalized)
            if existing_new is not None:
                for k, v in payload.items():
                    setattr(existing_new, k, v)
            else:
                session.add(_db.CHDMetadata(**payload))
            session.commit()

    async def move(self, old_path: str, new_path: str) -> None:
        await run_in_threadpool(self._move_sync, old_path, new_path)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def all_records(self) -> List[dict]:
        with self._session() as session:
            rows = session.scalars(select(_db.CHDMetadata)).all()
            return [
                {
                    "chd_path": r.chd_path,
                    "metadata": r.metadata_json,
                    "media_type": r.media_type,
                    "mtime": r.mtime,
                    "cached_at": r.cached_at,
                    "disc_id_checked": r.disc_id_checked,
                    "disc_id_checked_mtime": r.disc_id_checked_mtime,
                    "game_id": r.game_id,
                    "title": r.title,
                }
                for r in rows
            ]

    def _prune_missing_sync(self) -> int:
        with self._session() as session:
            paths = session.scalars(select(_db.CHDMetadata.chd_path)).all()
            missing = [p for p in paths if not os.path.exists(p)]
            if not missing:
                return 0
            # Chunk to stay under SQLite's bind-parameter limit (default 999).
            chunk_size = 900
            for i in range(0, len(missing), chunk_size):
                batch = missing[i:i + chunk_size]
                session.execute(
                    delete(_db.CHDMetadata).where(_db.CHDMetadata.chd_path.in_(batch))
                )
            session.commit()
            return len(missing)

    async def prune_missing(self) -> int:
        return await run_in_threadpool(self._prune_missing_sync)

    # ------------------------------------------------------------------
    # Legacy dirty/flush interface (kept as no-ops).  SQLite commits
    # synchronously per-call, so there is nothing to flush.
    # ------------------------------------------------------------------

    def is_dirty(self) -> bool:
        return False

    async def flush_async(self) -> None:
        return None

    def flush(self) -> None:
        return None

    async def _persist_async(self) -> None:  # pragma: no cover — kept for test compat
        """Legacy hook some tests monkeypatch.  No-op under SQLite."""
        return None


# Singleton instance
chd_metadata_store = CHDMetadataStore()
