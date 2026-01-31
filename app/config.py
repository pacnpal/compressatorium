"""Application configuration settings for the CHD converter."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and defaults."""

    model_config = SettingsConfigDict(
        populate_by_name=True,
        extra="ignore",
    )

    # Volume configuration (comma-separated paths)
    chd_volumes: str = Field(default="/data/games", alias="CHD_VOLUMES")

    # Persistent data directory
    data_dir: str = Field(default="/config", alias="CHD_DATA_DIR")

    # Job limits
    max_concurrent_jobs: int = Field(default=1, alias="MAX_CONCURRENT_JOBS")
    concurrency_lock_dir: str | None = Field(
        default=None,
        alias="CHD_CONCURRENCY_LOCK_DIR",
    )
    max_job_history: int = Field(default=500, alias="MAX_JOB_HISTORY")

    # Temporary working directory (archive extraction, etc.)
    temp_dir: str | None = Field(default=None, alias="CHD_TEMP_DIR")

    # Archive safety limits (listing + related extraction for cue/gdi)
    archive_max_entries: int = Field(default=5000, alias="CHD_ARCHIVE_MAX_ENTRIES")
    archive_max_member_size: int = Field(
        default=0,
        alias="CHD_ARCHIVE_MAX_MEMBER_SIZE",
    )
    archive_max_total_size: int = Field(
        default=0,
        alias="CHD_ARCHIVE_MAX_TOTAL_SIZE",
    )

    # chdman binary path
    chdman_path: str = Field(default="/usr/bin/chdman", alias="CHDMAN_PATH")

    # dolphin-tool binary path
    dolphin_tool_path: str = Field(
        default="/usr/local/bin/dolphin-tool", alias="DOLPHIN_TOOL_PATH",
    )
    chdman_nice: int | None = Field(default=10, alias="CHD_CHDMAN_NICE")
    chdman_ioprio_class: int | None = Field(
        default=2,
        alias="CHD_CHDMAN_IOPRIO_CLASS",
    )
    chdman_ioprio_level: int | None = Field(
        default=6,
        alias="CHD_CHDMAN_IOPRIO_LEVEL",
    )
    chdman_info_timeout: int = Field(default=60, alias="CHD_INFO_TIMEOUT")
    chdman_verify_timeout: int = Field(default=0, alias="CHD_VERIFY_TIMEOUT")
    verify_progress_timeout: int = Field(
        default=0,
        alias="CHD_VERIFY_PROGRESS_TIMEOUT",
    )

    # Debug logging
    debug: bool = Field(default=False, alias="CHD_DEBUG")
    debug_log_path: str | None = Field(default=None, alias="CHD_DEBUG_LOG_PATH")
    debug_heartbeat_interval: int = Field(default=30, alias="CHD_DEBUG_HEARTBEAT")
    debug_progress_interval: int = Field(
        default=30,
        alias="CHD_DEBUG_PROGRESS_INTERVAL",
    )
    debug_progress_timeout: int = Field(default=300, alias="CHD_DEBUG_PROGRESS_TIMEOUT")
    progress_timeout: int = Field(default=600, alias="CHD_PROGRESS_TIMEOUT")

    def model_post_init(self, __context: object, /) -> None:  # pylint: disable=arguments-differ
        """Set default paths after model initialization."""
        if self.temp_dir is None:
            self.temp_dir = str(Path(self.data_dir) / "temp")
        if self.concurrency_lock_dir is None:
            self.concurrency_lock_dir = str(Path(self.data_dir) / "locks")

    @property
    def volumes(self) -> list[str]:
        """Parse CHD_VOLUMES into a list of paths."""
        volumes = str(self.chd_volumes)
        return [v.strip() for v in volumes.split(",") if v.strip()]

    def get_volume_name(self, path: str) -> str:
        """Extract a friendly name from a volume path."""
        return Path(path.rstrip("/")).name or path


settings = Settings()
