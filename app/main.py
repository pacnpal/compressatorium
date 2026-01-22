from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
import logging

from routes import files, convert, info
from services.job_manager import job_manager
from config import settings


def configure_logging():
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
    title="CHD Converter",
    description="Web UI for converting game disc images to CHD format",
    version="1.0.0",
)

# Include routers
app.include_router(files.router, prefix="/api", tags=["files"])
app.include_router(convert.router, prefix="/api", tags=["convert"])
app.include_router(info.router, prefix="/api", tags=["info"])


@app.on_event("startup")
async def startup_event():
    """Start background job processor."""
    import asyncio

    configure_logging()
    asyncio.create_task(job_manager.process_queue())


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


# Serve static files
static_dir = os.environ.get("STATIC_DIR", "/static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """Serve the main HTML page."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "CHD Converter API", "docs": "/docs"}
