# Design: Tool Plugin Architecture

**Status:** Proposal · **Scope:** internal refactor, no user-facing behavior change ·
**Companion:** `ADDING_PLATFORMS_AND_TOOLS.md`

## 1. Problem

Adding one conversion tool today touches ~20 files, and the standing advice
is "copy what z3ds does" in each of them. The tool-service contract already
exists *implicitly*, every service (`chdman`, `dolphin_tool`,
`z3ds_compress`) exposes the same shape (`convert` / `verify` /
`verify_stream` / `info` / `is_convertible` / `get_output_path_for_mode`),
but it was never formalized, so tool-specific knowledge is hand-scattered into
hardcoded `if/elif` ladders and string-prefix checks across the codebase.

### 1.1 Where tool knowledge leaks today

| Concern | Location | Current shape |
|---------|----------|---------------|
| Which service handles a mode | `services/job_manager.py:1444` | `dolphin if startswith("dolphin_") else z3ds if == Z3DS_COMPRESS else chdman` |
| Output path on enqueue | `services/job_manager.py:139` | `if mode == Z3DS_COMPRESS … else chdman.get_chd_path` |
| Output path in routes | `routes/convert.py:100` `_get_output_path` | dolphin / z3ds / chdman branches |
| Verify dispatch | `services/job_manager.py:1556`, `:1591` | z3ds branch + dolphin/chdman pair |
| "is create mode?" etc. | `routes/convert.py` (`:87`, `:96`, `:305`–`:334`, `:407`) | `mode.startswith("create"/"extract"/"dolphin_")`, `== "z3ds_compress"` |
| Extension validation | `routes/convert.py:412`–`427` **and** `:656`–`666` | duplicated in `create_job` + `create_batch_jobs` |
| Archive-input guard | `routes/convert.py:357`, `:606` | per-mode allow/deny list |
| Convertibility flags | `routes/files.py:11`–`13`, `:169`–`204`, `:316`–`353` | one import + flag block per tool, twice |
| Info + verify endpoints | `routes/info.py` (~1500 lines) | three near-identical copies (chd / dolphin / z3ds) of verify, verify/events, verify-batch/events |
| Binary path | `config.py:111`–`121` | one `Field` per tool |
| Info model | `models.py:139`,`:176`,`:193` | `CHDInfo` / `DolphinDiscInfo` / `Z3DSInfo` |
| Mode enum | `models.py:7`–`25` | flat `ConversionMode` strings |
| Per-tool output flags | `models.py:42`–`63` | `has_chd`, `has_rvz`, `dolphin_ready`, `z3ds_ready`, … |
| Subprocess orchestration | `services/chdman.py`, `dolphin_tool.py`, `z3ds_compress.py` | ~150 near-identical lines each (spawn, nice/ionice, stall loop, `\r`/`\n` buffering, cancel watcher, PID tracking) |
| Frontend dispatch | `static/js/app.js` (~10 sites), `api.js` | tool branches for label, hint, filters, `MODE_GROUPS`, info modal, verify routing |

The rule of three is satisfied (three tools exist), so abstracting this is no
longer premature, it is overdue.

### 1.2 Goals / non-goals

**Goals**
- A new tool is defined in **one module** and registered in **one place**.
- Dispatch sites ask a **registry** instead of branching on tool identity.
- Eliminate the single-vs-batch and per-tool-endpoint duplication.
- Zero user-facing behavior change; ship incrementally, stay green.

**Non-goals**
- No dynamic/third-party plugin loading (no `importlib`/entry-points
  discovery). An in-process registry of first-party tools is the right scope.
- No change to the wire API (mode strings, endpoint paths, JSON shapes) until
  a later, optional cleanup phase.
- No change to the job queue / concurrency / locking model.

---

## 2. Target architecture

```
                         ┌──────────────────────────────┐
                         │  tool registry (singleton)    │
                         │  register(plugin)             │
                         │  for_mode / spec / for_input  │
                         └───────────────┬───────────────┘
        ┌────────────────────────────────┼────────────────────────────────┐
        ▼                                 ▼                                 ▼
 ChdmanTool(BaseTool)            DolphinTool(BaseTool)            Z3dsTool(BaseTool)
   modes=[ModeSpec...]             modes=[ModeSpec...]              modes=[ModeSpec...]
   _build_command()                _build_command()                 _build_command()
   _parse_progress()               _parse_progress()                _parse_progress()
        └───────────────── shared via ─────────────────────────────────────┘
                         SubprocessRunner (spawn / stall / cancel / PID)

 consumers (no tool branching):
   job_manager   → registry.for_mode(mode).convert(...) / .verify(...)
   convert route → registry.spec(mode) for validation + .output_path(...)
   files route   → loop registry.tools_for_listing(...)
   info route    → routes generated from registry (one generic verify adapter)
   frontend      → TOOLS descriptor table drives generic components
```

Two new descriptor types, one base class, one runner, one registry.

---

## 3. Concrete interfaces

New package `app/services/tools/`:

```
app/services/tools/
  __init__.py        # builds the registry, registers all tools
  spec.py            # ModeSpec, ModeKind
  base.py            # ToolPlugin (Protocol) + BaseTool (ABC)
  runner.py          # SubprocessRunner
  registry.py        # ToolRegistry
  chdman.py          # ChdmanTool   (wraps/owns today's chdman logic)
  dolphin.py         # DolphinTool
  z3ds.py            # Z3dsTool
```

