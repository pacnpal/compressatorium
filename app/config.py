"""Application configuration settings for the CHD converter."""

import os
from pathlib import Path

from pydantic import AliasChoices, Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and defaults."""

    model_config = SettingsConfigDict(
        populate_by_name=True,
        extra="ignore",
    )
    _startup_discovered_volumes: list[str] = PrivateAttr(default_factory=list)
    _startup_scan_completed: bool = PrivateAttr(default=False)

    # Volume configuration. Prefer COMPRESSATORIUM_* names; CHD_* remain supported aliases.
    chd_volumes: str = Field(
        default="",
        alias="COMPRESSATORIUM_VOLUMES",
        validation_alias=AliasChoices("COMPRESSATORIUM_VOLUMES", "CHD_VOLUMES"),
    )
    data_mount_root: str = Field(
        default="/data",
        alias="COMPRESSATORIUM_MOUNT_ROOT",
        validation_alias=AliasChoices("COMPRESSATORIUM_MOUNT_ROOT", "CHD_MOUNT_ROOT"),
    )

    # Persistent data directory
    data_dir: str = Field(default="/config", alias="CHD_DATA_DIR")

    # Web UI behavior
    search_auto_return_to_file_list: bool = Field(
        default=True,
        alias="COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST",
            "CHD_SEARCH_AUTO_RETURN_TO_FILE_LIST",
        ),
    )

    # Job limits
    max_concurrent_jobs: int = Field(default=1, alias="MAX_CONCURRENT_JOBS")
    max_queue_depth: int = Field(
        default=0,
        alias="MAX_QUEUE_DEPTH",
        description="Maximum queued+processing conversion jobs (0 disables backpressure)",
    )
    max_verify_concurrency: int = Field(
        default=1,
        alias="MAX_VERIFY_CONCURRENCY",
        description="Maximum concurrent verify workloads across all endpoints",
    )
    max_metadata_scan_concurrency: int = Field(
        default=1,
        alias="MAX_METADATA_SCAN_CONCURRENCY",
        description="Maximum concurrent metadata scan tasks",
    )
    concurrency_lock_dir: str | None = Field(
        default=None,
        alias="CHD_CONCURRENCY_LOCK_DIR",
        description="Directory for job lock files (default: ephemeral /tmp subdirectory, auto-cleaned on restart)",
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

    # z3ds_compressor binary path
    z3ds_compressor_path: str = Field(
        default="/usr/local/bin/z3ds_compressor", alias="Z3DS_COMPRESSOR_PATH",
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

    # Logging
    log_level: str = Field(default="INFO", alias="LOGLEVEL")
    log_path: str | None = Field(
        default=None,
        alias="LOG_PATH",
        validation_alias=AliasChoices("LOG_PATH", "CHD_DEBUG_LOG_PATH"),
    )
    debug_heartbeat_interval: int = Field(default=30, alias="CHD_DEBUG_HEARTBEAT")
    debug_progress_interval: int = Field(
        default=30,
        alias="CHD_DEBUG_PROGRESS_INTERVAL",
    )
    debug_progress_timeout: int = Field(default=300, alias="CHD_DEBUG_PROGRESS_TIMEOUT")
    progress_timeout: int = Field(default=600, alias="CHD_PROGRESS_TIMEOUT")
    progress_timeout_per_gib: int = Field(
        default=120,
        alias="CHD_PROGRESS_TIMEOUT_PER_GIB",
    )
    progress_timeout_cap: int = Field(
        default=7200,
        alias="CHD_PROGRESS_TIMEOUT_CAP",
    )

    def model_post_init(self, __context: object, /) -> None:  # pylint: disable=arguments-differ
        """Set default paths after model initialization."""
        if self.temp_dir is None:
            self.temp_dir = str(Path(self.data_dir) / "temp")
        if self.concurrency_lock_dir is None:
            # Use a subdirectory under /tmp for ephemeral lock storage
            # This is secure in the container context because:
            # 1. Container filesystem is isolated
            # 2. Non-root user runs the application
            # 3. Directory permissions are set restrictively (0o700) during creation
            #    in concurrency_manager.py and lock_manager.py, mitigating Bandit_B108
            #    concerns about predictable temporary paths
            # 4. Locks don't persist across container restarts
            # The fixed path is intentional to allow multiple processes to share locks
            self.concurrency_lock_dir = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'chd-locks')
        # CHD_DEBUG=true backwards compatibility: map to LOGLEVEL=DEBUG when LOGLEVEL
        # is not explicitly set in the environment and log_level was not explicitly
        # provided to Settings (e.g. via constructor argument in tests).
        # This ensures explicit config always wins over the legacy env-var fallback.
        if (
            os.environ.get("CHD_DEBUG", "").lower() == "true"
            and "LOGLEVEL" not in os.environ
            and "log_level" not in getattr(self, "__pydantic_fields_set__", set())
        ):
            self.log_level = "DEBUG"

    @property
    def volumes(self) -> list[str]:
        """Return explicit volume list when set, otherwise auto-discover /data/*."""
        explicit = str(self.chd_volumes).strip()
        if explicit:
            return [v.strip() for v in explicit.split(",") if v.strip()]
        if self._startup_scan_completed:
            return list(self._startup_discovered_volumes)
        return self.discover_data_volumes()

    def scan_data_mounts_on_startup(self) -> list[str]:
        """Capture discovered volumes once during startup for stable runtime behavior."""
        self._startup_discovered_volumes = self.discover_data_volumes()
        self._startup_scan_completed = True
        return list(self._startup_discovered_volumes)

    def discover_data_volumes(self) -> list[str]:
        """Return configured data volumes discovered from data_mount_root.

        Discovery strategy:
        1. Scan direct subdirectories under `data_mount_root`.
        2. If one or more entries are mount points, use only mount points.
        3. Otherwise use all direct subdirectories.
        4. If no subdirectories exist, allow `data_mount_root` itself when present.
        """
        root_path = Path(str(self.data_mount_root)).expanduser()
        try:
            resolved_root = root_path.resolve(strict=True)
        except (OSError, RuntimeError):
            return []

        if not resolved_root.is_dir():
            return []

        children: list[Path] = []
        try:
            children = sorted(
                [p for p in resolved_root.iterdir() if p.is_dir()],
                key=lambda p: p.name.lower(),
            )
        except OSError:
            return []

        mount_children = [p for p in children if os.path.ismount(str(p))]
        selected = mount_children if mount_children else children

        if not selected:
            return [str(resolved_root)]

        return [str(path) for path in selected]

    def get_volume_name(self, path: str) -> str:
        """Extract a friendly name from a volume path."""
        return Path(path.rstrip("/")).name or path


settings = Settings()
