"""Application configuration settings for the CHD converter."""

import os
from pathlib import Path

from pydantic import AliasChoices, Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


# Used only when no data dir is configured at all (a dev checkout without
# /config). One fixed path, whatever TMPDIR says, so every process on the host
# picks the same one. Bandit B108: a fixed path is intended here;
# lock_manager and concurrency_manager create it 0o700.
_UNCONFIGURED_LOCK_DIR = "/tmp/chd-locks"  # nosec B108


def _default_lock_dir(data_dir: str, *, configured: bool) -> str:
    """Where job lock files live when ``CHD_CONCURRENCY_LOCK_DIR`` is unset.

    A pure function of configuration, never of whether a folder exists yet
    (#306): every process that shares the data dir (the server,
    ``docker exec compressatorium ...``, a separate CLI container) must pick the
    same lock dir, or none of their output, input, folder and scheduler locks
    meet. With a data dir configured it is always ``<data dir>/locks``, created
    here (parents included, owner only); one that can't be created stops the
    process with a clear error rather than putting the locks where no other
    process looks. Only with no data dir configured at all is it the one fixed
    ``_UNCONFIGURED_LOCK_DIR``.
    """
    if not configured:
        return _UNCONFIGURED_LOCK_DIR
    lock_dir = os.path.join(data_dir, "locks")
    try:
        os.makedirs(lock_dir, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create the job lock directory {lock_dir}: {exc}. Make "
            "CHD_DATA_DIR writable by this user, or set CHD_CONCURRENCY_LOCK_DIR "
            "to a directory every process shares.",
        ) from exc
    if not os.access(lock_dir, os.W_OK | os.X_OK):
        raise RuntimeError(
            f"The job lock directory {lock_dir} is not writable by this user. Make "
            "CHD_DATA_DIR writable, or set CHD_CONCURRENCY_LOCK_DIR to a directory "
            "every process shares.",
        )
    return lock_dir