### 3.1 `spec.py`: modes carry metadata

```python
from dataclasses import dataclass
from enum import Enum

class ModeKind(str, Enum):
    CREATE = "create"      # source -> compressed container
    EXTRACT = "extract"    # compressed container -> source
    COPY = "copy"          # recompress in place
    COMPRESS = "compress"  # generic one-shot compressor (z3ds-style)

@dataclass(frozen=True)
class ModeSpec:
    mode: str                       # wire value, e.g. "createcd" (== ConversionMode value)
    tool_id: str                    # "chdman" | "dolphin" | "z3ds"
    kind: ModeKind
    label: str                      # UI label
    group: str                      # UI group id ("create","extract","copy","dolphin","z3ds")
    output_ext: str | None          # ".chd"/".rvz"/None when input-ext-mapped
    input_extensions: frozenset[str]
    supports_compression: bool = False
    supports_compression_level: bool = False   # dolphin rvz/wia only
    supports_delete_on_verify: bool = False
    allows_archive_input: bool = False         # opt-in; set True on every mode that can take a member straight from an archive (chdman create + extract, dolphin, z3ds). Only chdman copy (.chd recompress round-trip) leaves it False
```

**Archive browsing is global, scoped to known extensions; the convert gate is
narrower.** Two registry unions, two jobs:

- `convertible_extensions()` — every source extension any tool recognizes. The
  archive *browse* listing (`ArchiveService._listable_extensions`) uses this
  minus the archive containers, so anything we know how to compress shows up
  when you look inside an archive. A finished output a tool disowns as a source
  stays hidden for free (chdman drops `.chd` from its `input_extensions`).
- `archive_input_extensions()` — only the inputs of `allows_archive_input` modes.
  This is the *convert gate* in `plan_job` and the filter the recursive *search*
  path uses (`ArchiveService._convert_gate_extensions`).

A member is *listed* (browse) far more liberally than it is *convertible in
place* (gate). A romz ROM is a known source, so it shows when you browse into its
archive — fixing the "Empty folder / 0 items" bug — but no `allows_archive_input`
mode accepts it, so `tools_accepting_archive_member` returns nothing and it
carries no conversion affordance (recompressing an archived ROM would be
recursive). Search stays on the narrow gate so list-only members can't consume
the per-archive entry cap ahead of a genuine convertible member.

Every prefix check in the codebase maps to a field:

| Today | After |
|-------|-------|
| `mode.startswith("create")` | `spec.kind == ModeKind.CREATE` |
| `mode.startswith("extract")` | `spec.kind == ModeKind.EXTRACT` |
| `mode.startswith("dolphin_")` | `spec.tool_id == "dolphin"` |
| `mode == "z3ds_compress"` | `spec.tool_id == "z3ds"` (or `kind == COMPRESS`) |
| `supports_delete_on_verify(mode)` (`convert.py:87`) | `spec.supports_delete_on_verify` |
| GCZ/ISO compression special-cases (`convert.py:315`–`324`) | `spec.supports_compression` |

`ConversionMode` (the Pydantic enum in `models.py`) **stays**, it remains the
validated wire type. `ModeSpec.mode` equals `ConversionMode(...).value`. The
registry is the bridge between the two.

### 3.2 `base.py`: the plugin contract

```python
from collections.abc import AsyncGenerator, Sequence
from typing import Protocol, runtime_checkable
from pathlib import Path
from pydantic import BaseModel
import asyncio

@runtime_checkable
class ToolPlugin(Protocol):
    id: str
    display_name: str
    binary_path: str
    modes: Sequence["ModeSpec"]
    input_extensions: frozenset[str]    # convertible-from
    output_extensions: frozenset[str]   # produced (badges + scan discovery)
    verify_extensions: frozenset[str]   # accepted by verify()
    embedded_hash_is_exhaustive: bool   # miss => definitive (skip file SHA1)

    def output_path(self, mode: str, input_path: str, output_dir: str | None = None,
                    *, treat_as_stem: bool = False) -> str: ...

    def convert(self, input_path: str, output_path: str, mode: str, *,
                compression: str | None = None,
                cancel_event: asyncio.Event | None = None) -> AsyncGenerator[dict, None]: ...

    async def verify(self, path: str) -> dict: ...                  # {"valid","message"}
    def verify_stream(self, path: str) -> AsyncGenerator[dict, None]: ...
    async def info(self, path: str) -> dict: ...                    # raw dict
    def info_model(self, raw: dict, path: str) -> BaseModel: ...    # typed model for the API

    # DAT-match fast path: (sha1, match_type) pairs the tool can report
    # cheaply (chdman header/data SHA1 from the metadata cache, dolphin disc
    # SHA1 via verify). Default empty -> caller falls back to file-level SHA1.
    # Raise EmbeddedHashUnavailable when the tool *should* yield a hash but the
    # attempt failed / the cache is stale, so the caller skips the meaningless
    # file-level fallback and does NOT cache a false negative. cancel_event lets
    # a background scan/match job abort an expensive derivation promptly.
    async def embedded_hashes(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> list[tuple[str, str]]: ...

    def active_pids(self) -> list[int]: ...

    # Optional post-processing hook (chdman uses it to embed disc-ID GAME/NAME
    # tags after createcd/createdvd, today hardcoded at job_manager.py:1514).
    async def post_convert(self, input_path: str, output_path: str, mode: str) -> None: ...
```

