"""Tests for the Alembic integration.

Five behaviours under test, all load-bearing for a safe upgrade:

* **I1**, Fresh DB: ``apply_migrations`` runs ``upgrade head`` and
  leaves ``alembic_version`` at ``0001``.
* **I2**, Pre-Alembic DB (baseline schema exists, no
  ``alembic_version``): ``apply_migrations`` stamps the baseline
  revision and reaches head; Alembic may create ``alembic_version``,
  but the existing schema and rows must survive unchanged.
* **I3**, Already-stamped DB: ``apply_migrations`` is a no-op.
* **I4**, ORM drift guard: ``alembic.autogenerate.compare_metadata``
  against a just-upgraded DB must yield no diffs.  Catches future
  schema changes that land in ``Base.metadata`` without a migration.
* **I5**, Called before engine init: ``apply_migrations`` fails fast
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


def _head_rev() -> str:
    """Latest migration revision, read from the script directory.

    Derived rather than hardcoded so adding a migration doesn't require
    touching every revision assertion here.
    """
    from alembic.script import ScriptDirectory
    cfg = _db._alembic_config(_db.make_engine(":memory:"))
    return ScriptDirectory.from_config(cfg).get_current_head()


# ---------------------------------------------------------------------------
# I1, fresh DB
# ---------------------------------------------------------------------------


def test_upgrade_head_on_fresh_db(fresh_db_path: str):
    engine = _db.init_engine(fresh_db_path, create_schema=False)
    # Pre-condition: no tables at all.
    assert inspect(engine).get_table_names() == []

    _db.apply_migrations()

    tables = set(inspect(engine).get_table_names())
    assert _db._BASELINE_TABLES.issubset(tables)
    assert "alembic_version" in tables
    assert _current_rev(engine) == _head_rev()


# ---------------------------------------------------------------------------
# I2, pre-Alembic DB
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
    assert _current_rev(engine) == _head_rev()
    with engine.begin() as conn:
        got = conn.execute(
            text("SELECT name FROM dats WHERE id = 'pre00001'")
        ).scalar_one()
    assert got == "Pre-Alembic"


# ---------------------------------------------------------------------------
# I3, idempotency
# ---------------------------------------------------------------------------


def test_apply_migrations_idempotent(fresh_db_path: str):
    _db.init_engine(fresh_db_path, create_schema=False)
    _db.apply_migrations()
    first_rev = _current_rev(_db.engine)

    # Second call: must not error and must not change the revision.
    _db.apply_migrations()
    second_rev = _current_rev(_db.engine)

    assert first_rev == second_rev == _head_rev()


# ---------------------------------------------------------------------------
# I4, autogenerate drift guard
# ---------------------------------------------------------------------------


def test_no_model_drift_after_upgrade(fresh_db_path: str):
    """compare_metadata against a just-upgraded DB must be empty.

    If this test fails, it means ``Base.metadata`` has drifted from the
    migration chain, fix is to generate a new revision:

        scripts/new_migration.sh "describe the change"
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = _db.init_engine(fresh_db_path, create_schema=False)
    _db.apply_migrations()

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(ctx, _db.Base.metadata)

    # Filter out the alembic_version table, it's not in Base.metadata
    # by design and shows as a spurious diff otherwise.
    relevant = [
        d for d in diffs
        if not (isinstance(d, tuple) and d and getattr(d[-1], "name", "") == "alembic_version")
    ]
    assert relevant == [], f"ORM drift detected: {relevant}"


def test_0014_stacks_on_0013_and_walks_up_from_an_empty_database(fresh_db_path: str):
    """#305's migration is 0014, on #304's 0013, on #303's 0012, on #297's
    0011: one head.

    Forward-only (every ``downgrade`` raises), so the chain is walked upward:
    an empty database taken to 0011, then 0012, then 0013, then 0014, each
    revision bringing its own tables.
    """
    from alembic import command
    from alembic.script import ScriptDirectory

    engine = _db.make_engine(fresh_db_path)
    cfg = _db._alembic_config(engine)
    script = ScriptDirectory.from_config(cfg)
    # #386's 0016 sits on 0015, #388's 0017 on that, #387's 0018 on that and
    # #389's 0019 on that, #391's 0020 on that, #392's 0021 on that and
    # #428's 0022 on that; see their own tests below.
    assert script.get_heads() == ["0022"]
    assert script.get_revision("0014").down_revision == "0013"
    assert script.get_revision("0013").down_revision == "0012"
    assert script.get_revision("0012").down_revision == "0011"

    def tables() -> set[str]:
        return set(inspect(engine).get_table_names())

    added = {"platform_mappings", "findings", "finding_dismissals"}
    command.upgrade(cfg, "0011")
    assert "ledger_totals" in tables()
    assert not ({"folder_libraries"} | added) & tables()
    command.upgrade(cfg, "0012")
    assert "folder_libraries" in tables()
    assert not added & tables()
    command.upgrade(cfg, "0013")
    assert added <= tables()
    assert "image_checks" not in tables()
    assert _current_rev(engine) == "0013"
    # Ids of cleared Findings are never handed out again (PR #322).
    with engine.connect() as conn:
        sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'findings'",
        ).scalar()
    assert "AUTOINCREMENT" in sql
    command.upgrade(cfg, "0014")
    assert "image_checks" in tables()
    assert _current_rev(engine) == "0014"


