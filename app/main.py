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

    # Auto-sync MAMERedump DATs on startup if configured and no DATs loaded.
    if settings.mameredump_auto_sync:
        from services.dat_store import dat_store
        if not dat_store.has_dats():
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