`BaseTool(ABC)` provides shared defaults so concrete tools stay tiny:

```python
class BaseTool:
    id: str
    display_name: str
    modes: Sequence[ModeSpec] = ()

    def __init__(self, binary_path: str):
        self.binary_path = binary_path
        self._runner = SubprocessRunner(owner=self.id)

    # derived sets (no per-tool duplication)
    @property
    def input_extensions(self) -> frozenset[str]:
        return frozenset().union(*(m.input_extensions for m in self.modes))

    def spec(self, mode: str) -> ModeSpec:
        for m in self.modes:
            if m.mode == mode:
                return m
        raise KeyError(mode)

    # default verify() wraps verify_stream(), identical in all 3 services today
    async def verify(self, path: str) -> dict:
        final = {"valid": False, "message": "Verification failed"}
        async for u in self.verify_stream(path):
            if u.get("type") in ("complete", "error"):
                final = u
        return {"valid": bool(final.get("valid")), "message": final.get("message") or "Verification failed"}

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    async def post_convert(self, *a, **k) -> None:
        return None

    # subclasses implement: output_path, convert (via self._runner.run),
    # verify_stream, info, info_model
```

> **Normalization note:** `info()` is async on the contract.
> `chdman.info`/`dolphin.header` are already async; `z3ds.info` is sync, so
> `Z3dsTool.info` wraps it in `run_in_threadpool`. `dolphin` exposes header
> data via `header()` today, `DolphinTool.info` simply calls it.

### 3.3 `runner.py`: shared subprocess orchestration

This collapses ~150 near-identical lines now living in each of
`chdman.py:96`–`334`, `dolphin_tool.py:109`–`356`, `z3ds_compress.py:137`–`347`.

```python
class SubprocessRunner:
    def __init__(self, owner: str):
        self._owner = owner
        self._active_pids: set[int] = set()
        self._lock = threading.Lock()

    def active_pids(self) -> list[int]: ...

    async def run(self, cmd: list[str], *, input_path: str, output_path: str,
                  parse_progress, cancel_event=None, cwd=None,
                  start_message="Starting...") -> AsyncGenerator[dict, None]:
        """Spawn cmd, stream stdout, yield {"progress","message"}.
        Handles: nice/ionice wrap, stdbuf, PID tracking, \\r/\\n line buffering,
        stall timeout via compute_progress_stall_timeout, cancel-watcher
        (terminate->kill, clean partial output), ConversionCancelled, non-zero
        exit -> RuntimeError(tail), final 100%.
        `parse_progress(line) -> int|None` is the only per-tool knob.
        `cwd` (optional) sets the subprocess working directory — used by `romz`
        to run `7z a` from the ROM's directory so the archive stores just the
        basename, not the absolute volume path.
        """

    async def run_capture(self, cmd: list[str], *, timeout=None,
                          cancel_event=None, stderr_to_stdout=False
                          ) -> tuple[int | None, bytes, bytes]:
        """One-shot counterpart to run(): buffered (returncode, stdout, stderr)
        for tools that need a result rather than streamed lines (info / header /
        embedded-hash extraction). Same PID tracking; races communicate()
        against cancel_event + timeout and terminates (TERM->KILL) on either,
        reporting returncode None to signal the abort. Used by
        `dolphin_tool.disc_hashes` so `dolphin-tool verify --algorithm sha1`
        (the Dolphin disc-hash source for `embedded_hashes`) aborts promptly
        when a scan/match job is cancelled.
        """
```

Per-tool `convert()` becomes ~15 lines: build argv, then
`async for u in self._runner.run(cmd, ..., parse_progress=self._parse_progress): yield u`.
The dolphin/z3ds output-size heuristic and dolphin's heartbeat become opt-in
flags on `run()`. One-shot subprocess work (info / header / embedded-hash
extraction) shares `run_capture()` rather than re-implementing the
spawn / cancel / timeout / terminate dance per tool.

### 3.3.1 Shared archive-limit enforcement (`services/archive.py`)

`ArchiveService.enforce_archive_limits(members)` is the shared seam any
archive-backed tool MUST call before shelling out to its own extractor.
`ArchiveService`'s own extract path already applies the configured
`CHD_ARCHIVE_MAX_ENTRIES` / `CHD_ARCHIVE_MAX_MEMBER_SIZE` /
`CHD_ARCHIVE_MAX_TOTAL_SIZE` guards (zip-bomb / oversized-archive protection),
but tools that read archives via their own member listing and run a CLI
directly (e.g. `romz` shelling out to `7z` for extract/verify) would otherwise
bypass those limits. `members` is a list of `(name, uncompressed_size)` from the
tool's raw listing — pass the *unfiltered* listing so junk entries still count
against the entry/size budget. New archive-backed tools should route through
this helper rather than re-checking limits per tool; do not duplicate the size
arithmetic.

