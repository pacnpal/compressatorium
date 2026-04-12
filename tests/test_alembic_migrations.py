"""Tests for the Alembic integration.

Five behaviours under test, all load-bearing for a safe upgrade:

* **I1** — Fresh DB: ``apply_migrations`` runs ``upgrade head`` and
  leaves ``alembic_version`` at ``0001``.
* **I2** — Pre-Alembic DB (baseline schema exists, no
  ``alembic_version``): ``apply_migrations`` stamps the baseline
  revision and reaches head; Alembic may create ``alembic_version``,
  but the existing schema and rows must survive unchanged.
* **I3** — Already-stamped DB: ``apply_migrations`` is a no-op.
* **I4** — ORM drift guard: ``alembic.autogenerate.compare_metadata``
  against a just-upgraded DB must yield no diffs.  Catches future
  schema changes that land in ``Base.metadata`` without a migration.
* **I5** — Called before engine init: ``apply_migrations`` fails fast
  with the expected guard rather than running against an uninitialized
  engine/session setup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.services import db as _db


@pytest.fixture
def fresh_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "compressatorium.db")


@pytest.fixture(autouse=True)
def _reset_module_engine():
    """Each test gets a clean module-level engine / SessionLocal."""
    _db.engine = None
    _db.SessionLocal = None
    yield
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _current_rev(engine) -> str | None:
    from alembic.migration import MigrationContext
    with engine.begin() as conn:
        return MigrationContext.configure(conn).get_current_revision()


# ---------------------------------------------------------------------------
# I1 — fresh DB
# ---------------------------------------------------------------------------


def test_upgrade_head_on_fresh_db(fresh_db_path: str):
    engine = _db.init_engine(fresh_db_path, create_schema=False)
    # Pre-condition: no tables at all.
    assert inspect(engine).get_table_names() == []

    _db.apply_migrations()

    tables = set(inspect(engine).get_table_names())
    assert _db._BASELINE_TABLES.issubset(tables)
    assert "alembic_version" in tables
    assert _current_rev(engine) == "0001"


# ---------------------------------------------------------------------------
# I2 — pre-Alembic DB
# ---------------------------------------------------------------------------


def test_stamp_head_on_preexisting_schema(fresh_db_path: str):
    # Simulate the previous release: schema populated by create_all,
    # no alembic_version row.
    engine = _db.init_engine(fresh_db_path, create_schema=True)
    tables_before = set(inspect(engine).get_table_names())
    assert _db._BASELINE_TABLES.issubset(tables_before)
    assert "alembic_version" not in tables_before

    # Seed a row so we can prove stamping doesn't touch data.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dats (id, name, description, version, "
                "imported_at, file_count) "
                "VALUES ('pre00001', 'Pre-Alembic', '', '', '', 0)"
            )
        )

    _db.apply_migrations()

    # alembic_version is now present at head; schema is otherwise
    # unchanged; the seeded row still exists.
    assert _current_rev(engine) == "0001"
    with engine.begin() as conn:
        got = conn.execute(
            text("SELECT name FROM dats WHERE id = 'pre00001'")
        ).scalar_one()
    assert got == "Pre-Alembic"


# ---------------------------------------------------------------------------
# I3 — idempotency
# ---------------------------------------------------------------------------


def test_apply_migrations_idempotent(fresh_db_path: str):
    _db.init_engine(fresh_db_path, create_schema=False)
    _db.apply_migrations()
    first_rev = _current_rev(_db.engine)

    # Second call: must not error and must not change the revision.
    _db.apply_migrations()
    second_rev = _current_rev(_db.engine)

    assert first_rev == second_rev == "0001"


# ---------------------------------------------------------------------------
# I4 — autogenerate drift guard
# ---------------------------------------------------------------------------


def test_no_model_drift_after_upgrade(fresh_db_path: str):
    """compare_metadata against a just-upgraded DB must be empty.

    If this test fails, it means ``Base.metadata`` has drifted from the
    migration chain — fix is to generate a new revision:

        scripts/new_migration.sh "describe the change"
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = _db.init_engine(fresh_db_path, create_schema=False)
    _db.apply_migrations()

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(ctx, _db.Base.metadata)

    # Filter out the alembic_version table — it's not in Base.metadata
    # by design and shows as a spurious diff otherwise.
    relevant = [
        d for d in diffs
        if not (isinstance(d, tuple) and d and getattr(d[-1], "name", "") == "alembic_version")
    ]
    assert relevant == [], f"ORM drift detected: {relevant}"


# ---------------------------------------------------------------------------
# Called-before-init guard
# ---------------------------------------------------------------------------


def test_apply_migrations_without_engine_raises():
    _db.engine = None
    with pytest.raises(RuntimeError, match="before init_engine"):
        _db.apply_migrations()
