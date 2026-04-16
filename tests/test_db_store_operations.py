"""Store-layer CRUD and foreign-key behaviour.

Exercises ORM invariants that the application code depends on:
CASCADE on DAT delete, SET NULL on DAT delete for cached matches,
upsert idempotency, chunked-upsert boundaries against SQLite's
999-bind-parameter limit, and the ``DATSyncState`` singleton.
"""

# ruff: noqa: S101

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import delete, select

from services import db as _db


@pytest.fixture(autouse=True)
def _reset_engine():
    _db.engine = None
    _db.SessionLocal = None
    yield
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest.fixture
def ready_db(db_path: str):
    """Engine + schema ready to go via Alembic."""
    _db.init_engine(db_path, create_schema=False)
    _db.apply_migrations()
    return _db.engine


def _make_dat(session, dat_id: str, name: str = "Test DAT") -> None:
    session.add(_db.DAT(
        id=dat_id, name=name, description="", version="", imported_at="", file_count=0,
    ))


# ---------------------------------------------------------------------------
# Cascade / SET NULL
# ---------------------------------------------------------------------------


def test_dat_delete_cascades_to_hashes(ready_db):
    with _db.get_session() as s:
        _make_dat(s, "dat00001")
        s.flush()
        s.bulk_insert_mappings(_db.DATHash, [
            {
                "hash": "a" * 40, "hash_type": "sha1", "dat_id": "dat00001",
                "game_name": "g", "rom_name": "r.iso", "size": 1,
            },
            {
                "hash": "b" * 40, "hash_type": "sha1", "dat_id": "dat00001",
                "game_name": "g", "rom_name": "r.iso", "size": 1,
            },
        ])
        s.commit()

    with _db.get_session() as s:
        assert s.query(_db.DATHash).count() == 2
        s.execute(delete(_db.DAT).where(_db.DAT.id == "dat00001"))
        s.commit()

    with _db.get_session() as s:
        assert s.query(_db.DAT).count() == 0
        assert s.query(_db.DATHash).count() == 0


def test_dat_delete_sets_match_dat_id_to_null(ready_db):
    with _db.get_session() as s:
        _make_dat(s, "dat00002")
        s.flush()
        s.add(_db.DATMatch(
            path="/data/x.chd",
            matched=True,
            dat_id="dat00002",
            game_name="Game",
            rom_name="Game.iso",
            match_type="file_sha1",
            file_hash="a" * 40,
            payload={"matched": True},
        ))
        s.commit()

    with _db.get_session() as s:
        s.execute(delete(_db.DAT).where(_db.DAT.id == "dat00002"))
        s.commit()

    with _db.get_session() as s:
        row = s.get(_db.DATMatch, "/data/x.chd")
        assert row is not None, "match row must survive DAT deletion"
        assert row.dat_id is None, "dat_id must be NULL'd via ON DELETE SET NULL"
        # matched flag preserved — UI still shows the historical result.
        assert row.matched is True
        assert row.match_type == "file_sha1"


# ---------------------------------------------------------------------------
# Chunked upsert boundaries (DATStore._set_matches_batch_sync)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [899, 900, 901, 1800])
def test_set_matches_batch_crosses_chunk_boundary(ready_db, count: int):
    """dat_store.py chunks at 900 to stay under SQLite's 999-bind-parameter
    limit.  Verify 899 / 900 / 901 / 1800 all round-trip without
    `too many SQL variables` and without duplicating rows."""
    from app.services.dat_store import DATStore

    store = DATStore()  # uses module-level SessionLocal

    matches = {
        f"/data/item_{i:05d}.chd": {
            "matched": False,
            "path": f"/data/item_{i:05d}.chd",
        }
        for i in range(count)
    }

    store._set_matches_batch_sync(matches)

    with _db.get_session() as s:
        assert s.query(_db.DATMatch).count() == count

    # Rerun: upsert path (prefetch + update) must not duplicate rows.
    store._set_matches_batch_sync(matches)
    with _db.get_session() as s:
        assert s.query(_db.DATMatch).count() == count


# ---------------------------------------------------------------------------
# VerificationStore upsert idempotency
# ---------------------------------------------------------------------------


def test_verification_mark_sync_is_idempotent(ready_db):
    from app.services.verification_store import VerificationStore

    store = VerificationStore()
    store._mark_sync("/data/mario.chd", None)
    store._mark_sync("/data/mario.chd", "/data/mario.iso")
    store._mark_sync("/data/mario.chd", "/data/mario-v2.iso")

    with _db.get_session() as s:
        rows = s.query(_db.Verification).all()
        assert len(rows) == 1
        # Last write wins for source_path.
        assert rows[0].source_path.endswith("mario-v2.iso")


