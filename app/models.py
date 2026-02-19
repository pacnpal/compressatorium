from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class ConversionMode(str, Enum):
    CREATERAW = "createraw"
    CREATEHD = "createhd"
    CREATECD = "createcd"
    CREATEDVD = "createdvd"
    CREATELD = "createld"
    EXTRACTRAW = "extractraw"
    EXTRACTHD = "extracthd"
    EXTRACTCD = "extractcd"
    EXTRACTDVD = "extractdvd"
    EXTRACTLD = "extractld"
    COPY = "copy"
    DOLPHIN_RVZ = "dolphin_rvz"
    DOLPHIN_WIA = "dolphin_wia"
    DOLPHIN_GCZ = "dolphin_gcz"
    DOLPHIN_ISO = "dolphin_iso"
    Z3DS_COMPRESS = "z3ds_compress"


class DuplicateAction(str, Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    RENAME = "rename"


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileEntry(BaseModel):
    name: str
    path: str
    type: str  # "file", "directory", or "archive"
    size: int | None = None
    extension: str | None = None
    convertible: bool = False
    has_chd: bool = False
    has_rvz: bool = False
    dolphin_ready: bool = False
    dolphin_path: str | None = None
    chd_ready: bool = False
    dolphin_convertible: bool = False
    z3ds_convertible: bool = False
    has_z3ds: bool = False
    z3ds_ready: bool = False
    z3ds_path: str | None = None
    archive_items: int | None = None
    archive_has_chd: int | None = None
    archive_truncated: bool | None = None
    media_type: str | None = None  # "dvd", "cd", or None - for CHD files


class DirectoryListing(BaseModel):
    volume: str
    path: str
    entries: list[FileEntry]


class Volume(BaseModel):
    name: str
    path: str


class ConversionJob(BaseModel):
    id: str
    file_path: str
    filename: str
    mode: ConversionMode
    status: JobStatus
    progress: int = 0
    message: str = ""
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    output_path: str | None = None
    output_size: int | None = None
    temp_dir: str | None = None
    allow_overwrite: bool = False
    compression: str | None = None
    delete_on_verify: bool = False


class JobCreateRequest(BaseModel):
    file_path: str
    mode: ConversionMode = ConversionMode.CREATECD
    output_dir: str | None = None  # If None, output alongside source
    duplicate_action: DuplicateAction = (
        DuplicateAction.SKIP
    )  # What to do if output exists
    compression: str | None = None  # Comma-separated list (e.g. "zlib,lzma")
    delete_on_verify: bool = False


class BatchJobCreateRequest(BaseModel):
    file_paths: list[str]
    mode: ConversionMode = ConversionMode.CREATECD
    output_dir: str | None = None  # If None, output alongside source
    duplicate_action: DuplicateAction = (
        DuplicateAction.SKIP
    )  # What to do if output exists
    compression: str | None = None  # Comma-separated list (e.g. "zlib,lzma")
    delete_on_verify: bool = False


class CheckDuplicatesRequest(BaseModel):
    file_paths: list[str]
    output_dir: str | None = None
    mode: ConversionMode = ConversionMode.CREATECD


class DuplicateInfo(BaseModel):
    file_path: str
    output_path: str
    exists: bool


class ArchiveEntry(BaseModel):
    archive_path: str
    internal_path: str
    name: str
    size: int
    extension: str
    convertible: bool = False


class CHDInfo(BaseModel):
    file: str
    input_file: str | None = None
    file_version: str | None = None
    logical_size: str | None = None
    hunk_size: str | None = None
    total_hunks: str | None = None
    unit_size: str | None = None
    total_units: str | None = None
    compression: str | None = None
    chd_size: str | None = None
    ratio: str | None = None
    sha1: str | None = None
    data_sha1: str | None = None
    raw_data: str = ""
    media_type: str | None = None  # "dvd", "cd", or None


class BulkDeleteRequest(BaseModel):
    paths: list[str]


class BulkVerifyRequest(BaseModel):
    paths: list[str]


class MetadataBatchRequest(BaseModel):
    paths: list[str]


class FeatureEventRequest(BaseModel):
    event: str
    value: int = 1


class DeletePlanRequest(BaseModel):
    file_paths: list[str]
    mode: ConversionMode = ConversionMode.CREATECD


class DolphinDiscInfo(BaseModel):
    file: str
    game_id: str | None = None
    game_name: str | None = None
    disc_number: str | None = None
    revision: str | None = None
    region: str | None = None
    format: str | None = None
    compression: str | None = None
    block_size: str | None = None
    file_size: str | None = None
    raw_data: str = ""


class Z3DSInfo(BaseModel):
    """Information about a Nintendo 3DS ROM file."""
    file: str
    size: int
    size_display: str
    format: str | None = None
    extension: str
    compressed: bool
    compression_type: str | None = None


# ============ Igir Models ============


class IgirCommand(str, Enum):
    """Individual igir commands that can be combined in a single run."""
    COPY = "copy"
    MOVE = "move"
    LINK = "link"
    EXTRACT = "extract"
    ZIP = "zip"
    TEST = "test"
    CLEAN = "clean"
    REPORT = "report"
    FIXDAT = "fixdat"
    DIR2DAT = "dir2dat"
    PLAYLIST = "playlist"


class IgirLinkType(str, Enum):
    # Primary igir values
    HARDLINK = "hardlink"
    SYMLINK = "symlink"
    REFLINK = "reflink"
    # Back-compat aliases used by earlier UI/API revisions
    HARD = "hard"
    SYMBOLIC = "symbolic"
    RELATIVE = "relative"


class IgirJobCreateRequest(BaseModel):
    """Request to create an igir ROM management job.

    Captures the full igir CLI surface area.  At least one command is required.
    Only one write command (copy/move/link) may be specified per job.  Archive
    commands (extract/zip) require an accompanying write command.
    """

    # Commands (at least one required)
    commands: list[IgirCommand]

    # Input / Output
    input_paths: list[str]                        # --input (required, directories or globs)
    output_path: str | None = None                # --output (required for write commands)
    dat_paths: list[str] | None = None            # --dat (DAT files or directories)
    input_exclude: list[str] | None = None        # --input-exclude
    dat_exclude: list[str] | None = None          # --dat-exclude
    patch: list[str] | None = None                # --patch
    patch_exclude: list[str] | None = None        # --patch-exclude

    # Input checksum options
    input_checksum_quick: bool = False            # --input-checksum-quick
    input_checksum_min: str | None = None         # --input-checksum-min
    input_checksum_max: str | None = None         # --input-checksum-max
    input_checksum_archives: str | None = None    # --input-checksum-archives (never|auto|always)

    # DAT filtering/options
    dat_name_regex: str | None = None             # --dat-name-regex
    dat_name_regex_exclude: str | None = None     # --dat-name-regex-exclude
    dat_description_regex: str | None = None      # --dat-description-regex
    dat_description_regex_exclude: str | None = None  # --dat-description-regex-exclude
    dat_combine: bool = False                     # --dat-combine
    dat_ignore_parent_clone: bool = False         # --dat-ignore-parent-clone

    # Link mode
    link_mode: IgirLinkType | None = None         # --link-mode
    symlink_relative: bool = False                # --symlink-relative
    # Back-compat legacy toggle from previous UI versions
    symlink: bool = False                         # legacy alias for link_mode=symlink

    # Writing behavior
    overwrite: bool = False                       # --overwrite
    overwrite_invalid: bool = False               # --overwrite-invalid
    fix_extension: str | None = None              # --fix-extension (auto|always|never)
    move_delete_dirs: str | None = None           # --move-delete-dirs (never|auto|always)

    # Output directory organization
    dir_mirror: bool = False                      # --dir-mirror
    dir_dat_mirror: bool = False                  # --dir-dat-mirror
    dir_dat_name: bool = False                    # --dir-dat-name
    dir_dat_description: bool = False             # --dir-dat-description
    dir_letter: bool = False                      # --dir-letter
    dir_letter_count: int | None = None           # --dir-letter-count
    dir_letter_limit: int | None = None           # --dir-letter-limit
    dir_letter_group: bool = False                # --dir-letter-group
    dir_game_subdir: str | None = None            # --dir-game-subdir (never|multiple|always)

    # Zip options
    zip_format: str | None = None                 # --zip-format (torrentzip|rvzstd)
    zip_exclude: str | None = None                # --zip-exclude
    zip_dat_name: bool = False                    # --zip-dat-name

    # Header and trim options
    header: str | None = None                     # --header
    remove_headers: str | None = None             # --remove-headers
    trimmed_glob: str | None = None               # --trimmed-glob
    trim_scan_archives: bool = False              # --trim-scan-archives

    # ROM set options (DAT-dependent)
    merge_roms: str | None = None                 # --merge-roms
    merge_discs: bool = False                     # --merge-discs
    exclude_disks: bool = False                   # --exclude-disks
    allow_excess_sets: bool = False               # --allow-excess-sets
    allow_incomplete_sets: bool = False           # --allow-incomplete-sets

    # Filtering
    filter_regex: str | None = None               # --filter-regex
    filter_regex_exclude: str | None = None       # --filter-regex-exclude
    filter_language: list[str] | None = None      # --filter-language (comma list)
    filter_region: list[str] | None = None        # --filter-region (comma list)
    filter_category_regex: str | None = None      # --filter-category-regex
    no_bios: bool = False
    only_bios: bool = False
    no_device: bool = False
    only_device: bool = False
    no_unlicensed: bool = False
    only_unlicensed: bool = False
    only_retail: bool = False
    no_debug: bool = False
    only_debug: bool = False
    no_demo: bool = False
    only_demo: bool = False
    no_beta: bool = False
    only_beta: bool = False
    no_sample: bool = False
    only_sample: bool = False
    no_prototype: bool = False
    only_prototype: bool = False
    no_program: bool = False
    only_program: bool = False
    no_aftermarket: bool = False
    only_aftermarket: bool = False
    no_homebrew: bool = False
    only_homebrew: bool = False
    no_unverified: bool = False
    only_unverified: bool = False
    no_bad: bool = False
    only_bad: bool = False

    # 1G1R (One Game One ROM)
    single: bool = False                          # --single
    prefer_game_regex: str | None = None          # --prefer-game-regex
    prefer_rom_regex: str | None = None           # --prefer-rom-regex
    prefer_verified: bool = False                 # --prefer-verified
    prefer_good: bool = False                     # --prefer-good
    prefer_language: list[str] | None = None      # --prefer-language (ordered comma list)
    prefer_region: list[str] | None = None        # --prefer-region (ordered comma list)
    prefer_revision: str | None = None            # --prefer-revision (older|newer)
    prefer_retail: bool = False                   # --prefer-retail
    prefer_parent: bool = False                   # --prefer-parent

    # Command-specific output options
    playlist_extensions: str | None = None        # --playlist-extensions
    dir2dat_output: str | None = None             # --dir2dat-output
    fixdat_output: str | None = None              # --fixdat-output
    report_output: str | None = None              # --report-output

    # Clean options
    clean_exclude: list[str] | None = None        # --clean-exclude
    clean_backup: str | None = None               # --clean-backup
    clean_dry_run: bool = False                   # --clean-dry-run

    # Threading/retry/cache/temp/debug
    dat_threads: int | None = None                # --dat-threads
    reader_threads: int | None = None             # --reader-threads
    writer_threads: int | None = None             # --writer-threads
    write_retry: int | None = None                # --write-retry
    temp_dir: str | None = None                   # --temp-dir
    disable_cache: bool = False                   # --disable-cache
    cache_path: str | None = None                 # --cache-path
    verbose: int = 0                              # 0=normal, 1=-v, 2=-vv, 3=-vvv


class IgirQuickSetupRequest(BaseModel):
    """Request payload for igir quick setup recommendations."""
    input_paths: list[str]
    goal: str | None = None


class IgirFeatureEventRequest(BaseModel):
    """Track lightweight feature-adoption events for igir UX improvements."""
    event: str
    value: int = 1


class IgirJob(BaseModel):
    """An igir ROM management job."""
    id: str
    commands: list[IgirCommand]
    input_paths: list[str]
    output_path: str | None = None
    dat_paths: list[str] | None = None
    status: JobStatus
    progress: int = 0
    message: str = ""
    phase: str = ""
    files_found: int = 0
    files_processed: int = 0
    files_total: int = 0
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    report_output: str | None = None
    command_preview: str = ""
    options_summary: str = ""
    clean_dry_run_results: list[str] | None = None


class DatFileEntry(BaseModel):
    """A single DAT file in the DAT directory."""
    name: str
    path: str
    size: int
    modified: str


class DatDirectoryListing(BaseModel):
    """Directory listing of the DAT mount."""
    path: str
    entries: list[DatFileEntry]
    subdirectories: list[str]


class IgirValidationResult(BaseModel):
    """Result of validating an igir job request."""
    valid: bool
    errors: list[str]
    warnings: list[str]
    command_preview: str