Tools that extract with **preserved member paths** (e.g. `romz` running
`7z x`) MUST reject unsafe member paths up front on **every** member before
shelling out — not just the member they intend to keep. A CLI extractor
recreates the full archive tree, so an absolute or `..`-escaping path on any
member (including ignored junk sidecars like `__MACOSX/../../victim`) could
write outside the temp dir. `ArchiveService._validate_member` is the strict,
Windows-portable guard, but it also bans `:` and `\\` — characters a loose ROM
on a POSIX volume may legally contain — so a tool that must **round-trip** the
archive it produced (verify/extract its own output) should instead block only
the actual escape vectors (absolute path, `..` component) via a local check
like `RomzService._reject_traversal`, which `os.path.normpath`s the member and
rejects only absolute / `..`-leading results. Such tools must also forbid
symlink members (zip `external_attr` `S_ISLNK` / py7zr `FileInfo.is_symlink`),
since `7z x` restores symlinks as links.

### 3.3.2 Shared cached archive member reader (`services/archive_members.py`)

Reading an archive's member list is the hot path behind two unrelated features:
`ArchiveService.list_archive_contents` (the file browser's per-archive summary)
and `RomzService.is_single_rom_archive` (the romz Verify/Info gate, §3.6). Each
used to open and parse the archive itself, so one archive row on screen was
opened **twice**, and a directory of thousands of single-ROM archives turned a
single listing into thousands of redundant `zipfile`/`py7zr`/`rarfile` opens —
the listing endpoint blocked for ~40 s and starved the browser's connection pool
behind it.

`services/archive_members.read_archive_members(path)` is the single seam every
member-listing consumer MUST go through. It returns a uniform
`list[ArchiveMember]` (`name`, `size | None`, `is_dir`, `is_symlink`) for `.zip`
/ `.7z` / `.rar`, behind **one module-global** `utils.mtime_cache.MtimeCache`
keyed by `(path, mtime_ns, size)`. Because the cache is global and format-blind,
every consumer and every supported container benefits for free, and the
invalidation is automatic: a conversion that rewrites an archive bumps its
mtime/size and the next read recomputes. Adding a new archive format is one
`_read_<ext>` plus the extension — all consumers pick it up with no further
change.

Consumers keep their own *policy* over the shared *read*: `ArchiveService`
filters members to a caller-supplied extension set — the global known-source set
(`_listable_extensions`, browse) or the narrower convert-gate set
(`_convert_gate_extensions`, search) — and enforces the size limits (§3.3.1) in
`_filter_members` (one format-agnostic pass that replaced the old per-format
`_list_zip`/`_list_7z`/`_list_rar` triplet); `romz` rejects symlink members as a
security gate. A listed member is only badged convertible-in-place when a tool can
accept it straight from an archive — the archive route derives that via
`registry.tools_accepting_archive_member(ext)` rather than a bare
`ext in input_extensions`, so a listing-only romz ROM shows with no conversion
affordance the convert route would reject. "Raw" means unfiltered — directories, junk, and symlinks are all
returned so each policy can decide. Only **metadata listing** routes through the
reader; byte-extraction paths (`ArchiveService._extract_*`) and the `7z t`
integrity check still open the archive directly because they read content, which
isn't cacheable. The cache returns a shared list, so `list_archive_contents`
builds fresh dicts per call (the archive route annotates entries in place).

The directory listing is also **lazy by default**: `GET /files` takes
`summarize_archives` (default `True` for API back-compat) but the web UI calls it
with `summarize_archives=false`. In that lazy mode an archive row's counters
(`archive_items` / `archive_has_output` / `archive_truncated`) come back `null`
(Python `None`) and `verifiable_by` comes back as an empty list `[]` (the field is
always a list, never null), so the listing renders instantly. The browser then
hydrates those badges from `POST /archive-summary` (a batch keyed by archive path,
computed by the shared `_summarize_archive` helper so it's byte-identical to the
inline `summarize_archives=true` path) — mirroring how CHD `media_type` is
hydrated via `/chd-metadata`. To keep that hydration bounded, the browser
summarizes only the **visible page** of archive rows (re-hydrating on
page/sort/filter changes), and the endpoint additionally caps each batch at
`MAX_ARCHIVE_SUMMARY_PATHS`. The summary batch and the romz gate share the
reader's cache, so hydrating a folder opens each archive at most once.

### 3.3.3 Cross-tool chaining (`ChainSpec` / `ChainTool`, `services/disk.py`)

Some conversions are a pipeline of existing single-tool modes (issue #98:
`cso_to_chd` = maxcso `cso_decompress` → chdman `createdvd`). The chaining seam
sits **above** the plugin contract rather than rewriting it.

- **`ChainStep` / `ChainSpec` (`services/tools/spec.py`)** — a `ChainSpec` is a
  structural superset of `ModeSpec` (same `mode`/`tool_id`/`kind`/`output_ext`/
  `input_extensions`/`supports_*`/`allows_archive_input` fields every registry
  consumer reads), plus `steps` (ordered `ChainStep(tool_id, mode, weight,
  output_ratio)`), `intermediate_exts`, and `verify_step`. Because it quacks
  like a `ModeSpec`, `registry.spec(mode)` / `mode_specs()` / `convertible_extensions()`
  need no changes.
- **`ChainTool(BaseTool)` (`services/tools/chain.py`)** — a synthetic tool
  (`id="chain"`) registered **after** its component tools and handed the
  `registry`. Its `convert` orchestrates the steps via
  `registry.for_mode(step.mode).convert(...)`, writing intermediates into a
  private temp dir (cleaned in `finally`), aggregating each step's 0–100 into a
  **normalized-weight** slice of one bar, and routing `compression` only to a
  step whose sub-mode supports it. `verify`/`info`/`output_path` delegate to the
  `verify_step` (final) tool, so `job_manager`'s existing verify gate and
  delete-on-verify drive the chain with no special-casing — the original source
  is deleted only after the **final** output verifies. The chain claims no
  `verify_extensions`/`output_extensions` of its own (the final tool already
  owns them). Disc-ID tagging that `job_manager` only runs for literal
  `createcd`/`createdvd` is re-applied by the chain for its final step.
- **`services/disk.ensure_headroom`** — the shared free-space preflight a chain
  runs before step 1 (a chain holds source + full intermediate + partial final
  at once, which the single-step model never did). It is multi-volume aware:
  requirements are grouped by mount (`st_dev` of the nearest existing ancestor,
  since the output dir may not exist yet) and each mount is checked once, summing
  co-located targets. Cushion is `COMPRESSATORIUM_CHAIN_DISK_MARGIN_MB`.

New chained conversions (xci→nsp, wud→wua, a future folder→iso→chd) are a new
`ChainSpec` row + its component modes — no new machinery.

### 3.3.4 Directory inputs (`InputKind`, `accepts_directory`)

Every input seam keys off `Path(filename).suffix`; a folder has none. For
directory-unit conversions (issue #98 Phase 2, makeps3iso folder→iso) the seam
is an `InputKind` enum + `ModeSpec.input_kinds` (default `{FILE}`, zero behavior
change), a `ToolPlugin.accepts_directory(path)` predicate (default `False`; the
directory analogue of `ext in input_extensions`), and
`registry.tools_for_directory(path)`. The per-source-type detector for PS3 lives
in `services/ps3.py` (requires the `PS3_GAME/` disc/JB layout). Scaffolding only
so far — the makeps3iso tool, listing annotation, job-model `input_kind`, and UI
selection are not wired yet.

### 3.4 `registry.py`

```python
class ToolRegistry:
    def __init__(self): self._tools: dict[str, ToolPlugin] = {}; self._by_mode: dict[str, ToolPlugin] = {}
    def register(self, tool: ToolPlugin) -> None:
        self._tools[tool.id] = tool
        for m in tool.modes:
            if m.mode in self._by_mode: raise ValueError(f"duplicate mode {m.mode}")
            self._by_mode[m.mode] = tool
    def all(self) -> list[ToolPlugin]: return list(self._tools.values())
    def get(self, tool_id: str) -> ToolPlugin: return self._tools[tool_id]
    def for_mode(self, mode: str) -> ToolPlugin: return self._by_mode[mode]
    def spec(self, mode: str) -> ModeSpec: return self.for_mode(mode).spec(mode)
    def mode_specs(self) -> list[ModeSpec]: return [m for t in self._tools.values() for m in t.modes]
    def convertible_extensions(self) -> frozenset[str]:
        return frozenset().union(*(t.input_extensions for t in self._tools.values()))
    def tools_for_input(self, filename: str) -> list[ToolPlugin]:
        ext = Path(filename).suffix.lower()
        return [t for t in self._tools.values() if ext in t.input_extensions]
    def tool_for_verify(self, path: str) -> ToolPlugin | None:
        ext = Path(path).suffix.lower()
        return next((t for t in self._tools.values() if ext in t.verify_extensions), None)
    # Discovery helpers (issue #131): the union of every tool's produced /
    # verifiable extensions drives the registry-driven library scan, so a new
    # tool's outputs become scannable for free.
    def output_extensions(self) -> frozenset[str]:
        return frozenset().union(*(t.output_extensions for t in self._tools.values()))
    def verify_extensions(self) -> frozenset[str]:
        return frozenset().union(*(t.verify_extensions for t in self._tools.values()))
    def scannable_extensions(self) -> frozenset[str]:
        return self.output_extensions() | self.verify_extensions()
```

`__init__.py`:

```python
from config import settings
from .registry import ToolRegistry
from .chdman import ChdmanTool
from .dolphin import DolphinTool
from .z3ds import Z3dsTool

registry = ToolRegistry()
registry.register(ChdmanTool(settings.chdman_path))
registry.register(DolphinTool(settings.dolphin_tool_path))
registry.register(Z3dsTool(settings.z3ds_compressor_path))
```

### 3.5 Generic info/verify route factory (`routes/info.py`)

The three verify endpoint trios collapse into one factory driven by the
registry. Pseudocode:

```python
def register_verify_routes(router, tool: ToolPlugin):
    base = tool.id                                  # "chd" stays "chd" via alias map
    @router.get(f"/{base}-verify")
    async def _verify(path: str = Query(...)):
        _guard(path, tool.verify_extensions)
        token = await _acquire_verify_lane_or_429()
        try:
            r = await tool.verify(path)
            if r.get("valid"): await verification_store.mark_verified(path)
            return r
        finally: token.release()
    @router.get(f"/{base}-verify/events")
    async def _verify_events(path: str = Query(...)):
        return _sse_from_verify_stream(tool, path)   # the shared adapter
    @router.post(f"/{base}-verify-batch/events")
    async def _verify_batch(req: BulkVerifyRequest):
        return _sse_batch_from_verify_stream(tool, req.paths)
```

`_sse_from_verify_stream` / `_sse_batch_from_verify_stream` contain the
queue/done/2-second-heartbeat machinery that is copy-pasted ~6× in `info.py`
today. Endpoint **paths stay identical** (a `tool_id → url_prefix` alias map
keeps `chd`, `dolphin`, `z3ds`), so the frontend and API are unchanged.

### 3.6 Generic `FileEntry` outputs (`models.py`, `routes/files.py`)

```python
class OutputStatus(BaseModel):
    tool_id: str
    exists: bool          # finished file present
    ready: bool           # present and not mid-conversion
    path: str | None = None

class FileEntry(BaseModel):
    ...
    convertible_by: list[str] = []           # tool ids that accept this input
    outputs: list[OutputStatus] = []         # detected sibling outputs
    verifiable_by: list[str] = []            # tool ids whose Verify/Info apply here
    # legacy fields kept during migration (see Phase 7), removed at the end
```

`files.py` `scan_directory`/`search_files` stop hardcoding three flag blocks
and instead loop `for tool in registry.all()` calling a tool-provided
`detect_output(item_path)`.

#### Per-file verify gate (`verifies_path` / `verifiable_by`)

`verify_extensions` is a coarse, extension-level claim. Most tools verify
every file with a matching extension, but a tool can claim a **broad container
extension while only handling a narrow subset** of it — `romz` claims `.7z`/
`.zip` (its extract-mode inputs) yet its Verify/Info only apply to the
single-ROM archives it produced, not to an arbitrary multi-file `.zip` the user
happens to have. Gating the frontend's Verify/Info row-actions on the
extension alone would offer the affordance where the tool can't actually use it
(issue #146).

The plugin contract closes that gap with `ToolPlugin.verifies_path(path) ->
bool`: the per-file refinement of `verify_extensions`. `BaseTool` defaults it to
the plain extension match, so existing tools are unaffected; `RomzTool`
overrides it to inspect the archive's members (exactly one handheld-ROM member —
the same invariant verify/extract enforce, via
`RomzService.is_single_rom_archive`). The registry exposes
`tools_verifying_path(path)` (the per-file companion to `tool_for_verify`), and
`routes/files.py` materializes the result into the tool-neutral
`FileEntry.verifiable_by` list for on-disk files and archive containers. The
frontend (`RowActionsMenu`) gates Verify/Info on `verifiable_by` when present,
falling back to the registry's extension match for rows the listing doesn't
annotate (e.g. archive members, which can't be verified in place). `verifies_path`
may do disk I/O (archive inspection), so call it off the event loop — the
`files.py` scans already run in a threadpool.

Two consumer-side notes keep the gate airtight:

- **Frontend precedence.** The registry's extension match (`toolForVerifyPath`)
  stays the source of truth, because it encodes deliberate UX exclusions the
  backend's broader `verify_extensions` don't — e.g. Dolphin's
  `verify_extensions` include `.iso`, but the frontend intentionally omits
  `.iso` from `DOLPHIN_VERIFY_EXTS` so CD/DVD ISOs aren't routed to a Dolphin
  verify that fails. `verifiable_by` may therefore only **narrow** that match,
  never broaden it. This rule lives in one place — `registry.verifyToolForPath`
  (resolve the extension match, then drop it when a present `verifiable_by`
  excludes it) — and every verify entry point uses it: the per-row menu
  (`RowActionsMenu`), the "Verify selected" gate (`FileList`), and the bulk
  target picker (`BulkVerifyModal`), so a non-single-ROM archive can't slip a
  romz Verify through the batch path either.
- **Output targets.** A source row can verify *from* an existing sibling output
  (`entry.outputs[].path`), so that path needs the same gate. Rather than
  re-listing the output inside the row, the producing tool's `detect_output`
  validates the candidate before claiming it: `RomzTool.detect_output` only
  reports a finished `.7z`/`.zip` as a romz output when it is a genuine
  single-ROM archive (a mid-conversion placeholder still badges as
  in-progress). A coincidental multi-file `Game.gba.7z` thus never enters
  `outputs`, so the verify-from-output flow can't offer romz Verify on it.

### 3.7 Frontend descriptor (`src/lib/tools/registry.js`)

> **Status: implemented and extended.** The original §3.7 sketched a minimal
> descriptor on `static/js/tools.js`. The legacy Preact frontend was retired
> in favor of a Svelte 5 + Vite SPA (see README §"Frontend Development"), and
> the registry now owns every tool fact: verify URLs derive from
> `verifyPrefix`, per-tool `groups` map carries group labels (no hardcoded
> switch), `defaultMode` replaces the old chdman special-case, and optional
> `glyph` / `accent` give the sidebar / dashboard a visual escape hatch.
> See `src/lib/tools/registry.js` for the source of truth.

```js
// src/lib/tools/registry.js (abridged, see file for full schema)
export const TOOLS = [
  { id: 'chdman', label: 'CHDMAN', hint: '…',
    verifyPrefix: '',                                    // URL segment, '' → /api/verify
    sourceExts: ['.gdi','.iso','.cue','.bin'], verifyExts: ['.chd'],
    modeGroups: ['create','extract','copy'],
    groups: { create: 'Create', extract: 'Extract', copy: 'Copy' },
    defaultMode: 'createcd',
    glyph: 'CD', accent: 'var(--badge-cd)',
    modes: [/* ModeSpec rows mirroring app/services/tools/spec.py */],
    getInfo: api.getCHDInfo, verify: api.verifyCHD, verifyBatch: api.verifyBatchCHDs,
    productPath: (p) => p.replace(/\.[^.]+$/, '.chd') },
  { id: 'dolphin', verifyPrefix: 'dolphin', /* … */ },
  { id: 'z3ds',    verifyPrefix: 'z3ds',    /* … */ },
];
```

`getPrimaryToolLabel`/`getPrimaryToolHint`/`getFilterOptions`/`MODE_GROUPS`/
`CHDInfoModal` routing/verify-batch routing (all in `app.js`) become lookups
into `TOOLS` instead of `if (tool === 'dolphin') …` chains.

---

## 4. The payoff: what a new tool looks like *after*

Before: ~20 files (see `ADDING_PLATFORMS_AND_TOOLS.md` §3). After:

1. `app/services/tools/nszip.py`, one `BaseTool` subclass with `modes`,
   `_build_command`, `_parse_progress`, `verify_stream`, `info`, `info_model`.
2. `app/services/tools/__init__.py`, one `registry.register(NszipTool(...))`.
3. `app/config.py`, one `nszip_path` Field.
4. `static/js/tools.js`, one entry in `TOOLS`.
5. `Dockerfile`, install the binary (irreducible).
6. tests + docs.

Dispatch in `job_manager`, `convert`, `files`, `info`, **no edits**, because
they iterate the registry. That is the whole point.

---

## 5. Migration sequence

Each phase is independently shippable, preserves behavior, and ends green
(`pytest -q tests`, with special attention to `test_mode_parity_fixes.py`,
`test_z3ds_routes.py`, `test_dolphin_routes.py`, `test_chdman_annotations.py`).
Phases are ordered so the **registry exists first** and consumers migrate one
at a time. Nothing here changes the wire API until Phase 9 (optional).

### Phase 0: Scaffold the registry around existing services (no behavior change)
- Add `app/services/tools/` with `spec.py`, `base.py`, `registry.py`.
- Write `ModeSpec` rows for **every** current `ConversionMode` value.
- Create thin `ChdmanTool`/`DolphinTool`/`Z3dsTool` that **delegate** to the
  existing `chdman_service`/`dolphin_tool_service`/`z3ds_compress_service`
  singletons (no logic moved yet).
- Add `registry` singleton.
- **Tests:** new `tests/test_tool_registry.py`, assert every
  `ConversionMode` resolves to exactly one tool; assert
  `registry.spec(m).supports_delete_on_verify` matches today's
  `supports_delete_on_verify(m)` for all modes (characterization).
- Risk: ~none (additive).

### Phase 1: Replace prefix checks with `ModeSpec` in `convert.py`
- Swap `mode.startswith(...)` / `== "z3ds_compress"` and
  `supports_delete_on_verify` for `registry.spec(mode)` field reads.
- Keep the duplicated single/batch structure for now.
- **Tests:** existing `test_mode_parity_fixes.py` + convert route tests must
  pass unchanged.
- Risk: low; pure substitution guarded by characterization tests from Phase 0.

### Phase 2: Route output-path resolution through the registry
- `convert._get_output_path` (`:100`) → `registry.for_mode(mode).output_path(...)`.
- `job_manager._queue_job_locked` (`:139`) → same.
- Implement `output_path()` on each tool by calling the existing
  `get_output_path_for_mode` / `get_chd_path`.
- **Tests:** add path-equivalence tests (old vs new) for a matrix of modes ×
  archive/non-archive × output_dir/none.
- Risk: low-medium (path edge cases: `extractcd` `.cue`, archive stems). The
  equivalence test is the safety net.

### Phase 3: Route conversion + verify dispatch through the registry
- `job_manager._process_job` `_convert_service` ladder (`:1444`) →
  `registry.for_mode(job.mode.value)`.
- Verify branch (`:1556`, `:1591`) → `plugin.verify(output_path)` uniformly
  (drops the z3ds special-case).
- Move the disc-ID embed (`:1514`) into `ChdmanTool.post_convert`; job_manager
  calls `plugin.post_convert(...)` generically.
- **Tests:** job-pipeline tests (`test_external_job_api.py` neighbors), plus a
  conversion smoke test per tool with the binary stubbed.
- Risk: medium (hot path). Ship behind close review; the behavior is a 1:1
  re-route.

### Phase 4: Deduplicate single vs batch in `convert.py`
- Extract `plan_job(file_path, mode, output_dir, duplicate_action, …) -> JobPlan|Skip`.
- `create_job` and `create_batch_jobs` both call it.
- **Tests:** `test_mode_parity_fixes.py` becomes near-trivial (one code path);
  keep it as a regression guard.
- Risk: medium; well-covered by the existing parity suite.

### Phase 5: Extract `SubprocessRunner`; slim the three services
- Add `runner.py`; refactor `convert()` in each tool to use it.
- Move the real `chdman`/`dolphin`/`z3ds` logic *into*
  `services/tools/{chdman,dolphin,z3ds}.py` (Phase 0 left them delegating).
  The old `services/chdman.py` etc. become shims re-exporting the singleton
  for any stragglers, or are deleted once imports are updated.
- **Tests:** service-level tests (`test_z3ds_verification_service.py` etc.),
  plus a cancel-path test (cancel_event → `ConversionCancelled` + partial
  output cleaned).
- Risk: medium-high (subprocess timing/cancel/stall). Do tool-by-tool, not all
  at once; keep `ConversionCancelled` in its current import location to avoid
  breaking `job_manager`'s `except`.

### Phase 6: Generic info/verify routes from the registry
- Replace the three endpoint trios in `info.py` with the factory (§3.5) +
  shared SSE adapters. Maintain the `tool_id → url_prefix` alias map so paths
  (`/api/chd-verify`? no, keep `/api/verify`, `/api/dolphin-verify`,
  `/api/z3ds-verify`) are byte-for-byte identical.
- **Tests:** `test_z3ds_routes.py`, `test_dolphin_routes.py`, and CHD verify
  route tests must pass without edits (same URLs, same events).
- Risk: medium; the SSE event names/shapes are load-bearing for the frontend,
  assert them explicitly.

### Phase 7: Generic `FileEntry` outputs + registry loop in `files.py`
- Add `convertible_by` / `outputs`; populate from `registry.all()`.
- Keep legacy booleans (`has_chd`, `dolphin_ready`, …) populated **in
  parallel** so the frontend keeps working.
- **Tests:** `test_volume_discovery.py` + a listing test asserting both legacy
  flags and new `outputs` agree.
- Risk: low (additive on the model).

### Phase 8: Frontend descriptor table
- Add `static/js/tools.js`; migrate the ~10 `app.js` branch sites + `api.js`
  to consume it.
- Switch `FileList` badges to read `entry.outputs`/`convertible_by`.
- **Tests:** manual UI pass (no JS test harness in repo), browse, convert,
  verify, info modal, for each tool; confirm SSE progress + badges.
- Risk: medium (no automated FE tests). Verify in a browser per
  `ADDING_PLATFORMS_AND_TOOLS.md` §15 (no build step, edit JS directly).

### Phase 9: Cleanup (optional, behavior-preserving)
- Remove the now-unused legacy `FileEntry` booleans once the frontend reads
  `outputs` exclusively.
- Delete the old `services/{chdman,dolphin,z3ds_compress}.py` shims; update
  imports.
- Consider deriving `config` binary paths from the registry.
- **Tests:** full suite + manual UI pass.

---

## 6. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Hot-path regression in `_process_job` (Phase 3/5) | 1:1 re-route; characterization tests from Phase 0; tool-by-tool rollout |
| Subprocess cancel/stall behavior drift (Phase 5) | Keep `ConversionCancelled` import location; add explicit cancel + stall tests before refactoring |
| SSE event-shape drift breaks UI (Phase 6) | Snapshot-test event names/payloads; keep identical URLs via alias map |
| No automated frontend tests (Phase 8) | Gate on a manual browser checklist; ship FE last, after backend is stable |
| Output-path edge cases (`extractcd` `.cue`, archive stems) (Phase 2) | Old-vs-new path equivalence matrix test |
| Over-abstraction | Stop at an in-process registry; no dynamic loading; `BaseTool` only hoists code that is *already* duplicated 3× |
| Scope creep / long-lived branch | Each phase is a separate, shippable PR behind the existing test suite |

## 7. Testing strategy summary

- **Characterization first (Phase 0):** lock current behavior (mode→tool,
  capability flags, output paths) into tests *before* moving code.
- **Equivalence tests** for the mechanical re-routes (paths, dispatch).
- **Lean on existing suites:** `test_mode_parity_fixes.py` (single/batch),
  `test_*_routes.py` (endpoints + SSE), `test_*_service.py` (subprocess),
  `test_volume_discovery.py` (listing).
- **Manual UI checklist** for Phase 8 (no JS harness).
- Every PR: `ruff check app tests && pylint app && pytest -q tests`
  (+ `hadolint Dockerfile` if touched), per `ADDING_PLATFORMS_AND_TOOLS.md` §11.

## 8. Estimated impact

| Area | LOC delta (rough) |
|------|-------------------|
| New `services/tools/` (registry, spec, base, runner) | +400 |
| `services/{chdman,dolphin,z3ds}` after `SubprocessRunner` | −300 (dedup) |
| `routes/info.py` after verify factory | −600 (dedup) |
| `routes/convert.py` after single/batch merge + spec checks | −120 |
| `routes/files.py` after registry loop | −60 |
| Frontend `app.js`/`api.js` + new `tools.js` | net ~0 (moved, not added) |

Net: a smaller, flatter codebase where the cost of the *next* tool drops from
~20 files to ~5.
