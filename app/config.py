from pydantic_settings import BaseSettings
from typing import List
import os


class Settings(BaseSettings):
    # Volume configuration (comma-separated paths)
    chd_volumes: str = "/data/games"

    # Job limits
    max_concurrent_jobs: int = 2

    # chdman binary path
    chdman_path: str = "/usr/bin/chdman"

    class Config:
        env_prefix = ""
        case_sensitive = False

    @property
    def volumes(self) -> List[str]:
        """Parse CHD_VOLUMES into a list of paths."""
        return [v.strip() for v in self.chd_volumes.split(",") if v.strip()]

    def get_volume_name(self, path: str) -> str:
        """Extract a friendly name from a volume path."""
        return os.path.basename(path.rstrip("/")) or path


settings = Settings()