def test_0015_adds_the_re_pin_plan_generation(fresh_db_path: str):
    """#337's 0015, on 0014: the re-pin row records its plan's generation.

    Nullable and left null on existing rows, which read as the oldest
    generation, so a new plan may still supersede them.
    """
    from alembic import command
    from alembic.script import ScriptDirectory

    engine = _db.make_engine(fresh_db_path)
    cfg = _db._alembic_config(engine)
    assert ScriptDirectory.from_config(cfg).get_revision("0015").down_revision == "0014"

    def columns() -> dict[str, dict]:
        return {c["name"]: c for c in inspect(engine).get_columns("romm_repin")}

    command.upgrade(cfg, "0014")
    assert "plan_generation" not in columns()
    command.upgrade(cfg, "0015")
    assert columns()["plan_generation"]["nullable"] is True
    assert _current_rev(engine) == "0015"


def test_0016_adds_the_es_frontend_switch_off_by_default(fresh_db_path: str):
    """#386's 0016, on 0015: a folder Library's "used by an EmulationStation
    frontend" switch. A Library registered before it reads as off."""
    from alembic import command
    from alembic.script import ScriptDirectory

    engine = _db.make_engine(fresh_db_path)
    cfg = _db._alembic_config(engine)
    assert ScriptDirectory.from_config(cfg).get_revision("0016").down_revision == "0015"

    command.upgrade(cfg, "0015")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO folder_libraries (root, name, schedule_enabled, max_per_sweep, "
            "created_at) VALUES ('/roms', 'Roms', 0, 25, '2026-11-16T00:00:00Z')",
        )
    command.upgrade(cfg, "0016")

    columns = {c["name"]: c for c in inspect(engine).get_columns("folder_libraries")}
    assert columns["es_frontend"]["nullable"] is False
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT es_frontend FROM folder_libraries").scalar() == 0
    assert _current_rev(engine) == "0016"


def test_0017_adds_the_content_keys(fresh_db_path: str):
    """#388's 0017, on #386's 0016: the duplicate finder's content keys and each
    Library's last find-duplicates run."""
    from alembic import command
    from alembic.script import ScriptDirectory

    engine = _db.make_engine(fresh_db_path)
    cfg = _db._alembic_config(engine)
    assert ScriptDirectory.from_config(cfg).get_revision("0017").down_revision == "0016"

    command.upgrade(cfg, "0016")
    assert not {"content_keys", "duplicate_runs"} & set(inspect(engine).get_table_names())
    command.upgrade(cfg, "0017")
    assert {"content_keys", "duplicate_runs"} <= set(inspect(engine).get_table_names())
    columns = {c["name"]: c for c in inspect(engine).get_columns("content_keys")}
    # A key is tied to the file's size and mtime; a format with no key stores None.
    assert {"path", "size", "mtime_ns", "key", "container"} <= set(columns)
    assert columns["key"]["nullable"] is True
    # The CHD metadata cache records the file's identity, size and
    # st_mtime_ns (Codex 4589796866, CodeRabbit 4589859837); rows written
    # before it read None and are stale.
    chd = {c["name"]: c for c in inspect(engine).get_columns("chd_metadata")}
    assert chd["size"]["nullable"] is True
    assert chd["mtime_ns"]["nullable"] is True
    assert _current_rev(engine) == "0017"


def test_0018_adds_the_gamelists_folder_unset(fresh_db_path: str):
    """#387's 0018, on #388's 0017: a folder Library's ES-DE gamelists
    folder, null (none) for a Library registered before it."""
    from alembic import command
    from alembic.script import ScriptDirectory

    engine = _db.make_engine(fresh_db_path)
    cfg = _db._alembic_config(engine)
    assert ScriptDirectory.from_config(cfg).get_revision("0018").down_revision == "0017"

    command.upgrade(cfg, "0017")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO folder_libraries (root, name, schedule_enabled, max_per_sweep, "
            "created_at) VALUES ('/roms', 'Roms', 0, 25, '2026-11-16T00:00:00Z')",
        )
    command.upgrade(cfg, "0018")

    columns = {c["name"]: c for c in inspect(engine).get_columns("folder_libraries")}
    assert columns["gamelists_folder"]["nullable"] is True
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT gamelists_folder FROM folder_libraries",
        ).scalar() is None
    assert _current_rev(engine) == "0018"


