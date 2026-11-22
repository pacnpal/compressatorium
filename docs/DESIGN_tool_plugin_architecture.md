# Design: Tool Plugin Architecture

**Status:** Implemented (the code under `app/services/tools/` is the source of truth)
· **Scope:** internal refactor, no user-facing behavior change ·
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


### 2.1 Web/API authentication boundary

The FastAPI app can apply a single HTTP middleware (`app/auth.py`) before static
files and all routers. The entire auth system is gated on
`COMPRESSATORIUM_ENABLE_AUTH` (default `false`): when disabled, the middleware is
not registered and no token is generated. When enabled, it protects the Web UI
and every `/api` route with the same token check, while leaving `/health` open
for container health checks. Tokens are read from headers only (HTTP Basic,
`Authorization: Bearer`, or `X-Compressatorium-Token`) — never the query string —
and state-changing requests (`POST`/`PUT`/`PATCH`/`DELETE`) are additionally
restricted to same-origin callers (`Sec-Fetch-Site`/`Origin`) so cached browser
Basic credentials can't be abused cross-site. Keep new routers behind that
app-level middleware rather than adding per-router auth branches.

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

The package today also holds `binaries.py` (the `RequiredBinary` probe),
`chain.py`, and one plugin per further tool: `nsz.py`, `maxcso.py`, `romz.py`,
`makeps3iso.py`, `nkit2iso.py`, `jwud.py`, `xiso.py`, `rom_converto.py`,
`rar.py` and `exodos.py`. `__init__.py` registers all fourteen tools.

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
    tool_id: str                    # the owning plugin's id ("chdman", "dolphin", "rar", ...)
    kind: ModeKind
    label: str                      # UI label
    group: str                      # UI group id ("create","extract","copy","dolphin","z3ds")
    output_ext: str | None          # ".chd"/".rvz"/None when input-ext-mapped
    input_extensions: frozenset[str]
    supports_compression: bool = False
    supports_compression_level: bool = False   # dolphin rvz/wia only
    supports_delete_on_verify: bool = False  # may this job delete its source?
    supports_verify: bool = False            # can its output be checked at all?
    source_kept_reason: str = ""               # why it never deletes its Source (see 3.3.9)
    delete_proof: DeleteProof = DeleteProof.ROUND_TRIP  # or RECOGNIZED (see 3.3.12, #423)
    allows_archive_input: bool = False         # opt-in; set True on every mode that can take a member straight from an archive (chdman create + extract, dolphin, z3ds). Only chdman copy (.chd recompress round-trip) leaves it False
```

`spec.py` carries more fields than this sketch, each with a default: the codec
and level fields (`requires_codec`, `codecs`, `default_compression`,
`codec_levels`, `implicit_codecs`, `implicit_codecs_unknown`, `block_size`,
`block_size_rule`), `supports_round_trip` and `round_trip_required` (§3.3.12),
`companion_exts`, `input_kinds`, `platform_slugs`, `reads_extensions`,
`auto_target`, `auto_platform_slugs` and `automatable`. `round_trip_required`
means the container-only opt-out never applies: every delete-source job in the
mode runs the Round-trip. It is for an output with no container of its own to
check (the rar tool's extracted file).

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

    # The file to verify once a job finishes, or None when the product cannot
    # be found. Nearly always `output_path`; a split makeps3iso build writes
    # `<iso>.0`/`.1`/… and no bare `.iso`, so verifying the planned path failed
    # a conversion that had worked. BaseTool returns `output_path`.
    def verify_target(self, output_path: str, mode: str) -> str | None: ...

    # Whether deleting the source is justified for *this* job's settings, not
    # just whether the mode offers the option (`supports_delete_on_verify`).
    # BaseTool returns True; a tool whose verification can be weakened by a
    # compression choice overrides it — jwud returns False for `noverify`,
    # whose structural WUX check proves the geometry but not the bytes. Asked
    # at both plan sites and again in `_process_job`, right before the delete.
    def delete_on_verify_is_safe(self, mode: str, compression: str | None) -> bool: ...

    # Callers go through run_convert(), which resolves `compression` with
    # compression_for() first. `split` is honored only by makeps3iso.
    def convert(self, input_path: str, output_path: str, mode: str, *,
                compression: str | None = None, split: bool = False,
                cancel_event: asyncio.Event | None = None) -> AsyncGenerator[dict, None]: ...

    # Verify is bounded and cancellable on the same terms as convert (issue
    # #266): cancel_event is the job's own event, so Cancel stops the verifier
    # instead of being observed only after it finishes; a run cut short by it
    # returns {"valid": False, "cancelled": True} — it reached no verdict, so no
    # caller may record it as a verification failure or delete a source on it.
    async def verify(self, path: str, *,                             # {"valid","message"}
                     cancel_event: asyncio.Event | None = None) -> dict: ...
    def verify_stream(self, path: str, *,
                      cancel_event: asyncio.Event | None = None
                      ) -> AsyncGenerator[dict, None]: ...
    # Wall-clock bound for verifying this path (0 = unbounded). Default:
    # BaseTool resolves the shared size-scaled policy for `policy_owner`, so a
    # per-tool COMPRESSATORIUM_<OWNER>_VERIFY_TIMEOUT applies. Exposed so
    # job_manager can bound the whole verify() call — including a tool whose
    # verify spawns nothing (jwud's container walk, makeps3iso's PARAM.SFO
    # readback) — without re-deriving which knob that tool reads.
    async def verify_timeout(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> int: ...
    async def info(self, path: str) -> dict: ...                    # raw dict
    def info_model(self, raw: dict, path: str) -> BaseModel: ...    # typed model for the API
    # The five simple "what is this file" models (z3ds/nsz/cso/romz/makeps3iso)
    # subclass models.BasicFileInfo (file/size/size_display/format/extension/
    # compressed/compression_type); their info_model() builds the shared fields
    # via BaseTool._basic_info_fields(raw) and adds only their extras, so the
    # raw->model mapping is defined once.

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

    # Post-convert hook, run once after a successful conversion. Default no-op
    # (BaseTool); ChdmanTool overrides it to record the disc ID it reads from
    # the Source in chd_metadata_store for a freshly created CD/DVD CHD. Nothing
    # is written into the CHD: chdman opens a compressed CHD read-only (MAME
    # chd.cpp: m_allow_writes = !compressed()), so `chdman addmeta` fails on
    # every CHD this app makes (#352). job_manager calls it generically for
    # every mode, and ChainTool routes its final step through the same hook, so
    # a cso_to_chd CHD gets its disc ID exactly like a direct createdvd.
    async def post_convert(self, input_path: str, output_path: str, mode: str,
                           *, within: Sequence[str] | None = None) -> None: ...

    # Sibling outputs a multi-file mode writes beside its primary output_path
    # (extractcd's .cue + .bin data track; a split folder_to_iso's .iso.0/.1/…).
    # The single source conflict-check, unique-name probing, overwrite-clear,
    # size-sum and in-use tracking enumerate, instead of each re-encoding the
    # per-mode suffix. output_path itself is never included.
    def companion_outputs(self, output_path: str, mode: str) -> list[str]: ...

    # Every path an authorized overwrite must sweep, primary FIRST. A superset
    # of companion_outputs: it includes output_path, and it enumerates states
    # only a *failed* run leaves (a makeps3iso -s build interrupted mid-split
    # leaves the not-yet-renamed base AND numbered parts, which never coexist
    # on success — so companion_outputs, which describes a finished output,
    # would under-report). Enumeration only; the pipeline validates and unlinks.
    def overwrite_targets(self, output_path: str, mode: str) -> list[str]: ...

    # Runtime prerequisites met? False => GET /api/tools reports the tool
    # unavailable, with `not_ready_message` in its `reasons` map (#515), and
    # the dashboard lists it under "Not ready here" with that fix, instead
    # of offering jobs that can only fail. BaseTool answers it from `required_binaries` (the programs
    # the tool runs, through the shared probe, §3.4 "Declaring the programs a
    # tool runs"); nsz overrides it (prod.keys), exodos (always ready, #513),
    # and so do rom_converto (its `--version` probe) and the chain (every step).
    # Async because the probe may touch disk. `not_ready_reason` / `verify_not_ready_reason` say
    # why it answered False; callers show them through `not_ready_message`
    # (§3.4) rather than formatting their own line.
    async def is_ready(self) -> bool: ...

    # Whether info() runs the tool's program. True (BaseTool's default): the
    # File info route asks is_ready() first and answers 503 when it is False.
    # False: info() reads the file in Python and answers regardless (§3.4).
    info_needs_tool: bool
```

The Protocol in `base.py` has more members than this listing. The ones a new
tool is most likely to need:

- `convert_refusal(path) -> str | None`: why `converts_path(path)` said False,
  in the user's words, shown in place of the fixed skip reason. Default None.
  Asked only after `converts_path` refused, and may read the disk (the rar tool
  names the files a set holds when it holds more than one).
- `source_companions(path) -> list[str]`: the other files the source is made of
  (a split Wii U dump's parts, a RAR set's later volumes), never `path` itself.
  Default `[]`. Raise `CompanionsUnknown` when the set can't be read now; never
  answer `[]` for a set you can't read, or a delete would take the named file
  alone.
- `accepts_directory(path)` / `converts_path(path)`: per-path refinements of the
  extension claim.
- `detect_output(input_path, *, from_archive=False)`, `expected_output_size`,
  `compression_for(mode, compression)` and `default_compression`.
- `verify_is_ready()` (defaults to `is_ready()`), `verify_not_ready_reason`,
  `claims_check`, `verifies_path`, `scannable_path`.
- The content hooks: `header_platform`, `header_reads`, `raw_image_sha1`,
  `content_sha1`, `content_digest`, `source_problem`, with the attributes
  `disc_extensions`, `raw_image_hash_labels`, `raw_image_hash_rebuilds_content`
  and `platform_slugs`.

`BaseTool(ABC)` provides shared defaults so concrete tools stay tiny:

```python
class BaseTool:
    id: str
    display_name: str
    modes: Sequence[ModeSpec] = ()

    def __init__(self, binary_path: str):
        self.binary_path = binary_path
        # `policy_owner or id`, never bare `id`: the owner keys the shared
        # nice/ioprio/timeout policy's per-tool overrides, and it differs from
        # the plugin id wherever the service kept its historical name
        # (dolphin/dolphin_tool, cso/maxcso). In the tree today each *service*
        # constructs its own runner with that owner as a literal.
        self._runner = SubprocessRunner(owner=self.policy_owner or self.id)

    # derived sets (no per-tool duplication)
    @property
    def input_extensions(self) -> frozenset[str]:
        return frozenset().union(*(m.input_extensions for m in self.modes))

    def spec(self, mode: str) -> ModeSpec:
        for m in self.modes:
            if m.mode == mode:
                return m
        raise KeyError(mode)

    # The SubprocessRunner "owner" this tool's service runs under, which keys
    # the shared nice/ioprio/timeout policy's per-tool overrides. NOT always
    # `id`: the plugin id is the routing/UI name ("dolphin", "cso") while the
    # owner is the historical service name ("dolphin_tool", "maxcso").
    policy_owner: str | None = None

    # Default: the shared size-scaled verify bound for this tool's owner.
    async def verify_timeout(
        self, path: str, *, cancel_event: asyncio.Event | None = None,
    ) -> int:
        return await resolve_verify_timeout(
            path, self.policy_owner, cancel_event=cancel_event,
        )

    # verify() is the same drain-verify_stream()-and-keep-the-terminal-event
    # wrapper in every service, so it lives once in subprocess_runner as the
    # shared `collect_verify(stream, fallback_message=...)` (it also carries the
    # "cancelled" flag through to the result).
    async def verify(self, path, *, cancel_event=None) -> dict:
        return await collect_verify(self.verify_stream(path, cancel_event=cancel_event),
                                    fallback_message="Verification failed")

    def active_pids(self) -> list[int]:
        return self._runner.active_pids()

    async def post_convert(self, *a, **k) -> None:
        return None

    # Default: suffix-swap off output_path for each ModeSpec.companion_exts
    # (extractcd .cue -> .bin). makeps3iso overrides with its disk-probed
    # contiguous split parts using direct .0/.1/... probes rather than sibling
    # directory scans; modes with no companion_exts return [].
    def companion_outputs(self, output_path, mode) -> list[str]:
        return [str(Path(output_path).with_suffix(e)) for e in self.spec(mode).companion_exts]

    # Default: the finished output set. Correct for every mode whose failed
    # runs leave nothing a successful run wouldn't; makeps3iso overrides.
    def overwrite_targets(self, output_path, mode) -> list[str]:
        return [output_path, *self.companion_outputs(output_path, mode)]

    # The declared programs, each present and executable (#494). A tool
    # that declares none (exodos) is ready; the chain overrides it to ask
    # every step tool. Both are properties, read at each probe, so a tool
    # declares the path its service really spawns.
    @property
    def required_binaries(self) -> tuple[RequiredBinary, ...]: return ()
    @property
    def verify_required_binaries(self) -> tuple[RequiredBinary, ...] | None: return None
    async def is_ready(self) -> bool:
        return await self._binaries_ready(self.required_binaries, "convert")

    # subclasses implement: output_path, convert (via self._runner.run),
    # verify_stream, info, info_model
```

> **Normalization note:** `info()` is async on the contract.
> `chdman.info`/`dolphin.header` are already async; `z3ds.info` is sync, so
> `Z3dsTool.info` wraps it in `run_in_threadpool`. `dolphin` exposes header
> data via `header()` today, `DolphinTool.info` simply calls it.

### 3.3 `runner.py`: shared subprocess orchestration

This collapses the ~150 near-identical lines that were duplicated in every
tool's `convert()`. All twelve tools that spawn a program now delegate their
streaming loop here: `chdman`, `dolphin_tool`, `romz`, `makeps3iso`, `nkit2iso`,
`jwudtool`, `extract_xiso`, `rom_converto` and `rar_extract` directly, and
`z3ds_compress`, `maxcso`, `nsz` via the size-based-progress seam below (their
CLIs print no parseable percent, so the growing output file is the progress
signal).

```python
class SubprocessRunner:
    def __init__(self, owner: str):
        self._owner = owner
        self._active_pids: set[int] = set()
        self._lock = threading.Lock()

    def active_pids(self) -> list[int]: ...

    async def run(self, cmd: list[str], *, input_path: str, output_path: str,
                  parse_progress, cancel_event=None, heartbeat=False,
                  fail_label="process", complete_message="Conversion complete",
                  cwd=None, output_growth_paths=None, mode=None,
                  env=None, require_output=False) -> AsyncGenerator[dict, None]:
        """Spawn cmd, stream stdout, yield {"progress","message"}.
        Handles: nice/ionice wrap, stdbuf, PID tracking, deterministic \\r/\\n
        line buffering (CRLF/CR normalized to LF in one pass via
        `_split_stream_lines` so segmentation is a pure function of the byte
        stream, not chunk boundaries -- issue #183),
        stall timeout via compute_progress_stall_timeout, cancel-watcher
        (terminate->kill via the bounded `reap()` ladder), ConversionCancelled,
        (the watcher always gets its turn before the post-EOF reap when a cancel
        is pending, so a child whose stdout ended before the watcher ran is
        still reported cancelled, not "return code -15"; a child that had
        already exited stays complete),
        non-zero exit -> RuntimeError(tail), a signal -> RuntimeError in
        plain words (see "A tool's failure in a job's error" below),
        final 100%. `parse_progress(line) -> int|None` is the only per-tool knob
        in the common path. `cwd` sets the working directory (romz runs `7z a`
        from the ROM dir).

        Opt-in seams, each defaulting off so the direct callers are unaffected:
        - `heartbeat` — dolphin's 2s "Converting... (Ns)" keep-alive.
        - `output_growth_paths()` — widen the stall probe to a set of files whose
          summed size grows even as filenames change mid-run (makeps3iso split).
          `files_in_folder(folder)` builds one over every file in a private
          work folder, for a tool that writes under a temp name of its own and
          renames at the end (rom-converto); see "A tool that names its own
          temp file" below.
        - `mode` — the conversion mode, used to look up an expected output/input
          size ratio in `SIZE_RATIOS` so the size-growth fallback (below) can
          emit a percentage as well as a message. Optional: a mode with no row
          still reports bytes and rate.
        - `env` — forwarded to the subprocess (nsz's private keys-home).
        - `require_output` — treat a clean exit (return code 0) that left no file
          at `output_path` as a failure, raised as RuntimeError(tail) like the
          non-zero-exit path, so a tool that exits 0 without producing output
          still reports the reason it printed first (nsz, whose `output_path` is
          the temp file the runner already watches).
        """

    # A tool's failure in a job's error (#455) ------------------------------
    #
    # `killed_by_signal_message(label, returncode)`: a negative return code is
    # a signal, so the job's error says "<label> was killed (signal 9), most
    # likely out of memory" for SIGKILL the app didn't send (a cancel and a
    # stall are reported as themselves), and "<label> was killed (signal 11,
    # SIGSEGV)" for any other. The tool's last output is a progress line, not
    # the reason, so it goes to the log only. run() and hash_stdout() use it;
    # killed_verify_error() is the verify counterpart.
    #
    # `clean_tool_output(text)`: drops escape sequences and control
    # characters (a progress bar's backspaces, a coloured "ERROR:"), turns a
    # carriage return into a line end, folds runs of blanks and drops blank
    # lines. The runner cleans the output tail it puts in a RuntimeError, and
    # JobManager cleans every job error it records: a conversion's
    # `error_message = clean_tool_output(str(exc))` and every external job's
    # `finish_external_job(error_message=...)`. So a tool needs no wiring, and
    # a service that builds its own message from tool output is covered at
    # the sink.
    #
    # The status line gets the same cleaning (#466). Every status message set
    # from a tool's progress goes through JobManager's
    # `_set_status_message(job, message)`: a conversion's progress updates
    # (and the `warning` copied from one), `update_external_job(message=...)`,
    # `_set_job_message` and `finish_external_job_cancelled(message=...)`. A
    # failed job's `message` is then set to its cleaned `error_message`, so it
    # says why the job stopped instead of keeping its last progress text.

    async def reap(self, process, *, exit_timeout=_EXIT_GRACE) -> bool:
        """Bounded teardown for any spawned child. True if reaped, False if
        abandoned. Escalates: wait exit_timeout for a voluntary exit, then
        SIGTERM + grace, then SIGKILL + grace, then give up. A child blocked in
        uninterruptible I/O (D state) survives SIGKILL, so every wait must be
        bounded -- an unbounded one blocks the job forever and, at
        MAX_CONCURRENT_JOBS=1 (the default, which runs jobs inline in the
        dispatcher), every job queued behind it (issue #263). The single
        teardown path for run(), run_capture() and their finally blocks.
        """

    async def run_capture(self, cmd: list[str], *, timeout=None,
                          cancel_event=None, stderr_to_stdout=False,
                          env=None, on_abandoned=None,
                          verdict=False) -> tuple[int | None, bytes, bytes]:
        """One-shot counterpart to run(): buffered (returncode, stdout, stderr)
        for tools that need a result rather than streamed lines (info / header /
        embedded-hash extraction). Same PID tracking; races communicate()
        against cancel_event + timeout and terminates (TERM->KILL) on either,
        reporting returncode None to signal the abort. Used by
        `dolphin_tool.disc_hashes` so `dolphin-tool verify --algorithm sha1`
        (the Dolphin disc-hash source for `embedded_hashes`) aborts promptly
        when a scan/match job is cancelled. `env` mirrors run()'s, for a tool
        that needs a private environment. `on_abandoned(pid)` is called for
        a child that survives SIGKILL. `verdict=True` (capture_verify) spawns
        the tool bare, with no `nice`/`ionice` prefix, and applies priority
        to the child afterwards, so its exit code is the tool's own verdict.
        """

    # --- verify: two shapes, no per-tool loops (issue #266) ---------------

    async def run_verify(self, cmd: list[str], *, path: str, parse_progress,
                         success_message: str, failure_message: str,
                         cancel_event=None) -> AsyncGenerator[dict, None]:
        """Streaming verifier (chdman, dolphin-tool): spawn, segment lines with
        the shared `_split_stream_lines`, yield {"type","progress","message"}
        and one terminal complete/error. Owns every bound the conversion path
        has — the size-scaled overall bound from `resolve_verify_timeout(path)`,
        the no-output stall bound, `cancel_event`, and the `reap()` ladder
        instead of a bare `process.wait()` (which on a D-state child never
        returns and freezes the queue). The post-EOF voluntary-exit grace is
        capped by whatever is left of the verify's own bound — and skipped
        entirely once a cancel has fired — so a verifier that closes its pipe
        without exiting cannot hold the lane past its deadline. `parse_progress` is the only per-tool
        knob; it replaced two near-identical ~120-line loops.
        """

    async def capture_verify(self, cmd: list[str], *, path: str,
                             success_message: str, cancel_event=None, env=None,
                             start_message="Verifying integrity..."
                             ) -> AsyncGenerator[dict, None]:
        """One-shot verifier (maxcso --crc, nsz -V, 7z t): the same event shape
        around a single run_capture() with the same bound and cancel_event.
        run_capture reports both an abort and a timeout as returncode None, so
        the two are told apart by asking the cancel event which happened.
        """


# Module-level, not methods: cleanup runs after run() has already given up, and
# chains (which own no runner) need the same bound. See "Cleaning up partial
# output is bounded too" below.
async def remove_partial_output(*paths: str, discover=None,
                                label="partial output", timeout=None) -> bool:
    """Bounded unlink of every file a failed run may have left behind.
    True if the sweep finished, False if it timed out or failed. Raises only
    CancelledError. `discover()` enumerates further paths from inside the
    bounded worker, for a set only knowable by probing the disk.
    """

async def remove_partial_tree(path: str, *, label="work directory",
                              timeout=None) -> bool:
    """The same bound for a private work/scratch dir (rmtree, errors ignored)."""
```

`collect_verify(stream, *, fallback_message)` is the module-level reducer every
`verify()` uses to turn one of those streams into `{"valid","message"}`
(plus `"cancelled"`).

Per-tool `convert()` becomes ~15 lines: build argv, then
`async for u in self._runner.run(cmd, ..., parse_progress=self._parse_progress): yield u`.
nsz also forwards its keys-home `env=`, sets `require_output=True`, and writes
into a private work dir, moving the result onto `output_path` after `run()`
returns.

### How priority reaches a child process (issue #444)

The runner applies the priority policy, never the tool. A tool passes the
bare argv; it never adds `nice`/`ionice` itself. There are two shapes, by one
rule: **a verdict never comes from a process started through an `execvp`
wrapper.** `nice` and `ionice` exec through `execvp`, which hands a file the
kernel refuses (empty, a bare `#!`) to `/bin/sh`, and its exit 0 would read
as a pass (CodeRabbit 4616898119, 4617217666).

- **Wrapped** (`run`, and `run_capture` for info and header reads): the argv
  is `priority_prefix(owner) + cmd`, built in one place (`_wrapped_command`):
  `nice -n <level>` then `ionice -t -c <class> -n <level>`, resolved from the
  `COMPRESSATORIUM_TOOL_*` settings and any `COMPRESSATORIUM_<OWNER>_*`
  override. Their exit codes are no verdict on their own (a conversion's
  output is checked after it).
- **Bare, then reniced** (`capture_verify` through `run_capture(verdict=True)`,
  `run_verify`, and `hash_stdout`, whose digest is the Round-trip's): the tool
  is spawned directly, so the kernel's ENOEXEC is the only exec semantics and
  a verifier that can't start (missing, not executable, a missing
  interpreter) raises, which both verify paths report as inconclusive
  ("Verification could not run"). The priority is then applied to the child
  by its pid, best effort (`_prioritize_child`): `os.setpriority` to the
  increment over this process that `nice -n` gives, a syscall made inline,
  and `ionice -t ... -p <pid>` as an asyncio child of its own, from the
  cached path only, that the spawn never awaits, so a stalled one can't hold
  back the verify's deadline or cancel, which start at once. It stays out of
  the shared probe pool: bounded (`_IONICE_SECONDS`), killed and reaped on a
  stall, and at most `_MAX_IONICE_TASKS` in flight; past that the child gets
  no I/O priority (CodeRabbit 4617447724). One that outlives SIGKILL turns
  the step off for the rest of the process, with one warning, since its slot
  could never be counted back (CodeRabbit 4617587026). Either failing leaves the child at
  normal priority. Their argv comes from the same builder,
  `_wrapped_command(wrap=False)`.

A wrapper binary that isn't installed omits only its own prefix; the other
enabled one still applies. Each is looked up once per process, the first
time its setting asks for it, and cached as an absolute path (`_wrapper_path`;
nothing is scanned at import, and a tool started in another folder still
finds it). A spawn makes that lookup off the event loop, bounded
(`_resolve_wrappers` over `_bounded_probe`), with at most one lookup per
wrapper outstanding, even after its caller gave up on it, so a PATH entry
that never answers costs one probe thread. A lookup that doesn't answer runs
that spawn with no wrappers at all, both omitted, at normal priority, with
nothing cached, so a later spawn tries again. A wrapped spawn that fails to
start, or a wrapped run that exits 126/127 (an outer `nice` that starts while
its cached inner `ionice` is gone exits 127), forgets the cache, so a wrapper
removed since is looked up again; a tool's own 126/127 costs one extra
lookup. Building a prefix (`nice_prefix`, `ioprio_prefix`) reads the cache
alone and never scans. `ionice -t` is best effort, like the renice: a class
the kernel denies (realtime without CAP_SYS_ADMIN) doesn't stop ionice before
it runs the tool, which would otherwise hand back ionice's exit 1 as the
tool's. A nice level this process can't apply is left off (`_can_renice`, the
kernel's own rule: lowering niceness needs CAP_SYS_NICE, read once from
`CapEff`, or room under RLIMIT_NICE; being root is not the test): `nice` would
warn and run the tool anyway, putting its warning into output a caller
parses, where the old `preexec_fn` renice dropped the failure silently.
rom-converto's `--version` probe reads the version line from stdout's first
line or from stderr, skipping a wrapper's diagnostics. A wrapped run's exit
code reaches its caller as it is (dolphin-tool's hash read falls back to no
hashes on a non-zero exit).

Nothing passes `preexec_fn`. This process is multithreaded (threadpools, the
SSE routes), and forking a Python callable from it can deadlock the child on a
lock it inherited before `exec`: that hangs inside `create_subprocess_exec`,
before the PID is tracked or any timeout is installed, so no bound could rescue
it. `nice`/`ionice` only exec. `test_nothing_in_app_passes_a_preexec_fn` fails
on any `preexec_fn=` anywhere in `app/`.

### Every tool reports status (issue #263)

Progress reporting is **not** a per-tool responsibility, and no tool hand-rolls
it. The runner owns both signals and picks between them:

1. **Native, preferred.** `parse_progress(line) -> int|None` parses the tool's
   own percentage. The moment it returns a real percent, that tool is reporting
   for itself and the fallback stands down for the rest of the run — the two
   never compete for the message line.
2. **Output growth, automatic fallback.** When native parsing never yields a
   percent — the common case, because most of these CLIs draw a TTY bar that
   goes silent on a pipe — the runner reports from the output file growing on
   disk: `output_size_message()` emits **MB written and a MB/min rate**. This
   needs no per-tool wiring at all, so *a tool that does nothing gets it*.
   Passing `mode` adds a percentage when `SIZE_RATIOS` knows that mode's
   expected output/input ratio.

The rate matters as much as the byte count: it is what distinguishes a job that
is merely slow (bytes climbing, MB/min collapsing) from one that has stopped
dead. Before this, dolphin-tool reported only elapsed seconds against a 0% bar,
and users could not tell a crawling conversion from a hung one.

### A tool that names its own temp file (`files_in_folder`, #422)

The growth signal above watches `output_path`, which works for every tool that
writes there as it goes. rom-converto does not: it writes
`.Game.zar.<random>.tmp` beside the path it was given and renames it at the very
end, it prints nothing on a pipe (no TTY, no progress), and what it does print
goes to stderr. Watching `output_path`, such a run shows no growth at all, so
the stall watchdog would kill a healthy multi-gigabyte ZAR job at
`CHD_PROGRESS_TIMEOUT` and the bar would never move.

`subprocess_runner.files_in_folder(folder)` returns an `output_growth_paths`
callable that lists every regular file directly in *folder* (not following
subfolders or links; a missing folder is no files). The tool is run into a
private work folder beside the output (hidden, mode 0700, a same-filesystem
rename away from the output), and the folder's summed size becomes the growth
signal: the watchdog sees activity, the size fallback reports bytes and rate
(and a percentage when `SIZE_RATIOS` has the mode, `zar_compress` 0.5), and none
of it depends on the name the tool picks. The callable runs in the runner's
probe thread, never on the event loop. `tests/test_subprocess_runner.py` pins
both halves: such a child stalls out on the default probe and completes with
`files_in_folder`. Any later tool with the same habit passes the same helper.

stderr needs nothing new: `run()` already merges it into the stream it reads.

### Verify is bounded and cancellable (issue #266)

The conversion path was bounded first (#263/#265); verify had the same gap. A
verify that never returned never ended, and because `MAX_CONCURRENT_JOBS`
defaults to 1 and runs jobs inline in the dispatcher, it froze the whole queue —
reachable through **Delete sources after verification**, a commonly used option.
The delete-on-verify call site also passed no `cancel_event`, so Cancel set an
event that the `await` never observed.

Three pieces, none of them per-tool:

1. **A bound, on by default.** `resolve_verify_timeout(path, owner)` is the one
   source of truth: `COMPRESSATORIUM_TOOL_VERIFY_TIMEOUT` (baseline, 1800s) plus
   `…_PER_GIB` (600) for the file actually being read, capped by `…_CAP`
   (86400) — the same baseline+per-GiB+cap shape as the conversion stall
   watchdog, sharing `timeout_policy.compute_size_scaled_timeout`. Verify reads
   the whole file, so its runtime scales with size and a flat number cannot
   serve both a 400 MB CIA and a 90 GB PS3 ISO. Streaming verifiers additionally
   get `COMPRESSATORIUM_TOOL_VERIFY_PROGRESS_TIMEOUT` (600s of no output at
   all), which catches a wedge far sooner. The two are independent: zeroing the
   overall baseline does not zero the stall bound, deliberately — an operator
   who removes the overall bound for a huge image still wants a verifier that
   has gone completely silent to be caught. Zero both for no bound at all.
2. **`cancel_event` through the contract.** `ToolPlugin.verify()` /
   `verify_stream()` take it and `job_manager` passes the job's own event.
   Tools that spawn a child get cancellation from `run_verify` /
   `capture_verify`; the pure-Python verifies (jwud's WUX walk, makeps3iso's
   PARAM.SFO readback) check it between steps. A cancelled run returns
   `cancelled: True`, which `job_manager` turns into a CANCELLED job — never a
   verification failure, and never a reason to delete a source.
3. **A backstop at every call site.** `job_manager` resolves the bound first
   (`bound = await tool.verify_timeout(path, cancel_event=…)`) and then wraps
   the whole `verify()` in `asyncio.wait_for(tool.verify(…), timeout=bound or
   None)`, so the guarantee holds even
   for a tool whose verify spawns nothing for a subprocess timeout to bound.
   Resolving the bound sizes the file, which is itself a stat on the storage in
   question, so it takes the cancel event too: it runs *before* the verify that
   would otherwise observe one. The
   generated verify routes (`register_verify_routes`: sync, SSE, batch SSE) are
   the *other* entry point into the same verifiers, and they hold the `verify`
   workload lane while they run — so they apply the same per-path bound, once,
   in the shared factory rather than per tool. On the two SSE routes that bound
   lives in the **producing task**, never in the consuming loop: a client that
   stops draining suspends the consumer at its `yield`, and a deadline it cannot
   evaluate is no deadline at all. Bounding the producer also makes the clock
   measure verification instead of delivery backpressure, and makes a verdict
   disagreeing with a timeout impossible — one task decides both. The other half
   of that independence is a **bounded** hand-off (`_offer_verify_update` over a
   capped queue): a producer that no longer waits for the socket would otherwise
   let a peer that stays connected without reading buffer events for the whole
   of the bound. Progress is dropped under backpressure — it is a level, not a
   log — while a terminal event always lands, making room by discarding progress
   the verdict has just made stale. The **workload token follows the same rule**:
   it is released **beside the producer's `done.set()`**, not from the consumer's
   `finally`, which a parked reader never reaches; and the batch route takes a
   slot per file rather than carrying one across the walk (the route's admission
   token is handed back before the first file, since a token held across the
   suspensions *before* a verifier starts is lost the same way). A peer that
   stays connected without reading parks the consumer indefinitely, and a
   one-slot lane released only on that path is a lane lost for the life of the
   process. A route-level timeout reports the
   tool's own timeout shape (`{"valid": False, "message": "Verification timed
   out after Ns"}`, widened with `"type": "error"` on the SSE paths), not a 500:
   the file is not known bad, the check just did not finish.

**A conversion's filesystem steps outside its subprocess need a bound too.**
`SubprocessRunner.run`'s stall watchdog only runs while the child does, and a
job has no outer deadline. So a blocking step before the child starts (xiso's
header probe, its filesystem walk, its workspace setup) or after it exits (the
final rename into place) would hold the job, and the single-slot queue behind
it, for as long as a dead mount takes.
`run_detached_within_stall_bound(func, *args, what=..., cancel_event=...,
stall=..., report_progress=...)` is the one stall-bounded detached seam: it
runs `func` on a detached thread and gives up only after a stretch with no
progress. The bound is `stall` seconds or, by default, the limit the
subprocess phase gets (`CHD_PROGRESS_TIMEOUT`, floored the same way); `0`
disables it. On expiry it raises `Stalled`, a `RuntimeError` naming `what`
and, for a walk, the last folder reported. A cancel still raises
`ReadCancelled`, which a conversion must translate to `ConversionCancelled`,
or the job is recorded as failed rather than cancelled. Only the bound's own
expiry is reworded: a `TimeoutError` that fails at once
(`ProbeCapacityExceeded`, an OS `ETIMEDOUT`) keeps its own type and message,
and a call that finished just as the bound ran out is answered.

Without `report_progress` (a conversion's small steps: headers, directory
tables, a rename), `func` reports nothing, so the bound is simply how long it
may take; a whole-file pass belongs under a size-scaled bound instead. With
`report_progress=True` (a walk over a tree that may be large and healthy, or
frozen, #361), `func` is called with a `progress=<Progress>` keyword and
reports every step: `progress()` after each entry, `progress(where)` before
each folder. Each report restarts the clock, so a big Library that keeps
moving is never cut off and a listing blocked on a frozen mount ends at the
bound. Whatever ends the wait, the thread is abandoned (counted like any
other), and the next `progress()` call inside it raises `ProgressAbandoned`,
so a walk that is still moving stops instead of finishing for nobody.
`utils.path_utils.walk_tree(top, progress)` is the walk to use with it:
`os.walk(top)` semantics (top-down, a symlinked folder listed but never
walked into, unreadable folders skipped) built on `os.scandir`, reporting each
folder and each entry. A caller that must not take "skipped" as "not there"
passes `unlisted=[]`, which collects every folder it could not list and every
entry it could not classify (the duplicate finder's sheet walk, 3.3.22). Anything under the progress clock reads entries one at
a time and reports each: `os.listdir` reads a whole folder before its caller
sees an entry, so a huge folder on a slow but healthy mount would pass the
bound while still reading. `utils.path_utils.list_folder(path, progress)` is
the one-folder form (`os.scandir`, one report per entry, sorted afterwards so
order is unchanged), and `folder_has_entries(path)` answers "is it empty" by
reading at most one entry. Reading a file under the clock reports bytes as
they arrive, not loop iterations: `utils.path_utils.open_reporting_reads(path,
progress)` is a read-only binary file (a `BufferedReader` over a raw reader)
that asks the OS for at most `READ_CHUNK` (64 KiB) per read and reports every
read that returns data, and every seek. `zipfile` and `py7zr` take it as
their file, so a `.zip`/`.7z` member table streaming in from a slow but
healthy mount keeps the clock moving, and a read that returns nothing ends at
the bound. A file object never changes what they read: `zipfile` refuses a
multi-disk zip either way, and `py7zr` reads split `.7z.001` volumes only
through an explicit `multivolumefile` object (and `.001` is not a listed
archive extension). `.rar` is different and always opens by path: `rarfile`
finds a multi-volume archive's next volume from the file name and silently
stops after the first volume when given a file object. Its progress comes
from its supported `info_callback`, one report per header parsed, across
every volume. Known limit: a single huge header block on a slow mount reports
nothing while it is read; a complete member list matters more. `rarfile`'s
default `errors="stop"` ends a parse quietly at a missing or broken volume, so
`_read_rar` checks `strerror()` and raises `IncompleteArchive`, and a list
that stopped early is never returned or cached. A later volume of a set is
not an archive of its own (#369): `archive_members.later_volume_of(path,
progress)` is the one place that decides it. It matches the name first
(`<stem>.partN.rar` with N > 1; `.r00`-style volumes are not listed archive
extensions, so they are plain files already), then asks rarfile, which
refuses a volume whose flags say it is not the first (`NeedFirstVolume`), so
a single-volume RAR merely named `x.part2.rar` still lists. The answer is
cached per `(path, mtime, size)`, and it returns the first volume's path. A
later volume lists as a plain `file` row in `GET /files`, with no member
count and no convert or Verify actions, and `scan_tree` skips it. The
`list_archive_contents` call returns no members for it without logging an
error.
`services.archive_members.read_archive_members(path, progress)` opens the
archive through it when given a callback (a cached answer reads nothing),
and `ArchiveService.list_archive_contents(..., progress=...)`,
`_summarize_archive(path, progress)` and `scan_tree` pass the listing's
callback down. No archive is listed through a subprocess, so there are no
output lines to report. Callers without a callback (romz's gate, the
`/archive-summary` batch, the CLI) are unchanged. Separate from the bytes,
each archive member a listing or search checks is one report, because those
checks stat sibling outputs after the read. `scan_tree` lists an archive
before its Verify gate reads it, so that gate is served from the warmed
member cache. Progress callers:
- The metadata scan's file listing (`routes/info.py`, `_WALK_STALL_SECONDS`).
  A stall ends the scan as failed, with a warning naming the folder, and frees
  `_is_scanning` and the `metadata_scan` lane, so a later scan can start.
  Cancel ends it as cancelled.
- Routes go through `routes.volume_guard.off_loop_until_stalled(func, *args,
  what=...)`, which answers 503 on `Stalled` or a full probe ceiling with
  `WALK_STALL_SECONDS` as the bound. The folder listing (`GET /files`) and the
  recursive search (`GET /files/search`, `scan_tree(progress=...)`) use it,
  both through `list_folder`. The delete routes' emptiness check is
  `folder_has_entries` through `off_loop_bounded`.
- Folder Library walks (the Library platforms view, Findings, a Rule sweep)
  already run on a detached thread under a fixed bound, so they are unchanged.
- Routes that check every stored record (#373) take one pass per request
  through `off_loop_until_stalled`, one report per record, and answer 503 on
  a stalled Volume like the routes above: `GET /api/verified` (prune, then
  each record's containment), `POST /api/verified/lookup`, the CHD badge
  lookup `POST /api/chd-metadata`, and `POST /api/dat/prune`. The stores give
  them blocking, progress-aware forms (`prune_missing_blocking(progress)`,
  `all_records_blocking()`, `cached_media_type_blocking(path)`) and read the
  paths and delete the rows in separate sessions, so no session is held open
  while a Volume is asked.
- Pruning a stored record because its file is gone asks for a confirmed
  absence, never `os.path.exists`, which answers False for every error:
  `utils.path_utils.path_presence(path)` is True, False only for `ENOENT` or
  `ENOTDIR`, and None when it can't tell. `ToolRegistry.output_missing(path)`
  is the strict form of `output_exists` (a split output's logical name
  counts as there while its first part is). The verification, DAT-match and
  CHD-metadata stores prune on these, so a record whose check can't answer
  is kept. A prune that was given up on stops before it deletes anything.

If interrupting a step decides what gets cleaned up, base the cleanup on the
step's *completed* outcome, recorded by the worker thread itself. xiso's final
rename shares a small lock-guarded state with its worker:
- The worker marks `done` after `os.replace` returns.
- The handler marks `abandoned` when the job gives up.

The handler removes `output_path` only if the rename was already done. If the
rename hadn't finished (not started, or stuck on the mount), the handler leaves
the destination alone, because this job didn't write whatever is there. A worker
whose rename lands after the job gave up removes its own product instead. A flag
on the event loop, set before or after the `await`, can't make this
distinction. The lock is held only for those two assignments, never across I/O,
so taking it on the event loop can't block.

The placement itself must not overwrite anything either, because a move that
lands late replaces whatever is at the destination by then.
`utils.path_utils.place_without_clobbering(src, dst)` first uses the kernel's
atomic no-replace rename: `renameat2(RENAME_NOREPLACE)` on Linux,
`renamex_np(RENAME_EXCL)` (or `renameatx_np` with folder descriptors) on macOS,
called through `ctypes` with a feature check. It is one step, never replaces
anything (`FileExistsError` if `dst` exists), and creates nothing at `dst`
before it succeeds, so there's no placeholder to leave behind or for anyone to
swap out.

Where the kernel or filesystem has none, it falls back to a hard link, which is
atomic and no-replace too, then removes the `src` name, and a source the
caller's cleanup already swept doesn't count as a failure. That name is removed
only while it is still the inode just linked (`stat` through the same folder
descriptor, #431): a file put under the `src` name between the link and the
unlink is someone else's and stays. Where neither is available, it refuses
with `OSError(ENOTSUP)` rather than falling back to a check-then-rename.

That check still leaves a window before the unlink, so a move of a user's file,
whose name someone else may reuse at any moment, passes `identity_sensitive=True`
and, where there is no atomic no-replace rename (Linux CIFS, older NFS), takes
`_move_by_quarantine` instead (#431): (0) a fresh folder of its own beside `src`
is made first, so nothing is published if it can't be (`_private_folder`:
`mkdir(dir_fd=parent)` through the open parent, mode `stat.S_IRWXU`, a random
name tried again on `EEXIST`, then opened through the parent with `O_NOFOLLOW`;
CodeRabbit 4606147997, 4606148011); (1) `link(src, dst)`, which fails if `dst`
exists; (2) `rename(src, <private>/<name>)`, so no name in the shared folder is
involved and the quarantined entry is reachable only inside a folder only the
app's user can enter; (3) if the
quarantined entry is the inode linked, unlink it inside the private folder and
remove the folder, so a file put at `src` after step 2 is never touched. If it
isn't (`src` was replaced between steps 1 and 2), the file moved is the one that
was there at step 1, now held by `dst` alone, and stays there; the replacement
goes back to `src` by `link` (which fails if `src` was made again) and its
private folder is removed. If it can't go back, it stays in the private folder
and `MoveRollbackFailed` names it, which intake turns into a Finding. A
failure between steps 1 and 2 (the file still at `src`) unlinks `dst` again,
once it is checked to be the same inode, and raises: nothing moved. If that
unlink fails too, `dst` stays published beside `src` and it raises
`PlacementCommitted` with the first failure chained (CodeRabbit 4606441954),
never swallowing it. The same when that check can't stat either name for any
reason but `ENOENT` (CodeRabbit 4606738359): only "gone" says `dst` isn't
published, anything else means it may be. And when `src` was replaced (or
removed) after the link while `dst` is still the inode linked, `dst` holds the
original, its only name now: `PlacementCommitted` too, both files left as they
are (CodeRabbit 4607070950). The rollbacks never move a committed file back
over a name another file took meanwhile: the Image rename and
`_move_to_processed` check the old name first and, when it is taken, leave the
file under its new name and say so, naming both paths (the rename's
not-renamed reason; intake's `inbox_failed` Finding through
`MoveRollbackFailed`); a file whose old and new names are one inode (a move
that never let go of its old name) just loses the new name. A failure
after step 2 raises `PlacementCommitted` (an `OSError`): the file is at `dst`
and its old name gone, so a caller that rolls back counts it as moved (the
Image rename and `_move_to_processed` put it back with the rest).
`StagedOutput.take` / `restore` tell the two apart by what is left
(`staging._move_committed`, CodeRabbit 4606887339): the old name gone is a
move whose cleanup failed, answered as moved; the old name still the same
inode as the new one (both names: it never moved) is not a move, so the new
name is taken back after that inode check and the failure raised (raising too
if it can't be), and the RomM swap fails cleanly. Swap recovery drops a parked
name that is a second hard link of the Source the same way
(`swap_recovery._drop_second_link`) instead of refusing it. `move_anchored`
raises `PlacementCommitted` too when the move landed but the check of what
landed can't stat it (CodeRabbit outside-diff note). Each unlink removes a
second name of an inode another name still holds, so nothing is ever deleted. The identity-sensitive callers are `move_anchored` (the Image
rename and an Inbox's originals), intake's put-back, and `StagedOutput.take` /
`restore` (a RomM swap parking its Source, #391). The plain fallback stays for
the app's own files, whose names nobody else writes: a staging slot's placed
entries (`StagedOutput.place`, `_move_back`, `set_aside`), a temp file
(`archive_edit.rewrite_zip`, the Referrer writers in `referrers/base.py`) and a
tool's product (`OutputPlacement`). A
claim-then-rename can overwrite a file another writer swapped in for the claim,
so it is deliberately not used. The worker of an abandoned job removes its late
product only while `output_path` is still that inode (`remove_if_same_file`),
so cleanup never deletes a file another process has since put at that name.
POSIX has no atomic "unlink if this inode", so the instant between that check
and the unlink is the one window left.

This is the right rule for every conversion, not only xiso:
- An authorized overwrite already removed the old output before the conversion
  ran (`JobManager._clear_existing_output`).
- A non-overwrite job's destination was free when it was planned.

So anything at the destination at placement time isn't the job's to replace.
nsz's and JWUDTool's work-dir moves still use a plain `os.replace`.

Three rules follow for any code that runs a verifier:

- **Never offload a verify's blocking read to a shared pool** — including the
  event loop's *default* executor (what `aiofiles` uses), the route guards that
  run *before* the verify (their own bounded seam is `bounded_path_check`), and
  the searches a verify does before it reads anything: nsz resolves `prod.keys`
  through `run_detached` because with `SWITCH_KEYS` unset that walks the game
  volumes, inline and ahead of every bound meant to survive them. Nor wrap such a read in a context manager whose exit
  awaits a close: on a stuck read that close waits on the same lock, turning
  cleanup into a second unbounded wait, so an abandoned handle is left
  unclosed with its thread. Use `run_detached`, and pass it the `cancel_event`: cancellation abandons the
  thread either way, a pooled one is capacity the whole process shares, and
  without the event a Cancel pressed mid-read is not observed until the read
  finishes. Translate its `ReadCancelled` into the tool's own
  `{"cancelled": True}` terminal event.
- **Resolve the bound before the spawn.** It stats the file, and an `await`
  between the spawn and the `try/finally` is a window where a cancelled SSE
  request unwinds the coroutine with the child running, tracked, and nothing
  left to reap it.
- **Never record a verification result from a run that did not reach a
  verdict.** A verifier that outlived SIGKILL is one of those runs, and it is
  worse than a failure: it is still holding the storage. Every verify path
  builds that terminal event from one place (`abandoned_verify_error`) and gives
  it **precedence over the reason the stop was asked for** — a cancel or a
  timeout that ends with an unkillable child is an abandonment first, because
  the child is still running either way. The flag reaches callers through
  `collect_verify` (alongside `cancelled`), and the batch route stops the walk
  on it rather than opening the next file against the same mount and abandoning
  one more process per file. The captured verifiers get there through
  `run_capture`'s `on_abandoned` hook, since a `None` return code alone cannot
  tell an abort from a failed ladder. A verify with **no child at all** still
  leaves a trace when an outer deadline cancels it — the detached read it
  abandoned — so `run_detached` reports those too. Both report into the sink
  `collect_abandonment()` opens around *one file's* work rather than into global
  state: a context variable, because `asyncio.create_task` copies the context,
  so the producer task reports into its own caller's sink and no other. Global
  tallies cannot answer "did **this** verify abandon something" once
  `MAX_VERIFY_CONCURRENCY > 1` — one request's dead mount would stop another
  request's batch on healthy storage. Both kinds of wreckage mean the same
  thing to a caller walking a list: the storage just cost us something we cannot
  get back, so stop. An *outer* deadline (the route's or the
  job's) is the one case that cannot carry the flag on an event at all — it
  cancels the generator rather than letting it reach one — so `reap` records the
  pid it gave up on (`SubprocessRunner.abandoned_pids`) and the routes fold that
  into the timeout verdict. Issue #268 proposed replacing this with an exception;
  it was tried and rejected, because an exception cannot be raised from the
  `finally` an outer deadline unwinds through without masking what is already
  propagating — the very case the sink exists for. **The sink is the mechanism;
  there is no second one.**
- **Every loop that walks a list opens a sink (#268).** The verify routes were
  the first, but they are not special: the DAT-match paths and all three library
  scan phases walk a caller-supplied list of files on one storage in exactly the
  same way, and none of their return values can carry the fact — `disc_hashes`
  returns `list[str]`, `embedded_hashes` returns `list[tuple]`, and a match
  result has no field for it, so an unkillable `dolphin-tool verify` was
  indistinguishable from "this disc has no embedded hash". Because `reap()`
  reports into whatever sink is open and `run_capture` always reaps, the loop
  needs only the `with` block — no hook argument, and no change to the four
  layers between it and the child:

  ```python
  with collect_abandonment() as abandoned:
      result = await _match_single_file(path)
  if abandoned:
      ...  # stop walking; the next file is on the same mount
  ```

  Where the loop has a per-file `except` that swallows failures, the sink goes
  **outside** the handler, not inside: a corrupt file stays isolated to itself,
  while an unkillable child escapes. Scan Phase 2 is the sharpest case — its
  handler only logs at `debug`, so a stranded child there previously left no
  trace at all. A single-file endpoint has no walk to stop, so it reports instead
  (`/dat/match` answers 503).
- **A sink checked only at the loop is too late for a compound operation
  (#268).** The loop-level `with` is enough when the operation just succeeds or
  fails, but not when it *acts* on an intermediate result: two steps here decided
  something on a value that no longer meant what it said. Sizing the file for the
  verify bound is a read of the same storage, and when that probe is abandoned
  the resolver falls back to the flat baseline — so `disc_hashes` would spawn
  `dolphin-tool verify` into a mount already proven unresponsive, buying a full
  verify timeout and likely a second written-off resource. And an abandoned
  `chdman dumpmeta` reported "no GAME tag", which was indistinguishable from a
  genuinely untagged CHD — so `post_convert` answered it by firing `addmeta` at a
  file the abandoned reader still held, and the scan's disc-ID step fell
  through to a whole-disc sector read on the default executor with no bound of
  its own. (#352 later removed the GAME/NAME tag entirely; `dumpmeta` is now
  only read for the Dreamcast GDRO tag, and the same rule applies to it.)

  `abandonment_checkpoint()` is the seam for this: it yields a list for the
  immediate decision and then forwards whatever it caught to the enclosing sink,
  so the step aborts *and* the caller's walk still stops. A naive nested
  `collect_abandonment()` would shadow the outer sink and silently defeat the
  loop. Where the value itself is the trap, the deciding helper raises instead of
  returning it — `services.disc_id` raises `DiscIdStorageAbandoned` rather than
  the `None` that invites a write. This is the design rule *"re-check the cancel
  after resolving the bound, before spawning"* extended to abandonment, and the
  reward for ignoring it is larger: a child that blocks immediately and may
  outlive SIGKILL.
- **`post_convert` is best-effort about the disc ID, not about storage.** Its
  contract is that a disc-ID failure never fails the job, because a missing
  disc ID is cosmetic; it is logged as a warning naming the output, never
  silently. Storage that stopped answering is not that: every step after the
  hook reads that storage (sizing the output, verify, delete). So the hook
  raises `DiscIdStorageAbandoned` for it and `JobManager` lets that through to
  the handler that fails the job, rather than spending each later bound on
  storage known to be gone. Since #352 chdman's hook spawns no chdman child (it
  records the Source's disc ID in `chd_metadata_store`); it raises when its
  bounded read of the Source or its bounded stat of the output does not answer,
  and only warns for an ordinary read error (`OSError`, `ValueError`) or a
  database error (`SQLAlchemyError`, `RuntimeError`, `OSError`).
- **The finish path is bounded (#352).** After the hook, `_process_job` sizes
  the output (`_output_size_bounded`) and fingerprints it and the Source
  (`_build_produced_meta`) through `run_detached_within_stall_bound`, the
  conversion's no-progress bound. An output Volume that stops answering fails
  the job with an error naming the file instead of holding its queue slot.
- **Every disc-ID read is bounded (#352).** The disc ID is kept in
  `chd_metadata_store`, never in the CHD, and the reads that fill it are file
  reads on Volumes that may answer a cached `stat` and then block every read:
  - `disc_id.extract_from_chd` (the library scan's Phase 2 and `/api/info`)
    reads the CHD's header and any companion file through `bounded_disc_read`:
    `run_detached_bounded`, `_DISC_READ_TIMEOUT_SECONDS`, raced against the
    caller's `cancel_event` (Phase 2 passes the scan job's). Past the bound the
    thread is written off into the open `collect_abandonment` sink, a warning
    names the file and `DiscIdStorageAbandoned` is raised, so `/api/info`
    caches nothing and answers 503, and Phase 2 stops its pass at that file
    (the same rule as a chdman child that outlived SIGKILL). Phase 2 checks for
    Cancel before the sink, so a Cancel that cut a stuck read short ends the
    scan as cancelled, not failed. The scan's `_is_scanning` flag and
    `metadata_scan` lane are released either way.
  - The CHD's disc sectors are read by chdman, not decoded here (#362). The
    app parses only the v5 header, with MAME's layout (`parse_chd_header`:
    compressors at 16, logical bytes 32, map offset 40, meta offset 48, hunk
    bytes 56, unit bytes 60), for the unit size and the logical size. A
    compressed CHD's map is itself compressed and its hunks use lzma, zlib,
    huffman, flac or the CD codecs (cdlz, cdzl, cdfl), so the ISO 9660 walk
    (`_identify_iso`: volume descriptor, root directory, SYSTEM.CNF or
    PSP_GAME/PARAM.SFO) runs over a sector cache, and each run of sectors it
    asks for that is not there yet is read with `chdman extractraw -isb START
    -ib LENGTH` over the CHD's logical bytes (`_read_chd_units`), at most
    `_MAX_CHD_READS` reads of at most `_MAX_CHD_READ_UNITS` units. A DVD CHD's
    units are 2048-byte sectors; a CD CHD's are 2352 or 2448 bytes (with
    subcode), tried as Mode 2 Form 1 (data at 24) and then Mode 1 (data at 16).
    Each read goes through chdman's shared runner with
    `_DISC_READ_TIMEOUT_SECONDS` and the caller's `cancel_event`: a child that
    outlived SIGKILL or timed out raises `DiscIdStorageAbandoned`, a cancel
    raises `ReadCancelled`, and a chdman failure (a damaged CHD) is a warning
    and no disc ID.
  - **"Nothing found" and "could not read" are different outcomes (#490).**
    `extract_from_chd` returns None only when every strategy ran and found no
    disc ID; that is the outcome its callers cache by marking the CHD checked,
    and a checked CHD is not read again until its mtime changes. A read that
    could not run says nothing about the file, so it raises
    `DiscIdReadFailed` instead: chdman could not be started, its nice/ionice
    wrapper exited 126 or 127 (chdman not executable or not found), a signal
    killed it (a crash), the GDRO `dumpmeta` timed out, or the CHD's header or
    chdman's output could not be read (`OSError`). Every chdman read goes
    through one helper, `_capture_chdman`, which also raises
    `DiscIdStorageAbandoned` for a child that outlived SIGKILL. chdman's own
    error exit (1: no such tag, a damaged CHD) stays "nothing found". Both
    callers treat any exception the same way: `/api/info` logs a warning,
    caches nothing and still answers 200 with the rest of the CHD info, and
    Phase 2 logs a warning and moves on without marking the file, so the next
    request or scan retries it. (`DiscIdStorageAbandoned` on `/api/info` is
    still the 503 above.)
  - `ChdmanTool.post_convert` reads the Source's disc ID (and checks the output
    exists) in one call through the same `bounded_disc_read`; past the bound,
    `DiscIdStorageAbandoned` names the Source and the job fails (above).
  - `chd_metadata_store.is_disc_id_checked` / `mark_disc_id_checked` resolve
    and stat the file through `bounded_path_check`. A stat that does not answer
    is "not checked" for the scan (its own bounded read then decides) and is
    not marked; after a conversion, `post_convert` turns it into
    `DiscIdStorageAbandoned` naming the output.
- **One volume's failure stops the whole pass, deliberately.** A scan or batch is
  one user-initiated walk, and the loops do not resolve which configured volume
  each path belongs to, so the first abandonment ends all of it — including
  paths on volumes that are answering fine. That is the safe direction: stopping
  early costs a re-run, while carrying on costs one stranded process per
  remaining file. Note this does **not** transfer to the job queue, which is why
  the dispatcher deliberately keeps going (#265): queue entries are independent
  jobs that may target unrelated volumes, whereas one scan is a single pass the
  user asked for as a unit.
- **One-shot chdman/dolphin captures go through `run_capture`.** `chdman info`,
  `dolphin-tool header`, and the `chdman dumpmeta` helper in
  `services.disc_id` used to spawn directly, untracked, and finish
  with an unbounded wait after `kill()` — `dumpmeta` could hang the scan's Phase
  2 outright. They now share the capture path, so they are PID-tracked, bounded,
  subject to the tool priority policy, and able to report abandonment.
  `services.disc_id` uses `chdman_service.runner` rather than a runner of its
  own, so `active_pids()` still describes every chdman child.
- **Stop a list at the first unresponsive path, don't survey them all.** The
  batch route's path validation bounds each check, but bounding is not enough on
  its own: a batch is a list of paths on *one* storage, so carrying on after a
  timeout costs another probe bound and another written-off thread per path —
  minutes before any verify starts, and enough abandoned threads to exhaust the
  process-wide ceiling, which then fails path checks on volumes that are
  answering. The first timeout ends validation with a 503.
- **Re-check the cancel after resolving the bound, before spawning.** The
  resolution is itself a probe that can take its full bound on storage that has
  stopped answering, and spawning into that means a child that blocks
  immediately and may outlive SIGKILL — a prompt cancellation turned into an
  abandoned process, for work nobody wanted.
- **Every bound resolution takes the `cancel_event` too.** Sizing the file is a
  stat of the same storage the verify is about to read, and it happens *before*
  the verify that would observe a cancel — at the call site
  (`tool.verify_timeout(path, cancel_event=…)`) and again inside each verifier
  (`run_verify`, `capture_verify`, z3ds). Miss one and a Cancel waits out the
  probe bound with the dispatcher slot still held.
- **Keep every wait under the same live checks.** A wait decided once, up front,
  answers only for the instant it was decided. `run_verify`'s post-EOF grace —
  waiting for a verifier that closed stdout to exit on its own — is therefore
  sliced, and each slice re-asks what the read loop asked: has the cancel fired,
  has either deadline passed. Capping that grace by the *remaining* overall
  bound is not the same thing: it sees neither a cancel pressed a second later
  nor the stall bound, which is the only bound a stall-only configuration has.

With verify genuinely bounded, the stalled-job warning no longer *skips* jobs in
the verify phase (it did, to avoid calling a long checksum stalled — at the cost
of logging nothing for a job wedged **in** verify). It now reports them in their
own words, timed from when the verify phase began rather than from the progress
clock, which stopped when the conversion ended.

**Adding a tool:** do nothing and it already reports bytes + rate. Add a
`SIZE_RATIOS` row to also get a percentage bar. Implement `parse_progress` if
the tool prints a percentage that survives being piped — and verify that it
actually does, because several do not.
dolphin's heartbeat and the makeps3iso split-stall probe are likewise opt-in
flags. One-shot subprocess work (info / header / embedded-hash extraction)
shares `run_capture()` rather than re-implementing the spawn / cancel / timeout /
terminate dance per tool.

Directory-input tools that hand a source tree to a native recursive reader must
validate both at planning time and again in `JobManager._process_job` after the
source subtree lock is acquired, immediately before invoking the tool. The PS3
`folder_to_iso` path uses `is_safe_directory_tree` for both checks so a queued
job cannot be made unsafe by adding a symlink, special file, or volume-escaping
entry after planning but before `makeps3iso` starts. The worker runs the
re-check *before* `_clear_existing_output`, so a safety rejection on an
overwrite job is non-destructive — the prior output is only cleared once the
source is confirmed still safe. Because that walk can be slow on a large tree,
the worker also re-checks the cancel event immediately after it (before
clearing outputs), so a job cancelled mid-walk keeps its prior output. To keep
the planning-time walk off the hot rejection paths, `_plan_directory_job` runs
it only after the output is derived and skip/locked collisions short-circuit,
and the create/batch routes apply queue-depth backpressure (HTTP 429) *before*
planning when the queue is already full — so a doomed submit never pays for the
tree walk.

### Placing a product from a work folder (`services/output_placement.py`, #422)

A service whose tool writes into a private work folder (extract-xiso, and
rom-converto through `files_in_folder`) ends with one move: the product onto the
output path. That move runs after the child's stall watchdog has stopped, can
block on a stuck mount, and can complete long after the job gave up. Both
services do it through one helper, `OutputPlacement(output_path, label=...)`:

- `place(produced)` (blocking; run it through `run_detached_within_stall_bound`)
  records the product's identity (device and inode), then
  `place_without_clobbering`: a file already at the output path is not this
  job's, so it is refused, never replaced. Errors name the format from `label`
  ("... appeared while the XISO was being written; not overwriting it",
  "Could not place the ZAR output: ...").
- `abandon()` marks the job as having given up and returns the product's
  identity only if the move had completed. The two are synchronized under a
  lock held for a couple of assignments, never across I/O.
- `abandon_and_sweep()` is what the service's failure and cancel path awaits:
  a completed move's product is removed (bounded, through
  `remove_partial_output`) only while the path is still that file. A move not
  yet complete leaves the output path untouched, and when it lands later,
  `place` sees the job gave up and removes its own product, again only while it
  is still the same file.

So a cancel or a stall never deletes a file this job didn't put there, and a
late move never leaves one behind. `tests/test_output_placement.py` pins the
helper, including the late-landing cancellation path; the xiso suite's
placement tests pin extract-xiso on it unchanged.

### Cleaning up partial output is bounded too (issue #267)

Every tool that writes its output in place sweeps the partial away when a run
ends abnormally — a cancel, a stall, a non-zero exit, a task cancellation or
generator close. That sweep is **not** a per-tool block: it goes through
`remove_partial_output(*paths)` (files) or `remove_partial_tree(path)` (a
private work dir), both module-level in `services/subprocess_runner.py` and
re-exported from `services/tools/runner.py`.

Why they exist at all: the sweep runs *after* the runner has given up, on the
same storage the run just failed on. `stat`/`unlink`/`rmtree` on an
unresponsive mount block in uninterruptible I/O and cannot be cancelled, so a
hand-rolled `os.remove` there defeated the bounded reap ladder — the child was
abandoned promptly (#263/#265) and then the job hung on cleanup instead, and at
`MAX_CONCURRENT_JOBS=1` (the default, jobs inline in the dispatcher) the queue
froze anyway. These helpers run the blocking call on a throwaway daemon thread
(the `_bounded_probe` shape) with a hard `_CLEANUP_TIMEOUT`.

The contract, which is what makes them safe to use everywhere:

- **They never raise, except to cancel.** A cleanup problem must not replace the
  exception that explains the job's outcome on an error path, and must not fail
  an already-published conversion from the `finally` blocks that also run on
  success. It is a logged warning and a `False` return — the job finishes with a
  leftover partial file, a much smaller problem than a frozen queue. That
  includes being unable to start the cleanup thread at all, which is plausible
  precisely here since a run of dead-mount sweeps deliberately writes threads
  off. `CancelledError` is the one exception that propagates, like from any
  other `await`: swallowing it would defeat cancellation outright for the
  `finally` callers, which have no pending exception to re-raise and would go on
  to be marked complete.
- **The removal is dispatched before the wait**, so a cancellation arriving
  afterwards cannot take back work the sweep already did — the useful half of
  what the old blocks bought by unlinking *synchronously* on the event loop
  (awaiting during a cancellation could be re-cancelled and skip the cleanup),
  without the unbounded block.
- **Giving up stops the rest of the sweep.** Once the helper returns, the job
  finalises and stops owning those paths, so an abandoned thread must not keep
  deleting: the mount can recover minutes later, by which point a retry may own
  the names, and the thread would delete the *new* job's good output. The
  worker checks an `abandoned` flag before each unlink. The one unlink already
  in flight when the bound expired can't be recalled — an in-flight syscall is
  not cancellable — so that single path stays at risk and every path after it
  does not. On responsive storage the sweep finishes long before any of this,
  so the normal case pays nothing.
- **An absent path is success**, so no caller needs a separate `os.path.exists`
  round trip on the mount in question.
- **One bad path does not stop the sweep**: the rest are still removed, and
  everything that failed is named in one warning.

Rules for a new tool:

- Catch the abnormal exits *after* the spawn only. A setup or pre-spawn failure
  wrote nothing, so deleting a pre-existing output there would destroy a good
  file.
- Pass **every** path the run could have written. When the set is only knowable
  by probing the disk, pass `discover=` instead of enumerating first — it runs
  *inside* the bounded worker. makeps3iso's split parts are the case: those
  `isfile` probes hit the same volume the sweep is about to unlink from, so
  enumerating on the event loop would reintroduce the exact unbounded block
  this seam removes. It passes `output_artifacts()` as the discovery callable,
  keeping the one enumeration that also backs `overwrite_targets`.
- Where the result is load-bearing rather than best-effort, check it. romz
  clears a stale archive *before* running `7z a` (which appends) and refuses to
  start if that sweep reports failure. Note what the propagating `CancelledError`
  buys that call: a job cancelled mid-sweep raises out of the helper instead of
  reaching the "could not clear the existing archive" error, so it still reports
  as cancelled rather than as that failure.

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
the actual escape vectors (absolute path, any `..` component) with the shared
`services.archive.reject_member_traversal(name)`, which raises
`ArchiveSafetyRefused`. romz (`7z x`) and the rar tool (`unar`, #476) both call
it on every member. Such tools must also forbid symlink members (zip
`external_attr` `S_ISLNK` / py7zr `FileInfo.is_symlink`), since `7z x` restores
symlinks as links; the rar tool refuses any member that is not a plain file
(`RarSetMember.is_plain_file`: a RAR5 hard link or file copy reads as a file to
rarfile, so the redirect is checked too, and a Unix-mode device or FIFO).

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

**RAR sets (#476).** A multi-volume RAR is one archive in several files.
`archive_members` owns the set, in one place. A name is only ever a
candidate; the set's own headers decide what belongs to it:

- `is_rar_volume_name(name)`: `.rar` (`.partN.rar` included) or an old-style
  `.r00` … `.s99`. Only `r` and `s`: a wider range reads ROMs as volumes
  (`.t64`, `.v64`, `.z64`), and a test holds every registered input extension
  but `.rar` out of it.
- `later_volume_of(path)` names the first volume of a later one, for both
  namings, confirmed by rarfile's `NeedFirstVolume`.
- `rar_later_volumes(first)` lists the files *named* like the later volumes,
  by name plus one stat each, stopping at the first gap. Names only: a stray
  `x.part4.rar` or an unrelated `x.r00` is named like a volume too.
- `rar_set_volumes(first)` is the answer to "is this file part of that set":
  the volumes the headers read (rarfile follows each volume's "more volumes"
  flag). The read runs through `subprocess_runner.run_detached_blocking`
  (the synchronous form of `run_detached_bounded`, under the same ceiling
  every detached probe shares) bounded by `RAR_HEADER_READ_SECONDS`, the bound
  a route's Volume work gets. Both a list and an unreadable answer are cached
  against the stat of *every file named like the set* (the first volume and
  each `.partN.rar` / `.rNN` after it), so a set read while a volume was
  still downloading is read again as soon as it arrives, and a broken set is
  not reopened on every call. A read that ran out of time is not kept. It
  raises `RarVolumesUnknown` when the headers can't be read whole, in time,
  or with no probe slot free.

The rar tool's `source_companions` is `rar_set_volumes(first)[1:]` for every
`.rar` that is not a later volume. Names may suggest a set but never prove
there is none: a set whose `part2` / `.r00` is missing names no later volume,
yet its first volume's header says more follow, so the headers are always
read (once per version of the set's files). An incomplete set is unknown, not
empty. So is any partial read (`archive_members.incomplete_reason`): rarfile
stops without an error at headers it can't decrypt, reporting a password is
needed, no members and only the volumes it got through, and a read that lists
no members proves nothing either. Such a read is never cached as a complete
list; `rar_extract` refuses it ("the set's headers are encrypted"), and the
listing read (`read_archive_members`) raises `IncompleteArchive` rather than
showing an encrypted-header archive as empty. When the volumes are unknown it raises `tools.base.CompanionsUnknown`,
never `[]`: `registry.source_companions(path)` (a listing, the Library walk,
in-use tracking) then adds none, while `registry.source_companions(path,
strict=True)` lets it propagate, so anything that deletes refuses
(`build_delete_plan` reports it as an error), `source_companions_are_safe`
answers unsafe, and the output guard below treats the output as part of the
Source. The delete-plan route builds its plans through `off_loop_bounded`. Because `registry.source_companions` is the union over every tool, this
is what every delete of a `.rar` uses (the generic delete plan, the duplicate
cleanup, an archive-member job's delete-source), so a file the set never reads
is never planned for deletion. `rar_extract` also refuses a set whose named
and read volumes differ in either direction, naming the extra or missing
files. `read_rar_set(first)` is the uncached read an extractor needs and the
listing cache does not keep: each member's CRC, `is_plain_file`, whether a
password is needed (for the headers or any member) and the volumes rarfile
read.

**Companion rows in the listing (#504; one row per game, restructure 2/6).**
`GET /api/files` marks a row that is one of `image_sets.image_members` of
another row in the same listing (a `.cue`/`.gdi`'s tracks, a RAR set's later
volumes, a split Wii U dump's parts, any tool's `source_companions`) with
`FileEntry.companion_of`, the leading row's path, and a row a listed
`.m3u`/`.m3u8` names with `FileEntry.disc_of`, the playlist's path.
`routes.files.companion_marks(paths, progress)` asks `image_members` of each
listed file and archive row in turn, skipping a row already marked, in the
lenient form: a set whose members can't be told now (an unreadable sheet,
`CompanionsUnknown`, an unreadable or slow header read, each bounded and
detached as below) marks nothing. `disc_marks` reads each playlist with
`read_beneath` (no symlink, at most `PLAYLIST_READ_BYTES`, 64 KB), parses it
with `referrers.m3u.entries` and resolves each entry against the playlist's
folder; a playlist it can't read marks nothing. Both run in one pass,
`game_row_marks`. The pass runs after the listing, through
`run_detached_within_stall_bound` under the listing's own `WALK_STALL_SECONDS`
with one progress report per row; a pass that stops moving gives up and the
listing answers with no marks rather than 503. A marked row lists as before
(a later volume is still a plain `file` row). The SPA folds it under its head
(`fileBrowser.rows`, before sort and paging, so both count games): the head's
size is its own plus its parts', and its chevron shows them. `_isSelectable`
refuses a marked row, so it is never ticked, converted, checked or deleted
alone. Ticking a playlist ticks its discs (`selectedFiles`) and keeps the
`.m3u` in `selectedPlaylists`, which only Delete reads; Delete takes every
file of each game. Folder rows are never asked.

**A file another game still names is never deleted with this one (#549
review).** A track two sheets name, or a disc two playlists list, folds under
neither row. Every delete then asks one question, `image_sets.owners_naming(path,
stop_at=...)`: every file that names *path* as part of itself, whether a
`.cue`/`.gdi` (its track, from its folder up to the Volume boundary), a
`.m3u`/`.m3u8` (its disc, the same walk) or a file beside it whose tools read it
(`registry.source_companions` / `split_members`: a RAR set's later volume, a
split dump's part). It fails closed, raising `SheetOwnerUnknown` when a folder,
a sheet, a playlist or a set can't be read. `/files/delete-batch` keeps a file
with an owner outside the request (`still_owned`). `build_delete_plan` blocks a
delete-on-verify whose track or companion another file names (`_other_owner`).
The single-file `/files/delete` and `/files/rename` ask the same question after
containment (`_assert_not_named_elsewhere`): a file something names, or one part
of a set, answers 409 and stays as it is (renaming a sheet is allowed: its
tracks keep their names). A game row's Delete goes through the batch, whole.
The next companion type only has to answer through the registry hook to be
covered by all of them.

**Every RAR header read is bounded and detached.** `archive_members` reads a
RAR header in one way only, `_bounded_header_read`: the listing
(`read_archive_members`), a later volume's flag (`later_volume_of`), a set's
members (`read_rar_set`, which `rar_extract`'s planning, `converts_path`,
`convert_refusal`, info and the Round-trip digest use) and its volumes
(`rar_set_volumes`). It runs through `subprocess_runner.run_detached_blocking`
under the shared probe ceiling and gives up after `RAR_HEADER_READ_SECONDS`,
raising `RarVolumesUnknown`. `plan_job` itself runs its blocking steps
(`converts_path`, `convert_refusal`, the output path, the companion and output
checks, the delete snapshot) through `off_loop_bounded`, never the shared
threadpool, so a mount that hangs a header read answers 503 or a skip with the
reason, and holds no pool worker. (`ArchiveService`'s extraction of a member
from a `.rar` for another tool's job reads content, not headers, inside that
job's own bounds.)

**The output is never part of the Source** (every tool, every job creator).
`output_conflicts.output_is_part_of_source(output, source)`: the output may
not be the Source (the archive, for a member) nor any of
`registry.source_companions(Source, strict=True)` (unknown counts as yes).
Two callers, one rule. `plan_job` asks it of the *final* output, after the
duplicate handling, so a skip still says the output exists, a rename's free
name (`Game_1.rvz` beside `Game.rvz`, Dolphin's same-format re-encode) still
queues, and an overwrite onto a part answers `OUTPUT_IS_PART_OF_SOURCE`.
`output_conflicts.resolve_destination`, which Rules sweeps, Inboxes and the
RomM re-pin planner decide each Source's destination with, answers
`SKIP_ONTO_SOURCE` for such a Source whatever the policy (ahead of the
existing-output answer, which would hide it): a sweep counts it as skipped and
names it in the Platform's `onto_source`, an Inbox holds it with
`ONTO_SOURCE_REASON`, and the rest of the batch still queues.
`JobManager._reservation_keys` asks it once more, off the loop, for every spec
of every batch, and the locked reservation refuses the batch with
`OutputIsSourceError` before any job is created: a backstop for an answer that
changed between planning and queueing (the API routes answer 409, a sweep
records `output_onto_source`). A
RAR set's file named like its own later volume, or a split dump's part, would
otherwise wait on its own input lock forever under overwrite, or have a live
part of the Source cleared.

**One rule for logging a failed archive read (#506).**
`services.archive.log_archive_read_failure(logger, path, exc)` decides the level
for every path that opens an archive and logs on failure (the listing in
`list_archive_contents`, the `/archive-summary` batch). A zipfile, py7zr or
rarfile format error, `IncompleteArchive`, `ArchiveSafetyRefused` or an
`OSError` is the user's broken file: one WARNING naming the file and the reason,
no traceback. Any other exception is our bug and stays an ERROR with the
traceback. It only changes the log; what the API returns is unchanged.

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

#### A step can flag a caveat the chain must not bury (`warning`)

Each step's updates overwrite the job's `message`, so a caveat an *earlier* step
raised about its own output is gone by the time the chain finishes — and the
terminal `{"progress": 100, "message": "Conversion complete"}` says nothing.
That matters when the caveat changes what the output *is*: nkit2iso restoring a
Wii image whose update partition was removed produces a playable but **not**
bit-exact ISO, and `nkit_to_rvz` would otherwise report an unqualified success.

A step opts in by marking that update with `warning: True`; `ChainTool` collects
them and appends them to its own terminal message (itself marked `warning`).
`job_manager` reads only `progress` and `message`, so the extra key is inert for
a direct job and needs no contract change.

Note what this does **not** buy: the flag arrives mid-run, long after
`supports_delete_on_verify` was read at plan time. It informs the operator; it
cannot gate the pipeline. That is why `nkit_to_rvz` declines delete-on-verify
outright rather than conditionally — see the flag's comment in `chain.py`.

#### `expected_output_size`: preflight sizing without a tool-specific import

`ChainTool._preflight_headroom` has to bound three things at once (source, full
intermediate, partial final) before it starts, and `ChainStep.output_ratio` is
too blunt for it: a heavily compressed `.cso` or a scrubbed `.nkit.iso` can be
an order of magnitude smaller than the `.iso` its step will write, so a ratio on
the input size under-counts badly and the preflight passes on a volume that then
fills up mid-job.

It used to solve that by importing `services.maxcso.uncompressed_iso_size`
directly — a single-tool special case sitting on code every chain runs through,
the same shape as the `overwrite_targets` / `is_ready` branches removed earlier.
A second chain whose first step was a different tool would silently get the
ratio fallback with no way to do better.

The contract now carries it:

```python
def expected_output_size(self, input_path: str, mode: str) -> int | None:
    """Bytes this mode will write, if cheaply knowable from a header."""
```

`BaseTool` returns `None` (fall back to `output_ratio`). `MaxcsoTool` returns
`uncompressed_iso_size` for `cso_decompress`; `Nkit2IsoTool` returns the image
size the NKit header states. `ChainTool` asks `steps[0]`'s tool and never names
a tool itself. It is **preflight only** — never a correctness input — so an
unreadable file is `None`, not an error. Blocking (a small header read), so call
it off the event loop.

#### A chain's output name comes from its FIRST step

`ChainTool.output_path` used to delegate to the *last* step's tool, which is
correct only while every chain source has a plain single suffix. The first step
is the one that owns the input, so only its tool knows how to strip the source
name: `nkit_restore` must drop a whole compound `.nkit.iso`, where dolphin (the
final step of `nkit_to_rvz`) would leave `Game.nkit.rvz`. The rule is now "the
first step's own output path, with the chain's declared `output_ext` swapped
in", which is identical to the old behaviour for `cso_to_chd` (`Game.cso` ->
`Game.iso` -> `Game.chd`). `detect_output` and the scratch intermediate's name
follow the same rule.

### 3.3.4 Directory inputs (`InputKind`, `accepts_directory`)

Every input seam keys off `Path(filename).suffix`; a folder has none. For
directory-unit conversions (issue #98 Phase 2, makeps3iso folder→iso) the seam
is an `InputKind` enum (defined in `models.py` so `ConversionJob` can type its
field without an import cycle; re-exported from `services.tools.spec`) +
`ModeSpec.input_kinds` (default `{FILE}`, zero behavior change), a
`ToolPlugin.accepts_directory(path)` predicate (default `False`; the directory
analogue of `ext in input_extensions`), and `registry.tools_for_directory(path)`.
The per-source-type detector for PS3 lives in `services/ps3.py` (requires the
`PS3_GAME/` disc/JB layout, rejecting bare installed-game folders).

The first user, **`MakePs3IsoTool`** (`folder_to_iso`, the only
`InputKind.DIRECTORY` mode), is wired end-to-end:

- **Tool/service.** `MakePs3IsoTool.accepts_directory` runs `ps3.is_ps3_iso_source`;
  `makeps3iso_service` shells out via `SubprocessRunner.run(["makeps3iso", folder,
  out.iso], …)`, derives the output from the folder's **normalized basename**
  (`<folder>.iso`, never `.iso` from a trailing slash), removes a partial ISO on
  cancel/failure, and runs a light **PARAM.SFO `TITLE_ID` readback** from the
  built ISO (reusing the shared `disc_id.read_iso_file` ISO 9660 reader) as an
  advisory check — `supports_delete_on_verify=False`, so a curated source folder
  is never auto-deleted. Before queuing the native packer, `plan_job` also walks
  the whole source tree with `utils.path_utils.is_safe_directory_tree`: the root
  is `lstat`ed after stripping only trailing separators and `.` components
  (interior `..`/symlinked ancestors are left intact, so the kernel resolves
  them and `lstat` reports the true final entry — a symlinked root cannot hide
  behind a trailing `/`, `/.`, `/./`, or a `..`-cancelled symlinked ancestor)
  and confined to a configured volume, then every
  entry is `lstat`ed and any symlink or non-regular entry is rejected. Because
  `os.walk(followlinks=False)` never descends through a link, the surviving
  entries are all genuine children of the confined root — no per-entry resolve
  is needed — so makeps3iso cannot dereference a link and embed files outside
  the volume boundary. The directory job is also **canonicalized to the resolved
  real source path** (`os.path.realpath`) before being queued, so makeps3iso is
  handed a symlink-free path: a concurrent swap of a symlinked *ancestor*
  component after validation cannot retarget the native reader to an unchecked
  tree. (A symlinked *root* is still rejected outright, since the safety walk
  runs on the original submitted path.)
- **Job model.** `ConversionJob.input_kind: InputKind` is threaded end-to-end
  (derived from the mode spec at queue time, serialized to a string only at the
  API/persistence edge). The generic `_process_job` flow already handles a
  directory source — the archive-extract branch is gated on `"::"`, which a
  folder never carries — so no directory-specific convert branch is needed.
- **Listing + search.** `files.py` annotates convertible directory rows
  (`detect_directory_outputs` → `registry.tools_for_directory`) with
  `convertible_by` + the sibling `.iso` output in **both** `scan_directory` and
  the recursive search `_scan` (the latter emits the folder as a job unit and
  does not recurse past it, so it's discoverable / batch-selectable).
- **Lock manager.** A directory job protects its whole subtree two ways:
  - *Filesystem mutation* (rename / delete / delete-on-verify cleanup): the
    files-route `_assert_path_not_in_use` (→ `find_active_job_for_path`) rejects
    a rename or delete of any path that is a **descendant** of an active
    `InputKind.DIRECTORY` job's source, and delete-on-verify cleanup applies the
    same descendant check via `_is_path_in_use_by_other_job`. (New-job *output*
    collisions are handled separately by `check_output_conflicts` in `plan_job`,
    not these helpers.)
  - *Runtime* (concurrent conversions): the lock manager exposes directory-lock
    APIs — `acquire_dir_lock` / `release_dir_lock` plus the read-only
    `is_within_locked_dir` / `dir_lock_would_conflict`. `acquire_dir_lock` takes
    an `fcntl` lock on the **directory path itself** (so the exact same path is
    cross-process exclusive) and records the resolved path in an in-memory
    `_dir_locks` set; **subtree containment is enforced in-process** — a per-file
    `acquire_lock` checks `_dir_locks` and contends on a child output, and the
    code notes this is sound because `MAX_CONCURRENT_JOBS` concurrency all lives
    in one process (it is *not* a cross-process subtree lock — a child output in
    a second process hashes to a different lock file). A directory job holds the
    lock for its whole run. When `MAX_CONCURRENT_JOBS > 1`, a job whose
    input/output falls inside a locked subtree is **deferred, not failed**:
    `_process_job` releases the concurrency slot (and any output lock taken) and
    `_schedule_dir_lock_requeue` re-queues it after a fixed short delay, so it
    runs once the folder job releases the lock — a `Set[asyncio.Task]` holds
    strong refs so a requeue task can't be GC'd mid-sleep. (A *new* submission
    whose output is already inside the locked subtree never reaches this path: it
    is rejected at plan time by `check_output_conflicts`, which reads the dir lock
    through `check_file_status`.) All containment comparisons resolve
    symlinks via `os.path.realpath`, so a symlinked path into a locked folder
    can't bypass them; resolution is gated on a dir lock actually being held to
    keep the hot listing path cheap. `_plan_directory_job` additionally rejects a
    derived output that resolves inside the source or outside the configured
    volumes, so a folder-at-volume-root build can't write a sibling `.iso`
    outside all volumes or ingest its own in-progress ISO.
- **Plan + UI.** `plan_job` branches on `InputKind.DIRECTORY in spec.input_kinds`
  (validate `isdir` + the detector, normalized basename for output + display
  name). The frontend mode carries `inputKinds: ['directory']`; a *convertible*
  directory row becomes checkbox-selectable (`fileBrowser._isSelectable` /
  `conversion.allowsInputEntry`) while the name-click still navigates — a plain
  directory stays navigation-only.

- **4 GB FAT32 split (`-s`).** An opt-in per-job toggle exposes makeps3iso's
  split mode for FAT32 targets. `split` rides the shared `ToolPlugin.convert`
  contract beside `compression` (file tools ignore it) and threads
  request → `create_job`/`create_jobs_atomic` → `ConversionJob.split` →
  `convert(split=…)`; the frontend gates the checkbox on a `supportsSplit` mode
  flag. The split is **size-dependent and its part names are unknown until the
  build finishes**: makeps3iso only splits past ~4 GB (`0x1FFFE0` sectors),
  renaming the first part to `<output>.iso.0` and writing `.1`/`.2`/…, but emits
  a single `<output>.iso` below the threshold. So the service never assumes a
  shape — `split_parts()` probes the base then `.0`/`.1`/… post-build; the
  `TITLE_ID` readback reads the first part; failure cleanup unlinks the base and
  every numbered part. `_plan_directory_job` counts an existing split set as an
  output collision (RENAME steps past it via `get_unique_output_path`),
  and `files.py` folds a set into one logical listing entry
  (`_fold_split_iso_entries`: name `<base>.iso`, `path` = the `.0` part,
  summed size, `FileEntry.split_parts` = count, no convertible/verify
  affordances — it's a final deliverable, not a source). The recursive *search*
  path only emits convertible sources, so split parts never surface there.
  Lifecycle completeness across the multi-file model — every site below
  enumerates the extra files from the `companion_outputs` hook (§ the plugin
  contract) rather than re-encoding the per-mode suffix, the same single source
  extractcd's `.cue` + `.bin` flows through:
  - **Conflicts:** `check_output_conflicts` enumerates the set via the owning
    tool's `companion_outputs` (no per-mode `mode ==` branch), so the
    `/jobs/check-duplicates` preflight and the plan agree a set occupies the
    target; `get_unique_output_path(base, mode)` reuses it to step a rename past
    the whole set.
  - **Overwrite:** `JobManager._clear_existing_output` removes the prior output
    (single `.iso` **or** the whole split set) **only when `allow_overwrite` was
    granted** — so a set that appears after planning is never deleted out from
    under a skip/rename decision (the per-path `acquire_lock` only sees the bare
    name). The sweep is **tool-neutral**: it asks the owning plugin for
    `overwrite_targets()` and applies the same validate-then-unlink pass to
    every mode, file or directory. `MakePs3IsoTool` overrides that hook to
    return the base plus every numbered part (via the service's
    `output_artifacts`, which also backs its failure cleanup so the two can't
    drift) — wider than `companion_outputs`, which reports only the finished
    set. This used to be an `input_kind == DIRECTORY` branch calling
    makeps3iso's own part-removal helper directly, which meant a *second*
    directory-input tool would have had its outputs swept by makeps3iso's
    part logic.
  - **Completed size:** the completion block sums `output_path` plus
    `companion_outputs()`, so a split build (whose numbered parts replace the
    bare `.iso`) and an extractcd `.cue` + `.bin` both report a real
    `output_size` instead of `None`.
  - **Stall detection:** widened via `SubprocessRunner.run(output_growth_paths=…)`
    to the summed size of the set, so a split build isn't killed as stalled once
    the base is renamed away.
  - **In-flight protection:** `_split_output_blocks` marks a running split job's
    numbered parts in-use in `find_active_job_for_path` /
    `_is_path_in_use_by_other_job`, so a part can't be renamed/deleted mid-write.
  - **Row actions:** `RowActionsMenu` disables single-path rename/delete and
    `FileList.openBulkDelete` excludes a folded set (its `path` is only the `.0`
    part, so a single-path op would orphan `.1+`).

makeps3iso (GPL-3.0) is built unmodified from a pinned `bucanero/ps3iso-utils`
commit in the multi-stage `Dockerfile`, mirroring maxcso.

### 3.3.5 Metadata freshness (`chd_metadata_store.get_fresh_metadata`)

A consumer that reads cached CHD metadata to act on it — `ChdmanTool.embedded_hashes`
matches a CHD's stored header/data SHA1 against a DAT without re-hashing the file —
needs the hashes and the "is this still the current file?" verdict to describe **one**
observation. `get_metadata()` followed by `is_stale()` does not: two separate awaits,
each its own stat + row read, so the hashes can reflect one mtime while the staleness
verdict reflects another (a conversion rewriting the CHD in between). That is the single
seam every freshness-gated metadata read MUST go through instead.

`get_fresh_metadata(path)` returns the cached `metadata_json` **only if it still
describes the file**, in one `run_in_threadpool` call with no `await` between the row
read and the stat. The ordering matters: it reads the **row first, then re-stats**, and
returns the row only when the file's current mtime equals the row's captured mtime. The
writer (`_set_metadata_sync`) always stats then stores `(metadata_json, mtime)` together,
so the row's mtime lags the file; reading the row first and comparing against a *later*
stat means a concurrent rewrite (new bytes on disk, row not yet re-scanned) shows
`mtime != row.mtime` and the stale hashes are refused. A stat-**then**-read ordering would
re-use an already-captured old mtime that can still match the old row for a file that was
replaced in the window — the TOCTOU this seam exists to close. On any miss (no row, file
gone, or changed) it returns `None`, and the caller falls back to a file-level SHA1, which
is valid for any DAT that indexes the container bytes. New cache/tool code that needs
freshness-gated metadata must call this rather than reintroducing the two-read pattern.

### 3.3.5.1 Remote hash fallback (`services/hasheous/`)

Hash matching has two sources, and the ordering between them lives in
`routes.dat._lookup_match`, which runs `_local_lookup_match` and only then
`_remote_lookup_match`. Every match path — `POST /dat/match`,
`/dat/match-batch`, the background match job, and `info._scan_phase_dat_match`
— reaches the sources through those helpers, so the fallback was added there
rather than by introducing a provider registry. Two sources do not need an
interface; a third would.

One caller does not go through `_lookup_match`: `_match_single_file` calls
`_local_lookup_match` and `_remote_lookup_match` directly, because it has to
check *every* local candidate a file offers (embedded hashes, then the
file-level SHA1) before any of them may go out remotely. `_match_result`
builds the record both paths return.

The order is **fixed and local-first**: `_local_dat_record` (the imported DATs)
and only on a miss, and only when the operator opted in, `hasheous.lookup`. That
keeps a covered library fully offline and makes a given hash resolve the same way
regardless of network weather. Both helpers return the **same record shape** —
`dat_id` / `dat_name` / `game_name` / `rom_name` / `source`, plus whatever extra
identity fields the source carries — so `_match_result` builds the
result dict once and splats the record into it. Adding a field to a remote match
means adding a key to that record, not touching the builder.

#### Candidate ordering: every local lookup before any remote one

This rule has to hold across the *whole* of `_match_single_file`, not just
within one helper, and it took three passes to get right — so the shape is
deliberate.

The lookup is split into `_local_lookup_match` and `_remote_lookup_match`, with
`_lookup_match` as the local-then-remote composition. `_match_single_file` calls
the halves directly, because its candidates do not all exist at the same time:

1. `_try_embedded_hash_match` gets the tool's embedded hashes (a CHD reports a
   header SHA1 *and* a data SHA1) and checks them **locally only**.
2. Unless the tool's hashes are exhaustive, the file-level SHA1 is computed —
   expensive, hence lazy and size-capped — and checked **locally**.
3. Only once every one of those has missed does the **complete** candidate set
   go to Hasheous, in a single remote pass.

Two orderings that look reasonable are both wrong, and each was a real bug here:

- *Remote-checking candidate 1 before local-checking candidate 2.* Discloses a
  hash the local DATs could have identified, and a remote timeout on the first
  raises before the second — the one that would have matched — is tried.
- *Letting the embedded-hash helper go remote before the caller has hashed the
  file.* Same failure, one level up: for a non-exhaustive tool like CHD the
  container's own bytes may be exactly what the local DAT indexes, so the
  embedded hashes must not leave the machine until that has been checked.

`_try_embedded_hash_match` is therefore local-only by contract, and returns its
candidates so the caller can carry them into the single remote pass.

**The one place the rule is best-effort rather than absolute is a size-capped
file**, and it is a deliberate trade. `MATCH_MAX_FILE_SIZE` forbids reading the
file, so its container SHA1 can never be computed — meaning the local candidate
set *cannot* be exhausted, and "we never disclose a hash the local DATs could
have identified" is unachievable for that file by construction. The choice is
between disclosing the (already-in-hand, free) embedded hashes and identifying
the file at all. It still gets its remote pass, because the cap says "don't read
this file's bytes", not "don't identify this file" — and for a CHD the hash a
DAT actually indexes *is* the header SHA1, which is an embedded candidate and so
is still checked locally first. The residual exposure needs an unusual DAT that
indexes raw `.chd` container bytes rather than the header, plus a cap, plus a
file over it. A size-capped miss keeps its non-cacheable `reason`.

#### Cache entries are scoped to the sources that produced them

Every cacheable miss carries `checked_remote`, and `cached_result_usable(payload)`
is the single gate every cache read goes through (`/dat/match-batch`,
`/dat/matches/lookup`, `/dat/match-batch/job`, and the scan's Phase 3 skip).

A miss recorded before Hasheous was enabled came from a strictly weaker matcher,
so it must not be served once the stronger one is available — otherwise an
existing install switches the feature on and nothing happens, because every path
in the library already has a cached "not in any DAT" row. Hits are always usable:
local DATs are consulted first, so a remote source could not have improved on one.
This is a payload field, not a schema change.

#### Other load-bearing properties

- **A failure is not a miss.** `hasheous.lookup` raises `HasheousUnavailable` for
  timeouts, 5xx and unparseable bodies, and returns `None` **only** for the
  documented 404. `_match_single_file` converts the exception into
  `{**base_result, "error": ...}`, which the existing rule (see
  `_abandoned_match_result`) refuses to cache. Without this, one network blip
  would permanently record every in-flight file as being in no DAT.
- **An outage is learned once, not once per file.** A failure opens a 60-second
  module-level cooldown during which `lookup` raises immediately without a
  request. Files still come back non-cacheable — just without paying
  `hasheous_timeout` each. A 1,000-file scan against a dead endpoint would
  otherwise burn over four hours rediscovering the same fact. **Everything that
  can judge the server unhealthy sits inside the one `try`** — transport errors
  *and* response validation — because a proxy returning an identity-less `200`
  for every hash is exactly as much of an outage as a timeout, and validating
  after the guarded call left that case re-requesting once per file. A clean
  404 is the one non-failure: the server answered, so it clears the cooldown.
- **The timeout bounds the request, not each socket read.** `urlopen(timeout=)`
  restarts on every byte that arrives, so a server dripping slower than the
  timeout but never stopping pins the lookup — and the scan job around it —
  without ever raising, which also means the cooldown never opens. Measured:
  an 18.5s `open()` under a 2s timeout, and unbounded for a longer header.
  One `_DeadlineMixin` enforces one monotonic deadline (`_deadline_of`) inside
  `recv_into`, and it is mixed into **both** socket classes a lookup can hold:
  `_DeadlineSocket` (the raw socket) and `_DeadlineSSLSocket` (installed via
  `SSLContext.sslsocket_class`). Two classes over one mixin, because
  `wrap_socket` detaches the raw socket and rebuilds from its file descriptor,
  so the raw class cannot carry through. Doing it at the socket rather than
  around the body read is what makes the bound real: an earlier body-only
  version left the identical hole in the headers, where a hostile or broken
  server can drip just as easily.

  The connection is deadline-aware from the first packet:
  `_DeadlineHTTPSConnection` swaps in `_connect_with_deadline`, which loops the
  `getaddrinfo` results itself and gives each attempt only what is *left* of
  the budget. `socket.create_connection` gives each address the full timeout —
  measured, three blackholed addresses cost 9.0s under a 3s timeout, and a
  redirect opens a fresh connection with a fresh budget. `_DeadlineHTTPSHandler`
  is what puts that connection in `_opener`'s hands; both halves of the wiring
  (the handler class and the context's `sslsocket_class`) are pinned by tests,
  because rebuilding the opener with stock parts removes the protection
  silently while every behavioural test still passes.

  That covers all four phases through one mechanism — raw connect, the proxy
  `CONNECT` tunnel (`http.client._tunnel` reads it off the raw socket, so a
  dripping proxy used to pin the request and never raise), the TLS handshake
  (which makes **zero** `recv_into` calls, so `do_handshake` applies the
  deadline itself), and the response.

  Two things remain outside it, both accepted. A peer that drips one TLS
  *handshake record* at a time inside the remaining budget is not bounded, and
  **DNS is not bounded by `hasheous_timeout` at all** — `getaddrinfo` runs
  before any socket exists and ignores socket timeouts. The resolver bounds
  itself (`/etc/resolv.conf`: glibc defaults to 5s x 2 attempts per nameserver)
  and, unlike a drip, it *raises* — so it opens the cooldown and the rest of a
  bulk job short-circuits instead of stalling one file at a time. Closing
  either means leaving urllib or resolving on an abandonable thread.
- **The toggle persists before it applies.** `PUT /api/dat/hasheous` writes the
  preference first and only then flips the in-process override. The other order
  meant a failed write (locked SQLite, full disk) left the process sending
  hashes remotely while the endpoint reported failure and the UI still showed
  the switch off — a privacy-relevant lie, not just a stale display.
- **`dat_id` is always `None` on a remote hit.** It is a FK into the local `dats`
  table and a remote match has no row there. `dat_store` already nulls unknown
  values before writing, so this keeps the cached row byte-identical across
  re-runs rather than depending on that guard.
- **"Remote hit" is recorded, never inferred.** `_remote_hit_clause()` in
  `dat_store` is the one place the question is answered, and it answers from
  `payload.source`, not from `dat_id IS NULL`. The FK proxy was wrong for one
  real row: a *local* hit whose DAT was deleted between the match and the write
  has its dangling FK nulled by `_set_matches_batch_sync`, which also dodges the
  `WHERE dat_id = :id` cascade — so it was preserved through every later import
  and went on serving an identity from a DAT the operator had removed.
  `coalesce` is load-bearing there: SQL `NULL` is not `False`, so a payload with
  no `source` must compare unequal rather than unknown, or the negation spares
  exactly the rows it is meant to drop.
- **Local-first holds across the whole call — for the disclosure, not only the
  verdict.** The remote request is an await of its own, and a DAT import commits
  in its own transaction, so `_remote_lookup_match` re-checks the local index
  *between* candidate requests as well as once more after the loop. The post-loop
  pass alone was not enough: it protects the answer, but by the time it runs the
  hash the newly-imported DAT could have identified has already gone out. A CHD
  sends up to three, so that is up to two avoidable disclosures per file. Each
  check is over the whole candidate set, and each is one indexed lookup.

  That recheck is itself an await, so the stop conditions
  (`_remote_pass_stopped`: the toggle, and the job's cancel event) are tested on
  **both** sides of it — before, to stop a halted pass doing further work, and
  again immediately before the request, which is the load-bearing half. A guard
  that only preceded the recheck reintroduced, one await later, exactly the
  window the recheck existed to close.
- **Concurrent DAT imports have a boundary, and it is the transaction.** A match
  runs local-first, but the decision and the write are separate operations, so
  three windows exist between them: the file hash (seconds), the remote request
  (seconds), and decide-then-write (milliseconds). The first two are closed by
  re-checking the local index — before the remote pass, and again over the whole
  candidate set before a remote answer is accepted. The third is closed in
  `dat_store._local_index_now_covers()`, which re-checks inside the writing
  transaction and skips the write when the local index has since learned any of
  the candidates. The transaction boundary itself is closed by running that
  re-check once the writer holds SQLite's write lock (below), so no import can
  commit between the check and the write it guards.

  That third guard covers **every verdict the remote source took part in, over
  the whole candidate set** — not just the hit, and not just the hash that
  matched. A stamped *miss* is cacheable too, and its `checked_remote` stamp
  stays valid, so restricting the guard to hits let a fresh local match stay
  hidden until the next import happened to invalidate the row; and a CHD offers
  up to three hashes, so the DAT that landed mid-flight may know a candidate
  other than the one that hit. The route attaches the candidate SHA1s under
  `dat_store.CANDIDATE_HASHES_KEY` (`_carrying_candidates()`, on both cacheable
  remote exits); the store revalidates them and strips the key in
  `_persistable_payload()`, so it never reaches a persisted row. It is a hash
  re-check rather than a DAT-index generation on purpose: a generation would
  skip the write on any unrelated import and send the file straight back out to
  the remote source, which is the same re-disclosure cost rejected just below.

  **Local verdicts are checked by generation instead (issue #492).** A DAT hit,
  or a miss the remote source was not asked about, was decided entirely by
  the local index, so *any* change to it can change the verdict: a new DAT can
  know the hash of a local-only miss, or know a hit's hash too and outrank it,
  and a miss with no candidates has nothing to re-check. Each `DATStore` keeps
  a generation (`index_generation()`); every change to the local index (an
  import, a sync's `persist()`, a DAT delete) runs inside
  `_changing_local_index()`, which moves it on entry and on exit and reads
  `None` while the change is in flight. `_match_single_file()` stamps every
  result with the generation it read before its first lookup
  (`dat_store.INDEX_GENERATION_KEY`, stripped by `_persistable_payload()` like
  the candidates), and `_verdict_is_stale()` refuses a stamped local verdict
  unless its generation is the current one; `None` on either side never is. A
  local dict with no stamp makes no claim and is written as before.

  The generation is two counters added together: the in-memory one above, and
  a shared one in the database (the `preferences` row
  `SHARED_GENERATION_KEY`, `dat_index_generation`) that each of those changes
  moves inside its own transaction (`_bump_shared_generation`). The CLI's
  `dat import` and `dat sync` run in their own process, with their own
  in-memory counter, so only the shared one tells the server about them
  (CodeRabbit on PR #527). Both only grow, so the sum moves when either does.
  `index_generation()` reads the database (on the writer's own session at the
  write boundary), so `_match_single_file()` calls it off the event loop.

  Remote verdicts take no part in the generation check, even while a change is
  in flight. A refused verdict goes to a *local-only* rematch, which cannot ask
  the remote source again, so refusing a remote hit for an unrelated import
  turned it into a cached miss (Codex on PR #493). The hash re-check is enough
  for them: a change that committed first is seen by it, and one that commits
  afterwards keeps a remote hit (which the post-change listing rematches) and
  drops a remote miss (which the invalidation reports).

  Where the checks run is what makes them sound. The writer opens its
  transaction with `BEGIN IMMEDIATE`, so it holds SQLite's write lock before it
  reads anything, and only then reads the generation, prefetches the rows and
  makes the checks. An import holds that lock from its first DAT row to its
  commit, with the generation moved before that, so while the writer holds it
  the committed index cannot change and any change that started earlier is
  visible in the generation. Each verdict is decided on its own, so one stale
  verdict does not cost the rest of the batch. Holding the lock from the start
  also keeps out a concurrent delete: with an unlocked prefetch, an import's
  invalidation (or a prune) could remove a prefetched row before the flush,
  which raised `StaleDataError` and rolled back the batch with nothing
  reported. No Python lock is held across a database wait, and none across a
  network call.

  **A refused write is reported, not dropped.** `set_match()` and
  `set_matches_batch()` share one writer (`_set_matches_batch_sync()`; the
  single-row call is a batch of one) and return the paths they refused because
  the index moved. Every writer (the match job, the batch route, the scan)
  passes them to `routes.dat.rematch_refused_verdicts()`, which runs the same
  local-only rematch a DAT change does: deferred behind the running match job
  (the writer itself, when it is the job, so its own teardown drains it) or
  started now. A refusal needs a DAT change between a decision and its write,
  so a path can only be requeued as many times as the index changes.

  Remote verdicts stay out of the generation check, read-side too. Rejecting
  stale remote hits on read means stamping them with the DAT-set generation,
  which invalidates every remote hit on every DAT change — the recompute misses
  locally (the new DAT usually doesn't cover the file that needed Hasheous) and
  re-queries, re-disclosing thousands of hashes per sync, forever. That is a
  worse trade than the thing it fixes, whose worst outcome is a HASH badge where
  a DAT badge would do: same game, self-correcting on the next rescan.
  Write-side, a generation would refuse them on every unrelated import, which
  is the hash re-check's reason for existing.
- **A cancelled match job does not hand its queue to a successor.** The
  deferred-rematch drain lives in `_run_match_job`'s `finally`, which a
  cancellation reaches like any other exit — so the cancelled job started a
  *replacement* job on its way out. `/jobs/cancel-all` snapshots the job list
  before that replacement exists, so the rematch escaped the cancellation
  entirely and went on hashing moments after the operator asked for silence.
  Cancellation now discards the queue instead (`_discard_deferred_rematch`),
  logged rather than silent: those files keep their current verdicts until they
  are browsed or a rescan runs. Dropped rather than left queued because nothing
  but a finishing job drains the set, so holding them would mean waiting on an
  unrelated job that may never come.

  A job that *crashed* is treated the same way, and for a sharper reason. The
  abandonment guard raises when a hash helper outlives SIGKILL and is still
  reading unresponsive storage, deliberately stopping so the rest of the
  library does not strand another process on that volume (issue #268). But
  `job_success is False` cannot tell that mid-loop crash from a job that ran
  every path and merely reported failures, so the teardown drained the queue
  and resumed hashing the same volume seconds later. `job_crashed`, set only by
  the `except Exception` branch, is the distinction; an ordinary
  finished-with-failures job still drains, since nothing about it says the next
  batch cannot run.
- **A match job fails on its *checkable* files, not its total.** Policy skips
  (over `MATCH_MAX_FILE_SIZE`, not a regular file) are files the job
  deliberately did not check, so they are excluded from the all-failed
  denominator instead of counting as survivors. With `errors == total` a single
  oversized ISO in the batch downgraded a complete Hasheous outage to a green
  "complete" carrying a generic error count — exactly the misread that branch
  exists to prevent. A batch of nothing but skips still completes: nothing was
  attempted, so nothing went wrong.
- **A DAT change re-matches through one helper, over before *and* after.**
  `rematch_after_dat_change()` is shared by the manual upload (`import_dat`) and
  the MAMERedump sync; both want "recompute what already had a verdict against
  the new index", and it had been written twice. The matcher is single-flight,
  so when a job is already running those paths go into
  `_deferred_rematch_paths` and the finishing job drains them — "best-effort"
  means *later*, not *never*, which is what dropping the `None` return used to
  mean.

  The caller's snapshot is taken *before* the change, because the change is
  what invalidates — but it is not transactional with it, and a match already
  in flight can persist a remote hit inside that window. Invalidation preserves
  remote hits by design, so such a row survives while being absent from the
  snapshot, and since a cached hit is always usable nothing would ever
  recompute it: the DAT just imported could know that hash and never get the
  chance to say so. `_paths_needing_rematch()` therefore unions the snapshot
  with a listing taken *after* the change — anything still cached once the new
  index is live either predates the change or was written during it, and both
  need recomputing. This is why the sync calls the hook unconditionally now:
  an empty snapshot is exactly the case where the surviving row is the *only*
  thing to rematch, and the old `if previous_match_paths:` guard skipped it.

  The rows the change *dropped* need the same treatment, and the snapshot
  cannot supply all of them either (issue #492): a DAT-derived hit written
  after the snapshot and before the commit is deleted by the invalidation, so
  it is in neither the snapshot nor the survivors. `_invalidate_dat_derived_matches()`
  lists the rows it deletes inside the import's own transaction, under the
  write lock the import already holds, and `import_dat()` / `persist()` hand
  them to the caller through `invalidated=`; both callers add them to the
  snapshot. A row written after the commit was decided against the new index,
  because the store refuses one that was not (above). An empty rematch set is
  logged (`nothing to rematch`) rather than returned silently.

  Deleting a DAT drops the matches that cite it, and those are reported the
  same way (`delete_dat()` / `delete_dats_bulk()` with `invalidated=`, listed
  in the delete's transaction by `_delete_matches_citing()`). The sync adds
  the ones its old-DAT delete removes to its rematch set: a verdict decided
  after the persist can still cite an old DAT. The delete route rematches only
  the rows it removed, since a delete cannot improve any other row.

  **The rematch job is local-only** (`schedule_match_job(..., local_only=True)`,
  threaded down to `_match_single_file`). What a DAT change alters is the local
  index; the remote source is exactly as it was, so re-asking it once per
  previously-verdicted file cannot yield an answer different from the cached
  one — it only re-discloses every hash, thousands per MAMERedump sync, on a
  schedule the operator never chose. This was the single largest disclosure
  path in the feature and it was unintended: `_hash_one_for_job()` calls
  `_match_single_file()` with no cache consult, so *every* rematched path ran
  the full local-then-remote pipeline. The three outcomes fall out of guards
  that already existed:

  | after the change | result |
  |---|---|
  | new DATs cover the file | local hit written, badge upgrades HASH → DAT |
  | they don't, row was a remote hit | unstamped miss, which `_would_downgrade_remote_hit()` refuses to write — badge stays |
  | they don't, row was a remote-checked miss | that row was dropped by invalidation, so the unstamped miss replaces it and `cached_result_usable()` finds no stamp — re-checked remotely when the file is next browsed |

  The third row is the residual: a previously-unmatched file *is* re-disclosed,
  but lazily, one file at a time, on the operator's navigation rather than in a
  burst at sync time. Closing it too means keeping remote-checked misses across
  an invalidation, which would serve a stale negative for any file the new DAT
  does cover — the trade the invalidation exists to avoid.

  The unstamped miss must still **carry the recomputed hashes**
  (`_carrying_file_hash()`). "The DATs still don't know it" and "this is a
  different file now" both reach the store as an unmatched result, and a
  recomputed hash is the only thing that separates them:
  `_proves_content_changed()` compares against the stored one, and without it
  the row-two guard protects the badge of a file that has been *replaced*,
  permanently. The outage exit had carried the file-level hash for this reason
  since round 7; the local-only exit was written without it, so one helper now
  serves both.

  Carrying the evidence is only half of it: **every** consumer of a
  non-cacheable result runs the pruner. The scan did from the start; the match
  job did not, so a capped file replaced between DAT imports produced a
  non-cacheable result the store never saw, was never pruned by a scan that
  might not run, and kept naming the previous game with nothing left to
  re-check it. `drop_if_content_changed()` now sits on both paths, and the
  comparison itself lives in `dat_store.drop_match_if_changed()` — one
  predicate (`_proves_content_changed()`), one transaction. Reading the row in
  the route and deleting it in a second call also left a window where a
  concurrent match job could persist the *replacement* file's correct result in
  between, and the delete removed that fresh row instead of the one it had
  compared; the `match` workload token covers hashing, not this cache
  operation, so the two really can overlap.

  Both non-cacheable exits carry it too — a size-capped result never read the
  container, but the embedded hashes it *did* recompute are exactly what proves
  a swap to the scan's pruner. Which is also why the scan stopped *deleting*
  those rows outright: a cap is not evidence a file changed, and with a
  recomputed hash in hand the capped case goes through the same same-domain
  comparison as the outage case rather than dropping a valid identity for a
  file nobody touched.

  It carries the **whole typed candidate set**, not just `file_sha1`, and the
  comparison keys on the *stored row's own* `match_type` rather than demanding
  `file_sha1`. `dat_store.recomputed_hash_in()` is that lookup, module level
  because both ends of the rule need it: the store's write guard before
  overwriting a remote hit, and `drop_match_if_changed()` before deleting a row
  a non-cacheable result left behind. That restriction existed for a real reason — a CHD hit is stored
  against its embedded `chd_sha1` while a rescan recomputes the container's
  `file_sha1`, and those differ for a file nobody touched, so comparing them
  deleted valid badges. But it was too blunt: an *exhaustive* tool (Dolphin
  RVZ/WIA/GCZ) recomputes the very hash it matched on, so the domains agree and
  the comparison is sound — and such a format never computes a `file_sha1` at
  all, so under the old rule a replaced RVZ could never be proven changed and
  kept its previous game's badge for good. Keying on the stored domain admits
  that case and still refuses the cross-domain one.
- **The workspace re-checks matchability only while it has none.**
  `refreshMatchingAvailability()` already absorbs a provider flip made
  elsewhere (another tab, an API client) — it resets the match cache and the
  attempt guard when `hasheous_enabled` changed under it — but nothing drove it
  after mount, so a DAT-less install whose operator enabled Hasheous in a
  second tab sat at `matchingAvailable === false` and started no jobs until
  this tab visited the DAT view or reloaded.
  `watchMatchingAvailability()` polls it for as long as the workspace is
  mounted. An earlier version stopped once something could answer, on the
  reasoning that only `matchingAvailable === false` matters — but a provider
  *disabled* elsewhere makes this tab merge now-unmatched rows into `matches`,
  and stopping meant a later re-enable was never seen, so those paths stayed
  "known" forever. One transition healed, the other made permanent; it watches
  both now. The workspace owns the teardown, and the file list also cancels the
  retry timer when its visible set empties — filtering to zero entries does not
  unmount it, so neither the effect's normal path nor `onDestroy` would run.

  Observing the flip is not the same as acting on it. Every policy change —
  provider toggle, DAT import, delete, finished sync — clears `matches`, and
  that clear is invisible to the file list's `$effect` by design: `hydrate()`
  snapshots the map under `untrack` precisely so the effect does not subscribe
  to the cache it fills. With local DATs present `matchingAvailable` never
  flips either, so a provider enabled in another tab blanked the badges and
  nothing re-hydrated them until navigation. `datMatching.policyGeneration`
  (the reactive `_generation` counter) is the one dependency the effect reads
  for this, which makes the clear self-healing. It moves only on a real policy
  change — a `/dat/stats` poll that observes nothing new must not bump it, or
  it invalidates in-flight match jobs on every tick.

  The toggle itself re-applies its `PUT` response after refreshing stats, so a
  failed `/dat/stats` cannot bury the state the backend just switched to — but
  only when that refresh brought nothing back. Unconditionally it did the
  opposite harm: another tab flipping the provider while the refresh was in
  flight makes the refresh the newer truth, and re-applying the older response
  on top of it reported the opposite of the backend for up to a poll interval.
- **`matching_available(has_dats)` replaces the bare `has_dats` gates.** Those
  gates predate the remote source and would otherwise short-circuit before it is
  ever reached for an operator who imported no DATs at all. The frontend has the
  same gate — `datMatching.matchingAvailable`, derived from `total_dats > 0 ||
  hasheous_enabled` — and it is deliberately *not* named `hasDats`, because
  reading it as "are there DATs" is exactly what would silently stop the browse
  path from ever scheduling a match job.
- **A broken store still skips Phase 3.** `_scan_phase_dat_match` tracks store
  health separately from `has_dats`: the phase exists to prime the match cache
  and every write goes through that store, so proceeding on a dead DB would fail
  the whole scan instead of degrading quietly.
- **A scan says when its remote phase failed.** `_scan_phase_dat_match` returns
  `(matched, hasheous_errors)` and the scan's final line carries the second
  number. The errors are still non-cacheable and still retried, but a rescan
  run during an outage used to finish "0 matched" — indistinguishable from a
  library genuinely in no DAT. The scan is *not* reported as failed: phases 1
  and 2 succeeded and metadata was collected, so the honest signal is a
  qualified success, not a flipped boolean.

`services/hasheous/` is a two-module package: `__init__.py` is the client (the
toggle, the cooldown, `_fetch_json`, `lookup`, `health`, normalization) and
`transport.py` is the deadline-aware HTTPS stack described above. They are
split because the transport is a self-contained concern with its own failure
modes, and because the four phases it now covers were each found a review round
apart and patched separately before being made one mechanism. The package
re-exports the transport names, so `from services import hasheous` still
reaches the whole surface and `_fetch_json` remains the single patch seam —
important, because a second module object here is exactly how a test suite ends
up silently hitting the real network.

The client is stdlib-only (`urllib.request`), mirroring `services/dat_sync.py`:
`_require_https`, an explicit `User-Agent`, a hard timeout, a response size cap,
and `_fetch_json` as the single seam tests patch. Redirects go through
`_HTTPSOnlyRedirectHandler`, which re-runs the scheme check on every hop —
`urlopen` follows redirects itself, so validating only the initial URL would let
a misconfigured or hostile server bounce a lookup to `http://` and put the file's
SHA1 on the wire in the clear. Extra fields ride in the existing
`dat_matches.payload` JSON column, so no migration is involved.

### 3.3.5.2 Every DAT's match, DAT families and the ranking (`services/dat_ranking.py`, #424)

**The store keeps every match.** `dat_hashes` is unique on
`(hash, hash_type, dat_id)` (migration 0022), so a hash two DATs know keeps
both rows; it used to be `(hash, hash_type)`, and the later import overwrote
the earlier one. Both import paths (`/dat/import` and the MAMERedump sync's
staged `persist()`) go through one `DATStore._insert_dat`. `lookup_sha1` /
`lookup_md5` return every DAT's record under `matches` (each with `dat_id`,
`dat_name`, `family`, `game_name`, `rom_name`), and `_local_dat_record` carries
them into the match result, so the cached `dat_matches.payload` stores the
facts, not a verdict. `_local_lookup_match` looks up **every** candidate hash
it is given before ranking (a CHD's `chd_sha1` and `chd_data_sha1` can be
known to different DATs, and stopping at the first hit let the hash order pick
the name), and each entry in `matches` keeps the `file_hash` / `match_type` it
matched on, so the winner's hash and domain become the payload's and a later
re-ranking switches them with the name. Every match path (`/dat/match`,
`/dat/match-batch`, the match job, the metadata scan) reaches the DATs through
it. A file-level SHA1 that `_match_single_file` computes only when the
embedded hashes miss is still not forced.

**Families.** Each DAT has a `family`: Redump, No-Intro, TOSEC, MAME or other.
`dat_ranking.dat_family()` reads it at import from the header's name,
description, homepage, url and author (a No-Intro and a Redump DAT share the
"Company - System" name shape, so the name alone often can't tell). A Hasheous
hit gets the family of its `signatureSource` from the same function. A DAT
imported before 0022 kept only its name and description, so the migration
reads its family from those.

**The ranking** is one ordered list of DATs, stored as the nullable `dats.rank`
(0 first). A DAT the user hasn't placed sorts after every placed one, by its
family's place in `DEFAULT_FAMILY_ORDER`, which has separate orders for disc
and cartridge Images: Redump first for discs, No-Intro first for cartridges.
Whether an Image is a disc is `dat_ranking.image_kind(path)`, which asks
`registry.disc_extensions()`; there is no list of its own. A DAT imported under
the same name and family as a ranked one takes its rank, which is what keeps
the user's order across the MAMERedump sync (it replaces every DAT with a new
id). `GET/PUT/DELETE /dat/ranking` read, save and forget the order; a `PUT`
naming an unknown or repeated id is refused (400) and saves nothing.

**The verdict is computed when a match is read**, not stored.
`dat_ranking.apply_ranking()` orders a payload's `matches` by the current
ranking and sets the top-level `dat_id` / `dat_name` / `family` /
`game_name` / `rom_name` to the winner. `DATStore.get_match` and
`get_matches_batch` apply it to every cached row, and `_local_lookup_match` to
every fresh hit (`DATStore.with_current_ranking`), so a new order shows on the
next lookup without re-hashing anything. A payload with no `matches` (a miss,
a Hasheous hit) passes through unchanged. A row
whose every DAT is gone (one deleted between the lookup and the cache write)
reads as uncached (`None`), so the file is matched again rather than named by
a deleted DAT. A local hit cached before #424 (a `dat_id`, no `matches`) can
never be ranked, so `dat_ranking.is_unrankable_local_hit` makes it read as
uncached too, and the next match records every DAT's match. This is decided on
read rather than by deleting rows in migration 0022, so nothing is lost; a
Hasheous hit is not affected. Local-first is untouched: Hasheous is still asked only when no local DAT
knows any candidate hash.

**Conflicts.** When the best rank is shared by matches that give different
game names, nothing names the Image. The payload keeps `matched: true` and
shows the first tied match in the top-level fields (an existing badge still
reads something), and adds `conflict`, the list of every disagreeing match.
Equal ranks that agree on the name are not a conflict.

**The shared answer: `DATStore.ranked_match_for_image(path) -> RankedMatch`.**
This is the one function a caller asks "what is this Image called?" (rename to
DAT name, the Inbox, the RomM view). It reads the match the app already has
for the path, applies the current ranking and never hashes. `top` is the
record that names the Image, or `None` when there is no match or there is a
conflict; `conflict` lists the disagreeing matches (each with its DAT's id and
name); `matches` lists every match, best first. Code that acts on a name must
use this, never the payload's top-level fields, which are only a display
choice on a conflict. Blocking, so call it off the event loop.

### 3.3.6 Re-run fast path (`JobManager._output_already_verified`)

`_process_job` recognizes a prior success before it re-spawns the converter: a
re-queued job whose target artifact is already on disk **and provably the exact
result of the current request** completes as a no-op instead of re-running the
tool (issue #184, site 1). The check runs at the top of the conversion `try`
block — before archive extraction and before `_clear_existing_output` — so the
existing verified output is neither re-extracted-from nor deleted; on a hit the
job jumps straight to `COMPLETED` (progress 100, size via the shared
`_compute_output_size`, a `complete` event with `verified=True`,
`source_deleted=False`). A cancel is honored on both sides of the awaited
lookups: checked before, and re-checked after `_output_already_verified` returns
(a cancel that lands mid-lookup raises `ConversionCancelled` rather than
completing the no-op).

**The evidence is a `produced_meta` snapshot, not a bare "verified" flag.** A
plain verification record proves only that *some* file at that path passed
integrity verification at *some* time — not that the current file is that
artifact, nor that it matches the current request's output-shaping settings, nor
that a multi-file source (a `.cue`/`.gdi` and its tracks) is unchanged. So the
fast path trusts **only** records carrying a `produced_meta` blob, written by a
prior **delete-on-verify** conversion right after its verify passed (the
`verifications.produced_meta` JSON column, Alembic migration `0003`). A manual
`/info` verify records **no** meta and never qualifies. `_build_produced_meta`
captures, off the event loop:

- `mode`, `compression`, `split` — the producing conversion's output shape.
- `source` — a stat fingerprint (`{realpath: {size, mtime_ns, inode, device}}`)
  of the **complete** source set. It is the job's **pre-conversion** delete
  snapshot (captured at planning, before the converter read the source),
  re-validated against the on-disk source at record time: if the source changed
  between planning and verify — e.g. mutated while the converter was running —
  the snapshot no longer matches and **no** meta is recorded, so a later
  re-queue can't no-op against an output built from now-stale bytes.
- `output` — a `{size, mtime_ns, inode, device}` fingerprint of the produced
  artifact (taken after the disc-ID embed, so it reflects the final bytes).

The `inode`/`device` fields are the same four stat fields the delete-safety
snapshot trusts to authorize an *irreversible* delete (`build_delete_snapshot`),
so a reuse — a strictly less destructive decision — is held to no weaker a bar:
a copy-restore lands on a new inode, a moved mount on a new device, and either
forces a re-convert.

`_produced_meta_matches` (off the event loop) then admits the no-op only when
**all** hold: `mode`/`compression`/`split` equal the current request's, the
current output fingerprint equals the recorded one (so a replaced/corrupted file
can't pass), and the current complete-source-set fingerprint equals the recorded
one (so a changed track — not just the descriptor — forces a re-convert).
`_output_already_verified` additionally excludes directory (folder→ISO) jobs
(split-set outputs) and `delete_on_verify` *requests* (they must run their
guarded source deletion), and treats any store hiccup, missing record, absent
`produced_meta`, or un-fingerprintable source (archive members, unsafe/missing
tracks) as "no match" — always falling through to a full re-convert rather than
risk a surprising output.

**Known limitation.** `compression` is stored as the request declared it, so
`None` means "the tool's default." A tool that resolves its default from mutable
settings (e.g. nsz's `NSZ_COMPRESSION_LEVEL`) could, after an operator changes
that setting *and* a prior delete-on-verify run was cancelled after verify,
deliver the earlier-default artifact for a `None`-compression re-queue. That is a
compression-*level* difference on an otherwise-correct, verified artifact of the
exact source — not a content mismatch — and is accepted rather than have every
tool persist its resolved defaults.

### 3.3.7 Finished-job history and the Ledger's totals (#533)

`MAX_JOB_HISTORY` (default 500) caps how many **terminal** jobs `JobManager`
retains; `_prune_jobs` deletes the oldest past the cap. The job list is a
bounded window, not a record of the run, so no all-time count comes from it.
The Ledger is the record: every finished job lands in it (`ledger.record`, a
terminal listener) and its `ledger_totals` counters only ever grow, whatever
the cap, a Clear or a retention prune removes. `GET /api/ledger/totals`
answers `{"jobs": {"completed": N, "failed": N, "cancelled": N}}` from them,
and the dashboard's Done / Failed tiles read it (`ledger.totals`). The Jobs
panel's tab badges count the rows the list holds, and its Completed and
Failed tabs link to the Ledger for the rest.

Since the list is a window, its counts can stay flat while jobs finish (at
the cap a new finished job replaces an evicted one), so nothing keys a
refresh on them. The file listing (`App.svelte`) and the dashboard's Ledger
cards (`Dashboard.svelte`) both key on `terminalJobIds(jobs.jobs)`
(`stores/jobConstants.js`): the file listing refreshes when an id it has not
refreshed for appears. `ledger._load` reads recent, savings and totals with
`Promise.allSettled`, so a read that fails keeps its card's last good value
without dropping the others.

The jobs store still tombstones a finished job the backend stops listing
(`refresh()` reconciles against `/api/jobs`, and Clear names what it deleted),
so a late copy of it (an SSE frame, a REST snapshot, an Auto response) never
brings it back.

**The stream follows every job from its registration** (#456). It opens a
`JobFeed` (`JobManager.watch_jobs()`) before anything else, and
`JobManager._register_job`, the one place a job (conversion or external) is
added to the list, subscribes every open feed to the new job before anything
can send an event for it. So a job that starts and ends between two of the
stream's 0.1 s ticks (a patch job lives about 60 ms) still reaches the client
with its whole life, end state included, exactly once. The tick still follows
any queued or running job the feed doesn't follow yet, which covers the jobs
that were live when the stream opened (subscribed before the snapshot, as
above); a job already followed is never subscribed twice. Each tick drains
every followed queue rather than taking one event a job, since a queue is
bounded (100) and a burst faster than one event a tick would fill it and drop
the end state. A job is dropped from the feed at its terminal event, or once
the manager no longer has it and its queue is empty. A job can be registered
on a worker thread: the feed and `_subscribers` each sit behind a lock, and the
queues themselves are written only on the loop (`_notify_subscribers`). The
feed is closed in the stream's `finally`, which unwatches it and unsubscribes
every queue.

**The three invariants a client must preserve** (`src/lib/stores/jobs.svelte.js`):

1. **Displayed total = retained + evicted.** Apply the same filters to both
   halves; the tally is keyed by mode precisely so the external-scan filter
   (`metadata_scan` / `dat_match` hidden unless asked for) can be applied to
   evicted jobs too.
2. **Drop `evicted_ids` before taking the new tally.** A completion reaches the
   client over SSE *before* the prune it triggers, so the client briefly holds a
   row the backend has already deleted. Counting it as retained *and* as evicted
   inflates every badge by one per completion for as long as a batch runs past
   the cap.
3. **Never apply a payload older than the one already applied**, and never
   compare across generations. The REST read races the stream; without a `seq`
   check a hydration response captured before a newer event can land after it
   and roll the totals backwards, and since the backend only re-emits on change
   they would stay wrong until the next poll. Adopt `history_seq` from the Clear
   response for the same reason.

**Reading both halves without a stream** (the 30 s poll, and every reconnect —
a fresh stream opens its cursor at *now*, so its first frame cannot name what
was evicted while the client was away): capture the cursor, read `/api/jobs`,
then read the overflow **with that cursor**. In that order the two reads
reconcile exactly — anything evicted before the list read is already absent from
it and counted in the totals, and anything evicted after is named in
`evicted_ids` and dropped. Skip the overflow read entirely if the list read
failed: the halves are only coherent together.

Where retained rows exist but the total exceeds them, say so in the UI rather
than let a badge and a list disagree — `JobsPanel` renders *"Showing the 500
most recent of 1,153"*, and a distinct empty state for a tab whose jobs have all
aged out.

### 3.3.8 Output-conflict resolution (`services/output_conflicts.py`)

"Is this mode's output already taken, and what do I do about it?" is asked by
every path that queues a job — `/api/jobs`, `/api/jobs/batch`, and the RomM
automation sweep. It is not an HTTP concern, so it does not live in a route:
both helpers sit in `services/output_conflicts.py` and every caller gets the
same answer, or `duplicate_action` would mean different things depending on how
the job was started.

- **`check_output_conflicts(mode, output_path) -> (exists, locked)`** —
  companion-aware. An `extractcd` `.bin` sidecar or a split `folder_to_iso`
  build's numbered parts count as occupying the destination, enumerated through
  the owning tool's `companion_outputs` hook rather than re-derived per caller.
- **`get_unique_output_path(base_path, mode=None)`** — probes `name_1`,
  `name_2`, … until the file *and* that mode's companions are all free. Raises
  **`OutputPathLocked`** when `base_path` sits inside a locked directory subtree:
  every numbered sibling it would try shares the same held parent, so probing on
  would spin with no sleep until the dir lock released and then return an
  arbitrary name. When the mode's `output_ext` is compound (xiso's `.xiso.iso`)
  the counter goes before the whole compound (`Game_1.xiso.iso`), never inside
  it (`Game.xiso_1.iso`), so the renamed product still matches the tool's
  verify claim.

The *failure shape* stays a caller concern. `routes/convert.py` keeps a thin
`get_unique_output_path` wrapper that translates `OutputPathLocked` into
`SkipFile(SkipReason.OUTPUT_LOCKED)`, so the job pipeline defers and requeues
the file like any other locked output; the sweep simply drops the candidate and
picks it up next time. Both touch the disk (a directory mode's companion lookup
scans), so call them off the event loop.

#### Destination reservation is linear, and does no I/O (`_canonical_path`)

`JobManager._reject_claimed_destinations_locked` runs on the event loop while
`_create_lock` is held, so its cost is the cost of accepting a batch. Every
destination is canonicalised **once** (`_canonical_path`) and compared through a
dict, and the live-job map is built once per batch (`_active_output_map`) rather
than re-derived per spec. The pairwise form re-resolved every earlier
destination for every new one — quadratic in the batch size, in blocking
`realpath` stat chains, against the remote mounts this integration exists for.

Linear was not enough: *one* `realpath` into an unresponsive NFS/SMB/rclone
mount blocks in uninterruptible I/O, on the event loop, under the lock — which
is every unrelated API request and all job creation, frozen for as long as the
mount stays quiet. So the reservation now does **no filesystem work at all**.
`create_job` / `create_jobs_atomic` call **`_reservation_keys(job_specs, mode)`**
*before* taking the lock: it derives each destination with `_derive_output`
(pure string work — every tool's `output_path()` is stem + suffix, no stat),
collects them with the sources and every live job's output, and resolves the lot
in one `run_detached` thread under `asyncio.wait_for(_CANONICAL_PROBE_SECONDS)`.
The result is a `{path: canonical}` map, pre-seeded lexically
(`normpath(abspath())`). `_canonical_path(path, resolved)` reads that map and
never touches the disk.

**An expired bound refuses the submit — it does not degrade to the seed.**
Completing on lexical keys reads as the conservative choice and is the
opposite. The lexical seed is the path's *spelling*, and `realpath` is the only
thing that makes two spellings of one file compare equal; so a destination named
through a symlinked library root and one named through the path underneath it
get two different keys, and both submissions pass a check whose entire purpose
is to reject the second. When the mount comes back they write the same file —
concurrently once `MAX_CONCURRENT_JOBS` is above one, otherwise one over the
other — and with delete-on-verify both sources are removed for the single
artifact that survives. `_resolve_paths_bounded` therefore raises
**`DestinationUnresolvableError`**, which the submit routes answer **503**: the
volume is not answering, nothing was queued, come back. A volume that cannot
complete a `realpath` inside the bound is one the conversion would have failed
on anyway, and being told at submit time costs a retry rather than a file. The
partial result is not merged either — a half-applied map compares one
destination lexically against an earlier spec and canonically against a later
one, which is the same hole with extra steps.

**A new caller of the reservation must pass a map from `_reservation_keys`.**
Calling `_canonical_path` without one is the blocking form, and it is only
correct off the event loop.

`companion_outputs` is enumerated in that same detached pass, not under the
lock: it is **not** pure path math for every tool -- makeps3iso probes the disk
for its numbered split parts -- so the pre-flight returns a `_Reservation`
carrying both the canonical map and each destination's full key set, and the
locked check reads them. That pass refuses on the same terms: reserving the
primaries alone leaves the sidecars unclaimed, so a second submission whose
primary differs but whose companions land on these gets through, and a cue sheet
or a numbered split part written by two jobs is the same lost data as the image.

`_derive_output` is pure string work only because **every tool's `output_path()`
is stem + suffix arithmetic with no stat**, and three hot paths now depend on
that: this pre-flight, `registry.mode_is_automatable`, and the status route that
serves it. A tool whose `output_path` probed the disk would put filesystem I/O
back under `_create_lock` and on the event loop -- so keep it pure, and put the
probing in `detect_output` / `verify_target`, which have bounded seams.

Moving the resolution ahead of the lock opens one gap the map alone cannot
close: two submissions can both pre-resolve before either takes the lock, so
the second's map cannot contain a job the first queues in between. Falling back
to lexical for that job would miss that a symlinked spelling and the real path
name one file — and with overwrite plus delete-on-verify, both sources are
deleted for one surviving output. So `_queue_job_locked` records the key it
claimed the destination under in **`_output_keys[job_id]`**, and
`_active_output_map` prefers it over the (possibly stale) map. The entry is
dropped when the job leaves the queue; only live jobs are consulted.

#### One destination, one source (`output_conflicts.collapse_to_winners`)

Two inputs can resolve to the same output — a `.cue` beside its `.bin`, two
`Game.iso` files aimed at one output folder — and `/jobs/batch` collapses them
into a single job, keeping the highest-priority source (`input_priority`; first
wins a tie). Anything that records *per source* has to reach the same answer or
it describes a job that was never created: the RomM metadata snapshot was
written for whichever source came last, so the conversion that ran could be
re-pinned with the skipped ROM's identity. Both the batch route and the re-pin
plan call the same helper.

#### A row is keyed by its destination, and owned by its source

The dedupe key is `output_path`, which is right — one destination is produced
once, so re-recording *supersedes* rather than stacking, and re-submitting a
batch is harmless. But the key alone cannot say **which conversion** a row
belongs to, and that is a different question with a real answer.

It matters when a planned batch is not the one the queue accepts. Two clients
can plan different sources onto one destination; the second `record()`
supersedes the first's row, and if the *first* then wins job creation, the
second's failed submit tidies up. Retiring its row would take the accepted
conversion's only remaining snapshot with it — which is why the cancel route
keeps a row a queued job is writing to. Keeping it on *those* grounds was its
own bug: the surviving row holds the second source's provider ids while the
queued job converts the first, so the settle pass hashes one game's output and
stamps the other game's identity onto it, confidently, with nothing to notice.

So the row records `source_path` (migration `0007`), and the test is
`_destination_job_matches_source`: keep the row only when a queued job carries
**both** this destination and this source — `get_active_job_candidates` reports
a job's input and output together, so one job holding both is this row's
conversion. `_destination_has_pending_job` remains for the weaker question,
which still has uses ("may I treat what is on disk as settled"); the two are
distinct and both are needed.

A row with no `source_path` — written before the column existed — cannot answer
and answers **no**. Retiring it costs a manual re-match; keeping it can cost a
game its identity, which is the whole failure being removed. The column rides
through the unique-index retry path too, or a row that lost that race would come
back unable to prove ownership.

### 3.3.9 Re-pin queue and conversion provenance (`services/romm/repin.py`)

Two questions the filesystem cannot answer on its own, both owned here so the
manual submit (`routes/romm.py`) and the unattended sweep (`services/rules/sweep.py`)
give the same answer.

**"Has the conversion actually produced this output yet?"** A re-pin row is
written *before* the conversion runs — the source's provider ids are only
readable while the source is still the file RomM knows about — and under the
`overwrite` policy the destination is occupied at that moment by definition. So
`record()` also stores **`pre_fingerprint`** (`"size:mtime_ns"`, or `""` when the
path was free) and the settle pass treats an unchanged destination as "not yet".
Without it, a batch that was planned and then rejected leaves a row that hashes
the *previous* artifact and pushes this ROM's ids onto whatever RomM identifies
that as. `cancel(row_ids)` retires such rows immediately rather than waiting out
`repin_abandon_days`; the fingerprint is what makes leaving them safe until then.

Cancelling is keyed by **row id**, which is why `record()` returns one and
`/romm/repin/plan` answers with `recorded_ids` beside `recorded_paths`. A
destination names whichever row holds it *now*: `record()` supersedes, so a
second client planning the same output between this caller's plan and its
cancel owns that path — and cancelling by path would retire the live row its
queued conversion needs. The same asymmetry governs `release()`: a claimed row
is `settling`, which the partial unique index does not see, so a re-plan can
insert a new `pending` row for the same output while the claim is out.
Restoring the claim to `pending` then violates
`ux_romm_repin_pending_output` — inside the failure handler that called
`release`, replacing the real error with a 500 and stranding the claim until it
ages out — so `release()` retires a superseded claim instead, and returns
whether the row went back to `pending`.

**Who may supersede a pending row (ownership and generation, #337).**
`record()` supersedes the destination's pending row only when the caller owns
the right to. Two rules, both checked inside `record()`'s own transaction, and
on the unique-index retry too:

- **Generation.** `plan_generation()` is a process-wide counter, seeded from
  the table's `max(plan_generation)` so a restart never issues an older one.
  `/romm/repin/plan` takes one **before its first await** and stamps it on
  every row it writes (`romm_repin.plan_generation`). A row with a higher
  generation is never superseded, so the order that counts is the order plans
  *started*, not the order their writes landed: a plan stalled in its probes,
  or abandoned by an aborted Auto run, loses to a run that began after it
  however late its write arrives. A run plans one request per target mode;
  the response's `generation` goes back on the run's later requests (the
  request's `generation` field), so the whole run is as old as its first
  request. A claimed generation is capped at the newest one issued. NULL (rows
  written before the column existed) reads as the oldest. Callers that record
  the moment their own job is queued (the automation path) and the
  re-record of a redirected output omit it and get a fresh one.
- **Live job.** With `owned_by_live_job` given, a row a queued or running job
  is converting (its own source into this destination) is never superseded
  either, whatever its age. The plan route passes `owned_by_job`, pure string
  work, so nothing stats under `_settle_lock`; the resolving form
  (`_destination_job_matches_source`, the same function with
  `spell=_resolved`) is what `/romm/repin/cancel` uses through `_probe`.

A refused record returns `0` and writes nothing. The plan route also asks
`_destination_has_pending_job` again, bounded, inside `_settle_lock` right
before each write: its first check ran before the probes, and the queue does
not take that lock, so the recheck narrows the window and `record()` closes
it. Without these, the late write superseded the newer run's row, that run's
submit or the abandoned run's `retireLateSnapshot()` retired the replacement,
and the queued conversion finished with no metadata. The store side
(`ConversionStore.#planRepin`) keeps to the same order: it carries the run's
generation on every group and sends no further group once the run's signal
aborts.

`retire_all_pending()` (the identity swap) covers `settling` as well as
`pending` for the same reason `claim` re-issues a stale one: a claim whose
holder died mid-write comes back after `_CLAIM_STALE_SECONDS`, and a row left
behind by the swap would then apply the *old* instance's provider ids to
whatever the new one matches its digest to.

Every remaining filesystem check on these paths is bounded the same way:
`normalize_rules_bounded` (the save-time output-directory validation, inside
`_sweep_lock`), `_output_dir_problem` (the sweep's re-check of a stored
directory), and the re-pin plan's batch resolve, which is scaled by batch size
like the catalog scan. Each degrades toward *refuse*: an output directory
nobody can resolve pauses its rule or skips its platform, and a submitted path
that does not answer is skipped — the same answer a path outside the volumes
gets, which is what an unreachable one effectively is.

The change and its cleanup **commit together or not at all**. They are two
operations and either can be the one that survives a crash: cleanup-then-save
leaves the old identity live with its history gone and its snapshots retired,
while save-then-cleanup leaves the retry comparing the new values with
themselves — concluding nothing moved, and leaving the previous instance's ids
live against the new one forever. So `romm_settings.save(patch,
cleanup_pending=True)` writes the marker in the *same row* as the new identity,
`_run_identity_cleanup()` clears it only once the work is done, and
`replay_identity_cleanup()` finishes an interrupted change at startup. Both
halves of the cleanup are idempotent, so replaying costs nothing.

Startup is not the only replay point: the settings route runs the cleanup when
`changed` **or** `romm_settings.cleanup_owed()`. Retrying the same save
otherwise finds the new identity already cached, computes no change, and
returns success while the marker still says the old instance's records are
live.

The re-pin **plan** reads the catalog and then writes rows carrying that
catalog's provider ids, so it has to be sure the catalog is still the one in
use. `romm_settings.identity_generation()` is read up front and re-checked
before recording: a change landing in that window means the ids belong to an
instance nobody is pointed at, and the plan answers 409 rather than recording
them. A batch converted without a snapshot needs a manual re-match, which is
recoverable; a row holding another library's ids is not.

A re-check is not a guarantee, though — it is true at an instant, and the
inserts come after it. A save landing in between could install the new identity
*and finish its cleanup* while the rows were still going in, leaving them alive
against the new library, holding the old one's ids, and invisible to the pass
that was supposed to retire them. So the plan is split: the fingerprint probes
run unlocked (they are bounded filesystem reads, and holding a lock the settler
and the settings route both need across a dead mount, once per destination,
would trade this race for a much longer stall), and **only the inserts run
under `_settle_lock`** — the lock `_run_identity_cleanup` requires — with the
generation re-read inside it. That is what makes "the identity has not moved"
true *for the duration of* the writes.

**And while a cleanup is owed, nothing acts on the records it has not reached
yet.** The marker can outlive startup: `replay_identity_cleanup()` is
best-effort, a briefly locked SQLite file is enough to fail it, and the app
starts anyway by design. Until it lands, the conversion history and the pending
re-pin rows still belong to the previous instance — so `sweep()` refuses
(`identity_cleanup_pending`) rather than reading the old library's history, the
plan route answers 409 rather than adding a row the cleanup has already passed
over, and the settler skips its tick. The settler is also where the **retry**
lives: it takes `_settle_lock` and the sweep pause, runs the cleanup, and only
then proceeds. A sweep cannot retry it — it holds `_sweep_lock` and so is the
one caller that cannot take those — which is why the refusal and the retry sit
in different places.

Deciding *whether* the identity moved is itself bounded (`_identity_moved`),
because it resolves both library roots and the old one is the unresponsive
mount as often as not — that is why the operator is changing it. It runs
detached under `_IDENTITY_PROBE_SECONDS` and falls back to comparing the
normalised spellings, while the request holds `_settle_lock` and the sweep
pause. The fallback errs toward *moved*: two spellings of one directory then
cost a redone conversion history, where the opposite mistake writes one
library's provider ids onto another library's game.

**"Has this rule already converted this source?"** `skip` is idempotent from the
destination alone, but the other two policies are not: `overwrite` resolves an
occupied destination as queueable, and `rename` always finds a free suffix. Both
would therefore reconvert the whole library every interval.

What is recorded is **production, not queueing**. `converted` on the platform's
`rules.state` entry (key `romm:<platform id>`, see §3.3.11) maps each Image key
(for RomM, the ROM id) to `{path, pre}` — the destination the
rule chose and a fingerprint of whatever occupied it at planning time — and
`_was_produced()` answers it in three steps, strongest evidence first: the
verdict written down when the job ended (`done`), then the job's live status if
the queue still remembers it, then — only for a record predating either — the
fingerprint having changed.

The written-down verdict is what makes the rest trustworthy. A *failed*
`overwrite` job changes the destination just as visibly as a successful one, by
unlinking the old artifact or leaving a partial file, so the fingerprint alone
cannot tell them apart and the ROM would be skipped by every later sweep until
**Forget history**. `job_manager.add_terminal_listener` (§3.4) hands
`note_job_finished` the outcome at the moment it is certain; the sweep also
freezes any outcome the queue can still answer for, which covers a job that
finished before its record existed. Neither has to be kept in sync with the
queue: a missing verdict degrades to the older evidence rather than to a wrong
answer.

It is invalidated where it stops being true: `set_rules` drops it for any
platform whose `OUTPUT_IDENTITY_FIELDS` changed — a rule retargeted from RVZ to
GCZ is asking for a different file — and does so **under `_sweep_lock`**, since
a sweep running concurrently finishes by writing its own ids and schedule stamp
under the same key. `routes/romm.py` also clears it when `url` or `library_root`
changes, because a different RomM database reuses the same integer ids for
different games. `forget_converted()` (exposed as
`POST /api/romm/rules/forget-converted`, with a per-platform button in the
automation editor) is the operator's escape hatch after restoring a backup.
`_record_run` merges into the entry rather than replacing it, or every run would
forget the history it shares a key with.

**"What did the tool actually produce?"** A row also stores its `mode`, so the
settle pass can ask the owning tool through `companion_outputs`. makeps3iso's
`-s` build only splits past 4 GB — under it, the bare `.iso` appears and the row
settles normally; over it, only `<name>.iso.0`, `.1`, … exist, which from the
recorded path alone is indistinguishable from a conversion that never ran. RomM
matches a ROM on one file's hash, so a part set has no digest to join on: the
row is retired immediately with that as its reason rather than waiting out
`repin_abandon_days` and reporting "output never appeared".

#### Hasheous provider ids (#427)

`hasheous._normalize` keeps each *Mapped* metadata entry's id as a named field
in `provider_ids`, under RomM's own field names (`hasheous.PROVIDER_ID_FIELDS`
maps a Hasheous source to a `METADATA_ID_FIELDS` name; the record's own id is
`hasheous_id`). With the RomM Library's `hasheous_write_ids` switch on (off by
default, and only while `repin_enabled` is on: `repin.hasheous_write_on`),
every place that records a row also records the ids the source's cached
Hasheous hit carries and the ROM lacks (`repin.hasheous_fills`, one cache read
per batch, never a hash), under `metadata_ids[FILL_KEY]`. The rest of
`metadata_ids` is RomM's own ids, as before. The three recorders share it: the
plan route, the sweep's `after_queue` (which takes its plan generation before
awaiting the lookup, so a manual plan recorded meanwhile stays the newer), and
the swap Referrer's fallback re-pin (which does the bounded lookup before its
never-abandoned record step, and records RomM's own ids alone on a timeout;
it checks RomM's identity and binding again after that await, with none
between the check and counting the swap, and records nothing if they moved).
Those ids are recorded whatever the output format, a CHD
included, since RomM never matched these ROMs; RomM's own ids are still
carried only across a format that loses its match.

At settle, `repin.ids_to_write(stored, rom)` is what reaches
`update_rom_metadata`: the carried ids, and the fill ids only where RomM's
record for the output has none (`fill_to_write`), so a match RomM made is
never replaced, and none at all while the switch is off: a row recorded while
it was on still settles, with RomM's carried ids only. A row expected to
self-identify that did writes only the fill.
`settle_swapped` keeps a row holding fill ids pending with only those, since
they were never on the kept ROM; the next settle pass writes them there.
`FILL_KEY` is not a provider field, so nothing that sends ids to RomM can pass
it on, and no schema change was needed.

The fill lookup resolves each source on its mount, so the plan route and
`after_queue` call it through `repin.hasheous_fills`, which runs the blocking
read on a detached thread under `FILL_LOOKUP_SECONDS` and records no ids when
it doesn't answer in time. A row for a CHD or archive output (one only the
Hasheous ids put there) is settled by the path join, as for a Platform RomM
never hashes (`repin.joins_on_path`): RomM stores the digest of what is inside
those files, so the output's own SHA-1 never finds its ROM.

A Hasheous hit cached before the normaliser kept `provider_ids` has none to
write. It still Recognizes its file everywhere, but while the ids are wanted
and Hasheous is on, `routes.dat.cached_result_needs_refresh` has
`/dat/match-batch/job` look it up again (the row is still served meanwhile).
The rule is decided on read, like `is_unrankable_local_hit`; no row is dropped.

#### The RomM ROM listing (#427)

`GET /romm/roms` gains `unidentified` (only the ROMs `rom_identified` says
RomM couldn't identify, decided before any ROM is stat'ed). Each row carries
`romm_identified` and `dat_match`, the app's usable cached match
(`cached_matches`, one read in the same worker as the stats), and the listing
carries `romm` (`RommListingSummary`: `unidentified`, `recognized`,
`recognized_unidentified`). It never hashes; "Recognize these" in the view
posts the rows with no answer (on every page) to `/dat/match-batch/job`.
Which rows count is `util/datMatchable.isDatMatchable`, the same rule the file
list's match hydration uses: no folders, no folded split sets, no archives (a
zip or 7z would be hashed as the container, which no DAT lists; RomM lists
them as plain files, so the extension decides), and an extension the registry
lets a user filter by. The view counts the archived rows apart and says they
aren't recognized yet.

In the browser the rows' matches are seeded into the DAT match store
(`datMatching.seedListing`), only while the matching-policy generation is the
one the listing was loaded under, and the views read the store alone, so a
policy change clears them with every other badge. `startMatchJob` remembers
each job's full path set and `hydrateFinishedJobs` (an effect in the RomM view,
and once more right after "Recognize these" learns the job's id, in case it
already ended) reads all of it back once the job ends, not only the rows on
screen. The job stays tracked until that read answers; a failed read is
retried a bounded number of times, and when every try fails the job stays
tracked, with no loop of its own, for the next call to read again.

Every read of the match cache, a lookup (`hydrate`) or a RomM listing, takes a
place in one read order when it is requested (`_hydrateSeq`; a listing's comes
from `beginListingRead`), and its answer writes a path only if no newer answer
has written that path (`_claimPaths`, per path, never per map). A lookup
reconciles every path it is the newest for: written when the answer has it,
cleared when it doesn't. So a full job's read is not lost to a page's read
that answered first, a refreshed listing's newer hit replaces a cached miss, a
listing row with no match clears a stale one, and an older answer never undoes
a newer one. `matchBatch` and `startMatchJob` take their place when they are
asked, like a lookup. Which rows want a match is `util/datMatchable`:
`needsDatMatch(match, wantsHasheousIds(settings))`, where wanting the ids
needs re-pinning on too, as the backend's gate does.

### 3.3.9 Verify without delete (`ConversionJob.verify_after`)

`delete_on_verify` verifies as a *precondition* for deleting the source. A
caller that wants the check on its own — an unattended sweep proving its output
before it walks away — sets **`verify_after`** instead. One block in
`_process_job` runs the verification for either flag, so a verified output means
the same thing however it was requested, and only `delete_on_verify` reaches the
delete half.

The capability gate applies to the delete, not to the check. `verify_after`
simply runs the tool's verification and keeps the source, so any mode whose tool
can verify may ask for it. `delete_on_verify` must additionally satisfy
**both** `ModeSpec.supports_delete_on_verify` ("can this mode's output be
verified at all") and `ToolPlugin.delete_on_verify_is_safe(mode, compression)`
("can *this* job's settings prove it") — re-asked in `_process_job` itself,
because that is where the source is unlinked and a job can arrive there without
having passed either plan-time check.

**A mode that keeps its Source on principle says why.** `ModeSpec.source_kept_reason`
(mirrored on `ChainSpec`, and as `sourceKeptReason` on the frontend mode entry,
which `tests/test_frontend_parity_186.py` compares) is the user-facing reason for
a mode whose `supports_delete_on_verify` is False not for want of a check but
because deleting would lose something nothing can rebuild. rom-converto's
`zar_compress` (#422) is the first: a ZAR holds the disc's files, not the disc,
and v0.24.0 can't turn one back into an ISO, so there is no Round-trip against
the Source. The mode contract is the only switch: no caller branches on the tool.
It is read in four places, each falling back to its old wording when the reason
is empty:

- `routes/convert.py`: the 400 that refuses delete-on-verify (single, batch and
  `/jobs/delete-plan`) carries the reason (`_delete_on_verify_unsupported_detail`).
- `app/cli.py`: `--delete-source` refused at parse time, and each kept Source's
  "Source kept: ..." line in the plan.
- The convert panel shows "Sources are kept." and the reason where the delete
  checkbox would be; the Rule editor appends it to the verify checkbox's text.

A mode with a reason never sets `supports_delete_on_verify`
(`tests/test_rom_converto.py` checks every registered mode).

### 3.3.10 The Ledger (`services/ledger.py`)

`ledger.record` is a `JobManager.add_terminal_listener` callback (registered in
`main.py` beside the RomM one), so every runner outcome writes one `ledger` row
(`db.LedgerEntry`, migration `0009`) while the job object still says how it
ended. The job carries what the row needs and the in-memory history would lose:

- `ConversionJob.input_bytes` -- size of the whole Source set, read when the
  job starts, before anything can be deleted: a directory tree, or every file
  `utils.delete_plan.build_delete_plan` would remove (CUE/GDI tracks, registry
  source companions, the archive holding a member).
- `ConversionJob.check_passed` -- `"intact"` once the tool's own verify passed,
  `"round_tripped"` once the Round-trip check passed too (§3.3.12).
- `ConversionJob.deleted_paths` -- each path the delete-on-verify step removed;
  `source_deleted` is derived from it.
- `ConversionJob.platform` -- the Platform the row is filed under (#346). A
  Rule's job takes its Library's Platform for the Image (the sweep sets it as
  the batch is queued); any other job (file browser, Auto, the CLI) asks the
  shared `platform_detect.detect_platform` (§3.3.14, with its cache) when it
  starts, before anything can delete the Source, on a detached thread with the
  Source sizing's bound (`JobManager._detect_platform_bounded`). None when
  detection can't place it or does not answer in time.
- `ConversionJob.extra_inputs` -- inputs besides the Source that shaped the
  output, each `{"kind", "path", "sha1"}` (#428, migration `0026`, column
  `extra_inputs`, `[]` for every older row). A patch job (3.3.24) lists its
  patch file. The CSV writes each as `<path> (sha1 <hex>)`.

`tool` comes from `registry.for_mode`; `platform` is the job's own, else the
mode's when it serves exactly one Platform slug, else empty, which the savings
breakdown shows as "unsorted". Rows written before #346 keep what they had (no
backfill: their Sources may be gone). `referrers_updated`
comes from `ConversionJob.referrers_updated` (see 3.3.15). `library_id` and
`image_key` are written empty for later tickets to fill.
Savings count only completed rows whose Source was deleted, and can be
negative (an RVZ replaced by its larger ISO). A job announced terminal twice
(a queued cancel) writes one row: `(job_id, finished_at)` is unique and the
insert is `ON CONFLICT DO NOTHING`. The dashboard reads the Ledger through
`stores/ledger.svelte.js`, refreshed on each terminal job and once more after
a short settle delay, since the row is written after the completion event. A `retention_days`
in the `ledger` preference prunes older rows on each write; unset keeps them
forever. Recording never raises: a failure is logged and the job is unaffected.

Reads all go through `services/ledger.py` so the routes stay thin. A
`LedgerFilter` (path substring on Source or output, status, tool, Platform)
builds one set of SQL clauses used by `list_page` (newest first, with a total),
by `export_csv` (the same filter, keyset-paged on `(finished_at, id)` inside one
explicit read transaction held for the stream, so every page reads the same SQLite
snapshot and a job finishing or a prune mid-export neither repeats, skips nor drops
a row) and by `facets` (distinct values for the filter
menus). The Prometheus counters are not computed from the `ledger` table: the
`ledger_totals` table (migration `0011`, seeded from the Ledger) holds per-status
job counts and completed-job byte sums, bumped in the same transaction as each
new row and never decremented, so retention cannot lower a counter. `q` is escaped, so
`%` and `_` match literally. CSV cells starting with `=`, `+`, `-`, `@`, tab or
CR get a leading `'` so a spreadsheet doesn't run them. `metrics_text` renders the
Prometheus exposition (jobs by status, bytes of completed jobs, plus the live
`job_manager.get_queue_depth()` passed in by the route). It sits under `/api`,
so the auth middleware covers it with no exemption.

### 3.3.11 The Rule engine and the Library interface (`services/rules/`)

A Rule converts the Images of one Platform in one Library. The engine that runs
Rules is shared; where the Images come from is behind a `Library`
(`services/rules/library.py`). RomM is the first Library
(`services/romm/library.py`, `romm_library`). A folder Library (#303,
`services/libraries/folder.py`) is the second: one root folder inside a Volume.

| Module | Holds |
| --- | --- |
| `rules/rules.py` | Rule schema, `default_rule`, `normalize_rule(s)`, filter-pattern checks, `OUTPUT_IDENTITY_FIELDS` |
| `rules/schedule.py` | `_in_window` (weekday mask, time window, time zone), `_is_due` (interval) |
| `rules/store.py` | the `rules` and `rules.state` preference rows, keyed `"<library id>:<platform id>"`, the legacy-key move, the one-time upgrade to Round-trip and the notice it leaves |
| `rules/sweep.py` | `get_rules` / `set_rules` / `get_state` / `forget_converted`, the sweep, the converted history, `note_job_finished`, `run_forever` |
| `rules/library.py` | `Library`, `Image`, `Platform`, `QueuedImage`, `LibraryUnreadable`, `Placement`, `PlacementUnknown`, `placed_in`, capability names, `duplicate_actions(library)` |

**The interface.** A Library has an `id` (`"romm"`; a folder Library uses
`"folder:<row id>"`, the row in `folder_libraries`, so a path with a `:` in it
cannot reach a stored key), `capabilities`, `kind`, `name` and `root` (for the
API and the Rules view; the engine never reads those three), and:

- `platforms()` and `images(platform_id)`, both blocking (the sweep calls them
  in a worker thread). `images` raises `LibraryUnreadable`, which the sweep
  reports as `unreadable` for that Platform and moves on.
- `Image`: `key` (RomM: the ROM id; folder: the path relative to the root),
  `name` (file name, what name filters match), `title` (what the `name` order
  sorts by), `size`, `added_at` (any number that grows as Images are added;
  RomM uses the ROM id, so `newest` keeps its order), `recognized` (True,
  False, or None when unknown; see below), `identified` (the Library's own
  verdict; defaults to `recognized`), `platform` (the console slug checked against
  `registry.mode_allows_platform`), `path`, and `record` (the Library's own
  record, for its hooks). `path` may be lazy: RomM passes `resolve_path`
  because mapping a record onto the mount stats it, and the sweep reads
  `path` only inside the bounded candidate probe.
- `canonical_platform_id(raw)` validates a stored or submitted Rule key
  (RomM: an integer, written back as a canonical string), and
  `platform_value(id)` is how the API reports it and how Rules sort (RomM: the
  integer, so summaries and errors still say `"platform_id": 7`).
- `rule_defaults()`, `max_per_sweep()`, `schedule_enabled()`: the Library's
  settings. RomM reads them from its in-app settings, as before.
- `configured()`: when false the sweep stops with `not_configured`.
- `legacy_preference_keys`: rows an earlier release used for this Library's
  Rules and state, unprefixed.

**Hooks for steps only one Library performs.**

RomM's connection helpers live in `services/romm/connect.py` (#532). Discovery and pairing ask RomM before it has a token
of ours through `RommClient(base_url, anonymous=True)`, which sends no token
and never falls back to the saved one:
`discover()` (the heartbeat at RomM's usual addresses, bounded, no token
sent), `find_library_root(FS_PLATFORMS, before=...)` (a bounded breadth-first
look through the Volumes for the folder holding RomM's platform folders under
the declared prefix *before*, `games` for `games/{platform}/{game}`, or RomM's
default layouts when no structure is known; the connection test withholds its
suggestion when the token could read the structure and the read failed), and
the layout cache (`refresh_layout()`; `structure()` and
`excluded()` / `excluded_extension()` only read it). The layout is re-read at
most every five minutes, by `/api/romm/status`, every sweep and Inbox poll
(the Library's `refresh()` hook, below), and forced by a connection test and a
Save that changes the connection. The two parts, the exclusions
(`/api/config`) and the structure (`/api/setup/library`), are read, timed
and known separately: a part RomM didn't answer is retried after
`_LAYOUT_RETRY` (30 s) without re-reading the other, which keeps its full
TTL (a 404 for the structure is an answer: RomM before 5.3 has none). Until
a part has ever been read for the configured RomM, its emptiness means "not
known": `RommLibrary.sweep_blocked()` defers the sweep with
`romm_layout_unread` until `exclusions_known()`, and placement with no
declared structure raises `LibraryUnreadable` until `structure_known()`:
retryable, so an Inbox leaves the file for a later poll rather than holding
it as it would on `PlacementUnknown`. `structure()`, `structure_known()` and
`layout()` take an optional base URL, for the RomM a connection test probes.
RomM placement uses the declared structure when there is one, and refuses a
structure with a wildcard level (`{category}`); `platform_folder()` looks
under the same declared prefix (`games/{platform}/{game}` gives
`<root>/games/<platform>`), with `_declared`'s `roms` rule. A pairing code
(`POST /api/romm/pair`) saves nothing: the token it is traded for is held in
memory for that URL, and a connection test
or Save sent with `paired: true` uses it (400 when none is held for the URL it
names), so the token, URL and Library path are committed by one Save.

- `sweep_blocked()` returns an error code to refuse the sweep. RomM refuses
  with `identity_cleanup_pending` while a change of instance is half cleaned up.
- `excluded(name)` (#532) says the Library ignores a file or game folder by
  that name. The sweep skips such an Image, and one whose output file would
  have such a name (both counted in `skipped_filtered`).
  `excluded_extension(ext)` says it ignores every `.<ext>` file, by extension
  alone; the sweep skips a Rule whose output extension that is (error
  `output_excluded`, skip check `output_excluded`). A name pattern is never
  read as a whole extension. A retried kept delete (#329) asks `excluded()`
  of the Source's name first and leaves an excluded Source, and its pending
  entry, alone; a delete already `deleting` finishes. Both in memory, never
  blocking.
  `LibraryDefaults` answers False; RomM answers from its `exclude:` settings
  (`EXCLUDED_SINGLE_EXT`, `EXCLUDED_SINGLE_FILES`, `EXCLUDED_MULTI_FILES` and
  RomM's defaults, from `GET /api/config`), cached by
  `services/romm/connect.py`.
- `refresh()` (#532) brings what the Library caches about itself up to date,
  bounded and never raising. The sweep awaits it before taking its lock, an
  Inbox poll before placing, and the placements listing before it lists, so
  the scheduler and `run-rules` read RomM's layout without the RomM page
  open. `LibraryDefaults` does nothing; RomM
  calls `refresh_layout()` (within its five-minute TTL). The suite's conftest
  turns RomM's off, and reads both layout parts as known, unless a test module sets
  `READS_ROMM_LAYOUT = True`.
- `summary_fields()` seeds every per-Platform summary and the result (RomM:
  `repins_recorded: 0`).
- `after_queue(rule, spec, queued, summary, result)` runs once a Platform's
  batch is accepted by the queue, inside the same error handling. RomM records
  the metadata snapshot for re-pin here, so the re-pin bookkeeping stays in
  `services/romm`.
- `sweep_extras()` adds keys when a sweep ends (RomM: `pending_repins`).
- `before_pass(*, dry_run)` is not called by the sweep: `run-rules` (§3.3.17)
  calls it before it sweeps the Library, for the upkeep the server does at
  startup or in its background loops. RomM loads its settings and, on a real
  pass, runs `routes.romm.replay_identity_cleanup()` and one
  `routes.romm.settle_once()` (the settler loop's tick, shared with
  `settle_forever`). A dry run must write nothing. `LibraryDefaults` gives the
  no-op, and every Library class inherits it (`FolderLibrary`, `RommLibrary`,
  which overrides it), so `run-rules` calls it on any Library without a check.
- `after_intake(placed)` (#433) is not called by the sweep either: an Inbox
  pass (§3.3.25, `services.inbox.report.after_pass`) calls it once per pass for
  each Library its Inboxes placed Images in, with what was placed
  (`{"path", "platform", "output"}` each). It returns what it did for the pass
  summary (`{"status", "folders", "reason"}`) or None, and never raises.
  `LibraryDefaults` gives None (a folder Library has no one to tell). RomM
  resolves each output's Platform folder (`platform_folder`, its basename is
  RomM's `fs_slug`, in either RomM layout) on a detached thread under a bound,
  then asks for one quick scan of just those folders
  (`RommClient.scan_platforms`). It is asked once and never retried: a 403
  (the token lacks `tasks.run`) is `missing_scope`, anything else RomM refuses
  (409 a library scan already queued or running, 503 no scan worker, no
  answer) is `not_asked` with a reason worded from the status
  (`romm.library.scan_refusal`), never RomM's body.
- `walk_generation()` and `note_paths_changed(paths)` (#353), with
  `LibraryDefaults` giving `0` and a no-op for a Library that lists live.
  `services.libraries.note_job_finished`, attached to the job manager by the
  server's lifespan and the CLI's job runner (`libraries.attach`, like
  `notifications.attach`), hands every Library the paths a finished job
  changed (its output and its Source), on the event loop and from memory
  only. An externally-managed job (`is_external_mode`: a check run, a
  metadata scan, a DAT match) writes no Library file and is skipped, so a
  check job's own end never makes its run look stale (#360). A folder Library
  whose root holds one bumps a files counter, so the next listing walks again
  instead of serving the kept walk. The sweep reads `walk_generation()` before
  it lists a Platform and carries that number with the run to `_record_run`,
  the one place every run is recorded, which compares it again
  (`_listing_current`) at the moment of recording. For a conversion Rule that
  is the end of its tick; for a check Rule it is when its check job ends
  (`check_run_ended` -> `record_check_runs`), so conversions that finish while
  the job checks count too (#360). A run whose listing went out of date in
  between is not recorded (`stale_listing` in its summary), so its Rule stays
  due and the next tick lists the new Images. The same rule for conversion and
  check Rules.

**What the sanitizer changed is logged by the sweep, not on read (#477).**
`normalize_rule` runs on every read of the Rules (each GET, the editor,
startup, each scheduler tick), so it logs nothing. It records what it dropped
or refused on the Rule (`dropped_codecs`, `dropped_timezone`,
`unsafe_delete_on_verify`, `invalid_output_dir`, `invalid_pattern`);
`rules.rule_changes(rule)` puts those in words, and `rules.refusal(raw,
kind=)` says why it refuses a Rule outright (a mode no tool provides, or one
`mode_is_automatable` refuses; a Rule with no mode is an empty row and gets
None). Three helpers in `rules/sweep.py` log them, and a fourth logs the
Rules the run loop skips:

- `report_rule_changes(library, platform, rule, kind=, prefix=, seen=)` logs
  one line for a Rule about to run. The sweep calls it once per Rule it runs
  (after the paused, due and active-check gates, for both kinds, and after
  the skip checks below too: for a conversion Rule once it passes them all,
  for a check Rule from `_sweep_check` once it has listed its Images and
  picked one or found no verifier missing), so any Library logs it once per
  sweep that runs the Rule, and a Rule skipped every tick doesn't repeat it. An Inbox calls it as it converts under a
  Rule (`intake._take`), with `prefix="inbox"` and `seen` the pass's
  `_State.rules_reported`, so once per pass.
- `_report_unrunnable(library, stored, rules)` runs in each sweep right after
  the Rules are read (`_read_rules` gives the stored blob and the normalized
  set, so the store is read once), before the `no_rules` and
  `not_configured` returns. A Rule the sanitizer refused, or paused for a
  value it refused, is never reached by the run loop, so it is logged here:
  once per `(library id, kind, platform id)` and problem
  (`_reported_unrunnable`), again only when the problem changes, forgotten
  once fixed and in `forget_library`.
- `_report_saved(library, rules, kind)` runs at the end of `set_rules` and
  `set_check_rules`. A save stores each Rule as the sanitizer left it
  (`timezone: "UTC"`, delete-on-verify off, no output folder, a lenient save's
  codecs dropped), so re-reading it finds nothing to drop and the recorded
  field is gone before a sweep could see it. It re-normalizes each Rule as
  stored and logs, once, only the `rule_changes` that re-read loses. What
  survives the round trip is left to the sweep, so nothing is said twice:
  `invalid_pattern` is carried from the stored Rule, so a refused filter is
  reported by `_report_unrunnable`. The editor's own save (`strict=True`)
  still refuses a codec the mode can't use (#474) before anything is stored.
  It compares changes, not fields, so a field added to `rule_changes` later is
  covered without touching it.
- `_report_skip(library, kind, platform_id, check, reason)` logs a Rule the
  run loop skips (#480). Before it queues anything for a conversion Rule the
  loop makes the checks in `_SKIP_CHECKS`, in order: `tool` (its tool is
  installed, `_ready_bounded`), `output_dir` (its output folder is inside a
  Volume, `_output_dir_problem`, which also covers a Volume that doesn't
  answer within `_VOLUME_PROBE_SECONDS`) and, once the Platform is listed,
  `platform` (`registry.mode_allows_platform` for the Images' slug). A Rule
  that fails one is skipped with its `result["errors"]` entry
  (`tool_not_ready`, `output_dir_outside_volumes`, `tool_wrong_for_platform`)
  and no run recorded, so it is due on the next tick and runs as soon as the
  cause is fixed. A check Rule uses the `tool` check the same way:
  `_sweep_check` reports the verifiers it found not ready (still adding
  `tool_not_ready`, and recording no run when it picked nothing), and a pass
  that reached its Images without finding one missing clears it. The `tool`
  reason is the plugin's own (#484), from `not_ready_message` (§3.4, "Saying
  why a tool can't run"): `"<display_name> isn't ready: <not_ready_reason>"`
  for a conversion Rule, and for a check Rule the same with
  `verify_not_ready_reason` for each verifier not ready, sorted by tool id
  and joined with `"; "`. It is read after the readiness check, never before,
  since a plugin may learn its reason while it probes (rom-converto). So an
  nsz Rule with no `prod.keys` says so rather than "not installed", and a
  reason that changes while the tool stays not ready is a new reason, logged
  once more. The loop calls `_report_skip` with the reason on a failure
  and with `reason=None` on a pass. The reason is logged once per
  `(library id, kind, platform id)` (`_reported_skips`, which keeps the check
  and the reason), and again only when it changes. A pass forgets a reason
  from that check or an earlier one, so a cause that clears and comes back is
  logged again, while a Rule stopped every tick at `platform` keeps its memo
  through the earlier checks it passes. `_forget_skips` drops the memo of a
  Rule removed or paused by the operator, at the top of each sweep, and
  `forget_library` drops a removed Library's. A new skip check goes in
  `_SKIP_CHECKS` at its place in the loop and reports through `_report_skip`,
  never through a bare `logger` call, which would repeat on every tick.

Each line names the Library (`library.name`) and the Platform, and starts with
the Library's prefix, `rules.library.log_prefix(library)`: the Library's own
optional `log_prefix` (`RommLibrary` keeps `romm_auto`), else
`f"{kind}_library"` (`folder_library`), else `library` for a Library with
neither. It is optional on the contract, so a Library written before it still
logs. The logger is still `romm_auto`, so log filters keyed on it still match.
The same goes for every other line a sweep logs about one Library (#480): a
Platform that stops responding or can't be listed, sources dropped for an
unreadable delete snapshot, a batch that fails to queue or loses a
destination, a bounded probe that times out (`_probe` takes the `library` it
answers for), the scheduler's per-Library summary and failure. Those are real
failures, logged by each sweep that hits them, under `log_prefix(library)`
with `library.name` and the Platform. Only the scheduler's own lines (started,
couldn't list the Libraries, the tick's follow-up failed) keep `romm_auto:`.

Capabilities (`RECOGNIZED_FILTER`, `CONVERTED_HISTORY`, `FAILED_HOLD`) say
what a Library supports. Nothing in `rules/` asks what kind of Library it has.

- `CONVERTED_HISTORY`: `duplicate_actions(library)` is every policy with it
  and `("skip",)` without. A submission (`set_rules(..., strict=True)`, which
  both Rule APIs use through `rules.editor.save_rules`) raises
  `UnsupportedPolicy`, which the routes answer with 422
  (`duplicate_action_unsupported`); a stored blob falls back to `skip` in
  `normalize_rule`. `options.duplicate_actions` carries the same set, so the
  editor never offers a policy the Library cannot honour.
- `FAILED_HOLD`: when a job from this Library's sweep fails,
  `note_job_finished` stores the Image's fingerprint
  (`image_sets.image_fingerprint`: size and mtime of the file and every member,
  so a replaced track counts) under `rules.state[...]["failed"][image key]`;
  the candidate probe skips the Image (`skipped_failed`, in the result and in
  each Platform's summary) while the fingerprint is unchanged, and a later
  success drops the hold. Removing a Library bumps its generation
(`sweep._generations`); a job-finish callback captures it before its first
await and writes nothing if it changed, checked under `_sweep_lock`, so a
removal mid-callback cannot recreate the Library's state. A real run drops holds for Images the Library no
  longer lists. **Forget history** clears holds too; the editor shows the held
  count with **Retry failed** whatever the policy. RomM does not
  declare it, so a failed ROM is re-queued every sweep, as it always was;
  turning it on for RomM is adding the capability.

**Recognized filters.** `only_matched` takes Images whose `recognized` is
True, `only_unmatched` those whose `identified` is False. Unknown (None)
passes neither, so a Library that cannot say (nothing matched yet, or its
match cache could not be read) never has an `only_unmatched` Rule convert
everything. RomM always answers True or False for both.

**Bounded listing.** The sweep calls `library.images` through
`subprocess_runner.run_detached` (a daemon thread under the process-wide probe
cap, never the shared pool that conversions' `asyncio.to_thread` calls need)
under `asyncio.wait_for(..., _LISTING_SECONDS)` (600 s, RomM included). A
timeout reports `library_unresponsive` for that Platform and stops the sweep,
so a hung mount fails the sweep and releases `_sweep_lock`. `configured()` is
called on the event loop and must not touch the disk.

**Editor helpers** (`rules/editor.py`): `editor_payload(library)` (Rules,
state, defaults, options), `save_rules` (strict), `forget_converted`,
`sweep_now`. RomM's `/api/romm/rules` and `/api/romm/auto-convert/*` and the
Library routes are thin wrappers over them; `routes/rules.py` holds the shared
route pieces (422 mapping, the `platform_ids` reading where an empty list means
none). RomM's forget route still calls `romm_auto.forget_converted`, which is
the same engine call.

**What `recognized` and `identified` mean.** `recognized` says whether the
Image is Recognized, and `only_matched` reads it; `identified` is the
Library's own verdict, and `only_unmatched` reads it. Both come from the
match cache through one shared read, `routes.dat.cached_matches(paths)`: the
cached `dat_matches` row per path in one batched query (`get_matches_batch`),
kept only when `cached_result_usable` says it is still valid for the
configured sources. Listing never hashes.

A folder Library fills `recognized` with that row: True for a DAT or Hasheous
hit, False for a usable miss, None when nothing usable is cached; `identified`
is the same value.

RomM (#427) fills `identified` with RomM's "identified" status
(`romm.library.rom_identified`: `is_identified`, else any metadata-provider
id), a metadata match that is often by file name. `recognized` is True when
RomM identified the ROM **or** the app's own cached match for its file is a
hit (`romm.library.app_verdicts`), else False. So "only Recognized" includes
the ROMs RomM couldn't identify that a DAT or Hasheous knows, while "only ROMs
RomM could not identify" keeps reading RomM alone. `identified` needs no disk.
For an unidentified ROM, `recognized` is lazy (`Image(resolve_recognized=...)`,
like `resolve_path`): the first read maps the record onto the mount and reads
the cache for that one path. `_passes_filters` reads it only for an Image its
name and size filters kept, inside the bounded candidate probe, so a ROM those
filters exclude never touches a stalled mount.

**Storage.** Rules live in the `rules` preference row and run state in
`rules.state`, one entry per `"<library id>:<platform id>"`. Callers read and
write one Library's view keyed by bare Platform id. RomM's pre-#296 rows,
`romm.rules` and `romm.auto_state`, are copied under `romm:` keys on the first
read or write, and then `romm` is recorded in the `rules.migrated` row (written
last, so a crash mid-copy repeats it from the same rows). The old rows are
never written or deleted. They stay as a copy of the Rules and history at
upgrade time, so a downgrade still finds them, and anything a downgraded
release writes to them is ignored after the next upgrade. The first read can
therefore write, so a read-only or locked database fails that first request.

**Check level (#302).** Every Rule has `check_level`, `round_trip` (the
default, and the fallback for an unreadable value) or `container_only`
(`rules.CHECK_LEVELS`). It is part of the shared schema, so every Library has
it, and it is kept while delete-source is off. A sweep passes it to
`create_batch_jobs` as the job's existing `container_only` flag
(`rules.takes_container_only`: true only when the Rule deletes its Source and
says `container_only`), so `JobManager._delete_check` (§3.3.12) decides a
sweep's job and a manual one the same way. A delete-source Rule's
per-Platform sweep summary carries `check_level`, which is what Preview shows;
a Rule that keeps its Source gets no such key. It is the check that will run,
not the stored preference: `rules.effective_check_level` asks
`round_trip.round_trip_applies(mode, delete_on_verify=, container_only=)`, the
same helper `_delete_check` asks, so a stored `round_trip` on a mode with no
digest shows `container_only`, and a mode that gains a digest (#300) changes
both answers at once.

**The upgrade to Round-trip.** `store._upgrade_check_level` runs once, in
`_load` after the Library's legacy copy and under `store._lock`, before any
read or write. It sets `check_level: round_trip` on every entry of the
`rules` row with `delete_on_verify` on, for every Library, and then writes
`rules.check_level_upgrade` (`{upgraded_at, switched: [keys]}`) last, so a
crash before the marker repeats the pass with the same result. Rules saved
afterwards are never touched. RomM's pre-#296 rows predate the field too, so
`_copy_legacy` applies the same switch to the Rules it copies, and adds them
to `switched` when the pass already ran (another Library was read first).

**One-time notices.** `GET /api/rules/notices` returns
`{"check_level": {"show", "switched", "container_check"}}`. Before counting it
runs the legacy copy of each of the app's Libraries (`routes/rules._libraries`)
and the upgrade pass, so the first read sees Rules still under pre-#296 keys.
`switched` counts the switched Rules whose mode can Round-trip now (registry),
`container_check` the rest, which keep the container check until their tool
gains a digest. `show` is true while `switched` is not zero and nobody has
dismissed it.
`POST /api/rules/notices/check-level/dismiss` records the dismissal in the
`rules.notices` row. The routes are not RomM's, so a folder Library's editor
uses the same ones; the front end reads them through
`stores/ruleNotices.svelte.js`.

**Locks.** `_sweep_lock` is process-wide, not per Library: the active-source
snapshot and claimed destinations are shared by every sweep. `paused()` takes
it. `store._lock` serialises each read-modify-write of the shared rows, and is
always taken inside `_sweep_lock`, never around it.

**Where a sweep ends.** `sweep()` returns through one line after the lock is
released, with `result["dry_run"]` telling a preview from a run. That is the
place for anything that reports a finished sweep.

**RomM's binding.** `services/romm/auto.py` keeps the names and arguments the
RomM routes and `main.py` call (`get_rules()`, `set_rules(raw)`, `sweep(...)`,
`run_forever()`, ...) and passes `romm_library` to each, so the HTTP API did
not change. `run_forever(libraries)` takes a callable returning the Libraries to
sweep on each tick. `main.py` passes `services.libraries.all_libraries` (through
`services.libraries.run_forever`), so RomM and every folder Library whose
`schedule_enabled` is on are swept, then calls its `after_tick` once (the
Inbox poll and what runs after it, §3.3.25; its failure is logged, and a failed Library listing
still polls). `compressatorium run-rules` (§3.3.17) sweeps
the same `services.libraries.all_libraries()` list, so the server's scheduler
and a cron pass always see the same Libraries.

**Images from files** (`services/image_sets.py`). `group_images(paths)`
returns the Images among a set of files: a sheet owns the tracks it references
(`utils.delete_plan.sheet_track_paths`, the delete plan's own `.cue`/`.gdi` parsers
and containment rule), a tool's `source_companions` are owned by their primary
(jwud's split parts), and playlists (`.m3u`) are dropped. An owned file is never
an Image; a `.bin` no sheet references stays one. `image_members(path)` is the
per-file question, and `group_images` takes a `members` callable so a caller
can cache it. A track named in another case owns the listed file only when
`os.path.samefile` agrees (a case-insensitive share); on a case-sensitive
filesystem nothing is hidden.

**Unsafe sheets.** The converter opens every file a sheet names itself, so a
sheet is checked from its own text before any job runs:
`delete_plan.sheet_reference_problems` (the delete plan's cue/gdi parsers and
`utils.sheet_text` decoding) runs each reference through the one confined
resolver, `utils.sheet_text.resolve_track`, and reports one that is absolute,
leaves the sheet's folder, or resolves outside the trusted roots (a symlinked
track). `path_utils.source_companions_are_safe`, the gate `plan_job` and the
worker both call for every job (manual, batch, Auto, Rules), rejects such a
sheet whatever the tool's companion list says; an empty list never means safe.
Its companions go through the same resolver. The trusted roots are the
resolver's `within`: the configured Volumes by default, and for an archive
member the worker passes `[the folder it extracted the sheet and its tracks
into]` (its own `mkdtemp`, not a Volume), so every track and companion must
resolve inside that folder. An empty root list is refused, never read as
anything goes.
The sweep asks the same gate per candidate and reports the Image as unsafe
(`skipped_unsafe`, and each Platform's `unsafe` names) instead of queueing it.

**The folder Library** (`services/libraries/folder.py`). One walk of the root
(`os.walk`, symlinks not followed, symlinked files skipped) collects every
regular, readable file whose extension a tool reads or writes
(`registry.convertible_extensions` plus `output_extensions`), groups them with
`group_images`, and detects each Image's Platform with
`platform_detect.detect_uncached`. Detections and Image members are cached on
the Library per path and pruned to what the last walk saw. A detection is
checked against `detection_key`; members against the file's size and mtime
plus a marker of its folder's listing (a split set gains its part 2 without
part 1 changing). The marker is a digest of the folder's sorted file names,
built once per folder per walk and shared by its files, so the check per file
is constant-time however large the folder. The cache is as large as the
Library and a repeat walk reads no headers (tested at 10,000 files). The walk is kept for `SCAN_TTL_SECONDS`
(30 s): a sweep over several Platforms, and the listing before it, share one
walk; the platforms endpoint asks for a fresh one. A finished job that changed
a file under the root ends the kept walk early (`note_paths_changed`, #353),
through a counter of its own: unlike a mapping change (`invalidate`), it never
marks a walk in progress as overtaken, so a busy night of conversions makes
the next listing walk again without ever refusing one as
`listing_in_progress`. An Image with a Platform
belongs to it; a Conflicted or Unsorted one belongs to none, so no Rule sees it.
A file the process cannot read, or a folder it cannot list, is counted as
`unreadable` and skipped. A Platform id is the canonical slug
(`platform_detect.known_slug` refuses one no Platform has) and its name is
`platform_name`; an Image key is the posix path relative to the root,
`added_at` is the mtime, and `size` is every file of the Image
(`image_sets.image_size`: a sheet and its tracks), so size filters and orders
judge the set. `summary()` gives the Rules view its counts.
`configured()` is true: the engine calls it on the event loop, so it touches no
disk, and it must not latch one bad answer. Every walk checks the root against
the configured Volumes itself, on its worker thread, and raises
`RootUnavailable` (`code = "root_unavailable"`) when the root is gone or
unreachable, so a mount blip fails that one sweep with a reason and the next
sweep checks again. One walk at a time: `scan()` takes the Library's walk lock
without blocking and raises `ListingInProgress`
(`code = "listing_in_progress"`) when a walk is already running, so a dead
mount strands one thread per Library and every later sweep releases the sweep
lock at once. `LibraryUnreadable.code` is what the engine reports for the
Platform (default `"unreadable"`), so the summary can tell these apart; the
platforms route answers 409 `listing_in_progress` for the second. The Library
declares `RECOGNIZED_FILTER` and `FAILED_HOLD`, not `CONVERTED_HISTORY`, so its
Rules are skip-only. `_recognized` returns None for every path when the match
cache cannot be read.

**The catalogue** (`services/libraries/__init__.py`). Folder Libraries are rows
of `folder_libraries` (root unique, name, `schedule_enabled`, `max_per_sweep`,
migration 0012, `AUTOINCREMENT` so a removed Library's id is never reused and
state keyed by it cannot reach another folder), read once and cached; `register`, `update` and `remove`
replace the cached copy. `register` uses `ensure_path_within_volumes` (the file
browser's check), requires a folder, and refuses the same root as another
Library, a registered folder or RomM's library folder (409), and a root that
is, holds or sits inside an Inbox (409). The same-root rule is one function,
`libraries.same_root`, run in both directions: the RomM settings route checks
a new RomM library root against every folder Library (`check_romm_root`, a 409
naming the Library) before saving it.

Libraries may nest (#514): a folder inside RomM's library folder, or inside or
around another folder Library, registers, and RomM's root may be set around or
inside one. **The nearest root owns a file.** `library_for` takes the longest
root spelling that holds the path as listed and the deepest resolved root that
holds where it lives; when those two name different Libraries it still raises
`AmbiguousLibrary` (#413), and every caller fails closed as before. The owner's
Referrers apply, and only the owner's: a file in a folder Library inside RomM
gets that Library's gamelist and `.m3u` Referrers, never RomM's swap, re-pin
or gamelist, because RomM's Rules never convert it and RomM's settings were
not chosen for it (RomM sees the new file on its next library scan). No file
is swept twice: `libraries.inner_folders(library)` lists the roots of the
other Libraries inside *library*'s (resolved, relative to its root), and each
Library's listing leaves them out. A folder Library's walk prunes them (so its
Rules, check Rules, Findings, find-duplicates and counts never see an inner
Library's files), and `RommLibrary.images` gives a ROM whose local path is in
one no path (`image_from_rom(owned_elsewhere=...)`), asked only when the
path is read, inside the bounded candidate probe, so listing never maps every
ROM onto the mount (Codex PR #518). A root
that can't be resolved there raises `LibraryUnreadable`: the listing fails
rather than take an inner Library's files. Registering, removing a Library
and saving RomM's root drop every folder Library's kept walk, so an outer
Library re-lists, and move `libraries.ownership_generation()`, all as one
step under `libraries._ownership_lock` (`_changing_ownership`, which also drops
every cached resolved root; registration, removal and every RomM settings
save then seed RomM's again, `_reseed_romm_root`, bounded through
`run_admitted` and outside the registration lock, so the Rules view's nesting
is right before any scan and a dead RomM mount holds nothing up). A sweep reads the counter when it lists a
Platform and admits its work through `libraries.owned_as_listed(owned_at)`,
which raises `rules.library.LibrariesChanged` when it moved and otherwise
holds that lock while the work goes in: `JobManager.create_jobs_atomic(...,
admission=...)` (and `create_batch_jobs`) enters it under the queue lock around
the checks and the queueing, with no await inside, and the check job is
started inside it against the counter read when the sweep began. So no
registration lands between the check and the queueing: work from a stale
listing is never queued, the run is not recorded, and the Platform reports
`libraries_changed` (PR #518). The lock is taken before `libraries._lock`,
never after, and never held across an await or a disk read. Hidden staging (#398) asks `libraries.hidden_staging(path)`,
any Library holding the path with `HIDDEN_STAGING`, not only its owner: RomM
scans its whole folder, an inner folder Library included. Folder Platform
mappings belong to the nearest folder Library (`set_mapping`), and where two
Libraries mapped one folder the nearer root's mapping wins in the index.
Setting or clearing a folder's mapping also deletes the rows the outer
Libraries holding it keep for that folder (a mapping made before the folder
became a nearer Library's), so a clear always clears (Codex PR #518). The
Libraries API adds `within` (the nearest Library around this one, or null)
and `contains` (the Libraries inside it), worked out from the stored and
already-resolved roots without touching the disk; the Rules view shows them.

Registration and the RomM root save both
take `_register_lock`: `register` in its worker thread, held across RomM's
root resolve and the database write, and the RomM save through
`romm_root_claim`, which polls for the lock without parking a thread, so a
cancelled request never leaves it held, and keeps it until the settings are
saved. The route takes the claim before the settle lock and the sweep pause,
and the root check inside it is bounded (`ROOT_CHECK_SECONDS`, #294's
`VOLUME_PROBE_TIMEOUT_SECONDS`): a root on a mount that stopped answering is
refused with 503 and every lock is released. Registration and root-check
workers go through `libraries.run_admitted`: a disposable thread under a
bound, and at most `MAX_PENDING_WORKERS` outstanding. A slot is held until
the worker returns, even after its request timed out, so retries against a
dead mount cannot pile up threads; when every slot is taken the request is
refused with 503 at once. A unique-root `IntegrityError` is a 409 too. The catalogue's `_lock` guards the
in-memory cache only: the event loop takes it on every `get()`, so it is never
held across disk or database access or a Library's walk lock. `update()` writes
the row and changes the cached Library in place (`apply_settings`), as one
step under the Library's `settings_lock` so two concurrent edits cannot leave
memory and the row disagreeing; it never waits for a walk. The register route
runs `register()` on a detached daemon thread under a 60 s bound (504 past it),
and `register()` waits at most `REGISTER_WAIT_SECONDS` for another registration
(409 "still running"), so a dead RomM mount strands one thread, never a shared
worker. `remove` calls `sweep.forget_library`, which under the
sweep lock drops the Library's Rules and state rows and its entries in the
job-owner index, then deletes the row. No file is touched.

**The API** (`routes/libraries.py`): `GET/POST /api/libraries`,
`PATCH/DELETE /api/libraries/{id}` (folder Libraries only),
`GET /api/libraries/{id}/platforms` (folder only; also `tool_ids`, `mode_ids`
and `automatable_modes`, as RomM's platform list carries),
`GET/PUT /api/libraries/{id}/rules`, `POST .../rules/forget-converted`,
`POST .../preview` and `POST .../run`. RomM's own `/api/romm/rules` and
`/api/romm/auto-convert/*` are unchanged.
`GET /api/libraries/{id}/placements` (any Library, #430) lists, for each of
the Library's `platforms()` (one listing, passed to every row as `known`),
the Platform's own `id` (the row's key: two RomM platforms can share a slug),
its `placement` relative to the root, placed by its own record alone
(`known=[platform]`, so two RomM records show their own two folders, #512),
whether the folder exists, its `rom_count`, `guessed` (a best guess among
several: a folder Library's two folders for one Platform, #515, or RomM's, #512;
`Placement.guessed`, False by default, is the shared field every Library sets)
and `why`, or the
`PlacementUnknown` reason. Where the Platform's new Images go is placed from
the whole listing: when that has `choices`, the row it lands on has
`new_images_here` True, the `choices` and the `picked` folder name, and the
Platform's other rows have `new_images_here` False. When the pick can't take
Images, its row keeps the `PlacementUnknown` reason with the `choices`, so it
can be changed. The PATCH takes a folder
Library's `layout`. The
panel (`LibraryPlacement.svelte`) waits for the view's own Platform listing
and drops a superseded answer (`src/lib/util/latestOnly.js`).

**Placement** (#430). `Library.placement(platform, *, known=None) ->
Placement` is the
inverse of `platform_folder`: given a Platform slug (detection's vocabulary,
so the Inbox can pass what `detect_platform` returned without asking which
kind of Library it has), the folder its Images go in. `Placement` carries
`platform`, `folder` (absolute) and `exists` (False: whoever places an Image
creates the folder; `placement` never does), and, for a best guess the user
may want to change (#512), `guessed`, `why` (how it was picked, in plain
words) and `choices` (`(folder name, ROM count)` pairs it picked from, when
there were several). It raises `PlacementUnknown`
when the answer can't be told, and `LibraryUnreadable` when the Library can't
be read. Every implementation ends in `rules.library.placed_in(folder,
platform, mapped=...)`, which fails closed: the folder must be inside a Volume
(`volume_containment(...) is True`), not a file, and the folder evidence
detection would read for an Image placed directly in it
(`platform_detect.placed_platform`, the same mapping-then-name walk as
`detect_platform`) must name this Platform or none, never another one.
*known* is the Library's `platforms()` when the caller already listed them;
a Library that needs its Platform records (RomM's folder names, carried as
`Platform.folder`) reads them there instead of listing again.

- `FolderLibrary` has a `layout` (`folder_libraries.layout`, migration 0023,
  `"esde"` for existing rows): `esde` (`<root>/<platform>`), `romm`
  (`<root>/roms/<platform>`) or `flat` (`<root>`, where the header decides).
  For the first two it uses a folder already in the parent that names the
  Platform by the user's mapping or by a name detection reads
  (`platform_detect.platform_folders`, spelled as listed; with two or more it
  takes the one holding the most Image files, ties by name, and marks the
  `Placement` `guessed=True`, #515), else a
  new one named by `platform_detect.esde_folder_name(slug)` (ES-DE's system
  name, `_ESDE_FOLDERS`, each value an alias of its slug) or the RomM slug.
  The layout decides only where new Images go, and changing it moves
  nothing: `platform_folder` finds the folder holding an existing Image
  whatever the layout, directly under the root or, when that folder is the
  root's `roms`, directly under `roms`.
- `RommLibrary` names the folder by a RomM platform record's `fs_slug`
  (`Platform.folder`; the slug for a Platform RomM has no record of yet).
  RomM often keeps several records for one Platform (`famicom` beside `nes`,
  #512): their folders, each with its summed `rom_count` (`Platform.rom_count`),
  are `Placement.choices`, and the pick is the user's saved one
  (`romm_settings.platform_folders()`, the `platform_folders` key of RomM's
  settings row, written only by `romm_settings.pick_platform_folder`), else the
  only folder holding ROMs, else the one with the most (then the one named as
  the Platform) with `guessed` True. A folder name with a path in it is never
  picked over a good one, and refuses when it is the only one.
  `Placement.why` says how it was picked ("RomM's NES folder, 4933 ROMs: the
  only one with ROMs"). It reads RomM's structure off the disk at the
  directory level, as `RommClient.local_path` does: `<root>/roms/<name>` when
  `<root>/roms` exists, `<root>/<name>/roms` when that exists,
  `<root>/<name>` when it exists with neither `roms` nor `bios` in it (the
  root is RomM's `roms` folder). `<root>/<name>/bios` alone guesses
  `<root>/<name>/roms`, and nothing at all guesses RomM's default
  `<root>/roms/<name>`, both `guessed` (RomM 5.3 declares the structure in a
  setting this app doesn't read). With no folder for the Platform yet, it
  follows the other Platform folders under the root first (Codex PR #516):
  `<root>/<name>/roms` when one holds `roms` or `bios`, else `<root>/<name>`,
  never a new `<root>/roms`, which would flip `local_path`'s layout and lose
  every existing record. When `placed_in` refuses the pick,
  `PlacementUnknown.choices` and `.picked` carry the choices, so the
  listing's row for the picked folder keeps its chooser and the pick can be
  changed. `RommLibrary.platforms` drops a record whose
  `fs_slug` is a junk name (`utils.junk.is_junk_entry`, Synology's `@eaDir`).
  `RommLibrary.pick_folder(platform, folder)` saves a pick after checking it is
  one folder (no path in it) and one of the choices; the panel's "Automatic"
  option sends None to forget it (`PUT /api/libraries/{id}/placements/{platform}`, 422
  otherwise; 400 for a Library without `pick_folder`).

**The Rules view** (`src/lib/components/views/RulesView.svelte`). A Library
picker over `RuleEditor.svelte` (the former `RommAutomation.svelte`). The editor
reads one `RulesSource` (`src/lib/rules/rulesSource.svelte.js`), which owns the
Rule bookkeeping once (load, edit, save with the save-race guard, forget,
preview, run) over endpoints passed to its constructor. `RommStore` and
`FolderRulesSource` (`src/lib/stores/libraries.svelte.js`) both extend it and
add only their Platforms and settings; RomM's `afterRun` updates its re-pin
badge. The RomM view's Automation tab renders
the same view opened on RomM. Folders get **Register as a Library** in the row
menu (`RowActionsMenu.svelte`).

### 3.3.12 The Round-trip check (`ToolPlugin.content_digest`, `services/round_trip.py`)

A container check proves the output reads back cleanly. It does not prove the
output holds the same disc as the Source the job is about to delete. The
Round-trip check does (#298): the output is Round-tripped when the content it
decodes to equals the content its Source decodes to. It is a claim about image
content, never container bytes: `Game.iso` and the `Game.rvz` made from it have
different file hashes and the same digest.

**The contract.** One optional method and one mode flag:

```python
@dataclass(frozen=True)
class ContentDigest:
    kind: str    # shape of what was hashed, e.g. "gc_wii_disc_sha1"
    value: str   # lowercase hex; compared only for equality (chdman: "<sha1>/<sha256>")
    def same_content(self, other) -> bool: ...   # ValueError on a kind mismatch

async def content_digest(self, path, *, cancel_event=None, within=None) -> ContentDigest | None
```

- `content_digest(path)` hashes what `path` decodes to. `None` means the tool
  has no digest for this kind of file; `ContentDigestUnavailable` means it
  should have one and could not get it. `BaseTool` returns `None`.
- `within` (a sequence of folders, or `None`) is where a sheet's track
  references may resolve, with the same meaning as `post_convert`'s: `None`
  confines them to the configured Volumes. `round_trip(..., within=)` passes
  the job's value to the Source side only: `[job.temp_dir]` for an
  archive-member Source, else `None`. The output always lives in a Volume, so
  its side gets `None`. A tool that extracts an output into its own scratch
  folder before hashing it passes that folder to its own sheet reads. A tool
  that reads no sheet, like Dolphin, accepts the argument and ignores it.
- `kind` keeps digests of different shapes apart. `same_content` raises
  `ValueError` rather than answer "different" for, say, a CD track hash against
  a disc-image hash, and the check treats that as "not proven".
- `ModeSpec.supports_round_trip` (mirrored as `supportsRoundTrip` in
  `registry.js`, parity-tested) says the mode's tool has a digest for both its
  inputs and its output. `registry.mode_supports_round_trip(mode)` reads it.
  `ChainSpec` carries it too: a chain that sets it compares the first step's
  decoded Source with the final output (see "Chains" below).
- `ModeSpec.round_trip_required` (mirrored as `roundTripRequired`,
  parity-tested; read through `registry.mode_round_trip_required`) makes the
  Round-trip the only way a delete-source job in the mode can pass: the
  container-only opt-out is ignored (`round_trip_applies`, so
  `delete_check_level` and a Rule's Preview say `round_trip` too), and the
  convert panel and the Rule editor don't offer the box. For a mode whose
  output has no container of its own, so its own check proves nothing about
  its content: the rar tool's one file out of a set (#476), checked by reading
  it in full. `tests/test_tool_registry.py` accepts such a mode deleting its
  Source although no `verify_extensions` cover its output (any extension).

**Where it runs.** `JobManager._delete_check` is the one decision point. It runs
after the container check passes and returns `(check_passed,
keep_source_reason)`. The Round-trip runs only when it decides something:
`delete_on_verify` is set, the mode supports it, and the job did not take the
named opt-out `ConversionJob.container_only` (`container_only` on
`POST /api/jobs` and `/api/jobs/batch`, and a Rule's `check_level`, §3.3.11).
That test is `round_trip.round_trip_applies`, shared with Rule Preview.
Otherwise the answer is `("intact", None)` with no extra reads, which is what every `verify_after` job
and every mode without a digest gets. `ConversionJob.check_passed` carries the
result to the Ledger and to the job's `complete` event. On a pass,
`verification_store.mark_verified(..., check=check_passed)` writes it to
`verifications.check_passed` (migration `0010`, existing rows backfilled
`"intact"`). A mismatch or an unavailable digest leaves
`check_passed="intact"`, stores nothing for the output (see below), keeps the
Source and fails the job with the reason.

**`ToolPlugin.source_problem(path, *, within=None)`** (#301) is asked once
before any digest, under the same `verify_timeout` bound: a reason the Source
must not be trusted, or None (`BaseTool`'s default). chdman answers it for a
`.chd` Source (Copy) by running its verify; every other tool has nothing to
check. A reason keeps the Source with "Round-trip failed: <reason>. Source
kept.", an exception or a timeout counts as a reason, and a cancel is reported
as cancelled. It gets the same `within` as the Source's `content_digest` (the
job's extraction folder for an archive member, the Volumes otherwise); an
override that reads a sheet, a track or an archive member passes it to that
read and fails closed. `ChainTool` passes `within` unchanged to the tool that
reads the path, for both hooks. maxcso, z3ds, JWUDTool, nsz and romz read no
sheet: they read the Source file itself, their own decoder output or their own
scratch folder directly, none of which goes through the Volume-confined sheet
reader, so they accept `within` and ignore it.

**`services/round_trip.round_trip(tool, source, output)`** owns everything a
tool should not repeat: one side at a time (both are usually on the same
volume), each bounded by `tool.verify_timeout(path)`, any exception folded into
"could not run", and a fired `cancel_event` reported as cancelled (no verdict,
no delete) rather than as a failure. `source` is the file the converter read,
so an archive member is hashed from its extracted copy. The job marks the step
in `_verifying` so the stall reporter treats it like the container check.

**Per tool.** Each tool proves content the cheapest sound way it has:

| Tool | Source side | Output side |
| --- | --- | --- |
| dolphin | `.iso`: SHA1 of the file (detached read, no subprocess). `.rvz`/`.wia`/`.gcz`/`.wbfs`: `dolphin-tool verify --algorithm sha1` | same rule by extension |
| chdman (`createcd`, `createdvd`) | `.iso`/`.bin`: SHA1 of the file. `.cue`/`.gdi`: SHA1 of the track files the sheet names, read end to end in sheet order, each file once, plus the sheet's track table | `.chd`: `chdman info`, then by metadata tag. DVD (`DVD `) or hard disk (`GDDD`): the `Data SHA1` line. CD (`CHCD`/`CHTR`/`CHT2`): `chdman extractcd` to a `.cue` in a scratch dir, then the sheet rule. GD-ROM (`CHGT`/`CHGD`): the same, extracted as a `.gdi` |
| maxcso (`cso_compress`, `cso2_compress`, `zso_compress`, `dax_compress`) | `.iso`: SHA1 of the file | `.cso`/`.zso`/`.dax`: decoded in Python by `utils.cso_decode` and hashed as it streams (no temp file) |
| z3ds (`z3ds_compress`) | the ROM: SHA-256 of the file | the payload after the Z3DS header and metadata, fed to `zstd -dc`; stdout hashed by `SubprocessRunner.hash_stdout` (no temp file) |
| jwud (`jwud_compress`) | `.wud`: SHA-256 of the file, or of every part of a split set in order | `.wux`: the sector table decoded in Python by `jwudtool.iter_wux_image` and hashed as it streams (no temp file) |
| nsz (`nsz_compress`) | `.nsp`/`.xci`: SHA256 of the file | `.nsz`: the `[NSP SHA256]` line `nsz -V` prints for the NSP it rebuilds in a hashing stream. `.xcz`: `nsz -D` into a headroom-checked scratch dir, then SHA256 of the `.xci` |
| romz (`romz_7z`, `romz_zip`) | the ROM: its basename, size and CRC32, as the one member it is archived as | `.7z`/`.zip`: every member's name, size and stored CRC32, read from the listing (`archive_members.read_member_crcs`) |
| chain (`cso_to_chd`) | the first step's tool (maxcso: the decoded CSO) | the final step's tool (chdman: the CHD's data SHA1) |

chdman's digest kind is `image_data_sha1`. Its value is the SHA1 of the image's
bytes as files on disk, track files laid end to end in sheet order, followed by
`/<sha256 of the track table>` for a sheet whose table says more than a bare image
(`utils.disc_sheet.read_disc_sheet`). That one definition covers an ISO, a
cue/bin set (split or single-bin) and a DVD CHD's data SHA1. The data part is
SHA-1 because the DVD side is the CHD header's `Data SHA1`, and an `.iso`
Source is compared with a DVD CHD and a CD extract alike. The table hash is
only ever compared with another made by the app, so it is SHA-256.

**The track table** is compared so that equal sector bytes in a different disc
layout do not pass. Per track of a cue: the type token (`MODE1/2352`,
`MODE2/2352`, `AUDIO`, ...), its sector size, the pregap frames held in the file
(`INDEX 01` minus `INDEX 00`), the `PREGAP` and `POSTGAP` frames, and the frames
from `INDEX 01` to the end of the track; plus the number of tracks. Per track of
a gdi: start LBA, data or audio, sector size and frames. Positions follow MAME's
cue reader (`INDEX` times are frames within the file, a track's bytes start
where the previous track in that file ended), so a Redump set with one `.bin`
per track has the same table as the single `.bin` chdman writes back. A sheet
whose table is exactly what chdman infers for a bare image of those bytes (one
gapless `MODE1/2048`, `MODE2/2336` or `MODE2/2352` track, tried in that order)
carries no table hash: that is how an `.iso` Source compares with the one-track
cue a `createcd` of it extracts to. Not compared, because a CD CHD does not keep
them: `INDEX 02` and later, `FLAGS` (DCP, PRE), `CATALOG`, `ISRC` and `REM`
lines. Those are lost on conversion with or without the check.

**One sheet reader, and where tracks may live.** `read_disc_sheet` parses
nothing itself: it reads the sheet through `utils.sheet_text` (§3.3.14), the
same framing, `chd_tokens`, size cap, decodings and ambiguity refusal as every
other sheet reader. The track files are `sheet_track_list(sheet, within=...)`;
the table is built from the same reading's `sheet_line_tokens` (CUE, with
`cue_file_name` for the FILE rule) or `gdi_tracks` (GDI, which also carries
each line's LBA and type). Every track is confined to the trusted roots in
`within`. A Source sheet uses the `within` the job passes through
`round_trip` (its extraction folder for an archive member, `None` for the
Volumes). The sheet `chdman extractcd` writes is read with
`within=[the scratch dir it was extracted to]`: the scratch volume is outside
the Volumes, and that dir is the only place its tracks may be. Anything
`sheet_text` refuses makes the digest unavailable, so the Source is kept.

**The DVD side reads the header `Data SHA1`.** For `createdvd` that is the
SHA1 of exactly the logical bytes chdman read, which is the whole ISO (MAME
`chd_file_compressor` clips the hashed read to `logical_bytes()`). It proves
what the CHD decodes to only together with the container check, which the job
always runs first: `chdman verify` recomputes the SHA1 of the decoded data and
compares it with this header value. chdman exits 0 even on that mismatch (it
prints `Error: Raw SHA1 in header = ...` to stderr), so `ChdmanService.verify`
passes only when the output has `Raw SHA1 verification successful!` and no
`Error` or `No verification to be done` line (`SubprocessRunner.run_verify`'s
`judge_output` hook; the strings are the same from MAME 0.146 to master). That
rule also makes the plain Intact check of every chdman mode stricter.

The tag is read strictly from the `Metadata: Tag='XXXX'` lines. A CHD with no
`Metadata:` lines at all (what `createraw` writes) is read by its data SHA1 too,
which for a raw CHD is the SHA1 of the input bytes (checked against real chdman
0.285). `Metadata:` lines the tag reader cannot parse are not "no metadata":
that CHD is "not proven", like one with another tag (LaserDisc `AVAV`).
`createhd`, `createraw` and `copy` declare `supports_round_trip` since #301:
Create HD and Create raw compare the input's SHA1 with the CHD's data SHA1
(`createhd` may pad the image to its geometry, which reads as a mismatch, so
the Source is kept, never a false pass), and Copy digests both CHDs the same
way (a CD CHD Copy extracts both, one at a time). Before comparing, a Copy
checks its Source CHD with the same strict `chdman verify` (`source_problem`,
below): a Source that is not Intact is kept, with that reason, rather than
compared with an output that faithfully copied its damage. A Copy of a
LaserDisc CHD always keeps its Source ("no CD, DVD or hard-disk metadata"),
since an `AVAV` CHD has no digest. `createld` has no
delete-source since #301: its Source is an AVI, the CHD's data SHA1 covers
chdman's A/V hunk encoding, and `extractld` does not rebuild the AVI byte for
byte, so there is nothing to compare. Note that the app's `createraw` passes
no `--unitsize`, which chdman 0.285 refuses, so a Create raw job fails before
any check (a pre-existing bug, outside the Round-trip).

**The extract fallback** (`ChdmanTool._extracted_cd_sha1`) runs inside
`services.disk.headroom_scratch_dir(prefix, required_bytes)`, the shared form of
what a chain does for its work dir: create the dir on the scratch volume
(`create_scratch_dir`, so `CHD_TEMP_DIR`), run `ensure_headroom` for
`required_bytes` plus `scratch_margin_bytes()` (the `chain_disk_margin_mb`
cushion) before anything is written, and `remove_partial_tree` the dir on
every exit. `required_bytes` is the CHD's `Logical size`, which counts
2448-byte frames and so bounds the extracted tracks from above. Too little
space, a failed or cancelled `extractcd`, or the outer `verify_timeout` bound
all end the same way: the dir is removed and the digest is unavailable, so the
Source is kept. `ChdmanService.extract_cd` goes through the tool's
`SubprocessRunner.run_capture`, so the child is PID-tracked and Cancel stops
it. The extract reads the CHD once more and writes up to the image's size to
the scratch volume, so a CD delete-source job takes roughly one more read and
one write of the disc. The whole CD output digest (`info`, extract, re-hash)
runs under `verify_timeout` sized on the compressed CHD; at the defaults
(1800 s plus 600 s per GiB) that is ample, but an operator who tightened the
per-GiB knob will see "did not finish in time" and kept Sources.

Known limits, all in the safe direction (the Source is kept and the job says
the content differs): an extract gives back the layout chdman writes, so a
Source that chdman cannot reproduce byte for byte does not match. That covers
cue `WAVE` or `MOTOROLA` (big-endian) audio files, track files with a trailing
partial sector, and GD-ROM sets whose layout differs from chdman's `.gdi`
output (a CHD does not say whether its Source was a `.gdi` or a Redump-style
cue, and it is always extracted as `.gdi`). Two cases are kept every time: a
Redump-style GD-ROM cue/bin Source (a cue table can never equal chdman's gdi
table), and a cue with `CDI/2352` tracks (MAME stores them as `MODE2_RAW` and
extracts `MODE2/2352`, and track type tokens are compared as written).

**The rest of the tools (#301).** After #301 every mode with delete-source
Round-trips (a test pins zero exceptions). PS3 folder to ISO (`folder_to_iso`),
the XISO trim (`xiso_rewrite`) and chdman `createld` offer no delete-source at
all, and `nkit_to_rvz` still does not (its comment says why), so none of them
needs a digest. Kinds:
maxcso uses chdman's `image_data_sha1` (now
`tools.base.IMAGE_DATA_SHA1_KIND`): the SHA-1 of the image's bytes. SHA-1
because `cso_to_chd` compares the decoded CSO with a CHD header's data SHA-1,
which the app does not choose; `stream_digest.new_hasher` creates it with
`usedforsecurity=False`, so no scanner suppression is needed. z3ds and JWUDTool
compare only digests they make themselves, so they use `image_data_sha256`
(`IMAGE_DATA_SHA256_KIND`, SHA-256). nsz uses `switch_image_sha256`, romz
`rom_members_crc32` (SHA-256 of the sorted `name, size, crc` lines, so a missing, extra or renamed member, or another
size or CRC, gives another value). romz trusts the CRCs the archive stores
because the container check before it (`7z t`) decompresses every member and
checks it against that CRC. A zip name written without the UTF-8 flag (p7zip
does that) is read back as UTF-8 when it decodes, and names on both sides are
compared in NFC.

**Strength.** romz is the weakest bar in the set: CRC32 and size per member.
CRC32 catches every burst error up to 32 bits and every odd-weight error, and
random damage gets through about once in 4.3 billion, which is fine against
accidental damage and is what #301 asked for. Every other tool compares a SHA1
or SHA256 of the whole decoded image. The Ledger says Round-tripped for both.

**Streaming with no temp file.** Two shared seams hash a decoded image as it
is produced:

- `services.stream_digest.hash_stream(produce, algorithm=, cancel_event=)` runs
  a Python producer (`produce(stop) -> Iterable[bytes]`) on a detached thread
  (`run_detached`, never a pooled worker) and hashes what it yields. A cancel
  raises `ReadCancelled` at once, and on that, an outer deadline or any other
  exit it sets `stop`, which the producer checks between chunks, so the thread
  ends instead of decoding the rest of the image. `content_digest_of` wraps it
  into a `ContentDigest` and turns a decoder's `ValueError`/`OSError` into
  `ContentDigestUnavailable`. `file_chunks(paths)` is the producer for a plain
  file or a split set. Algorithms: `sha1`, `sha256`, `crc32`.
- `SubprocessRunner.hash_stdout(cmd, stdin_path=, stdin_offset=, timeout=,
  cancel_event=)` runs a decoder child and hashes its stdout as it arrives,
  optionally feeding stdin from a region of a file (read on detached threads).
  PID-tracked, command-wrapper nice/ionice, bounded by `timeout` and stopped
  through the `reap` ladder on a cancel (`ReadCancelled`), a timeout or a
  non-zero exit (`RuntimeError` with the stderr tail).
- `subprocess_runner.feed_stdin_from_file(process, path, offset=, cancel_event=)`
  is the one stdin feeder, shared by `hash_stdout` and z3ds verify. Reads are
  detached; a read still in flight when the feed is cancelled is abandoned
  with its handle, never closed, because closing waits on the lock the stuck
  read holds.

**No filesystem call on the event loop.** A digest runs while the job holds
the only dispatcher slot, and a stat on a hung mount blocks the loop itself, so
no deadline or cancel can fire. Every path, size, existence and sibling-file
lookup in a digest runs on a detached thread: inside the hashing producer
(JWUDTool's split parts, the produced XCI) or through `run_detached` (the
Z3DS payload offset and `zstd` lookup, nsz's file identity and XCZ size, romz
listings). Tests stand a hung stat in for a dead mount and check the deadline
still fires.

Both run under the same outer bound as every digest: `round_trip._digest`
wraps the call in `tool.verify_timeout(path)`.

Why each tool decodes where it does: maxcso writes every block at an explicit
file offset (libuv `fs_write` with `dstPos_`), which a pipe refuses, so it
cannot decode to stdout; `utils.cso_decode` reads the formats as maxcso's
`README_CSO.md` and `src/input.cpp` define them (CSO v1: raw deflate, high bit
uncompressed; CSO v2: a block at least the block size long is uncompressed,
else the high bit picks LZ4 over deflate; ZSO: LZ4, high bit uncompressed; DAX:
zlib frames of 8 KiB, NC areas uncompressed). LZ4 is decoded in Python (the
image has no `lz4` package) and stops once a block is full, like maxcso's
`LZ4_decompress_safe_partial`, because stored blocks may carry alignment
padding. That is slower than deflate; a large ZSO may run into
`verify_timeout`, which keeps the Source. JWUDTool writes a fixed
`game.wud` into a folder and refuses an existing target, so neither a pipe
nor a FIFO works; the WUX layout `read_wux_header` already validates is
decoded instead. z3ds's payload is plain seekable zstd, so the `zstd` binary
`verify` already uses decodes it to a pipe. A decoder bug in either Python
decoder can only make a good container hash differently from its Source,
which keeps the Source; it cannot produce the Source's bytes from a container
that does not hold them. `utils.cso_decode` refuses the headers maxcso's
reader refuses (block 2 KiB to 256 KiB and a power of two, a sector-aligned
image size), and two things maxcso's writer never produces: an index shift
past 16, and a stored block longer than the block size plus its alignment
padding and 1 KiB of codec slack. Every block's span is checked before its
batch is read, so a corrupt or crafted index (two entries far apart in a
sparse file) is refused as damaged rather than allocated; a read is at most
4 MiB of blocks. DAX needs no such check: its lengths are 16-bit. Otherwise
the decoder is more lenient than maxcso:
it does not check a deflate stream's end marker, the header-size field or
trailing data. maxcso's own `--crc` container check runs first and reads the
container with maxcso's reader, so a container that only this decoder accepts
does not get that far. Checked against real maxcso output in the image: every
compress mode at every effort preset, and a 2.1 GiB ISO, for which maxcso
picks a 16 KiB block and index shift 1 and leaves a short last block.

**Chains.** `ChainTool.content_digest(path)` hands a chain's Source (an input
extension of one of its specs) to the first step's tool and anything else to
the final step's tool, and `verify_timeout` follows the same rule. The two
tools must agree on the kind, or `same_content` refuses and the Source is
kept. `cso_to_chd` sets `supports_round_trip`: maxcso digests the `.cso` by
decoding it and chdman digests the `.chd` by its data SHA1, both
`image_data_sha1`, so the decoded disc image is what is compared.
`nkit_to_rvz` has no delete-source. The Source is recognised by any chain's
input extensions, not only the running chain's; that is safe while no chain's
output extension is another chain's input extension, and a chain that broke
that rule would have to resolve the spec from the output first.

**Spike (#301): does `nsz -V` prove content equality?** Read from nsz 4.6.1
and 5.0.0 (`nsz/NszDecompressor.py` / `Decompressor.py`). Result: **not on its
own.** `nsz -V file.nsz` decompresses every NCZ and checks each NCA's SHA256
against the hashes in the CNMT inside the same file
(`FileExistingChecks.ExtractHashes`). That proves the output agrees with its
own metadata, not with the Source: it never reads the Source, and the CNMT NCA,
tickets and certificates are not checked against anything outside the file.
So it does not count as Round-tripped. But the same run rebuilds the whole NSP
(`Pfs0VerifyStream`, header laid out as the original, no file written) through
SHA256 and prints `[NSP SHA256] <hex>`. That hash compared with the Source
NSP's SHA256 is a decode-and-compare with no temp file, and that is what the
nsz digest uses. For an XCZ, `nsz -V` prints no whole-image hash, so the XCZ
is decompressed with `nsz -D` into `headroom_scratch_dir` (required bytes
from the XCI cart header's valid-data-end page, or four times the container
when that cannot be read) and the `.xci` hashed. Known limits, in the safe
direction: nsz drops NDV0 delta fragments unless run with `--keep`, so an
update NSP that held them is kept with "content differs"; an XCZ whose
decompressed XCI is not byte-identical to the Source (nsz rewrites the XCI
container around the partitions) is kept the same way.

**How nsz rebuilds an XCI** (`nsz/Fs/Xci.py`, `XciStream`, 4.6.1 and 5.0.0):
it copies only the first 0x200 bytes of the cart header from the XCZ, leaves
0x200 to 0xF000 as zeros (a real dump has the card info and certificate area
there), writes the root HFS0 at 0xF000 and each partition back to back after
it, then rewrites the header. Nothing pads the image to the cart size. So an
untrimmed dump, or any dump whose bytes between 0x200 and 0xF000 are not all
zero, does not come back byte for byte. The Round-trip still runs (it is the
spec's fallback) after the scratch headroom check, and on a mismatch the XCZ's
digest carries `ContentDigest.mismatch_reason`, so the job says "nsz did not
rebuild this XCI byte-for-byte, so the Source is kept" rather than "content
differs". `mismatch_reason` is not part of the comparison; `round_trip` uses
the output's (or the Source's) when the digests differ. The XCZ scratch size is
never below twice the container, so an understated header cannot let `nsz -D`
fill the scratch volume.

**nsz -V runs once.** The container check is `nsz -V`, which prints the same
`[NSP SHA256]` line. `SubprocessRunner.capture_verify(on_success_output=)`
hands a passing run's output to nsz, which keeps the hash keyed by the file's
identity (path, device, inode, size, mtime); `rebuilt_nsp_sha256` takes it once
and runs `nsz -V` itself only when there is none or the file changed.

What the check guarantees
does not depend on the input format: only identical digests allow a delete,
and any mismatch keeps the Source. An NKit `.nkit.iso` Source is hashed as a
file; whether that equals the digest of its output depends on what
`dolphin-tool` writes, and either way the guarantee holds.

**Spike (#300): can a CD Source be hashed into the layout chdman's data SHA1
covers, without extracting?** Result: **not proven, so not used.** The CD path
extracts the output instead (the extract fallback above).

What the data SHA1 of a CD CHD covers, read from MAME's source
(`src/tools/chdman.cpp`, `src/lib/util/cdrom.cpp`, master, November 2026):

- Every frame is 2448 bytes: the track's sector data (`datasize`, 2048 or
  2352) zero-filled to 2352, then 96 bytes of subcode (zeros when the Source
  has no `.sub`). `chd_cd_compressor::read_data` starts each block with
  `memset(dest, 0, length)`.
- Each track is padded to a 4-frame boundary: `do_create_cd` computes
  `padded = (frames + TRACK_PADDING - 1) / TRACK_PADDING` with
  `TRACK_PADDING = 4` and stores the difference as `extraframes`.
- Audio is stored byte-swapped. The CUE parser sets `swap = true` for every
  `AUDIO` track ("this is true for cue/bin and cue/iso"), and `read_data` swaps
  each 16-bit sample when `swap` is set. `do_extract_cd` swaps it back for
  cue and GDI output.
- Pregaps: a `PREGAP` directive adds `padframes` that `read_data` zero-fills,
  while `INDEX 00` data in the file is stored as pregap data. GD-ROM images add
  `splitframes` that read across a track boundary.

So a direct hash is possible in principle: walk the sheet, read each track's
sectors, zero-fill to 2352, append 96 zero bytes, swap audio, emit the pregap
and 4-frame padding frames, and SHA1 the stream. But every one of those rules
must match chdman exactly, for every track type, pregap form and GD-ROM layout,
or a good CHD reads as a mismatch. No chdman binary was available to prove the
reimplementation against (none installed on the dev machine, and Docker was not
running), so the rule from #300 applies: use the extract fallback. A later
change can add the direct hash once it is tested against real chdman output
across a sample set, and then skip the extract.

DVD and HD CHDs need no spike: they store the image's bytes as-is, so the data
SHA1 is the SHA1 of the Source image. The overall `SHA1` that `chdman info`
also prints folds in the metadata, so it is never a content digest.

**A failed Round-trip stores nothing.** On a mismatch or an unreadable digest
the output's verification record is cleared, not written: the output gets no
check badge, no `produced_meta` for the re-run fast path (§3.3.6), and a record
an earlier run left for that path is dropped. `ConversionJob.check_passed`
(and so the Ledger row) still says `"intact"`, because the container check did
pass.

**A reused output keeps its check.** When the re-run fast path (§3.3.6) skips a
conversion, no check runs in that job, so `job.check_passed` stays unset. Its
`complete` event still sends `check_passed` read from the stored record
(`JobManager._stored_check`), so the UI keeps a Round-tripped badge rather than
falling back to Intact.

#### The Recognized proof (`ModeSpec.delete_proof`, `services/recognized.py`, #423)

Some outputs can't be Round-tripped against their Source at all: rom-converto's
PSP PBP -> ISO (nothing turns an ISO back into its PBP) and 3DS CIA <-> CCI (a
converted CIA is unsigned with a zero title key, spike #421). For those a mode
declares a different delete proof on the mode contract:

- `ModeSpec.delete_proof` (and `ChainSpec.delete_proof`) is a `DeleteProof`:
  `ROUND_TRIP`, the default for every mode, or `RECOGNIZED`. Read through
  `ToolRegistry.mode_delete_proof(mode)`; an unknown mode answers `ROUND_TRIP`.
  A `RECOGNIZED` mode sets `supports_delete_on_verify` and never
  `supports_round_trip` (`tests/test_rom_converto_423.py` checks every mode).
- `round_trip.delete_check_level(mode, delete_on_verify=, container_only=)` is
  the one answer to "which check must pass before this job's Source goes":
  `"recognized"`, `"round_trip"`, `"container_only"` or None for a job that
  keeps its Source. `recognized_applies` and `round_trip_applies` are its two
  halves; a Rule's `effective_check_level` (Preview, the delete decision
  identity, a retried delete's required check) calls it, as the job does.
- **The container-only opt-out is a Round-trip opt-out.** It never skips the
  Recognized proof, whatever a request or a Rule stores, and the convert panel
  and the Rule editor offer the box only for a mode with `supportsRoundTrip`
  and without `roundTripRequired`.
- `services/recognized.recognize(path, cancel_event=, timeout=)` hashes the
  output with `stream_digest.hash_files` (SHA-1 on a detached thread a cancel
  stops) under the tool's verify bound, looks the hash up with
  `dat_store.lookup_sha1` (ranked by `with_current_ranking`, so the DAT the user
  put first is the one named), then with `hasheous.lookup` when Hasheous is on,
  and passes only on a hit. A miss, an unreachable Hasheous, a hash that did
  not finish or DATs that could not be read all keep the Source.
- `JobManager._delete_check` returns a `_DeleteCheck`: the check passed, plus a
  `fail_reason` (a failed Round-trip: the job fails, as before) or a
  `keep_reason` (not Recognized: the output is Intact and placed, its record
  stored as `intact`, the Source kept, and the job **completes** with
  `job.warning` and its message ending `converted, source kept: output not
  Recognized`). On a hit, `job.check_passed` is `"recognized"` and
  `job.check_dat` names the DAT (a local DAT's name, or the source Hasheous
  names); the verification record stores `recognized` too.
- The Ledger's `check_passed` gains `recognized`, and the new `check_dat`
  column (migration 0024, appended last in the CSV) records the DAT. A retried
  delete (#329) whose Rule's level is `recognized` needs a `recognized` record
  and re-runs the proof on the output before removing anything
  (`_recheck_output`).
- The frontend mirrors it as `deleteProof` on the mode entry (compared by
  `tests/test_frontend_parity_186.py`); `deleteCheckFor` and
  `DELETE_CHECK_HINTS` in `src/lib/util/deleteCheck.js` are what the convert
  panel, the delete confirmation and the Rule editor say.

### 3.3.13 Notifications (`services/notifications.py`)

The one place that sends anything off the server. Callers use these functions and
never see a transport:

- `notify(event)` queues an `Event(kind, title, body, data)` and returns at once.
- `on_job_finished(job)` is a `JobManager.add_terminal_listener` callback
  (registered in `main.py`). A failed job that no sweep owns becomes one
  `job_failed` message.
- `sweep_owner()`, `sweep_jobs_queued(jobs)` and `sweep_finished(result, dry_run=,
  library_id=)` are the sweep's hooks (one line each in
  `rules/sweep.py::_sweep_locked`; `sweep_finished` is called from `sweep()` in a
  `finally`, so the post-lock return point named in the Rule engine section is not
  where a sweep ends for notifications). A sweep queues jobs and returns, so its
  message waits until the last of those jobs is terminal, then sends one
  `sweep_finished` message: counts, bytes saved (the Ledger's definition, via
  `ledger.job_saved_bytes`), and the failures listed inside it. The queue call
  enqueues and then awaits, so a job can end before it returns. Ownership is
  therefore set at creation: `sweep_owner()` returns a token, the sweep passes it as
  `owner=` through `create_batch_jobs` and `create_jobs_atomic`, and
  `JobManager._queue_job_locked` sets `ConversionJob._sweep_owner` before the job is
  enqueued (`_sweep_owner` is a private attribute, in no schema or serialized form; callers that pass nothing behave as
  before). The terminal listener decides by `_sweep_owner` alone: an owned job is never
  sent as a lone `job_failed`, and a manual job on the same Source is never
  absorbed. Once `sweep_jobs_queued` has the jobs, each is the sweep's by id
  whatever its status, so a late listener sends nothing alone and the message goes
  out once. A job already terminal when `sweep_jobs_queued` runs is counted
  but not waited on. `sweep()` calls `sweep_finished` from a `finally`, with a
  default `sweep_failed` error, so a sweep that raises after queueing still
  closes; a cancel (shutdown) reports nothing as a failure. A sweep that queued
  nothing and hit no error sends nothing; a preview sends nothing.
- A platform that keeps failing is due again every scheduler tick, so an error is
  reported once per `(library, platform, reason)`: repeats stay quiet and a different reason
  sends again. A reported error is forgotten only when a later sweep lists the
  platform in `result["platforms"]` (it was inspected) without that error; a
  filtered or capped sweep forgets nothing. There is no recovery message. The key
  is the error text, so those texts must stay constant.

Events are a registry: `register_event(key, title, default=True)` adds one,
`event_list()` feeds Settings. A later ticket (check failed, with check Rules)
registers its own event and calls `notify`; no transport changes. Each
destination chooses its events (#523): an `events` list of keys stored on it
(unknown keys refused on save), or, when it never chose, every event
registered `default=True`, so a new default event reaches it with no
migration. The dispatcher enqueues an event only for the destinations whose
`_events_of(dest)` holds its kind; Test ignores the choice. `job_succeeded`
(a job started by hand that completed, `default=False`) is the one event off
by default.

Destination types are rows of one table, `_KINDS` (`Kind(key, label, placeholder,
send, check, hint, signs)`); `KINDS`, `TRANSPORTS` and `kind_list()` (which Settings reads
to build its picker) derive from it, so adding a type is one row and nothing else
branches on the kind. Transports are `TRANSPORTS[kind](url, event)`, blocking and
raising on failure. A kind with `signs=True` (the generic webhook, #523) also
takes the destination's signing secret as a third argument, passed only when
one is set: `_post_json` then sends `SIGNATURE_HEADER`
(`X-Compressatorium-Signature: sha256=<hex>`), the HMAC-SHA256 of the exact
body bytes. A secret on any other kind is refused on save. Like the URL, it
is never returned (`public_view` says only `signed`); an edit omitting it
keeps it and `""` removes it. `webhook` and `discord` share one HTTP path, `_post_json`
(`http.client`, JSON `POST`, http/https only, one overall deadline) and differ only
in the payload builder: `generic_payload` is `{"event", "title", "message", "data"}`
(example in the README); `discord_payload` is one embed (colour by outcome, at most
10 scalar `data` fields, a UTC timestamp) with `allowed_mentions: {"parse": []}`,
truncated with an ellipsis to Discord's limits (content 2000, title 256,
description 4096, field value 1024, 6000 over one embed). `discord` requires https
when a destination is saved or tested. 200 and 204 are success; 429 is an error
naming Retry-After and is never retried. `apprise` is imported inside the function;
without the package its `check` refuses Apprise URLs with a
clear message. Tests replace entries in `TRANSPORTS` with a recorder. The webhook
never follows redirects (any status of 300 or more is an error, and `Location` is
never read), does not use `HTTP(S)_PROXY`, and runs under one overall deadline: a
timer shuts the socket down at 0.9 x `SEND_TIMEOUT_S`. Apprise keeps its own
per-operation socket timeouts (4 s by default), which are not an overall limit.

`notify` and `deliver` put the event in a module inbox synchronously, before any
await, so the order events were raised in is the order every destination sees
them. One dispatcher task drains the inbox: it loads the destinations and hands the
event to each destination's own FIFO queue (`MAX_QUEUED_PER_DESTINATION`, oldest
dropped with a warning when full), one event at a time. Each queue has one worker.
The worker reads the destination's kind and URL when it sends, so an edit applies to
messages already queued and a destination deleted meanwhile is skipped;
`remove_destination` also drops its queue. Empty queues and finished workers are
removed.

Sends run on `run_detached`'s daemon threads (`services/subprocess_runner.py`), never a
thread pool: pool workers are joined at interpreter exit, so one send that never returns
would hold a CLI command open after its work finished. A thread cannot be killed: if a send outlives `SEND_TIMEOUT_S` the
destination is marked busy until its thread really ends, and queued sends to it
fail fast ("still running") instead of stacking threads. Errors are scrubbed: a
destination URL is a secret and appears in no log line, no API response and no
error text. Foreign exceptions are logged by type name only; our own error texts
pass `_redact`, which strips the URL and each piece of it. An Apprise failure
reason is taken from Apprise's own log (`LogCapture`) and redacted the same way.

Destinations live in the `notifications` preference (`{"destinations": [{id,
kind, url, label}]}`). The API (`routes/notifications.py`) returns only
`{id, kind, label, hint}`, where `hint` is a webhook's host or an Apprise scheme.
An edit that omits `url` keeps the stored one. Pending sweeps are in memory, so a
restart mid-sweep drops that one message.

### 3.3.14 Platform detection (`services/platform_detect.py`, `ToolPlugin.header_platform`)

`detect_platform(path) -> Detection` is the one place an Image's Platform is
worked out without a library manager. The convert panel's Auto choice uses it
today; folder Libraries (#303), the Inbox and the CLI (#306) are meant to call
the same function. `Detection` is a frozen dataclass: `platform` (a slug, or
None), `how` (`"header"`, `"mapping"`, `"folder"`, `"conflict"`,
`"unsorted"`), what each source said (`header`, `folder` for the nearest
Platform name, `mapped`, `headers` when two disagree), the folder evidence that
decided (`place`, its path, and `place_platform`) and a one-line `reason` for
the UI.

Precedence is fixed and never guesses:

1. **Header.** Every registered tool whose `input_extensions` match the file is
   asked `header_platform(path)`. The default answers None; a tool that can tell
   its consoles apart from the bytes overrides it. chdman answers through
   `disc_id.disc_platform` (PS1/PS2 from `SYSTEM.CNF`, PSP from `PARAM.SFO`,
   Dreamcast from a `.gdi`'s `IP.BIN`, all the existing disc-ID parser, plus the
   Saturn / Sega CD / Dreamcast system id at the start of sector 0). dolphin
   answers through `disc_id.nintendo_disc_platform` (the GameCube magic at 0x1C,
   Wii at 0x18). xiso answers through `extract_xiso.detect_console` (the XDVDFS
   layout, or a trimmed image's boot executable). A probe that raises is
   treated as no answer. A new console is taught to detection by its tool's
   override, not by an edit here. Probes run on any file Auto is pointed at,
   including through an unauthenticated `/api/jobs/auto-plan`, so a probe
   must never let a size read from the image drive a read. The ISO 9660
   helpers in `disc_id` (`_bounded_read`) refuse any directory or file extent
   above `_MAX_ISO_READ` (1 MiB) or past the end of the image (a raw `.bin`
   reports its user-data size as `logical_size`), and a refused read is no
   header. The other probes read fixed-size windows, cap the sheet at 1 MiB
   (`utils.sheet_text`). The XDVDFS walk takes its limits as parameters:
   conversion and info keep `_MAX_DIR_TABLE` (64 MiB per table), while the
   probe (`detect_console`) passes 1 MiB per table, 4 MiB per walk and
   `root_only=True`, since the boot executable sits in the root.
   `tests/test_header_probe_bounds.py` probes images whose sizes are hostile.

   A folder is probed by the tools that take it as their input unit
   (`accepts_directory`): makeps3iso answers `ps3` for a PS3 disc folder, and
   `default_mode` sends a folder to the Platform's first folder-input mode
   (`InputKind.DIRECTORY`) whose tool accepts it, so a PS3 folder goes to
   `folder_to_iso`. The web UI offers a folder under Auto when the listing
   marks it convertible by a tool with a folder-input mode.
2. **The nearest folder that says** (`_place`). Walking up from the Image's
   folder, the first folder that either carries the user's mapping (#304,
   §3.3.16) or has a name that is a key or alias in `_ALIASES` decides. At the
   same folder the mapping wins. `_ALIASES` holds RomM's slug for each
   Platform a tool declares, with every other name for it (the tools' own
   spellings and the folder names ES-DE and Batocera use: `ps1`, `gc`,
   `megacd`, `pcenginecd`, `cdi`, `megadrive`, `mastersystem`, `n3ds`, ...).
   A mapping on `roms/` therefore names `roms/misc/x.iso` and leaves
   `roms/ps2/x.iso` to its folder name. Archive members (`a.zip::disc.iso`)
   skip the header and use the archive's folder. The walk runs over the
   resolved folders from the Image's own folder up to the root of the Volume
   that holds it, root included, and never above
   (`utils.path_utils.platform_boundary_root`, the deepest root; `_candidate_paths`), so a Volume
   mounted under `/mnt/md/...` does not make every Image in it a Genesis Image,
   and a mapping matches whether the Image was reached through a symlink or
   not. A path in no configured Volume uses only its own folder.
   That
   boundary is for Platform inference only: a lock walk (another process
   packing a folder, the delete/rename guards, the dispatch probe) stops at
   `lock_boundary_root`, the outermost root over the path with every
   overlapping root considered, so a folder locked above an inner Volume (with
   `/mnt` and `/games -> /mnt/MyGame/PS3_GAME`, a lock on `/mnt/MyGame`) is
   still found. Containment has a three-way form,
   `utils.path_utils.volume_containment(path)`: True inside, False only on
   positive evidence of outside (a `..` segment, a path under no configured or
   cached root spelling while every root's spelling is known, or freshly
   resolved roots that don't hold it), None whenever the gate gave up on an
   error instead (a root probe that failed or went over its bound, an OSError
   resolving the path, a root missing from the resolved-spelling cache; each
   is noted through `_note_unknown`). `is_within_configured_volumes` is that answer with None
   counted as outside; a caller that must not mistake a stalled mount for
   "outside" (the `.m3u` Referrer, §3.3.15) uses the three-way form.
   A containment check proves where a path pointed when it was checked, not
   where a later `open()` lands: anyone who can write the Volume can swap a
   component for a symlink in between. A file whose bytes a tool copies into
   its product is therefore read with `utils.path_utils.read_beneath(root,
   relative, max_bytes=)`, which opens every component, of the root from `/`
   and then of the relative path, with `O_NOFOLLOW` against the directory
   descriptor above it (Python exposes no `openat2`, and the walk also runs on
   macOS), requires a regular file (`fstat`), and reads only from that one
   descriptor. Any symlink, `..`, non-regular file or oversized file is
   refused. The eXoDOS repack reads its templates this way (§3.3.19).

Track sheets are read one way everywhere, through `utils.sheet_text`: as UTF-8
(a BOM stripped), as cp1252 when they are not valid UTF-8, and as Latin-1 last,
and the first spelling of a track name that exists beside the sheet wins. The
disc-ID probe (`disc_id._cue_track_paths`), the header-dependency list
(`delete_plan.sheet_track_paths`) and the delete plan all use it, so
`Jeu (Édition).bin` resolves, and is cached, from either encoding. There is one
CUE FILE-line parser (`sheet_text.cue_tracks`: quoted, unquoted, names with
spaces) and one GDI line parser (`sheet_text.gdi_tracks`, numeric fields only
when ASCII decimal).

There is also one track resolver, `sheet_text.resolve_track`, and
`sheet_tracks` built on it, used by the Platform probe, the detection cache's
track list (`header_reads`), the delete plan and the job gate. It refuses an
empty name, an absolute name, any `..` segment, and a name whose real path
lies outside the trusted roots: the configured Volumes by default, or the
roots a caller passes as `within` (the job worker's extraction dir for an
archived set; an empty `within` is refused). When any track of a sheet is
refused, or the sheet is unreadable (missing, or over `MAX_SHEET_BYTES`, read
as cap + 1 bytes and never parsed partially), `sheet_tracks` returns None and
the probe says nothing, so a planted sheet cannot make the probe read a file
outside the Volumes.

Sheets are read exactly the way chdman 0.285 reads them
(`src/lib/util/cdrom.cpp` at mame0285), because chdman opens the tracks
itself and any difference lets a name pass `resolve_track` while chdman opens
another. `fgets(buf, 511)` reads at most 510 bytes per line (`CHD_READ_BYTES`),
so a sheet with a longer line (a `REM` padded to hide a `FILE`) or a NUL byte
is unreadable. `chd_tokens` is chdman's `tokenize`: C-locale whitespace,
`"` and `'` each toggle a quoted run and are dropped, no comment syntax (a GDI
`#name` is a name). A CUE track is the token after an exact `FILE`; a GDI
track is a line's fifth token. `resolve_track` checks the name verbatim (no
trimming, `\` a filename character on POSIX), and every spelling of every
track is checked, including the raw-byte one (`os.fsdecode`) chdman opens for
a sheet that is not UTF-8: one refused spelling refuses the sheet. The delete
plan takes its targets from that native spelling (`sheet_native_text`), never
from a decoded reading, and a sheet whose decoded and native spellings name
two different existing files is ambiguous (`sheet_ambiguity`): unreadable for
every reader, so the job gate and the delete plan refuse it. So is a sheet
with a line whose quote never closes: chdman keeps the newline inside the
quote (`'pipe` opens `pipe\n`). A CUE `REM` line is exempt, since chdman reads
no file name from one.

The containment gate (`path_utils._confined_real_path`) checks a path's
lexical root inline, right before it resolves the path: first against the
configured root spellings (no filesystem call), then against the cached
resolved spellings of the roots (a Volume configured through a symlink). The
roots come from the deployment, so their resolved spellings are cached:
built at startup (`refresh_volume_roots`) and whenever the configured Volumes
change, refreshed in the background once older than `ROOT_REFRESH_SECONDS`,
each root resolved on its own thread with its own `ROOT_PROBE_SECONDS` bound.
A root that times out keeps its last good spelling. A cached spelling only
selects a root; granting access needs a fresh resolution of exactly that root
(`_fresh_root`, bounded by `ROOT_PROBE_SECONDS`, one probe in flight per
root), so a Volume symlink that was repointed stops covering its old target at
once, and a probe that times out refuses. The final check resolves the
submitted path itself and accepts a real path inside the freshly resolved
matched root, or inside another Volume that its cached spelling selects and a
fresh probe confirms, so a symlink from one Volume into another (Unraid
shares) is accepted and an unrelated stalled Volume is never waited on.

Special files are never opened. Opening a FIFO with no writer blocks forever,
which would strand the threadpool worker an Auto request runs detection on.
`path_utils.is_special_file` (`lstat`, a symlink judged by its target) is
asked at the top of `detect_platform`, before any header probe or open, so a
FIFO, socket or device input gets no Platform ("not a regular file or
folder"). `resolve_track` refuses a track that exists and is one of these, so
a sheet cannot point the probe at one either; a missing track is still not
refused.

Disc-ID tagging reads the same sheets after a conversion. `_process_job`
builds `within` once: None for a source in a Volume, `[temp_dir]` (the
`mkdtemp` root it extracted into) for an archive member, never the input
file's parent. It passes that one value to the job gate and to
`post_convert(input_path, output_path, mode, *, within=None)`, which every
plugin accepts. CHDMAN's hook hands it to `disc_id.extract_from_source`, whose
cue/gdi readers resolve tracks through `sheet_tracks(..., within=within)`.
When a refused or unreadable track stops tagging, the hook logs a warning
naming the sheet (`sheet_text.sheet_track_list` tells that case apart from a
disc with no ID); it never skips silently, and a refused track is never
opened. `header_reads` returns None for an unreadable sheet and
a failing `header_reads` is treated the same way, so neither is cached. A
folder Image (a PS3 disc folder) is never cached: its answer depends on files
below it.

Every answer goes through `canonical_slug`, which maps aliases, IGDB's slugs
and the tools' own vocabulary (`ps`, `ps1`, `gamecube`, `neogeocd`,
`genesis-slash-megadrive`) onto RomM's own slugs (`psx`, `ngc`, `neo-geo-cd`,
`genesis`). RomM's slug is its `UniversalPlatformSlug` value, which differs
from IGDB's for PlayStation and Genesis; `test_every_canonical_key_is_a_romm_slug`
pins every key to RomM's list. A test also fails when a tool declares a slug
the table does not cover, so a new Platform brings its folder names with it.
`platform_name(slug)` gives the display name from `_NAMES` (a test fails when a
slug has none). `detect_uncached` is the same decision without the shared LRU,
for a caller (the folder Library) that keeps its own per-file cache. Such a
caller reads the folder Platform mappings once (`mapping_snapshot()`) and
passes that one snapshot as `mapped=` to both `detection_key` and
`detect_uncached`: two reads could see a mapping change in between and cache
one answer under the other's key. `detect_platform` does the same for the
shared LRU (one read for the key and the decision). A header
that disagrees with the deciding folder, or two headers that disagree (kept
in `Detection.headers`), is a `conflict` with no Platform. Nothing at all is `unsorted`. Results are
cached per path in a bounded LRU and dropped when the file's size or mtime
changes; `clear_cache()` exists for tests. A header probe that reads other
files declares them through `ToolPlugin.header_reads(path)` (default empty;
chdman returns a `.cue`/`.gdi` sheet's tracks via the delete plan's own parser,
`utils.delete_plan.sheet_track_paths`). Their path, size and mtime join the
cache key, and while one of them is missing or unreadable (a `.bin` still
copying in) the answer is not cached at all.

**Auto targets** (`plan_auto`, `default_mode`). The candidates for an Image with
a Platform are the create-direction modes (`ModeKind.CREATE` / `COMPRESS`) that
claim the Platform (`registry.mode_platform_slugs`, or the mode's
`auto_platform_slugs`), may be an Auto default (`ModeSpec.auto_target`), and
whose command can read the file (`registry.mode_reads_extensions` plus the
tool's `converts_path`). Among them:

1. the most specific extension match wins (`.nkit.iso` over `.iso`, the
   `tool_for_verify` rule);
2. then the smallest read set, the most specific reader (a PS2 `.iso` goes to
   `createdvd`, which reads only `.iso`, not `createcd`, which also reads track
   sheets);
3. then registration order (`createdvd` before the CSO modes, which also read
   only `.iso`).

So a PS2 `.iso` gets `createdvd`, a PS2 or PS1 `.cue` `createcd`, a GameCube
`.iso` `dolphin_rvz`, a `.nkit.iso` `nkit_to_rvz`, a CD image in an `arcade`
folder `createcd`. `tests/test_auto_routing_table.py` holds the whole table, one
row per (Platform, readable extension), and fails when a tool or mode changes
the set of pairs.

`auto_route` wraps `default_mode` and leaves an Image alone when it is already
in its target's format: when the chosen mode's `output_ext` matches the input
name, the Image is skipped as "Already <FORMAT>" rather than re-encoded. It is
read off the spec, so it holds for any tool. Today the only such row is a
GameCube/Wii `.rvz` under `dolphin_rvz` ("Already RVZ"); a `.wia` or `.gcz`
still converts to RVZ.

**Apply recommended** (#541) reuses it for a whole Library:
`GET /api/libraries/{id}/recommended-rules` routes up to 25 games of each
console with games and no conversion Rule through `auto_route`, keeps the most
common mode, and drops one that `mode_is_automatable` refuses or whose tool is
not in `ready_tool_ids()` (that console goes to `skipped` with the tool's own
`not_ready_message`). It runs off the loop under the platforms route's bound
and saves nothing; the Automation tab merges `rules` into the Rules it holds
(`RulesSource.applyRecommended`, never over a console that has a target) and
saves them through `PUT .../rules`. RomM uses the same route with `id=romm`.
On the frontend every edit in the console table saves on its own:
`RulesSource.scheduleSave(delay)` (600 ms for typed fields) goes through the
existing one-at-a-time `saveRules` chain, and `flush()` sends a waiting edit
before Preview, Run now or turning automation on. A save sends only the
consoles changed since the last one (`{id: rule | null}`) to `PATCH
.../rules` (`editor.patch_rules`, one merge at a time, then `save_rules` with
all its checks), so a partial map can never delete the other consoles; the
table isn't drawn until the Rules have loaded. `PUT` still replaces the whole
set for the API. An edit still being checked (`hold(promise)`) is waited for by
`flush()`, and one the check refused (`markUnsaved(key, reason)`) makes it
refuse with that reason, so Run now never runs with a filter the screen
doesn't show. A name filter is saved only
once `POST /api/rules/check-pattern` says it compiles: the route runs
`rules.pattern_problem`, the one check `_valid_pattern` makes on every save
(Python's `re`, the 500-character cap, the backtracking guard), so the browser
never builds a RegExp from what was typed and never autosaves a filter the
save would refuse and pause the console for.

`ModeSpec.reads_extensions` is the mode's real input domain when it is
narrower than `input_extensions`; empty means "every input extension". It must
be a subset of `input_extensions`, which keeps gating manual selection and
`plan_job` exactly as before. chdman uses it because its five create modes all
accept `.iso/.cue/.bin/.gdi` by hand while the commands differ: `createcd`
reads a track sheet or a single-track `.iso`, `createdvd` a flat 2048-byte
`.iso`. A bare `.bin` is read by neither (chdman needs the table of contents),
so its `.cue` is the Image. `ModeSpec.auto_target` is false for chdman's
`createraw`, `createhd` and `createld`: they claim the arcade Platforms, but
are never the right default for an Image.

`ModeSpec.auto_platform_slugs` lets a mode be a Platform's Auto target without
being offered for it by hand. `createcd` declares `ps2`, because PS2 CD-ROM
titles are plain Mode 2 CDs kept as cue/bin. Adding `ps2` to its
`platform_slugs` would have made `createcd`, the first chdman create mode, the
default of every platform-narrowed PS2 panel and rule, which
`test_chdman_media_commands_are_narrowed_per_platform` forbids.

Two Images whose targets would write the same output (`game.iso` and
`game.nkit.iso` both to `game.rvz`, or two `game.iso` into one output
directory) cannot both convert. `plan_auto` keeps the one the batch path would
keep (`output_conflicts.input_priority`: `.cue`/`.gdi` over `.iso` over `.bin`,
then the earlier in the selection) and skips the other with a reason naming the
output. The compression is the saved per-tool value (the `conversion`
preference) when the mode takes one, else the tool default; a saved value the
mode would refuse also falls back to the tool default. Conflicted and Unsorted
Images get a `skip_reason` ("Console unclear: …", "Unknown console: …"), and so
does an Image with a Platform but no target: "No converter here handles
<console> games" when no mode serves the Platform at all (Neo Geo Pocket Color,
WonderSwan Color), "No converter here reads <ext> files for <console>" when the
file is the problem. The reasons name the console by its display name.

**A target and a picked console (restructure 2/6).** `plan_auto(paths, *,
target=None, platform_overrides=None, …)` is the one planner for the Convert
panel and the CLI's `--to`. `route_for(path, detection, target, registry)`
makes the decision per Image: with no target it is Auto (`auto_route` for a
decided Platform, skipped otherwise); with a target (a format or a mode id) a
Conflicted Image is still skipped and any other goes to `format_route`, so an
Unsorted Image goes ahead only to a mode that reads it. `platform_overrides`
maps a path to the console the user picked; it replaces only a detection that
decided nothing (`how = "chosen"`, checked with `known_slug`). Each planned row
carries `output_name`, the basename of the file its mode would write.
`POST /api/jobs/auto-plan` and `POST /api/jobs/auto` take `target` and
`platform_overrides`, so the queue replans exactly what was shown. The Auto
route also takes the panel's `verify_after`, `delete_on_verify`,
`container_only` and `split`, and `_auto_group_request` gives each mode group
only what it can honour: delete (and the quick check with it) where
`supports_delete_on_verify`, the check where `mode_supports_verify`, the split
for a folder mode. `prepare_batch` validates and snapshots every group as a
hand-picked batch, so no check is ever weaker than the mode's own.
`BatchJobCreateRequest.verify_after` reaches `create_jobs_atomic`.

A target whose tool cannot run here is skipped too. The route awaits the
shared, bounded `registry.ready_tool_ids()` and passes it to `plan_auto`
(`ready_tool_ids=`), which skips the Image with
"<display_name> isn't ready: <not_ready_reason>" (`not_ready_message`, §3.4). `ToolPlugin.not_ready_reason`
says why in the user's words; the default names a missing program, nsz names
`prod.keys`, jwud the JWUDTool launcher and Java. Both Auto routes plan through
this, so the plan the user confirms and the jobs queued agree.

From a RomM platform listing, Auto records RomM metadata for re-pinning. The
Convert panel has one submit, `conversion.submitAuto`, through
`conversion.#queueWithRepin`, which calls `planRommRepin` once per target mode
before queueing, then reconciles the records against the jobs actually created
(redirected outputs re-recorded under their own mode, rows for sources that
never became jobs retired).

`POST /api/jobs/auto-plan` returns the plan without queueing.
`POST /api/jobs/auto` groups the planned Images by (mode, compression). The
batch route is split into `prepare_batch` (validate and `plan_job` every file,
queue nothing) and `queue_prepared` (one atomic `create_jobs_atomic` call);
`create_batch_jobs` is the two in sequence. It answers the job list, or with
`report_skipped=true` a `BatchJobsResponse`, `{jobs, skipped: [{path, reason,
code}]}` (#504). The flag keeps the bare list for every client written before
it (the API's users, scripts); the SPA sets it and its submit toast names the
first three reasons and counts the rest. Every file `prepare_batch` leaves out
is a `BatchSkip`: a `plan_job` skip becomes one through `BatchSkip.of(path,
skip)`, whose `reason` is `SkipFile.message` (the tool's `detail`, else the
single-job text) and whose `code` is `SkipFile.code` (the `SkipReason` value),
and a file whose output another file of the batch writes is `same_output`. So a
new `SkipReason` (a tool that isn't ready, #505) reaches the batch report, the
CLI and Auto with no change to the batch code. `plan_job` stats a Source
only through `_is_file_within_volumes`, which confines it with
`ensure_path_within_volumes` first: a path outside every Volume is refused
(`SOURCE_OUTSIDE_VOLUMES`, 403 for a single job) before any filesystem call,
and only the confined path is stat'ed. Auto prepares every group first, so
a planning error queues nothing, then checks the whole batch against
`max_queue_depth`, then queues the groups. Each group is its own queue call.
A group the queue refuses whole (503 when a volume stops answering, or an
unexpected exception, logged with its traceback) reports each of its files as
`queue_failed` and the other groups stay queued (#520); only a cancelled
request (a client disconnect) withdraws what it queued (`_cancel_queued`).
`/jobs/auto` queues only the plan the user confirmed: the client sends the
confirmed plan's actionable rows as `items: [{file_path, mode}]` (an Image the
preview skipped is never sent), and the route replans those paths. Each item
whose target is unchanged is queued; one whose target changed (another tool,
or now skipped) is not, since queueing an Image the user never saw would also
skip its RomM metadata snapshot. `AutoBatchResponse` carries `skipped:
[{path, reason, code}]` like the batch route: `plan_changed`, `queue_failed`,
`output_claimed` and every `prepare_batch` skip, and each skipped file's plan
row has `mode: null` and the reason. The SPA's shared submit toast names them.

**One claimed destination skips one file (#520).** The locked reservation
(`_reject_claimed_destinations_locked`) still never lets two live jobs write
one file. `create_jobs_atomic(..., skipped=[...])` leaves a spec whose
destination (or a sidecar) a live job or an earlier spec holds out of the
batch, appends `(file_path, reason)`, and queues the rest; `queue_prepared`
passes the list and reports each as `output_claimed`, so the batch route, Auto
and the CLI skip that file rather than failing a Select-All of thousands over
it. Without the list (the sweep, the Inbox, a single job) the call stays
all-or-nothing and raises `OutputClaimedError` (409 for a single job). Nothing in either Auto endpoint, or in the
batch preparation they share, touches a Volume on the event loop: the per-path
Volume checks run together with planning (`_checked_auto_plan`) or on their
own (`_check_within_volumes`) through `routes.volume_guard.off_loop_bounded`,
a `run_detached` call bounded by `VOLUME_PROBE_TIMEOUT_SECONDS` (30 s, beside
`ROLLBACK_WAIT_CAP_SECONDS` in `job_manager`). Past the bound the request
answers 503 "A Volume isn't responding"; the abandoned thread may linger until
the mount answers, holding no shared pool worker, and its result is dropped.
That helper is the one copy of the rule for every route (#327): single-job
create, the delete plan, check-duplicates, `plan_job`'s same-path check, and
the file browser's listing, search and archive routes run their containment
and `realpath` work through it too. `tests/test_routes_off_loop_327.py` walks
every `async def` in `app/routes` and fails on a containment or `realpath`
call made on the loop rather than handed to a thread.
An Image that preparation skipped (its output
appeared after planning) is marked skipped in the returned plan with
preparation's reason. In the browser, `conversion.runAuto` runs the flow once
at a time (an in-flight guard), on the selection and RomM context captured
before its first await, and updates the re-pin badge whether queueing
succeeded or not. It takes the caller's AbortSignal: `ConvertPanel` aborts it
in `onDestroy`, so leaving the view mid-plan aborts the plan request, never
opens the modal afterwards, resolves a pending confirmation as cancelled, and
clears `autoInFlight` (the store stops waiting at the abort even when a request
ignores the signal, and including a RomM snapshot that stalls: its rows are
retired on their own when it returns, never touching a newer run's state);
`jobs.adopt` applies the same freshness rule as `refresh()`
(`snapshotIsNewer`) and never adopts an unknown job the store saw evicted
from history or removed by Clear completed (bounded tombstones, one helper),
so a response built before a job finished and was pruned or cleared cannot
put it back in the queue. That holds only while
the job's tombstone is kept: the store keeps the newest 2000, so a response
delayed past that many evictions can still restore the job. `refresh()` infers
an eviction (and tombstones it) only for jobs it knew before it sent the
request; a job SSE created during the request is newer than the snapshot and
is kept. The rollback (`_cancel_queued`) asks every queued job to
cancel, then waits for them to settle with `JobManager.wait_until_terminal`,
for at most `CHD_PROGRESS_TIMEOUT` or `ROLLBACK_WAIT_CAP_SECONDS` (30 s),
whichever is shorter; the cap holds when the timeout is 0, since the wait runs
inside the HTTP request. `cancel_job()` returning True for a
processing job means only that its cancel event is set, and a worker past its
last check (in `post_convert`) still completes, so the retained list is built
from the real final states: completed with an output, or still running at the
bound. The error detail is then structured, `{"message": "... Kept (finished
or still running): ...", "jobs": [...]}`, with `jobs` in the shape a success
returns. `client.fetchJson` keeps a structured detail on `err.detail`, the store
adopts those jobs and reconciles its re-pin snapshots against them before it
retires the rest, so a kept output keeps its record. Auto never deletes a
Source (no delete-on-verify), so nothing is lost either way.

An explicit target choice wins over Auto. Every mode selector (the convert
panel's dropdown, RomM's "Convert to") calls `conversion.chooseTarget(mode)`,
which sets the mode and, if Auto was on, turns it off with an "Auto is off"
notice. `setMode` stays the programmatic path (platform adoption, tool
switches) and leaves Auto alone.

The legacy UI's "ISO handling" toggle is gone. It lived only in the browser,
under the `primary_tool_preference` key that now names the selected tool tab;
its values were tool ids, so a value left from it still loads as a tab.

### 3.3.15 Referrers (`services/referrers/`)

A Referrer is anything that points at an Image by its path: an `.m3u`
playlist, an EmulationStation frontend's `gamelist.xml` (#386), and RomM's
ROM (the swap, #391). When
delete-on-verify replaces a Source, every Referrer is moved to the output
**before** the Source is removed. What is guaranteed: every reference a
registered Referrer finds is either rewritten to the output before the Source
goes, or the Source is kept. A reference no Referrer looks at is not covered;
for `.m3u` that is a playlist outside the replaced file's folder and its
ancestors up to the Volume root (a sibling folder's `../other/Game.cue`, or
another Volume). Referrers are plugins on one contract, registered like tools,
and the job pipeline never branches on which ones exist.

```python
class Referrer(ABC):                          # services/referrers/base.py
    id: str                                   # stable; lands in the Ledger
    label: str
    opt_in: bool                              # False: always on; True: per Library (#383)
    phase: Phase                              # Phase.FILE or Phase.NETWORK (#383)
    def detect(self, image_path) -> bool      # could it point at this Image?
    def plan(self, renames) -> list[ReferrerChange]   # writes nothing
    def apply(self, change) -> Applied | None # atomic, one-time backup; raises ReferrerError
                                              # always synchronous
    async def apply_async(self, change) -> Applied | None  # the async form (#391);
                                              # default: apply() on a worker thread
    def places_output(self, image_path, renames) -> bool  # default False (#391)

@dataclass(frozen=True)
class ReferrerChange:
    referrer: str                             # Referrer id
    target: str                               # the file/record rewritten
    edits: tuple[tuple[str, str], ...]        # (old, new) as shown to the user
    payload: Any                              # Referrer-private, never serialized
    summary: str | None                       # one line for the confirmation (#391)
    record: Mapping | None                    # extra fields for its Ledger record (#391)
    # to_dict() adds "entries": len(edits), for "gamelist.xml, 3 entries" (#386),
    # and "summary" when set

class ReferrerError(Exception):               # target, reason, kept=None (#391)
@dataclass(frozen=True)
class Applied:                                # an apply's return (#391)
    removed: tuple[str, ...]                  # Source files it removed itself
    note: str | None                          # what the job and the Ledger say
@dataclass(frozen=True)
class StagedPlacement:                        # the job's still-staged output (#391)
    staged: Mapping[str, str]                 # {final output path: staged path}
    place: Callable[[], Awaitable[None]]      # the job's _place_staged
```

`renames` is `{old_path: new_path}`: every file the delete will remove, mapped
to the output that replaces it (a `.cue` and its `.bin` tracks all map to the
`.chd`; an archive maps to the output extracted from it).

**The Library, opt-in and phases (#383).** Both entry points take the Image's
Library: `libraries.library_for(path)` returns the Library whose root holds
the path, or None. Each root is resolved once (`_canonical_root`) and cached,
so a lookup doesn't resolve every Library's mount; the cache is dropped
whenever RomM's settings are saved (`romm.settings.save_generation()`), since
its root, or where its symlink points, may have moved. With no database open
only RomM is asked. Callers run it off the loop, bounded: the job through
`JobManager._image_library` (a delete-step probe, so a lookup that times out
or fails refuses the delete and removes nothing), the delete-plan route
through `off_loop_bounded` (a failure becomes the item's "the source will be
kept" warning).

**Which Library: the path as listed (#413).** A folder Library's system folder
can be a symlink that leads outside its root (`ROMs/psx -> ../esde_real/psx`).
A file reached through the Library's folders is covered by the Referrers that
Library opted into, wherever it lives, so `library_for` looks two ways:

- *By listing:* the path as the caller was given it, made absolute with `.`
  and `..` folded but never resolved, against each root as stored and as
  resolved (so a path spelled through a symlinked `ROMM_LIBRARY_ROOT`, or
  through its target, matches).
- *By location:* the path resolved against each resolved root, as before
  (codex 4588646131), so a file reached from outside every Library through a
  link into one still belongs to that one.

Both agree, or only one finds a Library: that Library. They name two
different Libraries (`A/psx -> B/psx`): it raises `AmbiguousLibrary` rather
than pick one. Callers pass the job's paths as the job was given them (the
Source, the output, a Referrer target's folder), never resolved first. The
rule is fail closed: a file a Referrer opted into on a Library would cover is
never removed without that Referrer running. A lookup that can't tell keeps
the Source: the job's `_image_library` and the delete plan's lookup turn
`AmbiguousLibrary` into `referrers.UndecidedLibrary(target, reason)`, for which
`registry.active` raises `ReferrerError` (kept wording "couldn't tell which
Library holds it"), so the hook keeps the Source and the job completes with
"converted, source kept: couldn't tell which Library holds it (...)", the
confirmation says the Source will be kept, and `places_output` answers False.
The staging check (`_needs_stage`) stages on it, and a Referrer's temp-file
lookup (`base._hidden_slot`) fails its write. Finding the Library decides only
which Referrers run, never what may be written: containment stays on resolved
paths. A job whose Source leads out of every Volume is refused before it is
queued (`_check_job_paths`), each Referrer checks its own targets
(`volume_containment`, `rewrite_in_place`), and the Platform folder is still
found on resolved paths (`folder_holding`), spelled as listed. A RomM ROM
reached through such a link has no record `RommClient.local_path` maps inside
RomM's root, so the swap finds no ROM and doesn't apply, as before. A chain of
links that reaches a Library's folder from outside it and then leaves again
(`links/psx -> ROMs/psx -> elsewhere/psx`, listed as `links/psx/...`) is in
neither Library by listing or location; the frontend doesn't list it either.
`registry.active(library)` picks the Referrers that run: every one with
`opt_in = False`, and each `opt_in = True` one whose Library says
`referrer_enabled(referrer_id)`. That method is on the `Library` contract
(`rules/library.py`); `LibraryDefaults` answers False, so no opt-in Referrer
runs until a Library's settings turn it on, and an Image in no Library gets
the always-on ones only. It reads settings already in memory; if it raises,
the hook fails with the Library as its target and keeps the Source (fail
closed). `register` refuses a Referrer that doesn't declare `phase` (a
`Phase`) and `opt_in` (a bool). `active` sorts by `phase`, keeping
registration order within a phase. `Phase.FILE` (`.m3u`,
`gamelist.xml`) runs before `Phase.NETWORK` (the RomM swap): the hook runs one
phase at a time, and a later phase runs only when every Referrer of the
earlier ones succeeded. Its Referrers are not even planned; each gets a
`skipped` record. `apply` is always synchronous, on every Referrer. A network
Referrer also overrides `apply_async` (#391), the contract's async form,
whose default runs `apply` on a worker thread (`asyncio.to_thread`, so the
caller's context and its guard come along). `Referrer.applies_async()` says
whether a Referrer overrides it, and the hook asks that, never the
signature. A phase's plans and its other applies run on the detached thread
as before. Its async applies (`apply_async`) wait in `ReferrerOutcome.pending`
and run after the thread returns, on the event loop, one at a time
(`_await_bounded`): each is raced against the job's `cancel_event` and
bounded by the hook's bound (`bound`, else `conversion_stall_bound()`). Past
the bound or on a Cancel the apply's task is cancelled, then awaited for up to
`CANCEL_GRACE_SECONDS` (5 s) so its cleanup can finish; it fails with "did not
answer within Ns" or "cancelled", which keeps the Source. One still running
after the grace is treated like an abandoned mutation
(`run_detached_mutation`): its record is `abandoned`, the task goes to
`ReferrerOutcome.lingering` (kept referenced until it ends, and its end, an
exception included, logged), and the job hands it its delete holds and
travelling locks through `ReferrerOutcome.outlive(release)`, which lets them
go from the last lingering task's done callback, never earlier. A network
Referrer therefore never holds
the loop: its blocking work goes in `plan` or `apply` (the thread), and its
network calls in `apply_async` (the loop). Its own `apply` stays a working
synchronous form for a caller off the loop: the RomM swap's runs
`apply_async` on a loop of its own (`asyncio.run`).

The package exposes the registry and two entry points:

- `plan_changes(image_path, renames, library)` runs `detect` + `plan` on every
  Referrer `registry.active(library)` returns, in phase order. Both entry
  points ask `detect` about the Image *and*
  every path in `renames`, so a companion that is deleted too (a cue's track
  in a subfolder) gates planning as well. `POST /api/jobs/delete-plan` calls it
  per item and returns `referrer_changes: [{referrer, target, entries, edits:
  [{old, new}]}]`, which `DeletePlanModal.svelte` lists under each source as
  "updates gamelist.xml, 3 entries: old → new". The request
  carries `output_dir` and `duplicate_action`; the planned output goes through
  `_renamed_output`, the same helper `plan_job` uses, so a RENAME preview names
  the numbered path the job writes. The route builds its plan with
  `build_delete_plan(..., confine_to_volumes=True)`: a track that resolves
  outside every Volume (through a symlinked folder) goes to `unsafe_paths`,
  blocks the item, and never reaches `delete_paths` or the Referrer scan. The
  preview plans through `plan_changes_bounded`, under the same detached,
  no-progress bound as the hook, so a playlist on a mount that stopped
  answering can't hang the confirmation. A plan that can't be made (or times
  out) becomes a warning on the item ("the source will be kept").
- `update_before_source_removal(image_path, renames, library=...)` is the
  **one hook** in `_process_job`. It runs after `_delete_check` (§3.3.12) has
  passed and after the delete plan's safety checks, immediately before the
  `os.remove` loop. Phase by phase, off the event loop, it plans every active
  Referrer of the phase first, then applies each change; the first failure
  stops it, and it never raises. It returns a `ReferrerOutcome` whose
  `records` hold one entry per target, `{referrer, target, status}` with
  `status` `updated`, `failed` or `skipped` (not attempted after a failure),
  `entries` (how many entries the change moved, #386) for an updated one and
  a `reason` for the others. The records are copied to
  `ConversionJob.referrers_updated` and the Ledger's `referrers_updated` JSON
  column, so a row says which Referrer kept the Source and why. On a failure
  the job skips the delete, keeps the Source and the output, ends `completed`
  with the message `converted, source kept: couldn't update <file> (<reason>)`,
  and `deleted_paths` stays empty. `JobRow.svelte` shows that message on a
  completed delete-on-verify job that deleted nothing. Changes already applied
  before the failure stay: they point at the output, which exists.
  The hook runs on a detached thread (`run_detached_within_stall_bound`, the
  conversion's no-progress bound) and takes the job's `cancel_event`, so a
  playlist on a mount that stopped answering can't hold the job slot and its
  locks: on a timeout or a Cancel the thread is abandoned and the outcome keeps
  the Source with the reason, recording one `failed` entry per Referrer of that
  phase (target the Image; the outcome's target is the phase's
  `Phase.target`, "playlists and gamelists" for the file phase) so the
  Ledger row says why.
  An abandoned thread can't write late:
  `_update_sync` runs under a `base.WriteGuard` holding an `abandoned` event
  (set by the hook on a timeout or Cancel, on any failed outcome, and when the
  hook's own awaiter is cancelled mid-phase, before the cancel propagates) and
  each output's identity (`st_dev`, `st_ino`, size, `mtime_ns`) captured when
  the phase started. The guard lives in a `contextvars.ContextVar`
  (`base.write_guard`), not thread-local state, so it reaches both kinds of
  apply: the worker thread sets it for its plain applies, and each async
  apply's task runs in `base.guarded_context(guard)`, so a `check_write` after
  an `await` refuses an output replaced meanwhile, as the thread's does.
  `apply` passes `base.check_write` as the `guard` of `atomic_write_bytes`,
  which runs it under the playlist's lock after the temp file is written and
  synced, immediately before `os.replace` publishes it (backup and playlist).
  A refusal removes the temp file and publishes nothing, so a write that
  stalled in fsync while the job let go, or another conversion replaced the
  output, never lands.
  Because the hook awaits a worker thread, the job re-asks two things after
  it and before `os.remove`: a Cancel pressed meanwhile keeps the Source
  ("Delete skipped (cancelled)"), and `JobManager._check_delete_paths` (lock,
  Volume, other job, symlink, type, not-the-output, snapshot size / mtime /
  inode / device) runs a second time; it ran once before the hook. A failure
  the second time keeps the Source with `converted, source kept: <reason>`,
  and since that check awaits as well, cancel is asked once more after it,
  immediately before the removal loop.

`atomic_write_bytes(path, data)` (in `base.py`) is the shared writer for file
Referrers and anything else that writes a playlist: a temp file in the same
directory, fsync, `os.replace`, keeping the old file's permission bits and
group (and its owner when running as root). Readers that reach a shared
playlist through its group would otherwise lose access, so a group that can't
be set on the temp file aborts the write (`PermissionError` "can't keep its
group"), which keeps the Source. A crash leaves the old file or the new one,
never half of one.

**The temp file in a RomM Library (#404).** RomM indexes dot-prefixed files, so
a scan between the temp file's write and its rename could list it. Both writers
make their temp file through `base._temp_file(directory)`, which asks
`libraries.library_for(directory)` (by listing, then location: #413; one it
can't decide fails the write): a Library declaring `HIDDEN_STAGING` (RomM)
gets a slot from the existing `staging.open_slot` (section 3.3.21) and the temp
file is made in it, then renamed (or no-replace placed) into the Platform
folder, which is one filesystem, so the rename stays atomic. The slot is closed
with the write, success or not. A Folder Library, or a path in no Library, keeps
the temp file beside the target as before, and a slot that can't be made fails
the write rather than falling back to a visible temp file. The `.bak` copy goes
through the same writer, so it is hidden too; the one-time rule is unchanged.

`write_new_bytes(path, data)` is its no-overwrite sibling: the same temp file
and fsync, then `utils.path_utils.place_without_clobbering` (a hard link or a
no-replace rename), so "is the name free" and "create it" are one atomic step and
a file or symlink already at `path` raises `FileExistsError` untouched.
`m3u.playlist_bytes(entries)` builds a fresh playlist in the encoding the
Referrer reads and rewrites with. **Create an `.m3u` from selected discs** (#299,
`services/disc_playlist.py`, `POST /api/files/playlist/preview` and
`/api/files/playlist`) is built on these two, not a second writer. It accepts two
or more files directly in one folder inside a Volume whose extension is in
`registry.disc_extensions()`, the union of each tool's `disc_extensions` (chdman
`.cue .gdi .iso .chd`, Dolphin `.iso .gcz .wia .rvz .wbfs`, maxcso `.cso .zso`; a
tool with no standalone disc files declares nothing). `registry.js` mirrors it as
`discExts` (checked by `tests/test_frontend_parity_186.py`), and the file list
shows **Create .m3u** only for two or more such files in one folder. Order is by
`(Disc N)` / `Disc N` / `CD N`, then name. A ticked `.bin` that a ticked `.cue` /
`.gdi` names is dropped from the selection with a warning; two files for one disc
(a `.cue` and its `.chd`, or one disc number twice) are refused. Entries are file
names beside the playlist. The preview computes a free name (`<Title> (2).m3u`);
the create call writes exactly the previewed name and answers 409 with the next
free one if it was taken since, never picking a different name on its own.

**Shared file-Referrer helpers** (`files.py`, #386). What every Referrer
that is a text file inside a Volume shares, so `.m3u` and `gamelist.xml`
don't each carry a copy: `Renames.of(renames, name_forms)` (each replaced
file's identity and target, plus the folded names for the fail-safe check),
`search_dirs` / `dirs_for` (the replaced file's folder and each one above it
up to its Volume root), `points_at` (does a written entry reach the output:
identity when the output exists, resolved path while planning before it does),
`fold` / `named_file`, `may_read` (planning refuses a symlink and "couldn't
tell" containment, and skips a file confirmed outside the Volumes),
`target_lock` (the in-process lock per resolved path) and `rewrite_in_place`,
the one `apply`: it refuses a symlinked, read-only or not-confirmed-inside
file, takes the in-process lock and the cross-process
`lock_manager.referrer_lock` (bounded wait, ending as soon as `check_write`
refuses), re-reads the file, runs the Referrer's rewrite on what is there now,
writes `<file>.bak` once and the new file through `atomic_write_bytes` with
`check_write` as the publish guard. A Referrer supplies only its reader and
rewrite.

**The `.m3u` Referrer** (`m3u.py`) is always on (`opt_in = False`), in
`Phase.FILE`, with a plain (thread) `apply` (`files.rewrite_in_place`).

- *Where it looks.* The replaced file's resolved folder and each folder above
  it up to and including its Volume root (`utils.path_utils.lock_boundary_root`,
  with nested Volumes the outermost one wins, so `/roms/All.m3u` is found for
  a disc under a `/roms/psx` Volume). Only the `*.m3u` files directly in those folders,
  no recursive walk, so the cost is one `scandir` per level. This covers a
  playlist beside the discs and the Batocera / ES-DE layout (`psx/Game.m3u`
  naming `.discs/Game (Disc 1).cue`). `detect` looks at the same folders.
  If the boundary can't be determined the Referrer fails (Source kept). Each
  playlist found is checked with `volume_containment`: confirmed outside is
  left alone, "couldn't tell" fails (Source kept); `apply` checks again right
  before writing and refuses unless confirmed inside.
- *What it rewrites.* Only entries whose path (as the reader below reads it)
  resolves exactly to a replaced file, matched by `(st_dev, st_ino)` so any
  spelling that reaches the file counts (relative, absolute, `./`, other case
  on a case-insensitive volume). Every file comparison in the Referrer (entry
  to replaced file, entry to output, the write guard's output check) goes
  through `utils.path_utils.same_identity`: the same size and `mtime_ns`
  always (so a file rewritten in place, same inode with new bytes, is not
  the file that was checked), and then the same `(st_dev, st_ino)` or the
  same resolved path. FUSE/SMB mounts such as Unraid's shfs user shares can
  report a new `st_ino` for the same file on each stat, and can reuse
  inodes, so the inode alone never decides. A `path|label` entry and a quoted path are
  rewritten with their label and quotes kept. Everything else is kept byte
  for byte: `#` lines, other entries, blank lines, line endings and a UTF-8
  BOM.
- *Not valid UTF-8* (Windows-1252, Latin-1). The file is read as cp1252 only
  to look for a replaced file's name; if any entry could name one the Source
  is kept ("playlist isn't UTF-8 and may name this disc"). Otherwise it goes
  through the normal rewrite, and `surrogateescape` decoding keeps its other
  bytes as they were.
- *Fail safe.* An entry that does not resolve but whose file name matches a
  replaced file's name (splitting on `/` or `\`, ignoring case, both sides in
  Unicode NFC; the names are also compared trimmed of C-locale spaces) is not
  guessed at: `plan` raises `ReferrerError` naming the playlist and line
  (`line 2 names game.cue but doesn't point at it`), so the Source is kept.
  That catches absolute paths in the emulator's own path space, mixed
  separators and case differences on case-sensitive volumes. An unresolved
  entry for some other file does not block anything. For a labelled or quoted
  line, two other readings are also checked: the whole trimmed entry (a plain
  line reader with no label split or unquoting) and the label-split path with
  its quotes kept (RetroArch's `rm3u.c`). If either reaches or names a
  replaced file (a Source literally named `A|B.cue` or `"Game.cue"`) the
  Source is kept.
- *UTF-16 with a BOM* (#334). `FF FE` or `FE FF` names the byte order. The
  text after it is decoded strictly, rewritten and read back exactly like a
  UTF-8 playlist (`_rewrite_utf16` transcodes to UTF-8, runs `_rewrite`, and
  encodes the result back), then written with the same BOM and byte order.
  Line endings are part of the text, so they survive too. The UTF-8 stand-in
  gets a BOM of its own, so `_rewrite` strips exactly that one and a first
  filename that itself starts with U+FEFF keeps it. One rule covers readers
  that ignore the BOM (an 8-bit line appended to the file): the bytes that
  would be left on disk, rewritten or not, are read back as the UTF-16 text
  and as the NUL-stripped bytes in UTF-8 and cp1252. Every reading goes
  through `_text_reaches_replaced`, which runs each entry line through
  `_entry_reaches_replaced`: the one check that `_rewrite` also uses for its
  own lines. It asks whether a line reaches a replaced file by any reading a
  player could use: the parsed path, the literal line, the label-split path
  with quotes kept, and an unresolved name. If any reading still reaches a
  replaced file, or a new entry can't be encoded in UTF-16, the Source is
  kept. Text that doesn't decode strictly falls to the rule below.
- *UTF-16 without a BOM, and NUL bytes.* Such a playlist is never rewritten:
  without a BOM the encoding can't be known safely. It is UTF-16
  only with a BOM, or when NULs fill most of the odd (little-endian) or even
  (big-endian) byte positions, and then it is decoded strictly (or with
  replacements if that fails). The NUL-stripped UTF-8 and cp1252 readings are
  always checked too, BOM or not: the parity guess can be wrong (an 8-bit
  file with a zero-filled tail), a file can mix content (an 8-bit line
  appended to a UTF-16 one), and they are what a line reader sees. Both
  UTF-16 byte orders are read too (with replacements), since long non-ASCII
  names have too few NULs for the parity guess. If any
  reading contains a replaced file's name the Source is kept, and the reason
  names the reading that matched (`UTF-16` or `NUL bytes`); otherwise the
  playlist is left alone.
- *Entry style.* The new entry is the entry's own folder part plus the path
  from the replaced file's resolved folder to the resolved output, so both
  sides use the same path form (a symlinked folder does not turn `Game.cue`
  into `../link/Game.chd`), `./` and absolute prefixes are kept, and a
  backslash entry stays backslashed. When the output is in another folder the
  normalised form is tried first, then the literal one (`link/../out/G.chd`
  still resolves through a symlinked `link`).
- *One reader.* `_read_line(raw_line, first)` is the only parser, modelled on
  C byte readers (dropping quotes is what some readers do; RetroArch keeps
  them, which the fail-safe readings above cover): lines split on CR/LF bytes only; a UTF-8 BOM
  dropped on the file's first line; only the C-locale spaces (space, tab, CR,
  LF, VT, FF, as libretro's `ISSPACE`) trimmed
  (NBSP, U+3000, U+2028 and the like stay part of the name); blank and `#`
  lines skipped; a `path|label` split at the first `|`; quotes around the
  path dropped; a slash-free entry with backslashes read as
  backslash-separated. It returns the path plus the text before and after it,
  so a rewrite keeps the label, quotes and spacing. Planning, rewriting,
  `entries(data)` and every read-back check call it.
- *Choosing what to write* (`_choose_entry`). Each candidate is tried as
  written in the entry's style, then with a `./` prefix, then (for a
  backslash entry) in forward slashes. A form holding a control character (CR,
  LF, NUL, VT, any Unicode `Cc`) is skipped. Each remaining form is rendered
  into its full line (BOM prefix, the entry's own text before and after the
  path: whitespace, quotes, label) and read back with `_read_line`, so a `|`
  or a quote in the path reads back wrong and can't be written (the Source is
  kept). The first whose path, read from the playlist's folder, reaches the
  output is used: same `(st_dev, st_ino)`. Only the delete confirmation, which
  plans before the output exists, falls back to comparing resolved paths;
  `apply` requires the output to exist, so a late, abandoned apply can't point
  a playlist at an output removed meanwhile.
  So `./` protects a leading `#`, whitespace, a U+FEFF that a reader would
  take for a BOM, and a literal backslash in a Linux file name. If no form
  works (a trailing space, a line break), `plan` raises and the Source is
  kept.
- *Read-back of the whole file.* After rendering, the bytes about to be
  written go through `entries()`: the entry count must be unchanged, every
  untouched entry must read back as it was, and every rewritten entry, at its
  position, must reach the output. Anything else raises and keeps the Source.
- *For new playlists.* `entry_for(playlist_dir, path, first_line=...)` uses
  the same `_choose_entry` and reader, so code that creates a playlist
  (#299) encodes entries exactly as the Referrer reads them, or gets `None`
  for a path that can't be one entry.
- *Concurrency.* Each playlist (by resolved path) has a `threading.Lock`
  for threads in one process, and `apply` also holds the cross-process
  Referrer lock, `lock_manager.referrer_lock(path)`: the same lock dir and
  real-path keying (`os.fsencode(realpath)`) as every other lock, but its own
  kind of lock file (`referrer-`, not `filelock-`), so it never meets an
  output, input or folder lock on any path, and its holder waits on nothing
  else. Both are held while `apply` re-reads the playlist, re-runs the rewrite
  on what is there now and writes, so two jobs updating one playlist both
  land, in one process or two (a CLI run beside the server, §3.3.17; codex
  4577804154), and an edit made after planning is kept. The wait for the
  Referrer lock is bounded (`PLAYLIST_LOCK_WAIT_SECONDS`, 30 s) and, inside
  the hook, ends at once when the job stops waiting (`check_write` is asked
  before every attempt). Still held then, the Referrer fails with "is being
  updated by another job" and the Source is kept, with that reason in
  `job.warning`.
- *Writing.* `plan` refuses a symlinked playlist before reading it (it may
  lead out of the Volumes), which keeps the Source; `apply` refuses a
  read-only or symlinked one. It writes `<name>.m3u.bak` once, only if no
  backup exists, with the playlist's own mode and group
  (`atomic_write_bytes(..., like=playlist)`; if the playlist vanished
  meanwhile the write fails rather than fall back to 0644, and the Source is
  kept), then the new playlist.

**The `gamelist.xml` Referrer** (`gamelist.py`, #386) is opt-in
(`opt_in = True`), in `Phase.FILE`, with a plain `apply`
(`files.rewrite_in_place`). EmulationStation frontends (ES-DE, Batocera,
Knulli) key each game's artwork, favourite flag and play count on its
`<path>`. muOS reads no gamelist (its catalogue is keyed by game name).

- *When it runs.* Only for an Image whose Library says
  `referrer_enabled("gamelist")` (`rules.library.ES_FRONTEND_REFERRER`): a
  folder Library's `es_frontend` column (migration 0016, off for every
  existing row), RomM's `es_frontend` setting (`ROMM_ES_FRONTEND`). Both are
  in memory, so the registry's question never blocks. `describe()` and
  `GET /api/libraries` report `es_frontend`; `PATCH /api/libraries/{id}` and
  `PUT /api/romm/settings` set it. The Rules editor shows it as a
  **Frontend** select (#523) over the two stored settings, with no column of
  its own: None is the switch off, Batocera or Knulli the switch on with no
  gamelists folder, ES-DE the switch on with one. ES-DE is turned on only once
  its folder is chosen. `GET /api/libraries/gamelists-folders`
  (`libraries.find_gamelists_folders`) finds candidates: a folder named
  `gamelists` holding a `<system>/gamelist.xml`, at most
  `GAMELISTS_SEARCH_DEPTH` (4) below a Volume root, hidden folders and
  symlinks not walked, each confirmed inside the Volumes, capped by
  `GAMELISTS_SEARCH_MAX_FOLDERS` and run under `admitted`'s time bound.
- *Where it looks.* A `gamelist.xml` directly in each folder of
  `files.dirs_for(renames)`: the Platform folder, with the Image beside it or
  in a subfolder. Each entry resolves against its gamelist's folder; the
  change's payload carries that base.
- *ES-DE's gamelists folder* (#387). ES-DE keeps its gamelists in a folder of
  its own, one subfolder per system named after the system's ROM folder (its
  `es_systems.xml` puts each system at `%ROMPATH%/<name>` and its gamelist at
  `gamelists/<name>/gamelist.xml`), with entries relative to that ROM folder.
  A Library can name the folder: `referrer_options("gamelist")` (the
  `Library` contract; `LibraryDefaults` returns `{}`) gives
  `{"gamelists_folder": path}` (`rules.library.GAMELISTS_FOLDER`) from a
  folder Library's `gamelists_folder` column (migration 0018, null for
  existing rows) or RomM's `gamelists_folder` setting
  (`ROMM_GAMELISTS_FOLDER`). The registry binds each active Referrer to the
  Image's Library through `Referrer.for_library(library)` (default: the
  Referrer itself; a failure keeps the Source); the gamelist Referrer returns
  a copy carrying the folder and the Library. *The system-folder rule*
  (`_systems`): a replaced file's ES-DE system is its Platform folder, which
  the Library gives for its own layout through `Library.platform_folder(path)`
  (on the contract; blocking, called on the hook's thread). `LibraryDefaults`
  (folder Libraries) answers the folder directly under the root;
  `RommLibrary` answers the folder directly under `<root>/roms` when the root
  holds one (RomM's standard layout, the same directory-level test
  `RommClient.local_path` makes), else directly under the root (which covers
  `<root>/<platform>/roms/...`). Both go through
  `rules.library.folder_holding(parent, path)`: the folder listed in
  `parent` (`os.scandir`) whose resolved path holds the resolved file,
  returned as listed. When more than one does (an alias, `alias -> ps2`), the
  answer would depend on the listing's order, so it raises `AmbiguousFolder`
  and the gamelist Referrer keeps the Source. `folder_holder(parent)` is the
  same answer for many paths, listing `parent` once: a Library's listing finds
  every game's console folder through `FolderLibrary.platform_folders()`, one
  listing of the root (and `roms`), not one per game (#541). So `ROMs/ps2/psx/Game.iso` is
  `ps2` and the `psx` system's gamelist is never read. Only
  `<gamelists folder>/<system>/gamelist.xml` is read, with the Platform
  folder as the entries' base: `ROMs/ps2/Game.iso` reaches
  `gamelists/ps2/gamelist.xml` and its `./Game.iso`, `ROMs/ps2/ps2/Game.iso`
  its `./ps2/Game.iso`, RomM's `ps2/roms/Game.iso` its `./roms/Game.iso`. The
  system's name is the Platform folder's name as listed, never a resolved
  one, so a symlinked `ROMs/ps2 -> actual/PlayStation2` is still `ps2`. Fail
  closed: a file no Platform folder holds (it sits directly in the root), or
  whose folder can't be listed, raises `ReferrerError` and keeps the Source;
  it is never "no gamelist, nothing to do".
  The subfolder is the ROM folder's own name, not a mapping of the Platform
  slug: ES-DE's system names are its ROM folder names, and the slug table's
  canonical ids differ from them (`ngc` for ES-DE's `gc`). Each gamelist is
  settled before it is read (`_settled`): every way to reach it is collected,
  and one reached with two different bases (a gamelists folder that overlaps
  the ROM folders) has no single base, so planning refuses ("more than one
  ROM folder could be its system's") and the Source is kept. The
  Platform folder lookup still runs beside it. The folder must be confirmed
  inside the Volumes when planning (`volume_containment`, "couldn't tell"
  refused), or the Source is kept. Saving it is two steps.
  `libraries.check_gamelists_folder` is read-only and runs bounded
  (`libraries.admitted`, which abandons its worker past `ROOT_CHECK_SECONDS`
  with a 503): it refuses a relative path (400), one outside every Volume
  (403), "couldn't tell" (503) and a missing folder (404), and returns it
  resolved. Only then do `PATCH /api/libraries/{id}` (`libraries.update`,
  which only persists) and `PUT /api/romm/settings` save it, so a check
  abandoned at the bound never saves anything later; `""` clears it. The Rules editor edits it under the switch
  (`source.gamelistsFolder` / `source.setGamelistsFolder`).
- *What it reads.* `defusedxml`'s `DefusedXMLParser` (no entity declarations,
  no external references) with a target that records each `<path>` that is a
  child of a child of a top-level element (`<game>` and `<folder>` entries),
  its text, and expat's `CurrentByteIndex` at its start and end tags. ES-DE
  writes `<alternativeEmulator>` beside `<gameList>`, two top-level elements
  that strict XML refuses and pugixml reads as a fragment, so an
  ASCII-compatible file is parsed wrapped in one more element after its
  BOM and declaration, its byte indexes shifted back. A file that doesn't
  parse fails with "can't be parsed (...)". The wrapper would let an empty,
  declaration-only or text-only file parse as a list with no entries, so a
  file with no top-level `<gameList>`, or with text between its top-level
  elements, fails with "isn't a gamelist" and keeps the Source. An entry is read the way
  ES-DE reads it: backslashes are separators, `./` is the base, anything else
  is taken as written (joined to the base when relative).
- *What it rewrites.* Only an entry that resolves to a replaced file
  (`same_identity`). The new text keeps the entry's `./` prefix, folder part
  and backslash style (normalised form first, then literal, each checked with
  `points_at`). It is escaped (`&`, `<`, `>`) and encoded in the file's own
  encoding (BOM, else the declaration, else UTF-8; characters it can't hold
  become numeric references) and replaces exactly the byte span of the old
  text. Nothing else changes: declaration, BOM, line endings, comments,
  attributes, element order and every other element stay byte for byte. The
  result is parsed again and must give the same entries, untouched ones
  unchanged and moved ones reaching the output.
- *Fail closed.* An entry that doesn't resolve but names a replaced file
  ("names game.iso but doesn't point at it"), or a matching entry that can't
  be edited in place (CDATA, a comment or child element inside `<path>`, a
  file whose encoding isn't ASCII-compatible such as UTF-16) raises
  `ReferrerError` naming the gamelist: the Source and the output are kept.

**The RomM swap (#391, `services/romm/swap_referrer.py`).** RomM's ROM as a
Referrer: `id = "romm_swap"`, `opt_in = True`, `Phase.NETWORK`, with its own
`apply_async` (and a synchronous `apply` that runs it). `RommLibrary.referrer_enabled` turns it on with the
`swap_keep_rom_id` setting (#390). It is registered when
`services.romm.library` is imported (it imports the job manager's hook, so it
can't be registered from `services/referrers/__init__.py`). It takes a
`renames` of one file replaced by one output file beside it under another
name, not an archive (`.zip`/`.7z`: RomM hashes an archive's largest member,
so the read-back can't confirm one), and `plan` finds RomM's single-file ROM
for that file. The Platform is the RomM Library's Platform folder holding the
file (`RommLibrary.platform_folder`, #387: `<root>/roms/<platform>/...` or
`<root>/<platform>/...`, `<root>/<platform>/roms/...` and subfolders
included), never the file's parent folder; only the RomM Platform whose
`fs_slug` (or `slug`) is that folder's name is listed, and the record must be
the file itself (`local_path` equal to the Source's real path). The rule
when the ROM isn't found:

- *Not a ROM RomM knows on its own: today's behaviour* (the job deletes the
  Source, the re-pin queue carries the metadata). The file is in no Platform
  folder, RomM has no Platform for that folder, no record names the file, or
  the record covering it is its folder (a multi-file ROM: the folder, and so
  the record and its id, stays).
- *Can't be told: fail closed* (`ReferrerError`, the Source is kept, so the
  record is never orphaned). The Platform folder or RomM can't be read, two
  Platform folders claim the file (`AmbiguousFolder`), two records are the
  file, or the record that is the file says it has several.

The change's `target` is `RomM ROM <id>`, its `summary`
"RomM: rename ROM <id> to Game.chd", and its `record` `{rom_id, kept_rom_id:
true}`, which lands in the Ledger's `referrers_updated` once it applied.

*Why the output stays staged.* RomM refuses a rename onto a name a file
already has (409), and a RomM scan that sees the output beside the Source
makes a duplicate ROM. So the job keeps the checked output in its hidden
staging slot (3.3.21) until the hook: `JobManager._staged_placement` asks
`referrers.places_output(source, renames, library)`, which is True when a
Referrer on for the Library says `places_output` (no I/O; the swap answers
for one file beside the Source). Only for a delete-on-verify job whose output
is one file in a slot, not an archive member. The job then writes the
verified record and the reuse snapshot at the staged path (a rename keeps the
fingerprint; `_place_staged` carries the records along) and hands
`_delete_source_set` a `StagedPlacement`. The hook carries it in the
`WriteGuard` (`base.staged_placement()`, `base.hook_running()`), which also
watches the staged path instead of the final one, so the file Referrers'
writes still check the output's identity. `files.points_at`, which the
`.m3u` and gamelist Referrers use to confirm a rewritten entry reaches the
output, asks `base.staged_output(final)` too: while the output is staged,
the entry must resolve to its final path and the staged output must exist.
A file Referrer's own temp file (#404) lives in a `slot-` slot of its own,
opened and closed per write; the job's output slot is locked and the swap's
`park-` slot is never swept, so the three never touch each other's slot or
lock. Right after the hook,
`_delete_source_set` calls `placement.place()` (a no-op once placed), so a
kept Source keeps its output beside it and nothing is unlinked before the
output is in place; a lingering apply is left to place it itself. In the hook
the swap plans nothing when the output is not staged (the job placed it).

*The steps*, each written to the swap journal before it runs
(`services/romm/swap_journal.py`, table `romm_swaps`, migration 0020):

| State | Step |
| --- | --- |
| `renaming` | `RommClient.rename_rom` (`PUT /api/roms/{id}`, multipart `fs_name`). RomM moves the Source to the new name itself. The name is read back with `GET /api/roms/{id}`. |
| `parking` | The file now at the new name must have the Source's size and mtime (a rename keeps them). It moves into a parking slot (`staging.open_slot(folder, parking=True)`) under its original name (`StagedOutput.take(name, as_name)`, #415), so a parked file never carries the output's name. The journal's `parked_path` records that path; the delete and both undo branches use it, and the restore beside the output puts it back under its original name. |
| `parked` | Written by the move itself, once the Source is in the slot. |
| `placing` | `placement.place_blocking()` (the job's `_place_staged_blocking`), run like every other move through `_mutate`: never abandoned, so a cancel waits for its real outcome before the undo picks its branch, and with no hand-off of the job's holds to a placing worker (the swap and its undo keep them, and past the hook's grace `outlive` hands them to the swap's task; Codex 4594650576). The staged output takes the name. |
| `placed` | The output is in place, the Source still parked. |
| `rehashing` | `RommClient.scan_rom_hashes([id])` (`POST /api/tasks/scan`, `{"type": "hashes", "roms_ids": [id], "apis": []}`), `RommClient.task` polled to an end (`REHASH_SECONDS`), then the ROM read back: its name, `fs_size_bytes` and `sha1_hash` must be the output's. The one exception: on a Platform RomM never hashes (`UNHASHED_PLATFORM_SLUGS`, RomM's own `NON_HASHABLE_PLATFORMS`: Switch, Switch 2, Wii U, PS3 to PS5, Xbox 360, Xbox One, PC and others) and only when RomM reports no SHA-1, the name and size are all RomM has, so they alone confirm it; a SHA-1 RomM reports is always compared (CodeRabbit 4593839047). The task's stats are never read: RomM's say `updated_roms: 0` even when it worked. |
| `deleting` | The parked file must still be the Source; it is removed and the parking slot closed. |

A success ends `done`, returns `Applied(removed=(source,))` (the job records
the Source as deleted and unlinks nothing), and closes the output's pending
re-pin row as done (`repin.settle_swapped`): the provider ids never left the
ROM. With the switch off, or when the swap doesn't apply, the re-pin queue
works exactly as before.

*When the output is already in place* (CodeRabbit 4593839018). A job whose
Source a reader held places its output and keeps the Source; the later
retried delete (`retry_kept_delete`, `finish_kept_delete`) and any job that
placed its output before the hook run the hook with no `StagedPlacement`.
The fallback applies only when the output's check record says it was
converted from this Source (`_converted_from`): #389's duplicate delete runs
the same delete step with the copy it keeps as the "output", which was never
converted from the copy deleted, so no swap was owed there and none is
reported. Otherwise RomM can't rename onto a name that is taken (409), so the
swap doesn't try:
`plan` returns a fallback change (`summary` "RomM: ROM <id> can't keep its
id", `record` `{rom_id, kept_rom_id: false}`) whose apply queues the re-pin
the switch-off path relies on (`repin.record`, `pre_fingerprint=""`, the
ROM's provider ids) and returns `Applied(note=...)`: "the ROM did not keep
its id: the swap can't run after the output was placed; re-pin queued (RomM
ROM <id>)". The job deletes the Source as today. The note reaches the
Ledger's record (`note`), the job's message and warning
(`_SourceDelete.notes`), and a retried delete's outcome reason, which the
sweep logs and lists under `kept_sources_notes`; a retried delete writes no
Ledger row of its own. A re-pin that can't be queued keeps the Source, and
so does one `repin.record` refuses (it returns 0; CodeRabbit 4594841873).
The fallback is refused, keeping the Source, while RomM's identity is being
changed or once the settings no longer name the instance `plan` bound to,
and counts itself in `_active_swaps` while it writes the re-pin, so
`identity_change()` waits for it (CodeRabbit 4594841887). A
plain `apply`'s `Applied` result now counts like an async one's: the hook
keeps it (`_plan_and_apply`) and carries each phase's `removed` and `notes`
into the outcome (CodeRabbit review 5930768363).

*A Source check that fails before the hook* (CodeRabbit 4593839025).
`_delete_source_set` runs its pre-hook part in `_prepare_delete` (the plan
rebuilt, the order, the path checks, the Library). If that raises while the
output is still staged, `_place_checked_output` places it before the error
goes on, so the job's cleanup never discards a checked output because the
Source side failed.

*Failures, and what each journal state leaves.* `ReferrerError.kept` sets the
job's wording (`ReferrerOutcome.kept_message`): "converted, source kept:
RomM refused the rename (<RomM's detail>)" for a refusal, "RomM didn't
confirm the new file (...)" for a rehash that failed or didn't match, and the
default "couldn't update RomM ROM <id> (...)" otherwise. `RommError.detail`
carries RomM's own `detail` text.

- Before RomM renamed: a 409 ends `refused`; any other HTTP answer (the 500
  "file is gone", a 403) means nothing changed and ends `failed`. Nothing to
  undo.
- RomM renamed (or wrote another name, which counts as a refusal, or didn't
  answer at all) and the output is not in place: a parked Source goes back
  under the new name (`restoring`), then RomM is asked for the old name
  (`reverting`; a no-op when it never renamed) and it is read back. Ends
  `refused` or `rolled_back`; the job places the output beside the Source.
- The output is in place (the rehash failed or didn't match, or the parked
  Source couldn't be deleted): the parked Source goes back under its own name
  beside the output (`restoring`), and the ROM keeps its id on the checked
  output; RomM's next scan refreshes its hashes. Ends `kept`.
- A Cancel or the hook's bound (`_undo_within_grace`): the forward step it
  lands on finishes and records its outcome first (`_mutate`, below), then
  the same undo runs, each step journaled, under
  `asyncio.wait_for(..., referrers.CANCEL_GRACE_SECONDS)` (#383's grace,
  read when it runs). The hook waits that long for the cancelled task; if
  the undo outlasts it, the hook treats the task as lingering and hands it
  the job's delete holds and output locks (`ReferrerOutcome.outlive`), which
  are let go only when it ends. From the swap's first move until it ends,
  the output is held back from the job (`StagedPlacement.held`, which
  `_delete_source_set` and the job's end-of-job placing both honour), so a
  lingering swap is never overtaken by the job placing the output over the
  Source's way back (Codex 4594772381). `run` releases it when the swap
  ends anywhere but a row left for the next run, so the job places the
  output beside the Source when it still can (the row ends `rolled_back`,
  or `kept` once the output was in place), and otherwise sets it aside. A
  swap stopped before anything changed ends `failed`. The lingering task's
  result is still taken when it ends (Codex 4595004160): the hook puts a
  `referrers.HandedOff` per lingering apply in `ReferrerOutcome.handed_off`,
  and `_delete_source_set` gives each the same `_take_late_outcome`
  callback the shutdown hand-off uses. That callback adds what the apply
  removed to the job's deleted list, puts its `updated` record in place of
  the hook's `abandoned` one, adds a note to the job's message and tells
  the amend listeners, so the Ledger row is amended too. Until then, the
  job's kept reason does not say the Source is definitely kept: it adds
  that the Referrer "is still finishing, so the source may yet be removed".
- An undo that runs out of grace or fails, or a process that dies, leaves
  the row on the state reached (`parked`, `placed`, or the undo step it was
  on), with the reason and "left for the next run" in `detail`; a parked
  Source stays parked; and the output stays held, since the name it would
  take may hold, or be owed to, the Source (the job then sets the checked
  output aside, below). `swap_journal.unfinished()` lists the rows left for
  the next run, which startup recovery settles (below, #392).
- At shutdown the job's task is cancelled, so the hook's awaiter is.
  `_await_bounded` cancels the apply's task, keeps it referenced
  (`_let_it_end`) and, if it is still running, puts it in the caller's
  `unawaited` list (`update_before_source_removal(..., unawaited=...)`).
  `_delete_source_set` catches that cancel and hands its delete holds and
  output locks to those tasks (`_hand_holds_to`, the same transfer as the
  `outlive` hand-off after the grace) before letting the cancel go on, so
  they are let go only when the swap's undo ends (Codex 4594286704). Each
  `unawaited` entry is a `referrers.HandedOff` (the Referrer, its change and
  the task), not a bare task: the job adds a done callback per entry
  (`JobManager._take_late_outcome`) that, when the task ends with a result,
  adds the Source files it removed to the job's deleted list and its Ledger
  record to the job's records, so a swap that finishes after the cancel is
  not reported as nothing done (CodeRabbit 4594841860). An apply already
  done when the cancel reaches the awaiter (it finished in that instant) is
  handed off the same way, so its result, removed paths and records are
  taken at once and its holds let go through the same hand-off (CodeRabbit
  4594978832). The job's Ledger row may be written (it is announced
  terminal) before a handed-off apply ends, so the late result also
  amends the persisted row rather than deferring the terminal write: the
  callback tells `JobManager`'s amend listeners (`add_amend_listener`,
  sync or async like a terminal listener), and `ledger.amend` updates that
  row's `source_deleted`, `deleted_paths` and `referrers_updated`. Writing a
  row (`ledger.record`, which now builds the row from the job in the
  thread) and amending it share `ledger._write_lock`, so an amend either
  finds the row written or runs before it is built, and the row then
  carries the amended values. `main.py` and the CLI register
  `ledger.amend` beside `ledger.record`. The
  undo runs as far as the event loop lets it before the process exits, each
  step journaled; whatever it didn't reach is left for the next run, and a
  parked Source is never removed.

*Startup recovery* (#392, `services.romm.swap_recovery`). `main.py` starts
`swap_recovery.run_forever()` as a background task after startup, so it
never holds up startup. It makes a pass over the rows that were unfinished
when it started (`recover_once(started_before=...)`: a row written since
belongs to a swap of this run), then repeats every `RETRY_SECONDS` (10
minutes) while any of them is left. Migration 0021 adds what a row needs for
this, written by `swap_journal.begin`:

- the instance the swap ran against: `romm_url` and `library_root`;
- the Source's fingerprint (`source_size`, `source_mtime_ns`);
- the output's fingerprint (`output_size`, `output_sha1`).

A swap left for the same reason on each pass is logged once: a WARNING when
the reason is new for that swap (`_last_reason`, in process, so a restart
logs it once more), again when the reason changes, and at DEBUG when a pass
repeats it. The Finding is written as before and the retry schedule is
unchanged. A swap that settles is forgotten.

Recovery takes these steps for each row:

1. Take the job locks on the Source and output paths (`lock_manager`). If a
   job holds them, try again later.
2. Classify what is at the Source path, the output path and the parked path
   (`_what_is`: Source, output, other or absent; the output's SHA-1 is read
   only when the size matches). Look for the job's set-aside output in its
   staged slot, or in that slot's `park-` twin (`StagedOutput.set_aside`'s
   name).
3. Check that the instance is the one RomM's settings name now, and read
   RomM's `fs_name` for the ROM through a client bound to that instance
   (`_Binding`).

It finishes only three cases:

- **RomM has the old name and the Source is in place.** The row ends
  `failed` (from `renaming`) or `rolled_back`, and the kept output is placed
  beside the Source.
- **RomM has the new name and the output is in place.** The `hashes` scan is
  retried and the ROM read back (`_read_back_problem`). On a match, the
  parked Source is deleted after a re-check, the row ends `done` and the
  re-pin is settled. On a mismatch, the Source is restored beside the
  output and the row ends `kept`. At `deleting` with the Source already
  gone, the row ends `done`.
- **The undo was at `restoring` beside the placed output.** The restore is
  finished and the row ends `kept`.
- **The row was finished by hand, as its Finding says.** RomM has the new
  name, the output is in place and the Source is gone, at whatever step
  the row is on. The same rehash and read-back confirm it, and only then
  does the row end `done` and the Finding clear. If RomM doesn't confirm
  it, the Finding stays (Codex PRRT_kwDOQ9nULM64SAMo). A settled row's
  kept-output slots are removed too, once each is empty.

A kept output is placed the way the job places one: `StagedOutput.place`,
then `staging.carry_records_blocking`. That is the helper
`_place_staged_blocking` and `_keep_records_blocking` now share, so the
output's check and CHD records move with it (Codex PRRT_kwDOQ9nULM64SAMu).

Recovery never renames a ROM: the rename back to the old `fs_name` was not
proven in the spike. Every other combination raises `_Left` and becomes a
`findings.ROMM_SWAP` Finding (Library `romm`, image key
`romm-swap-<row id>`). The Finding names the ROM, the step, why, where the
Source and the output are, the slots left (`cause["slots"]`) and the manual
steps. A pass that can't settle the row writes the same Finding again, and a
pass that settles it resolves the Finding.

The manual steps (`_manual_steps`, #415) are two numbered lists built from
where the files are. They never put two files on one path:

- **Keep the conversion.** The output goes to the output path, after the
  Source is deleted when RomM had moved the Source to that name. The Source
  is deleted at its exact path, then the ROM is rescanned.
- **Undo.** A placed output is first moved aside to
  `.compressatorium-stage/aside-<new name>` (hidden, never indexed). Then
  the Source moves to the name RomM has for it. Then the ROM is renamed back
  to the old name in RomM, which moves the file itself. Then the ROM is
  rescanned.

When RomM already has the old name, the Source just goes back to its own
path. The undo puts the Source at RomM's current name rather than its
original one, because RomM's rename moves the file from that name. The
spike's fake answers 500 when the file isn't there.

Rows written before #415 parked the Source under the output's name. Recovery
reads the parked file's name from the row's `parked_path`, so it handles
both layouts, and its restore puts the Source back under the original name
either way.

These cases also become Findings, and nothing is sent to RomM:

- a row without the 0021 fields;
- a row from another instance or library root;
- a row read while an identity change is waiting (`_identity_changes`);
- an unreachable RomM, which is retried later.

The only RomM write, the rehash, counts in `swap_referrer._active_swaps`, so
`identity_change()` waits for it.

Moves go through `_mutate` and reads through `_probe` or `_read_whole`, the
same seams the swap uses. Building a Finding looks at the Volume (the slots
left), so it is built and written in one bounded detached call
(`_leave_finding`), never on the event loop (Codex 4595373645). The job
locks are taken through `run_detached_undoable` under `LOCK_SECONDS`, with
`_unlock` as the undo. A worker that takes the locks after the bound gives
them back, and a partial acquisition is rolled back (Codex 4595373643).
Each destructive step is journaled before it runs, as in the swap:
`rehashing`, then `restoring` or `deleting`. A stop after deleting the
parked Source therefore leaves `deleting` with the Source gone, which the
next pass ends `done` (Codex 4595373639). Slots are taken again with
`staging.reopen_slot`, which has the same Volume and no-link guards as
`open_slot` and takes the slot's flock. A failed flock closes the
descriptor and leaves the lock file, which a live holder owns. Only a lock
file this call locked is removed on a later failure, so no opener can make
a second lock beside a holder (Codex 4595373632). A flock that succeeds
counts only while the name still points at the locked file
(`partial_writes.same_file`, the check the sweeps make). A holder's `keep`
may unlink the lock file between the open and the flock, so a lock on the
unlinked file is let go and the current file tried, within a bounded
number of attempts (`staging._relock`). `_claim` makes the same check
after its flock (Codex PRRT_kwDOQ9nULM64SAMh). The `O_EXCL` claims in
`_claim` and `partial_writes.claim_temp` already remove only a lock file
they created, and the sweeps never remove one they could not lock. A slot is removed only once its own
listing is empty (a kept output's slot may still hold its keep marker,
which is removed first). A pass that finds nothing new to do changes
nothing.

*Bound to one RomM instance* (Codex 4594286722). The settings-following
client (`romm_client`) resolves the URL and token on every request, so a
settings change mid-swap would send the rename, or its undo, to another
instance, where ROM 12 is another game. `plan` captures a `_Binding`: the
base URL and token, and `romm_settings.identity_generation()` (the counter
every save that moves the instance or library root bumps, which the re-pin
plan already checks), before it looks the ROM up, and refuses if it moved
during the lookup. The swap makes every RomM call, undo included, through
`binding.client()`, a `RommClient` with that URL and token. Before each RomM
write (the rename, the rehash, putting the old name back) it asks
`binding.still_live()`; if RomM's settings name another instance, it raises
`_Unbound`: no undo, nothing more changed on either instance, and the row is
left for the next run with the reason. The settings route serialises
against swaps too: a save that moves the identity runs inside
`swap_referrer.identity_change()`, which stops new swaps from starting (they
keep the Source, "RomM's settings are being changed") and waits for those
running, undo included, to end. It waits rather than refuses, as the same
route waits for sweeps (`romm_auto.paused`) and the settle pass
(`_settle_lock`); each swap is bounded by the conversion's stall bound. The
wait is bounded too (`IDENTITY_CHANGE_WAIT_SECONDS`): a swap still running
then raises `SwapsStillRunning`, which the route answers with 409 and
nothing saved (CodeRabbit 4594841850). A step on its thread (`_mutate`) is
waited for through every cancel, not just the first, so a second cancel
never leaves a rename or a move unrecorded (CodeRabbit 4594841855).

*A record RomM marks missing* (CodeRabbit 4594841868). `_rom_for` keeps
records with `missing_from_fs`: when the record that is the file says it is
missing while the file is there, RomM's scan state can't be trusted, so the
swap raises `ReferrerError` and the Source is kept, rather than treating
the file as one RomM doesn't know.

*A checked output is never discarded* (CodeRabbit 4593970363). The job
marks its output `placeable` once its checks pass (or none was asked). If
it is still staged when the job ends (its name was taken, a Referrer holds
it, placing failed), `_set_aside_output` keeps it, never sweepable while it
is the only copy (CodeRabbit 4594086355):

- `StagedOutput.set_aside` renames its slot to a `park-` slot (no replace;
  its `partial_writes` record dropped) and only then lets go of its lock.
  If the rename fails, it writes `partial_writes.KEEP_MARKER`
  (`.compressatorium-keep`) into the slot instead, which every slot sweep
  honours (`_sweep_slot` skips a slot carrying it, and one it can't check
  for it), and only then lets go. If even that fails it raises with the
  slot still open, and the job holds it, lock and all, in `_held_slots`
  for as long as the process runs. `_sweep_slot` looks for the marker once
  before taking the slot's lock and again under it, after checking the name
  still names the same slot, so a marker written (and the lock let go)
  between the two looks still keeps it (CodeRabbit 4594218526). The other
  sweeps (`_sweep_one`, `_sweep_orphan_sidecar`) already make their last
  check under the lock.
- `set_aside` is started first, with `start_detached_or_own_thread`, before
  anything in `_set_aside_output` that a cancel could interrupt (listing the
  output's names comes after; CodeRabbit 4594495308), so the slot is renamed
  or marked kept whatever happens to the job's task. With every detached
  thread in use it gets a daemon thread of its own (the fallback
  `_close_stage` and `_let_go_in_background` take, now shared as
  `subprocess_runner.start_detached_or_own_thread`; CodeRabbit 4594613612),
  so it always starts. If even that thread can't start (the OS thread
  limit), the helper raises `NoThreadAvailable`, and the job holds the
  slot, lock and all, in `_held_slots`, which no sweep passes while the
  process lives (CodeRabbit 4594745731). It hands the keep marker
  (`StagedOutput.mark_kept`: one small create through the slot's
  descriptor, then the lock goes) to `start_detached_eventually`, so the
  marker is written off the loop once a thread starts, never on it, because
  the create can block on a dead Volume (CodeRabbit 4594981268). Until the
  marker is written, the job's note says preservation is "not yet durable".
  Once it is written, the slot leaves `_held_slots` and a note says it is
  durable. If writing the marker fails, the slot stays held. A free detached slot whose thread the OS
  refuses is given back and reported as `ProbeCapacityExceeded`
  (`_probe_in_daemon_thread`), so it takes the same fallback rather than
  raising a bare `RuntimeError` with the slot lost. Cleanup the caller no
  longer owns (letting go of locks in `_let_go_in_background`, closing a
  slot in `_close_stage`, the late set-aside answer) starts through
  `subprocess_runner.start_detached_eventually`: the same helper, and when
  no thread can start at all, the same again on the loop every
  `NO_THREAD_RETRY_SECONDS` until one does, so the locks are let go once a
  thread frees up rather than dropped (CodeRabbit 4594855525). The job's `finally` holds back anything the
  set-aside raises, a cancel or an error, until every release and the
  terminal notice have run. In the swap, `_mutate` raising
  `NoThreadAvailable` means the step never ran: the swap treats it as that
  step failing (undo, or the row left for the next run). The slot stays protected
  all along (its lock is held until the rename or the marker is in place).
  The job waits `SET_ASIDE_SECONDS` for it (shielded). An answer that comes
  after the bound, or after a cancel, goes once to `late` on a thread of its
  own, which moves the records (`_keep_records_blocking`), logs the real path
  and adds it to the job's message and warning; the job has reported the
  stage folder meanwhile (and the Ledger row, already written, keeps it).
- Its records go with it: the check and CHD-metadata records are moved from
  the staged paths to the kept ones with the store helpers placing uses
  (`move_blocking`), and when placing failed before the job wrote the check
  record, it is written at the kept path from the job's `check_passed`.
- The job never runs `_close_stage` on it (so its records are never
  cleared), and its message, warning, error (the Ledger's `error` for a
  failed job), a `{"referrer": "staging", "status": "kept"}` entry in its
  Ledger record and the log all say where it is.

The set-aside runs first in the job's `finally`, and a cancel during it is
held until every release after it has run (the terminal notice, the output
locks and delete holds, the input and folder locks, the concurrency slot,
`_close_stage`, the temp folder, the pruning), then raised (CodeRabbit
4594326475); the slot is taken out of the job's hands before the await, so
a set-aside the cancel cut short finishes on its worker and is never closed.

*The Source the swap touches is the one converted* (Codex 4594537162). The
job puts the Source's pre-conversion fingerprint, the delete snapshot the
normal delete path checks (size, `mtime_ns`, inode, device, by path), in
`StagedPlacement.source_fingerprints`. The swap compares the Source against
it (`_is_snapshot`, the job's own rule) under the Source's hold right before
the RomM rename, the file RomM moved before parking it, and the parked file
right before deleting it. A swap with no fingerprint doesn't start; a
mismatch before the rename ends the row `failed` with nothing changed, and a
later one rolls back as any failure does. The Source is kept, with the
reason.

*After the Source is deleted the result is never lost* (Codex 4594537178).
Recording `done` and settling the re-pin run as a task of their own
(`_finish`), awaited shielded: a cancel landing there leaves it running
(referenced in `_after_swaps`) and the swap still returns
`Applied(removed=(source,))`, which the hook takes, so the job records the
Source as deleted.

Three more edges of the swap (CodeRabbit review 5931463835):

- The removal of the parked Source sets `removed` on its own thread. A
  cancel or an error after that ends the swap `done` with
  `Applied(removed=(source,))`, never `kept`; the hook takes the result of
  an apply that finished despite its cancel (`_await_bounded` returns it).
- Settling the re-pin after the Source is gone is bookkeeping: any error
  there, a `DeadlineExpired` included, is logged and the swap still reports
  applied.
- `_delete_source_set(..., hook_records=job.referrers_updated)` fills the
  job's records as soon as the hook returns, so a Referrer's failure stays
  in the Ledger when placing the output then raises.

`_close_stage` discards only an output the job never got to place: one
rejected by its own checks, or a conversion that failed or was cancelled
before them. The Source is kept in every such case (nothing is deleted
before the output is placed).

*What happened is recorded before anything acts on it* (Codex
PRRT_kwDOQ9nULM64NxUt). Each step that moves something (opening the parking
slot, `take`, the restore, the delete) is one function run on its thread by
`_mutate` that does the move, sets its flag (`parked`) and, for `take`,
writes `parked` to the journal; `_mutate` shields it, so a cancel waits for
it to finish and then propagates with the flag already right. Placing reads
`StagedPlacement.is_placed()` (the job's slot) when it ends any other way.
Before the parking slot is let go, `_release_park` lists it
(`StagedOutput.contents()`): it is closed (removed) only when empty. One
that holds anything, or can't be listed, is kept (`StagedOutput.keep()`),
never swept (the sweeps match `slot-` names only, and a parking slot is not
journaled in `partial_writes`), and the row says so. So the Source is never
deleted unconfirmed.

*Off the loop, never on its shared executor* (Codex PRRT_kwDOQ9nULM64NxU1).
Every read of a file or of RomM in the swap runs on a detached thread under
a bound: a stat or a RomM call through `run_detached_bounded`
(`PROBE_SECONDS`, `ROMM_SECONDS`), hashing the output through
`run_detached_within_stall_bound`. Every move, journal write and RomM write
runs through `start_detached_or_own_thread` (`_mutate`: a detached thread,
or one of its own when none is free, so a step, or its undo, always starts).
Each carries the caller's context
(`contextvars.copy_context().run`), so `check_write` sees the hook's guard. A
mount that stops answering strands a throwaway thread, never a worker every
other offload needs; the hook's grace and `lingering` hand-off cover a move
that outlasts it. The fake
RomM (`tests/fake_romm.py`) holds ROMs backed by files and answers the
rename, the scan, the task and the read-back the way RomM 5.4.1 does, with
knobs for a refusal, a sanitized name, a scan that doesn't rehash and a task
that fails.

**Formats RomM re-identifies by itself (#393,
`services/romm/reidentify.py`).** Spike #382 found that RomM 5.4.1, with
title-id extraction on, moves a missing ROM onto its converted file by title
id at its next library scan, keeping the id, for some (Platform, output
format) pairs. `REIDENTIFIED` is that finding as data: RomM version ->
frozenset of `(RomM Platform slug, output extension)` (5.4.1: PSP CSO,
GameCube RVZ). DAT-safe extensions (`DAT_SAFE_OUTPUT_EXTS`: `.chd`, `.zip`,
`.7z`) never appear: `repin.mode_needs_repin` says no re-pin for them, so no
row is recorded and there is nothing to mark (a test asserts the list holds
none; #414). A RomM release that behaves differently gets
its own key; a version with no key gets the re-pin. `reason(heartbeat,
platform_slug, mode, output_path)` is the one decision: it answers only when
the heartbeat's `SYSTEM.VERSION` (a leading `v` dropped) has a key,
`FILESYSTEM.TITLE_ID_EXTRACTION_ENABLED` is `True`, and the output's
extension is one `registry.mode_output_extensions(mode)` says the mode writes
and is listed with the source ROM's Platform slug. Never by tool identity.

An answer never skips the re-pin on its own. RomM keeps the id only when it
never sees the output beside the Source, and the output is placed just
before the Source is deleted: a RomM scan in that moment adds a new ROM for
the output and leaves the old one a ghost. So the answer only marks the
re-pin row, and the settle pass checks what RomM actually did.

*The mark.* A second RomM Referrer: `id = "romm_reidentify"`, `opt_in = True`,
`Phase.NETWORK`, registered by importing `services.romm.library`.
`RommLibrary.referrer_enabled` turns it on while `repin_enabled` is on and
`swap_keep_rom_id` is off, so it never runs beside the swap. The re-pin row
is still written before the conversion (the plan route and `after_queue`
are unchanged: the provider ids are readable only then). `plan` takes a
`renames` of one file replaced by one file in the same folder, finds that
conversion's pending row (`repin.pending_for(output, source)`, matched on
the row's `source_path`), reads RomM's heartbeat, and asks `reason`. Every
failure (no row, RomM not answering, anything raised) returns no change, so
the row stays unmarked and re-pins as today; it never keeps a Source. The
change has no edits; its `summary` (shown in the delete confirmation) says
the re-pin is expected to be unnecessary and will be confirmed, and its
`record` carries `rom_id` and the row's `repin_id`. `apply` marks the row
(`repin.mark_expected`) and returns that line as `Applied.note`, so the job's
message and warning and the Ledger record say it; a mark that can't be
written returns no note. The mark is the pending row's `detail`
(`repin.EXPECTED_SELF_IDENTIFY` and the reason), a column a pending row
doesn't otherwise use, so no migration; `pending_rows` adds whether a row is
marked as the tuple's last field, and any settle overwrites `detail`.

*The check.* `_settle_one_repin` passes a marked row's source ROM id to
`_match_and_settle` (`kept_by`). Once the join (by SHA-1, or by path where
RomM doesn't hash) has found the output's ROM, `repin.self_identified(rom,
source_rom_id, ids)` asks whether it is the source ROM itself with every
provider id the row captured. If so, the row closes as done with
`repin.SELF_IDENTIFIED` ("RomM re-identified it on its own") and nothing is
written to RomM; the pass counts it under `reidentified`. Anything else (a
new ROM beside a ghost, another match, a missing id) takes the normal claim
and write. An unmarked row is never checked. The fake RomM serves
`FILESYSTEM.TITLE_ID_EXTRACTION_ENABLED` (`title_id_extraction`), can fail
its heartbeat (`heartbeat_status`), answers `GET /api/roms/by-hash`, and
records metadata writes (`metadata_writes`).

To add a Referrer: subclass `Referrer` in `services/referrers/`, give it a new
`id`, declare `opt_in` and `phase`, and `registry.register(...)` it in
`services/referrers/__init__.py`. A file Referrer builds on `files.py`. An opt-in one also needs the Library's
setting that `referrer_enabled` reads, and one with settings of its own reads
them from `referrer_options` in `for_library`. Nothing in the job pipeline, the
delete-plan route or the modal changes. The
existing RomM re-pin (3.3.9) is not a Referrer yet.

### 3.3.16 Folder Platform mappings and the Findings store (`services/platform_mappings.py`, `services/findings.py`)

**Mappings.** A row of `platform_mappings` says "this folder is PSP": a
`library_id`, the folder's path relative to that Library's root (`/`
separators, `""` for the root), and a Platform slug checked with
`known_slug`. `set_mapping(path, platform)` takes the folder's absolute path,
finds the folder Library that holds it (403 outside the Volumes, 404 outside
every folder Library, 422 for an unknown Platform), and clears the mapping
when `platform` is None. The rows are kept in memory as an index of absolute
folder paths, rebuilt on first use after any change; `mapped_folders()`
returns it, and detection's `_place` walks the Image's parents against it and
the Platform folder names together. Removing a Library deletes its rows.

"No mappings set" and "the mappings could not be read" are different states.
An empty table answers `{}`. A database read that fails raises
`platform_mappings.MappingsUnavailable` and caches nothing, so the next call
reads again. Missing evidence never decides a Platform:
`detect_platform` answers Unsorted ("the folder Platform mappings could not
be read") and caches nothing, and a folder Library's walk stops with
`MappingsUnreadable` (code `mappings_unavailable`), so its Rules listing
returns nothing and no Finding is written until the mappings read again.
Otherwise a header could pass as the Platform of a folder whose mapping says
something else, with no conflict. Only a process with no database at all (a
CLI dry run on a fresh data dir) reads "no table" as "none set".

Detection reads the mapping as evidence inside `_decide`, never as an override
applied afterwards, so a mapping that disagrees with a header is a conflict in
the same way a folder name is. `detection_key` includes the deciding folder
evidence as `(source, folder path, slug)`, which is how a changed mapping
invalidates both caches: the shared LRU and a folder Library's per-file cache
see a new key for exactly the Images it decides. The path, not the name, is in
the key, because where a mapping sits relative to a Platform-named folder
changes the answer. `set_mapping` also calls `FolderLibrary.invalidate()`,
which bumps the Library's generation so the next listing walks again instead
of serving the last walk (`SCAN_TTL_SECONDS`).

**Findings.** `findings` holds one row per `(library_id, image_key, kind)`.
The store is Library agnostic: a producer passes `reconcile(library_id,
observed, kinds=...)` everything it currently sees, each an `Observed(kind,
image_key, path, reason, cause)`. Rows not observed again (of those kinds, for
that Library) are deleted; new ones get `first_seen`; every observed row gets
`last_seen`. `fingerprint` is a digest of `kind` and the structured `cause`
(for a Platform Finding: `how`, `header`, `folder` and `folder_platform` for
the folder that decided, by its Library-relative path, `mapped`, and `headers`
when two disagree), not of the
`reason` text, so a reworded message does not resurface a dismissed row.
`dismiss(id)` writes a row to `finding_dismissals`, keyed by `(library_id,
image_key, kind, fingerprint)` and kept apart from the Finding row. A Finding
is dismissed while such a row matches its current fingerprint. Because the
dismissal outlives the Finding row, a row that a walk drops for not seeing the
Image (a share that was unmounted, so its mount point listed empty) comes back
dismissed when it returns with the same cause. A fingerprint `reconcile` sees
change deletes the Image's dismissals of that kind, and so does writing a new
row whose fingerprint differs from a stored dismissal (the cause changed while
the Image was out of the listing), so a cause that changes and then changes
back stays shown. `undismiss(id)` deletes them by hand, and `forget_library`
deletes a removed Library's. A dismissal of a cause that went away for good
stays behind, at most one per Image and kind. `dismiss` and `undismiss` take
the store's write lock; `dismiss` is idempotent (a double click is one row)
and asks `current(library_id)` under the lock, so a dismiss racing the
Library's removal writes nothing (the route passes #303's token check).
`reconcile(..., keep_under=[...])` leaves alone every row whose `image_key`
sits under a folder the producer could not look in. An `Observed` marked
`only_if_new` (an Image file the producer could not read) does the same for
one Image that already has a row: every row of it is kept as it is, and only
an Image with no row yet gets the new Finding.
`reconcile(..., current=...)` asks the producer, under the store's write lock,
whether it may still write; `forget_library` takes the same lock. Kinds today
are `platform_conflict` and `unsorted`; later producers (RomM, the Inbox, Check
Rules) add their own kinds and pass them in `kinds`, so one producer never
clears another's rows.

**The producer.** The folder Library's walk (§3.3.11, #303) is the only one: at
the end of `_walk` it reconciles a `platform_conflict` per conflicted Entry, an
`unsorted` per Entry with no Platform, and an `unsorted` per Image file the walk
could not read (`only_if_new`; not a track a readable sheet lists, which is
not an Image). A
listing served from the last walk writes nothing, and a walk that fails (root
unavailable) never reaches the reconcile. A subfolder that does not list is
passed as `keep_under`, so a mount blip at any depth clears no Finding.

The walk's `current` check refuses the write in two cases. The Library was
removed: `FolderLibrary` captures the Rule engine's token for its id
(`rules_sweep.library_generation`, #303) when it is built, and
`rules_sweep.forget_library` moves it before `libraries.remove` drops the
Findings, so a walk of the removed object writes nothing whether it was
running then or starts later. Or the Library's own generation moved since the
walk started: a mapping was set while it ran, so its detections are out of
date. The walk then marks its Scan `stale`. Whenever the generation moved
during a walk (before its Findings were written or after), `scan()` starts
`_schedule_rewalk` on a daemon thread once its lock is released, and still
returns the walk to a listing or the Findings path. Rules never get it:
`images()` (via `_scan_for_rules`) walks once more, and when that walk is
stale too it raises the transient `listing_in_progress`, so the sweep skips
the Library this tick and retries on the next instead of converting Images
under Platforms the new mapping changed. A database error while reconciling is logged and costs only
the Findings; the walk still serves the Rules. Nothing that reads the store
moves, renames or deletes a file.

**API** (`routes/findings.py`). `GET /api/findings` (`include_dismissed`), with
each row's Library name and folder; `POST /api/findings/{id}/dismiss`; `POST
/api/findings/check` walks every folder Library on a detached thread, bounded
as #303's listings are, and reports each Library's status
(`listing_in_progress`, `root_unavailable`, ...); `GET /api/platforms` lists
what a folder can be set to; `GET`/`PUT /api/platform-mappings` lists and sets
mappings, and a `PUT` walks the Library again right away so its Findings
follow. When a walk is already running the `PUT` still saves and answers
`walk.status: "listing_in_progress"` with "A listing is running; Findings
refresh after it."; the running walk drops its stale Findings and walks again.
`POST /api/findings/{id}/undismiss` shows a dismissed Finding again. Check now
walks the Libraries side by side.

Dismiss and undismiss send the row's `fingerprint` with its id
(`{"fingerprint": ...}`), and the store refuses with 409 when the row's cause
is another one now. With `findings` on SQLite AUTOINCREMENT, a cleared
Finding's id is never handed to another, so a row left on a screen after a
walk changed or cleared it can never act on a different Finding. In the UI,
each mutation drops every listing that started before it and lists again.

### 3.3.17 The headless CLI (`app/cli.py`, #306, #307)

`compressatorium convert|verify|run-rules` runs the pipeline with no web server. It is a
caller of the same seams the routes use, not a second conversion path:

| Step | Shared seam |
| --- | --- |
| Find Images | `routes.files.scan_tree`: the `GET /files/search` walk (subfolders, convertible folders, archive members as `archive::member`). `purpose="verify"` also lists every file some tool can check though none converts it (`.chd`), from `tools_verifying_path` |
| `--to <format>` | `platform_detect.route_for` (the Convert panel's planner's decision) over `format_route` / `format_candidates` (below) |
| `--auto` | `routes.convert.plan_auto_targets` (`plan_auto` plus `checked_compression`), the Auto route's planner |
| Plan | `routes.convert.prepare_batch`: `plan_job` per file; each skip a `BatchSkip` (#504) |
| Queue and run | With a server running: `POST /api/jobs/batch?report_skipped=true` on it, each job followed with `GET /api/jobs/{id}` (`cli._run_remote`, `services.server_client`). Alone: `routes.convert.queue_prepared` into the `job_manager` singleton, started with `process_queue()`, emptied with `JobManager.drain()` and stopped with `JobManager.shutdown()`. Fed in chunks below `max_queue_depth` when it is set. A group the planner refuses is re-planned one Image at a time, so only the Image at fault fails |
| Delete | `JobManager._delete_check`, so `--delete-source` gets the Round-trip check where the mode has one and `--container-only` is the job's `container_only` |
| Ledger | `ledger.record` as a terminal listener, as in `main.py` |
| Verify | `ToolPlugin.verify` bounded by `verify_timeout`, then `verification_store.mark_verified`, or `clear` on a conclusive failure only |

**`format_route(path, target, platform, registry)`** returns `(mode, None)` or
`(None, reason)`. The CLI reaches it through `route_for` after
`detect_platform`: a Conflicted Image
is skipped with the detection reason (Auto's wording), a decided Platform is
passed in, an Unsorted Image passes None. `--any-platform` (the `CHD_MODE=cli`
alias) passes None for every Image. `target` is a format (`chd`, `.rvz`) or a mode id. For a
format, the candidates are the Auto-eligible modes (`ModeSpec.auto_target`)
whose read set (`mode_reads_extensions`) matches the Image and whose
`output_path` for it ends in the format, kind regardless (so `--to iso` reaches
`dolphin_iso` and `nkit_restore`, `--to cue` reaches `extractcd`). A decided
Platform keeps the modes that claim it (`platform_slugs` or
`auto_platform_slugs`, canonicalised; a mode with none, or a Platform no mode
claims, rules nothing out). The rest are ranked by `_best_reader`, the same
ranking `default_mode` uses. When the winner's matched read extension is the
target format the Image is left alone ("Already CHD"). A mode id is used as
given once its command reads the Image (`mode_reads_extensions`, so createdvd
never gets a `.cue`) and it serves the Platform. `target_formats` lists every
format some mode writes, read from `output_path`, for argument checking, with
a compound output's last extension too, so `7z` and `zip` resolve per Image to
the romz mode for its ROM. `_can_read` asks a tool's `converts_path` only about files its own
`input_extensions` cover, so chdman's `.chd` readers are not refused by a tool
that leaves `.chd` out of its tool-wide inputs.

**One process works a data directory (#533).** The server, `run-rules` and a
`convert` that runs its jobs itself never work one data directory at once. Each
holds the worker lock (`services.scheduler_lock`, §3.3.17's Rule scheduler
lock, `scheduler.lock` in the lock dir) while it works: the server for its
life, `run-rules` for its pass, `convert` while its jobs run. `convert`
decides where its jobs go before it routes and plans them (`cli._worker`),
so the tools ready where they will run (the server's `GET /api/tools`, or
this process's) decide which Images are converted:

* `--server URL` names a server: the jobs go there.
* The lock held by a server: the jobs go to it. The server names its host in
  the lock's holder note when it takes it, and its port at once, from
  uvicorn's launch (`main._launch_port`: `--port N`, `--port=N` or
  `UVICORN_PORT`; uvicorn binds only after the lifespan starts and tells the
  app nothing), so a server on `PORT=9090` (`run_dev.sh`) is found before any
  request. Each `/health` request also names the port it arrived on
  (`SchedulerLock.note_port`), for a launch that named none, and writes the
  note again when its last write failed (`_write_record` answers False), in
  place: the lock file is never replaced, since the flock is on it. It also writes a random `id` and its own non-loopback IPv4
  addresses (`scheduler_lock.own_addresses`, stdlib only: the hostname's
  entry and the address a UDP socket would send from; none readable, none
  written), because a container's hostname resolves for its siblings on a
  user-defined network (Compose) but not on Docker's default bridge
  (`docker run`, Unraid). `Server.of_holder` lists `127.0.0.1` when the host
  is this one, else the host by name, then each address, at that port (8080
  until one is named). `Server.find_holder` asks each `/health` in turn, 3 s
  apiece and without the auth token, and takes the first that answers the
  note's `id` as `worker`, so another server on a stale address is never
  taken. None does: exit 3, before anything is queued, saying to pass
  `--server`.
* The lock free: `convert` takes it (`acquire("convert")`) and runs its jobs
  in-process as before. A server started meanwhile waits for it
  (`acquire_waiting`, logging who holds it), so it never starts work beside
  the CLI.
* The lock held by `run-rules` or another `convert`: this run waits its turn,
  logging who holds it every 30 s.

On a server, `cli._run_remote` queues each chunk with the server's batch
route (the CLI's own plan names the files; the server plans them again with
the same planner, and its `report_skipped` list marks what it left out),
follows each job with `GET /api/jobs/{id}` once a second, and prints each
line from the job it reads back (`_finish`, as in-process). A chunk the
server turns away with 429 (its queue full: its own `MAX_QUEUE_DEPTH`, which
may be below the CLI's window, or other clients' jobs) is never failed: it is
split in half until it fits, and a single job waits a poll, following this
run's jobs, and is offered again. A stop cancels
this run's jobs there (`DELETE /api/jobs/{id}`) and waits for them to end.
`Server.cancel` answers whether that is settled: cancelled, or already ended
or gone (404, the 400 for a job past cancelling). No answer, a 429 or a 5xx
leaves it pending, and `_run_remote` asks again each turn, for every job, so
one lost connection never abandons the others. A
server that stops answering fails the jobs still followed. The auth token,
when the server has one, comes from the same place the server reads it
(`COMPRESSATORIUM_AUTH_TOKEN` or `<data dir>/auth_token`).

So a CLI and a server never both work on one file: either the server runs
the job, or the CLI does while no server can start. The same holds for the
`MAX_CONCURRENT_JOBS` limit, which is now one process's semaphore. The
cross-process ticket queue that used to share slots between the two
(`ConcurrencyManager`: `convert_slot_<i>.lock`, `holder_<id>.lock`,
`queue_*.ticket`, `queue_counter`) is gone. The job manager orders its own
queue with an in-memory counter (`JobManager._tickets`). `verify` only reads:
it runs beside a server, under the same shared input lock a job takes, so it
never checks a file a job is writing, and a job never replaces one it is
checking.

Output, input, folder and Referrer locks are still `flock` files in
`CHD_CONCURRENCY_LOCK_DIR`, kept as the second wall. The default is
`<data dir>/locks` (`config._default_lock_dir`), so a separate container that
mounts the same `/config` shares them. The choice is a pure function of
configuration, never of whether a folder exists yet: with a data dir configured
(`CHD_DATA_DIR` or the constructor; the image sets `CHD_DATA_DIR=/config`) it is
always `<data dir>/locks`, created when chosen (parents included, `0o700`), and
one that can't be created raises at startup instead of moving the locks. Only
with no data dir configured at all is it the fixed `/tmp/chd-locks`, the same
for every process whatever `TMPDIR` says. Two rules keep a lock file meaningful
across processes (`LockManager`):

1. A lock file is unlinked only by a process holding its flock, while it holds
   it, and only when the path still names the held file (`_unlink_held`).
   Release, the stale probe (`_check_external_lock`), the stale sweep and the
   refused-output cleanup all go through it.
2. Every acquirer, once its flock succeeds, compares the held file with the
   path (`_still_linked`: device and inode, ENOENT counts as a mismatch) and
   retries on a mismatch.

The server and the CLI raise the soft open-file limit to the hard limit at
start (`utils.process_limits.raise_open_file_limit`). The CLI keeps at most
twice `MAX_CONCURRENT_JOBS` of its jobs queued (never more than
`MAX_QUEUE_DEPTH`), in-process and on a server alike, so a cron run does not
put a whole library ahead of the web UI.

**What a `--to` walk lists.** A folder walk lists what the web search lists
(`scan_tree`, tool-wide inputs). With `--to`, the same single walk also lists
every file whose name ends with an extension the target's modes accept and no
tool-wide input matches (`scan_tree(also_reads=...)`, fed by
`platform_detect.target_reads_extensions`: a mode id's `mode_reads_extensions`,
or for a format the union over the Auto-eligible modes), so a mode that reads
what its tool leaves out of its inputs (chdman's extract modes and `.chd`)
finds its files. `format_candidates` then decides per file. `--auto` keeps the
tool-wide walk. A folder that can't be read is skipped as the web search skips
it, and with `scan_tree(unreadable=[...])` the CLI reports it as a skipped line
("This folder can't be read: ..."), exit code unaffected.

**A split output is one logical Image.** A split makeps3iso build writes
`Game.iso.0`, `.1`, ... and no `Game.iso`, but its check is recorded under
`Game.iso`. A verify treats the parts as that one Image, registry-driven, with
the split-part lookup the delete and overwrite paths use: `ToolRegistry.split_owner`
maps a numbered part to its output when the name without its last suffix
carries a mode's declared output extension (`.iso` for folder_to_iso, so
`save.0` never qualifies and stays a plain file) and that mode's
`companion_outputs` for it lists the part. Containment
(`_skip_outside_volumes`) checks every real file such an Image's check reads
(`_logical_files`: the verify target and every part), named or found, and one
outside the Volumes skips the whole Image. The walk lists such parts
(`scan_tree(also_include=...)`), a named part maps the same way, and
`_to_logical` folds them into one item; a stored record whose own path is gone
is kept when its recorded mode's `verify_target` finds the file
(`_stored_logical`). The check is handed that target (the first part) and the
line and record stay on `Game.iso`. makeps3iso's verify then covers every
part, not only the first: its TITLE_ID readback reads part 0, and for a split
set (`MakePs3IsoService.split_set_of`, from the logical path or any numbered
part) `check_split_set` checks the whole set's structure: the ISO 9660 volume
size in part 0's descriptor must fit in the parts' summed size, so a later
part missing or cut short fails, and every part must read to its last byte (a
read error is no verdict). PS3 ISOs carry no checksums, so a later part's
bytes can't be proven beyond that. The job's own verify-after takes the same
path, since it is handed the same first part. The CLI's check of a split
Image also holds a shared lock on the logical `Game.iso` and every part for
the whole check, not only the first part: a rebuild holds just the logical
output's lock (`_take_output_locks` for a directory job), so an in-progress
build makes the check skip as busy instead of judging a half-written set.

**Discovered inputs stay inside the Volumes.** A folder walk follows file
symlinks, so every discovered file, folder and archive (for a member) goes
through the shared containment check the web's verify guard uses
(`is_within_configured_volumes`, which resolves the path) before any tool is
chosen (`cli._skip_outside_volumes`), run once on the final input set: for a
verify, after the stored checks under the folder are added
(`_with_stored_checks`, which dedupes the two sources on the real path, so a
checked file since replaced by a symlink is listed once). One that resolves
outside every Volume
is skipped ("It resolves outside the configured Volumes") and never read,
checked, converted or recorded, in both `convert` and `verify`. Paths named on
the command line are checked up front (`_check_paths`).

**One database initialisation at a time.** The web startup and every
non-dry-run CLI open the database through `services.db.init_app_database`
(Alembic to head, then the legacy JSON import). It runs under an exclusive,
cross-process `init.lock` flock in the shared lock dir (`db._init_lock`,
blocking up to `INIT_LOCK_WAIT_SECONDS`, 300 s, then an error), so several
processes started on a fresh database don't all run Alembic at once; one
that waited finds the schema at head, since `apply_migrations` reads the
current revision once it has the lock.

**Stopping during planning.** The CLI's planning runs blocking filesystem work
on `run_in_threadpool`, which can't be cancelled: SIGINT, SIGTERM or SIGHUP
during `--auto` planning (header reads) over a Volume that has stopped
answering waits for that read to return before the run ends. Jobs already
queued are cancelled as usual; the wait is only for the planning thread.

**One output, one job.** `cli._prepare` keeps every output path the run will
write, across groups and re-planned batches, and skips a later Image that
writes one already taken ("Same output as X"). The planner's own collapse
works within one batch only.

**Lazy, and read-only for a dry run.** Choosing the lock dir creates the
directory and nothing in it (`config._default_lock_dir`). The module singleton
`lock_manager` is built `lazy=True`: the directory and the stale sweep happen
on first use. A dry run wraps the run in `lock_manager.read_only()`, in which
no lock is taken or probed and no lock file removed; a lock file that is
present counts as held. A dry run queues nothing and takes no worker lock.

**Folder locks across processes.** A directory lock (folder_to_iso packing a
PS3 folder) protects its subtree only in the process that took it
(`_dir_locks`). The web mutation guard (`routes/files.py::_assert_path_not_in_use`,
behind rename and delete) therefore also asks
`LockManager.locked_folder_above(path, volume_root)`: it probes, without
blocking, the lock file of the path and of every folder above it up to the
Volume root, and refuses with 409 when another process holds one. So a CLI job
packing a folder blocks a web rename or delete inside it. The exact-path check
(`check_file_status`) runs first, as before.

**Lock identity is the real path.** Output and input locks are keyed, named
and recorded by `os.path.realpath` of the path, as folder locks always were
(the same rule `resolve_track` uses). A path reached through a symlinked folder
(`/volume/alias/Game.wia` into `/volume/real`) therefore locks the file it
names, and every probe (`check_file_status`, `locked_folder_above`,
`locked_path_below`) realpaths its query the same way, so a delete through the
real path, or packing the real parent, sees it. The identity is fixed when the
lock is taken (`_lock_keys` for outputs, the key stored beside each input
handle) and the release uses exactly that, even if a symlink changed since.
An output lock covers both places a write can land
(`LockManager._output_keys`): the destination the converter creates,
`realpath(dirname)` joined with the basename (not following a symlink at the
final component, so once an overwrite sweep has unlinked an output symlink
the new file's path is still locked), and, when the output is a symlink
today, its current real target, so nobody rewrites the old target mid-job.
Both are taken or neither, and `release_lock` lets go of exactly the keys it
took. The folder-above check runs on each of those identities too
(`_folder_locked_above_keys`: a dir lock here, or a held lock file on any
folder above the key, up to its Volume root), so `GAME/new.rvz ->
../outside.rvz` is refused while another process packs `GAME`, and any
conflict releases every key taken. The dispatch probe
(`JobManager._blocked_by_dir_lock`) asks the same thing through
`output_folder_locked_above`, over the same `_output_keys` list (codex
4577847011).
A split build (folder_to_iso `-s`) locks only its logical output
(`Game.iso`) and writes its numbered parts as it goes, with no lock file of
their own. So every guard about to mutate a path (the web's rename and delete,
`_assert_path_not_in_use`, and the job's delete check, `_check_delete_paths`)
asks one helper, `services.mutation_guard.locked_for_mutation`: the path's
own lock, and for a numbered part the lock on its owner, found by name
(`registry.split_owner_name`: a numeric last suffix on a name carrying a
mode's output extension, so `save.0` never qualifies). A part made after the
lock was taken (`Game.iso.3`) is covered too (codex 4577940381). The
in-process `_split_output_blocks` stays as the same-process check. Every output lock goes through this, the CLI's jobs included (they use
the same job manager), and companion outputs too.
The identity is filesystem bytes: `_lock_file_path` hashes
`os.fsencode(realpath)`, and the recorded path is one line, `hex:` and the hex
of its `os.fsencode` bytes, read back with `os.fsdecode`
(`_encode_recorded_path` / `_decode_recorded_path`, one pair for every lock
kind). So a native name a sheet reads (`Caf\xe9.bin`, a surrogate escape in
Python), or a path with a newline in it (`GAME\nDisc/Game.wia`, which the old
plain record cut at the newline; codex 4578027196), locks, records and probes
like any other path. A first line without the prefix is the older plain
record, still read, so a lock an older process wrote during an upgrade counts.
If taking a job's input locks raises anyway, `JobManager._fail_before_start`
ends the job FAILED and lets go of everything it holds (the input locks taken
so far, its folder and output locks, its concurrency slot): a job never stays
queued holding the slot, which would stall every later conversion at
concurrency 1.

**Archive deletion fails closed.** Deleting an archive member's Source deletes
the archive, so `build_delete_plan` (shared by the web batch, the job's
delete-time recheck, Rule sweeps and the CLI) refuses unless every member
belongs to the converted Image. `utils.delete_plan.archive_members_beyond_image`
lists the rest: the owned set is the member itself, the tracks its sheet names
(the sheet is extracted to a temp dir and each reference resolved with
`resolve_track`, confined there, in every spelling the sheet reads as), and its
`source_companions` split parts. Before anything is created from any member
name, every name must pass the shared extraction validator
(`ArchiveService._validate_member`: absolute, `..`, a drive letter, a
backslash, a NUL), no member may be a symlink, and no name may be listed
twice (two entries are two files; only one can be the Image); otherwise the
archive is kept and the summary names those members and says why
(`archive_kept_summary`: "has an unsafe member name X", "has a duplicate
member X", "has an ambiguous sheet spelling X", "also holds X", "can't be
read"). Names are compared as
they are, never collapsed. A sheet's tracks are matched per track across its
spellings: two spellings of one track matching two different members make
the sheet ambiguous (chdman opens one of them), and the archive is kept, the
rule #294 applies on disk. Only then is the sheet extracted
(which validates its destination again), and only then, for the registry's
on-disk split-part lookup, are the validated names laid out as empty files in
a fresh temp root under the app's temp dir (`ArchiveService._create_temp_dir`,
`CHD_TEMP_DIR`, as extraction uses; a read-only system temp must not refuse
every archive delete; only when the app temp dir can't be created does it fall
back to the system temp, as extraction does, and every name has been validated
by then), each through `_prepare_destination`, which checks by real
path that it stays inside that root. A directory entry counts only when
nothing owned lives under it. Everything else, of any type and junk included, is an extra; there is no
list of harmless sidecars. An archive or sheet that can't be read counts as an
extra (the archive's own name). The error and the CLI line name the first three
(`describe_extras`).

**Companion outputs are locked like the output.** `JobManager._take_output_locks`
takes the cross-process exclusive output lock on the primary output and on
every companion output the mode declares (`companion_outputs`: extractcd's
`Game.bin` beside its `Game.cue`), all or none, before the job starts, and
`_release_output_locks` lets them all go on every path (the job's `finally`,
a deferral, a failure before the run). So the web guard's exact-path probe
refuses a rename or delete of the companion mid-write, and no other job can
take it to write. Directory modes' split parts are found on disk later and
keep their prefix guard.

**Input locks.** Every file-input job, whichever entry point queued it, holds a
*shared* flock on each file it reads (`JobManager._hold_inputs`: the Source, or
the archive for a member, plus `_track_candidate_paths`' cue/gdi tracks and
split parts) from the moment it takes its output lock until it ends
(`LockManager.acquire_input_lock` / `release_input_lock`). A folder job
already holds its subtree lock and takes none. The lock file is the one an
output lock on that path would use, so:

- readers share it, and two jobs reading one Source never block each other;
- anything wanting the path exclusively is refused while a reader holds it:
  the stale probe behind `check_file_status` (so the web guard's exact-path
  check answers 409 to a rename or delete of a file a job in *any* process is
  reading) and an output lock on that path (a job that would overwrite it);
- the last reader lets go by upgrading to `LOCK_EX|LOCK_NB` and unlinking while
  it holds that (rule 1); a reader that fails the upgrade leaves the file to the
  remaining reader. An acquirer retries briefly when the stale probe holds the
  file for a moment, and checks `_still_linked` (rule 2).

**Locks below a folder.** Lock files are named by a hash, so each one records
the path it locks as its first line when it is taken (`_record_path`; readers
sharing a file may each write it, the first line counts). For a folder, the web
guard also asks `LockManager.locked_path_below(folder)`: it reads the lock dir,
and for each lock file naming a path at or inside the folder (normalized or
resolved) probes it without blocking, as `check_file_status` does. Held by any
handle means a job in some process writes or reads something inside, and a
rename or delete of the folder answers 409. So a recursive delete of `vol/ps3`
is refused while another process packs `vol/ps3/GAME`, converts
`vol/ps3/Game.wia`, or writes an output there.

**Subtree exclusion across processes.** A folder lock and a lock on anything
inside it never coexist, whichever processes hold them and in whichever order
they were taken. Each acquirer takes its own lock first, then looks for the
other kind, and lets go and returns False on a conflict:

- `acquire_lock` (an output) and `acquire_input_lock` (a Source) look for a
  folder lock above the path, up to its Volume root (`folder_locked_above`,
  which is `locked_folder_above(..., include_self=False)`);
- `acquire_dir_lock` looks for any lock inside the folder
  (`locked_path_below(..., include_self=False)`) and for a folder lock above.

Since both sides lock before they look, two racing acquirers can't both miss
each other; at worst both back off. The job manager defers a job that hit
such a conflict (`_blocked_by_dir_lock` now also asks `folder_locked_above`)
and re-queues it after the retry delay plus random jitter, so two processes
don't retry in lockstep. The delete-on-verify step takes an exclusive lock on
each delete path (below), which also asks `folder_locked_above`.

**Limits of the lock dir.**

- Keys are container-local paths. Containers that share `/config` (the web UI
  and a CLI container) must mount each Volume at the same path, or they don't
  see each other's locks. Two containers mounting different folders at one path
  get false 409s, which fail safe.
- A probe that can't run (a lock file another uid owns, `ENOLCK` on a lock dir
  over NFS) counts as not held, here and in `check_file_status`. That holds
  while every process runs as the same uid, which the entrypoint's and the
  wrapper's `gosu converter` drop ensure.
- **No filesystem I/O under `_lock_mutex`** (codex 4579852997). The mutex
  guards only in-memory state: an acquirer claims the identity in `_locks`
  (or `_dir_locks`) under it, then resolves paths, stats the target and
  opens and flocks the lock file without it, and takes it again only to
  record the handle or roll the claim back (the overlap checks compare the
  stored, already resolved keys as they are); a release takes the identity out
  of memory under it and unlinks and closes the lock file after. A path
  resolve or stat that stalls on a dead mount therefore never holds the
  mutex against another caller, the event loop included.
- **A release lets go of the flock first, then the memory record**
  (`_release_key`, `release_dir_lock`): the handle is taken out under the
  mutex, the lock file is unlinked and unlocked outside it, and only then is
  the identity dropped from `_locks` / `_dir_locks`, so another job here sees
  the path free only once the flock is actually gone.
- **Letting go never stalls a caller** (codex 4579986280). A job's cleanup and
  a retried delete release their exclusive locks through one helper,
  `JobManager._let_go_in_background`: the release runs on a detached worker
  (never the shared threadpool), the caller waits for it at most
  `KEPT_DELETE_PROBE_SECONDS` and moves on, and the worker finishes on its
  own, closing every handle it took. The worker is started before the first
  await (`subprocess_runner.start_detached`, CodeRabbit on job_manager.py:3362),
  since the caller already gave up ownership of the paths: a cancel can only
  stop the waiting, never the release.
- Taking a lock is different: only contention (`BlockingIOError` from the
  non-blocking `flock`, or an existing output) answers "busy" and requeues.
  Any other `OSError` making or locking the lock file (`ENAMETOOLONG`,
  `EACCES`, `EROFS`, `ENOSPC`, `ENOLCK` ...) raises `LockFailed("Could not
  lock <path>: <reason>")` from every acquire (output, input, folder,
  Referrer), so `_process_job` fails the job with that reason before it
  starts and the CLI line says so, instead of requeueing it forever (codex
  4577893015).
- Lock file names are `<kind>-<base>-<sha256>.lock`, where `<base>` is the
  sanitised basename cut to 64 UTF-8 bytes on a character boundary
  (`_lock_name_base`, one helper for every kind) and the hash is of the whole
  `os.fsencode(realpath)` key, so a 184-byte cue track name still gives a name
  far under the 255-byte limit, and the identity is unchanged.

A job that cannot take its input locks (another job holds the path for
writing) is deferred and re-queued, like a folder-lock block. The locks are
taken on a worker thread that a cancelled job does not wait for, so each
acquisition is recorded only while the job's attempt is still live
(`_begin_input_attempt`, checked under `_input_mutex`); once
`_release_inputs` has ended the attempt (a cancel, the job's cleanup), a lock
the thread takes afterwards is released at once. At the
delete-on-verify step the job trades its shared read locks for exclusive ones
on every delete path (`JobManager._hold_delete_paths_waiting`, through
`acquire_lock(path, allow_existing=True)`) and holds them until the unlinks
finish (`_release_delete_holds`, also run in the job's `finally`); no job can
start reading a path between the checks and its unlink, since a new reader's
shared lock is refused while the exclusive one is held. A path another job is
still reading (a shared lock in any process), or one under a folder another
process packs, is waited for, polling without blocking, up to
`DELETE_READER_WAIT_SECONDS` (60 s). The wait checks the job's cancellation
before every attempt and again right after one succeeds, and `_process_job`
looks once more just before the unlinks: a cancel that lands while it sleeps
keeps the Source and ends the job CANCELLED, even if the reader lets go
before the next poll. Still held at the bound, the Source is kept and the
job ends COMPLETED with `job.warning` set to "Source kept: it was being read by
another job": the conversion succeeded, so it never fails for this. The CLI
prints the warning on that line and exits 0 (transient, as #307's run rules
treat it); a safety refusal, such as an archive with extra members, exits 1.

The hold spans the Referrer hook (§3.3.15). `_check_delete_paths`, run before
and after the hook, skips the lock probe for a path in this job's own
`_delete_holds` (that exclusive lock is the job's, and while it is held no
other job or process can lock the path) and still refuses any other locked
path. Every kept-Source ending after the hold (a Referrer that couldn't be
updated, a failed second check, a Cancel during the hook) completes the job
with that reason in both `job.message` and `job.warning`, so the CLI line
says why the Source stayed and exits 0; the job row shows it once
(`jobDetailLines` in `src/lib/util/jobDetail.js`). A Cancel seen at the last
look just before the unlinks ends the job CANCELLED instead. Two processes
rewriting one playlist take turns on the cross-process Referrer lock (§3.3.15,
*Concurrency*), so neither edit is lost.

**A Source kept for a reader is retried (#329).** The delete step itself
(plan again, `_check_delete_paths` with its `mutation_guard`, the Referrer
hook, the check again, the unlinks, the store clears) is one method,
`JobManager._delete_source_set`, shared by a job and a retry. When a Rule's
job keeps its Source for a reader, the sweep's terminal listener
(`note_job_finished`) records it in that Platform's Rule state, the same
`rules.state` row (`services/rules/store.py`) that holds the converted
history and the failed holds: `kept_sources[image key] = {source, output,
job_id, kept_at, attempts, decision, decision_generation}`.

`decision` is the delete decision identity (`_delete_decision_identity(rule,
library)`): every input to whether the delete is allowed. The Rule's
`DELETE_DECISION_FIELDS` (`OUTPUT_IDENTITY_FIELDS`: mode, output_dir,
compression, compression_level, split, duplicate_action; plus
delete_on_verify and check_level), the effective check level as the registry
answers it now (`effective_check_level`), and the Library
(`_library_identity`: its id and root, plus `decision_identity()` where a
Library has one, RomM's instance URL). `decision_generation` is a counter per
(Library, Platform) in `rules.state`, moved on by every invalidation
(`_invalidate_decisions`, which also drops the Platform's pending deletes).
Both are stamped on the job when it is queued (`_job_decision`), and a job
that ends after either moved records nothing, checked under `_sweep_lock`. Every later sweep, the web app's and
`compressatorium run-rules`'s alike (never a preview), plans the retries
under `_sweep_lock` (`_plan_kept_deletes`, before the Platform pass: the
checks below that need no file locks, at most `KEPT_DELETES_PER_SWEEP`) and
tries them once it has let go of the lock (`_run_kept_deletes`, codex
4580346573). Each try prepares the delete under its own file locks
(`JobManager.kept_delete`, the context `retry_kept_delete` and
`finish_kept_delete` also run through), which re-runs the output's check and
can take as long as a verify; only then does it take `_sweep_lock`, confirm
the entry is still the one planned (`_still_pending`: same Library
generation, same Rule revision, same entry), delete (`run.delete()`), and
record the outcome (`_settle_kept`). A long check never holds up another
sweep, a Rule edit or a job completion, and anything that changed the entry
meanwhile (and reported a started delete) wins. An entry being tried is
skipped by a concurrent sweep (`_kept_running`).

- **Any Rule change re-plans** (codex 4580441297). `set_rules` moves a
  Platform's `rule_revision` (in its Rule state entry) on for any change to
  its Rule, whatever the field: a selection filter, the schedule, the
  priority (only where the Platform has pending deletes, the only place a
  try can be under way, so no other state moves). The try records the revision it was planned under, and under
  `_sweep_lock`, right before the unlinks, a different one means it does not
  delete: the entry stays, and the next sweep plans it again under the Rule
  as it is. That comparison is the authority, so no field can be missed; the
  delete decision identity stays the cross-sweep pre-check.
- **A stop deletes nothing new** (codex 4580441303). The sweep's
  `should_stop` (`run-rules` on SIGTERM) reaches the tries: asked before
  each one, before its output check (`kept_delete(stopping=...)`), and at the
  last look before its unlinks (`_delete_source_set`'s `cancelled`). Once
  true, nothing new starts, no unlink begins, and the outcome is
  `KEPT_STOPPED`: not an attempt, nothing recorded. Unlinks already handed to
  their worker carry on and are recorded as in flight.

The preparation:

- It takes the output's lock and every delete path's, exclusively and without
  waiting, as the job did. One still held is `reader`: try again next sweep.
  Taking a lock stats the path, so each acquisition runs through
  `subprocess_runner.run_detached_undoable` under `KEPT_DELETE_PROBE_SECONDS`:
  a volume that does not answer is `kept`, and a lock the abandoned call takes
  afterwards is let go.
- Under those locks it establishes again, failing closed on each
  (`_kept_delete_snapshot`, bounded on a detached thread): the output has a
  passing check record naming this Source, with a full `produced_meta`; the
  output on disk is the very file that record fingerprinted (size, mtime,
  inode, device); the Source is inside the Volumes
  (`is_within_configured_volumes`); and a fresh delete snapshot of the Source
  equals the `produced_meta` Source fingerprints. Any of these not holding is
  `stale`: the entry is dropped and the Source kept for good. The companion
  outputs count as the output (CodeRabbit 4580102953): `produced_meta`
  records each one's fingerprint (`companions`), the retry takes their locks
  with the output's, and a companion gone, changed or new is `stale`. So is an
  archive member's Source, which has no `produced_meta`.
- **The output's check runs again** (`_recheck_output`, codex 4580346573).
  The stat fingerprint is only a cheap filter: an in-place rewrite of the
  same length with its mtime put back keeps size, mtime, inode and device,
  and the app's locks bind no writer outside it. So, under the locks, the
  job's own check runs again: the tool's container check
  (`JobManager._container_check`, the one method the job verifies with, under
  the tool's `verify_timeout`, not `KEPT_DELETE_PROBE_SECONDS`), plus the
  Round-trip (`round_trip`, the job's function) when the Rule's effective
  level is `round_trip`. A failed check is `stale` (dropped, nothing deleted,
  a Finding when the delete had started); one that reached no verdict
  (`VerifyUnanswered`, or a Round-trip whose digest timed out,
  `RoundTripOutcome.timed_out`) is a `kept` attempt. Finishing a started
  delete re-runs the container check only: the Round-trip cannot compare
  against a part-deleted Source. The stat filter runs once more right before
  the unlinks.
- **A re-check that finds an output bad records it** (#351). Every re-check
  records its result by one rule (`verification_store.check_verdict` /
  `record_check`): a pass refreshes the record (`refresh_check`), a real
  verdict that the file is bad marks it failed (`mark_failed`, the call a
  check Rule makes), and no verdict (`error`, `cancelled`, `abandoned`,
  `inconclusive`, `timed_out`, `uncheckable`) leaves the record as it was. The
  CLI's `verify`, the verify routes (the single check, its event stream and
  the batch, whose own timeouts and failed streams say `timed_out` or
  `error`), and `JobManager._container_check` (a job's check and every
  retried delete's re-check) all apply it, so no badge, `/api/verified`
  lookup or delete warning keeps showing a once-checked output as checked
  after a re-check found it bad. What counts as a verdict is decided where a
  tool's verify turns an exception into its result, by one helper,
  `subprocess_runner.verify_exception_event`: "could not read the file" (any
  `OSError` but a broken pipe or reset, a missing tool, a timeout) is
  `inconclusive`, and a header or format error is a verdict. Every verify
  path that catches an exception goes through it, so a transient EACCES or
  EIO never marks a checked output failed (codex 4582164683: eXoDOS had built
  a plain `valid: False` from an `OSError`). A Round-trip outcome is read by
  the same rule (`RoundTripOutcome.as_verify_result`): only digests that were
  both read and differ (`mismatch`) are a verdict, so a retried delete whose
  Round-trip finds other content marks the output failed and keeps the
  Source, and one whose sides could not be read records nothing. In
  `_container_check`, a result with no verdict (but `uncheckable`) raises
  `VerifyUnanswered`, like the outer bound: a job fails as before, and a
  retried delete counts an attempt instead of dropping the entry
  (CodeRabbit 4582189529). The verify event streams persist before they
  publish (`routes/info.py::_publish_terminal`, one helper for both streams):
  the producer writes the terminal verdict, then queues the terminal event,
  so a reader that sees it, and may stop reading and cancel the producer at
  once, can rely on the record (codex 4582270883). The write is its own task
  under `asyncio.shield`, so a cancel stops the waiting, never the write, and
  the verify lane is let go only once the write has finished
  (`_release_after_writes`): no newer check of the same file can start and
  be overwritten (CodeRabbit 4582189521). The batch counts the verdict the
  producer recorded.
- Then `_delete_source_set` runs, as for the job. A Referrer or path check
  that keeps the Source, or a removal the filesystem refuses part way, is
  `kept`: try again.
- **A cancel once the unlinks began is recorded** (CodeRabbit 4580360083,
  4580360089). `asyncio.CancelledError` is not an `Exception`, so the delete
  step catches it on its own: a retry's `run.delete()` leaves `run.started`
  (the plan, as `unknown`), which the sweep records as a `deleting` entry,
  bounded by `_CANCEL_RECORD_SECONDS`; a job records `_unfinished_delete` (a
  Rule's job) or the started-delete Finding (a hand-started one), ends
  CANCELLED and is announced (`_notify_terminal`). Then the cancel goes on;
  nothing waits on the unlink worker, which owns the rest of the plan.
  **A handed-off delete counts as started** (CodeRabbit 4580360083): when
  `run_detached_mutation` hands the unlinks to their worker while they still
  run (a timeout or a cancel; `_hand_off_locks` marks `handed_off`), their
  outcome is unknown even with nothing seen removed yet, since the first
  unlink may be the one running. `unfinished_delete(planned, removed,
  in_flight=True)` then returns every planned file not seen removed, for the
  job, the retry and the finish alike, recorded as `deleting` or as the
  started-delete Finding; the next finish, or `clear_gone`, settles it
  against what actually remains, so a delete that went through just clears.
- **A job whose Library was removed reports its own partial delete** (codex
  4580441301). `forget_library` drops the Library's jobs from `_job_owners`
  and clears their `_sweep_owner` (`_orphan_job`), so `job._sweep_owner` only
  ever names a live owner: a job that stops part way afterwards files the
  started-delete Finding under `NO_LIBRARY` itself. A plan the job had
  already handed over is reported there by `_orphan_job`, and one whose
  `note_job_finished` finds the Library removed under `_sweep_lock` by
  `_remember_kept_source`.
- **A job's cleanup releases everything, cancel or not** (codex 4580441305).
  In `_process_job`'s `finally`, each release runs in its own enclosing
  `finally` (the output locks' bounded wait, then the folder lock, the input
  locks, the concurrency slot and the runtime bookkeeping,
  `_forget_job_runtime`, then the temp folder and the prune), so a cancel
  during one await skips none of the others; it goes on once they ran.

It may only proceed while the decision that kept the Source still stands:

- A paused Rule waits: the same `_rule_paused` gate as the Platform pass (an
  operator's pause holds unless the Platform is named, a validation pause
  always), and its entries are neither retried nor dropped.
- An entry whose `decision` differs from the one recomputed from the current
  Rule and Library, or whose generation is not the current one, is dropped
  without deleting. So is one whose Rule is gone or keeps its Sources.
- **The authority is the sweep's own plan** (codex 4580012814). The decision
  identity and the generation are fast pre-checks; before deleting, the retry
  asks the function the sweep decides with, `_inspect_candidate`, for this one
  Source under the current Rule and Library (`_current_plan` →
  `_planned_delete`: the Platform's Images listed once, the mode's Platform
  check, then the name, size and match filters, containment, the input
  check, the companions and the destination, bounded on a detached thread).
  Only a plan that still converts this Source to the same output deletes;
  any other answer drops the entry without deleting, and an unanswered or
  busy one is an attempt. The output exists by then, so the destination is
  asked as `overwrite` would; the duplicate policy is an identity input.
  Under `rename` the recorded output may be the numbered sibling the rename
  gave that destination (`output_conflicts.is_renamed_from`, the same
  derivation `get_unique_output_path` probes with); `overwrite` and `skip`
  take it exactly (codex 4580078019).
- Apart from that, the record's check must meet the Rule's current effective
  check level (`retry_kept_delete(required_check=...)`, `_check_satisfies`):
  Round-tripped meets either level, Intact only the container check. So does a
  Source no longer inside the Library's root as it resolves now (checked
  fresh, bounded; an unanswered volume is an attempt).
- Invalidations move the generation on: `forget_converted_locked` for every
  Platform it targets, and `set_rules` for a Platform whose decision identity
  changed (a retarget through `forget_converted_locked`, any other decision
  field directly). So every caller of `forget_converted_locked` drops pending
  deletes and outruns in-flight completions: Forget
  history (`forget_converted`, the Rules and Libraries routes, RomM's), a
  Rule's retarget (`set_rules`), and RomM's identity cleanup
  (`routes/romm.py::_run_identity_cleanup`). `forget_checks_locked`, which
  that cleanup also calls, drops the `source_kept` Findings, keyed by Image
  like the "not Intact" ones. A Library's removal (`forget_library`) runs the
  same `_invalidate_decisions` before it wipes the state.
- `_invalidate_decisions` drops a pending delete that has not started
  silently (the Source is whole), but reports one in `state: deleting` first
  (codex 4580328581), through `_report_started_delete`, the same helper and
  Finding (`findings.started_delete`, naming the files left in
  `findings.REMAINING`) as a `finish_kept_delete` whose output no longer
  stands. Where the Library will not hold that Finding (its removal, or
  RomM's identity cleanup, which drops RomM's own) it goes under
  `findings.NO_LIBRARY`, keyed by the Source's path (`owner=`).
- One entry's exception is that entry's `kept` attempt, logged with its
  traceback; the sweep goes on to the other entries and Platforms.

`reader` and `kept` count an attempt; at `KEPT_DELETE_MAX_ATTEMPTS`
(10) the entry is dropped and a `source_kept` Finding (`findings.SOURCE_KEPT`)
names the Source, cleared once the Source is gone (`findings.clear_gone`).
Both writes run detached and bounded, and carry a `current` predicate on the
Library's generation, checked under the Findings write lock, so a write that
outlives the sweep's wait writes nothing once the Library was forgotten or
its RomM identity changed (codex 4580078026).
The sweep's result counts `kept_sources_deleted` and `kept_sources_given_up`
when either happened.

Every path probe of the shared delete step (`build_delete_plan`, the path
resolution, `_check_delete_paths`' per-path checks) runs on a detached thread
under `_CANONICAL_PROBE_SECONDS` (`_bounded_delete_probe`), for the job and the
retry alike: a volume that stops answering refuses the delete instead of
stalling the event loop.

A retry holds file locks while it prepares, and `_sweep_lock` while it
deletes, so every stage that touches the filesystem (but the output's check,
under its own verify bound) is bounded by `KEPT_DELETE_PROBE_SECONDS` on a
detached thread, and a timeout is a counted `kept` attempt (codex 4579683118):

- the Library containment check (`_inside_library`, through `_probe`);
- the output lock and the delete-path holds (`run_detached_undoable`);
- the check record lookup (`verification_store.get_record_blocking`);
- the Source snapshot and identity checks (`_kept_delete_snapshot`);
- in `_delete_source_set` (its `probe_seconds`): the plan, the path
  resolution, the path checks, the Referrer hook
  (`update_before_source_removal(bound=...)`), and the store clears
  (`clear_blocking`), where a timeout after the unlinks only leaves a stale
  record of a Source that is gone.
- **A started delete is one unit** (codex 4580176201). The whole set of
  planned unlinks (`_remove_in_order`) is one mutation, for the job and the
  retry alike: a caller that stops waiting hands the rest of the plan and its
  locks to the worker, which removes every planned file before it lets go.
  An `unknown` retry moves its entry to `state: deleting` with the planned
  list; the next sweep does not plan it again (a part-deleted Source is
  expected) but calls `JobManager.finish_kept_delete`: under the output's,
  companions' and remaining files' locks it re-checks that the output and its
  companions are still the checked ones and that each remaining file is one
  the check fingerprinted, unchanged, then removes them. If the output no
  longer stands, the remaining files stay, the entry is dropped and a
  `source_kept` Finding names the part-deleted Source; it is never dropped
  silently.
- **Once a file is removed, the delete has started** (codex 4580227623). One
  decision, `job_manager.unfinished_delete(planned, removed)`, applies to
  every outcome: once any planned file is gone, anything but "all removed"
  (an error, a timeout, a cancel) leaves the files still to remove, and the
  entry goes to `deleting` (`KEPT_STARTED`, or `KEPT_UNKNOWN` for a timeout);
  only a failure before the first removal is a plain `kept` attempt. A Rule's
  job that stops part way hands the rest to its sweep
  (`job._unfinished_delete`, recorded as a `deleting` entry, or a Finding if
  its decision no longer stands); a hand-started job, which has no pending
  store or Library, files the `source_kept` Finding at once and fails saying
  how much of the Source is gone. That Finding's owner is the explicit
  `findings.NO_LIBRARY` (the empty id, which no Library can have): the
  Findings view lists it as "Started by hand", Dismiss does not ask a Library
  that is not there whether it is current, and no Library's removal or RomM
  identity cleanup touches it. Every reporter of a started delete builds the
  one Finding, `findings.started_delete`: the Source as its path (where Open
  folder goes) and every file left in `cause[findings.REMAINING]` (CodeRabbit
  4580360093). With no sweep to settle a hand-started one, the Findings view
  runs `clear_gone` for it as it loads (bounded by `_SETTLE_SECONDS`,
  detached, at most one in flight: `_settle_running`, so loads during a stuck
  one start none and a dead mount costs one probe slot, CodeRabbit
  4580360066). It clears once every file left is gone (the same "gone needs a
  folder that lists entries" evidence as `NOT_INTACT`; with `REMAINING` the
  path alone is not enough, since the Source's first file may be the one
  removed). A Library's started-delete Findings carry `REMAINING` too and
  settle the same way in its sweep. `clear_gone` looks at the files outside
  the write lock, so its delete is a compare-and-delete on the row id and
  cause fingerprint it looked at (CodeRabbit 4580360075): a Finding a producer
  updated meanwhile stays.
- Each unlink is a mutation, not a probe, so it is never abandoned with its
  locks (below). One that outlives `KEPT_DELETE_PROBE_SECONDS` makes the
  retry's outcome `unknown`: the Source may or may not be gone. That is not
  an attempt, and the entry stays; the next sweep's own checks settle it (a
  Source that is gone is dropped as `stale`, one still there is retried).

The record names the Source by its real path, so `_kept_delete_snapshot`
compares it with the Source resolved now: a Library reached through a symlink
is still the same Source (codex 4579683123).

`run_detached_undoable(func, *args, timeout, undo)` (`subprocess_runner`) is
the shared form of a bounded call that takes something (a lock): when its
caller gives up, whatever the abandoned call returns later is handed to
`undo`, so nothing it took stays held.

A bounded helper tells its deadline from the call's own `TimeoutError` (a
network filesystem's `ETIMEDOUT`; `asyncio.TimeoutError` is the built-in
`TimeoutError`) by who finished, not by type (codex 4579904147): the call
records that it settled, and how, before it returns or raises (`_Finished`).
A call that settled with its own error has that error re-raised as itself,
also when it failed just as the wait ran out (CodeRabbit 4580102968; a
caller's cancel still propagates as the cancel); an
answer that came just as the wait ran out is returned; only a wait that ran
out while the call still ran is the deadline: `DeadlineExpired` from
`run_detached_bounded` (the read-only probe form) and `run_detached_undoable`,
`MutationInFlight` from `run_detached_mutation`, "stopped responding" from
`run_detached_within_stall_bound`.

Read-only probes may be abandoned; irreversible mutations may not
(codex 4579725632). `run_detached_mutation(func, *args, timeout, hand_off)`
runs a mutation (an unlink) on a detached thread. When its caller stops
waiting while it still runs (the timeout, None for no bound, or a cancel),
`hand_off()` takes the locks it runs under from the caller and the worker
releases them only after the mutation has returned or raised, so nothing that
waits on them (a new job's input lock) is granted while it can still take
effect. The timeout raises `MutationInFlight`. "Who owns these locks now" is
one object, `HeldLocks`: a job's output locks (the output and its companions,
`_output_locks`) and a retry's output lock are each one, passed to
`_delete_source_set` as `travelling_locks`; a hand-off `take()`s them, and the
owner's own `release()` (`_release_output_locks`, the retry's cleanup, run off
the loop) is then a no-op (CodeRabbit 4579839955). Only a worker that started can
own the locks: when none could be launched (`ProbeCapacityExceeded`) or the
caller gave up before it began, nothing is handed off, the mutation never
runs, the caller keeps and releases its locks, and `MutationNotStarted` (a
counted `kept` attempt for a retry, never `unknown`) says so (codex
4579801887). `run_detached_undoable` needs no such case: a call that never
started took nothing, so there is nothing to undo. `_delete_source_set` runs every
unlink through it for the job and the retry alike (`_hand_off_locks`: the
delete holds, plus a retry's output lock).

**Root.** `scripts/compressatorium` started as root execs `/entrypoint.sh
compressatorium ...`: the entrypoint's PUID/PGID remap and `gosu converter`
drop run unchanged, and the re-executed entrypoint hands straight back to the
command before printing anything to stdout.

**No verdict is not a failure.** The shared verify error
(`SubprocessRunner._verify_error`) takes `inconclusive=True` next to
`cancelled` and `abandoned`, and `collect_verify` keeps it. It is set when the
check ended without judging the file: the runner's overall timeout and stall
bound in `run_verify`, the one-shot timeout in `capture_verify`, z3ds's own
timeout, and the open probe's errors ("File not found", "Could not open
<file> to check it: [Errno N] ..."); not for an empty file or a wrong
extension, which are verdicts. `valid` stays False, so nothing is deleted on
it, but the CLI's verify keeps a stored check unless the failure is
conclusive.

**Whether the verifier can open the file is decided before it runs** (#359).
A verifier that can't open its input exits non-zero (chdman's "Permission
denied" on a `chmod 000` CHD), and an exit reads as a verdict, so a file the
app cannot read used to lose its stored check. The shared verify entry probes
first: `subprocess_runner._probe_for_verify` sizes the file and opens it for
reading, detached and bounded by `_STAT_TIMEOUT`, raced against the cancel
event. One that exists but can't be opened (EACCES, EPERM, EIO) is
`inconclusive`, naming the file and the errno, and an open that stalls is
abandoned as before. A missing file is not "could not read": it is gone, and
each seam keeps its old answer (`verify_preflight`'s "File not found"; the
subprocess verifiers run and judge it).
Every tool goes through it: the Python checks via `verify_preflight`, the
subprocess ones via `SubprocessRunner.run_verify` and `capture_verify`
(`verify_readable`), so every caller (the verify routes, the CLI, a job's
check, a retried delete) leaves the record as it was. A verifier's own
non-zero exit after a successful open is still its verdict. No tool was found
whose open succeeds but whose exit still means "could not read"; such a tool
would say so through the plugin contract, not a branch on its name. The batch
verify counts an `inconclusive` file with the uncheckable ones ("can't be
checked"), not as failed, and the file menu's Verify warns rather than
reporting a failure. The batch and the CLI read "could not be checked" through
one function, `verification_store.cannot_check` (`uncheckable` or
`inconclusive`, #370). The CLI's `verify` prints an `inconclusive` file as
`uncheckable` with the reason and exits 4 (`cli.EXIT_UNCHECKED`) when nothing
failed. Its own timeout is no verdict too (`timed_out`, `inconclusive`), and an
exception from a check is read by `verify_exception_event`, as the check Rules
read it. An archive the safety rules refuse stays `skipped` (exit unaffected),
a real verdict stays `failed` (exit 1), and a path that does not exist is
still a usage error (exit 2), raised before anything is checked; exit 3 is
`run-rules` only. Among the Images checked the worst wins: 1, then 4, then 0.

**Ran out of time is no verdict, marked at the source** (#374). Every
producer of a result that ran out of time marks it `inconclusive` itself, so
every consumer (`cannot_check`, the batch, the file menu, the CLI's exit 4,
the check Rules' `outcome`, which now reads through `cannot_check` with no
`abandoned` case of its own) agrees: the open probe that never answered
(`_probe_for_verify`, `abandoned` + `inconclusive`), a verifier that outlived
SIGKILL (`abandoned_verify_error`), the verify routes' own timeout
(`_verify_timed_out`), and, as before, the runner's and z3ds's timeout and
stall events and the CLI's own timeout. `abandoned` still stops a walk over a
list (`stops_verify_batch`); the CLI's stop on it exits 4, or 1 when an Image
had already failed.

`inconclusive` is an allowlist, never a catch-all. An exception a check raises
is classified by one helper, `subprocess_runner.verify_exception_event`, for
every tool (maxcso, nsz, jwud, xiso, z3ds, romz): `ToolUnavailable` (a missing
program, library or prerequisite, e.g. zstd or py7zr), any `OSError` (a failed
launch, an input that could not be read) and a timeout are inconclusive; a
header or format error, a bad archive, and a `BrokenPipeError` or reset from a
decoder that quit early on bad data are verdicts. nsz without `prod.keys` and
z3ds without `zstd` say so the same way, and the ISO reader behind the
makeps3iso read-back raises its I/O errors (`read_iso_file(strict=True)`, down
through `_read_pvd` and `_list_dir`).

A verifier ended by a signal (a negative return code: SIGKILL, the OOM killer)
is no verdict either; a non-zero exit is. One helper decides it from the return
code, `subprocess_runner.verify_exit_event(returncode, message)` (through
`killed_verify_error` for a negative one), used by `run_verify`,
`capture_verify` and z3ds's own zstd pipeline.

**Can't be checked here.** `_verify_error(..., uncheckable=True)`, kept by
`collect_verify`, says the tool refuses to check this file at all, and nothing
will change that on a retry. romz sets it when an archive safety rule refuses
the archive (`services.archive.ArchiveSafetyRefused`, a `ValueError` carrying
`rule`): a symlink member, an absolute or `..` member path, an encrypted member,
or `CHD_ARCHIVE_MAX_ENTRIES` / `_MAX_MEMBER_SIZE` / `_MAX_TOTAL_SIZE`. The
message is "This archive can't be checked (refused by the archive safety rules:
<rule>)". The CLI reports it as skipped: exit code unaffected, a stored pass
kept. Only an archive whose bytes cannot be read or decoded fails. Encryption
comes from the listing (`ArchiveMember.is_encrypted`: zip flag bit 0, py7zr's
`needs_password()`, rarfile's), so it is known before `7z t` runs.

**Nothing waits on a terminal.** Every child the shared runner starts (`run`,
`run_capture`, `run_verify`) gets `/dev/null` for stdin, and every 7z command
that reads an archive (`x`, `t`) names a throwaway random password, built per
call (`romz._no_prompt_switch()`; no password literal), so an encrypted archive
fails at once instead of prompting. `7z a` names none:
there the switch would encrypt the archive being written.

**A check holds its file.** The CLI's `verify` takes the shared read lock jobs
use (`acquire_input_lock`) on the file and its `source_companions` before the
check (`cli._hold_for_check`) and holds it through the check and the
verification-store update, releasing it on every exit (a `finally`, so a
cancel or a stop too). No other process can take the file for writing in the
meantime, so a replacement can't be recorded as Intact. A file a writer holds
is skipped as "Busy: another job is writing it": no verdict, the record
untouched, not a failure. A dry run takes nothing and only looks.

**Provenance.** A plain re-check that passes (the CLI's `verify`, the web's
Verify, single and batch) records it with `verification_store.refresh_check`,
which updates when and which check passed and keeps the stored `produced_meta`
and `source_path`: refreshing a check never erases the mode that made the file,
which step 1 below routes by. A conversion records with `mark_verified`, which
replaces both, so an output rebuilt at the same path by another mode never
inherits the old one. Every passing conversion check records its verifier
identity (`JobManager._verifier_identity`: `mode` and `tool`), with or without
a delete snapshot; the fingerprints that back the re-run fast path come only
from the snapshot, and `_produced_meta_matches` refuses a record that has no
`source` and `output` fingerprints.

**A failed check keeps provenance.** A check that finds a file not Intact
calls `verification_store.mark_failed` (#305's API, hosted here): the row stays
with its `produced_meta` and Source, `check_passed` becomes `FAILED`, and every
read that means "this passed" (`is_verified`, `get_record`, `all_records`)
skips it, while `produced_meta_blocking` still returns what made the file. The
CLI routes a file with no passing record by that mode, so a repaired PS3 ISO
goes back to makeps3iso. A folder verify finds such a file too: its stored
checks come from `all_provenance` (every row's path and `produced_meta`,
failed ones included), while `--since` and the re-run fast path stay on the
passing-only reads, so a failed file is always re-checked. A job whose
Round-trip fails records its own mode and
tool for the output, marked failed. `clear` stays for a file that is gone or
replaced: a deleted Source, an authorized overwrite, a web delete or rename.

**Which tool checks a file** (`cli._verify_tool`), in this order:

| Step | When | Verifier |
| --- | --- | --- |
| 1. Stored record | the file has a stored check | the tool of the mode in its `produced_meta` (makeps3iso for a PS3 ISO it built), with no Platform narrowing. A recorded mode that cannot verify is reported "can't be checked", the record kept, not a failure. A legacy record with no mode never guesses among tools for different Platforms: for a shared extension the detected Platform picks among every tool that reads or produces the name, and without a Platform it is skipped |
| 2. Detected Platform | no record, and a tool for another Platform also reads the extension (a generic `.iso`) | the claimants that serve `detect_platform`'s answer; none serves it: skipped as unsupported |
| 3. Skip | no record, a shared extension, and no Platform (Unsorted or Conflicted) | none: skipped, "its Platform couldn't be determined" |

Otherwise the most specific claimant (`tool_for_verify`) checks it. Claims come
from `ToolPlugin.claims_check(path)` through `registry.tools_checking_path`,
wider than `verifies_path` (the listing's Verify/Info gate, unchanged). romz
decides by shape first (`RomzService.claims_for_verify`, the one place): an
archive that reads and plainly is not a single-ROM one (no ROM, several files,
a texture pack, an encrypted header) is not romz's and is skipped; one that
cannot be read or decoded is claimed and fails; a single-ROM archive a safety
rule rejects is claimed and reported "can't be checked" (above). A folder run adds the stored checks under it, and one no
tool can check is reported, not dropped. "Shared" counts only tools that name
another Platform, so the Platform-less chain tool does not make `.cso` shared.

Readiness is per operation: `ToolPlugin.verify_is_ready()` (default
`is_ready()`), asked through `registry.tool_is_ready(tool, purpose="verify")`.
jwud's WUX check needs no JWUDTool or Java, and makeps3iso's reads PARAM.SFO
in Python; z3ds's needs `zstd`. Each says so through
`verify_required_binaries` (§3.4, #494), and `verify_not_ready_reason` names
the missing program. The CLI asks once per tool per run, dry
runs included, and skips a tool that cannot check here: exit 1 when the file
was named on the command line, a reported skip inside a folder.

`subprocess_runner.stops_verify_batch(result, abandoned)` and
`BATCH_STOPPED_MESSAGE` are the one stop rule for a walk over many files: true
when the result says `abandoned` or when the check's `collect_abandonment()`
sink caught something (a deadline that cancelled a check stuck on dead storage
leaves its detached read behind without saying so in any result). The web UI's
batch verify and the CLI's `verify` both wrap each check in the sink and end
there.

**Terminal listeners hear each job once.** `_notify_terminal` announces a job's
end exactly once, whichever path gets there first: a flag per job id
(`_terminal_announced`), set under `_terminal_mutex` before notifying, so a
queued job cancelled while the start path runs (the cancel path, then
`_fail_before_start` or the run's own terminal path) is announced once, with
the ending that won. `_fail_before_start` keeps an ending that already
happened and only lets go of what the job still holds. The id leaves the set
when the job is deleted.

**`JobManager.drain()`** waits until every queued run has ended, cleanup
included: a job notifies its listeners before `_process_job` removes its temp
dir and prunes history, so a caller that leaves on the notification alone
cancelled that cleanup and left an archive's extracted copy in `/config/temp`.
Inline runs are covered by the queue's `task_done`, parallel runs by
`_job_tasks`, folder-lock requeues by `_requeue_tasks`.

**`JobManager.shutdown()`** stops the dispatcher and maintenance loops, awaits
the listener tasks still running (Ledger writes) and makes the queue, its
semaphore and the create lock new, because asyncio binds them to the loop that
first waits on them. `add_terminal_listener` ignores a callback already
registered; `remove_terminal_listener` drops one. `cli._job_runner` wraps this
lifecycle (Ledger listener, `process_queue()`, cancel, `shutdown()`) for both
`convert` and `run-rules`.

**`run-rules` (#307).** One pass over the stored Rules, like one tick of the
server's scheduler, then exit. Under the scheduler lock and with the database
open, it lists the Libraries from `services.libraries.all_libraries()`, the
list the server's scheduler sweeps: RomM and every folder Library (§3.3.11),
or the one `--library` names (`romm`, `folder:<id>`). They are listed only
now because the folder Libraries are stored in the database. For each it runs the Library's `before_pass(dry_run=...)` hook
(§3.3.11), then `rules.sweep.sweep(library, should_stop=...)`: the scheduler's
own sweep, so each Rule's switch, days, window, interval and caps apply, and
the Ledger rows, `after_queue` hooks and Rule state are written the same way.
The Library's master switch (`schedule_enabled`, RomM's auto-convert toggle) is
not read: running the command is the switch. `rules.sweep.note_job_finished`
is a terminal listener, as in `main.py`, and the jobs are waited for with
`JobManager.drain()`. Each job's line is printed as it ends, under the Library
whose sweep queued it.

Then one Inbox pass (#433, `cli._inbox_pass`, §3.3.25) over the Inboxes of
the Libraries it swept, still under the scheduler lock and inside the job
runner: it restores intake's settle memory, polls those Libraries' Inboxes
(each intake job's line names the Library from `job.library_id`),
drains the jobs and copies the poll started, finishes them
(`intake.finish_all`), saves the settle memory and runs
`inbox.report.after_pass` (RomM's scan, the `inbox_pass` notification). It
prints one `{"inbox": true, ...}` line per Library whose Inboxes placed or
left something. An Image left with a Finding, or a RomM scan that wasn't
asked, doesn't change the exit status. A poll that reports an `error` (the
folder gone or outside a Volume, the Library missing, or the poll raised;
not `poll_running` / `inbox_removed`) prints a `{"inbox": true, "status":
"error", "error": ...}` line and exits 1, as a failed sweep does (Codex
4607809607); so does an exception in the pass. A stop before the pass skips
it; a stop during it reaches `poll_all(should_stop=...)` and `poll`, which
check it between files as they check `revoked`, so no further file is
admitted while what already started drains (Codex 4607809613). The server's
tick passes nothing: its predicate never stops. `--dry-run` polls nothing and prints
a `skipped` / `dry_run` line per Library with Inboxes.

Compared with the server, a pass restores the Hasheous toggle saved in the web
UI (`routes.dat.load_hasheous_override`, as startup does; folder-Library
recognition reads it) and does the RomM upkeep `before_pass` covers
(identity-cleanup replay, one settle pass), and nothing else the server starts:
no DAT sync, no re-pin settler loop, so re-pins this pass records wait for the
next pass or the server.

Errors and exit status. As in `run_forever`, an exception from one Library's
hook or sweep is caught (`cli._sweep_one`), reported as an `error` line for
that Library, and the next Library runs. Exit 1 when that happened, when a
sweep result has any `errors` entry (`unreadable`, `tool_not_ready`,
`identity_cleanup_pending`, `queue_failed`, `destination_claimed`, ...), when it
stopped for `library_unresponsive`, `not_configured` (a Library that has
Rules but cannot be swept) or `queue_full` (the queue refused a batch: the
sweep puts the batch size in that Platform's summary as `unqueued`, and the
line says how many Images were left), or when a job failed or was cancelled. The same
applies to `--dry-run`. A job the shared delete step completes with
`job.warning` (`SOURCE_KEPT_FOR_READERS`: the Source was still being read once
`DELETE_READER_WAIT_SECONDS` ran out) is a success: its line carries the
warning as `reason`. A delete refused for safety raises in that step, so the
job ends FAILED and counts.

Stopping. `sweep()` takes `should_stop`, asked before each Platform, every
0.2 s while a Library lists a Platform's Images (`_wait_unless_stopped`: the
listing runs on a detached worker bounded by `_LISTING_SECONDS`, and a stop
abandons it to finish on its own), before each Image is inspected, before each
delete snapshot is read, and again right before a batch is queued; once true
the sweep queues nothing more, does not stamp that Platform's clock, and ends
with `stopped_reason == "stopped"`. The server passes nothing. On SIGTERM,
SIGINT or SIGHUP `run-rules` sets `state.stopping` (which `should_stop`
reads) and cancels the queued and running jobs; after the sweep returns it
cancels again, for a batch let in between, and only then drains. A Library's
`before_pass` does not read the flag (RomM's settle pass gives a large output
its full hashing time, `bounded=False`, which can run for an hour), so
`cli._until_stopped` runs it as a task and cancels it once `state.stopping` is
set (checked every 0.2 s); the Library is reported `stopped` and no later
Library runs.

`--dry-run` is `sweep(library, dry_run=True)` inside
`preferences_store.read_only()`: the Rule store's first read can write (the
legacy-key copy, the one-time Round-trip upgrade), and in that block a write
lands in an in-memory overlay that later reads return, so the database is left
as it was. The overlay is on the store instance, not per task, so it is for a
process doing one thing (the CLI), never the server. Unlike the editor's
Preview it keeps the Rule clock (`ignore_schedule` is not set). With no
database a dry run lists `services.libraries.without_a_database()` (RomM: the
folder Libraries are rows of the database), so an unknown `--library` still
exits 2, then reports `no_rules` for each.

**One scheduler per data directory (`services/scheduler_lock.py`).** The server
and `run-rules` each hold an exclusive `flock` on `scheduler.lock` in the lock
directory above (`<data dir>/locks` by default, so every container mounting the
same `/config` sees it). The file is never unlinked, so the lock is always on
the file every process opens, and the kernel drops it when the holder dies. The
holder writes a note into it (`by`, `pid`, `host`, `since`) that
`SchedulerLockHeld` reads back to say who holds it.

- `run-rules` calls `acquire`: up to a second of retries (so a dry run's
  momentary probe never turns a real pass away), then `SchedulerLockHeld`, and
  the CLI prints why and exits 3 (`cli.EXIT_LOCKED`) having touched nothing. It
  takes the lock before opening the database and holds it through `drain()`.
- The server's `main.lifespan` calls `acquire_waiting` before `_serve` opens
  the database, and releases the lock in `finally` once `_serve` has cancelled
  the Rule scheduler task. While another process holds it (a `run-rules` pass,
  or a second server on the same data directory) startup waits: a
  non-blocking `flock` every `_WAIT_POLL` seconds with `asyncio.sleep` between,
  logging the holder on the first miss and every `_WAIT_LOG_EVERY` (30) seconds.
  uvicorn only acts on SIGTERM once startup returns, so `_watch_stop_signals`
  wraps its handlers for the wait: a SIGTERM or SIGINT is noted, passed on to
  uvicorn's handler, and ends the wait with `StoppedWaiting`, which startup
  turns into a `RuntimeError`. The lock file is closed on every way out.
- uvicorn opens its port only once startup returns, so a waiting server answers
  nothing, not even `/health`. While it waits, `acquire_waiting` keeps
  `<lock dir>/scheduler.waiting.<hostname>` (the holder note inside, rewritten
  each time it logs) and removes it in `finally`: on success, on
  `StoppedWaiting`, on cancellation. A marker left by a waiter killed outright
  is removed by the next `acquire_waiting` on that host. The Docker
  `HEALTHCHECK` exits 0 while the marker for its own `$(hostname)` exists, so the
  container is not marked unhealthy during a long `run-rules` pass. It is named
  per host because containers sharing `/config` share the lock dir, and one
  container's wait must not make another's broken server look healthy.
- A dry run only calls `check()`: it opens an existing file read-only and
  probes with a shared non-blocking `flock`, creating nothing.

The stale-lock sweeps leave the file alone (`LockManager` only touches
`filelock-*` and `referrer-*`). `convert` takes it while it runs its jobs
itself and hands them to the server that holds it (#533, §3.3.17);
`verify` does not take it: it only reads, under shared input locks.

### 3.3.18 Check Rules (`services/rules/check.py`, `services/image_checks.py`)

A Rule has a kind, `rules.CONVERT` or `rules.CHECK`, and its identity is
(Library, Platform, kind): a Platform can have a conversion Rule and a check
Rule. A check Rule has no target format (`rules.is_check_rule`).
`normalize_rule(raw, kind=...)` drops a `CONVERT` Rule with no mode and gives a
`CHECK` Rule none, with `check_budget_gb` (1 to 1048576, default 50), the usual
window, weekdays, timezone, priority and `enabled`. A conversion Rule whose
window starts and ends at the same time is refused as `invalid_window`
(paused, the editor says why) and `_window` keeps it literal, so it never
converts all day. `invalid_window` is recomputed from the submitted window on
every save, for every kind, and a client-sent value is ignored, so a corrected
window re-enables the Rule; the editor never sends the flag
(`rulesSource.svelte.js`). `invalid_pattern` is the one flag read back from the
stored Rule, because the refused pattern itself is replaced by none. A check
Rule's `interval_minutes` is
fixed at `CHECK_INTERVAL_MINUTES` (a day) but the schedule does not read it: a
check Rule is due once per window (`schedule._is_due`): the window is open on an
allowed weekday (an overnight window's tail belongs to the day it opened) and
`last_run_at` is before the moment this window opened (`_window_opened`,
which resolves the wall-clock start with `_instant`: a start in a
spring-forward gap opens at the first real instant after it, a start in a
fall-back repeated hour at its first occurrence, so either opens once; for a
check Rule a window whose start equals its end is the whole day
(`schedule._window`), as is no
window: the whole local day, opened at midnight). A run that starts late
does not push the next night later, and one that crosses midnight counts for
the window it started in. Its output folder is never read. Each kind has its own preference rows
(`store.rules_key(kind)`, `store.state_key(kind)`: `rules` / `rules.state` and
`rules.check` / `rules.check.state`), each keyed `"<library id>:<platform id>"`,
so the two kinds never overwrite each other and run on their own clocks. No
table change: Rules live in preferences, and migration 0014 (on #304's 0013)
holds only the check times. `sweep.get_rules` / `get_state` take the kind;
`sweep.set_check_rules` replaces the check Rules. The editor payload carries
`check_rules` and `check_state` beside `rules` and `state`, and a save replaces
`check_rules` only when the submission has it.

**Sweep.** `_sweep_locked` walks every (kind, Platform) in priority, Platform,
kind order, gating each on its own `enabled` and its own kind's state, and
hands a check Rule to `_sweep_check`. Run now ignores both clocks, so it runs
both kinds and a check spends another budget. A check Rule's Run now stamps
`last_manual_run_at`, not `last_run_at` (either stamped with the `now` the
sweep found the Rule due at, so a pick that runs past midnight counts for the
window that selected it), and `_is_due` reads only
`last_run_at`, so the nightly run still comes in its window; a Platform whose tool is not ready and that picked nothing is not
stamped at all. Nor is a run whose listing a finished job made out of date
before it was recorded (#353, `_listing_current`): a conversion that lands
just before the nightly run, during its pick, or while its check job runs
(#360: compared when the job ends, in `_record_run`) would otherwise wait a
full day for its first check. What such a run picked is still checked and
stamped per Image, so the next run moves on to the next stalest. A run that looked at a current walk and found nothing to check is
a real run and is stamped. Before the loop a real sweep calls `_clear_vanished` (bounded,
detached): `findings.clear_gone(library, NOT_INTACT)` drops a not-Intact
Finding whose file is missing while its folder lists other entries. A share
that is not mounted is a missing folder or an empty mount point and clears
nothing; an Image deleted from a folder it was alone in keeps its Finding until
it is dismissed. A folder walk calls the same after its reconcile. The per-sweep cap counts conversions only, so a
check Rule ordered after a capped conversion Rule still runs. `_sweep_check`
lists the Platform through the same bounded `_list_images` as a conversion,
reads `image_checks.order_stamps(library.id)`, and walks
`check.stalest_first` (no row first, then by the later of `checked_at` and
`attempted_at`, oldest first, then the key: an Image checked long ago that is
now inconclusive goes to the back too). An inconclusive check writes only
`attempted_at` (`image_checks.record_attempt`): no check time, no Finding,
nothing cleared, but the Image goes behind the others, so one that never
reaches a verdict cannot take every night's budget. Each candidate's disk questions go through `check.inspect` on a detached,
bounded thread, as a conversion candidate's do. It returns a skip reason
(`unresolvable`, `missing`, `uncheckable`) or a `Check`: the Image key, path,
size, the checking tool and the Image's file fingerprint. An Image already
being checked (`check.in_flight`) or converted (the active-path set) is
skipped. The other way round too (#330): every file of an Image a running
check picked (`check.in_flight_paths`) joins the sweep's active-path set
(`sweep._active_source_paths`) beside the queued and converting Sources, so a
conversion Rule on the same Platform skips it as active, records no failure
hold, and queues it on a sweep after the check is done. Readiness is the verifier's (`registry.tool_is_ready(tool,
purpose="verify")`), not conversion's: jwud checks WUX in pure Python without
JWUDTool, and z3ds needs zstd to check. A tool that is not ready skips its
Images and reports
`tool_not_ready`; it never yields a verdict. Images are taken stalest first
until the next would pass `check_budget_gb`, never skipping ahead to a smaller
one; the first is always taken, so one Image larger than the budget is checked
alone rather than never. Skipped
Images cost nothing. The Platform summary carries `check: true`, `checks`,
`check_bytes`, `budget_bytes`, `skipped_uncheckable` and `candidates`; the
result gains `checks` only when a check Rule ran, so a conversion sweep keeps
its shape. A preview stops there.

**Which tool.** `ToolRegistry.check_tool(path, slug)` is the first tool from
`tools_checking_path(path)` (#306's `claims_check`, so an archive too damaged
to open is still claimed and fails) left after `narrow_to_platform(slug)`. A
PS2 `.iso` is not given to dolphin-tool for sharing GameCube's extension; with
nothing left the Image is uncheckable. No per-tool branching: a new tool's
`verify_extensions`, `claims_check` and `platform_slugs` are all a check needs.
A stored check comes first, as in the CLI (#306): `check.inspect` reads the
Image's Verify record and passes its producing mode as
`check_tool(..., recorded_mode=...)`, and when that mode can verify its tool
checks the Image with no Platform narrowing (makeps3iso for a PS3 ISO it
built, though it claims no extension).

**A split output is one Image.** A split makeps3iso build writes
`Game.iso.0`, `.1`, ... and no `Game.iso`. `ToolRegistry.split_owner(path)`
maps a numbered part to `(Game.iso, mode)` when the name without its last
suffix carries a mode's declared output extension (so `save.0` never
qualifies) and that mode's `companion_outputs` lists the part, the same rule
the CLI uses (`cli._is_split_part`); `split_members(path)` gives the later parts for the
first one. The folder Library's walk lists such parts and `image_members`
folds them into one Image on `Game.iso.0`, so the budget pays the parts'
summed size and the lock and fingerprint cover every part. The check is
handed `Game.iso.0`, and makeps3iso's verify checks the whole set (#306). The
record lives under `Game.iso` (`Check.record_path`), so a pass refreshes it
there and a failure marks it failed there. `ToolRegistry.output_exists(path)`
is the presence rule for a stored record: the path is on disk, or it is the
logical name of a split output whose first part is (the same rule as
`split_owner`, read the other way). `verification_store.prune_missing`, run
by `/api/verified`, drops only records it answers False for, so a split ISO's
record and its producing mode survive a page load. `_hold` also takes the shared lock on
that logical path, with the first part and every later part, from the verify
slot through the recorded verdict: a directory-input rebuild locks only
`Game.iso`, so while one runs the Image is skipped as busy. Every file a check reads (each member,
split part or track) must be inside the Volumes, or the Image is skipped as
`unresolvable`.

**Run.** A real sweep passes everything every check Rule picked to
`check.start` once, after its loop, so a sweep is one run, one external job
(`ConversionMode.CHECK`, in `_EXTERNAL_JOB_MODES`) and at most one
notification. Each check Rule's run is recorded only once that job has
reached every Image the Rule picked (`check.start(..., on_finished=...)`, under the
sweep lock), so a run cut short by a cancel, a stop or a restart leaves its
Rule due. A sweep that picked nothing records at once. The stamp is still the
moment the sweep found the Rule due, so a night's run that ends past midnight
counts for the window that picked it. Until the run ends, its scheduled Rules
are held in `sweep._active_check_runs` (by Library and Platform), and the
scheduler picks nothing more for them, so the budget applies once per window.
A scheduled pick that comes up empty only because its Images are held
elsewhere for now records nothing, so the Rule is still due once they are
free. Held elsewhere is one count for every such reason: another check run
(Run now, `check.in_flight`), a conversion queued or running (the sweep's
active paths), or a writer's lock on any of the Image's files, which
`check.inspect` reports as `busy`. A pick that is empty because nothing is
eligible records as before. Each Image's new state goes to an open page as it
settles: `check._publish` sends `verification_changes` (the Verify record path
with its check now, or null once it is no longer Intact) on the run's own
`progress` event (`update_external_job(..., event_extra=...)`), which the jobs
stream forwards in order. The browser's `verification.applyJobEvent` applies it
beside a conversion's `verified` / `source_deleted` flags. Nothing is replayed
at the end, so a newer pass on a file (a repair and a re-verify) is never
overwritten by the run's older verdict. A subscriber's queue is bounded (100)
and drops when full, so the terminal event also lists the paths the run
settled (`verification_paths`, no verdicts). The store re-reads just those from
the database through `POST /api/verified/lookup` (at most
`MAX_VERIFIED_LOOKUP` paths a request, chunked by the client), with the same
`loadVerified` live-log replay a full reload uses, so a live event that lands
during the re-read still wins.
`on_finished(attempted)` is called exactly once as the run ends, with the keys
of the picked Images the run reached and settled (a verdict either way, no
verdict, or skipped as gone, changed or in use); a crash, a cancelled task or a
changed Library reports none. A Rule is recorded only when every Image it
picked is in that set, so a run that stops early (an abandoned verifier, a
cancel) leaves the Rules it never reached due; the stop itself still ends the
run at once. The hold is let go either way. Run now is not held back by it. The hold is in memory: one scheduler runs per data directory
(#307's scheduler lock), and a restart ends every run. A stop (#307's
`should_stop`, asked before each Platform, while a Platform lists, before each
Image is inspected and once more after the loop) starts nothing new: the
picked Images are dropped and no run is recorded, so the next sweep picks them
again.

**`run-rules`.** A check run is its own task, not a queued job, so
`JobManager.drain` does not wait for it. `_run_rules_pass` awaits
`check.drain(should_stop=...)` after the jobs, still inside the job runner, so
its terminal listener hears the run end. A stop cancels the running check jobs
there (their cancel event reaches the slot wait and the verifier), and nothing
is recorded for the Image in hand. `check.run_outcome(job_id)` holds each
run's counts; `cli._finish_check_run` fails the pass under the CLI's rule
(exit 1 when any Image failed, as `verify` does): an Image not Intact, or one
whose check reached no verdict. A cancelled or crashed run is already a
cancelled or failed job. The run's `check_failed` alert goes through the queued
`notifications.notify`, so `cli._job_runner` awaits `notifications.drain()`
before `job_manager.shutdown()` for every command that runs jobs, bounded by
three `SEND_TIMEOUT_S` sends in a row. For each Image in order the run waits for a slot on the
`verify` workload lane, the one the Verify action uses (raced against the job's
cancel event, so Cancel ends a queued run at once), and only then takes
#306's shared input lock (`lock_manager.acquire_input_lock`) on the Image and
its files (`image_members`), so nothing is held while it queues. `_hold` lets
go of what it took if an acquisition raises. It then compares the Image with
what the sweep picked (`_unchanged`: still a file, same `image_fingerprint`). A
lock it cannot get, or an Image gone or changed, is `SKIPPED`: nothing written,
no notification line. Then `tool.verify(path, cancel_event)`, bounded by
`tool.verify_timeout(path)`, the same call and bound as the Verify action. The
verdict is written (`_apply`, then `refresh_check` or `clear`) while the locks
are still held, so an overwrite conversion cannot replace the file between the
Verify and the record that describes it; the locks go once it is written,
cancel included. While they are held,
#306's delete step finds the Source in use: a delete-source conversion that
finishes then waits briefly (`_hold_delete_paths_waiting`) and, if the check
still holds it, completes with `SOURCE_KEPT_FOR_READERS` as its warning,
keeping the Source and its output. `check.outcome(result)` reads it:

| Result | Outcome | Writes |
|---|---|---|
| `valid` | Intact | `image_checks` row (`intact`), `verification_store.refresh_check` (keeps the record's `source_path` and `produced_meta`), `findings.resolve(not_intact)` |
| not `valid` | not Intact | `image_checks` row (`not_intact`), `verification_store.mark_failed` (keeps `produced_meta` and `source_path`), `findings.observe(not_intact)` |
| `inconclusive`, `abandoned` or `uncheckable` flag (#306: a verifier killed by a signal via `verify_exit_event`; an allowlisted exception or a timeout via `verify_exception_event`; a file this app's own rules refuse, such as an archive over the safety limits) | inconclusive | `attempted_at` only |
| any other exception from the check (`verify_exception_event` reads it as the tool rejecting the data) | not Intact | as not `valid` |
| `cancelled` | the run stops | nothing for that Image |

A failed check keeps the Verify row with `check_passed = "failed"`
(`verification_store.FAILED`, migration 0010's column): `is_verified`,
`get_record` and `all_records` skip it, so the Intact badge, the re-run fast
path and the CLI's `--since` all treat the file as unverified, while
`produced_meta_blocking` still returns the producing mode. A PS3 ISO that
failed therefore goes back to makeps3iso on the next run, and a pass
(`refresh_check`) restores the row and clears the Finding. With no size from
the Library (a RomM ROM without `fs_size_bytes`), `check.inspect` charges
`image_sets.image_size`: every file of the Image, split parts and tracks
included.

When `stops_verify_batch` says so (an `abandoned` result, or a read the
check's `collect_abandonment` sink saw written off), the run stops, as a batch
Verify does. Cancel on the
job sets its cancel event, which the running verify honours. Every write is
guarded by the Library's generation (`sweep.library_generation`), so a Library
removed mid-run gets no rows (`_apply` reports whether it wrote; when it did
not, the run ends cancelled and counts nothing for that Image), and `libraries.remove` drops its `image_checks`
rows with its Findings. RomM's identity cleanup (a new instance or library
root) calls `sweep.forget_checks_locked(romm_library)`: the generation moves,
then RomM's not-Intact Findings with their dismissals
(`findings.forget_kind`) and its `image_checks` rows go, since ROM ids are
reused by the new instance. The job ends with "N Intact, M not Intact".

**Findings.** `findings.NOT_INTACT` rows are written one Image at a time with
`findings.observe` (an upsert on `reconcile`'s terms, sharing its `_upsert`) and
`findings.resolve` (delete the row, keep its dismissal). `KINDS`, what a folder
walk reconciles, does not include it: a walk sees every Image, a check only a
few. The cause is `{tool, files}` (the file fingerprint when picked), so a
dismissal holds while the file is unchanged and a changed file that still fails
is shown again.

**Notification.** `check.py` registers `check_failed` with
`notifications.register_event`. A run with failures sends one `Event` listing
up to `MAX_LISTED_FAILURES` of them, with `data = {library_id, failed: [{image,
path, error}]}`, through `notifications.notify`. The run's job is external, so
the sweep's own `sweep_finished` message does not count it.

**Ledger.** Each external job mode declares itself in
`job_manager.EXTERNAL_JOB_MODES` (`ExternalMode`). `CHECK` sets
`in_ledger=False`, since a check run converts nothing, and `ledger.record`
skips a job only when `job_manager.in_ledger(mode)` says so. Metadata scans and
DAT matches keep their rows. `CHECK` also sets `started_by_hand=False`, so a
failed run is not #293's "A job you started failed"
(`notifications._started_by_hand`).
### 3.3.19 Editing a zip (`services/archive_edit.py`) and the eXoDOS repack

`rewrite_zip(src, dst, *, rename, add, is_cancelled, on_progress)` is the shared
way to produce an edited copy of a zip. The first user is the eXoDOS repack
(`exodos_repack`, #323); the planned Patch bench is the next.

- **Through zipfile's public API.** Each member is read (zipfile checks its CRC
  at the end of the read) and written back with its own compression method,
  timestamp and attributes. Member content is identical; compressed bytes may
  differ. A corrupt Source member fails the copy instead of reaching the product.
- **`rename`** maps a member name to its new name, or to `None` to drop it.
  **`add`** holds new members, written *first* (a reader that takes the first
  file of a kind finds them) and stamped `added_date_time` (the zip epoch by
  default), so a product does not depend on the clock. An added name replaces
  any copied member that would have taken it. Two copied members renamed onto
  one name raise `ValueError`.
- **Nothing that looks finished appears early.** The archive is written to a
  temp file named `.partial-XXXXXXXX.tmp` in the destination directory
  (`utils.path_utils.short_temp_name`, the name rule the Referrers' atomic
  writes use too through `short_temp_file`: short and fixed-length, so a target near the 255-byte name limit
  still writes; hidden, and ending in `.tmp`, which RomM's scanner skips by
  default, so a partial file is never listed as a game), fsynced, passed to
  the caller's optional `check(temp_path)`, given the Source's permission
  bits without setuid, setgid or sticky (`mkstemp` makes it `0600`, which a
  player running as another user could not read; `0o666 & ~umask` if the
  Source cannot be stat-ed), then placed with
  `utils.path_utils.place_without_clobbering`, the atomic no-replace move, and
  the directory is fsynced. Any failure, including
  `ConversionCancelled` from `is_cancelled`, unlinks the temporary file. An
  existing file at `dst` raises `FileExistsError` and is never replaced; the job
  pipeline's authorized-overwrite sweep clears it first.
- **What a hard kill leaves is swept (#331, `services/partial_writes.py`).**
  The writer (`claim_temp`) first creates the temp file's sidecar lock file
  and takes an exclusive `flock` on it, opened read-write without truncating
  (NFS refuses an exclusive `flock` through a read-only open), and only then
  creates the temp file, so the temp file never exists without a held lock.
  The temp archive itself is never locked: where locks are mandatory (SMB), a
  lock on it would stop the product check from reading it. The temp file is
  also journaled (one small file per temp file under
  `<data dir>/partial-writes/`, with the device and inode of its folder). The
  claim is all or nothing: if `flock` fails (`ENOLCK`), every descriptor is
  closed and every file it made is removed. When the temp file is placed or
  removed, the sidecar is unlinked while its lock is still held, then the lock
  goes, and the journal entry goes only if the temp file is known to be gone
  (`TempClaim.release(temp_gone=...)`). `temp_gone` comes only from an
  observed absence (no-follow `lstat`) or a successful unlink, never assumed:
  `place_without_clobbering` (on its link fallback) ignores a failed unlink of
  the temp name after linking the output, so `rewrite_zip` checks the name afterwards and tries
  once more. A temp name still there keeps the entry for the next sweep (the
  output is published, so the rewrite still succeeds), and a failed rewrite
  whose cleanup could not remove it logs that, re-raises the original error,
  and keeps the entry too. The sidecar's name has one rule, shared by the writer
  and the sweep (`sidecar_name`): `.partial-<16 hex>.lock`, the first 16 hex
  characters of the SHA-256 of the temp file's name
  (`utils.path_utils.companion_name`), so it is fixed-length and a 254-byte
  legacy temp name still has one.
- **The sweep is bound to the folder it checked.** A sweep resolves the
  folder's real path, requires `volume_containment(real) is True` ("can't
  tell" keeps everything), and opens it once with
  `utils.path_utils.open_dir_beneath`, the walk from `/` that opens every
  component `O_NOFOLLOW | O_DIRECTORY` (the same walk `read_beneath` uses).
  Every later step runs on that descriptor (`os.scandir(fd)`, `open`, `stat`
  and `unlink` with `dir_fd`), the sidecar's included, so a folder symlink
  repointed after the check cannot move the sweep outside the Volumes. It
  removes a leftover only when its name is exactly that pattern (or the
  `.<target>.partial-XXXXXXXX.tmp` spelling used before #331); it opens with
  `O_NOFOLLOW` as a regular file and is over a minute old; the sweep can take
  the sidecar's lock (through the same read-write open); and both names still
  name the inodes it opened. The sidecar goes with it, and a sidecar no
  leftover maps to is swept the same way. A journal entry is dropped only on
  proof that its file is gone: the sweep removed it, or the opened folder has
  the device and inode recorded when the temp file was made and no longer
  holds the file. A Volume that is unmounted, or an empty mount point in its
  place, makes a file look missing, so "missing" alone keeps the entry for the
  next start. `rewrite_zip` sweeps its destination folder before every write;
  at startup `main.startup_volume_work` sweeps the journaled files once the
  Volume roots are resolved, so a crash is cleaned up without walking the
  Volumes. The sweep is synchronous and runs only on threads that are already
  off the event loop: the rewrite's detached worker, and the startup thread
  that resolves the Volume roots. It needs no `run_detached_mutation` hand-off
  even when that worker is abandoned by the stall bound: its unlink is guarded
  by the folder descriptor, sidecar lock and inode checks the same thread
  holds while it unlinks, not by locks a caller could release.
- **No gating (#513).** `is_ready` and `verify_is_ready` are always true: the
  tool runs no binary and needs no setting. The template tree is found per zip
  by `ExodosService.candidates(path)`: the `EXODOS_DOS_DIR` override first, then
  `<folder>/!dos` for the zip's real folder and each parent up to
  `lock_boundary_root(path)`, nearest first. A candidate is used only when its
  real path is a directory inside a Volume (`is_within_configured_volumes`), and
  the template is read beneath that real path through `read_beneath`, so it
  resolves inside the `!dos` folder it came from. The first candidate holding
  `<game>/dosbox.conf` wins (`NoTemplate`, a missing file, moves on to the next).
  A template that is there but refused (a link, not a regular file, too large)
  or unreadable just now stops the search and fails the zip: it never becomes
  a repack without a conf.
- **No template.** A zip with no template found still repacks when its one game
  folder holds a DOS program (`.exe`, `.com`, `.bat`, what DOSBox Pure's start
  menu offers). That is the line between an eXoDOS game zip and any other zip
  with one folder: the tool is manual only and writes a new `.dosz` beside a
  Source it never changes, so a false yes costs one extra file, while a folder
  with nothing to run is refused. Such a product gets no `dosbox.conf`: the
  zip's own `<Game>/dosbox.conf` (in any case, `DOSBOX.CONF` too), never
  rewritten, is left out too (PR #517), or DOSBox Pure would load it instead
  of its start menu. The job's last update is a warning naming where it
  looked (`RepackPlan.no_template_reason`, worked out during the bounded
  inspection, so nothing is read on the event loop), and saying so when it
  left the zip's own conf out. A candidate tree that is missing is skipped;
  one whose stat fails just now (`EIO`) raises `TemplateUnreadable`, a
  `ProbeUnavailable` the probe never caches. The repack's own
  check requires the root conf only when it wrote one; Verify accepts a `.dosz`
  with no root conf when it holds a DOS program.
- **Content-refined source.** `.zip` is romz's archive extension too, so
  `converts_path` reads the zip's directory: every name passes
  `member_segments` and `enforce_archive_limits`, every member sits in one game
  folder (a loose root file refuses it), and `<tree>/<game>/dosbox.conf`
  is read through `read_beneath` (no symlink on any component, a regular
  file, one descriptor). The answer, a yes or a no, is cached per (path,
  size, mtime) of the zip and its candidate list, and kept only while every
  candidate tree is unchanged (#331, #513): each tree's, its game folder's
  and its template file's mtime, size and inode. That stamp is taken before the template is read
  and checked again after; if the tree moved in between, the answer is not
  cached. Only a verdict is cached (not an eXoDOS layout, no template): any
  OSError reading the zip or the template (a permission or I/O error, an
  unreadable folder, a timeout) raises `ProbeUnavailable` (`ZipUnreadable`
  or `TemplateUnreadable`; a `NotRepackable`, so a job still fails and keeps
  its Source) and is asked again on the next probe, since restoring access
  changes no stamp. Adding, removing
  or fixing a template in place is seen at once, while an unrelated zip is not
  reopened on every pass, and
  `convert` reads the template again the same way rather than trusting the
  earlier check, so a template swapped for a link in between is refused and
  the job fails with the Source untouched. `_NOT_CONVERTIBLE_REASON["exodos"]` names the plan-time reason. The
  frontend descriptor is *not* `contentRefined`: a `.zip` is an archive row,
  and the listing never annotates archive rows with `convertible_by`, so the
  content gate would refuse every zip. Any `.zip` can be ticked in this mode;
  `plan_job` skips the ones `converts_path` refuses, with the reason. A
  registry test pins that no content-refined tool takes an archive input.
- **The product.** DOSBox Pure mounts the loaded file as C: and, with the
  `dosbox_pure_conf=inside` core option, loads `C:\DOSBOX.CONF`. So the members
  are re-rooted (`<game>/x` becomes `x`) and the template is added as
  `dosbox.conf` at the root, as the first entry (RomM's default EmulatorJS,
  4.2.3, extracts the file and hands the core the first file with an extension
  it lists, which for DOSBox Pure is `.conf`; the nightly loads a generated
  `BOOTUP.BAT` and relies on `dosbox_pure_conf=inside` instead), its
  `mount`/`imgmount`/`boot` lines rewritten by `rewrite_conf` (bytes in, bytes
  out; CRLF and encoding kept). The product is
  `<name>.dosz` beside the Source, the extension DOSBox Pure documents as a zip,
  so it can never share its Source's path.
- **Warnings.** Lines that still point into `.\eXoDOS\` after the rewrite,
  folder mounts on a drive other than C:, and a C: mount of anything but the
  file root (`mount c .\HD`: C: is already the file, so the game's subfolder
  is never mounted) end the job with a `warning`
  update, the same shape a chain uses for a caveat. `JobManager._process_job`
  copies any update carrying `warning` onto `ConversionJob.warning` (the field
  #306 adds for a kept Source), so the caveat outlives later messages such as
  the verify step's; the job row shows it under the output path
  (`src/lib/util/jobDetail.js`).
- **A volume that stops answering.** The two reads before the rewrite (the
  zip's directory in `inspect`, the template) go through
  `run_detached_within_stall_bound`, like xiso's header reads: Cancel returns at
  once (`ReadCancelled` becomes `ConversionCancelled`) and a read with no answer
  past the bound fails the job. The rewrite itself runs through `run_detached`
  (a daemon thread, reported to any `collect_abandonment()` sink if it is left
  behind), never the shared pool. Cancel returns at once: `run_detached` raises
  `ReadCancelled` the moment the event fires. A worker that stops polling its
  `is_cancelled` (it polls between every chunk it copies or checks) for
  `conversion_stall_bound()` seconds, the same `CHD_PROGRESS_TIMEOUT` bound the
  subprocess phase uses, is given up on and the job fails. Either way the job
  loop returns, so the concurrency slot is freed while the worker is still
  stuck, and an `abandoned` flag makes `is_cancelled` true: `rewrite_zip`
  checks it as its last step before placement, so when the stuck syscall
  finally returns the worker removes its hidden temp file and publishes nothing.
- **Check.** `verify_stream` tests every CRC, the member names and limits, and
  the root `dosbox.conf`. It is served by the shared verify routes
  (`_VERIFY_CONFIG["exodos"]`, `/api/exodos-verify`), and `convert` passes the
  same check to `rewrite_zip`, so every job checks its own product before it
  is placed; a failure fails the job and leaves no output. The frontend
  descriptor has no `getInfo`, and `registry.infoToolForRow` (the row menu's
  Info rule) only resolves to a tool that has one, so `.dosz` rows offer Verify
  and no Info. The mode sets `supports_verify` without
  `supports_delete_on_verify` (listed in the registry test's declined set): the
  product differs from its Source by design, and the Source is the operator's
  eXoDOS install.
- **Never automatic.** The mode sets `automatable=False` (Rules refuse it)
  and `auto_target=False` (§3.3.14: Auto never picks it, so a `dos` folder's
  zips are skipped with a reason by the convert panel's Auto choice and by
  `compressatorium convert --auto`). `dos` is in Platform detection's alias
  table like every other Platform a tool declares.
- **Extending.** The pack-specific facts (template folder, pack folder name, conf
  name, Platform slug) live in a `CollectionProfile`. eXoWin3x and the other eXo
  packs are a new profile, not new logic. Only eXoDOS ships.

### 3.3.20 The duplicate finder (`services/content_keys.py`, `services/duplicates.py`, #388)

Finds the same disc stored more than once in one Library, across containers.
Read-only: a group is a Finding, and nothing is deleted.

**The content key.** One kind in the MVP, `raw:<sha1>`: the SHA1 of the raw
disc image a file holds.

| File | Where the SHA1 comes from |
| --- | --- |
| `.iso`, `.bin` (the longest declared extension, so not `.nkit.iso`) | the file's own SHA1, read by `content_keys` |
| Dolphin RVZ/WIA/GCZ/WBFS | `embedded_hashes` label `dolphin_disc_sha1` |
| DVD or hard-disk CHD | `embedded_hashes` label `chd_data_sha1` |
| CD CHD, CSO/ZSO/DAX, NSZ, WUX, Z3DS, sheets, archives | none: no key, reported as not checked |

The key builder never branches on a tool. It asks the tool that verifies the
file for `raw_image_sha1(path, *, cancel_event)`, a contract method beside
`embedded_hashes` and `content_digest`:

```python
raw_image_hash_labels: frozenset[str]   # labels whose value is the raw image SHA1
async def raw_image_sha1(self, path, *, cancel_event=None) -> str | None
```

`BaseTool`'s default reads `embedded_hashes` and returns the one hash under a
label in `raw_image_hash_labels`; with no labels it returns `None` without
asking. A tool that declares labels but gets none back (or two that disagree)
raises `EmbeddedHashUnavailable`: no key this time, nothing stored. Dolphin
declares `dolphin_disc_sha1`. `ChdmanTool` declares `chd_data_sha1` and
overrides the method: only a `.chd` whose metadata says DVD or hard disk
(`_holds_image`, the same tag reading the Round-trip uses) goes on to the
labelled hash; a CD or GD-ROM CHD is `None`, since its data SHA1 covers
2448-byte frames padded per track. A CHD with no current row in the metadata
store is read once with `chdman info` and the store primed. `chd_sha1`, the
overall SHA1, is never a raw label: it folds in the metadata hashes.
`tests/test_duplicates_388_real.py` checks against the real bundled chdman
that a `createdvd` CHD's data SHA1 equals its ISO's SHA1 (and its overall SHA1
does not), and that an ISO, its RVZ and its DVD CHD group together.

**CHD metadata freshness.** `chd_metadata_store.get_fresh_metadata` checks the
file's identity, its size and integer `st_mtime_ns` (`chd_metadata.size` and
`mtime_ns`, added by 0017), the size and mtime `content_keys` also ties to, so a
replacement that kept the float mtime never hands back the old hashes, for
every caller. One check (`CHDMetadataStore._describes`) answers it for
`get_fresh_metadata`, `is_stale` and `cached_media_type_blocking`, so they
cannot disagree. A row written before the identity was recorded is stale
everywhere: the metadata scan and the info route read it again, after which
`embedded_hashes` supplies its SHA1s as before.

**Stored per file, tied to its identity.** `content_keys` (migration 0017) holds
`path`, `size`, `mtime_ns`, `key` (None for a format with no key) and
`container`, and (0019, #389) the file's `dev` and `ino`.
`content_keys.stored(...)` returns only rows whose size, mtime, device and
inode all still match (`content_keys.Identity`), so a changed file, or another
file moved into its path with the same size and mtime, loses its key; a row
from before 0019 has no device or inode and is read again. The DAT match cache is not read
for this: its rows carry no size or mtime to tie a SHA1 to the file.

**The run.** `duplicates.start(library)` creates an external job of
`ConversionMode.FIND_DUPLICATES` (`ExternalMode(in_ledger=False)`, so never a
conversion and never a slot) and runs it as a task; one per Library at a time
(409 otherwise). It:

1. lists the Library with `Library.all_images()`, a new Library contract method
   returning an `ImageListing`: the Images, plus what the listing could not see
   (`unlisted` folders, `unreadable` files). `LibraryDefaults` walks every
   Platform's `images()` and reports nothing unseen; `FolderLibrary` returns
   every entry of a fresh walk, Unsorted and Conflicted included, with the
   walk's own `Scan.unwalked` and `Scan.unreadable_files`. The walk already
   skips junk (`utils/junk.is_junk_entry`, RomM's `.romm_tmp_*`). A walk
   already running is waited for, within `_LIST_SECONDS`;
2. leaves out a file outside the Volumes, gone, locked (`lock_manager.is_locked`)
   or on an active job's candidate paths: an output being written is never
   grouped (counted as `busy`);
3. reads each missing key one file at a time. An image's SHA1 goes through
   `open_reporting_reads` under `run_detached_within_stall_bound(report_progress=True)`;
   a tool's hash is bounded by its `verify_timeout`; stats by the stall bound.
   A file whose identity changed while it was read is not stored;
4. groups by key (two or more), re-checks each grouped file's identity and lock,
   and reconciles `duplicate` Findings for the Library. A stored group with a
   copy the run did not see (under an `unlisted` folder, an `unreadable` file,
   a stat or key read that failed, a file in use or changed while it looked)
   is held: left out of the observed groups and
   passed as `keep_under`, so it stays exactly as it was. The write's `current`
   predicate, asked under the store's write lock, is the commit point: it is
   false once the job is cancelled or the Library removed, and a rejected write
   ends the run as cancelled with nothing written. A cancel after it is too late
   to undo the write, so the run reports what it did (`findings.DUPLICATE`, one
   per group, `image_key` = the content key, `cause` = `{"key", "copies": [{"image_key",
   "path", "container", "size"}]}`);
5. stores the summary (`duplicate_runs`: keyed, read now, groups, `not_checked`
   by container, unreadable, busy), which `/api/findings` returns as
   `duplicate_runs`.

Every Image's path is read in the detached, stall-bounded inspection step,
never on the loop: RomM resolves `Image.path` lazily on its mount, so a mount
that stops answering ends that step at the bound, or on Cancel.

Each file's key is read under `collect_abandonment()`, and what it collected is
checked once, right after the read and before anything branches on its result
(a key, `KeyUnavailable`, `StorageAbandoned`): a child that outlived teardown,
or a detached read the stall bound gave up on, stops the run with nothing
written, as the metadata scan stops. An ordinary failure costs that file only.

The active jobs' paths come from `job_manager.active_job_candidates()`: the
jobs are snapshot on the loop (in memory) and their filesystem-dependent
candidate paths (a sheet's tracks) resolved in one detached, stall-bounded
step. `get_active_job_candidates()` is the blocking form, for callers already
off the loop (the sweep's and RomM's bounded probes, the delete route's
threadpool step).

Cancel reaches every wait. Each tool call the key builder makes
(`verify_timeout`, `raw_image_sha1`) is raced against the run's cancel event
through `subprocess_runner.until_cancelled`, which cancels the pending call and
gives it `CANCEL_SETTLE_SECONDS` to unwind. `workload_limiter.acquire(lane,
cancel_event=...)` ends a lane wait on a cancel and leaves nothing queued, and
`ChdmanService.info(..., cancel_event=...)` forwards it to `run_capture` so the
child stops.

A cancel, or the Library's removal (`library_generation` moved), ends the run
with no Findings written. `findings.clear_gone(library, DUPLICATE)` settles a
group once fewer than two copies are left; the folder walk calls it with
`NOT_INTACT`'s. A Library's records (every Finding with its dismissals, its `duplicate_runs`
row, its check times) are forgotten by one call, `library_records.forget`,
which both a folder Library's removal and RomM's identity change
(`rules_sweep.forget_checks_locked`) make, so a new record kind is added once.

Routes: `POST /api/libraries/{id}/find-duplicates` → `{job_id}`.

### 3.3.21 Staging output out of a scanner's sight (`services/staging.py`, #398)

RomM 5.4.1 indexes every file in a Platform folder, dot-prefixed ones
included, but skips dot-prefixed folders (the swap spike, #382; see the
findings under Phase 3 of `docs/ROMM_INTEGRATION_ROADMAP.md`). A scan that
lands while a conversion's temp file or unchecked output sits beside the
Source makes a duplicate ROM, and a ghost once the Source goes. So a job whose
output lands in a Library declaring the `HIDDEN_STAGING` capability
(`services/rules/library.py`; RomM declares it, folder Libraries do not)
writes into a hidden slot and places the output only once its checks pass.
The RomM swap (#391) stages through the same helper.

- **The helper.** `staging.open_slot(folder, *, make_folder=False)` makes
  `<folder>/.compressatorium-stage/slot-XXXXXXXX/` and returns a
  `StagedOutput`. The folder must be inside a configured Volume
  (`volume_containment(real) is True`), and with `make_folder` a missing one
  is made only after that check; it is opened with `open_dir_beneath`,
  the stage folder and the slot `O_NOFOLLOW | O_DIRECTORY` beneath it (a link
  in place of the stage folder is refused), and every later step works
  through those descriptors. `path_for(name)` is where a tool writes: the
  output's own name inside the slot, so a `.cue` naming its `.bin` is still
  right once moved. `place(names)` moves the named entries (files or
  folders) into the folder with `utils.path_utils.place_without_clobbering`
  given both folder descriptors (`src_dir_fd` / `dst_dir_fd`):
  `renameat2(RENAME_NOREPLACE)` / `renameatx_np(RENAME_EXCL)`, or where the
  filesystem has neither, a `linkat` then removing the staged name while it is
  still the linked inode.
  Each move is atomic and no-replace, so an entry another process creates at
  the destination at any moment raises `FileExistsError` and is never
  touched; the existence check before the moves is only a fast path. All or
  none: when one move fails, the ones already made go back into the slot the
  same no-replace way. Returns the `(staged, final)` pairs.
  `close()` removes what is left in the slot (an unplaced output, a tool's
  scratch) with the descriptor-based `shutil.rmtree(dir_fd=...)`, then the
  slot's lock file, its journal record, and the stage folder once no other
  slot is in it. `place` and `close` share a mutex, so a close waits for a
  placing still running on another thread. Everything is blocking.
- **Crash cleanup is the `.partial` sweep's (#331).** The slot's lock file,
  `slot-XXXXXXXX.lock` beside it in the stage folder, is created and
  `flock`ed before the slot exists and held until it is gone, and the slot is
  journaled in `<data dir>/partial-writes/` like a temp file.
  `partial_writes.sweep_recorded` (the startup sweep) removes a journaled
  slot only when it can take that lock, the slot is a real folder (never a
  link) older than `MIN_AGE_SECONDS`, and both names still name what was
  checked; then the stage folder if empty. A stage folder that is gone drops
  the record only on the same device as the one recorded (an empty mount
  point is another). `open_slot` sweeps its stage folder first
  (`partial_writes.sweep_stage`), so a slot whose record was lost goes the
  next time a job stages there. The name rules (`is_slot_name`,
  `slot_lock_name`) live in `partial_writes`, shared by writer and sweep.
- **In the job pipeline (`JobManager._process_job`).** `_open_stage` asks
  `libraries.library_for(output_path)` (detached, bounded; the output path as
  planned, never resolved, #413): a Library with
  `HIDDEN_STAGING` gets a slot, one without it (or no Library) writes in place
  exactly as before, and "can't tell" stages, since hiding the work is always
  safe. The slot opens with `make_folder=True` in the same bounded call,
  since staging runs before the tool's runner would make a missing output
  folder (a conversion into a new subfolder). A slot that can't be made
  fails the job; it never falls back to writing in sight. A slot that
  arrives after the bound gave up is closed (`run_detached_undoable`'s
  `undo`). `job.output_path` stays the final path; a local
  `write_path` (the slot's copy of that name) goes to `run_convert`,
  `post_convert`, the output size, the Intact check and the Round-trip. Once
  both pass, `_place_staged` moves the output and its `companion_outputs`
  into place under `run_detached_mutation` (the output locks travel with a
  rename the bound gives up on), and carries their store records (a CHD's
  disc ID, `chd_metadata_store.move_blocking`, `verification_store.move_blocking`)
  to the final paths. Then the verified record, the reuse snapshot and the
  Source delete run as before on the placed output. A job with no check
  places once the conversion ends. An output that fails a check is never
  placed: `_close_stage`, in the job's `finally`, clears its records and
  discards it with the slot, so a failed or cancelled job leaves nothing
  visible. That close always runs: like `_let_go_in_background`, its worker
  starts before the first await (`start_detached`), and with no detached
  slot free it gets a thread of its own, so the slot's descriptors and lock
  are never left open until a restart.
- **Hidden everywhere here too.** `STAGE_DIR_NAME` lives in `utils/junk.py`
  and is an ignored entry (`is_junk_entry`), so listings, search and every
  walk skip it.
- **Parking slots (#391).** `open_slot(folder, parking=True)` makes a
  `park-XXXXXXXX/` slot for the RomM swap's Source while the output takes its
  name. It is never swept (the sweeps match `slot-` names only) and not
  journaled in `partial_writes`; the swap journal records it. `take(name)`
  moves an entry of the folder into the slot, `restore(name, as_name=None)`
  moves it back out, both no-replace renames through the slot's descriptors;
  `remove(name)` deletes a file in it; `keep()` lets go and leaves the slot
  and its content in place. A job whose output a Referrer places itself keeps
  it staged until the Referrer hook (`StagedPlacement`, 3.3.15).

### 3.3.22 Deleting from a duplicate group (`services/duplicate_cleanup.py`, #389)

A `duplicate` Finding (3.3.20) becomes a cleanup the user confirms. Nothing here
deletes on its own.

**Suggested keeper** (`duplicate_cleanup.annotate`, called by `GET
/api/findings`). Database only, so listing the Findings touches no Volume:
`verification_store.records_blocking` (each copy's stored check, passing or
failed, looked up by the path as listed) and `dat_store.get_matches_batch`. A
copy whose check failed is never a keeper. Order: Intact (the latest check
passed and was made after the copy's mtime; the fingerprint a conversion
recorded in `produced_meta.output` is not read, since a re-check keeps it as it
was and it would bar a file that changed and then passed a check, Codex
4593499342; a passed check outranks everything), then a
copy whose key was computed from its whole content, then Recognized, then
smallest, then path. `keeper_reason` names the rule that decided. Each copy in
the Finding's cause now carries `mtime_ns` beside `size`: the identity its key
was read for.

**A proven keeper** (`keeper_problem`, Codex PRRT_kwDOQ9nULM64Lh-l). A copy may
be kept for a delete only when it is proven to decode to the disc: a current
passing check (fresh against its identity), or a key computed from its whole
content (`content_keys.key_reads_whole_content`: an `.iso`/`.bin` hashed here,
or a tool whose `raw_image_hash_rebuilds_content` is true, which Dolphin's
rebuilt disc SHA1 is). A key read from a header (a CHD's data SHA1) is only
what the file claims, so a damaged CHD is not trusted over a healthy ISO: the
plan and the delete answer 409, "check it first". A copy that failed its check
is refused outright. The row carries `keeper_problem` for the view.

**What may be deleted from** (`not_deletable`): only a group whose key kind is
in `HASH_EQUAL_KINDS` (`raw`), not one the last run held (`held_keys` in the
run summary), and with two or more copies. No run recorded for the Library
means "fully seen" is unknown, so no delete.

**The plan** (`POST /api/findings/{id}/delete-plan`, `{fingerprint, keep}`):
the Finding must still have the fingerprint the user saw (409 otherwise). On a
detached thread, bounded by `KEPT_DELETE_PROBE_SECONDS`, every copy and the
keeper are checked against the cause: inside the Volumes, present, the same
size and mtime, and still holding the group's key in `content_keys` for that
identity. For each copy but the keeper: the shared `build_delete_plan`
(confined to the Volumes), a refusal when a `.cue`/`.gdi` beside it lists it as
a track (`_sheet_problem`), and one when its plan would touch the keeper. The
track check is the shared `image_sets.owning_sheet` (the other direction of
`image_members`): a sheet may name a track in a subfolder (`Disc.cue` →
`tracks/Track.bin`), so it looks in the copy's folder and every folder above
it, up to and including the Volume boundary (`lock_boundary_root`), never
above (Codex PRRT_kwDOQ9nULM64L3Le). It fails closed (Codex
PRRT_kwDOQ9nULM64Lh-c): a folder on the way that cannot be listed, a sheet that
cannot be read whole (`utils.sheet_text.sheet_is_readable`: unreadable, or over
the size cap), a sheet whose tracks do not resolve as one set
(`sheet_track_list` is None, as when one reference leaves its folder: Codex
PRRT_kwDOQ9nULM64MWFq), or a copy with no Volume boundary blocks the copy,
since "is it a track?" is unanswered. The same distinction holds for grouping:
`image_members` no longer reads such a sheet as owning nothing; it keeps every
track name that does resolve, so those tracks are never listed as Images of
their own. The walk runs inside the plan's detached, bounded
step, so a slow Volume answers 503 rather than an unchecked plan. The Referrer changes come
from `referrers.plan_changes_bounded` with every delete path mapped to the
keeper, as a conversion maps them to its output. The answer has the
delete-plan route's item shape (`delete_paths`, `missing_paths`,
`unsafe_paths`, `errors`, `referrer_changes`) and `blocked`.

**The delete is a job** (`POST /api/findings/{id}/delete`, the `delete-file`
confirmation header and `{fingerprint, keep, delete_paths}`). The keeper's
full read makes it too long for a request, and its checks must not rest on
snapshots taken before it, so the route only does the quick, database-only
checks (`duplicate_cleanup._offered`: the Finding as shown, the keeper one of
its copies, `not_deletable`; one delete per Finding at a time, 409 otherwise)
and answers `{job_id}`. The job is an external job
(`ConversionMode.DELETE_DUPLICATES`, `ExternalMode(in_ledger=False)`, as
find-duplicates): progress and message through `update_external_job`, cancel
through its cancel event, its outcome on the job's new `result` field
(`{finding_id, keep, keeper_changed, results}`, also its `deleted_paths`,
`warning` and message), and a refusal as a failed job whose `error_message`
says why. `duplicate_cleanup.drain()` waits for the running ones. The UI keeps
its in-flight lock per Finding with the job id, follows the job in the jobs
store (`findings.noteFinishedDeletes`, from the Findings view), shows its
progress in the dialog, and settles the dialog when the job ends (closed, the
`keeper_changed` warning, or the failure's reason).

In the job, before anything is deleted, the Volumes' sheets are read again
(`image_sets.volume_sheet_tracks`, the same walk, stall bound and incomplete
rules as the run's record, Codex PRRT_kwDOQ9nULM64OuXm), and that fresh set,
not the run's, is what each copy is checked against: a sheet added since the
run keeps its tracks. The run's record stays the gate for offering a delete at
all. A walk that is incomplete or stalls fails the job with nothing deleted; a
cancel ends it cancelled. Then the plan is made again, must not be blocked,
and must list exactly the confirmed paths. Then
`JobManager.delete_duplicate_copies` takes the keeper's lock
(`_take_output`) and, under it and before any copy goes, proves the keeper by
its bytes (`_keeper_problem`, Codex 4593004806): its size and mtime must still
be the Finding's, then `content_keys.recompute` computes its key again from
what it holds now, never the stored row and never a header alone, and it must
be the group's key; its full fingerprint (size, mtime, inode, device) must be
the same after that read as before. An `.iso`/`.bin` is hashed whole under the
stall bound; any other format goes through the tool contract's
`content_sha1`, a full pass bounded by `verify_timeout` and raced against the
cancel (`BaseTool`'s default: a tool whose raw hash rebuilds the image, so
Dolphin rebuilds the disc; `ChdmanTool`: `chdman verify` must pass, which
recomputes the data SHA1 from every hunk, then the header read fresh). A format
with no full-content key cannot be kept. A read that fails, stalls, is
cancelled or gives another key refuses every copy and deletes nothing. The
job's cancel event reaches that read and stops the batch before the next
copy.

The keeper is opened (`_open_keeper`, through `run_detached_undoable`: a
descriptor delivered after the bound gave up is closed by the undo,
`_close_opened`, Codex 4593499336) and held open from that read to the end of
the batch; an image's hash is read through that descriptor
(`content_keys.recompute(fd=...)`), and any other format's full pass reads it
too (Codex 4593632748): the contract's `content_sha1(path, fd=...)` resolves,
once in `BaseTool`, a path that names the held file (`held_file_path`:
`/proc/<pid>/fd/<fd>`, checked by device and inode, which a tool's child can
open and which keeps naming that inode whatever is renamed into the old name),
and the tool's own pass (`_content_sha1(path, target)`) reads *target* while
*path* only names the format. Where no such path exists (no `/proc`), the
delete is refused for that keeper rather than reading the name. Before each copy, and once after the last
delete, `_keeper_still_held` checks that the batch still holds the keeper's
lock (`HeldLocks.holds`: a lingering Referrer operation may have taken it,
CodeRabbit 4593240091) and that the path still names the held file, with the
same device, inode, size and mtime (`_keeper_moved`, Codex 4593214751). Any
change stops the batch and what is left stays; a change seen only after the
last delete is reported (`keeper_changed`); the dialog then stays open with
a warning naming the copy kept and asking for a check, and the view shows a
warning, not a success (Codex 4593499350). This catches a rename swap. An
in-place rewrite by a writer with write access to the keeper cannot be caught
by any ordering here, since the same writer could corrupt it right after the
cleanup: that is inherent to keeping one copy. The copies being deleted are
not read in full (a decision, CodeRabbit 4593240082). Instead each must still
be the very file its content key was read from (Codex PRRT_kwDOQ9nULM64OuXs):
`content_keys` stores the file's device and inode beside its size and mtime
when the key is set (`content_keys.Identity`, migration 0019; a row from
before has none and matches no file, so it is read again), and under the
copy's holds `_copy_under_holds` requires the copy's identity now to equal
the stored key's and that key to be the group's. A file moved into the copy's
place with the same size and mtime is a new inode, so it is kept with no
re-hash. Only an in-place rewrite that puts the mtime back is not caught, and
only something that can already write the copy, and so delete it, can do
that.

Then for each copy it runs
the retried-delete pattern: `build_delete_snapshot` (the copy's own file must
still have the Finding's identity), its delete paths taken exclusively without
waiting (`_take_exclusively`: a copy a job holds is `reader`, kept), the
snapshot again under the holds, then `copy_problem`
(`duplicate_cleanup._copy_under_holds`): the content-key identity above, and
the sheet check again (the walk up and the job's fresh set of sheet tracks: a
sheet that names the copy since the plan keeps it, CodeRabbit 4593240099),
then the shared delete
step (`_delete_source_set`: per-path checks, the Referrer hook pointing
playlists at the keeper, the unlinks as one mutation, the copy's check and
metadata records cleared). Each copy's outcome is a `KeptDeleteRetry`.
Afterwards `duplicate_cleanup._settle` rewrites the Finding with the copies
left (`duplicates.finding_from_copies`), or resolves it once fewer than two
are, and only when the stored Finding still has the fingerprint the delete was
confirmed on (`findings.observe` / `resolve` with `fingerprint=`, checked under
the store's write lock, CodeRabbit 4593240053): a run that wrote a newer cause
meanwhile wins.

A sheet can reach a track through a link in another folder (`disc/Disc.cue`
names `disc/Track.bin`, a link to `shared/Track.bin`), which no walk up from the
target finds (Codex PRRT_kwDOQ9nULM64NUn9), and that folder may be in another
Library, or in none (Codex PRRT_kwDOQ9nULM64OXPI). `resolve_track` accepts a
track anywhere inside the configured Volumes, so ownership is read over the
same scope, from the disk, never from a Library's listing: the find-duplicates
job walks every configured Volume (`image_sets.volume_sheet_tracks`, over the
shared `walk_tree`, detached under the stall bound, cancellable), reads only
the `.cue`/`.gdi` sheets, no hashing, and records each one's track identities,
`(st_dev, st_ino)` with links followed (`image_sets.sheet_track_identities`,
through the same `resolve_track`), as `sheet_tracks` in the run's summary on
`duplicate_runs` (no migration), with the Volumes it walked as
`sheet_tracks_scope`. Junk and RomM's `.romm_tmp_*` staging are skipped as
every walk skips them; a linked folder is not walked into (its target is
walked where it is, when it is in a Volume), and a Volume nested in another is
walked once. The plan, and the check under each copy's locks
(`_copy_sheet_problem`), block a copy whose identity is any recorded track,
besides the walk up, which stays as a second guard.

No delete is offered without a complete, current record. A run with no record
(an older run) offers none. Nor does an incomplete one, stored as
`sheet_tracks: None` with what was missed in `sheet_tracks_problem`: a Volume
root that is not a folder, any folder (or entry) the walk could not see
(`walk_tree(unlisted=...)` collects them), a sheet that cannot be read whole
(unreadable, over the size cap, tracks that do not resolve as one set, a
track it cannot stat: `sheet_track_identities` raises, Codex
PRRT_kwDOQ9nULM64N2yy), or a walk that hit its stall bound. So does a run whose
Library listing did not see the whole Library (Codex PRRT_kwDOQ9nULM64OGLk:
any `ImageListing.unlisted` / `.unreadable`, named by
`duplicates.listing_problem`), and `not_deletable` also refuses a run whose
`unlisted` count is non-zero, so no flag says complete while another says
partial. A record whose `sheet_tracks_scope` is not the current Volumes
(`duplicates.volume_scope`, e.g. a Volume added since) is stale: no delete.
Since the record no longer comes from a Library's listing, a RomM Library is
covered the same way. The listing itself no longer shows a linked target as an
Image: `group_images` also counts a member's resolved link target as owned.

The rule throughout, so the class cannot recur: no helper that answers an
ownership question returns an empty answer for "could not tell".
`image_sets._named_tracks` returns None for a sheet it cannot read whole (`[]`
only for a file that is not a sheet, or a sheet that names nothing);
`sheet_track_identities` and `owning_sheet` raise `SheetOwnerUnknown`; every
caller handles it. `image_members` keeps listing what it cannot name (it
cannot hide files it cannot name) and says so; nothing that deletes relies on
it.

`owning_sheet` compares a sheet's tracks with the copy by file identity (device
and inode), so a case-insensitive share's `track.bin` is `Track.bin`; a track
that does not exist is compared by its case-folded real path, and any other
stat failure leaves the owner unknown (CodeRabbit 4593240059).

A first check that fails is recorded (`verification_store.mark_failed` adds a
failed row when there is none), so a raw ISO that failed is never read as
"never checked" and never kept. A group whose every copy failed has no keeper
and offers no delete.

### 3.3.23 The Image rename and rename to DAT name (`services/image_rename.py`, `services/dat_rename.py`, #425, #426)

**The shared Image rename** (`image_rename.rename_image(image_path, renames,
*, folder, library, cancel_event)`) renames every file of an Image (`renames`:
`{old: new}`, one entry per file) with its Referrers following, or nothing. It
takes the Image and its map, never a single path, so a multi-file Image (#426)
is the same call with a longer map. It runs the existing Referrer hook
(`referrers.update_before_source_removal`) with a `StagedPlacement` whose
`rename` is set:

- `staged` maps each new name to the file still under its old name, so file
  Referrers treat the rename exactly as a conversion's staged output: an entry
  that reaches the old file is rewritten to the new name, checked with
  `points_at` against the staged file, and the hook's `WriteGuard` watches the
  old files. No Referrer's planning changed for this.
- A Referrer that renames the file itself checks `placement.rename`. The RomM
  Referrer (`RommSwapReferrer._plan_rename`, `_RenameInPlace`) does: it asks
  RomM to rename the ROM's file (`RommClient.rename_rom`, the swap's own call),
  which moves the file and keeps the ROM id. No rehash and no journal row: no
  file is parked or deleted and the bytes don't change. The answer is read
  back: a 409 or any other answer changes nothing; a sanitized or misplaced
  name is put back and refused; no answer is settled by the read-back; and a
  cancel during the request reads RomM back and puts a sanitized name back
  before the cancel goes on, so RomM and the file agree on one of the two
  names. Archives are not left out as they are for the swap: nothing is
  rehashed. Counted as a running swap, so a RomM identity change waits for it.
  RomM renames one file within its folder, so `_plan_rename` refuses (a
  `ReferrerError`, which skips the Image in the preview) a map of several
  files or one that moves a file to another folder (#426): renamed under
  RomM, they would leave the ROM and its saves as a ghost.

**A sheet's Image and its folder (#426).** When `image_path` is a `.cue` or
`.gdi` (`is_sheet`) and `renames` holds its tracks, the placing step
(`_Files.move_blocking`, which the hook's placement runs) does three things in
order, all through the checked folder descriptors:

1. *Rewrite the sheet's track references.* `sheet_reference_changes` maps each
   reference as written (its folder part kept) to the track's new name. A
   reference is resolved through every spelling of the sheet in order
   (`sheet_text.sheet_spellings_of`, the same readings `sheet_track_list`
   picks from), so a Windows-1252 `café` whose file is spelled in UTF-8 is
   found; the native token is the one changed, and every renamed track must
   be reached by a reference or the rename stops. Then
   `utils.sheet_text.rewrite_track_names` changes only that token, in the
   quoting it had (double quotes when the new name needs them), reading the
   sheet as chdman does (native spelling, `chd_tokens`). Every other byte is
   kept, and the result must read back as the old references with the changes
   applied, or the rename stops. The write is atomic: the whole new sheet
   goes to a hidden file beside it (written on after a short write; one that
   accepts nothing fails), given the sheet's mode and group, synced, and
   only then renamed over the sheet, which must still be the file
   fingerprinted. A failure before that rename leaves the sheet exactly as
   it was and removes the hidden file. Once it lands, the new file's
   fingerprint is taken and the write is recorded in the rename's
   `WriteJournal` under the sheet's old path, like a Referrer's file.
2. *Rename each file* within its folder, as for a single file.
3. *Rename the folder* (`folder=Folder(old, new)`), last: only while it still
   holds exactly the Image's files, under their new names, and is the folder
   that was opened (device and inode through its parent's descriptor), with
   `place_without_clobbering` (a folder takes the no-replace rename). With a
   folder, `renames` names each file under the folder's new name, which is
   what the Referrers see; `inner_renames` gives the in-folder step. The
   folder itself is in the Referrers' map too (`referrer_renames`, for the
   preview and the hook), so an entry naming the folder (an ES-DE
   `<folder><path>./game</path>`) follows it like a file's entry. The locks
   cover the old files, their new paths in both folders, and the new folder.

A step that fails puts back the files it already renamed, newest first and
each only while it is still the file it moved, then raises. The files are then
found under their old names, so the usual compensation restores the sheet's
bytes and every Referrer from the journal. `where_blocking` counts the folder
too, and a file whose name doesn't change (it moves only with its folder) is
looked for under its one name.

**Bound to the folders it checked.** After the locks, each file's folder is
resolved, confirmed inside a Volume (`lock_boundary_root`,
`volume_containment`) and opened beneath the Volume root with no symlink on
the way (`open_dir_beneath`), and each file is fingerprinted through that
descriptor. The open is bounded with `run_detached_undoable`, whose undo closes
the descriptors of an open that answers after the rename stopped waiting. Every later look (`where_blocking`) and every move goes through
the descriptors: `place_without_clobbering(old_name, new_name,
src_dir_fd=..., dst_dir_fd=...)`, only while the file is still the one
fingerprinted. A folder swapped for a link to somewhere else after the check
can't redirect a move. Nothing is ever moved over another file.

**One rule for every ending: settle, inspect, carry or compensate, release.**
The rename runs as a task of its own (`asyncio.shield`), so a cancel of its
caller (a shutdown) never cuts it short; a Cancel reaches it through
*cancel_event*, which stops the hook. Then:

1. *Settle.* Every RomM request the hook handed off or abandoned
   (`HandedOff`, `lingering`) is awaited, and every Referrer worker the hook
   stopped waiting for is waited on (`WriteJournal.wait_idle`); such a worker
   writes nothing more once abandoned (`check_write`). There is no bound:
   deciding while one still runs could undo a rename it then completes. A
   RomM request ends at its client's timeout; a worker stuck on a Volume that
   stopped answering keeps the rename waiting with its locks held, and the job's
   message says what it waits for (`on_wait`, every `HANDED_OFF_SECONDS` /
   `SETTLE_SECONDS`). A cancel during the RomM put-back also reads RomM back
   before the cancel goes on.
2. *Inspect* where the files are, through the descriptors.
3. *Carry or compensate.* Every file under its new name: renamed, whatever the
   hook reported (RomM may have finished after the hook gave up). What the app
   knows about each file follows it: the check record, the cached `chdman
   info` and the DAT match (`DATStore.move_match_blocking`), under the listed
   path and the real path the folders had when they were opened. Every file
   under its old name: not renamed, and each Referrer file the hook wrote is
   put back. Anything else is never guessed at; the reason says so.
4. *Release* the locks (`lock_manager.acquire_all` took them all or none).

**Putting Referrers back** uses the hook's new `WriteJournal`
(`update_before_source_removal(..., journal=)`, carried on the `WriteGuard`):
`files.rewrite_in_place` records each file's bytes before and after once the
write lands (`record_write`), including a write made by a phase worker the hook
stopped waiting for. `referrers.restore_written` then puts each one back,
newest first, with `files.restore_in_place`: under the same locks, and only
when the file still holds exactly the bytes the hook wrote. The bytes it held
are written back, never a re-plan, so an entry the rename never changed (a
playlist line that already named the new name) is left as it is, and no file
moves, so a new name someone else took meanwhile is never touched. A file
changed since is left alone and named in the reason.

**Two additions to the Referrer contract.** `plan_changes(..., placement=)`
plans as the hook will (`referrers.base.planning`: `staged_placement()`
answers it outside the hook too), which is how the preview shows "RomM:
rename ROM 12 to Ico (USA).iso". `Referrer.rename_refused(library)`: an opt-in
Referrer that is off says why an Image in that Library must not be renamed
without it (`ReferrerRegistry.rename_refused` asks each one). The RomM
Referrer answers for a RomM Library: renamed under RomM's feet the file would
get a new ROM, so the swap switch must be on.

**Shared helpers.** `utils.path_utils.safe_file_name` is the one rule for names
made from data: `< > : " / \ | ? *` and C0 control characters become `_`,
leading and trailing spaces and trailing dots go, and a name that comes out
empty, `.`, `..` or over 255 UTF-8 bytes is refused (None).
`registry.extension_of(name)` is a name's extension as the tools know it, the
longest registered one (`game.nkit.iso` keeps `.nkit.iso`).
`subprocess_runner.run_detached_to_end` (promoted from the swap's `_mutate`)
runs a step that changes something to its end, never abandoned.
`LockManager.acquire_all` takes several locks or none, letting go of those
taken on any exit, `LockFailed` included.

**Rename to DAT name** (`dat_rename`, routes in `routes/dat_rename.py`). The
name comes from `DATStore.ranked_match_for_image` (3.3.5.2): cache only, never
hashing, looked up as listed and then along the real path. The new name is the
top match's `game_name` plus `registry.extension_of`, through
`safe_file_name`. Each preview and each run first reads every sheet's tracks
over the Volumes, each with the sheets that name it
(`image_sets.volume_sheet_owners_scoped`, the walk `volume_sheet_tracks` and
the duplicate delete use): a walk that doesn't finish skips every Image (fail
closed). A sheet whose tracks can't be told (an absolute `FILE` name, a sheet
that can't be read whole) doesn't fail the walk (#454): it holds back only
what it could name. `resolve_track` takes a relative name without `..`, so
that is its folder and below, through any link inside the Volumes;
`image_sets.sheet_reach` walks it, following linked folders, and records each
file by identity, so a hard link counts too. It walks junk names as well
(`walk_tree(skip_junk=False)`: `resolve_track` takes `__MACOSX/x` like any
other name). A file link whose real path is outside the Volumes reaches
nothing, since `resolve_track` refuses it. A folder link out of the Volumes
fails the whole walk instead, naming the sheet and the link: a link inside
that folder may lead back in, `resolve_track` checks only the final real
path, and the walk never goes outside the Volumes to find out. Those identities go in
`SheetOwners.unknown`, and an Image or a sheet's track among them is skipped
with the sheet that holds it. A folder or file that walk can't read still
fails the whole walk. `volume_sheet_owners` keeps the stricter rule (one such
sheet makes it None) for the duplicate delete. `plan_blocking` skips, with the reason: a path not confirmed inside
the Volumes, a symlink, a playlist, an Image with members that isn't a sheet
(`image_sets.image_members`), a track a sheet lists (the sheet above it by
`owning_sheet`, or any sheet in the Volumes by the walk, through a link too),
not Recognized, already named, a case-only difference, a new name that exists,
or a Library the registry refuses. `preview` also skips a second Image given a
path (a file's or the folder's) another selected one already takes.

A sheet's Image (#426) is planned whole. Its tracks are the sheet's own
(`sheet_track_list`): each a regular file, not a link, named by this sheet and
no other one (the owners map), and no member beyond them. Their names come
from the file names the DAT that named the sheet lists for its game
(`DATStore.game_files(dat_id, game_name)`, sheets left out). They are read
from each DAT's file list (`dat_files`, migration 0025, filled at import),
kept apart from the hash index, which keeps one row per hash and so loses a
track whose bytes another file shares (two silent tracks); a DAT imported
before 0025 has no list until it is imported again, and its hash index is
read meanwhile. A track pairs
with the DAT file of its size, and tracks of one size pair in the sheet's
order with those files in natural name order (`Track 2` before `Track 10`);
nothing is hashed. The count must match and every track must pair, or the
Image is skipped; so is one named only by Hasheous, which lists no tracks. The
DAT's name is taken whole unless its extension differs from the track's, which
is kept. The folder (`_folder_for`) is renamed only when it holds exactly the
Image's files, its name is the Image's file name without the extension, and it
is no Volume root, Library root or Platform folder; a new folder name that
exists skips the Image. A preview item carries `files` (every file, old and
new path), `sheet_rewrite` (each reference, old and new) and `folder`;
`renames_of(item)` turns it into `rename_image`'s map and folder, and the run
skips an Image whose `files` differ from the confirmed ones, and an Image of
several files confirmed without `files` (one confirmed path never renames
several files; only a single-file plan may come without them). One Image that can't be
checked, or whose rename stops on an unexpected error, is skipped or reported
not renamed on its own, never hiding the others. A `conflict` (equally ranked
DATs that disagree) skips the Image and records a `rename_conflict` Finding
(keyed by the Image's path, its cause the disagreeing matches);
`settle_conflicts` clears each one the ranking now resolves. Requests run it
through one shared `settle_conflicts_bounded` (after the ranking is saved or
reset, and whenever the Findings are listed): detached, bounded by
`SETTLE_SECONDS`, at most one in flight until its worker really ends. A settle
that fails or stalls leaves the Findings as they are, and one that ends late
only ever clears a row whose cause it read.

The run (`POST /api/files/dat-rename`, the rename confirmation header) is an
external job (`ConversionMode.DAT_RENAME`, no Ledger row), one at a time, like
the duplicate delete. A Cancel stops it before the next Image and ends it
cancelled, also when it came during the last one. It plans every Image again: one whose new name is not
the one confirmed is skipped. Each confirmed Image goes through
`rename_image`. The job's `result` lists every Image as `renamed`, `skipped`
or `not_renamed` with its reason and Referrer records, and one
`rename_finished` notification (a registered event) carries the counts and
what wasn't renamed. The dialog has the jobs store track the job
(`jobs.track`: read from the API until it ends, each read added to the list,
failed reads tried again, a 404 ending it), independent of the dialog. It was
added because a rename could end before the job stream subscribed to it; since
#456 the stream follows every job from its registration, so the tracking is
now only a fallback (a stream that is reconnecting) and harmless. The
single-file `POST /api/files/rename` is unchanged.

### 3.3.24 The patch engine and the Patch action (`services/patch_engine.py`, `services/patch_bench.py`, #428)

**The engine** (`patch_engine.apply_patch(source, patch, *, max_target)`) is
pure: bytes in, a `PatchResult(target, kind, checked)` out, no I/O, so it is
tested on hand-built patches. The format comes from the patch's header
(`patch_kind`), never its name. IPS: records (3-byte offset, 2-byte size,
data), RLE records (size 0, a 2-byte run, one byte), a record past the end
grows the ROM with zeros, and three bytes after `EOF` are the truncation
length. `EOF` is also the offset `0x454F46`; only the end marker can be
followed by nothing or by exactly three bytes, which tells them apart. BPS:
varint sizes and metadata, then SourceRead, TargetRead, SourceCopy and
TargetCopy (TargetCopy may read what it just wrote: a run), then the source,
target and patch CRC32s. The patch CRC is checked first (`PatchFormatError`,
"damaged"), the source size and CRC before applying (`SourceMismatch`), the
target size and CRC after (`TargetMismatch`). Every action is bounds-checked
against the source, the patch and the declared target. `max_target` bounds
the output before it is allocated, so a header claiming a huge target or a
record far past the end raises `PatchTooLarge`. `checked` is True for BPS
only. `apply_patch_in_place(rom, patch, *, max_target)` is the form the
action uses (#455): *rom* is a `bytearray` the caller gives up. IPS patches
it in place (growing or truncating it) and returns it as the target; BPS
builds one target `bytearray`, copying view to view (`memoryview` slices, so
a SourceRead of the whole ROM makes no ROM-sized slice), and a long
overlapping TargetCopy doubles its run in place instead of building the run
in a buffer. Nothing ROM-sized is copied on the way out.

**The action** (`patch_bench.start`, `POST /api/patch`) is an external job,
`ConversionMode.PATCH`, declared in `EXTERNAL_JOB_MODES` with a Ledger row
(`ExternalMode()`): it takes no conversion slot. The route does only the
quick checks: one Image and exactly one patch, the patch named `.ips`/`.bps`,
a disc Image refused by `registry.disc_extensions()` (on the file, or the
named archive member), a RAR refused, the Image and a picked patch inside a
Volume (`volume_containment(...) is True` under `bounded_path_check`;
"couldn't tell" refuses), and the 7z packer present. The job's `file_path` is
set to the Image, so the Ledger's Source is the Image (no in-use check reads
an external job's path). Then, every blocking step through
`run_detached_within_stall_bound` (the conversion stall bound):

1. The patch is read with `read_beneath` from its resolved path's
   `lock_boundary_root`, after containment is checked again on the resolved
   path, so no symlink anywhere below the root is followed. Over
   `CHD_PATCH_MAX_PATCH_SIZE` is refused. Its SHA1 goes in `extra_inputs`.
2. The ROM: a loose one the same way, under `CHD_PATCH_MAX_ROM_SIZE`. An
   archive's whole listing goes through `enforce_archive_limits` first; a
   symlink or encrypted member refuses it; with no member named it must hold
   exactly one non-junk file; the member must have a recorded size within the
   ROM limit; then `ArchiveService.extract_file` unpacks it.
3. The engine runs on its own detached thread, `apply_patch_in_place` on
   the ROM's buffer, and the same call writes the patched ROM as
   `<stem> (Patched)<ext>` in a work directory under `CHD_TEMP_DIR` and
   returns only its path, size and SHA1 (`_Patched`). For IPS, which proves
   nothing about the ROM, the ROM's SHA1 is taken before the patch changes
   it, and `_recognized` asks `dat_store.lookup_sha1` and then the cached
   match for the Image; neither a hit nor a readable store means
   `job.warning`, not a refusal.
4. The patched ROM is packed by `romz_service.convert(..., mode="romz_7z",
   dictionary=pack_dictionary(size), threads=PACK_THREADS)` into a hidden
   staged `.patch-*.7z` beside the Image (the same filesystem as the target).
   The staged archive is read back with py7zr, unpacked straight into a
   SHA1 (a `py7zr.io` writer factory, nothing kept), and must hold exactly
   that one member with the patched ROM's size and SHA1
   (`check_passed = "intact"`). `place_without_clobbering` then moves it to
   `<stem> (Patched).7z`, and on `FileExistsError` to `(2)`, `(3)`, … up to
   999. The staged file and the work directory are always swept. Placing is
   the one irreversible step, so it runs through
   `subprocess_runner.run_detached_to_end`, not the stall-bounded seam: a
   cancel (or a stuck mount) never abandons it, because the link could land
   after the job said "nothing was written". The job waits for it, then
   records the archive in `output_path` and the Ledger, noting a cancel that
   came too late. A cancel before placement still writes nothing.

**Memory (#455).** `CHD_PATCH_MAX_ROM_SIZE` allows a 512 MiB ROM, and the
first version held the source and the patched bytes while romz's
`-mx=9 -md=256m -mfb=273 -mmt=on` ran, which the OOM killer stopped on a
400 MiB ROM in a container with 4 GiB free. Now:

- The ROM is read once into one `bytearray`: `read_beneath_mutable` (a loose
  ROM) and `read_fd_capped` (an unpacked member), both in
  `utils/path_utils.py`, read straight into a buffer sized by `fstat` and on
  to the real end, under the cap. Patching costs no copy for IPS and one
  target for BPS. The job holds no ROM-sized buffer once the patched ROM is
  written, so none while 7z runs.
- 7z keeps romz's Max profile (LZMA2, `-mx=9 -mfb=273`) with two switches
  replaced through `romz_service.convert`'s `dictionary` and `threads`
  (`romz._compress_flags`): the dictionary is the smallest power of two that
  holds the ROM, from 1 MiB to `PACK_MAX_DICTIONARY` (64 MiB), and `-mmt=2`
  runs one LZMA2 block encoder with a two-thread match finder, whatever the
  core count. 7-Zip's table puts BT4 at about 11.5 times the dictionary, so
  7z needs about 740 MiB at most (`pack_memory_estimate`). 7z shrinks its
  dictionary to the input anyway, so a ROM up to 64 MiB packs exactly as
  before; a bigger one packs with a 64 MiB window instead of up to 256 MiB,
  a little larger, and any 7z reader opens it with 64 MiB.
- Nothing is refused for size up front: with the bound, a ROM at the cap
  needs about one ROM (two for BPS) in the app and 740 MiB in 7z, at
  different times.

Uploads (`POST /api/patch/upload`, `patch_bench.save_upload`) are read by
`services/upload_stream.file_upload`, not FastAPI's `UploadFile`, which
spools the whole body to temp disk before the route runs. It feeds
`request.stream()` to python-multipart's streaming `MultipartParser` and hands
back the `file` part's name and its bytes as they arrive. The body is capped
at the limit plus `MULTIPART_OVERHEAD` (64 KiB of framing): a
`Content-Length` over that is refused (413) before a byte is read, and a body
without one (chunked) is cut off the moment it passes it. Nothing is spooled.
`save_upload` writes the bytes into a hidden temp file in
`<data dir>/patches/` and stops at the exact
`CHD_PATCH_MAX_PATCH_SIZE` (413, nothing kept). Any other upload route that
must hold a size limit can use `file_upload` the same way, or
`file_uploads` for a form of many files (one async iterator of them, the cap
on the whole body; `POST /dat/import-batch` uses it). The header must say IPS or
BPS. The file is kept as `<16 hex>-<safe name>`, the `upload_id`, so the
Ledger's path stays valid; `POST /api/patch` accepts an `upload_id` only in
that exact shape (no separator can appear) and reads it with `read_beneath`
from the uploads folder.

The frontend offers **Patch…** in `RowActionsMenu` when
`util/patchDialog.canPatch` allows it (a file, a `.zip`/`.7z`, or a member of
one, never a disc Image by `registry.discImages`), and `PatchModal` uploads or
names the patch and starts the job.

### 3.3.25 Inboxes (`services/inbox/`, #431, #433)

An Inbox is a folder that feeds one Library: what settles in it is placed in the
Library and the original moved aside. Nothing here deletes anything.

| Module | Holds |
| --- | --- |
| `inbox/__init__.py` | the `inboxes` rows (migration 0027), cached like the folder Libraries: `register`, `update`, `remove`, `note_processed`, `forget_library`, `describe` |
| `inbox/intake.py` | one poll of an Inbox (`poll`, `poll_all`), its in-memory state, the copy job; for `run-rules`, `finish_all` and the settle memory (#433) |
| `inbox/report.py` | what runs after a pass (#433): each Library's `after_intake` hook (RomM's Platform scan) and the `inbox_pass` summary notification |

**Saving** (`inbox.register`, blocking, run through `libraries.run_admitted`).
Fails closed: the folder must be absolute and `volume_containment(...) is True`
(None is 503), a real folder, not inside any Library's root nor holding one
(`inbox.overlap_problem`, resolved both: its own Library would feed intake its
own output, another's would have its Images moved to `processed/`), and not
equal to, inside or around another Inbox. The overlap checks and the insert run
under `libraries.registration_lock`, the lock a folder Library's registration,
RomM's root save and a folder Library's removal take (removal takes it before
anything is cleaned up, its Rules' run state included, waiting at most
`REGISTER_WAIT_SECONDS` and answering a retryable 409 with the Library untouched
when it isn't free, CodeRabbit 4606147989, and holds it across its Inboxes'
removal, the delete and the cache; the delete's worker lets go of it once it
has started, else the request does, decided under a lock, so a removal
cancelled while waiting for a worker leaves no lock held, CodeRabbit
4606887332), so an Inbox and a Library saved
or removed at once can't interleave (Codex 4603071815); `inboxes.folder` unique is the backstop. The other way
round, `libraries.register` and `check_romm_root` refuse a Library root that
is, holds or sits inside an Inbox (`inbox.holding`), and every poll checks its
Library's root again before it lists anything (a RomM root saved elsewhere since). Removing an Inbox (or its folder Library, from `libraries._remove`)
drops the row, its intake state (revoked: a poll still running on it starts
no copy or conversion, CodeRabbit 4606147955) and its Findings. The revoke is
set under the state's own `threading.Lock` (removal runs on a worker thread):
a copy's check and its scheduling are one step under it, and a conversion
checks it again under it once `create_batch_jobs` returns, cancelling the jobs
it just made while they are still queued, the Source untouched, if the Inbox
was removed meanwhile (CodeRabbit 4606443430)
(`findings.forget_under(library_id, "inbox:<id>", INBOX_KINDS)`); no file. A
settle-time change writes the row and publishes it to the cache under one
mutation lock, so two saves publish in commit order.

**The poll.** `rules.sweep.run_forever(libraries, after_tick=...)` calls
`intake.poll_all()` once per tick, after the sweeps, so there is no loop of its
own. `poll(inbox, now=...)` takes the clock as an argument, so a test runs one
tick with a fake clock and real files. Each poll:

1. Settles the conversions it queued (`job_manager.get_job_for_lookup`):
   completed moves the original; failed or cancelled holds it with a Finding.
2. Lists the Inbox on a stall-bounded detached thread
   (`run_detached_within_stall_bound(report_progress=True)`): regular files
   with an extension a tool reads or writes, or a numbered split part
   (`registry.split_owner`, as the folder Library lists `Game.iso.0`), never the
   top-level `processed/`, a dot-entry (a staging folder), a junk entry or a
   link (a linked folder is reported as not listed), grouped into Images
   by `image_sets.group_images` and fingerprinted by `image_fingerprint`
   (every file's size and mtime). The folder must still be inside a Volume
   and still clear of its Library's root. Progress is reported between
   Images all through the grouping, the fingerprinting and the ownership
   checks below as well as the walk, so the stall bound measures stalled
   storage, never how long a big pass takes (Codex 4607108046).
   An archive Image (`.zip`, `.7z`, `.rar`) is checked before it is taken
   (`ArchiveService.check_inbox_archive`, Codex 4607318277), reading its
   listing only: anyone who can write the Inbox can drop a compression bomb,
   and `7z t` would decompress all of it in a conversion slot. Its declared
   uncompressed size, per member and in all, may be at most
   `CHD_ARCHIVE_MAX_MEMBER_SIZE` / `CHD_ARCHIVE_MAX_TOTAL_SIZE` where set, else
   `CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE` (32 GiB); it may expand at most
   `CHD_ARCHIVE_INBOX_MAX_RATIO` (200:1) per member (its declared compressed
   size, `ArchiveMember.compressed`) and in all (against the archive's size);
   a member with no declared size, a link or an absolute or `..` path is
   refused too. One over any of them is held with an `inbox_failed` Finding
   naming the limit, never unpacked (#432, below). romz's Verify sizes its bound by
   the members' declared sizes (`capture_verify(size=...)`, through
   `resolve_verify_timeout` and `compute_size_scaled_timeout`), never the
   archive's own size, and keeps its stall bound.
3. Every loose track-type file in the folder of a track sheet whose owner is
   unknown, or in any folder below it (a sheet may name `tracks/Track.bin`,
   Codex 4607108018), is held with a Finding naming the sheet
   (`_behind_unreadable_sheets`). Unknown means what `owning_sheet` treats as
   unknown: the sheet can't be read whole (unreadable, over the size cap), or
   its track list doesn't fully resolve beneath the Inbox
   (`sheet_track_list(within=[the Inbox])` is None: an absolute, escaping or
   missing reference, anything `image_members` drops; Codex 4607280415). Such
   a sheet owns nobody, so its tracks would otherwise be taken as Images of
   their own.
   Images that share a file (two sheets naming one track, `_sharing`) are
   held, each with a Finding naming the others, once settled: taking one in
   would move a file the other still needs. None of them is taken. Each of
   these ownership holds keeps what caused it (`_ownership_key`: the sheets'
   paths and fingerprints, in `_State.held_by`), recomputed every poll; once
   it changes (the other sheet removed, the unreadable sheet removed or
   fixed), the hold lifts, its stale Finding goes and the Image is looked at
   again as settled, without waiting for it to change (Codex 4607108025).
4. Settles each Image: first seen (or changed) records `(fingerprint, now)`;
   it is taken once a later poll at least `settle_seconds` after that sees the
   same fingerprint. So it always takes two polls, even with a settle time of 0.
5. Takes it (`_take`): `detect_platform`; not decided is held with
   `inbox_unsorted` or `inbox_conflict`. A track sheet is checked with
   `delete_plan.sheet_reference_problems(within=[the Inbox])` first: a reference
   the confined resolver refuses (`../tracks/Game.bin`), or a sheet that can't
   be read, is dropped by `image_members`, so the Image would look trackless;
   it is held instead. Its tracks must sit beside it, so the Image moves as one. `_plan` reads the Library's conversion Rules
   (`sweep.get_rules`) and finds the Platform's Rule (`canonical_platform_id`,
   or the Library's `platforms()` by slug for RomM's numeric ids, passed on as
   `known`), then `Library.placement(platform)` (§3.3.11). Each file's
   identity (device, inode, size, mtime) is read then through the Inbox's own
   folders (`open_dir_beneath`, no link followed) and kept with the flight.
   `PlacementUnknown` holds it; `LibraryUnreadable` or a timeout retries next
   poll.

**Converting.** An enabled Rule whose mode is for the Platform and takes the
file (`sweep._accepts_source`, on a bounded worker thread: it can read a
header), with its tool ready, queues through
`job_manager.create_batch_jobs`, the call a Rule sweep makes: the placement
folder (made if missing) as `output_dir`, the destination resolved with the
`skip` policy (`resolve_destination`: an output on disk, `SKIP_EXISTING`,
holds it; a name another job only holds, `SKIP_LOCKED` from
`output_conflicts.output_status`, is a retry with no Finding, so the next poll
queues it once the lock is gone, Codex 4606794174), the Rule's compression
(`sweep._compression_arg`) and `split`, `verify_after` wherever
`registry.mode_supports_verify` says the mode can check its output (the Intact
check; a `jwud_decompress` WUD can't be), and never `delete_on_verify`. It also passes `source_identities`: the Source's `file_snapshot` and the Inbox
folder it must stay under (`path_utils.source_identity`). That is a shared job
mechanism, not an Inbox branch: any caller may queue a conversion with it, and
`_process_job`, once it holds the job's Source lock and before the tool reads
anything, re-reads the Source beneath that folder with no link followed and
refuses the job (`source_identity_problem`: outside the Volumes, reached through
a link, gone, or another file) with the reason. A folder swapped for a link
while the job waits converts nothing from outside the Inbox.

**Every conversion carries one (#449).** `create_job` and `create_jobs_atomic`,
which every queueing path goes through (a Rule sweep, a hand-started job, the
Auto route, `run-rules`), give each spec that has none the identity of its
Source as it is then (`_capture_source_identities` over
`path_utils.queued_source_identity`, bounded, off the event loop): the path
resolved, read beneath its Volume root with no link followed the way
`AnchoredFolders` opens a folder, plus `path` (the resolved path checked; an
archive's own file for `archive::member`), `in_place`, and `companions`:
every file a job of its mode opens alongside it, from the one shared
enumeration `path_utils.source_companion_paths(path, mode)` (the sheet's
tracks in sheet order, then the mode's own tool's `source_companions`, the
same tool-scoped set `source_companions_are_safe` checks, so a `createhd` on
`game_part1.wud` has no part 2): an ordered list of the reference the job
opens, where it resolves, the Volume root it was read beneath (a sheet linked
across Volumes names tracks in another one) and its snapshot (CodeRabbit
4614956755; codex 4615261287, 4615553372, 4615553385). The path is resolved through the shared gate
(`_confined_real_path`), so a spelling outside every Volume is refused before
the filesystem is asked. One that can't be captured carries
`{"error": reason}`. Captures go into the attempt's own copies of the specs,
never the caller's, so a submission refused for backpressure captures again
when retried. The capture is bounded by progress
(`run_detached_within_stall_bound`), not by the batch's size: a large healthy
batch is never cut off, and when a Source stops answering the ones already
captured keep their identities (codex 4614857109). When the job runs, a file
job with no identity ("queued without its Source's identity") or with an
`error` is refused like a changed one, so a run-time containment check is
never skipped. An `in_place` identity is not bound (a copy of every Library
Image into the work area is not a cost a Library job should pay):
`path_utils.queued_source_problem` requires the path the job was submitted as
to still resolve to `path`, checks `path` as above, and only then reads the
sheet for its companions, which must be the captured references in the same
order, each still resolving where it did (two track links that swapped
targets read the disc in another order) and unchanged.
`_input_paths` locks only the Source of a job with no usable identity, and the
Source plus its captured companions for an `in_place` one, so nothing is read
inside the unbounded input-lock wait and a sheet is never parsed before its
check (CodeRabbit 4614956778, 4615215842, 4615955374). A Source a stalled
batch capture never reached is marked `unanswered`; its refusal says nothing
about the Image, so a Rule's failed-hold neither holds nor releases it and the
next sweep queues it again (`sweep._never_checked`, CodeRabbit 4615955365).
The job's own check marks its identity `unanswered` the same way when it
times out or when a Volume root it depends on doesn't answer: every
root-probing lookup in the identity's capture and checks (the containment
gate, each Volume-root choice, the sheet's track list) goes through one
helper, `path_utils._answered`, which raises `TimeoutError` when the probe's
unknown count rose during the call, so "couldn't tell" is never "outside the
Volumes" or "other companions". A `TimeoutError` (ETIMEDOUT) from any open,
stat or folder walk in those paths is re-raised past their `except OSError`
refusals, and a sheet read that times out or fails with EIO is counted as
unknown, so it ends unanswered too. Every existence check a companion
enumeration makes (a split set's parts, a sheet's tracks, its spelling
check) is `path_utils.path_present`, never `os.path.isfile`/`exists`, which
swallow every error: a stat that doesn't answer is counted as unknown, so the
tool's companion list is "couldn't tell" through `_answered`, never shorter
(codex 4620404219); capture marks such a Source unanswered too, and a Cancel while it waits ends the job CANCELLED, never refused.
The capture runs before the destination keys are resolved, so its wait never
sits between those keys and their comparison under the lock. The active-job
inventory other sweeps read (`_candidate_paths`) names the same files as the
input locks, so a queued sheet isn't read there either. The job reads the submitted path, not `path` itself (codex
4614857083): a sheet submitted through a link names its tracks beside the
link, which is where the job locked them. Its output is staged only where its
Library asks. **What this does not close:** the check runs once, under the
Source lock, before the tool starts. A parent folder swapped for a link after
the check and before the tool opens the file still redirects the read; only a
bound Source (#431) is immune to that, and binding every Library Image is the
cost declined above. A folder Source
(a DIRECTORY mode, `folder_to_iso`) has no file identity; the folder checks
that already run under its locks cover it. Both checks, and the bind below, run inside the job's full
finalization region (CodeRabbit 4606147979): a cancel while either waits ends
the job CANCELLED and lets go of its output locks and its cross-process slot,
and a refusal raises `SourceRefused`, ending it FAILED ("Not converted: ...")
with nothing written or removed. A job that passes is then bound before anything else reads its Source
(`path_utils.bind_source`, Codex 4605281757). **Where binding lives** (Codex
4606562350): in a folder of the job's own in the app's work area
(`disk.create_scratch_dir("bind_<job>_")`, under `CHD_TEMP_DIR` or the data
folder's `temp/`, mode 0700), never in the Inbox, so no Inbox writer has any
right on its parent chain and none can rename it or swap it for a link. The
Source and each member it carries (`identity["members"]`, a sheet's tracks) are
put there through descriptors opened beneath the Inbox root with no link
followed, each `fstat`ed against the identity validated: a hard link where the
work area is on the Inbox's filesystem, else (`EXDEV`, or `ENOTSUP`/`EPERM` on a
NAS or under protected hard links) a copy read through a descriptor checked
before and after. The bind runs under `run_detached_within_stall_bound`
(`_BIND_STALL_SECONDS`, the conversion's own stall limit by default) with the
job's cancel event (Codex 4606539525): each copied chunk reports progress, so a
slow copy that keeps moving finishes, only a real stall fails the job, and a
Cancel stops it at its next chunk and ends the job CANCELLED (`ReadCancelled`
becomes the job's own cancel). A bind given up on or cancelled removes its
folder; one that answers after its awaiter left is removed too. `bind_source`
returns a `BoundSource` (`path`, `read_path`, `changed()`, `close()`) that keeps
descriptors from bind time: the folder's and its parent's, which `close()`
removes it through (the folder taken from its parent only while that name is
still the same inode), and one per bound file with its size, mtime and ctime
then, taken only once every name is bound (linking a second name of one
inode, two track names hard-linked together, moves its ctime; CodeRabbit
4606887346). A hard link shares its inode with the Inbox's file, so a resumed download
writing it in place would reach the converter: before any output is published
(both placement points in `_process_job`), `_require_bound_unchanged` `fstat`s
every bound file through its descriptor, and one that changed raises
`SourceChanged` ("the source changed while it was being converted"), so the job
fails, its staged output is discarded, nothing is published and the Source is
left alone (Codex 4606539513). For that, every job that reads a bound Source
stages its output (`_needs_stage`), whichever Library holds it. The
`BoundSource` is runtime-only: an archived snapshot of a finished job
(`ConversionJob.archived_copy`) leaves out every private in
`models.RUNTIME_ONLY_PRIVATES`, without touching the live job (Codex
4606539536). The metadata probes keep their fixed bound. Sizing,
detection, the companion checks and `run_convert` read `job._read_path`, never
the Inbox name. The bound folder is the job's trusted root (`_trusted_roots`,
the same `within` an archive member's extraction dir gets): the companion check,
the post-conversion checks and the Round-trip take its sheet's tracks there,
outside every Volume, and still refuse one that leaves it (Codex 4606794168).
`_cleanup_temp_dir` removes the folder on every ending, through
`start_detached_eventually` (a thread of its own when every probe slot is
taken, retried when no thread can start), so it is never skipped (CodeRabbit
4606887319). The
move to `processed/` still acts on the Inbox names, with its own inode check. So the job is a conversion in every sense the
queue has: it takes a `max_concurrent` slot, gets its Ledger row and its
Cancel. A Rule that is disabled, or whose mode doesn't take the file (it is
already in the target format), copies instead; one whose mode isn't for the
Platform, or whose tool isn't ready, holds the file with a Finding. A tool
that isn't ready gives its own reason, `not_ready_message(tool)` read after
`tool_is_ready` (§3.4, #484), followed by ", so the Rule can't run".

**Copying** (`ConversionMode.INBOX_COPY`, an `ExternalMode(in_ledger=False,
started_by_hand=False, takes_slot=True)`: it converts nothing). One background
task per Image, each an external job. Its file work runs in one of the same
`max_concurrent` slots conversions take: `job_manager.hold_slot(job_id,
cancel_event)` acquires the dispatcher's own semaphore. With the dispatcher
running a copy joins the conversions' FIFO (Codex 4606061259): it takes the
next ticket when it asks, its id goes on the dispatcher's queue under that ticket, and the
dispatcher hands it the local slot when the ticket comes up (`_hand_slot`), so
nothing holds the local slot while waiting behind someone else and one slot
can't deadlock; a cancel drops the ticket and gives back a slot already handed
over (only for a mode that
declares `takes_slot`), racing the job's cancel, and `release_slot` gives it
back on every outcome. A `hold_slot` whose own task is cancelled while it
waits cancels its pending acquisition, and gives back a slot that acquisition
took anyway, so a cancelled wait never leaks one. The job still never enters the queue, and keeps its
external-job presentation. The #377 concurrency invariant now counts by
`job_manager.slots_in_use()`: every PROCESSING conversion, plus every external
job holding a slot (a check run or scan still holds none). The copy goes into a fresh
`staging.open_slot(folder, make_folder=True)` (§3.3.21), each file opened
through the Inbox folder's descriptor with `O_NOFOLLOW` and refused unless its
`fstat` identity is the one taken, then read back and compared
(`stream_digest.new_hasher`). Every source's identity is checked again once
the last byte is read, and the whole Image (identities and fingerprint,
`_still_taken`) again just before it is placed, and each open source's own
`fstat` after its last read: a source rewritten in place
mid-copy reads back as written yet is neither version, so the staged copy is
discarded and the source left to settle again. When `registry.check_tool(staged,
platform)` (on a bounded worker thread: it can open the file) names a tool,
`check.verify_file` (the check Rule's Verify on the `verify` lane) must say
Intact; only then `slot.place(names)`, which never replaces (a taken name holds
the file). The check and the placement run under one asyncio lock
(`_checking()`), and `_storage_stop` is read and set under it: a copy staged
beside one whose check stopped (`max_concurrent` > 1) neither checks nor places.
The flag is thread-safe (`_stopped()`, `_set_stop()`): a copy reads it after
taking its slot, and its staging reads it again on its worker thread right
before it opens each file (`_may_open`), so a copy that reaches its first open
after another's check stopped opens nothing (`deferred`). The lock is never held across an
open, which can stall on a mount that stopped answering, so one open already in
flight when the stop lands may complete: the stop (and a stop generation that
moves with every stop) is checked again the moment it returns (`_opened`), and
the file is closed without a byte read. The read-back of the staged copy is
guarded the same way, before and after its open. A cancel is checked
again once the check-and-place lock is held and right before placing, so a copy
cancelled while it waited for the lock places nothing even when no tool checks
its file. Before it stages anything, a copy takes the shared output locks
(`LockManager.acquire_all`) on every name it will place, as a conversion does on
its outputs, and holds them through the placement: a name another job (a
conversion here or in the CLI) is writing leaves the Image for the next poll,
untouched and with no Finding. The acquisition runs through
`run_detached_undoable` (`_LOCK_SECONDS`): one that answers after its wait gave
up gives back the locks it took, never leaving them held.
A stop is honoured before the verdict, so a cancelled read that left a reader on
the storage stops the pass too. The staged slot is handed from its worker to
the awaiter by `_SlotHandoff`, under one lock: once the awaiter has taken it,
it owns it; a slot delivered after the awaiter gave up (a cancel after the last
progress report) is closed by whichever side comes second, never left open.
Placing is a rename that can outlast any bound, so it runs through
`run_detached_mutation` (`_PLACE_SECONDS`): past the bound, intake keeps waiting
for the worker to report that it finished and reads `slot.placed`, rather than
failing a copy that may still land. A verifier that returns #306's `stop` (a
reader still on the storage) places nothing, and no copy opens another file
until the next pass (`poll_all` clears `_storage_stop`); its Image settles
again. `_stage_copy` reports progress once more before it returns, so a result
that arrives after its awaiter gave up closes its own slot. The slot is always
closed, so nothing half-copied is left in sight.
A copy is an external job with no output path, so `libraries.note_job_finished`
can't see it: intake calls the Library's `note_paths_changed` with what it
placed before the job is announced finished, so its kept walk is dropped. A
conversion's output is already covered by `note_job_finished`.

**Moving the original** (`_move_to_processed`). Only under the lock
manager's exclusive locks on every original, the sheet and its tracks
(`_lock_all`, `LockManager.acquire_all`, the acquisition a copy's output locks
take too): while another job reads one (its input lock), nothing moves, no
Finding is written, and the Image stays in flight (`_Flight.archive`) for the
next poll to try again; the locks go on every outcome (Codex 4607108034). And
only while the Image is still the one taken: its whole fingerprint is compared first, and a source replaced
after its conversion stays in the Inbox to settle again (the Library holds the
old contents). The move is anchored, as the Image rename's is: the source
folder and `processed/<its folder in the Inbox>/` are opened beneath the Inbox
with no link followed (`open_dir_beneath`, then `mkdirat`/`openat` with
`O_NOFOLLOW`), every file's identity is checked through the source descriptor,
and each file moves by `place_without_clobbering` between the two descriptors,
so a folder swapped for a link after the conversion can't redirect it (Codex
4603069915). Each file moves by `path_utils.move_anchored`, the anchored move
the Image rename (§3.3.23) uses too, with its `file_snapshot`, `stat_at` and
`AnchoredFolders`: the entry that landed in `processed/` is checked again, and
a file swapped in under the checked name meanwhile is moved back (never over
another file) along with the rest, and the Image stays in the Inbox. A put-back
that fails is never swallowed: `move_anchored` logs it and raises
`MoveRollbackFailed` (an `OSError`), and so does intake's own rollback of the
files already moved, so the Image gets an `inbox_failed` Finding naming what
was left in `processed/`, never the quiet "changed, settle again". A name already there uses `processed/.../<stem> (2)/` and so on.
All or none: a failed move puts back the ones already moved, through the same
descriptors. The move runs like a copy's placement (`_owned`: past
`_MOVE_SECONDS` its worker's outcome is still waited for), so a move that lands
late is counted and reported, never lost behind a Finding. After a conversion
it is its own tracked flight (`_track`, CodeRabbit 4606147968): the poll that
saw the job complete starts it and goes on, the Image stays in flight (never
taken again) until the move ends, and its real outcome is reconciled then. A
copy's flight likewise lasts until its own move has ended. Then
`processed_count` is incremented in one `UPDATE ... SET
processed_count = processed_count + 1` (a copy and a conversion can finish at
once; the cached total only grows to what the database answers) and the file's
Findings cleared.

**A cancel's reason** (#433). `ConversionJob.cancel_reason` records why a
cancel was taken: `job_manager.cancel_job(job_id, reason=...)` and
`cancel_all_jobs(reason=...)` default to `CANCEL_REQUESTED` (the UI and API
routes pass nothing), and every stop path passes `CANCEL_SHUTDOWN`: the CLI's
signal handler and its stop-time cancel-alls, and a check Rule run's stop
(`rules.check.drain`). The first reason a job gets is kept. Intake reads the
job, never who cancelled: a conversion or copy that ends cancelled with
`CANCEL_SHUTDOWN` isn't the file's fault, so it gets no Finding and isn't
held; its seen and held entries are dropped (`intake._release`), and the next
poll (the next `run-rules`, with the settle memory) sees it as new and takes
it once it has settled again. Any other cancel keeps the hold below.

**Holding.** A file that gets a Finding (`inbox_unsorted`, `inbox_conflict`,
`inbox_failed`, in `findings.INBOX_KINDS`, under the Inbox's Library, keyed
`inbox:<id>/<path in the Inbox>`) is remembered with its fingerprint and not
taken again until it changes. Every poll reconciles the Inbox's stored
Findings against its listing (`findings.stored_of`), not only the held paths in
memory, so a Finding for a file removed across a restart is cleared too; one
under a folder the walk couldn't list (or a link it won't walk) is kept. So a second poll over the same files changes nothing. The
state (seen, held, in flight) lives in memory: after a server restart every
file settles again, and a held one gets its Finding again. `run-rules` keeps
the seen and held state between its runs (#433, below).

**Status and API.** `GET/POST /api/libraries/{id}/inboxes`, `PATCH/DELETE
.../inboxes/{inbox_id}`. `inbox.describe` adds `intake.status`: last poll,
waiting, in progress, held, and the error a poll hit (the folder gone or
outside a Volume). `LibraryInboxes.svelte` in the Rules view lists and adds
them.

**Archives (#432).** An archive the walk lists (`.zip`, `.7z`: extensions a
tool reads; `archive_service.is_archive` decides) is unpacked before its Platform is worked out (`_take_archive`, first
thing in `_take`), unless it is the Image itself. Which Platforms keep their
archives whole is platform data, not an Inbox branch:
`platform_detect.archive_is_image(slug)` reads `_ARCHIVE_IMAGES` beside the
Platform aliases (today `arcade`, so its aliases `mame`, `fbneo`, `naomi`, ...
count too; a user's folder mapping to Arcade does as well). When the archive's
folder evidence (`Detection.place_platform`: a Platform folder or a mapping)
names such a Platform, the archive is taken whole: its identity is read first,
then the gate runs on that very file (`_gate_opened`: opened beneath the Inbox
with `O_NOFOLLOW`, its `fstat` compared with the identity, then
`ArchiveService.check_inbox_source` on the open file), and the take goes on only
while the archive is still that file; it then goes down the ordinary path
(detected, Recognized, copied or converted), never unpacked. A Platform the
folder can't tell is unpacked: its Images say.

Otherwise it is unpacked as an `inbox_unpack` job (`ConversionMode.INBOX_UNPACK`,
an `ExternalMode(in_ledger=False, started_by_hand=False, takes_slot=True)` like
the copy) on a stall-bounded worker (`run_detached_within_stall_bound`, its
progress reported per chunk written). **A private copy** (Codex 4608007766,
4608936764): the archive's identity is read when it is taken; the job opens it
through the Inbox folder's descriptor with `O_NOFOLLOW`, refuses it unless its
`fstat` is that identity, and copies it into a hidden folder of its unpack slot
(`.archive/`, which the walk never lists) through `path_utils.copy_checked`, the
copy a bound Source falls back to: bounded by its progress and cancellable, its
source's identity, ctime included (`file_snapshot(ctime=True)`), checked before
the first read and after the last. A rewrite in place that keeps the inode and
size and puts the mtime back can't put the ctime back. The gate and the
extraction then read only that copy (`ArchiveService.check_inbox_source`, built
on `archive_members.read_members_of`: the listing and the size read from the
descriptor), so nothing done to the original afterwards reaches them. The copy
counts toward the slot's room and goes with the slot. ctime is compared only
there: a bound Source's hard link and the move to `processed/` (a rename) both
move it, so the identities that must survive those leave it out.

An unpack that finds no room (the cap on what is unpacked at once, or the work area's free space) is not a failure: `JobManager.finish_external_job_waiting` ends the `inbox_unpack` job COMPLETED with `job.warning` set and `job.result = {"waiting": True}`, which the job list reads; `run-rules` reports the wait from the Inbox pass's `waits`, not from the job (#470, below). Every external job (`is_external_mode(job.mode)`, whose `file_path` is the internal `/__external_jobs__/<id>`) is named on its `run-rules` line by `job.filename`, whatever its result (#463). Intake writes no Finding and tries the archive again next poll (#458). The Findings kinds an Inbox writes are `findings.INBOX_KINDS`: unsorted, Platform conflict, DAT name conflict (`inbox_dat_conflict`, #457) and failed.

**Extraction** is `ArchiveService.extract_members` (shared, in
`services/archive.py`), the one extraction path that never hands a library a
path name (Codex 4608030847). It checks every member it is about to write again,
whatever the gate read: a link (or, in a 7z, an entry that is neither a file nor
a folder), a name the extract path refuses (`_validate_member`: a backslash, an
absolute or `..` path, an empty segment, a drive letter) or two members landing
on one path refuses the whole archive (`ArchiveSafetyRefused`, held with an
`inbox_failed` Finding naming it) before anything is written. Each member's
bytes come through the library's read API (a zip's or rar's `open`, py7zr's
writer factory, `_BudgetedWriters`) and go into a file created beneath the held
destination folder by `_create_member`: each folder on the way made with
`mkdir(dir_fd=...)` and opened `O_NOFOLLOW`, the file opened
`O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW` relative to it. The bytes are counted
as they are written (`_UnpackBudget`), never the header trusted: a member that
yields more than it declares, or more than the Inbox's member cap, or a total
past its total cap (`inbox_limits`), or past the room the slot reserved for its
declared contents (`reserved_total`, exact: 0 allows nothing, CodeRabbit
4608968265), or more than `CHD_ARCHIVE_INBOX_MAX_RATIO` times its compressed
size, or a total past that many times the archive's own size (Codex 4608936764),
is refused before the chunk that would cross the line is written. Junk is skipped; an empty 7z member, which py7zr
hands no writer, is made empty.

**The unpack slot** (`_UnpackSlot`) is a private folder in the app's own work
area (`disk.create_scratch_dir("inbox_unpack_")`, under `CHD_TEMP_DIR` or the
data folder's `temp/`, the tree a bound Source uses), on which no Inbox writer
has any right. It is held open; extraction writes beneath its descriptor, and a
slot whose path no longer names the folder held (`intact()`) refuses the
archive. Closing removes it, or, when its path was swapped, empties only the
folder held, through its descriptor. Its real path is registered
(`_slot_roots`) as the one place outside an Inbox intake reads an Image under:
`_folder_beneath`, and so every identity read, copy and comparison, opens an
unpacked Image beneath its slot (`_root_of`). An archive inside an unpacked one
is held, not unpacked again; one holding no Image is held too. A walk of the
slot that couldn't list a folder is no inventory (CodeRabbit 4608968267): the
archive is held with a Finding naming the folder, until it changes. A crash
leaves the slot in the work area; nothing sweeps it yet.

From then on the slot (`_State.unpacked`, an `_Unpacked` per archive: its
fingerprint, slot, identity, the Images found in it and those placed) is walked
with the Inbox by its recorded path (`_list(extra=...)`, the walk shared as
`_walk`), and each Image in it settles and is taken like a loose file. Its
Platform is read from its own header and from the folders it would sit in had
the archive been unpacked beside it (`platform_detect.detect_platform_as(path,
placed_at, within=...)`: the archive's folder in the Inbox, then the folders
inside the archive; mappings apply). Its header probes resolve a sheet's tracks
within its slot too (Codex 4608919681): `ToolPlugin.header_platform(path,
within=...)`, an optional trusted root every tool takes and chdman passes on to
`disc_id.disc_platform`, `extract_from_source` and `sega_sector0_platform`, so an
unpacked PS1 cue is read from its own track. A probe is asked with no `within`,
exactly as before, everywhere else. Its sheet's tracks resolve within its slot (Codex
4608417860): `image_members`, `image_fingerprint` and `group_images` (through
`image_sets.image_members(within=...)` and `delete_plan.sheet_track_paths(within=...)`)
are called with the slot as the one trusted root, the value its conversion job
is created trusting (`_slot_within`, `_members`, `_fingerprint`), so a cue and its
bin are one Image whose identity lists the track. A conversion
of it keeps every #431/#443 protection: it is queued with `source_identities`
rooted at its slot, and with that slot as the job's one trusted root
(`create_batch_jobs(trusted_roots=...)`, set on the job when it is created as
`ConversionJob._trusted_root`). `source_identity_problem(path, identity,
trusted_root)` then takes the Source outside the Volumes only when its identity
is rooted at exactly that folder, reached beneath it with no link followed;
anywhere else is refused as before. `_trusted_roots(job)` adds the folder (after
a bound Source's), so the bind, the companion check and the post-conversion
checks read beneath it. A Finding for one of them
is written on the archive (the file the user dropped), naming the Image (`_hold`
redirects through `_unpacked_holding`). A placed Image stays in the slot,
counted (`note_processed`) and reported in `placed` with the archive's path; the
archive's own Findings stay until it moves, since another of its Images may
still be held. Once every Image is placed, `_settle_unpacked` starts the
archive's move to `processed/` (the same anchored, locked move, a flight
carrying `unpacked`), and the slot is closed then: intake's own scratch, the
archive still holds every byte. An archive that leaves the Inbox or changes has
its slot closed the same way; a changed one is unpacked again once it settles.

**A slot is never kept for held Images** (Codex 4608429476). When every Image
left in a slot is held and none is in flight, `_settle_unpacked` closes it and
holds the archive itself (`_hold_unpacked`), its Findings as they are: the
archive stays the source of truth. The archive's hold carries its Images'
dependencies: the DAT ranking a naming conflict was held under
(`ranked_holds`) and the Library paths a taken name was held for
(`taken_holds`, lifted once one of them is free, `_freed`). When either
changes, or the archive does, it is unpacked again, and an Image placed before
counts by its bytes (`_same_as_placed`), or, for a conversion, whose output's
bytes can't be compared with its Source, by its record (CodeRabbit 4608968248):
every Image placed from an archive is recorded in `_State.placed_members`,
outside the slot, by the archive's fingerprint and the Image's path in the
archive, with every output file and its identity, the file and its members (a
CUE's BINs, an extractcd's tracks; `_record_placed`, Codex 4609181297). An Image
unpacked again whose output files are all still those very files counts as
placed with nothing taken (`_placed_before`); one missing or changed is taken
again, and a name still taken holds it. The records go when the archive moves to
`processed/`, changes or leaves the Inbox, and are kept in the settle memory.
A loose file's taken-name hold lifts the same way once the name is free.

**Room to unpack** (same thread). Every open slot reserves its archive's
private copy (`_UnpackSlot(size)`) and, once the copy is gated, its declared
contents (`grow`), in `_slot_sizes`, and all of them together
stay under `CHD_ARCHIVE_INBOX_MAX_UNPACKED_SIZE`
(`ArchiveService.inbox_unpacked_limit`: the Inbox total cap,
`CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE`, when unset; 0 is no limit). The work area
must also hold it beyond its reserve (`disk.ensure_headroom` with
`scratch_margin_bytes`, the cushion every scratch-dir check keeps). An archive
that doesn't fit waits (`_NoRoom`): no Finding, it stays settled and is tried
again on a later poll, logged once per wait (`_wait_for_room`). `_take_archive`
looks first, from the cached listing, so a waiting archive starts no job; the
unpack job checks again as it reserves. Either way the wait goes through one
place, `_wait` (#470): it appends `{"path", "mode", "reason"}` to the Inbox's
`waits`, which `poll` and `finish_all` return like `placed` and `left`, and the
unpack job, when there is one, ends with the same reason
(`finish_external_job_waiting`). An unpack left to settle (`changed`,
`deferred`) is recorded the same way. `run-rules` prints one `waiting` line per
recorded wait from the pass results (`cli._print_wait`, under the archive's
file name), and skips a waiting job's own line, so each wait is reported once
whichever check found it. `report.after_pass` and the server's tick ignore
`waits`: a wait is neither news nor a failure. The cap keeps many archives from
adding up past it: one with nothing else unpacked always goes in (the gate
bounds it), so a single large archive is never stuck behind its own copy.

**Removing the Inbox** (Codex 4608007773) closes a slot nothing reads at once.
One a queued or running conversion, or a copy's task (one waiting for a
conversion slot included, CodeRabbit 4608968241), still reads from is kept
(`_close_after`): jobs are counted down by the job manager's terminal hook
(`add_terminal_listener(_job_ended)`), tasks by a done callback added on their
own loop, one that ended before it was watched is counted at once, and the slot
is removed when the last reader ends, so the work runs as started.

**Recognizing and naming (#432).** Where something can answer
(`routes.dat.matching_available`: a DAT is loaded, or Hasheous is on), an Image
is matched before it is placed, after its plan: an `inbox_recognize` job
(`ConversionMode.INBOX_RECOGNIZE`, the same `ExternalMode` as the copy) runs
`routes.dat.match_one` (the match job's per-file step: embedded hashes, then the
file SHA-1, then Hasheous) on the very bytes taken (CodeRabbit 4608968259):
the Image's main file is bound into the app's work area first, as a
conversion's Source is (`_bind_for_match` over `bind_source`: a hard link or a
checked copy, opened beneath its root only while it is the file taken), the
match reads the bound file (its tool first primes the embedded hashes it reads
for it, `BaseTool.prime_embedded_hashes`, a no-op that only chdman overrides:
chdman reads a CHD's SHA-1s with
`chdman info` into `chd_metadata_store`, bounded and cancellable, Codex
4609181291), and it is kept only while the bound file is
unchanged (`BoundSource.changed`), with the identity it was made for; `_name`
uses it only while the file's fingerprint and identity are still those. Its
first look at the file (its stat, and the size cap's) is detached, bounded and
cancellable too for this caller (`_stat_source`, CodeRabbit 4608968234). Its
whole-file SHA-1 is
stall-bounded and cancellable (Codex 4608007769): `match_one` asks
`_match_single_file` for it through `run_detached_within_stall_bound`, the seam
a bound Source's copy uses, with the job's cancel event, and
`file_hasher.compute_file_sha1_sync(progress=...)` reports each chunk. A read
with no progress for `_RECOGNIZE_STALL_SECONDS` (the conversion's own stall
limit by default) fails the job with a Finding, a Cancel raises `ReadCancelled`
and ends it cancelled, and the slot is given back either way. The match job and
the routes keep their own read, unchanged. Its wait for the `match` lane
(`workload_limiter.acquire`) stops at the job's Cancel too (Codex
4608417866), for this caller only. The result is kept with
the fingerprint it was made at (`_State.recognized`); the next poll names the
Image from it. It never reads a row cached for the path: an Inbox name is
reused by the next download, and a cached hit would name it. The result is
ranked by the ranking as it stands (`DATStore.with_current_ranking`,
`dat_ranking.ranked_match`, the two halves of `ranked_match_for_image`). A match
that can't be made (an error, a process stuck on the storage) holds the Image
with an `inbox_failed` Finding; one over the match size cap is not Recognized
and keeps its name.

The top-ranked DAT names it by the rename's one rule
(`dat_rename.dat_file_name`: the game name with the file's own extension,
compound ones whole, through `safe_file_name`; a numbered part of a split output
keeps its logical extension and part number, `Game.iso.0` becoming
`Canonical Game.iso.0`, read with the registry's `split_owner_name`, Codex
4608919686). A copy places its files under `dat_rename.dat_file_names`
(`_Flight.placed_names`, read by the staging, the output locks and the
taken-name check): its main file, and every other part of a split output alike,
so the set is still one; a sheet's tracks keep their names, so the sheet still
names them. A conversion's output takes it with the output's extension
(`_destination(game_name=...)`, checked with `output_status`). Two DATs the
ranking puts level that name it differently (`ranked.conflict`) hold it with an
`inbox_conflict` Finding naming both (`dat_rename.conflict_text`, the words the
rename's `rename_conflict` Finding uses): never guessed at, as the rename leaves
it unnamed. The hold records the ranking it was made under
(`_State.ranked_holds`, `_ranking_now`: every DAT and its rank); a poll that
finds the ranking changed lifts it and takes the Image again, without waiting
for the file to change.

**Taken names (#432).** The name an Image would take in the Library, its DAT
name or its own, is checked before anything is written: a copy's names
(`_taken`), a conversion's output and its companions (`output_status`). A
copy's taken names are first compared with the Image's own files
(`_same_as_placed`, stall-bounded, under the output locks a copy would take, so
no job writes them meanwhile): every name a regular file of the same size, then
the same SHA-1 (`stream_digest.new_hasher`), both sides opened with no link
followed and the Image's files only while they are the ones taken; after
hashing, the Library's name must still be the very file read (device, inode,
size, mtime and ctime, CodeRabbit 4608968255). All the same
means it was placed already (by this Inbox before a restart, with its state
lost): it is counted placed with no Finding and finished as after a copy
(`_start_finish`: the original to `processed/`, or, unpacked from an archive,
counted so the archive can complete). This holds for loose files too. A
conversion's output can't be compared with its Source, so a taken one stays a
hold. One on disk that differs holds the Image with an `inbox_failed` Finding
naming it; one only another
job holds is a retry with no Finding. Nothing is ever overwritten: the copy's
placement never replaces, and a conversion is queued without
`allow_overwrite`.

**Seams.** Each poll returns
`placed` (`{"path", "platform", "output"}` for every Image whose output
landed in the Library since the last poll, an Image unpacked from an archive
with the archive's path, which also counts it toward that archive's move: recorded by `_note_placed` the
moment a copy is placed or a conversion completes, before the original's move
to `processed/`, so a move that fails, which keeps its `inbox_failed` Finding
and its hold, still has its output scanned by RomM and summarised, Codex
4608095510) and `left` (`{"path", "kind", "reason"}` for every Image `_hold`
gave a Finding since the last poll; a held file isn't held again, so it is
reported once). Both are kept on the Inbox's state until a poll hands them
out, so a copy or a move that ends between polls is reported by the next one.

**After a pass** (`inbox/report.py`, #433). `report.after_pass(results)` takes
one pass's poll results, groups them by Library, calls each Library's
`after_intake` hook (§3.3.11) once with what was placed in it (RomM asks for a
quick scan of just those Platform folders), and, when anything was placed or
left, or a poll hit a new error, sends one `inbox_pass` notification
(registered with the others in `services.notifications`) listing the files,
the Findings' reasons, the errors and the scan outcome. Every result's real
`error` is carried in the summary (`errors` per Library); `poll_running` and
`inbox_removed` aren't failures. An error notifies only when it's news: `poll`
sets `error_new` when its error differs from the Inbox's previous
`state.error` (a poll that raises keeps its error the same way, CodeRabbit
4608135791, and `poll_all` compares one that escapes `poll` too), so an Inbox
left unreadable doesn't notify every minute on the
server, and notifies again once it was fixed and breaks again. A
`run-rules` process starts with no state, so each run that hits it reports
it. A pass that placed and left nothing and hit no new error sends nothing
and asks no one. The server's tick (`libraries.run_forever`) runs it after
`intake.poll_all()`; `run-rules` runs it after its own pass, so the two can't
drift.

**RomM's Platform scan.** `RommClient.scan_platforms(folders)` is `POST
/api/tasks/scan` with `{"type": "quick", "platform_fs_slugs": [...]}`, each
entry a Platform's folder name on disk. Confirmed against RomM 5.4.1 (source
and a live container, `scripts/spike/romm_platform_scan`, findings in
`docs/ROMM_INTEGRATION_ROADMAP.md`): it needs `tasks.run` (403 without),
scans only the named folders, finds a folder with no Platform row yet and
creates the Platform, scans nothing for a folder that isn't there, and refuses
an unknown key with 422 (RomM's `ScanPayload` forbids extras), so a typo can't
widen it to the whole library. `apis` is left out, so the scan uses the
metadata sources RomM has on, as its own scheduled scan does. The fake RomM
(`tests/fake_romm.py`) gates every scan on `tasks.run`, refuses unknown keys,
records each Platform scan in `platform_scans`, and `scan_reply` makes it
answer 409 or 503.

**From `run-rules`** (§3.3.17). The CLI is a new process each run, so the
settle memory (seen, held, what an ownership hold depends on, and the last
poll's error, so the same outage isn't notified again each run, Codex
4608095505, and each original whose move to `processed/` was put off
because another process held it, with its files, identities and output,
CodeRabbit 4608135794) is kept
between runs: `intake.settle_memory(inbox_ids)` serialises it and
`intake.restore_settle_memory` takes it back (what the process already knows
wins; an unreadable entry is dropped, so the file is simply seen again), under
the `inbox_settle` preference. A put-off move comes back as a flight with
only its archival left: the next poll's `_settle_flights` retries the move,
which goes ahead only while the original's fingerprint and identities are
the ones taken; changed, the file is new and is taken normally. Saving the
memory is guarded on its own (CodeRabbit 4608135785): if it fails, the run
still runs `after_pass` (scan, summary), prints an
`inbox_memory_not_saved` error line and exits 1. An Inbox job (one with an
`inbox_id`) that fails doesn't fail the run: it leaves its Finding, which is
the report (CodeRabbit 4608135778); a Rule job that fails, or an Inbox that
can't be polled, still does. `poll_all(library_ids=...)` polls only the
Inboxes of the Libraries the run swept. Every job intake makes is tagged when
it is made (`intake._tag`: `ConversionJob.library_id` and `inbox_id`), and
run-rules names a job's Library from that, not from which poll was running
when the job appeared: a copy's job is made when its task runs, which can be
during another Inbox's poll (Codex 4607940252). Each Inbox conversion is
settled the moment it ends: run-rules registers `intake.on_job_finished` as a
terminal listener, which finds the flight by the job's `inbox_id` and settles
it from the job it was handed (`_settle_ended`, shared with `_settle_flights`;
every change to the flight happens before its first await, so the two never
settle it twice), so a big pass doesn't depend on the job history still
holding early jobs (Codex 4608095516). The listener counts itself in intake's
tasks, so `drain()` waits for it. `intake.finish_all()` then sweeps up what is
left as the next poll would on the server: settles any conversion still in
flight (originals to `processed/`), waits for every copy and move, and hands
back what was placed or left, so nothing a run started is left in flight when
it exits.

**#432 under #433.** The unpack and Recognize jobs are tagged with their
Library and Inbox (`_tag`) like a copy, a shutdown cancel of either releases the
file with no Finding (`_cancelled_by_shutdown`), and `on_job_finished` leaves
them to settle themselves. An unpacked or Recognized Image is taken on at once
by the job that made it ready (`_take_on`, `_take_unpacked`; never after the
last poll's stop, `_State.should_stop`): it doesn't wait a poll, and a
`run-rules` pass, which polls once and keeps no match or unpack between runs,
places it in the same run. `finish_all` then starts the move of every archive
whose Images are all placed and, the process ending, closes what was unpacked
(`_close_unpacked`); a later run unpacks an archive still in the Inbox again
and counts what it placed already by its bytes (`_same_as_placed`).
`run-rules`'s pass drains to a fixed point before `finish_all` (Codex
4608417841): it waits for the job queue and intake's own tasks in turn
(`job_manager.has_live_conversions`, `intake.busy`) until neither has anything
left, since an unpack or a Recognize may queue a conversion after the queue
last emptied. A stop cancels what is queued and ends the loop; once intake's
own work has drained, whatever it queued meanwhile is cancelled and drained too
(CodeRabbit 4608968227), so the run never ends with a live job. The settle
memory a run keeps includes `ranked_holds` and `taken_holds` (Codex
4608417851), so a later run lifts a hold whose ranking or name changed.

**Taking a path in now (#522, `services/inbox/trigger.py`, `routes/hooks.py`).**
`POST /api/hooks/arr` (Sonarr, Radarr, ROMarr webhooks) and `POST /api/scan`
(`{"path"}` or `{"paths"}`) are one path into the same intake; there is no
second pipeline. Both need the app's auth when it is on and the action
confirmation header `X-Chd-Action-Confirm: scan`, the *arr `Test` event
included, so its Test button proves the whole setup. `trigger.paths_in` reads
`path`/`paths` and the `path` of every `*File` object and `*Files` list, so an
*arr with its own field name needs no code; only `Download`/`Import` (or no
`eventType`) take anything. Each path is rewritten by the longest whole-folder
`from` (Autoscan's `from`/`to`, stored in the `arr_hook` preference and set in
the UI only), then must be `volume_containment(...) is True` (a "couldn't
tell" refuses it) and lie under an Inbox's folder. `intake.mark_ready` records
it on that Inbox's `_State.ready`, and the Inbox is polled at once
(`_poll_now`, waiting out a poll already running, then
`report.after_pass`). A poll treats an Image as settled when a marker names it,
a folder above it, or one of its files (a sheet's track): only the settle wait
is skipped. Every marker covers only what was last written by the time it
was named (every file of the Image, a sheet and its tracks, by mtime against
the marker's wall-clock time), so a download still being written there, or a
new one at a named path, settles as usual. A taken Image consumes its own
markers (the file, its tracks), never another sheet's beside it; a folder's
marker stays for the rest of the folder until it expires. `trigger.process` resolves the marker's path in the same bounded
worker as the check (`_checked`) and hands it to `mark_ready(...,
resolved=True)`, so nothing touches the disk on the event loop (PR #526). A held Image stays held, and only files the poll lists (no link,
no `processed/`) are ever taken, so a marker can't reach anything intake
wouldn't. Markers expire after `READY_SECONDS`. `trigger.check` is the
dry run the UI's **Test** uses; for a path that lands nowhere,
`trigger.suggest` looks for the same tail of folders under each Volume and
offers the rewrite. The last 20 paths received are kept in memory for the UI.

A path no Inbox holds but a Library does (an *arr importing straight into the
games folder, #541) is that Library's: `check` names its `library_id`
(`libraries.library_for`, so overlapping Libraries pick the nearest root and an
ambiguous one is refused), and `_checked` works out, in the same bounded
worker, the Library's console ids for the game: what `detect_platform` says,
the person's folder mappings included, matched on the console's id or slug;
only a game detection can't name at all falls back to the folder it sits in
(`Library.platform_folder`, matched on the console's folder, RomM's renamed
ones), and one whose header and folder disagree runs nothing. `_due_in_library` then fails closed: the path is converted only when
the Library converts on its own (`schedule_enabled()`), the console's
conversion Rule has a mode and is on, and now is inside its window
(`schedule._in_window`); otherwise it is skipped with why (only a console
outside its hours waits for its next scheduled run). Then
`rules_sweep.sweep(library, platform_ids=..., ignore_schedule=True,
kinds=(CONVERT,), honour_pauses=True)` runs, the same pass as Run now for those
consoles: its containment, filters, duplicate policy and in-flight checks
decide. `kinds` (default every kind) keeps a console's check Rule from spending
a budget on an import. `honour_pauses` drops Run now's named-Platform override
(a sweep that names its Platforms runs a Rule the person paused): the pause is
read again under `_sweep_lock`, so a console paused after the import was
accepted stays paused, and it holds for the deferred deletes of kept Sources
too: an import never deletes an original for a paused console. Consoles that
are one to detection (RomM's `nes` and `famicom`) are narrowed to the one
whose folder holds the file, or none. The Library's listing is dropped once
per request (`note_paths_changed` with every accepted path), not once per path. `trigger.check_one`, the UI's **Test a path**, returns
`_due_in_library`'s answer for a Library path, so it never says "works" for an
import that would be skipped.

`rules_sweep.release_failed` drops a console's failed holds and nothing else:
the console table's **Try them again** sends `failed_only` to
`.../rules/forget-converted`, which keeps the converted history a rename or
overwrite Rule needs to never convert a game twice. Forget history still
clears both.

### 3.3.26 Event streams end cleanly on shutdown (`services/event_stream.py`, #465)

Every server-sent event route returns `event_stream(generator)`, never a bare
`EventSourceResponse` (a test checks `routes/`). `sse_starlette` notices
uvicorn's shutdown and, by default, cancels the whole response, so the closing
body chunk never goes out and uvicorn logs "ASGI callable returned without
completing response." for each open stream. `event_stream` passes the
library a `shutdown_event` and a `shutdown_grace_period`
(`SHUTDOWN_GRACE_SECONDS`, 5 s), and wraps the generator in `until_shutdown`:
each wait for the next item runs in a cancel scope that the shutdown event
cancels, after which the generator is closed (its `finally` runs) and the
iteration ends. The response then sends its closing chunk and returns like
any finished stream. The grace period is only a backstop for a generator that
ignores cancellation. A new streaming route uses `event_stream` too.

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
    def convertible_extensions(self) -> tuple[str, ...]:   # sorted (issue #183)
        return tuple(sorted(set().union(*(t.input_extensions for t in self._tools.values()))))
    def tools_for_input(self, filename: str) -> list[ToolPlugin]:
        return [t for t in self._tools.values()
                if match_extension(filename, t.input_extensions) is not None]
    def tool_for_verify(self, path: str) -> ToolPlugin | None:
        # Most specific claim wins (longest matched extension; registration
        # order breaks ties), so a compound product like `.xiso.iso` resolves
        # to its own tool rather than Dolphin's generic `.iso`. Single-suffix
        # paths resolve exactly as the old first-match did.
        best, best_len = None, 0
        for t in self._tools.values():
            m = match_extension(path, t.verify_extensions)
            if m is not None and len(m) > best_len:
                best, best_len = t, len(m)
        return best
    # Discovery helpers (issue #131): the union of every tool's produced /
    # verifiable extensions drives the registry-driven library scan, so a new
    # tool's outputs become scannable for free.
    def output_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(set().union(*(t.output_extensions for t in self._tools.values()))))
    def verify_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(set().union(*(t.verify_extensions for t in self._tools.values()))))
    def scannable_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.output_extensions()) | set(self.verify_extensions())))
```

#### Platform narrowing (`narrow_to_platform`, `ToolPlugin.platform_slugs`)

Extension matching decides *convertibility*, which is all a bare filesystem
listing can know. A library manager knows more: RomM stamps a `platform_slug` on
every ROM record, and that is the only thing able to tell a GameCube `.iso` from
a PS2 one.

Each tool declares the platforms it serves as `platform_slugs: frozenset[str]`
(dolphin: `{"ngc", "gamecube", "wii"}`; maxcso: `{"psp", "ps2"}`; the default is
an empty set, meaning "no opinion"). `narrow_to_platform(tool_ids, slug)` then
*removes* candidates the platform contradicts — it never adds any.

It is conservative by construction, because a wrong exclusion is worse than a
missing one. With no slug, an unrecognised slug, or a slug no tool claims, the
list is returned untouched; a tool declaring no `platform_slugs` is never
dropped. So a platform we have never heard of degrades to plain extension-only
behaviour rather than to an empty list. Both consumers go through it: the RomM
catalog listing narrows each row's `convertible_by`, and `/romm/platforms`
returns the narrowed `tool_ids` so the automation editor offers a platform only
the modes it can actually use.

#### Operator-authored filter patterns

The automation rules take real regexes — "convert only `(USA)`" is the feature —
which makes them the one place user input becomes executable. Two bounds, and
one accepted alert.

`_valid_pattern` caps the source at `_MAX_PATTERN` characters, refuses one that
does not compile (refuses, never drops: a rule whose filter silently vanished
would come back as *unfiltered*, and the next unattended sweep could queue the
whole platform with delete-on-verify attached), and then refuses one that
backtracks. `re` cannot be interrupted, so a pattern like `(a+)+$` would hold
`_sweep_lock` indefinitely — blocking previews, manual runs, and the very rule
edit that would remove it.

**`_has_nested_quantifier` reads the pattern rather than running it.** The
obvious implementation times the regex against an adversarial string, and that
was the first attempt; it is wrong three ways. It has to survive the very
blow-up it is looking for (the runtime doubles per character, so measuring at
full length hangs the check). It is load-dependent, so a busy machine rejects
patterns that are fine. And executing an operator-supplied regex is exactly what
CodeQL's `py/regex-injection` flags — the query's only sanitizer is `re.escape`,
which would turn "convert only `(USA)`" into a literal search and delete the
feature. A structural test avoids all three: it looks for a quantified group
whose body itself repeats (`(a+)+`, `(\w+\s?)*`) or is an alternation
(`(a|a)+`), which are the shapes with exponentially many ways to split one
input. Escapes and character classes are stripped first, so a literal `\+` or a
`[+|]` class is not mistaken for a quantifier.

**The two quantifier sets are not the same set.** What may follow a *group*
is `*+{` — `(...)?` cannot blow up, because the outer level consumes at most
once. What counts *inside* a group that is already repeated unboundedly is
`*+{?`, because there every optional atom doubles the ways the engine can split
the same input: `^(a?){30}a{30}b$` compiles, looks tame, and walks
exponentially many choices against a long run of `a`. Bounded repetition is not
a reprieve — `{30}` is thirty levels of doubling. Getting this wrong is not just
a slow sweep: the probe thread is abandoned rather than stopped, so repeated
scheduled retries eat into the process-wide probe capacity that unrelated
filesystem checks share.

A group's leading `?` is stripped before that test, because in `(?:…)`, `(?=…)`,
`(?<!…)`, `(?P<name>…)` and inline-flag groups it is *syntax*. Reading it as an
optional atom would refuse every non-capturing group — and a refused filter
disables the rule, so an operator who cannot write `(?:USA|Europe)` writes
something looser instead, which is the widening this validator exists to stop.

It is conservative on purpose — `(USA|Europe)+` is harmless and still refused —
because being wrong at save time costs one clear message, while being wrong the
other way wedges the scheduler.

#### Composite modes narrow per mode (`ModeSpec.platform_slugs`)

Narrowing by tool cannot separate a composite tool's modes. `ChainTool` is a
shell that belongs to no system: it owns `nkit_to_rvz` (GameCube/Wii) and
`cso_to_chd` (PS2/PSP), so it legitimately survives `narrow_to_platform` on
both — and offering *both* modes on *both* is how a GameCube disc gets a rule
targeting a PS2 format.

**`ModeSpec.platform_slugs`** (default empty, meaning "ask the tool") lets a
mode name its own systems. `mode_allows_platform(mode, slug)` applies the same
conservative rules as `narrow_to_platform` one level down, and
`modes_for_platform(slug)` returns the whole applicable set — served to the
browser as each platform's `mode_ids` beside its `tool_ids`, so neither the
automation editor nor the RomM target picker carries a second copy of the
platform table. The sweep checks the *mode*, not `spec.tool_id`.

Composite tools are not the only case. A tool whose modes are *different
commands over different media* has to declare per mode too: CHDMAN's
`createcd` writes a CD track layout and `createdvd` a flat DVD image, and
inheriting the tool's combined slug set offered a PS2 catalog both — with
`createcd`, the first CREATE mode, as the default an ISO submission landed on.
`chdman.py` therefore splits its platforms into `_CD_PLATFORMS` /
`_DVD_PLATFORMS` / `_HD_PLATFORMS` / `_LD_PLATFORMS`, gives each create and
extract mode the set its command actually serves, and derives the tool-level
`platform_slugs` as their union so the two levels cannot drift. `copy` declares
nothing and inherits: recompressing a finished `.chd` is media-agnostic.

The rule for a new tool: declare `platform_slugs` on the *mode* whenever two of
a tool's modes would be wrong for each other's platforms, and let the tool-level
set be the union.

#### What may run unattended (`registry.mode_is_automatable`)

A rule fires repeatedly over a library RomM rescans, so a mode is only
automatable if that loop **terminates**. The one that does not is CHDMAN's
`copy`: `Game.chd` becomes `Game_copy.chd`, RomM lists it, the same rule
accepts it as a source, and the next sweep writes `Game_copy_copy.chd` — full
size, once per sweep, until the volume fills. Nothing downstream stops it,
because the destination is new every round: the duplicate policy and the
converted history both agree it is work not yet done.

The test is not "does it accept its own output" but **where would it write
it**. `dolphin_rvz` over a `.rvz` resolves to the *same* path, which the
queue's same-path guard refuses — so it settles rather than multiplying. The
registry therefore asks the tool: derive the output path for a synthetic source
carrying the mode's own output extension, and refuse the mode when the answer
differs from it. Derived, so a future tool of the same shape is excluded
without anyone remembering.

Enforced in two places, because a rules blob can be written straight into the
database: `normalize_rule` drops such a rule, and the editor omits the mode
(served through `/romm/status`'s `automatable_modes`, since the derivation
needs the tool's `output_path` and cannot be mirrored in JS). Extract modes are
a *separate*, UI-side exclusion — they terminate, and rules using them work.

**A mode can also opt out by policy.** `ModeSpec.automatable` (default `True`,
mirrored on `ChainSpec`) set `False` makes `mode_is_automatable` answer no
before the termination test runs, so the same two enforcement points refuse
it. The eXoDOS repack (`exodos_repack`) is the one mode that sets it: it is
manual only, because a Rule over RomM's `dos` folder would write a second copy
of every game beside it, and the Rule's verify step would hide the conf warning
nobody is there to read.

#### What a mode writes (`registry.mode_output_extensions`)

`mode_output_extensions(mode)` is the set of extensions a mode writes,
lowercased. A mode with a declared `output_ext` answers that. A mode whose
output is mapped from its input (Z3DS's `.cia` to `.zcia`, NSZ's `.xci` to
`.xcz`) declares none, so the registry asks its tool's `output_path` for each
input extension the mode accepts, the same question `mode_is_automatable` asks.
It is path arithmetic only and reads nothing from disk.

Its first caller is RomM's converter (#385, `services/romm/converter.py`).
RomM names its library targets (`chd`, `rvz`, `z3ds`, ...) and the module maps
each onto the extensions rom-converto writes for it. A mode matches a RomM
target on a Platform when it is in `modes_for_platform(slug)`, is automatable,
is not an extract mode (the Rule editor offers none), and writes one of those
extensions. The first match in registry order is what "Import RomM's targets as
Rules" picks. A new tool that writes one of RomM's formats is matched with no
change there.

RomM's converter config comes from two endpoints, both read by
`RommClient.converter_config`. `GET /api/heartbeat` carries
`CONVERTO.ENABLED` (absent on a RomM from before the converter).
`GET /api/config` carries `CONVERTO.platform_formats` (RomM Platform slug to
target) and `CONVERTO_LIBRARY_TARGETS` (every target each slug can take). The
config is read only when the converter is on, during the RomM status refresh,
and the status's `converter` block is the one source for the banner, the Rule
flags and the can't-convert marks. None of it changes what a Rule or a sweep
does.

#### The swap switch and its token check (#390)

`swap_keep_rom_id` is a RomM setting (`services/romm/settings.py`, env
`ROMM_SWAP_KEEP_ROM_ID`, off by default), stored with the rest of the RomM
settings and returned by `GET /api/romm/settings`. It turns on the RomM swap
Referrer (#391, 3.3.15) through `RommLibrary.referrer_enabled`.

The swap needs `roms.read` (the read-back), `roms.write` (renaming the record)
and `tasks.run` (the rescan). RomM has no endpoint that lists a token's scopes,
so `RommClient.token_permissions()` asks with the three probes found in the
spike (#382, RomM 5.4.1): `PUT /api/roms/2147483647` is 404 with `roms.write`
and 403 without it, `GET /api/tasks/status` is 200 with `tasks.run` and 403
without, and `GET /api/roms?limit=1` is 200 with `roms.read`. A 403 is a missing
scope; any other status is not an answer about scopes and raises `RommError`,
so a 401 or a 5xx is never read as "missing". Each probe goes through
`RommClient._request` with its own 10 second `timeout` (the new optional
parameter, which `_urlopen` takes too), and the route runs the three in the
threadpool under `swap.CHECK_SECONDS`, so a RomM that stops answering holds
neither the loop nor the request. `services/romm/swap.py` owns the wording:
the scope list, the admin-token hint, and the refusal.

`PUT /api/romm/settings` with `swap_keep_rom_id: true` runs the check first,
against the URL and token the same request would save, before any lock and
before anything is persisted. Missing scopes answer 422 naming each one; no
token is a 422; RomM not configured, unreachable, or rejecting the token is
503, 502, or 504 and the switch stays off. Turning it off, or saving anything
else, asks RomM nothing. `GET /api/romm/status` carries
`swap: {keep_rom_id, missing, error}`; it probes only when the switch is on and
RomM is connected, so a token that has lost a scope shows up there.
`tests/fake_romm.py` answers the probes from a configurable `scopes` set.

#### Asking a tool whether it can run (`registry.tool_is_ready`)

`ToolPlugin.is_ready()` is a coroutine, which makes it look bounded and does
not make it so. NSZ's delegates to a pooled `keys_available()` that walks every
configured volume looking for `prod.keys`, with no deadline of its own: on an
NFS/SMB/rclone mount that has stopped answering it blocks in uninterruptible
I/O and holds a shared-pool worker while every caller waits behind it.

The callers all fan out over the whole registry, so the cost is a whole screen.
`GET /api/tools` — the sidebar's tool list — awaited each tool *in turn*, so one
dead volume held up every tool that was fine behind it.
`GET /api/romm/platforms` gathered them, leaving the Library and the automation
editor loading forever. Retry either and each attempt stranded another worker.

**Ask through `registry.tool_is_ready(tool)`, never `tool.is_ready()`
directly.** It bounds the probe at `READY_PROBE_SECONDS` (20s — generous,
because the honest answer for NSZ is a real walk of a possibly large share) and
reports **not ready** when it expires: the same answer a missing binary gets,
and the right one, since a tool whose prerequisites cannot be read cannot
convert. `registry.ready_tool_ids()` is the fan-out form, concurrent rather
than sequential. The RomM sweep's `_ready_bounded` is a thin alias kept for its
local consequence: the sweep holds `_sweep_lock` throughout, so a hang there
also blocks previews, manual runs, rule edits and settings saves.

**And the other half is the plugin's.** The bound frees the *caller*, and only
the caller — Python can abandon an awaiter, never an OS thread. Whose thread is
abandoned is decided by the implementation, so **an `is_ready()` that touches
the filesystem must use `run_detached`, not `run_in_threadpool`.** A pooled
worker wedged on a dead mount is never handed back, and since every caller fans
out over the whole registry, repeated probes retire the pool one worker at a
time until unrelated offloads have nowhere to run — the bound turns a hang into
a leak rather than removing it. `nsz` (a recursive walk for `prod.keys`),
`exodos` (its template tree), `rom_converto` (its own `--version` probe) and
every tool that declares `required_binaries` (below) touch the disk; the
requirement is stated on `ToolPlugin.is_ready` so a new gated tool inherits it.

#### Declaring the programs a tool runs (`required_binaries`, #494)

Where a program is found (#515): each image-installed fixed path
(`config._IMAGE_BINARIES`: `CHDMAN_PATH`, `DOLPHIN_TOOL_PATH`, ...) is
resolved once, when `Settings` is built, by `config.detected_binary`: the
default when it exists, else the same base name on `PATH`, else the default
(so the not-ready reason names it). A path the operator set is never
replaced. `SEVENZIP_PATH` falls back to `7zz` the same way. A new tool whose
image path is fixed adds its field to `_IMAGE_BINARIES`; the probe below then
checks the resolved path like any other.

A tool that spawns a program does not write its own readiness check. It
declares the program on `BaseTool.required_binaries`, a tuple of
`services.tools.binaries.RequiredBinary(label, path, setting)`, and
`BaseTool.is_ready()` asks the shared probe for each one. `path` is the value
the service spawns, read at each probe (`self._service.chdman_path`, not a copy
taken at construction), so a test or a settings change that moves it is seen.
A value with a path separator is checked where it is; a bare name (`7z`,
`zstd`) is looked up on `PATH`, as the spawn would. `setting` is the
environment variable that sets it, named in the reason.

| Tool | Converts with | Checks with (`verify_required_binaries`) |
|---|---|---|
| chdman | `chdman` (`CHDMAN_PATH`) | same |
| dolphin | `dolphin-tool` (`DOLPHIN_TOOL_PATH`) | same |
| z3ds | `z3ds_compressor` (`Z3DS_COMPRESSOR_PATH`) | `zstd` on `PATH` |
| cso | `maxcso` (`MAXCSO_PATH`) | same |
| romz | `7z` (`SEVENZIP_PATH`) | same |
| makeps3iso | `makeps3iso` (`MAKEPS3ISO_PATH`) | none: PARAM.SFO is read in Python |
| xiso | `extract-xiso` (`EXTRACT_XISO_PATH`) | same |
| nkit | `nkit2iso` (`NKIT2ISO_PATH`) | (no check) |
| jwud | the JWUDTool launcher (`JWUDTOOL_PATH`) | none: the WUX walk is Python |
| rar | `unar` (`UNAR_PATH`) | none: the extracted file is read in Python |

`verify_required_binaries` is None by default, meaning a check needs what a
conversion needs and `verify_is_ready()` is `is_ready()`. A tuple, possibly
empty, is probed on its own, which is how a check that runs something else
(z3ds's `zstd -t`) or nothing at all (makeps3iso, jwud) stays verifiable when
the converter is missing. Not covered: nsz and rom-converto keep their own
`is_ready()` (prod.keys; a `--version` run), and exodos runs no program.

**The Pipeline (chain) tool.** It runs no program of its own, so it declares
none, but it is only as ready as the tools its steps run. `ChainTool.is_ready()`
asks `registry.tool_is_ready` for every step tool of its modes (concurrently,
each under the registry's bound) and is not ready when any one is, with that
tool's `not_ready_message` as its reason ("Pipeline isn't ready: NKit isn't
ready: nkit2iso was not found at ..."). No tool id is checked: the step tools
come from `ChainSpec.steps` through `registry.for_mode`. Readiness is per tool
in the contract, and every caller (the tool list, Auto's plan, Rules, Inbox
intake, the CLI) asks it of `registry.for_mode(mode)`, which for a chain mode
is the chain. So both chain modes go together: with dolphin-tool missing,
`cso_to_chd` is held back as well as `nkit_to_rvz`. Readiness per mode would
keep the other one, at the cost of changing every caller (`plan_auto` takes a
set of ready tool ids); until then the chain fails closed.

**What the probe checks.** Present, a regular file, and executable for this
process (`os.access(X_OK)`), nothing more. It never runs the program: several
of these tools have no cheap, non-interactive way to print a version, and a
spawn on every probe costs far more than a stat. A binary built for the wrong
architecture therefore still reads as ready and fails at job time, as before;
rom-converto, a downloaded per-arch release, keeps its own `--version` probe
for exactly that.

**Why it isn't ready.** `_binaries_ready` keeps the first failing program's
reason on the instance, as `not_ready_reason` for a conversion and as
`verify_not_ready_reason` for a check, and drops it once the program is there.
The reasons are "`<label>` was not found at `<path>` (set `<SETTING>` to where
it is)", "`<label>` is not installed (no `<name>` on PATH)", "`<label>` at
`<path>` is not executable", "... is not a file" and "... could not be read:
`<error>`". Every caller shows them through `not_ready_message` (below), so the
tool list, the File info 503, the CLI, Auto's plan, the Rules sweep and Inbox
intake say the same thing.

**How it runs, and the cache rule.** The stat is `run_detached`: a configured
path on a mount that has stopped answering costs a disposable thread, never a
shared pool worker. Answers are cached per configured path for
`BINARY_PROBE_TTL_SECONDS` (30s), success and failure alike, so the tool list
and every sweep, which ask every tool each time, stat each binary at most twice
a minute, and a program that is installed, removed or `chmod`-ed is noticed
within that window. While a probe of a path is running, other callers on the
same event loop await that probe (keyed by loop and path) (shielded, so one caller's bound doesn't
cancel it for the rest) instead of starting another, so a dead mount holds one
written-off thread per path rather than one per request. A probe that raises
(the detached-probe ceiling) is not cached. `binaries.forget_binary_probes()`
drops every answer, for tests.

**Callers.** Every readiness caller already went through
`registry.tool_is_ready`: the tool list, the RomM platform list, the File info
routes, the CLI, Auto's plan, Inbox intake and the Rules and Library sweeps.
Each already handled a tool that answers False (nsz without keys), so a tool
whose program goes missing drops out of all of them at once. Inside the Docker
image every program ships, so nothing changes there; outside it, a tool whose
program is missing is hidden rather than offered.

**Tests.** The suite runs as if inside the image: an autouse fixture in
`tests/conftest.py` (`_programs_as_in_the_image`) counts the paths the nine
tools above other than jwud (not jwud, which checked before, nor `zstd`) are configured with
at the start of each test as installed, at the probe's one seam
(`binaries._inspect`), and drops every cached answer around each test. Any
other path is probed for real, so a test of a missing or non-executable
program points the service's path at a file of its own in `tmp_path`
(`tests/test_binary_readiness_494.py`) rather than weakening the probe.

#### Saying why a tool can't run (`not_ready_message`)

`ToolPlugin.not_ready_reason` and `verify_not_ready_reason` say, in the user's
words, why `is_ready()` or `verify_is_ready()` answered False. Every caller
that tells someone a tool isn't ready builds the line with
`services.tools.not_ready_message(tool, purpose="convert" | "verify" | "info")`,
which returns `"<display_name> isn't ready: <reason>"` with the reason for that
purpose (#484). It takes `purpose` the way `tool_is_ready` does; `"info"` gives
`not_ready_reason`, the same as `"convert"`. The CLI's `--to` and `verify`
paths, Auto's plan (`platform_detect._plan_targets`), the Rules sweep's skip
line (§3.3.11), the Inbox's hold under a Rule whose tool isn't ready (§3.3.25,
which adds ", so the Rule can't run") and the File info routes' 503 (below) all
use it, so they say the same thing. A new caller does too, rather than
formatting its own.

It is a free function rather than a method so a tool that satisfies the
protocol without subclassing `BaseTool` still works. Read it after the
readiness check that answered False: rom-converto's `not_ready_reason` is a
property that returns what its last probe found. Reading a reason is an
attribute read, never I/O, so it is safe on the event loop; a plugin whose
reason needs I/O would have to compute it in its readiness probe. A reason
that raises, or is empty, gives `BaseTool.not_ready_reason` (with a warning
logged) instead of failing the caller.

**Job creation (#505).** `plan_job` (`routes/convert.py`), the one planner
behind `POST /api/jobs`, `POST /api/jobs/batch`, Auto and the CLI's batch,
asks `registry.tool_is_ready(registry.get(spec.tool_id))` before it reads the
source. A tool that answers False raises `SkipFile(SkipReason.TOOL_NOT_READY)`
whose detail is `not_ready_message(tool)`. The single-job route turns it into a
503 with that line, and nothing is queued. A batch skips the item with that
reason and the code `tool_not_ready` (`report_skipped=true`), the same path as
every other planning skip. A chain mode is refused when any step tool is not
ready, since `ChainTool.is_ready` asks each. The CLI, the Rules sweeps and the
Inbox already skip a not-ready tool before they plan, so they never reach this
check and nothing is reported twice. Per-route or per-tool checks are not
needed. `plan_job` and `prepare_batch` take `ready_tool_ids` (as `plan_auto`
does) to answer for the process that will run the job instead of probing
here: a `convert` that hands its jobs to a server routes and plans with the
server's `GET /api/tools` list (`Server.ready_tool_ids`, #533), so an NSZ
Image converts on a server that has the keys even when the CLI's container
lacks them, and the server's own planning has the last word.

**File info routes (#488).** Every `GET /api/*info` route in `routes/info.py`
(`/info` for chdman, `/dolphin-info`, `/z3ds-info`, `/nkit-info`, `/xiso-info`,
`/rom-converto-info`, `/cso-info`, `/romz-info`, `/jwud-info`, `/nsz-info`)
calls the one helper `_require_info_ready(tool_id)` after its 403/404/400
checks. It asks `registry.tool_is_ready(tool, purpose="info")` and answers 503
with `not_ready_message(tool, purpose="info")` when that is False, so the File
info modal can say the tool is missing rather than "No tool could read this
file". Whether the read needs the tool is the plugin's
`ToolPlugin.info_needs_tool`:

- `True` (`BaseTool`'s default): `info()` runs the program, so `purpose="info"`
  asks `is_ready()` under the registry's usual bound. chdman (`chdman info`),
  dolphin (`dolphin-tool header`) and rom-converto (`rom-converto xenon info`).
- `False`: `info()` reads the file in Python, so `purpose="info"` answers True
  without probing and the route answers whether or not the tool could convert
  here. z3ds, cso and nsz (size and extension), nkit (the NKit header), xiso
  (the XDVDFS tables), romz (the archive listing, through zipfile / py7zr) and
  jwud (the WUX header). nsz in particular never waits on its `prod.keys` walk
  for a File info read. makeps3iso (PARAM.SFO) and exodos (size and
  extension) read in Python too and say so, though neither has an info route
  today. `ChainTool` keeps the default: its `info()` is its last tool's.

A new tool sets `info_needs_tool = False` only when its `info()` never spawns
its program; leaving the default is the safe choice, since a ready tool's info
route answers as before.

chdman's route checks before it reads its metadata cache, so a CHD whose info
is cached is not shown while chdman isn't ready. chdman and dolphin declare
their programs (`required_binaries`, #494), so a missing or non-executable
`chdman` / `dolphin-tool` makes `/info` and `/dolphin-info` answer 503 with
that program's reason instead of a 500 from the failed run.

The SPA shows the 503's text as the server sent it. The shared client's
`fetchJson` (`src/lib/api/client.js`) throws an `Error` whose `message` is the
response's `detail` and whose `status` is the HTTP status, so a caller reads a
503 the same way for every tool. The File info modal's `loadInfo`
(`src/lib/util/fileInfo.js`) does this for the candidate tools' `getInfo`
bindings: when none answers and it falls back to the listing's basics, each
candidate that answered 503 has its `detail` added to the note under the
basics, beside the usual "No tool could read this file" line when another
candidate ran and failed (#486). No tool id is checked there; a tool's info
route opts in by answering 503 with `not_ready_message(tool)`, which every
info route now does through `_require_info_ready` when its read needs the tool.

#### Resolving what to verify (`_verify_target_bounded`)

`verify_target()` answers "which file actually holds this conversion's output"
— for makeps3iso, a stat of the bare ISO plus a scan for numbered split parts.
Cheap, and dangerously placed: it runs after the conversion and *before*
`verify_timeout()` is resolved, so it sits ahead of every bound the verify
itself carries, on storage that has just taken a multi-gigabyte write and is
therefore the likeliest thing in the process to have stopped answering.

Blocked there the job holds its output lock and its source-directory lock,
`_verifying` is still empty so the watchdog has nothing to report, and at the
default `MAX_CONCURRENT_JOBS` of 1 the whole queue stops. So it goes through
`run_detached` under `asyncio.wait_for` — an abandoned disposable thread rather
than a pooled worker — and takes the job's `cancel_event`, because a Cancel
pressed while a mount is silent must return now rather than after the probe
bound. An expired bound fails the job with a message naming the storage; a
cancel raises `ConversionCancelled`, the same translation the verify makes, so
nothing is judged and the source is not deleted on it.

#### A platform claim is a promise about extensions

`platform_slugs` and the modes' `input_extensions` are two halves of one
statement, and nothing enforces the join — so they can disagree, and the
disagreement is invisible until an operator hits it. ROMZ claimed NES, SNES,
N64, Genesis, Game Gear and a dozen more while accepting only `.gb`, `.gbc`,
`.gba` and `.nds`. RomM offered the ROMZ modes on an NES platform, every `.nes`
row was unselectable, and an enabled automation rule skipped the whole platform
as unconvertible on every sweep — silently, because "nothing to do" and
"nothing I *can* do" report the same way.

**Widen the extensions rather than dropping the slugs**, whenever the tool can
honestly serve them: ROMZ is a `7z` wrapper with no per-format logic, so a
`.nes` packs exactly as a `.gba` does. Dropping the claim would have been the
smaller diff and the worse answer — it removes a capability to make a doc
string true. And keep the two lists collision-free against other tools:
`.bin` stays chdman's, because a Genesis dump sharing an extension with a CD
track is not a collision worth creating.

The frontend mirrors these lists in `src/lib/tools/registry.js`, and
`tests/test_frontend_parity_186.py` fails when the two drift — so widening a
tool's inputs is a two-file change by construction.

#### What counts as a source (`registry.mode_input_kind`)

`ModeSpec.input_kinds` is a set, and "does this mode take a file or a
directory" was re-derived from it at each seam. **`mode_input_kind(mode)`** is
the one answer, used by the queue (which carries it end-to-end so the pipeline
skips the file-only assumptions), by the sweep's declaration check, and by the
sweep's existence check.

That last one matters because existing is not the same as being the right kind.
The declaration check is extensions and the tool's predicate, so a directory
whose *name* carries an accepted extension passes it, and a plain
`os.path.exists()` then let it into the queue. The manual batch route stats for
a regular file; automation has to as well, because nothing is watching an
unattended sweep — an `overwrite` rule authorises the job,
`_clear_existing_output` removes the previous artifact, and only then does the
converter fail on a directory it cannot open.

#### How a job ended, after the queue forgets (`add_terminal_listener`)

`JobManager.jobs` is capped (`max_job_history`) and lives in memory, so
`get_job(id)` answers "unknown" for anything pruned or predating a restart.
Any consumer that needs to know how a job *ended* — not whether it exists —
therefore cannot ask later; it has to be told at the time.

Ownership is registered the instant the queue accepts a batch (`_own_jobs`),
before the first await after acceptance — the listener fires from the queue
worker, so a fast conversion on an idle queue can end while the post-queue
bookkeeping is still running. The *record* it writes into does not exist until
a few awaits later, so a verdict arriving in between is stashed in
`_late_verdicts` and applied by `_mark_converted`. Dropping it instead left the
row with no verdict at all, after which the only evidence is the destination
having changed — which a *failed* overwrite produces just as convincingly.

**`job_manager.add_terminal_listener(callback)`** registers a callback for every
job reaching `COMPLETED` / `FAILED` / `CANCELLED`. Delivery is **at least once**,
not exactly once. It fires from
`_process_job`'s `finally` (every runner outcome), from both
`finish_external_job*` paths, from `cancel_job`'s QUEUED branch, and from the
four early returns that precede `_process_job`'s try block (two cancels, the
output-lock failure and the split-set collision) — which is why a job cancelled
before it starts is announced twice. The callback
may be sync or async, exceptions are logged and swallowed (a listener must
never fail a conversion), and the same job may be announced twice — listeners
are required to be idempotent.

The Rule engine is the first consumer: `rules.sweep.note_job_finished` writes
the verdict into the rule's converted-history record (`done: true|false`), and
`_was_produced` reads that first. Without it, provenance fell back to "the
destination changed since planning", which a *failed* overwrite produces just
as well as a success — by unlinking the old artifact or leaving a partial one
— and the ROM would be skipped by every later sweep until **Forget history**.
The sweep also freezes any outcome the queue can still answer for
(`_persist_known_outcomes`), covering the window before a record exists.

The listener fires for **every** job in the app, most of them manual, so it
cannot afford to search: `_mark_converted` files the `job id -> (platform,
rom)` pairing in `_job_owners` as it queues, and `note_job_finished` pops that
one entry. A miss means the job is not the automation's — a record can only
name a job this process queued, since the queue is in memory — so there is
nothing to write and nothing to scan. The alternative walked every remembered
ROM of every platform per completion, on the event loop under `_sweep_lock`.

#### A codec for "tool default" (`ToolPlugin.default_compression`)

The compression *level* is not a separate argument anywhere in the pipeline: it
rides on the compression string as `"codec:level"`, which the tool splits back
apart. A level with no codec therefore serialises as `":19"`, and whether that
is meaningful is a per-tool fact:

- Where a mode declares `supports_compression=False` but
  `supports_compression_level=True` — nsz, whose dropdown picks a solid/block
  *layout* rather than a codec — the tool reads the empty part as its own
  default and honours the level.
- Where a mode offers codecs (`supports_compression=True`), the empty part is a
  blank, not a default. `DolphinTool._build_convert_command` emits
  `-c "" -l 19`, which dolphin-tool refuses.

A mode can also need a codec when nothing at all was chosen: dolphin-tool
refuses RVZ and WIA without `-c`. Those modes declare
**`ModeSpec.requires_codec=True`**; a mode that works without one (chdman picks
its own best codecs) leaves it False and is sent nothing.

The codecs and the default can differ per mode of one tool: dolphin-tool
refuses zstd in a WIA container (`DolphinTool/ConvertCommand.cpp`), so one
tool-wide default cannot serve RVZ and WIA. **`ModeSpec.codecs`** lists the
codecs a mode takes (empty for no opinion) and **`ModeSpec.default_compression`**
its own default; the dolphin plugin fills both from its `CONTAINER_CODECS` and
`CONTAINER_DEFAULTS` tables (RVZ: zstd, as Dolphin's convert dialog selects;
WIA: none, the dialog's first entry, since it selects nothing for WIA). The
routes refuse a codec outside `codecs` (`_validate_request_compression`), and
the plugin refuses it again before its tool runs
(`refuse_unsupported_compression`), for a Rule saved before. Levels are per
codec too: **`ModeSpec.codec_levels`** holds each codec's `(min, max, default)`,
read through `ModeSpec.level_range(codec)`, from the plugin's `CODEC_LEVELS`
(Dolphin's `DiscIO/WIABlob.cpp` `GetAllowedCompressionLevels`: bzip2, lzma and
lzma2 1-9, zstd 1-22; zstd keeps this app's 19, the others Dolphin's dialog
default, 5). `compression_for` gives a codec with no level its default level;
an explicit level outside the range is clamped into it by the routes
(`base.clamp_levels`, #520: `zstd:40` on RVZ runs as `zstd:22`, for a single
job, a batch and a saved Auto preference), and one that reaches the plugin
some other way is refused there (`base.compression_problem`). The frontend mirrors both per mode in `registry.js`
(`compressionCodecs`, `defaultCompression` on the mode entry, read through
`registry.codecsForMode` / `defaultCompressionForMode`), and each codec entry's
`levelRange` (`registry.levelRangeFor(mode, codec)`), so the pickers never
offer or send a codec or level the mode refuses: the level control follows the
selected codec, and switching codec resets a level the new codec does not take
to its default. `test_frontend_parity_186` fails on drift.

chdman's create modes declare `codecs` the same way (#471), from chdman's own
rules (MAME `src/tools/chdman.cpp` `parse_compression`, `src/lib/util/chdcodec.cpp`):
every create mode takes the general codecs (none, zlib, zstd, lzma, huff, flac),
Create CD also the CD-frame codecs (cdzl, cdzs, cdlz, cdfl, which refuse a hunk
that is not whole 2448-byte frames, so createdvd, createhd and createraw fail
with "Codec error"), and Create LD also avhu (it encodes the A/V frames only
createld writes). Each mode's `implicit_codecs` is a subset of its `codecs`.
`copy` declares none: what it can use follows the input CHD. The multi-codec
picker (chdman's chips) keeps one selection per tool, so the store sends only
the codecs the current mode offers (`ConversionStore.compressionValue`), drops
the hidden ones on the next toggle, and the chip cap counts only offered codecs;
an Auto target whose saved preference the mode refuses runs on the tool default
(`checked_compression`).

Rules go through the same check (#474). A Rule submission (`PUT
/api/libraries/{id}/rules`, `PUT /api/romm/rules`, both `editor.save_rules`
with `set_rules(strict=True)`; an Inbox runs its Library's Rules) is refused
400 with `compression_problem`'s message when the Rule's mode refuses its
compression (`rules.refuse_unusable_compression`, raising
`UnusableCompression`), so nothing is saved that would fail once per Image. A
stored Rule is read leniently: `normalize_rule`, which every sweep and Inbox
plan reads its Rules through, keeps only the codecs the mode takes and records
the rest in the Rule's `dropped_codecs` (the editor names them, and the sweep that
runs the Rule logs it once, #477), so the job runs on what is left, or on the tool's own codecs when
nothing is. The drop is one shared helper, `base.without_unusable_codecs(spec,
compression)`, which `checked_compression` calls too, so an Auto target keeps
the saved codecs its mode takes before falling back. The frontend's matching
rule is `registry.offeredCodecs(mode, selection)`: the convert store sends,
and the Rule editor shows and counts in "(N selected)", only those codecs.

The convert panel keeps one level. `ConversionStore.#reconcileLevel` runs after
every mode, codec or saved-preference change, in that order: the codec is
`singleCodec` (the mode's default when the mode refuses the selected one), and
the level is kept when that codec takes it, else it becomes the codec's default
(`fitLevel`). A typed level is pulled into the range (`clampLevel`; a blank box
mid-edit is kept and sent as the default). The slider and the number box both
read `compressionLevel` and `ConversionStore.levelRange`, so they never
disagree. `fitLevel` and `clampLevel` live in `src/lib/tools/levels.js`, with no
imports, and the Rule editor uses the same two for its codec and level fields.

A level exists only for a codec that takes one (#363). The codec a job runs
with is its own, or the mode's `default_compression` when blank, and on a mode
with per-codec levels a codec with no range takes none (Dolphin's `none`, which
is WIA's default). **`ModeSpec.takes_no_level(codec)`** answers that on the
backend, and `registry.levelRangeFor(mode, codec)` answers it on the frontend
by returning null. Every consumer reads one of the two:

- the job routes refuse such a level with 400, through `compression_problem`,
  as they refuse an out-of-range one, and the plugin refuses it the same way
  before its tool runs;
- `compression_for` drops it from a job queued before (`"none:15"` runs as
  `"none"`) and logs it;
- `normalize_rule` drops it from what a Rule stores and runs, since the
  sanitizer is lenient with every stale value;
- the convert panel and the Rule editor show no level control when the range
  is null, and take the bounds and the "Blank uses" hint from that codec.

#### Block size (`ModeSpec.block_size`, `block_size_rule`)

dolphin-tool refuses GCZ, RVZ and WIA without `-b` ("Block size must be set for
GCZ/RVZ/WIA", `DolphinTool/ConvertCommand.cpp`). **`ModeSpec.block_size`** is
the block size a mode writes (None: the mode takes none), and
**`ModeSpec.block_size_rule`** is the rule one must meet, as
`(minimum, multiple_from)`: at least `minimum`, a power of two below
`multiple_from` (everywhere when it is None), and a multiple of `multiple_from`
from there up. `base.block_size_problem(spec, size)` checks it. The Dolphin
plugin fills both from `CONTAINER_BLOCK_SIZES`, which encodes
`DiscIO/DiscUtils.cpp` `IsDiscImageBlockSizeValid` (GCZ a power of two; WIA at
least 2 MiB and a multiple of it; RVZ at least 32 KiB, a power of two below
2 MiB, a multiple of 2 MiB above) and Dolphin's convert-dialog defaults (128 KiB
for GCZ and RVZ, 2 MiB for WIA). `DolphinTool.convert` passes the mode's block
size to the service, which adds `-b`, so every job, chain step and CLI run
(all through `run_convert`) gets it. No API, CLI or UI field takes a block size
today, so there is no explicit value to refuse; a future one would be checked
with `block_size_problem`.

**`ToolPlugin.default_compression: str | None`** names the codec to use in both
cases, so the resolution stays a per-tool declaration instead of an if-ladder on
tool identity at the call site. **`ToolPlugin.compression_for(mode,
compression)`** (`BaseTool` implements it) applies it, the mode's own default
first: a chosen codec is returned as it is; on a codec-offering mode,
`":level"` becomes `"<default>:level"` and nothing chosen becomes `"<default>"` when the mode
`requires_codec`; a mode without a codec picker keeps its string (nsz's
meaningful empty part survives); and with no declared default a level is
dropped and logged rather than send a job the tool will reject or invent a
codec the operator did not choose. `JobManager` calls it in
`_queue_job_locked`, where every conversion job is built, so Auto, a single
job, the CLI (`--compression default` included) and a Rule all run with the
same default (#345). The job records the resolved codec, so the Ledger shows
what ran and the re-run fast path compares a resolved request with a resolved
record.

It applies again at the convert boundary. **`ToolPlugin.run_convert`**
(`BaseTool` implements it) is `convert` with the compression passed through
`compression_for` first, and it is the only way callers start a conversion:
the job runner calls it, and so does each `ChainTool` step. A chain passes a
step the chain's own compression only when both advertise compression, so
`nkit_to_rvz` passes its RVZ step None; `run_convert` on that step resolves
`dolphin_rvz`'s default, with no chain-specific code. `compression_for` returns
a resolved value unchanged, so the job runner's second pass is a no-op. A tool
implements `convert` and receives the resolved value; nothing calls a tool's
`convert` directly. The Rules sweep (`_compression_arg`) passes the Rule's choice through
as it is; a Rule's delete decision (`_delete_decision_identity`) reads the
Rule's own fields, so it is unaffected.

The value mirrors `defaultCompression` in `src/lib/tools/registry.js`, which
seeds the same choice in the manual picker — `tests/test_frontend_parity_186.py`
fails on drift, and on a codec+level mode that declares no default at all.

#### Extension matching is a suffix match (`utils.path_utils.match_extension`)

Every "does this tool handle this file?" decision goes through one helper:

```python
def match_extension(name: str, extensions: Iterable[str]) -> str | None:
    """The longest declared extension ``name`` ends with, else None."""
```

It replaced the `Path(name).suffix.lower() in declared` test that used to be
re-typed at each site (`tools_for_input`, `tool_for_verify`,
`BaseTool.verifies_path`, the archive-member gate in `services/archive.py`, and
the plan-time input gate in `routes/convert.py`). The reason is **compound
extensions**: nkit2iso's sources are `.nkit.iso` and `.nkit.gcz`, whose trailing
component is the generic `.iso` / `.gcz` that CHDMAN, Dolphin and maxcso already
own. A single-component match can only see the generic tail, so the specific
format could not be declared without either over-claiming every ISO or
hard-coding a content sniff.

Three properties make this safe to apply everywhere:

- **Single-component declarations are unchanged.** `game.iso` ends with `.iso`
  and nothing else, so every pre-existing tool matches exactly what it did
  before. (`tests/test_compound_extension_matching.py` pins this.)
- **Per-tool, not global.** Each tool is asked independently, so an NKit image is
  claimed by nkit *and* by the generic-tail owners, the same way a raw `.iso` is
  already claimed by chdman, dolphin and cso. The user picks; nothing is
  displaced.
- **Longest match wins within a tool**, so a caller that needs the concrete key
  (the archive listing) gets the most specific answer rather than an arbitrary
  one.

The subject may be a full path, a bare filename, or an extension string
(`".nkit.iso"` still ends with `".iso"`), so a caller holding only a member's
recorded extension uses the same helper.

Two display-side sites deliberately still record the plain `Path.suffix`: the
archive listing's `entry["extension"]` and, downstream of it, the frontend icon
buckets. They want the generic tail. Convertibility for those rows is re-derived
from the member's *name* via `registry.tools_accepting_archive_member(member)`,
which takes a name-or-extension for exactly this reason.

**Compound *outputs* (xiso's `.xiso.iso`).** NKit only *reads* a compound
extension. xiso *writes* one, so the product's name also has to survive the
paths that resolve a finished file:

- **`registry.tool_for_verify`** picks the **longest** matching
  `verify_extensions` entry, with registration order breaking ties (sketch
  below). Under first-match, `Game.xiso.iso` resolved to Dolphin, which claims
  `.iso`, in the DAT-match path. Single-suffix paths resolve exactly as before.
- **The shared verify-route guards** in `routes/info.py` (`_guard_verify_path`
  for the single and SSE routes, and the batch route's path filter) test
  `match_extension(path, tool.verify_extensions)`. They used to test
  `os.path.splitext(path)[1]`, which only ever sees `.iso`, so every
  `.xiso.iso` was refused with a 400. `tests/test_verify_routes_factory.py`
  includes xiso in its per-tool table so all three routes are exercised with a
  compound name.
- **The rename duplicate policy** numbers the stem in front of the whole
  compound (`Game_1.xiso.iso`), as described in §3.3.8, so the renamed file
  still matches the verify claim.
- **The Extension column** shows `registry.displayExtension(entry)` (frontend),
  the longest declared compound extension the row's name ends with. This is
  display only: `FileEntry.extension` stays the trailing `.iso`, so sorting,
  DAT-match gating and the file icons are unchanged.

> **Ordering (issue #183):** the extension-union helpers return a **sorted
> `tuple`**, not a `frozenset`. Hash-seeded set iteration order varies across
> processes, so any consumer that serializes an extension list (or uses it as an
> ordered filter) would churn run-to-run; sorting at the seam makes every
> consumer deterministic for free. The few consumers that still need set algebra
> (`registry.scannable_extensions() - ARCHIVE_EXTENSIONS`, the archive
> listable/gate unions) wrap the result in `set(...)` / `frozenset(...)` locally.

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
            # Every await that follows the token lives inside this try, so the
            # finally is already registered when the first one runs: a client
            # disconnecting during tool.verify_timeout(path) still releases the
            # lane's only slot.
            bound = await tool.verify_timeout(path)          # issue #266
            r = await asyncio.wait_for(tool.verify(path), timeout=bound or None)
            # `valid` alone, so neither a timeout nor a cancelled run (which
            # reach no verdict) is ever recorded as a verification.
            if r.get("valid"): await verification_store.mark_verified(path)
            return r
        except asyncio.TimeoutError:
            return _verify_timed_out(bound)   # a verdict-shaped answer, not a 500
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

#### Tool availability (`is_ready` / `GET /api/tools`)

`GET /api/tools` splits `registry.all()` into `{"available", "unavailable"}` with
`registry.ready_tool_ids()`, which runs every tool's bounded `tool_is_ready`
probe at once; a probe that runs out of time counts as unavailable. `App.svelte` feeds the result to
`ui.applyToolAvailability()`, which hides unavailable tools everywhere (the
sidebar and dashboard both derive from `registry.all()` minus `ui.hiddenTools`,
and the active tool falls back to a visible one).

The readiness check belongs to the plugin, not the route. `BaseTool.is_ready`
probes the tool's `required_binaries` (§3.4); `NszTool` overrides it with a threadpooled
`keys_available()` because Switch content is encrypted and the operator must
supply their own `prod.keys` (§16 of `ADDING_PLATFORMS_AND_TOOLS.md`). The
route previously computed `nsz_service.keys_available()` itself and branched on
`tool.id == "nsz"`, so no *other* tool could ever be gated — a second
secret-dependent tool would have been advertised in the UI while being unable
to run.

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
the declared-extension match (`match_extension`, §3.4), so existing tools are
unaffected; `RomzTool`
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

#### Multi-file sources (`converts_path` / `source_companions`)

The two hooks above describe the *output* side per-file. `jwud` needed the same
treatment on the **input** side, because a split Wii U dump is one disc image
spread over twelve files: JNUSLib's `WUDDiscReaderSplitted` reads
`game_part1.wud` … `game_part12.wud` (11 parts of exactly 2 GiB plus a
1,402,994,688-byte twelfth) and joins them when you hand it part 1. Two shared
paths assumed a source is exactly one file, and both got it wrong for a set:

- **The listing** derived `convertible_by` from `ext in tool.input_extensions`,
  so all twelve parts were offered as sources. Eleven of them can only fail —
  a part on its own is not a disc image.
- **The delete planner** (`utils/delete_plan.py`) collected the source plus, for
  a `.cue`/`.gdi`, its parsed track files. A verified conversion with
  delete-on-verify would therefore remove the part the job named and orphan the
  other eleven — ~23 GB of silent leftovers.

`ToolPlugin.converts_path(path) -> bool` is the input-side mirror of
`verifies_path`: `BaseTool` defaults it to the plain `input_extensions` match
(so the seven pre-existing tools are unchanged), and `JwudTool` returns `False`
for a secondary part that has its primary beside it. The registry exposes
`tools_converting_path(path)` next to `tools_verifying_path`, and
`routes/files.py` feeds it into the same tool-neutral `FileEntry.convertible_by`
list — so the secondary parts stay **visible** (you can still see and delete
them) but are never offered for conversion, the same shape as the
visible-but-not-convertible archive members in §17.5 of the adding-a-tool guide.

`ToolPlugin.convert_refusal(path) -> str | None` (#476) says *why*
`converts_path` answered `False`, in the user's words, for a tool that can name
what it found. `BaseTool` returns `None`. `plan_job` asks it only after
`converts_path` refused, and `SkipFile(reason, detail=...)` carries the answer:
the single-job 4xx and the batch's skipped list (`?report_skipped=true`, one
`{path, reason, code}` per file) show it in place of the
reason's fixed `_SKIP_HTTP` message. The CLI's `format_route` (`--to <mode>`)
says it too in place of "<mode> does not read .ext files". The rar tool is the
user: its
`converts_path` is `convert_refusal(path) is None`, and the refusal names the
files a RAR set holds when it holds more than one, or says it is encrypted, a
later volume, or missing a volume.

`ToolPlugin.source_companions(path) -> list[str]` is the input-side mirror of
`companion_outputs`: the sibling files this input also consumes, never including
`path` itself, `[]` by default. `build_delete_plan` adds
`registry.source_companions(source)` to its delete set, so removing a split
source takes the whole set. A gap in the set truncates it — JNUSLib reads the
parts in order, so a set missing part 3 is broken rather than an 11-part set,
and the stragglers must not be reported as belonging to a usable source.

Both hooks may touch the disk (they look for the sibling primary / enumerate the
set), so they run in the `files.py` threadpool scan and inside
`build_delete_plan`, never on the event loop.

A set also costs two things a single-file source never did, and both are
registry-driven rather than jwud branches:

- **Containment.** `source_companions` is *name*-derived, so a companion can be
  whatever the filesystem puts under that name. The converter opens them itself
  — JNUSLib enumerates the parts from part 1 rather than taking a list from us —
  so the volume boundary the route enforces on the submitted path has to be
  enforced on the companions too. `utils.path_utils.source_companions_are_safe`
  rejects a symlinked companion outright (rather than following it) and any
  companion resolving outside the configured volumes.

  Two properties make it correct, and both were review findings:

  * **Checked twice, like the directory case.** `plan_job` rejects at queue time
    with `SOURCE_COMPANION_UNSAFE`, and `_process_job` re-checks after taking
    the job's locks — a queued job's `game_part2.wud` can be swapped for a
    symlink in between, and the plan-time answer would be stale. The re-check
    runs *before* clearing any existing output, so a rejection on an overwrite
    job leaves the user's prior file intact. This is the same shape (and the
    same ordering rationale) as the `is_safe_directory_tree` re-check that
    guards makeps3iso's recursive read.
  * **Scoped to the mode's own tool**, not the registry-wide union that
    `build_delete_plan` uses. Deletion should take everything any tool considers
    part of the source; conversion should consult only the tool that will read
    it, or a chdman `createhd` on a raw file that happens to be named
    `game_part1.wud` would be rejected over a sibling chdman never opens. A
    no-op for the tools whose `source_companions` is `[]`.
- **Scan cost.** `.wud` is a produced output, so `scannable_extensions` puts
  every `game_partN.wud` in the DAT-match walk — twelve 2 GiB files SHA1'd in
  full, ~25 GB per set, to match nothing, because a part is a slice of a disc
  image rather than one. `ToolPlugin.scannable_path(path) -> bool` (default
  `True`) is the per-file veto on that walk, the scan-side analogue of
  `converts_path`; the registry drops a path any tool rejects, since a file some
  tool knows is not a standalone artifact cannot match a DAT whatever the others
  think. It may do *bounded* disk I/O: the veto is on membership of a real set
  rather than on the name, so a lone `game_part1.wud` stays scannable and the
  same path stops being scannable once its siblings appear — which a pure
  filename check could not express, and which keeps the hook agreeing with
  `converts_path` and `output_stem`. It runs once per candidate across the whole
  library, so implementations stay to a stat or two and never a read, inside the
  threadpool scan walk rather than on the event loop.

**Naming the set's product is a third disk-reading decision.** Part 1 of a set
produces `game.wux`, not `game_part1.wux` — the parts are one image and the part
number is meaningless once they're joined. But that rewrite is only correct when
the set can actually *be* joined, so `JwudToolService.output_stem` gates it on
`split_set_is_complete()`: parts 1…N present with no gap **and** summing to
exactly `WUD_IMAGE_SIZE`, which is what JNUSLib itself requires. A half-finished
dump, a set with a hole in it, an orphan part, and an archive member (whose
synthesised path has no set behind it on disk) all keep their own stem. The
reason is `allow_overwrite`: an input that claims `game.wux` but cannot produce
it would let the worker unlink an unrelated finished image *before* JWUDTool
failed.

Disk state answers that for a real path, but an **archive member's path is
synthesised** — the location it would occupy once extracted — so reading its
directory reads someone else's files. `ToolPlugin.detect_output` therefore takes
`from_archive` (default `False`), the listing-side counterpart of the
`treat_as_stem` `plan_job` already passes for the same member;
`_detect_archive_member_outputs` sets it and `JwudTool` forwards it to
`output_path`. The other seven tools ignore it, because they swap a suffix and a
synthesised path resolves like a real one. It lives on the seam rather than in
jwud because the property is general: extraction hands over one member and never
its siblings, so a member can only ever produce a single-file output — a future
tool whose output name depends on its input's neighbours would otherwise inherit
the wrong answer silently. Without it, an archive holding `game_part1.wud` next
to a genuinely extracted set badges the set's `game.wux` while the planner
targets `game_part1.wux`.

#### Content-refined sources (xiso: `converts_path` reads the file)

`converts_path` was added to tell a split set's parts apart by *name*. xiso
uses the same hook to tell files apart by *content*: every Xbox / Xbox 360 disc
image is a plain `.iso`, which chdman, Dolphin and cso also claim, so the
extension can't say whether a file is an Xbox disc. `XisoTool.converts_path`
runs a header probe (`ExtractXisoService.is_convertible`): at most five small
reads, looking for the XDVDFS volume descriptor at the partition offsets
extract-xiso itself checks, and rejecting an image that already carries
extract-xiso's rewrite tag. The result is `lru_cache`d per (path, size, mtime),
so the listing scan and `plan_job` don't re-read an unchanged file. Because the
listing derives `FileEntry.convertible_by` from `tools_converting_path`, xiso
shows up there only for real Xbox images, and PSP/PS2/CD `.iso` rows are
unchanged.

Three registry-driven seams carry that claim the rest of the way, with no
`xiso` branch in shared code:

- **The skip reason (`routes/convert.py::_NOT_CONVERTIBLE_REASON`).** When the
  mode's tool rejects a file in `plan_job`'s `converts_path` gate, the reason
  defaults to `SOURCE_NOT_INDEPENDENTLY_CONVERTIBLE`, which describes jwud's
  split sets. A tool that refines on content maps its id to its own
  `SkipReason` here (xiso: `XISO_NOT_XBOX_IMAGE`), so the user is told the file
  isn't an Xbox disc instead of being told it is one part of a set. Adding a
  reason is a data row, not a branch.
  `tests/test_single_batch_plan_parity.py::test_converts_path_rejection_names_the_tools_reason`
  pins both the mapped reason and the default.
- **The descriptor flag (`contentRefined: true` in `registry.js`).** It marks a
  tool whose extension claim means nothing until the backend's probe agreed.
- **One reader of that claim (`registry.contentClaimsFor(entry)`).** It returns
  the content-refined tools named in the row's `convertible_by`, or `null` when
  the row has no `convertible_by` at all ("unknown", not "no"). It has two
  consumers:
  - The selection gate, `ConversionStore.allowsInputEntry`, narrows a
    content-refined mode to rows that claim the tool. This is the one place
    that gate reads `convertible_by` for a file row: elsewhere that list is
    mode-agnostic and would block CHDMAN extract/copy, but a content-refined
    tool has no such modes. Without it, every PSP/PS2 `.iso` could be ticked
    (and caught by select-all) in Xbox mode, only to be rejected on submit.
  - The Info modal tries these tools before the extension owners from
    `infoToolsForPath`, because the generic `.iso` owners would otherwise
    describe an Xbox disc as an ordinary ISO.

The `/xiso-info` route returns 422 for an image without an XDVDFS volume, so on
a non-Xbox `.iso` the modal moves on to the next candidate. Archive input is
deliberately not offered: the probe needs the real file, and inflating a 7–8 GB
image only to look inside isn't worth it.

#### Per-job delete-on-verify guard (`delete_on_verify_is_safe`)

`ModeSpec.supports_delete_on_verify` answers "can this *mode* offer it?", which
was enough while every tool's `verify()` read the whole output. jwud breaks that
assumption: WUX carries no content checksums, so its verify walks the container's
structure, and what actually justifies deleting a 25 GB source is JWUDTool's
byte-for-byte comparison *during* the conversion — which the job can switch off
with `-noVerify`. The two settings together would delete the only copy of a disc
image on the strength of a geometry check.

`ToolPlugin.delete_on_verify_is_safe(mode, compression)` answers the narrower
"can this *job* offer it?". `BaseTool` returns `True`, so every other tool is
unchanged; `JwudTool` returns whether the job kept verification on.
`routes/convert.py::_validate_request_compression` — already the shared
request-level validator for both the single and batch endpoints — rejects the
unsafe combination with a 400, so the two paths can't drift.

It lives on the plugin rather than as a `spec.tool_id == "jwud"` branch for the
same reason `is_ready` did: a second tool whose verify is weaker than its
conversion-time check would otherwise silently inherit "safe".

> **Not yet folded in: `.cue`/`.gdi` tracks.** `build_delete_plan` still parses
> those out of the file's *contents*, with its own unsafe-reference handling
> (absolute paths, refs escaping the source directory) that the naming-derived
> hook has no equivalent for. Routing chdman's track files through
> `source_companions` is the obvious next consolidation, but it would move that
> security surface — and the `unsafe_paths` wire strings tests pin — so it is
> deliberately left for its own change.
#### Source-tool ordering is most-specific-first

`registry.toolsForSourcePath` / `infoToolsForPath` rank claiming tools by the
**length of the matched `sourceExts` entry**, not declaration order. The Info
modal walks that list and keeps the first `getInfo()` that returns, so with a
plain order a `.nkit.iso` would reach Dolphin's header reader first — and that
reader *succeeds*, because an NKit image carries a genuine GC/Wii disc header,
reporting the shrunk file as if it were an ordinary disc. Ranking the compound
claim above the generic tail it shares (`.iso`) puts the tool that actually
understands the container first. This mirrors the backend's longest-match
`match_extension` (§3.4).

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

**Parity guard (`tests/test_frontend_parity_186.py`).** `registry.js` is a
hand-maintained mirror of the backend `ModeSpec`s, and there is no shared build
step, so a drift in `allows_archive_input` / `supports_delete_on_verify` /
`input_extensions` / `output_ext` / `kind` would silently make the UI offer a
job `plan_job` rejects ("0 queued") or fail on submit. The test evaluates
`registry.js` with Node and asserts its mode rows match `registry.mode_specs()`
field-by-field (skipped when Node is unavailable). `tool_id` is excluded: the
synthetic `cso_to_chd` chain mode is owned by the `chain` tool on the backend
but grouped under `cso` in the UI. The deliberate `.iso`-not-in-Dolphin-verify
divergence lives on `verifyExts` (a verify-routing fact), not on a `ModeSpec`
field, so it is outside the guard's scope.

**Registry-derived frontend fact lists (issue #186, site 3).** Two presentation
surfaces used to re-type extension/mode facts the registry already owns, so a
new tool or mode silently missed them:

- `src/lib/util/fileIcon.js` builds its disc/game extension buckets from
  `registry.all()` via a small `TOOL_MEDIA` map — the one presentation fact the
  registry can't express (whether a tool's media reads as an optical disc or a
  cartridge/handheld "game"). `.chd` keeps its own Disc3 glyph and the
  handheld-ROM archives (`.7z`/`.zip`) get the archive glyph.

A Node-evaluated guard, `tests/test_frontend_registry_derives_186.py`, fails if
it drifts: a filterable extension with no icon bucket, or an unclassified tool.
(Help used to carry a per-mode table built from the registry; the restructure
moved that detail to the README, and the Convert-to list below is now the
surface that must cover every mode.)

The same Node harness also pins the behaviour of the registry's own query
helpers: `infoToolsForPath` ordering, `displayExtension`, and
`contentClaimsFor`. A new shared helper on `registry` gets its test here, next
to the facts it reads. A new *tool* still needs no edit to this file.

**Shared pieces on top of the registry (restructure, #534).** Every picker reads
these instead of keeping its own copy, so a new tool or mode reaches them all
with no edit:

- `src/lib/tools/compression.js` owns the compression wire format. A choice is
  `{ codecs: string[], level: number|null }`; `formatCompression(mode, value)`
  writes the string a job is sent (`"zlib,lzma"`, `"zstd:19"`, `"none"`, or
  null for the tool's own default, sending only the codecs the mode offers,
  #471) and `parseCompression` reads it back. `fromRule` / `toRule` map a Rule's
  `compression` + `compression_level`, `defaultCompression` / `isDefault` drive
  Reset, and `toggleCodec` / `withCodec` / `withLevel` are the picker's edits
  (the codec cap, a level pulled into the codec's own range). Tested by
  `tests/test_frontend_compression_util.py`.
- `CompressionPicker.svelte` is controlled: `{ mode, value, onchange, compact,
  label }`. It reads only the registry and the compat table, never a store. The
  convert store exposes `compressionChoice` / `setCompressionChoice(v)` as thin
  wrappers (its `compressionValue` stays the wire string a job is sent).
- `src/lib/tools/convertTargets.js` groups every mode for a "Convert to"
  select: *Recommended* (best format for each game), *Make smaller* (one option
  per output format, or per mode with `perMode`), *Unpack* (`kind: 'extract'`)
  and *Other* (`kind: 'copy'` plus `OTHER_MODES`, which change a container
  without shrinking it). It narrows by the games at hand (`entries`, by
  `inputExtensions` or a folder mode's `convertible_by`), a console's
  `modeIds` / `toolIds`, and the `ready` tools. `PLAIN_MODE_LABELS` names the
  extract and other modes by what comes out ("BIN/CUE from CHD"). Its guard,
  `tests/test_frontend_convert_targets.py`, fails when a mode lands in no group
  or in two.
- `src/lib/util/safetyOptions.js` (drawn by `SafetyOptions.svelte`) decides
  which safety choices a set of mode specs offers: check each new file, delete
  the original (only where `supportsDeleteOnVerify`; the rest are named as
  kept, with `sourceKeptReason`), and the quick check (only where a Round-trip
  runs and isn't required). Deleting forces the check on.
- `FolderPicker.svelte` / `FolderPickerModal.svelte` pick a folder by walking
  the Drives (`/api/volumes`, then `/api/files`), never through a typed path.
- The defaults store (`src/lib/stores/defaults.svelte.js`, `GET`/`PUT
  /api/preferences/defaults`, model `DefaultsPreferences`) holds the safety
  choices new conversions and new console settings start with. Nothing stored
  answers the safe defaults: check on, delete off, full check.
- Routes live in one pure file, `src/lib/router/routes.js`: `VIEWS`,
  `VIEW_TITLES`, `parseHash`, `hashFor` and `legacyRedirect`, which moves every
  old route (`#/workspace/<tool>`, `#/rules/<id>`, `#/findings`, …) to its new
  home. The router applies a redirect with `history.replaceState`, so Back
  never bounces. `tests/test_frontend_routes_8.py` checks every row.

---

## 4. The payoff: what a new tool looks like *after*

Before: ~20 files (see `ADDING_PLATFORMS_AND_TOOLS.md` §3). After:

1. `app/services/tools/nszip.py`, one `BaseTool` subclass with `modes`,
   `_build_command`, `_parse_progress`, `verify_stream`, `info`, `info_model`.
2. `app/services/tools/__init__.py`, one `registry.register(NszipTool(...))`.
3. `app/config.py`, one `nszip_path` Field.
4. `src/lib/tools/registry.js`, one entry in `TOOLS`.
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
- Disc-ID embed now lives in `ChdmanTool.post_convert`; `_process_job` calls
  `registry.for_mode(mode).post_convert(...)` generically (default no-op) and
  `ChainTool` routes its final step through the same hook (done, #181).
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
- ~~Remove the now-unused legacy `FileEntry` booleans once the frontend reads
  `outputs` exclusively.~~ **Done (issue #186, site 6).** The ~23 legacy
  per-tool flags (`has_*` / `*_ready` / `*_convertible` / `*_path` and the bare
  `convertible`) are gone from `FileEntry`, the `_legacy_output_fields` helper
  and the per-archive `has_chd` special-case are removed from `routes/files.py`,
  and the two remaining frontend fallbacks (`FileRow` `convertibleBy`,
  `fileBrowser` `has_chd` merge) now read `convertible_by` / `outputs` /
  `verifiable_by` exclusively. The per-archive "converted" badge derives from
  the registry-driven `archive_has_output` count.
- The verify-route factory (`routes/info.py`) is already registry-driven with no
  per-tool branching; its six explicit `register_verify_routes(...)` calls are
  retained deliberately to preserve the module-level `verify_*` handler names the
  route tests call directly. Folding them into a `registry.all()` loop would
  require moving the per-tool prefix/detail strings onto the plugin contract and
  rewriting those tests, for no behavior change, so it stays as-is.
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

## Compatibility presets (`app/data/compat_presets.yaml`)

"Compatible with…" is data, not code. `app/data/compat_presets.yaml` maps each
target emulator to `entries`: a `mode`, the `codecs` it reads for that mode,
the `preferred` ones the picker selects, and a mandatory https `source` (the
doc or source file the claim rests on; an entry that can't be cited is left
out). `services/compat_presets.py` loads and validates it (a malformed file
raises, it is never served half-read) and `GET /api/compat-presets` returns it
unchanged. The frontend holds no copy: `stores/compat.svelte.js` fetches it
once, and `tools/compat.js` holds the two pure rules both surfaces share
(`compatTargetsForMode`, `compatWarning`). `CompressionPicker.svelte` and
`RuleEditor.svelte` stay descriptor-driven; they offer only the targets with an
entry for the current mode, set `preferred` on choosing one, and show the
warning for a codec outside `codecs`. The warning is advisory: nothing is
disabled and a Rule still saves and runs. A mode a target has no entry for gives
no opinion. An empty selection is not "nothing": chdman then sends no `-c` and uses its own
codecs, so `ModeSpec.implicit_codecs` names them once, `GET /api/compat-presets` serves them as
`implicit_codecs`, and `compatWarning` checks those instead. A mode whose defaults follow the input (chdman's
`copy`) sets `ModeSpec.implicit_codecs_unknown`; the warning then says compatibility can't be
confirmed and suggests choosing codecs. A test fails if a multi-codec compressing mode declares neither. `tests/test_compat_presets_429.py` checks every mode and codec in
the file against the tool registry (backend `ModeSpec.codecs` and the
frontend `registry.js` codec lists), so a rename breaks a test, not the picker.

## Ignored entries (`utils/junk.py`)

`is_archive_sidecar(path)` is the wider predicate for an archive member that is
never the archive's payload: junk, or a release sidecar (`.nfo`, `.sfv`,
`.txt`, `.url`, `RELEASE_SIDECAR_SUFFIXES`) that usenet and scene sets ship
beside the file (#476). Those are real files a user may want to see, so they
stay out of `is_junk_entry` and keep listing. The rar tool counts a set's
payload without them.

`is_junk_entry(name)` is the one predicate for names no listing or walk should
see: OS and NAS metadata, plus RomM's `.romm_tmp_*` converter staging (prefix
match only) and this app's own `.compressatorium-stage` folder
(`STAGE_DIR_NAME`, section 3.3.21). The file browser listing and search, `walk_tree` (DAT scan, the duplicate finder's sheet walk), the
folder Library walk (which feeds sweeps, check Rules and the Platform walk) and
the Switch key search all route through it. Add a new ignored name there, not in
a caller.

## DAT import and Redump sync (`services/dat_import.py`, #524)

One path imports DATs for the batch upload (`POST /dat/import-batch`), the
Redump sync (`POST /dat/redump/sync`) and the CLI (`dat import`, `dat sync`):

- `import_files([(label, path)])` takes `.dat`/`.xml` files and `.zip` files
  of them. `extract_dats` reads a zip under the Inbox gate
  (`ArchiveService.check_inbox_source`: entries, declared sizes, links,
  absolute and `..` paths, expansion ratio) and the extract path's name rules
  (`member_segments`), counts the bytes it reads with `_UnpackBudget`, skips
  `is_junk_path` members, and copies each DAT to `<n>.dat` in a private temp
  folder, so a member name never reaches the disk. Any bad member refuses the
  whole zip, and so does data that can't be read (corrupt deflate data,
  `zlib.error`), never the rest of the batch (PR #527).
- Each DAT is imported with `dat_store.import_dat(..., replace=True)`: an
  earlier DAT with the same name and family is deleted in the same
  transaction (its rank carries over). One rematch runs after the batch
  (local-only, `routes.dat.rematch_after_dat_change`). The CLI has no server
  to finish it later, so it waits for it (`routes.dat.wait_for_match_jobs`),
  and `dat sync` reads RomM's saved settings (`romm_settings.load`) before it
  asks which consoles are present (PR #527).
- `sync_redump` maps Platform slugs (`REDUMP_SYSTEMS`) to Redump's systems,
  asks `redump.org/datfile/<system>/` through `http.client` on the fixed host
  (`_connect_redump`). Verified HTTPS comes first (`HTTPSConnection` with
  `ssl.create_default_context()`, so the certificate and host name are
  checked). redump.org serves no HTTPS, so when that connection can't be made
  at all (refused, unreachable, timed out) the same path is asked over plain
  `HTTPConnection`: the owner's choice (#556), superseding the HTTPS-only rule
  from Codex on PR #527. A certificate or host-name failure (`ssl.SSLError`,
  `ssl.CertificateError`) never falls back; nor does a connection that drops
  after it was made. Both transports send the same headers, follow no
  redirect, use the generic `USER_AGENT`, and are held to the same
  `save_capped`/`MAX_ZIP_SIZE` cap; the zip then goes through every archive
  and DAT check below unchanged. `redump_url(system, scheme)` names the URL
  actually used, and a failure says which transports were tried. Plain HTTP
  is allowed for redump.org only. One system at a time with
  `REQUEST_GAP_SECONDS` between requests, at most one sync at once. The cache
  (preference `redump_sync`) skips a system synced within `FRESH_FOR` whose
  DATs are still installed, sends `If-None-Match`/`If-Modified-Since` when it
  has them, and skips re-importing an identical download (SHA-256). "Still
  installed" is by DAT id (`dat_ids`), not name: a DAT of the same name and
  family imported by hand replaces the synced one under a new id, and the
  next sync then asks again, unconditionally, and re-imports (PR #527). The
  answer's `changed` says some DAT went in, even for a system that failed
  part-way; the UI refreshes on it.
- A loose `.dat`/`.xml` over `MAX_DAT_SIZE` is refused before it is parsed,
  for the CLI and Redump's bare-DAT answer as much as the upload, and a file
  that can't be read (`OSError`, such as `PermissionError`) is that file's
  error, never the batch's (PR #527).
- `POST /dat/import-batch` reads its body with
  `upload_stream.file_uploads`, the many-files form of `file_upload`: the
  whole body is capped at `MAX_BATCH_SIZE` while it arrives (413 by its
  `Content-Length`, or as it comes), and each file at its own cap as it is
  written (PR #527).
- `dat_sync.save_capped` is the capped download loop both syncs share.
- The MAMERedump sync follows the same replace rule (#525): after a full
  success it deletes only the existing DATs whose name and family match a DAT
  it just staged, never DATs from another source.