# ---------------------------------------------------------------------------
# CHDMetadata upsert — arbitrary JSON payloads survive round-trip
# ---------------------------------------------------------------------------


def test_chd_metadata_upsert_preserves_arbitrary_json(ready_db):
    with _db.get_session() as s:
        s.add(_db.CHDMetadata(
            chd_path="/data/psx.chd",
            metadata_json={
                "nested": {"list": [1, 2, {"深い": "値"}], "bool": True, "null": None},
                "emoji": "🎮🕹️",
                "big_int": 2**50,
            },
            media_type="cd",
            mtime=1700000000.0,
            cached_at="2026-04-10T14:00:00Z",
            disc_id_checked=False,
            disc_id_checked_mtime=None,
            game_id=None,
            title=None,
        ))
        s.commit()

    # Re-save with a mutated payload (upsert via ORM merge).
    with _db.get_session() as s:
        row = s.get(_db.CHDMetadata, "/data/psx.chd")
        assert row is not None
        row.metadata_json = {"changed": True, "emoji": "🎮"}
        row.title = "New Title"
        s.commit()

    with _db.get_session() as s:
        assert s.query(_db.CHDMetadata).count() == 1
        row = s.get(_db.CHDMetadata, "/data/psx.chd")
        assert row.metadata_json == {"changed": True, "emoji": "🎮"}
        assert row.title == "New Title"


# ---------------------------------------------------------------------------
# has_stale_dats — startup self-heal trigger (see main.py auto-sync branch)
# ---------------------------------------------------------------------------


def test_has_stale_dats_empty_store_returns_false(ready_db):
    from app.services.dat_store import DATStore

    store = DATStore()
    assert store.has_stale_dats() is False


def test_has_stale_dats_all_healthy_returns_false(ready_db):
    from app.services.dat_store import DATStore

    with _db.get_session() as s:
        s.add(_db.DAT(
            id="healthy1", name="Healthy One", description="", version="",
            imported_at="", file_count=42,
        ))
        s.add(_db.DAT(
            id="healthy2", name="Healthy Two", description="", version="",
            imported_at="", file_count=1,
        ))
        s.commit()

    assert DATStore().has_stale_dats() is False


def test_has_stale_dats_detects_zero_count(ready_db):
    from app.services.dat_store import DATStore

    with _db.get_session() as s:
        s.add(_db.DAT(
            id="healthy", name="GameCube", description="", version="",
            imported_at="", file_count=2018,
        ))
        s.add(_db.DAT(
            id="stale", name="PlayStation", description="", version="",
            imported_at="", file_count=0,
        ))
        s.commit()

    store = DATStore()
    assert store.has_stale_dats() is True

    # Removing the stale row clears the flag.
    with _db.get_session() as s:
        s.execute(delete(_db.DAT).where(_db.DAT.id == "stale"))
        s.commit()
    assert store.has_stale_dats() is False


# ---------------------------------------------------------------------------
# list_match_paths — snapshot source for post-sync rematch hook
# ---------------------------------------------------------------------------


def test_list_match_paths_returns_empty_on_fresh_store(ready_db):
    from app.services.dat_store import DATStore

    assert DATStore().list_match_paths() == []


def test_list_match_paths_returns_all_paths(ready_db):
    from app.services.dat_store import DATStore

    with _db.get_session() as s:
        s.add(_db.DATMatch(path="/data/a.chd", matched=True))
        s.add(_db.DATMatch(path="/data/b.chd", matched=False))
        s.add(_db.DATMatch(path="/data/c.chd", matched=True))
        s.commit()

    assert sorted(DATStore().list_match_paths()) == [
        "/data/a.chd", "/data/b.chd", "/data/c.chd",
    ]


# ---------------------------------------------------------------------------
# DATSyncState singleton invariant
# ---------------------------------------------------------------------------


def test_dat_sync_state_singleton_never_duplicates(ready_db):
    with _db.get_session() as s:
        # Insert / update the singleton many times; row count stays 1.
        for tag in ("0.280", "0.281", "0.285"):
            existing = s.get(_db.DATSyncState, 1)
            if existing is None:
                s.add(_db.DATSyncState(
                    id=1, last_sync_tag=tag, last_sync_at="t", last_sync_files=0,
                ))
            else:
                existing.last_sync_tag = tag
            s.commit()

    with _db.get_session() as s:
        assert s.query(_db.DATSyncState).count() == 1
        assert s.get(_db.DATSyncState, 1).last_sync_tag == "0.285"
