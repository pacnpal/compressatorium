from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, PrivateAttr


class InputKind(str, Enum):
    """The unit of work a conversion mode consumes.

    The codebase historically keyed every input seam off
    ``Path(filename).suffix``, which assumes a file. A directory has no suffix,
    so a mode that packages a folder (makeps3iso folder->iso) declares
    ``DIRECTORY`` and relies on a detector predicate
    (``ToolPlugin.accepts_directory``) instead of an extension match.

    Defined here (rather than in ``services.tools.spec``) so ``ConversionJob``
    can type its ``input_kind`` field against the enum without importing the
    ``services.tools`` package — that package imports ``models`` while building
    the registry, so the reverse import would be a cycle. ``spec.py`` re-exports
    it, keeping ``from services.tools.spec import InputKind`` working.
    """

    FILE = "file"
    DIRECTORY = "directory"


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
    Z3DS_DECOMPRESS = "z3ds_decompress"
    NSZ_COMPRESS = "nsz_compress"
    NSZ_DECOMPRESS = "nsz_decompress"
    CSO_COMPRESS = "cso_compress"
    CSO2_COMPRESS = "cso2_compress"
    ZSO_COMPRESS = "zso_compress"
    DAX_COMPRESS = "dax_compress"
    CSO_DECOMPRESS = "cso_decompress"
    CSO_TO_CHD = "cso_to_chd"
    ROMZ_7Z = "romz_7z"
    ROMZ_ZIP = "romz_zip"
    ROMZ_EXTRACT = "romz_extract"
    JWUD_COMPRESS = "jwud_compress"
    JWUD_DECOMPRESS = "jwud_decompress"
    FOLDER_TO_ISO = "folder_to_iso"
    NKIT_RESTORE = "nkit_restore"
    NKIT_TO_RVZ = "nkit_to_rvz"
    XISO_REWRITE = "xiso_rewrite"
    ZAR_COMPRESS = "zar_compress"
    PBP_TO_ISO = "pbp_to_iso"
    CIA_TO_CCI = "cia_to_cci"
    CCI_TO_CIA = "cci_to_cia"
    EXODOS_REPACK = "exodos_repack"
    METADATA_SCAN = "metadata_scan"
    DAT_MATCH = "dat_match"
    # A check Rule's run (#305): Verify over the Images it picked. An external
    # job like the two above, never a conversion.
    CHECK = "check"
    # A Library's find-duplicates run (#388): reads content keys and groups
    # the Images that hold one disc. External, never a conversion.
    FIND_DUPLICATES = "find_duplicates"
    # A confirmed delete of a duplicate group's other copies (#389): reads
    # the keeper in full, then deletes each copy under its locks. External.
    DELETE_DUPLICATES = "delete_duplicates"
    # A patch applied to a cartridge Image (#428): writes "Name (Patched).7z"
    # beside it and never changes it. External, but it has a Ledger row.
    PATCH = "patch"
    # A confirmed rename of Images to their DAT names (#425): each one renamed
    # through the Referrer hook, nothing converted or deleted. External.
    DAT_RENAME = "dat_rename"
    # An Inbox copying an Image into its Library as it is (#431): no Rule
    # converts it. External: it converts nothing.
    INBOX_COPY = "inbox_copy"


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
    # Human-facing label when it differs from the filename: a library manager's
    # curated game title ("Super Mario Bros." for smb_u_rev1.sfc). Display only
    # -- `name` remains the real filename that rename/delete/convert act on, so
    # nothing that touches the filesystem may read this. None for ordinary
    # directory listings.
    display_name: str | None = None
    path: str
    type: str  # "file", "directory", or "archive"
    size: int | None = None
    extension: str | None = None
    archive_items: int | None = None
    # Count of archive members that already have an existing output from any
    # registered tool (.chd/.rvz/.z3ds/.nsz/…), finished or mid-conversion.
    archive_has_output: int | None = None
    archive_truncated: bool | None = None
    media_type: str | None = None  # "dvd", "cd", or None - for CHD files
    convertible_by: list[str] = []   # tool ids whose input_extensions accept this file
    outputs: list[OutputStatus] = []  # detected sibling outputs, one per producing tool
    # Tool ids whose Verify/Info apply to THIS concrete path (per-file refinement
    # of verify_extensions). Mostly an extension match, but romz inspects an
    # archive's members so only single-ROM .7z/.zip surface its row-actions —
    # not every archive. Empty when no tool can verify/info the path.
    verifiable_by: list[str] = []
    # For a folded split-ISO set (makeps3iso -s output: Game.iso.0, .1, ...): the
    # number of part files this single logical entry represents. None for an
    # ordinary single-file entry. The entry's ``path`` points at the ``.0`` part
    # (what RPCS3 mounts / verify reads) and ``size`` is the summed part size.
    split_parts: int | None = None
    # RomM listings only (#427): whether RomM identified the ROM, and the app's
    # own cached DAT or Hasheous match for its file (what ``/dat/matches/lookup``
    # would return), None when the app has no usable one. Read from the match
    # cache; listing never hashes. None for ordinary directory listings.
    romm_identified: bool | None = None
    dat_match: dict | None = None


