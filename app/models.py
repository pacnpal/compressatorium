from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


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
    NSZ_COMPRESS = "nsz_compress"
    NSZ_DECOMPRESS = "nsz_decompress"
    METADATA_SCAN = "metadata_scan"
    DAT_MATCH = "dat_match"


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


class OutputStatus(BaseModel):
    """A sibling output a tool could produce for a given input file."""
    tool_id: str
    exists: bool          # finished output file present on disk
    ready: bool           # present and not mid-conversion
    path: str | None = None


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
    nsz_convertible: bool = False
    has_nsz: bool = False
    nsz_ready: bool = False
    nsz_path: str | None = None
    archive_items: int | None = None
    # Count of archive members that already have an existing output from any
    # registered tool (.chd/.rvz/.z3ds/.nsz/…), finished or mid-conversion.
    archive_has_output: int | None = None
    archive_truncated: bool | None = None
    media_type: str | None = None  # "dvd", "cd", or None - for CHD files
    convertible_by: list[str] = []   # tool ids whose input_extensions accept this file
    outputs: list[OutputStatus] = []  # detected sibling outputs, one per producing tool


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
    game_id: str | None = None    # disc serial / title ID (e.g. SLUS-20312)
    title: str | None = None      # human-readable game title (when available)


class BulkDeleteRequest(BaseModel):
    paths: list[str]


class BulkVerifyRequest(BaseModel):
    paths: list[str]


class MetadataBatchRequest(BaseModel):
    paths: list[str]


class DeletePlanRequest(BaseModel):
    file_paths: list[str]
    mode: ConversionMode = ConversionMode.CREATECD


class DolphinDiscInfo(BaseModel):
    file: str
    game_id: str | None = None
    game_name: str | None = None
    title_id: str | None = None
    disc_number: str | None = None
    revision: str | None = None
    region: str | None = None
    country: str | None = None
    format: str | None = None
    compression: str | None = None
    compression_level: str | None = None
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


class NszInfo(BaseModel):
    """Information about a Nintendo Switch NSP/XCI/NSZ/XCZ file."""
    file: str
    size: int
    size_display: str
    format: str | None = None
    extension: str
    compressed: bool
    compression_type: str | None = None


class LayoutPreferences(BaseModel):
    """Workspace layout widths persisted across sessions.

    ``panels`` holds the global panel-split widths (px); ``columns`` holds
    per-tool table column widths keyed by tool id. Extra keys are allowed
    so the client can evolve the shape without a schema change here; the
    server just stores and returns the JSON.
    """

    model_config = ConfigDict(extra="allow")

    panels: dict | None = None
    columns: dict | None = None


class ConversionPreferences(BaseModel):
    """Per-tool compression defaults, keyed by tool id.

    Values are the wire-format compression string the convert pipeline expects
    (e.g. ``"solid:18"`` for nsz, ``"zstd:19"`` for dolphin, ``"zlib,lzma"`` for
    chdman). Extra keys are allowed so new tools persist without a schema change.
    """

    model_config = ConfigDict(extra="allow")