# Retired names (#542), still read so no deployment breaks; each one set is
# named in one startup warning (``Settings.retired_env``).
# Per-tool overrides: each feeds the shared ``tool_<key>`` setting when that is
# not set itself (two that disagree: the largest).
_RETIRED_PER_TOOL = {
    key: tuple(
        f"COMPRESSATORIUM_{tool}_{key.upper()}" for tool in (
            "CHDMAN", "DOLPHIN_TOOL", "NSZ", "Z3DS", "MAXCSO", "MAKEPS3ISO",
            "ROMZ", "JWUD", "NKIT2ISO", "XISO", "ROM_CONVERTO",
        )
    )
    for key in ("nice", "ioprio_class", "ioprio_level", "info_timeout", "verify_timeout")
}
# No longer read at all: fixed timings, and a choice the Web UI now remembers.
_RETIRED_IGNORED = (
    "CHD_DEBUG_PROGRESS_INTERVAL", "CHD_DEBUG_PROGRESS_TIMEOUT",
    "COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST",
    "CHD_SEARCH_AUTO_RETURN_TO_FILE_LIST",
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables and defaults."""

    model_config = SettingsConfigDict(
        populate_by_name=True,
        extra="ignore",
    )
    _startup_discovered_volumes: list[str] = PrivateAttr(default_factory=list)
    _startup_scan_completed: bool = PrivateAttr(default=False)
    _retired_env: list[str] = PrivateAttr(default_factory=list)

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

    # Unified SQLite database path.  Defaults to <data_dir>/compressatorium.db.
    db_path: str | None = Field(
        default=None,
        alias="COMPRESSATORIUM_DB_PATH",
        description="SQLite database file; default: <CHD_DATA_DIR>/compressatorium.db",
    )

    # Web UI authentication. Disabled by default; set COMPRESSATORIUM_ENABLE_AUTH=true
    # to require a token for the Web UI and /api routes. When enabled, set a token
    # explicitly or the app creates a persistent token in CHD_DATA_DIR/auth_token.
    enable_auth: bool = Field(default=False, alias="COMPRESSATORIUM_ENABLE_AUTH")
    auth_token: str | None = Field(
        default=None,
        alias="COMPRESSATORIUM_AUTH_TOKEN",
        validation_alias=AliasChoices("COMPRESSATORIUM_AUTH_TOKEN", "CHD_AUTH_TOKEN"),
    )
    auth_username: str = Field(default="admin", alias="COMPRESSATORIUM_AUTH_USERNAME")

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
    max_match_concurrency: int = Field(
        default=1,
        alias="MAX_MATCH_CONCURRENCY",
        description=(
            "Maximum concurrent DAT-match hashing operations. Matching a raw "
            "ISO/WBFS requires full-file SHA1; bound this to protect "
            "disk/CPU when many uncached files are browsed at once."
        ),
    )
    match_max_file_size: int = Field(
        default=0,
        alias="MATCH_MAX_FILE_SIZE",
        description=(
            "If non-zero, skip DAT hash-matching for files larger than this "
            "many bytes. 0 disables the cap (match any size)."
        ),
    )
    concurrency_lock_dir: str | None = Field(
        default=None,
        alias="CHD_CONCURRENCY_LOCK_DIR",
        description=(
            "Directory for job lock files, conversion slots and the Rule scheduler "
            "lock. Default: always <data dir>/locks when a data dir is configured "
            "(shared by every container mounting /config); startup fails if it "
            "can't be created. /tmp/chd-locks only when no data dir is configured "
            "at all."
        ),
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
    # An archive an Inbox takes in (#431, Codex 4607318277): anyone who can
    # write the Inbox can drop one, so it is never checked past these. The
    # declared uncompressed size, per member and in all, where
    # CHD_ARCHIVE_MAX_MEMBER_SIZE / CHD_ARCHIVE_MAX_TOTAL_SIZE are unlimited
    # (either one set wins); and how far it may expand, uncompressed to
    # compressed, per member and in all. 0 turns either off.
    archive_inbox_max_total_size: int = Field(
        default=32 * 1024 ** 3,
        alias="CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE",
    )
    archive_inbox_max_ratio: int = Field(
        default=200,
        alias="CHD_ARCHIVE_INBOX_MAX_RATIO",
    )
    # What every archive an Inbox has unpacked at once may hold in all, in
    # the work area (#432, Codex 4608429476): one more waits until there is
    # room. Unset, it is CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE; 0 turns it off.
    archive_inbox_max_unpacked_size: int | None = Field(
        default=None,
        alias="CHD_ARCHIVE_INBOX_MAX_UNPACKED_SIZE",
    )

    # The Patch action (#428). A patch file over this is refused (an upload
    # stops reading at it), and so is a ROM, or a patched ROM, over the other.
    # The engine holds the ROM, the patch and the output in memory at once.
    patch_max_patch_size: int = Field(
        default=64 * 1024 * 1024, alias="CHD_PATCH_MAX_PATCH_SIZE",
    )
    patch_max_rom_size: int = Field(
        default=512 * 1024 * 1024, alias="CHD_PATCH_MAX_ROM_SIZE",
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

    # maxcso binary path (PSP/PS2 ISO <-> CSO/ZSO). Built from source into the
    # image at /usr/local/bin/maxcso; set MAXCSO_PATH to relocate/override.
    maxcso_path: str = Field(
        default="/usr/local/bin/maxcso", alias="MAXCSO_PATH",
    )

    # makeps3iso binary path (decrypted PS3 disc/JB folder -> .iso). Built from
    # source (bucanero/ps3iso-utils, GPL-3.0) into the image at
    # /usr/local/bin/makeps3iso; set MAKEPS3ISO_PATH to relocate/override.
    makeps3iso_path: str = Field(
        default="/usr/local/bin/makeps3iso", alias="MAKEPS3ISO_PATH",
    )

    # JWUDTool launcher (Wii U .wud <-> .wux). JWUDTool is a Java .jar, so the
    # image ships a tiny `exec java -jar ...` launcher at /usr/local/bin/jwudtool
    # next to the jar; set JWUDTOOL_PATH to point at your own launcher/wrapper
    # (e.g. for a local checkout without the image's JRE).
    jwudtool_path: str = Field(
        default="/usr/local/bin/jwudtool", alias="JWUDTOOL_PATH",
    )
    # nkit2iso binary path (NKit-shrunk GameCube/Wii image -> plain .iso). Built
    # from the vendored source in third_party/nkit2iso (DonMikone/nkit2iso, MIT)
    # into the image at /usr/local/bin/nkit2iso; set NKIT2ISO_PATH to
    # relocate/override.
    nkit2iso_path: str = Field(
        default="/usr/local/bin/nkit2iso", alias="NKIT2ISO_PATH",
    )
    # What nkit2iso does with a Wii image whose update partition was removed at
    # shrink time: that data is not in the file and cannot be regenerated.
    #   "none"     - zero-fill the region (default). Fully offline; the result is
    #                playable but not redump-verifiable, and the CRC32 check is
    #                skipped, so the job reports the caveat in its final message.
    #   "download" - fetch the publicly archived recovery partition and splice it
    #                in for a bit-exact restore. This is the ONLY situation in
    #                which nkit2iso touches the network, so it stays opt-in: a
    #                converter should not reach out to the internet unless the
    #                operator asked it to.
    # The tool's own "ask" mode is deliberately not offered — it prompts on a
    # terminal, and a job worker has no one to answer it.
    nkit2iso_recovery: str = Field(
        default="none", alias="NKIT2ISO_RECOVERY",
        pattern="^(none|download)$",
    )
    # extract-xiso binary path (Xbox / Xbox 360 disc image -> trimmed XISO).
    # Built from source (XboxDev/extract-xiso) into the image at
    # /usr/local/bin/extract-xiso; set EXTRACT_XISO_PATH to relocate/override.
    extract_xiso_path: str = Field(
        default="/usr/local/bin/extract-xiso", alias="EXTRACT_XISO_PATH",
    )
    # rom-converto binary path (DevYukine/rom-converto, the converter RomM
    # ships; Xbox 360 ISO -> ZAR so far). The image downloads the pinned
    # release (v0.24.0, SHA-256 checked) to /usr/local/bin/rom-converto; set
    # ROM_CONVERTO_PATH to relocate/override.
    rom_converto_path: str = Field(
        default="/usr/local/bin/rom-converto", alias="ROM_CONVERTO_PATH",
    )

    # Extra free space (MB) a chained conversion (e.g. cso->iso->chd) keeps
    # beyond its estimated peak before starting. A chain holds source + full
    # intermediate + partial final at once, so it preflights disk headroom on
    # both the work-dir and output-dir volumes; this is the per-volume cushion.
    chain_disk_margin_mb: int = Field(
        default=512, alias="COMPRESSATORIUM_CHAIN_DISK_MARGIN_MB",
    )

    # nsz (Nintendo Switch NSP/XCI <-> NSZ/XCZ). Installed via pip into the
    # venv, so the console script lives on PATH (/opt/venv/bin in Docker, .venv
    # locally). A bare name resolves in both; set NSZ_PATH to pin an absolute
    # path if needed.
    nsz_path: str = Field(default="nsz", alias="NSZ_PATH")
    # Directory holding the operator's own Switch keys (a `prod.keys`, or a
    # `keys.txt`). nsz needs console keys to decrypt the NCA content before
    # compressing (and to re-encrypt on decompress). We ship NO keys: the
    # operator mounts their own and points SWITCH_KEYS at the directory. This is
    # the source of truth when set. When unset, the app best-effort searches the
    # standard locations (~/.switch, ~/.config/nsz) at runtime.
    switch_keys_dir: str | None = Field(default=None, alias="SWITCH_KEYS")
    # Optional override for eXoDOS's per-game template tree (the `!dos` folder
    # holding `<Game>/dosbox.conf`). Unset is normal: the repack finds `!dos`
    # beside each game zip or in a parent folder, up to the volume (#513).
    # When set, it is searched first, and must sit inside a configured volume.
    # The operator supplies their own eXoDOS; nothing is shipped or downloaded.
    exodos_dos_dir: str | None = Field(default=None, alias="EXODOS_DOS_DIR")
    # zstandard level for nsz compression (1-22, nsz default is 18).
    nsz_compression_level: int = Field(
        default=18, alias="NSZ_COMPRESSION_LEVEL", ge=1, le=22,
    )

    # 7z CLI (handheld ROM .gb/.gbc/.gba/.nds <-> .7z/.zip). Ships in the image
    # via the p7zip-full package; the binary is `7z`. A bare name resolves on
    # PATH; set SEVENZIP_PATH to pin an absolute path (e.g. `7zz` on distros that
    # ship the newer `7zip` package instead).
    sevenzip_path: str = Field(default="7z", alias="SEVENZIP_PATH")

    # unar (The Unarchiver), the rar tool's extractor (#476): a multi-volume
    # RAR4/RAR5 set to the one file inside it. Ships in the image via the
    # Debian `unar` package; a bare name resolves on PATH.
    unar_path: str = Field(default="unar", alias="UNAR_PATH")

    # MAMERedump DAT sync
    mameredump_repo: str = Field(
        default="MetalSlug/MAMERedump", alias="MAMEREDUMP_REPO",
    )
    mameredump_auto_sync: bool = Field(
        default=False, alias="MAMEREDUMP_AUTO_SYNC",
        description="Auto-sync DATs from MAMERedump on startup if none loaded",
    )

    # Hasheous remote hash lookup (https://hasheous.org).
    #
    # Consulted ONLY when the locally imported DATs don't know a hash, so local
    # matching stays instant and fully offline. Off by default on purpose: a
    # lookup sends the SHA1 of the operator's file to a third-party service,
    # which has to be a deliberate choice rather than a default.
    hasheous_enabled: bool = Field(
        default=False, alias="COMPRESSATORIUM_HASHEOUS_ENABLED",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_HASHEOUS_ENABLED", "HASHEOUS_ENABLED",
        ),
        description=(
            "Fall back to Hasheous when a hash isn't in the imported DATs. "
            "Sends file SHA1s to hasheous_base_url; off by default."
        ),
    )
    hasheous_base_url: str = Field(
        default="https://hasheous.org", alias="COMPRESSATORIUM_HASHEOUS_URL",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_HASHEOUS_URL", "HASHEOUS_URL",
        ),
        description=(
            "Base URL of the Hasheous server. Hasheous is self-hostable; point "
            "this at your own instance to keep lookups on your network."
        ),
    )
    hasheous_timeout: int = Field(
        default=15, alias="COMPRESSATORIUM_HASHEOUS_TIMEOUT",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_HASHEOUS_TIMEOUT", "HASHEOUS_TIMEOUT",
        ),
        description=(
            "Per-request timeout (seconds) for Hasheous lookups. This call sits "
            "in the file-browse path, so it must stay bounded."
        ),
    )

    # RomM catalog overlay. Compressatorium reads RomM's catalog over HTTP but
    # touches the ROM bytes only through the filesystem: RomM's library is
    # mounted as an ordinary Compressatorium volume (locally via the same bind
    # mount, remotely via NFS/SMB/rclone), so no ROM ever crosses the network.
    #
    # None of its settings are Settings fields. They are runtime-editable in
    # the app (URL, library root, unattended-conversion policy, per-platform
    # rules), which makes the saved value the authority and the environment
    # only the first-run default -- a layering `Settings` cannot express, since
    # it is populated once at import. `services.romm.settings` owns the whole
    # table: ROMM_URL, ROMM_LIBRARY_ROOT, ROMM_AUTO_CONVERT,
    # ROMM_AUTO_CONVERT_INTERVAL_MINUTES, ROMM_AUTO_CONVERT_MAX_PER_RUN,
    # ROMM_REPIN*, ROMM_VERIFY_AFTER_CONVERT, ROMM_DELETE_SOURCE_AFTER_VERIFY.
    # Duplicating any of them here would create a second source of truth that
    # nothing reads and that silently drifts from the one that matters.
    #
    # The credential is likewise not here, for the additional reason that
    # applies to MAMEREDUMP_GITHUB_TOKEN: a secret on the settings singleton
    # ends up in every repr()/log line that dumps config.

    # Process-priority and timeout policy shared by EVERY conversion tool's
    # subprocess (chdman, Dolphin, 3DS, Switch) and the shared SubprocessRunner.
    # These began life as chdman-only knobs; prefer the tool-neutral
    # COMPRESSATORIUM_* names. The chdman-era CHD_*/CHDMAN_* names remain
    # supported as backwards-compatible validation aliases (the same pattern as
    # chd_volumes -> COMPRESSATORIUM_VOLUMES).
    #
    # One value for every tool (#542). The old per-tool overrides
    # (COMPRESSATORIUM_<TOOL>_NICE and so on, _RETIRED_PER_TOOL) still feed these.
    tool_nice: int | None = Field(
        default=10,
        alias="COMPRESSATORIUM_TOOL_NICE",
        validation_alias=AliasChoices("COMPRESSATORIUM_TOOL_NICE", "CHD_CHDMAN_NICE"),
    )
    tool_ioprio_class: int | None = Field(
        default=2,
        alias="COMPRESSATORIUM_TOOL_IOPRIO_CLASS",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_IOPRIO_CLASS", "CHD_CHDMAN_IOPRIO_CLASS",
        ),
    )
    tool_ioprio_level: int | None = Field(
        default=6,
        alias="COMPRESSATORIUM_TOOL_IOPRIO_LEVEL",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_IOPRIO_LEVEL", "CHD_CHDMAN_IOPRIO_LEVEL",
        ),
    )
    tool_info_timeout: int = Field(
        default=60,
        alias="COMPRESSATORIUM_TOOL_INFO_TIMEOUT",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_INFO_TIMEOUT", "CHD_INFO_TIMEOUT",
        ),
    )
    # Verify is bounded the same adaptive way a conversion is: a *baseline* plus
    # an allowance per GiB of the file being read, capped. A flat bound cannot
    # serve both a 400 MB CIA and a 90 GB PS3 ISO -- one is generous, the other
    # kills healthy work -- because verify reads the whole file, so its runtime
    # scales with size. Defaults are deliberately loose (30 min + 10 min/GiB,
    # i.e. a ~1.7 MB/s floor, capped at 24h): the point is that a verify wedged
    # on dead storage *ends*, not that a slow one is policed. Set
    # COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_PER_GIB=0 for a flat bound, or
    # COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT=0 to disable *this* bound (issue #266
    # -- it used to default to 0, so a verify that never returned never ended,
    # freezing the whole queue behind it). The stall bound below is a separate
    # knob and stays on when this is zeroed, deliberately: an operator raising
    # or removing the overall bound for a huge image still wants a verifier that
    # has gone completely silent to be caught. Zero both for no bound at all.
    tool_verify_timeout: int = Field(
        default=1800,
        alias="COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT", "CHD_VERIFY_TIMEOUT",
        ),
    )
    tool_verify_timeout_per_gib: int = Field(
        default=600,
        alias="COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_PER_GIB",
    )
    tool_verify_timeout_cap: int = Field(
        default=86400,
        alias="COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT_CAP",
    )
    # Stall bound for verifiers that stream progress (chdman, dolphin-tool):
    # no output at all for this long means wedged, and it catches a hang far
    # sooner than the size-scaled overall bound above. Mirrors the conversion
    # path's CHD_PROGRESS_TIMEOUT default, and is independent of it in the same
    # way -- zeroing the overall verify timeout does not zero this.
    tool_verify_progress_timeout: int = Field(
        default=600,
        alias="COMPRESSATORIUM_TOOL_VERIFY_PROGRESS_TIMEOUT",
        validation_alias=AliasChoices(
            "COMPRESSATORIUM_TOOL_VERIFY_PROGRESS_TIMEOUT",
            "CHD_VERIFY_PROGRESS_TIMEOUT",
        ),
    )

    # Logging
    log_level: str = Field(default="INFO", alias="LOGLEVEL")
    log_path: str | None = Field(
        default=None,
        alias="LOG_PATH",
        validation_alias=AliasChoices("LOG_PATH", "CHD_DEBUG_LOG_PATH"),
    )
    log_color: str = Field(
        default="always",
        alias="LOG_COLOR",
        description=(
            "ANSI-color the stdout log stream: 'always' (default), 'auto' "
            "(TTY + no NO_COLOR env), or 'never'. File logs are never colored. "
            "Default is 'always' so `docker logs` is colored out of the box; "
            "set 'never' to opt out, or 'auto' to follow TTY / NO_COLOR."
        ),
    )
    debug_heartbeat_interval: int = Field(default=30, alias="CHD_DEBUG_HEARTBEAT")
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
        self._apply_retired_env()
        if self.temp_dir is None:
            self.temp_dir = str(Path(self.data_dir) / "temp")
        if self.concurrency_lock_dir is None:
            # "Configured" means named by CHD_DATA_DIR or the constructor; the
            # image sets CHD_DATA_DIR=/config, so a container always is.
            self.concurrency_lock_dir = _default_lock_dir(
                self.data_dir, configured="data_dir" in self.model_fields_set,
            )
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

    def _apply_retired_env(self) -> None:
        """Read the retired names in the environment (``_RETIRED_PER_TOOL``)."""
        for key, names in _RETIRED_PER_TOOL.items():
            values = []
            for name in names:
                raw = os.environ.get(name)
                if raw is None:
                    continue
                self._retired_env.append(name)
                try:
                    values.append(int(raw))
                except ValueError:
                    continue
            if values and f"tool_{key}" not in self.model_fields_set:
                setattr(self, f"tool_{key}", max(values))
        self._retired_env += [name for name in _RETIRED_IGNORED if name in os.environ]

    @property
    def retired_env(self) -> list[str]:
        """Retired environment names this process was started with."""
        return list(self._retired_env)

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
        1. Scan direct non-symlink subdirectories under `data_mount_root`.
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
                [p for p in resolved_root.iterdir() if not p.is_symlink() and p.is_dir()],
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
