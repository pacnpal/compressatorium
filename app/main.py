import logging
import os
import shutil
from pathlib import Path

from config import settings
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from routes import convert, files, igir, info
from services.job_manager import job_manager


class CacheControlledStaticFiles(StaticFiles):
    """Static files with explicit JS module cache revalidation."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code=status_code)
        request_path = scope.get("path", "")
        if isinstance(request_path, str) and request_path.startswith("/static/js/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def get_version() -> str:
    """Read version from .version file at project root."""
    # Try multiple possible locations for the .version file
    possible_paths = [
        Path(__file__).parent.parent / ".version",  # /app/../.version
        Path("/app/.version"),  # Docker container path
        Path(".version"),  # Current working directory
    ]
    for version_path in possible_paths:
        try:
            if version_path.exists():
                return version_path.read_text().strip()
        except OSError:
            # File exists but unreadable, try next location
            continue
    return "0.0.0"


def configure_logging() -> None:
    logger = logging.getLogger("chd")
    if logger.handlers:
        return

    level = logging.DEBUG if settings.debug else logging.INFO
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if settings.debug_log_path:
        log_dir = os.path.dirname(settings.debug_log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(settings.debug_log_path)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


app = FastAPI(
    title="Compressatorium",
    description="Web UI for converting game disc images using chdman and dolphin-tool",
    version=get_version(),
)

# Include routers
app.include_router(files.router, prefix="/api", tags=["files"])
app.include_router(convert.router, prefix="/api", tags=["convert"])
app.include_router(info.router, prefix="/api", tags=["info"])
app.include_router(igir.router, prefix="/api", tags=["igir"])


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
    asyncio.create_task(job_manager.process_queue())


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    configured_igir = str(settings.igir_path or "").strip()
    if configured_igir and os.path.isabs(configured_igir):
        igir_available = os.path.isfile(configured_igir) and os.access(
            configured_igir, os.X_OK,
        )
    elif configured_igir:
        igir_available = shutil.which(configured_igir) is not None
    else:
        igir_available = False
    return {
        "status": "healthy",
        "version": get_version(),
        "igir_available": igir_available,
    }


# Serve static files
static_dir = os.environ.get("STATIC_DIR", "/static")
if os.path.exists(static_dir):
    app.mount("/static", CacheControlledStaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """Serve the main HTML page."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, headers={"Cache-Control": "no-store"})
    return {"message": "Compressatorium API", "docs": "/docs"}