class RommListingSummary(BaseModel):
    """Counts over the rows of one RomM listing (#427).

    ``unidentified``: rows RomM couldn't identify. ``recognized``: rows that
    are Recognized, by RomM or by the app's own cached DAT or Hasheous match.
    ``recognized_unidentified``: rows RomM couldn't identify that the app
    Recognizes, the ones still safe to convert on a DAT's word.
    """

    unidentified: int = 0
    recognized: int = 0
    recognized_unidentified: int = 0


class RommDiagnostics(BaseModel):
    total: int = 0
    resolved: int = 0
    no_path: int = 0
    outside_volumes: int = 0
    not_found: int = 0
    not_readable: int = 0
    not_regular: int = 0
    sample_paths: list[str] = []
    library_root: str = ""
    volumes: list[str] = []


class DirectoryListing(BaseModel):
    volume: str
    path: str
    entries: list[FileEntry]
    diagnostics: RommDiagnostics | None = None
    romm: RommListingSummary | None = None


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
    # Why a cancel was asked for, once one was (#433): ``requested`` (someone
    # asked, through the UI or API) or ``shutdown`` (the app or ``run-rules``
    # is stopping). See ``job_manager.CANCEL_REQUESTED`` / ``CANCEL_SHUTDOWN``.
    cancel_reason: str | None = None
    output_path: str | None = None
    output_size: int | None = None
    temp_dir: str | None = None
    allow_overwrite: bool = False
    compression: str | None = None
    delete_on_verify: bool = False
    # The named opt-out from the Round-trip check (#298): delete the Source on
    # the container check alone ("faster, weaker"). Only read when
    # delete_on_verify is set and the mode can round-trip.
    container_only: bool = False
    # Verify the output and keep the source. Independent of delete_on_verify,
    # which verifies as a precondition for deleting; this asks for the check on
    # its own, so an unattended conversion can prove its result without the
    # destructive half.
    verify_after: bool = False
    # Split the output into ~4 GB parts for FAT32 targets (makeps3iso -s).
    # Only meaningful for the folder->iso directory mode; ignored elsewhere.
    split: bool = False
    # The unit of work this job consumes. FILE for every existing mode; a
    # directory-input mode (makeps3iso folder->iso) carries DIRECTORY so the
    # pipeline skips the archive-extract / file-only assumptions and the lock
    # manager can protect the whole source subtree. Serialized to its string
    # value at the API/persistence edge (InputKind is a str enum).
    input_kind: InputKind = InputKind.FILE
    # Ledger bookkeeping (#292). Source bytes are read before the converter
    # runs, so a row written after the Source was deleted still has them.
    input_bytes: int | None = None
    # The Platform the Ledger files this job under (#346): the Library's
    # Platform for a Rule's job, else what shared Platform detection made of
    # the Source before the job could delete it. None when neither places it.
    platform: str | None = None
    # Which check the output passed: "intact" (its tool's container check),
    # "round_tripped" (its content digest equalled the Source's, #298) or
    # "recognized" (its SHA-1 is in a DAT or Hasheous, #423). None if no check
    # ran.
    check_passed: str | None = None
    # The DAT that Recognized the output (a local DAT's name, or the source
    # Hasheous names), when ``check_passed`` is "recognized". Else None.
    check_dat: str | None = None
    deleted_paths: list[str] = []
    # A finished job that did less than asked without failing, such as a
    # delete-source run that kept its Source because another job was reading
    # it (#306). None when there is nothing to say.
    warning: str | None = None
    # What the Referrer hook did before the Source was to be removed (#295):
    # {"referrer": id, "target": file, "status": "updated" | "failed" |
    # "skipped", "reason": why, for failed and skipped}.
    referrers_updated: list[dict] = []
    # What an external job came to, for whoever started it: a duplicate
    # delete's per-copy outcomes and whether the copy kept changed (#389).
    # None for every other job.
    result: dict | None = None
    # Inputs besides the Source that shaped the output, for the Ledger: a
    # patch job's patch file, {"kind": "patch", "path": ..., "sha1": ...}
    # (#428). Empty for every other job.
    extra_inputs: list[dict] = []
    # Who queued the job when it is not a person (a sweep passes its token), so a
    # terminal listener can tell the two apart from the first moment the job
    # exists. A private attribute: not a field, so it is in neither the API schema
    # nor any serialized form.
    _sweep_owner: str | None = PrivateAttr(default=None)
    # The planned Source files still to remove when the delete step stopped
    # part way (#329): the Rule's sweep finishes them, else a Finding says so.
    _unfinished_delete: list[str] | None = PrivateAttr(default=None)
    # The Source's identity when the job was queued and the folder it must stay
    # under (``utils.path_utils.source_identity``, #431), or None. Checked under
    # the job's Source lock right before the tool runs.
    _source_identity: dict | None = PrivateAttr(default=None)
    # Where a job with a ``_source_identity`` reads its Source once bound
    # (``utils.path_utils.bind_source``): the ``BoundSource`` (a folder of
    # the job's own in the app's work area, removed through its kept
    # descriptors), and the Source's path in it. None until bound, and for
    # every other job. Runtime-only (``RUNTIME_ONLY_PRIVATES``).
    _bound: object | None = PrivateAttr(default=None)
    _read_path: str | None = PrivateAttr(default=None)

    def archived_copy(self) -> "ConversionJob":
        """A deep copy for the archive of finished jobs, without the
        runtime-only private state (``RUNTIME_ONLY_PRIVATES``: open handles,
        locks), which can't be copied and means nothing once archived. The
        live job is never touched (Codex 4606539536)."""
        shallow = self.model_copy()
        shallow.__pydantic_private__ = dict(shallow.__pydantic_private__ or {})
        for name in RUNTIME_ONLY_PRIVATES:
            shallow.__pydantic_private__[name] = None
        return shallow.model_copy(deep=True)


