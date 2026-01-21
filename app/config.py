from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional
import os


class Settings(BaseSettings):
    # Volume configuration (comma-separated paths)
    chd_volumes: str = Field(default="/data/games", alias="CHD_VOLUMES")

    # Job limits
    max_concurrent_jobs: int = Field(default=2, alias="MAX_CONCURRENT_JOBS")
    concurrency_lock_dir: str = Field(default="/tmp/chd_converter_locks", alias="CHD_CONCURRENCY_LOCK_DIR")
    max_job_history: int = Field(default=500, alias="MAX_JOB_HISTORY")

    # chdman binary path
    chdman_path: str = Field(default="/usr/bin/chdman", alias="CHDMAN_PATH")
    chdman_nice: Optional[int] = Field(default=10, alias="CHD_CHDMAN_NICE")
    chdman_ioprio_class: Optional[int] = Field(default=2, alias="CHD_CHDMAN_IOPRIO_CLASS")
    chdman_ioprio_level: Optional[int] = Field(default=6, alias="CHD_CHDMAN_IOPRIO_LEVEL")

    # Debug logging
    debug: bool = Field(default=False, alias="CHD_DEBUG")
    debug_log_path: Optional[str] = Field(default=None, alias="CHD_DEBUG_LOG_PATH")
    debug_heartbeat_interval: int = Field(default=30, alias="CHD_DEBUG_HEARTBEAT")
    debug_progress_interval: int = Field(default=30, alias="CHD_DEBUG_PROGRESS_INTERVAL")
    debug_progress_timeout: int = Field(default=300, alias="CHD_DEBUG_PROGRESS_TIMEOUT")

    class Config:
        populate_by_name = True
        extra = "ignore"

    @property
    def volumes(self) -> List[str]:
        """Parse CHD_VOLUMES into a list of paths."""
        return [v.strip() for v in self.chd_volumes.split(",") if v.strip()]

    def get_volume_name(self, path: str) -> str:
        """Extract a friendly name from a volume path."""
        return os.path.basename(path.rstrip("/")) or path


settings = Settings()
