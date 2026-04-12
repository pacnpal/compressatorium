import logging
import os

from config import settings
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from routes import convert, dat, files, info
from services.job_manager import job_manager


def get_version() -> str:
    """Return app version from APP_VERSION env var (set by Docker build arg).

    In production containers the version is injected at build time from the
    GitHub release tag.  Local / dev runs fall back to "dev".
    """
    return os.environ.get("APP_VERSION", "dev")


def configure_logging() -> None:
    # getLevelName() returns an int for known level names (DEBUG, INFO, WARNING,
    # ERROR, CRITICAL — plus legacy aliases WARN=WARNING and FATAL=CRITICAL)
    # and a "Level <name>" string for unknown values — unlike
    # getattr(logging, name, None) which can return non-integer logging
    # attributes (classes, format strings, etc.) for non-level names.
    level_str = settings.log_level.strip().upper()
    level = logging.getLevelName(level_str)
    invalid_level = not isinstance(level, int)
    if invalid_level:
        level = logging.INFO

    logger = logging.getLogger("chd")
    # Always update the level so re-calls (e.g. test isolation or reloads)
    # respect the current LOGLEVEL setting, even when handlers already exist.
    logger.setLevel(level)
    if logger.handlers:
        return

    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if settings.log_path:
        log_dir = os.path.dirname(settings.log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(settings.log_path)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    if invalid_level:
        logger.warning(
            "Unknown LOGLEVEL %r — defaulting to INFO. "
            "Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL",
            settings.log_level,
        )


app = FastAPI(
    title="Compressatorium",
    description="Web UI for converting game disc images using chdman and dolphin-tool",
    version=get_version(),
)

# Include routers
app.include_router(files.router, prefix="/api", tags=["files"])
app.include_router(convert.router, prefix="/api", tags=["convert"])
app.include_router(info.router, prefix="/api", tags=["info"])
app.include_router(dat.router, prefix="/api", tags=["dat"])


@app.on_event("startup")
async def startup_event():
    """Start background job processor."""
    import asyncio

    configure_logging()
    logger = logging.getLogger("chd")

    # Initialise the SQLite DB and run any pending JSON → SQLite migrations
    # BEFORE any store is touched (dat_store.has_dats() below needs it).
    from pathlib import Path
    from services import db as _db

    db_path = _db.resolve_db_path(settings.db_path, data_dir=settings.data_dir)
    # Initialise the engine *without* create_all — Alembic owns the
    # schema on the production path.  apply_migrations then stamps
    # pre-Alembic DBs or upgrades the rest.  Only after the schema is
    # guaranteed at head do we run the JSON→SQLite importers.
    _db.init_engine(db_path, create_schema=False)
    _db.apply_migrations()

    _config_dir = Path(settings.data_dir)

    def _resolve_legacy_json_store(env_var: str, default_filename: str) -> Path:
        """Return the JSON migration source path, honouring any legacy env-var override."""
        default_path = _config_dir / default_filename
        legacy_override = os.environ.get(env_var)
        if not legacy_override:
            return default_path
        resolved_path = Path(legacy_override)
        if not resolved_path.exists():
            logger.warning(
                "db.migrate: legacy JSON store override %s=%s does not exist; "
                "falling back to default store %s",
                env_var,
                resolved_path,
                default_path,
            )
            return default_path
        if not resolved_path.is_file() or not os.access(resolved_path, os.R_OK):
            logger.warning(
                "db.migrate: legacy JSON store override %s=%s is not a readable file; "
                "falling back to default store %s",
                env_var,
                resolved_path,
                default_path,
            )
            return default_path
        logger.info(
            "db.migrate: using legacy JSON store override for migration %s=%s",
            env_var,
            resolved_path,
        )
        if resolved_path != default_path and default_path.exists():
            logger.warning(
                "db.migrate: legacy JSON store override %s=%s will be used for migration; "
                "default store %s also exists and will not be imported in this run",
                env_var,
                resolved_path,
                default_path,
            )
        return resolved_path

    _db.init_and_migrate(
        db_path,
        dat_store_json=_resolve_legacy_json_store(
            "CHD_DAT_STORE", "dat_store.json",
        ),
        verification_json=_resolve_legacy_json_store(
            "CHD_VERIFICATION_STORE", "verified_chds.json",
        ),
        chd_metadata_json=_resolve_legacy_json_store(
            "CHD_METADATA_STORE", "chd_metadata.json",
        ),
        dat_sync_json=_resolve_legacy_json_store(
            "CHD_DAT_SYNC_STORE", "dat_sync.json",
        ),
    )
    explicit_volumes = [v.strip() for v in str(settings.chd_volumes).split(",") if v.strip()]
    discovered_volumes = [] if explicit_volumes else settings.scan_data_mounts_on_startup()
    logger.info(f"Compressatorium v{get_version()} starting...")
    logger.info(
        "Runtime limits pid=%s max_concurrent_jobs=%s max_job_history=%s lock_dir=%s",
        os.getpid(),
        settings.max_concurrent_jobs,
        settings.max_job_history,
        settings.concurrency_lock_dir,
    )
    logger.info(
        "Volume discovery mount_root=%s explicit=%s discovered=%s effective=%s",
        settings.data_mount_root,
        explicit_volumes,
        discovered_volumes,
        settings.volumes,
    )
    # Initialise the set used to hold strong references to background tasks so
    # they are not garbage-collected before they finish.
    app.state.background_tasks = set()

    process_queue_task = asyncio.create_task(job_manager.process_queue())
    app.state.background_tasks.add(process_queue_task)
    process_queue_task.add_done_callback(app.state.background_tasks.discard)

    def _log_process_queue_error(t: asyncio.Task) -> None:
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "process_queue task exited unexpectedly",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    process_queue_task.add_done_callback(_log_process_queue_error)

    # Auto-sync MAMERedump DATs on startup if configured and no DATs loaded.
    if settings.mameredump_auto_sync:
        from fastapi.concurrency import run_in_threadpool
        from services.dat_store import dat_store
        if not await run_in_threadpool(dat_store.has_dats):
            from services.dat_sync import dat_sync_service
            logger.info("MAMEREDUMP_AUTO_SYNC enabled and no DATs loaded — starting background sync")

            async def _auto_sync():
                try:
                    await dat_sync_service.sync()
                except Exception:
                    logger.exception("Auto-sync failed")

            # Store a strong reference so the Task is not garbage-collected
            # before it completes; done callback removes it.
            _auto_sync_task = asyncio.create_task(_auto_sync())
            app.state.background_tasks.add(_auto_sync_task)
            _auto_sync_task.add_done_callback(app.state.background_tasks.discard)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "version": get_version()}


# Serve static files
static_dir = os.environ.get("STATIC_DIR", "/static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """Serve the main HTML page."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, headers={"Cache-Control": "no-store"})
    return {"message": "Compressatorium API", "docs": "/docs"}
