"""Alembic environment for Compressatorium.

The SQLAlchemy URL is resolved dynamically from the same settings the
running app uses, so ``alembic upgrade`` always hits the same DB file
as the FastAPI process.  Callers may override the URL by setting the
``COMPRESSATORIUM_ALEMBIC_URL`` environment variable — primarily used
by tests that want to point alembic at a throwaway file without
clobbering the real ``COMPRESSATORIUM_DB_PATH``.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the app package importable regardless of where alembic is invoked from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "app"))
sys.path.insert(0, str(_REPO_ROOT))

from services import db as _db  # noqa: E402  (sys.path manipulation above)


config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False is critical: the app's "chd.*"
    # loggers are already configured by the time Alembic runs (both
    # at startup and in tests via caplog).  The default True would
    # silence them as a side-effect of Alembic config.
    fileConfig(config.config_file_name, disable_existing_loggers=False)


# Resolve the DB URL.  Precedence:
#   1. ``COMPRESSATORIUM_ALEMBIC_URL`` env var (tests, ad-hoc CLI ops).
#   2. ``main_option("sqlalchemy.url")`` — explicitly set by the app via
#      ``_alembic_config()`` or in ``alembic.ini``.
#   3. Runtime settings (``settings.db_path``).
def _resolve_url() -> str:
    override = os.environ.get("COMPRESSATORIUM_ALEMBIC_URL")
    if override:
        return override

    cli_url = config.get_main_option("sqlalchemy.url")
    if cli_url:
        return cli_url

    try:
        from config import settings  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover — only when settings aren't importable
        from app.config import settings  # type: ignore[no-redef]
    path = _db.resolve_db_path(settings.db_path, data_dir=settings.data_dir)
    return f"sqlite:///{path}"


target_metadata = _db.Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing against a live DB."""
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite-friendly (ALTER TABLE emulation).
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live engine.

    If the caller injected a connection via ``config.attributes["connection"]``
    (e.g. ``apply_migrations()`` in ``app/services/db.py``), that connection is
    reused so migrations run against the same DB as the already-open engine.
    This is important for ``sqlite:///:memory:`` targets and ensures any
    connection-level PRAGMAs set by the application carry through.
    """
    injected_conn = context.config.attributes.get("connection")
    if injected_conn is not None:
        # Reuse the connection provided by the application.
        context.configure(
            connection=injected_conn,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    # Normal path (CLI / ad-hoc scripts): build a fresh engine from the URL.
    url = _resolve_url()
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