# A job's private attributes that hold runtime handles (the bound Source's
# descriptors and lock, #431): left out of every archived copy. Add any new
# private that holds a handle, a lock or a thread here.
RUNTIME_ONLY_PRIVATES: tuple[str, ...] = ("_bound",)


class JobCreateRequest(BaseModel):
    file_path: str
    mode: ConversionMode = ConversionMode.CREATECD
    output_dir: str | None = None  # If None, output alongside source
    duplicate_action: DuplicateAction = (
        DuplicateAction.SKIP
    )  # What to do if output exists
    compression: str | None = None  # Comma-separated list (e.g. "zlib,lzma")
    delete_on_verify: bool = False
    # Skip the Round-trip check and delete on the container check alone.
    container_only: bool = False
    split: bool = False  # makeps3iso folder->iso: split output into 4 GB parts


class BatchJobCreateRequest(BaseModel):
    file_paths: list[str]
    mode: ConversionMode = ConversionMode.CREATECD
    output_dir: str | None = None  # If None, output alongside source
    duplicate_action: DuplicateAction = (
        DuplicateAction.SKIP
    )  # What to do if output exists
    compression: str | None = None  # Comma-separated list (e.g. "zlib,lzma")
    delete_on_verify: bool = False
    # Skip the Round-trip check and delete on the container check alone.
    container_only: bool = False
    split: bool = False  # makeps3iso folder->iso: split output into 4 GB parts


class AutoPlanRequest(BaseModel):
    """Images to send to their Platform's default target (the Auto choice)."""

    file_paths: list[str]
    output_dir: str | None = None


class AutoJobItem(BaseModel):
    """One Image of a confirmed Auto plan and the target the user saw for it."""

    file_path: str
    mode: str


class AutoJobCreateRequest(BaseModel):
    """Queue the Auto plan the user confirmed.

    ``items`` are the plan's actionable rows only (an Image the preview
    skipped is never sent). The route replans them and refuses with 409 when
    any target changed, so what is queued is what was confirmed.
    """

    items: list[AutoJobItem]
    output_dir: str | None = None
    duplicate_action: DuplicateAction = DuplicateAction.SKIP


class AutoPlanItem(BaseModel):
    """One row of an Auto plan: the Image's Platform and where it goes.

    ``how`` is ``header``, ``folder``, ``conflict`` or ``unsorted``. ``mode``
    is None when the Image is skipped, and ``skip_reason`` then says why.
    """

    file_path: str
    platform: str | None = None
    how: str
    reason: str
    mode: str | None = None
    tool_id: str | None = None
    compression: str | None = None
    skip_reason: str | None = None


class AutoBatchResponse(BaseModel):
    plan: list[AutoPlanItem]
    jobs: list[ConversionJob]


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