def test_0019_ties_a_content_key_to_its_file(fresh_db_path: str):
    """#389's 0019, on #387's 0018: a content key's device and inode beside
    its size and mtime, null on a row written before (such a key is read
    again)."""
    from alembic import command
    from alembic.script import ScriptDirectory

    engine = _db.make_engine(fresh_db_path)
    cfg = _db._alembic_config(engine)
    assert ScriptDirectory.from_config(cfg).get_revision("0019").down_revision == "0018"

    command.upgrade(cfg, "0018")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO content_keys (path, size, mtime_ns, key, container, computed_at) "
            "VALUES ('/v/Game.iso', 4, 1, 'raw:aa', '.iso', '2026-11-16T00:00:00Z')",
        )
    command.upgrade(cfg, "0019")

    columns = {c["name"]: c for c in inspect(engine).get_columns("content_keys")}
    assert columns["dev"]["nullable"] is True and columns["ino"]["nullable"] is True
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT dev, ino FROM content_keys").one() == (None, None)
    assert _current_rev(engine) == "0019"


def test_0020_adds_the_romm_swap_journal(fresh_db_path: str):
    """#391's 0020, on #389's 0019: the RomM swap journal."""
    from alembic import command
    from alembic.script import ScriptDirectory

    engine = _db.make_engine(fresh_db_path)
    cfg = _db._alembic_config(engine)
    assert ScriptDirectory.from_config(cfg).get_revision("0020").down_revision == "0019"

    command.upgrade(cfg, "0019")
    assert "romm_swaps" not in set(inspect(engine).get_table_names())
    command.upgrade(cfg, "0020")
    columns = {c["name"]: c for c in inspect(engine).get_columns("romm_swaps")}
    assert {
        "rom_id", "source_path", "output_path", "staged_path", "parked_path",
        "old_name", "new_name", "state", "detail",
    } <= set(columns)
    assert columns["parked_path"]["nullable"] is True
    assert _current_rev(engine) == "0020"


def test_0021_records_each_swaps_instance_and_fingerprints(fresh_db_path: str):
    """#392's 0021, on #391's 0020: what startup recovery needs from a swap's
    row (its RomM instance, the Source's and the output's fingerprints). Rows
    written before it have none of them, so each column is nullable."""
    from alembic import command
    from alembic.script import ScriptDirectory

    engine = _db.make_engine(fresh_db_path)
    cfg = _db._alembic_config(engine)
    assert ScriptDirectory.from_config(cfg).get_revision("0021").down_revision == "0020"

    command.upgrade(cfg, "0021")
    columns = {c["name"]: c for c in inspect(engine).get_columns("romm_swaps")}
    added = (
        "romm_url", "library_root", "source_size", "source_mtime_ns",
        "output_size", "output_sha1",
    )
    assert set(added) <= set(columns)
    assert all(columns[name]["nullable"] is True for name in added)
    assert _current_rev(engine) == "0021"


def test_0022_gives_ledger_rows_their_extra_inputs(fresh_db_path: str):
    """#428's 0022, on #392's 0021: the Ledger's list of inputs besides the
    Source (a patch job's patch file, with its SHA1). A row written before it
    keeps every value and reads as having none."""
    from alembic import command
    from alembic.script import ScriptDirectory

    engine = _db.make_engine(fresh_db_path)
    cfg = _db._alembic_config(engine)
    assert ScriptDirectory.from_config(cfg).get_revision("0022").down_revision == "0021"

    command.upgrade(cfg, "0021")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO ledger (job_id, finished_at, mode, source_path, status,"
            " check_passed, source_deleted, deleted_paths, referrers_updated)"
            " VALUES ('old', '2026-01-01T00:00:00.000000Z', 'createcd', '/v/a.cue',"
            " 'completed', 'intact', 0, '[]', '[]')",
        )
    command.upgrade(cfg, "0022")
    columns = {c["name"]: c for c in inspect(engine).get_columns("ledger")}
    assert columns["extra_inputs"]["nullable"] is False
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT source_path, extra_inputs FROM ledger WHERE job_id = 'old'",
        ).one()
    assert row == ("/v/a.cue", "[]")
    assert _current_rev(engine) == "0022"


# ---------------------------------------------------------------------------
# Called-before-init guard
# ---------------------------------------------------------------------------


def test_apply_migrations_without_engine_raises():
    _db.engine = None
    with pytest.raises(RuntimeError, match="before init_engine"):
        _db.apply_migrations()