class PlaylistPreviewRequest(BaseModel):
    """Disc Images (full paths, one folder) to list in a new .m3u."""

    paths: list[str]
    # Playlist title without ".m3u"; omitted, it comes from the disc names.
    title: str | None = None


class PlaylistCreateRequest(BaseModel):
    paths: list[str]
    # The file name the preview showed, so what is written is what was shown.
    file_name: str


class DeletePlanRequest(BaseModel):
    file_paths: list[str]
    mode: ConversionMode = ConversionMode.CREATECD
    # Where the outputs will land, and how a taken output is handled (RENAME
    # writes a numbered path), so Referrer changes name the file the job writes.
    output_dir: str | None = None
    duplicate_action: DuplicateAction = DuplicateAction.SKIP


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


class BasicFileInfo(BaseModel):
    """Shared shape for the simple per-file info models: identity, size, and the
    format/compression summary.

    The five tools whose /info just reports "what is this file" (z3ds, nsz, cso,
    romz, makeps3iso) return this exact shape; the two that add fields (romz's
    archive ratio, makeps3iso's title) subclass it instead of repeating it.
    """
    file: str
    size: int
    size_display: str
    format: str | None = None
    extension: str
    compressed: bool
    compression_type: str | None = None


class Z3DSInfo(BasicFileInfo):
    """Information about a Nintendo 3DS ROM file."""


class NszInfo(BasicFileInfo):
    """Information about a Nintendo Switch NSP/XCI/NSZ/XCZ file."""


class CsoInfo(BasicFileInfo):
    """Information about a PSP/PS2 ISO/CSO/ZSO/DAX file."""


class RomzInfo(BasicFileInfo):
    """Information about a handheld ROM (GB/GBC/GBA/NDS) or its .7z/.zip archive."""
    # Archive-only extras: the single ROM inside, its uncompressed size, and the
    # archive-to-original size ratio (None for a loose ROM source).
    contained_name: str | None = None
    original_size: int | None = None
    ratio: str | None = None


class JwudInfo(BasicFileInfo):
    """Information about a Wii U disc image (.wud) or its .wux container."""
    # WUX-only extras, read straight from the container header: the size of the
    # image it decompresses back to, and the resulting compression ratio (None
    # for a raw .wud source, which carries no such header).
    original_size: int | None = None
    ratio: str | None = None


class Ps3IsoInfo(BasicFileInfo):
    """Information about a decrypted PS3 disc/JB folder (makeps3iso source)."""
    title: str | None = None
    title_id: str | None = None


class NkitInfo(BasicFileInfo):
    """Information about an NKit-shrunk GameCube/Wii image (nkit2iso source).

    Everything below the shared fields comes from the NKit/disc header the
    binary itself reads: which console the disc is for, how it identifies
    itself, how big the restored ISO will be, and the CRC32 the restore is
    checked against.
    """
    platform: str | None = None          # "GameCube" | "Wii"
    game_id: str | None = None           # 6-char game + maker code, e.g. GALE01
    title: str | None = None
    disc_number: int | None = None
    disc_version: int | None = None
    restored_size: int | None = None     # bytes the restored .iso will occupy
    restored_size_display: str | None = None
    crc32: str | None = None             # of the ORIGINAL image, stored by NKit
    ratio: str | None = None             # shrunk size as a % of the original


class XisoInfo(BasicFileInfo):
    """Information about an Xbox / Xbox 360 disc image (extract-xiso source/product).

    Read from the image's own XDVDFS filesystem: which disc layout it uses,
    how many files it holds, and how much of it is actual game data (roughly
    what a trim keeps).
    """
    console: str | None = None           # "Xbox" | "Xbox 360"
    layout: str | None = None            # "XGD1" | "XGD2" | "XGD3" | "XISO"
    file_count: int | None = None
    data_size: int | None = None         # sum of every file's size, in bytes
    data_size_display: str | None = None
    data_ratio: str | None = None        # game data as a % of the file
    has_system_update: bool | None = None
    trimmed: bool | None = None          # already an XISO (no disc padding)
    optimized: bool | None = None        # written by extract-xiso


class RomConvertoInfo(BasicFileInfo):
    """Information about a file rom-converto writes (an Xbox 360 ZAR so far).

    Read with ``rom-converto xenon info``: what the container holds, and
    whether the boot executable Xenia mounts it by is at its root.
    """
    file_count: int | None = None
    directory_count: int | None = None
    logical_size: int | None = None      # the files' own bytes, uncompressed
    logical_size_display: str | None = None
    blocks: int | None = None
    boot_executable: bool | None = None  # default.xex at the archive root


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
