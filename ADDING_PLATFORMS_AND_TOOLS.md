# Adding Platforms and Compression/Decompression Tools

This is a developer guide for extending Compressatorium with **new conversion
tools** (e.g. a brand-new compressor binary) and **new platforms / formats**
(e.g. a new chdman create mode, or a new file type a tool can handle). It
walks the full vertical slice — from the binary in the Docker image, through
the Python service and job pipeline, the FastAPI routes, and finally the
Preact web UI — and shows how to add a tool and a platform *in tandem*.

It is written against the codebase as it stands today, with three tools
already wired up:

| Tool | Binary | Service module | Handles |
|------|--------|----------------|---------|
| **chdman** | `mame-tools` (`/usr/bin/chdman`) | `app/services/chdman.py` | CD/DVD/HD/Raw/LaserDisc disc images ↔ `.chd` |
| **dolphin-tool** | `dolphin-emu` (`/usr/local/bin/dolphin-tool`) | `app/services/dolphin_tool.py` | GameCube/Wii images ↔ `.rvz/.wia/.gcz/.iso` |
| **z3ds_compressor** | built from source (`/usr/local/bin/z3ds_compressor`) | `app/services/z3ds_compress.py` | Nintendo 3DS ROMs → `.zcci/.zcia/.z3ds` |

`z3ds_compress` is the cleanest, most self-contained example of "a new tool
that handles a new platform," so this guide uses it as the reference
implementation throughout. When in doubt, **copy what z3ds does.**

---

## 1. Architecture overview

A conversion request flows through these layers. Adding a tool/platform means
touching the same layers in the same order:

```
            ┌─────────────────────────── Web UI (static/js) ──────────────────────────┐
            │  app.js: tool selector, MODE_GROUPS, filters, info modal, verify routing │
            │  api.js: HTTP/SSE client methods                                          │
            └───────────────────────────────────┬───────────────────────────────────────┘
                                                 │  POST /api/jobs   (mode=...)
            ┌────────────────────────────────────▼──────────────────────────────────────┐
            │  Routes (app/routes)                                                        │
            │   convert.py  – validate mode/extension, compute output path, enqueue job   │
            │   files.py    – mark files convertible in directory listings + search       │
            │   info.py     – per-tool info + verify (single + batch + SSE) endpoints      │
            └────────────────────────────────────┬──────────────────────────────────────┘
                                                  │  job_manager.create_job(mode=...)
            ┌─────────────────────────────────────▼─────────────────────────────────────┐
            │  Job pipeline (app/services/job_manager.py)                                 │
            │   _queue_job_locked  – pick output path                                      │
            │   _process_job       – pick service, stream convert(), optional verify+delete│
            └─────────────────────────────────────┬─────────────────────────────────────┘
                                                   │  service.convert(...) async generator
            ┌──────────────────────────────────────▼────────────────────────────────────┐
            │  Tool service (app/services/<tool>.py)                                      │
            │   spawns the binary, parses progress, yields {progress, message}            │
            └──────────────────────────────────────┬────────────────────────────────────┘
                                                    │  subprocess
            ┌────────────────────────────────────────▼───────────────────────────────────┐
            │  Binary in the image (Dockerfile) + path in app/config.py                    │
            └──────────────────────────────────────────────────────────────────────────────┘
```

Two supporting layers:

- **`app/models.py`** — `ConversionMode` enum (every mode string lives here)
  and the Pydantic info models (`CHDInfo`, `DolphinDiscInfo`, `Z3DSInfo`).
- **`app/config.py`** — `Settings` holds the binary path for each tool
  (`chdman_path`, `dolphin_tool_path`, `z3ds_compressor_path`).

### The tool-service contract

The job pipeline talks to every tool through a small duck-typed interface.
A new service class must provide:

| Member | Signature | Purpose |
|--------|-----------|---------|
| `convert` | `async def convert(self, input_path, output_path, mode="...", *, compression=None, cancel_event=None) -> AsyncGenerator[dict, None]` | Spawn the binary, `yield {"progress": int, "message": str}`, raise on failure, `yield {"progress": 100, ...}` at the end. Must raise `ConversionCancelled` when `cancel_event` fires. |
| `verify` | `async def verify(self, path) -> {"valid": bool, "message": str}` | Deep integrity check of an output file (used by delete-on-verify and the Verify button). |
| `verify_stream` | `async def verify_stream(self, path) -> AsyncGenerator[dict, None]` | Streaming variant yielding `{"type": "progress"/"complete"/"error", ...}`. `verify()` is normally just a wrapper over this. |
| `info` | `def info(self, path) -> dict` *(or async)* | Metadata for the info modal. |
| `is_convertible` | `@staticmethod (filename) -> bool` | Whether a filename is an input for this tool. |
| `get_output_path_for_mode` | `@staticmethod (mode, input_path, output_dir=None, *, treat_as_stem=False) -> str` | Compute the output path for a mode. |
| `active_pids` | `() -> list[int]` | Optional but recommended; used by the debug heartbeat. |

Each service module also exposes, at module scope:

- a `*_CONVERTIBLE_EXTENSIONS` set (input extensions),
- usually a `*_OUTPUT_FORMATS` map (input ext → output ext, or mode → output),
- a **module-level singleton instance** (e.g. `z3ds_compress_service = Z3DSCompressService()`).

`ConversionCancelled` is defined once in `app/services/chdman.py:22` and
imported by the other services — reuse it, don't define your own.

---

## 2. Two kinds of extension

### Scenario A — a new platform on an *existing* tool

This is the lightweight case: the binary already ships and the service class
already exists; you just need a new mode string or a new input extension.

Examples:
- A new chdman create variant (chdman already supports `createcd`,
  `createdvd`, …). You add a `ConversionMode`, teach
  `chdman.get_output_path_for_mode` the new suffix, add validation in
  `convert.py`, and surface it in the UI `MODE_GROUPS`.
- Letting chdman accept a new input extension: add it to
  `CHDMAN_CONVERTIBLE_EXTENSIONS` in `app/services/chdman.py:16`.

Go to **§4**.

### Scenario B — a brand-new tool (and usually a new platform with it)

A new binary, a new service module, a new `ConversionMode`, new info/verify
endpoints, and new UI wiring. This is the z3ds-shaped case.

Go to **§5**.

### Scenario C — a tool and a platform in tandem

This is the common real-world request: "support platform X, which needs new
binary Y." It is just **Scenario B**, because adding the tool *is* what makes
the platform reachable. The platform shows up as:

- the input extensions in `*_CONVERTIBLE_EXTENSIONS`,
- the `ConversionMode` value(s) the tool exposes,
- a UI entry in `MODE_GROUPS` plus a primary-tool option,
- (optionally) platform-specific flags inside `_build_command`.

Follow §5 end to end. §6 is the concrete tandem checklist.

---

## 3. Inventory: every file a tool/platform touches

This is the **exhaustive** list. Not every item is required for every change
(the right-hand column says when it applies), but check every row. The order
is the recommended implementation order (bottom of the stack → top). Deep
detail for the non-obvious rows is in §8–§14.

### 3.1 Binary / packaging

| # | File | What you do | When |
|---|------|-------------|------|
| 1 | `Dockerfile` | Build/install the binary into the image (builder stage + `COPY`, or apt install); add runtime shared libs; multi-arch (amd64+arm64). | New tool |
| 2 | `.dockerignore` | Verify the binary/source you reference isn't excluded. `app/`, `static/`, `migrations/` are copied; `tests/` and most `*.md` are excluded. | New tool (check only) |
| 3 | `requirements.txt` | Add any new **Python** runtime dependency the service imports. | If service needs a new pip dep |
| 4 | `requirements-dev.txt` | Add test-only Python deps. | Rare |
| 5 | `.local-bin/` | Local-dev copy of a from-source binary (z3ds lives here for `run_dev.sh`). Add yours so the app runs outside Docker. | New from-source tool |
| 6 | `entrypoint.sh` | Add the binary path env passthrough; extend the **CLI batch mode** loop if the tool should run headless (see §9). | New tool, CLI support optional |

### 3.2 Backend code

| # | File | What you do | When |
|---|------|-------------|------|
| 7 | `app/config.py` | Add a `<tool>_path` `Field` with an env alias (`<TOOL>_PATH`). | New tool |
| 8 | `app/services/<tool>.py` | New service module — `convert`/`verify`/`verify_stream`/`info`/`is_convertible`/`get_output_path_for_mode`/`active_pids`, the `*_CONVERTIBLE_EXTENSIONS` set, `*_OUTPUT_FORMATS`, and the module singleton. | New tool |
| 9 | `app/models.py` | Add `ConversionMode` value(s); add a `<Tool>Info` model; add `FileEntry` flags (`<tool>_convertible`, `has_<tool>`, `<tool>_ready`, `<tool>_path`). | New mode and/or tool |
| 10 | `app/services/job_manager.py` | Import the service; add output-path branch in `_queue_job_locked`; add `_convert_service` branch in `_process_job`; add verify branch if delete-on-verify is supported. | New tool |
| 11 | `app/routes/convert.py` | `_get_output_path` branch; extension validation in `create_job` **and** `create_batch_jobs`; `supports_delete_on_verify`; archive-input guard. | New mode and/or tool |
| 12 | `app/routes/files.py` | Import the ext set; set convertibility/output-existence flags in `scan_directory` **and** `search_files`. | New tool |
| 13 | `app/routes/info.py` | `/<tool>-info`, `/<tool>-verify`, `/<tool>-verify/events`, `/<tool>-verify-batch/events`; `*_VERIFY_EXTENSIONS`; verify-lane token. | New tool |
| 14 | `app/services/disc_id.py` | Add a serial/title parser if the new **disc** platform should get GAME/NAME tags embedded (chdman create modes only). | New disc platform, optional |
| 15 | `app/services/archive.py` | Add input extensions to its `CONVERTIBLE_EXTENSIONS` only if the tool must read inputs out of `.zip/.7z/.rar`. | Rare (chdman-only today) |
| 16 | `app/services/dat_*.py` / `app/routes/dat.py` | Touch only if the platform participates in DAT (MAMERedump) hash-matching. | Rare |
| 17 | `migrations/versions/*.py` | New Alembic migration **only** if you add DB-persisted columns/tables (use `scripts/new_migration.sh`). The verification/metadata stores are keyed by path and need no migration for a new tool. | If schema changes |

### 3.3 Frontend (no build step — edit JS directly)

| # | File | What you do | When |
|---|------|-------------|------|
| 18 | `static/js/api.js` | `get<Tool>Info`, `verify<Tool>`, `verifyBatch<Tool>` client methods. | New tool |
| 19 | `static/js/app.js` | Primary-tool option (label/hint/filters), `MODE_GROUPS` entry, `getModeTerm`, product-path helper + extension consts, `FileList` selectability + badges, `CHDInfoModal` routing, verify-batch routing, isoHandling kind ordering. | New mode and/or tool |
| 20 | `static/css/style.css` | Add styles only if you introduce new badge/status classes. | If new UI classes |
| 21 | `static/index.html` | Usually untouched (single mount point). | Rarely |

### 3.4 Tests

| # | File | What you do | When |
|---|------|-------------|------|
| 22 | `tests/test_<tool>_routes.py` | Info + verify endpoint tests (copy `test_z3ds_routes.py`). | New tool |
| 23 | `tests/test_<tool>_service.py` | `convert`/`verify`/cancel/bad-extension tests (copy `test_z3ds_verification_service.py`). | New tool |
| 24 | `tests/test_mode_parity_fixes.py` | Add the mode so single-vs-batch validation parity is enforced. | New mode |
| 25 | `tests/conftest.py` | Add a binary stub/fixture if the service needs the binary present. | New tool |

### 3.5 CI / quality gates (must stay green)

| # | File | What it enforces | Action |
|---|------|------------------|--------|
| 26 | `.github/workflows/codacy.yml` | Push/PR: ruff, eslint, pylint, etc. via Codacy CLI. | New code must pass; see §11 |
| 27 | `.github/workflows/codeql.yml` | Weekly + push CodeQL for Python **and** JS/TS. | Auto-covers new code |
| 28 | `.github/workflows/docker-image.yml` | On release: hadolint, multi-arch build, Trivy scan, SBOM/attestation. | Dockerfile must pass hadolint; see §8/§10 |
| 29 | `.github/labeler.yml` / `label.yml` | PR auto-labeling by path. | Add a path glob if you want a label |
| 30 | `.github/dependabot.yml` | Dependency bumps. | Only if adding a new ecosystem |
| 31 | `pyproject.toml` | pylint + ruff config (Codacy reads this). | Respect line-length 100, py310 |
| 32 | `.pylintrc`, `.prospector.yaml`, `.bandit` | Python lint/security (bandit flags `shell=True`, predictable tmp). | Follow the no-shell rule (§15) |
| 33 | `eslint.config.js`, `.eslintrc.json`, `.jshintrc` | JS lint for `static/js`. | New JS must pass |
| 34 | `.stylelintrc.json`, `.csslintrc` | CSS lint. | If you edit CSS |
| 35 | `.markdownlint.json` | Markdown lint. | If you add docs |
| 36 | `.codacy.yml`, `.codacy/` | Codacy tool config/excludes. | Rarely |
| 37 | `config/pmd/ruleset.xml` | PMD ruleset (Codacy). | Rarely |

### 3.6 Deployment & docs

| # | File | What you do | When |
|---|------|-------------|------|
| 38 | `docker-compose.yml` / `.cli.yml` / `.multi-volume.yml` | Document/override the new `<TOOL>_PATH` env or CLI mode env if relevant. | If ops needs the knob |
| 39 | `.version` (+ `scripts/sync-version.sh`) | Bump version across `.version`, `package.json`, `package-lock.json`, `RELEASE_NOTES.md`. | Every release |
| 40 | `README.md` | Supported-formats/tool table, feature list. Also the **Docker Hub** description (published from README by CI). | New tool/platform |
| 41 | `RELEASE_NOTES.md` | Changelog entry. | Every change |
| 42 | `DEPLOYMENT.md`, `DOCKER-COMPOSE.md` | New env vars / volumes / tool requirements. | If deploy surface changes |
| 43 | `walkthrough.md`, `CHANGES_SUMMARY.md` | Narrative docs, if you maintain them. | Optional |
| 44 | `AGENTS.md` | Runbook — add a test/run note if workflow changes. | Optional |
| 45 | `.github/copilot-instructions.md` | Repo AI guidance — update if conventions change. | Optional |

---

## 4. Scenario A — add a platform/mode to an existing tool

Worked example: add a hypothetical **`createcd_audio`** chdman mode (pretend
it produces `.chd` with a platform-specific flag). The same pattern applies
to any new chdman/dolphin sub-format.

### 4.1 Add the mode to the enum — `app/models.py`

```python
class ConversionMode(str, Enum):
    ...
    CREATECD = "createcd"
    CREATECD_AUDIO = "createcd_audio"   # NEW
    ...
```

The string value is what travels over the API and what `job_manager` /
`convert.py` switch on. Pick a value consistent with the tool's existing
prefix convention (`create*`, `extract*`, `dolphin_*`, `z3ds_compress`),
because a lot of logic keys off these prefixes (see below).

### 4.2 Teach the service the new mode — `app/services/chdman.py`

- If it needs special binary flags, branch in `_build_command`
  (`chdman.py:34`), mirroring the existing `createdvd` `-hs 2048` special case
  at `chdman.py:42`.
- Add the output-suffix rule in `get_output_path_for_mode`
  (`chdman.py:561`). The `create*` branch already maps to `{stem}.chd`
  (`chdman.py:579`); only touch this if your suffix differs.
- If it accepts a new input extension, add it to
  `CHDMAN_CONVERTIBLE_EXTENSIONS` (`chdman.py:16`).

### 4.3 Wire validation — `app/routes/convert.py`

`convert.py` keys off mode **prefixes** in several places. Because
`create_job` / `create_batch_jobs` treat anything starting with `create`
generically (e.g. the `.chd` input rejection at `convert.py:407`, output
path via `_get_output_path` at `convert.py:100`), a new `create*` mode often
needs **no new validation code** — it inherits the create-mode rules. Verify
the prefix-based helpers cover you:

- `supports_delete_on_verify` (`convert.py:87`) — `create*` already returns True.
- `_get_output_path` (`convert.py:100`) — routes to `chdman_service` by
  default, so a chdman mode is covered.

If your mode breaks a prefix assumption, add an explicit branch.

### 4.4 Surface it in the UI — `static/js/app.js`

Add an option to the relevant `MODE_GROUPS` entry (`app.js:155`):

```js
{
    id: 'create',
    label: 'Create CHD',
    options: [
        { value: 'createcd', label: 'Create CD CHD (Dreamcast, PS1, Sega CD)' },
        { value: 'createcd_audio', label: 'Create CD CHD (Audio-CD platform)' }, // NEW
        ...
    ]
},
```

Because it reuses the `chdman` tool and `.chd` output, the file badges,
filters, info modal, and verify path all already work. Done.

### 4.5 Test

Add a case to `tests/test_mode_parity_fixes.py` (validation parity between
single + batch create) and `tests/test_chdman_annotations.py` if relevant.

---

## 5. Scenario B/C — add a brand-new tool (with its platform)

Worked example throughout: a fictional **`nszip`** tool that compresses
Nintendo Switch `.nsp`/`.xci` dumps into `.nsz`/`.xcz`. Replace names as
needed. This mirrors z3ds exactly.

### 5.1 Install the binary — `Dockerfile`

Three patterns exist in the current `Dockerfile`; pick the one that fits:

- **Distro package** (chdman via `mame-tools`, pinned to a
  `snapshot.debian.org` `.deb` with a SHA256 check — see `Dockerfile:38`).
- **Distro package, best-effort** (dolphin-emu, amd64-only, non-fatal —
  `Dockerfile:67`). A wrapper script is created at
  `/usr/local/bin/dolphin-tool` only if the binary exists (`Dockerfile:92`).
- **Build from source in a builder stage** (z3ds — `Dockerfile:1-23`), then
  copy the artifact into the runtime image (`Dockerfile:105`).

For `nszip` built from source, add to the builder stage:

```dockerfile
FROM debian:trixie-slim AS builder
RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      git build-essential libzstd-dev ca-certificates && \
    git clone https://github.com/example/nszip.git /tmp/nszip
WORKDIR /tmp/nszip
RUN g++ -O3 src/*.cpp -o nszip -lzstd && chmod +x nszip
```

…and copy it into the runtime stage next to the existing z3ds copy
(`Dockerfile:105`):

```dockerfile
COPY --from=builder /tmp/nszip/nszip /usr/local/bin/nszip
```

If the tool needs a runtime shared lib, add it to the runtime `apt-get
install` list (`Dockerfile:53`). Note that `zstd` (the CLI) is already
installed — z3ds's `verify_stream` shells out to `zstd -t`, so if your tool's
output is a zstd container you can reuse that.

> Multi-arch: the image builds `linux/amd64` and `linux/arm64`
> (see `.github/workflows/docker-image.yml`). A source build compiles per
> arch automatically; a downloaded `.deb` needs a per-arch URL/SHA like the
> `mame-tools` block (`Dockerfile:40`).

### 5.2 Add the binary path setting — `app/config.py`

Add next to the other tool paths (`config.py:111-121`):

```python
nszip_path: str = Field(
    default="/usr/local/bin/nszip", alias="NSZIP_PATH",
)
```

This lets ops override the path/env without code changes, and is what the
service reads in `__init__`.

### 5.3 Write the service module — `app/services/nszip.py`

This is the heart of the work. Copy `app/services/z3ds_compress.py` and
adapt. The required shape (see the contract in §1):

```python
import asyncio, contextlib, logging, os, shutil, threading, time
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from fastapi.concurrency import run_in_threadpool
from services.chdman import ConversionCancelled          # reuse the shared exception
from services.timeout_policy import compute_progress_stall_timeout

NSZIP_CONVERTIBLE_EXTENSIONS = {".nsp", ".xci"}
NSZIP_OUTPUT_FORMATS = {".nsp": ".nsz", ".xci": ".xcz"}    # input ext -> output ext

logger = logging.getLogger("chd.nszip")


class NszipService:
    def __init__(self):
        self.nszip_path = settings.nszip_path
        self._active_pids: set[int] = set()
        self._pid_lock = threading.Lock()

    def _build_command(self, input_path: str, output_path: str) -> list[str]:
        cmd = [self.nszip_path, input_path, output_path]
        # I/O priority: mirror the other services so heavy jobs stay nice.
        if (settings.chdman_ioprio_class is not None
                and settings.chdman_ioprio_level is not None):
            ionice = shutil.which("ionice")
            if ionice:
                cmd = [ionice, "-c", str(settings.chdman_ioprio_class),
                       "-n", str(settings.chdman_ioprio_level)] + cmd
        return cmd

    def _track_pid(self, pid): 
        with self._pid_lock: self._active_pids.add(pid)
    def _untrack_pid(self, pid):
        with self._pid_lock: self._active_pids.discard(pid)
    def active_pids(self):
        with self._pid_lock: return list(self._active_pids)

    async def convert(self, input_path, output_path, mode="nszip_compress",
                      *, compression=None, cancel_event=None) -> AsyncGenerator[dict, None]:
        # 1. mkdir -p output dir
        # 2. build cmd, spawn with asyncio.create_subprocess_exec (NEVER shell=True)
        # 3. apply settings.chdman_nice via preexec_fn (posix only)
        # 4. stream stdout, parse progress, yield {"progress": int, "message": str}
        # 5. honor cancel_event -> terminate/kill, clean partial output,
        #    raise ConversionCancelled
        # 6. apply a stall timeout via compute_progress_stall_timeout(...)
        # 7. on non-zero exit raise RuntimeError(tail of output)
        # 8. finally yield {"progress": 100, "message": "Compression complete"}
        ...

    async def verify(self, path) -> dict:
        final = {"valid": False, "message": "Verification failed"}
        async for u in self.verify_stream(path):
            if u.get("type") in ("complete", "error"):
                final = u
        return {"valid": bool(final.get("valid")), "message": final.get("message") or "Verification failed"}

    async def verify_stream(self, path) -> AsyncGenerator[dict, None]:
        # deep integrity check; yield {"type":"progress"/"complete"/"error", ...}
        ...

    def info(self, path) -> dict:
        # filesystem-level metadata; return keys matching your info model
        ...

    @staticmethod
    def is_convertible(filename) -> bool:
        return Path(filename).suffix.lower() in NSZIP_CONVERTIBLE_EXTENSIONS

    @staticmethod
    def get_output_path_for_mode(mode, input_path, output_dir=None, *, treat_as_stem=False) -> str:
        input_p = Path(input_path)
        stem = input_p.name if treat_as_stem else input_p.stem
        ext = "" if treat_as_stem else input_p.suffix.lower()
        out_ext = NSZIP_OUTPUT_FORMATS.get(ext, ".nsz")
        filename = f"{stem}{out_ext}"
        return str(Path(output_dir) / filename) if output_dir else str(input_p.parent / filename)


nszip_service = NszipService()   # module-level singleton — imported everywhere
```

**Critical implementation rules** (all enforced by the existing services —
copy them, don't improvise):

- **Never** use `shell=True` / string commands. Build an argv list and use
  `asyncio.create_subprocess_exec(cmd[0], *cmd[1:], ...)`. The binary path
  comes from validated settings; inputs are never shell-interpreted.
- **Always** support `cancel_event`: spawn a watcher task that
  `process.terminate()`s (then `kill()`s after a timeout), set a
  `cancelled_by_request` flag, clean up the partial output file, and raise
  `ConversionCancelled`. See `z3ds_compress.py:193` and `chdman.py:156`.
- **Always** apply a stall timeout via `compute_progress_stall_timeout(...)`
  (`z3ds_compress.py:182`) so a wedged binary can't hang a job forever.
- **Progress** is a 0–100 int. If the binary doesn't report percentages,
  estimate from output-file growth like z3ds does (`z3ds_compress.py:251`),
  or emit a heartbeat with elapsed seconds like dolphin (`dolphin_tool.py:241`).
- Respect `settings.chdman_nice` / `chdman_ioprio_*` (these knobs are shared
  across all tools — they aren't chdman-specific despite the name).
- Track PIDs (`_track_pid`/`_untrack_pid`) so the debug heartbeat can report
  them.

### 5.4 Add the mode + info model — `app/models.py`

```python
class ConversionMode(str, Enum):
    ...
    Z3DS_COMPRESS = "z3ds_compress"
    NSZIP_COMPRESS = "nszip_compress"        # NEW
    ...

class NszipInfo(BaseModel):                  # NEW — shape it like Z3DSInfo (models.py:193)
    file: str
    size: int
    size_display: str
    format: str | None = None
    extension: str
    compressed: bool
    compression_type: str | None = None
```

If your tool extracts/decompresses too, add a second mode
(e.g. `NSZIP_EXTRACT = "nszip_extract"`). Keep a consistent prefix
(`nszip_`) — the job pipeline and routes pattern-match on it.

### 5.5 Dispatch in the job pipeline — `app/services/job_manager.py`

Two edits:

**a) Output-path selection** in `_queue_job_locked` (`job_manager.py:139`).
Today it special-cases z3ds and otherwise calls `chdman_service.get_chd_path`:

```python
if output_path is None:
    if mode == ConversionMode.Z3DS_COMPRESS:
        output_path = z3ds_compress_service.get_output_path(file_path, output_dir)
    elif mode == ConversionMode.NSZIP_COMPRESS:                       # NEW
        output_path = nszip_service.get_output_path_for_mode(         # NEW
            mode.value, file_path, output_dir)                        # NEW
    else:
        output_path = chdman_service.get_chd_path(file_path, output_dir)
```

(In practice `convert.py` almost always passes an explicit `output_path`, so
this branch is a fallback — but wire it for correctness.)

**b) Service selection** in `_process_job` (`job_manager.py:1444`). Extend the
`_convert_service` ladder:

```python
_convert_service = (
    dolphin_tool_service if job.mode.value.startswith("dolphin_")
    else z3ds_compress_service if job.mode == ConversionMode.Z3DS_COMPRESS
    else nszip_service if job.mode == ConversionMode.NSZIP_COMPRESS      # NEW
    else chdman_service
)
```

Add the import at the top (`job_manager.py:27` neighborhood):
`from services.nszip import nszip_service`.

**c) Delete-on-verify** (optional). If you want the "verify then delete
source" feature for this tool, also extend the verify branch in
`_process_job`. z3ds has a dedicated branch at `job_manager.py:1556`
(`if job.mode.value == "z3ds_compress": ... z3ds_compress_service.verify(...)`)
because its verify lives outside the dolphin/chdman pair. Add an analogous
branch calling `nszip_service.verify(...)`, or generalize the selector.

### 5.6 Validation + output dispatch — `app/routes/convert.py`

- **Output path dispatch:** extend `_get_output_path` (`convert.py:100`):

```python
def _get_output_path(mode, input_path, output_dir, *, treat_as_stem=False):
    if _is_dolphin_mode(mode):
        return dolphin_tool_service.get_output_path_for_mode(...)
    if mode == ConversionMode.Z3DS_COMPRESS.value:
        return z3ds_compress_service.get_output_path_for_mode(...)
    if mode == ConversionMode.NSZIP_COMPRESS.value:                    # NEW
        return nszip_service.get_output_path_for_mode(                 # NEW
            mode, input_path, output_dir, treat_as_stem=treat_as_stem) # NEW
    return chdman_service.get_output_path_for_mode(...)
```

- **delete-on-verify support:** add your mode to `supports_delete_on_verify`
  (`convert.py:87`) if applicable.
- **Extension validation:** add a block in both `create_job`
  (`convert.py:421`, the z3ds example) and `create_batch_jobs`
  (`convert.py:662`):

```python
is_nszip = mode == ConversionMode.NSZIP_COMPRESS.value
...
if is_nszip:
    ext = Path(file_path).suffix.lower()
    if ext not in NSZIP_CONVERTIBLE_EXTENSIONS:
        raise HTTPException(status_code=400,
            detail="nszip mode requires Switch dumps (.nsp, .xci)")
```

- **Archive inputs:** z3ds/dolphin block archive (`::`) inputs
  (`convert.py:357` and `:606`). If your tool can't read out of archives, add
  your mode to those guards. (chdman is the only tool that reads archive
  members.)
- Import the convertible set + service at the top of `convert.py`
  (`convert.py:27` shows the z3ds import).

### 5.7 Mark inputs convertible in listings — `app/routes/files.py`

`files.py` annotates each `FileEntry` with convertibility flags so the UI can
badge rows and gate selection. Today it imports each tool's extension set
(`files.py:11-13`) and sets `convertible` / `dolphin_convertible` /
`z3ds_convertible` plus output-existence flags.

1. Import your set: `from services.nszip import NSZIP_CONVERTIBLE_EXTENSIONS, NSZIP_OUTPUT_FORMATS`.
2. Add a model field. In `app/models.py` `FileEntry` (`models.py:42`) add
   `nszip_convertible: bool = False` and, if you show "output already exists"
   badges, `has_nszip: bool = False`, `nszip_ready: bool = False`,
   `nszip_path: str | None = None` — paralleling the z3ds fields
   (`models.py:56`).
3. In `scan_directory` (`files.py:150`) compute
   `is_nszip_convertible = ext in NSZIP_CONVERTIBLE_EXTENSIONS` and an
   output-existence check modeled on `_detect_z3ds_output_path`
   (`files.py:34`), then pass them into the `FileEntry(...)` constructor
   (`files.py:232`).
4. Do the same in `search_files` (`files.py:316` adds the extension to the
   "is this file interesting" test, and `:349` records the flags).

### 5.8 Info + verify endpoints — `app/routes/info.py`

Mirror the z3ds endpoints (`info.py:338-663`, `:1506`). Add:

- `GET /api/nszip-info` → `nszip_service.info(...)`, returns your `NszipInfo`
  model. Model the validation on `get_z3ds_info` (`info.py:1506`).
- `GET /api/nszip-verify` (one-shot) and `GET /api/nszip-verify/events`
  (SSE), modeled on `verify_z3ds` (`info.py:527`) and `verify_z3ds_events`
  (`info.py:564`). **Acquire the verify lane** with
  `_acquire_verify_lane_or_429()` (`info.py:55`) so verification stays
  globally bounded by `MAX_VERIFY_CONCURRENCY`, and call
  `verification_store.mark_verified(path)` on success.
- `POST /api/nszip-verify-batch/events` (SSE) modeled on
  `verify_z3ds_batch_events` (`info.py:355`).
- Define `NSZIP_VERIFY_EXTENSIONS` / `NSZIP_INFO_EXTENSIONS` near the z3ds
  ones (`info.py:341`) and a `_is_nszip_verify_file` helper.

No new router registration is needed — `info.router` is already mounted under
`/api` in `app/main.py:285`.

### 5.9 Frontend — `static/js/api.js`

Add client methods next to the z3ds ones:

- `getNszipInfo(path)` — like `getZ3DSInfo` (`api.js:233`).
- `verifyNszip(path, {onProgress})` — SSE, like `verify3DS` (`api.js:344`).
- `verifyBatchNszip(paths, {...})` — like `verifyBatchZ3DS` (`api.js:645`).

`createJob` / `createBatchJobs` (`api.js:63`, `:82`) are generic over `mode`,
so no change there — the UI just passes `mode: 'nszip_compress'`.

### 5.10 Frontend — `static/js/app.js`

The UI has a **primary-tool selector** (`'chdman' | 'dolphin' | 'z3ds'`) plus
a per-tool default mode. To add `nszip`:

1. **Tool label** — `getPrimaryToolLabel` (`app.js:97`): add
   `if (toolSelection === 'nszip') return 'Switch';`.
2. **Tool hint** — `getPrimaryToolHint` (`app.js:139`): add a description line.
3. **Filters** — `getFilterOptions` (`app.js:104`): add an `nszip` branch with
   `{ value: '.nsp,.xci', label: 'Switch dumps' }`.
4. **Mode group** — `MODE_GROUPS` (`app.js:155`): add
   `{ id: 'nszip', label: 'Nintendo Switch', options: [{ value: 'nszip_compress', label: 'Compress to NSZ/XCZ' }] }`.
5. **Mode terminology** — `getModeTerm` (`app.js:73`): add an `nszip` branch
   for product/verification labels.
6. **Output-path helper** — add a `getNszProductPath(path)` like
   `get3dsProductPath` (`app.js:48`) and the source/verify extension consts
   like `Z3DS_SOURCE_EXTENSIONS` (`app.js:25`).
7. **Selectability + badges** in `FileList` (`app.js:793`): add an
   `isNszipMode` flag and an entry-selectable clause, plus "convertible" /
   "output exists" badges paralleling the z3ds ones (`app.js:851-858`).
8. **Info-modal routing** — `CHDInfoModal` (`app.js:1104`): extend the `mode`
   memo (`app.js:1109`) and the `fetchInfo` switch (`app.js:1129`) to route
   `nszip` files to `api.getNszipInfo`.
9. **Verify-batch routing** (`app.js:2076`, `:2145`, `:2446`, `:2516`): add an
   `nszip` kind to the verify item partitioning and call `verifyBatchNszip`.
10. **isoHandling / preferred-kind ordering** (`app.js:1605`, `:2009`): add
    `nszip` to the kind-preference arrays if a single file could be claimed by
    multiple tools (rare — usually extensions are tool-exclusive).

> The frontend is plain ES modules + Preact via CDN (`static/index.html:` loads
> `app.js` as a module; there is **no build step**). Edit the `.js` directly.
> If you touch Svelte anywhere, use the Svelte MCP server — but this project is
> Preact/`htm`, not Svelte.

### 5.11 Tests

Add, modeled on the existing suites:

- `tests/test_nszip_routes.py` — info + verify endpoint behavior
  (copy `tests/test_z3ds_routes.py`).
- `tests/test_nszip_service.py` — `convert`/`verify` happy path, cancel path,
  bad-extension rejection (copy `tests/test_z3ds_verification_service.py`).
- Extend `tests/test_mode_parity_fixes.py` so single-job and batch-job
  validation stay in lockstep for the new mode.

Run them:

```bash
cd /home/user/compressatorium
pytest -q tests/test_nszip_routes.py tests/test_mode_parity_fixes.py
```

`tests/conftest.py` stubs the binaries, so tests don't need the real tool
installed.

### 5.12 Docs + version

- Update `README.md` (tool table / supported formats) and `RELEASE_NOTES.md`.
- Bump the version with `./scripts/sync-version.sh <new-version>` (keeps
  `.version`, `package.json`, `package-lock.json`, `RELEASE_NOTES.md` in
  sync — see `AGENTS.md` §5).

---

## 6. Tandem checklist (copy/paste)

A new tool **is** a new platform, so do all of these together:

```
BINARY
[ ] Dockerfile: build/install binary into runtime image (+ runtime libs)
[ ] config.py: add <tool>_path Field with env alias

SERVICE (app/services/<tool>.py)
[ ] *_CONVERTIBLE_EXTENSIONS, *_OUTPUT_FORMATS, module-level singleton
[ ] convert(): exec (no shell), progress yields, cancel_event, stall timeout,
    nice/ionice, PID tracking, ConversionCancelled, final 100%
[ ] verify() + verify_stream()
[ ] info(), is_convertible(), get_output_path_for_mode(), active_pids()

MODE + MODELS (app/models.py)
[ ] ConversionMode.<TOOL>_<ACTION> with a consistent prefix
[ ] <Tool>Info model (if it has an info modal)
[ ] FileEntry: <tool>_convertible / has_<tool> / <tool>_ready / <tool>_path

PIPELINE (app/services/job_manager.py)
[ ] import the service
[ ] _queue_job_locked: output-path branch
[ ] _process_job: _convert_service ladder branch
[ ] _process_job: verify branch (only if delete-on-verify supported)

ROUTES
[ ] convert.py: _get_output_path branch, extension validation (single+batch),
    supports_delete_on_verify, archive-input guard
[ ] files.py: import ext set, compute flags in scan_directory + search_files
[ ] info.py: /<tool>-info, /<tool>-verify(+/events), /<tool>-verify-batch/events,
    *_VERIFY_EXTENSIONS, verify-lane acquire, mark_verified

FRONTEND
[ ] api.js: get<Tool>Info, verify<Tool>, verifyBatch<Tool>
[ ] app.js: tool label, hint, filters, MODE_GROUPS, getModeTerm,
    product-path helper, FileList selectability + badges,
    CHDInfoModal routing, verify-batch routing

TESTS + DOCS
[ ] tests/test_<tool>_routes.py, tests/test_<tool>_service.py, mode-parity
[ ] tests/conftest.py: binary stub fixture
[ ] README / RELEASE_NOTES / ./scripts/sync-version.sh

PERIPHERAL (don't forget)
[ ] .dockerignore: confirm nothing new is excluded
[ ] requirements.txt: new pip deps (if any)
[ ] .local-bin/<binary>: local-dev copy (from-source tools)
[ ] entrypoint.sh: CLI-mode loop (only if headless batch support wanted)
[ ] disc_id.py: serial/title parser (only for new disc platforms w/ tagging)
[ ] archive.py CONVERTIBLE_EXTENSIONS (only if reading from archives)
[ ] migrations/: new Alembic rev (only if persisting new columns/tables)
[ ] docker-compose*.yml + DEPLOYMENT.md + DOCKER-COMPOSE.md: env/volume docs
[ ] lint clean: ruff, pylint, eslint, hadolint (Dockerfile), markdownlint
[ ] CI awareness: hadolint + Trivy gate the release build; CodeQL scans new code
```

---

## 8. The Docker image in depth — `Dockerfile`, `.dockerignore`, deps

The image is a two-stage multi-arch build (`debian:trixie-slim`).

**Build args & arch.** `ARG TARGETARCH` (`Dockerfile:36`) is `amd64`/`arm64`.
A from-source build (g++) compiles per-arch automatically. A downloaded
`.deb`/binary needs a **per-arch URL + SHA256**, like the `mame-tools` block
(`Dockerfile:38-84`) which branches on `TARGETARCH` and verifies with
`sha256sum -c`. Reproduce that pattern for any pinned download.

**Builder stage** (`Dockerfile:1-23`): install toolchain (`build-essential`,
`libzstd-dev`, …), `git clone`, compile, `chmod +x`. Add your build here.

**Runtime stage** (`Dockerfile:25+`):
- Add any runtime shared libraries to the `apt-get install` list
  (`Dockerfile:53-66`). The CLI `zstd` is already present (z3ds verify uses
  `zstd -t`); `ionice`/`nice` come from `util-linux`.
- `COPY --from=builder /tmp/<tool>/<binary> /usr/local/bin/<binary>`
  (next to the z3ds copy at `Dockerfile:105`).
- Optional/best-effort installs (a tool that only exists on one arch) should
  follow the dolphin-emu pattern: install with `|| echo WARNING`
  (`Dockerfile:67-73`) and create a wrapper only `if command -v` succeeds
  (`Dockerfile:92-95`). The service must then tolerate a missing binary
  gracefully (surface a clear runtime error).
- The image runs as **uid/gid 999 (`converter`)** (`Dockerfile:148`). Install
  to a world-readable path (`/usr/local/bin`) and `chmod +x`.
- `HEALTHCHECK` (`Dockerfile:137`) only probes the web UI; no change needed.

**hadolint** runs in CI (`docker-image.yml` `lint` job) with
`failure-threshold: error` and `ignore: DL3008,DL3013`. Keep your `RUN`
layers clean (combine `apt-get update && install`, `rm -rf
/var/lib/apt/lists/*`, pin where the existing file pins).

**`.dockerignore`**: `app/`, `static/`, `migrations/`, `entrypoint.sh`,
`requirements.txt` are copied in. `tests/` and most `*.md` (except
`README.md`) are excluded — so this guide and your service tests never enter
the image. If you reference a new top-level file from the Dockerfile, confirm
it isn't ignored.

**Python deps**: anything your service `import`s that isn't stdlib or already
in `requirements.txt` must be added there (and it'll be picked up by
`pip install -r` at `Dockerfile:102`). Dev/test-only deps go in
`requirements-dev.txt`.

**Local dev (`run_dev.sh`)**: it bootstraps a `.venv` and runs uvicorn
against your host. From-source binaries are kept in `.local-bin/` (the repo
already ships `.local-bin/z3ds_compressor`) so the app works without Docker.
Drop your compiled binary there and point `<TOOL>_PATH` at it (or add it to
`PATH`) for local testing.

---

## 9. Headless CLI mode — `entrypoint.sh`

`entrypoint.sh` supports `CHD_MODE=cli` (batch-convert a volume with no web
UI; see `docker-compose.cli.yml`). **This path is independent of the Python
app** — it shells out to `chdman` directly and currently only knows
`createcd`/`createdvd` over `*.gdi *.iso *.cue` (`entrypoint.sh:127-176`),
validating `CHDMAN_MODE` against that allow-list (`entrypoint.sh:135`).

If your new tool/platform should be usable headlessly:
- Extend the `CHDMAN_MODE` case statement (or add a parallel `CONVERT_TOOL`
  env) to accept your mode.
- Add a globbing loop for your input extensions and invoke your binary,
  mirroring the existing `for i in *.gdi *.iso *.cue` block.
- Skip-if-output-exists logic (`[[ -e "${i%.*}.<ext>" ]]`) should match your
  output suffix.

If headless mode is out of scope, you can skip this — the web UI path
(`exec uvicorn main:app …`, `entrypoint.sh:197`) already dispatches your tool
via the Python pipeline.

The same file also handles **PUID/PGID remap** and the privilege drop to
`converter` (`entrypoint.sh:4-60`). No changes needed there.

---

## 10. CI/CD & GitHub workflows

| Workflow | Trigger | Relevance to a new tool |
|----------|---------|-------------------------|
| `docker-image.yml` | **release published** | Builds/pushes multi-arch image to Docker Hub + GHCR, runs **hadolint** (Dockerfile must pass), **Trivy** CRITICAL/HIGH scan (a vulnerable new dep can fail this), generates SBOM + provenance attestation, and **publishes `README.md` as the Docker Hub description**. Also auto-syncs `package.json` from the release tag. |
| `codacy.yml` | push, PR, weekly | Runs the Codacy CLI (ruff, eslint, pylint, bandit, etc.). New Python/JS must lint clean. Honors `pyproject.toml`, `.pylintrc`, `eslint.config.js`, `.codacy.yml`. |
| `codeql.yml` | push, weekly | CodeQL SAST for **python** and **javascript-typescript** (`codeql.yml:30-32`). New code is scanned automatically — no config change, but don't introduce flagged patterns (e.g. command injection — another reason for the no-`shell=True` rule). |
| `label.yml` + `labeler.yml` | PR | Path-based auto-labels. Add a glob if you want your area labeled. |
| `stale.yml` | schedule | Marks stale issues/PRs. Irrelevant. |
| `dependabot.yml` | schedule | Dependency PRs. Add an ecosystem entry only if you introduce a new manifest. |

Key takeaways for a contributor:
- **The image only builds on a GitHub Release.** There is no build-on-push.
  To ship your tool you (or a maintainer) cut a release tag `vX.Y.Z`; CI then
  builds `linux/amd64,linux/arm64` and tags `latest`/`beta` accordingly.
- **Trivy can block a release** if a new system/python package pulls a
  CRITICAL/HIGH CVE (`ignore-unfixed: true` softens this). Prefer pinned,
  patched packages — the `mame-tools` snapshot pin is the model.
- **Dockerfile changes must pass hadolint** locally before you push
  (`hadolint Dockerfile`).

---

## 11. Lint & quality gates you must satisfy

Run these before pushing (they mirror Codacy):

```bash
cd /home/user/compressatorium
ruff check app tests            # style/lint — config in pyproject.toml ([tool.ruff], py310)
pylint app                      # config in .pylintrc / pyproject.toml ([tool.pylint])
pytest -q tests                 # full suite (binaries are stubbed in conftest)
npx eslint static/js            # JS lint — eslint.config.js
hadolint Dockerfile             # if you touched the Dockerfile
```

Conventions baked into the configs that affect new tool code:

- **Line length 100** (`pyproject.toml [tool.pylint.format]`, ruff `py310`).
- **`bandit`** (`.bandit`) flags `subprocess` with `shell=True` and
  predictable temp paths. The services deliberately use
  `create_subprocess_exec` with argv lists and document the temp-dir
  reasoning (`config.py:186-194`). Follow suit or bandit/CodeQL will flag you.
- **Broad `except Exception`** at subprocess boundaries is allowed by
  `.pylintrc`/`pyproject.toml` *if* you log at the call site (review
  enforces this). The existing services do `logger.exception(...)`.
- **`isort`/import order** is enforced in CI (pylint's `wrong-import-order` is
  disabled because ruff/isort own it). Keep imports sorted.
- Markdown docs are linted by `.markdownlint.json`; CSS by
  `.stylelintrc.json`.

---

## 12. Database, migrations & persistence

The app uses **SQLite via SQLAlchemy + Alembic** (`app/services/db.py`,
`migrations/`). On startup, `apply_migrations()` brings the schema to head and
imports legacy JSON stores (`app/main.py:137-201`).

For a **new tool**, you almost certainly need **no migration**, because the
persistence layers are generic over file paths:

- **`verification_store`** records "this output path was verified" keyed by
  path. Your verify endpoints call `verification_store.mark_verified(path)`
  and it just works for any extension.
- **`chd_metadata_store`** caches CHD-specific metadata; a non-CHD tool
  doesn't use it.

You need a migration **only** if you add new persisted columns/tables (e.g. a
tool-specific metadata cache). In that case:

```bash
./scripts/new_migration.sh "add nszip metadata table"
# edit the generated migrations/versions/000X_*.py (upgrade/downgrade)
pytest -q tests/test_alembic_migrations.py tests/test_db_migration.py
```

`migrations/env.py` and `migrations/versions/0001_baseline_schema.py` are the
references. `tests/test_alembic_migrations.py` validates head consistency.

---

## 13. Supporting services: disc_id, archive, DAT

These are **disc-format-specific** and usually irrelevant to a non-disc tool,
but matter when your *platform* is a disc image processed by chdman.

- **`app/services/disc_id.py`** extracts a game serial/title (e.g.
  `SLUS-20312`) from PS1/PS2/PSP/Dreamcast sources and embeds GAME/NAME tags
  into the CHD after `createcd`/`createdvd` (`job_manager.py:1514`). To make a
  **new disc platform** get tags, add a parser branch in
  `extract_from_source` (`disc_id.py:379`) and a normalizer like
  `_normalize_ps_serial` (`disc_id.py:224`). Not needed for RVZ/3DS/etc.
- **`app/services/archive.py`** has its **own** `CONVERTIBLE_EXTENSIONS`
  (`archive.py:28`) listing inputs that can be converted *from inside* a
  `.zip/.7z/.rar`. Only chdman reads from archives today; z3ds and dolphin
  block archive (`::`) inputs in `convert.py`. Add to this set only if you
  want your tool to consume archive members (and then you must handle
  extraction + related-file handling in `job_manager._process_job`).
- **DAT / MAMERedump** (`app/services/dat_*.py`, `app/routes/dat.py`,
  `app/services/file_hasher.py`) is for hash-matching dumps against DAT
  databases. Touch only if your platform participates in DAT verification.

---

## 14. End-to-end "in tandem" example map

For the `nszip` (Nintendo Switch) tandem example used in §5, here is every
edit on one screen:

```
Dockerfile                      builder: clone+g++ nszip; runtime: COPY binary, libs
.dockerignore                   (verify nothing new is excluded)
requirements.txt                (only if service imports a new pip pkg)
.local-bin/nszip                local-dev binary for run_dev.sh
entrypoint.sh                   (optional) CLI-mode loop for .nsp/.xci
app/config.py                   nszip_path Field (NSZIP_PATH)
app/services/nszip.py           NszipService + NSZIP_CONVERTIBLE_EXTENSIONS + singleton
app/models.py                   ConversionMode.NSZIP_COMPRESS; NszipInfo; FileEntry flags
app/services/job_manager.py     import; _queue_job_locked + _process_job + verify branches
app/routes/convert.py           _get_output_path; validation (single+batch); del-on-verify
app/routes/files.py             import set; flags in scan_directory + search_files
app/routes/info.py              /nszip-info, /nszip-verify(+events+batch); VERIFY exts; lane
static/js/api.js                getNszipInfo, verifyNszip, verifyBatchNszip
static/js/app.js                tool label/hint/filters; MODE_GROUPS; getModeTerm;
                                product-path helper; FileList badges; info modal; verify routing
static/css/style.css            (only if new badge classes)
tests/test_nszip_routes.py      info+verify endpoint tests
tests/test_nszip_service.py     convert/verify/cancel/bad-ext tests
tests/test_mode_parity_fixes.py add nszip_compress to parity matrix
tests/conftest.py               binary stub fixture
docker-compose*.yml             (optional) NSZIP_PATH / CLI env docs
.version + scripts/sync-version.sh   version bump
README.md / RELEASE_NOTES.md    supported-formats table + changelog
DEPLOYMENT.md / DOCKER-COMPOSE.md    new env var, if any
```

---

## 15. Gotchas & conventions

- **Mode prefixes are load-bearing.** Code branches on
  `mode.startswith("create"|"extract"|"dolphin_")` and on equality with
  `z3ds_compress`. Choose a clear, unique prefix and grep for every
  prefix check when you add a mode that doesn't fit an existing family.
- **Single-job and batch endpoints must validate identically.**
  `create_job` and `create_batch_jobs` in `convert.py` duplicate their
  validation; `tests/test_mode_parity_fixes.py` exists to catch drift. Edit
  both.
- **The verify lane is global.** All verification across all tools shares
  `MAX_VERIFY_CONCURRENCY` via `workload_limiter` (`info.py:55`). Always
  acquire/release the token in verify endpoints, or you'll bypass
  backpressure.
- **Reuse `ConversionCancelled`** from `services.chdman` — the pipeline's
  cancel handling (`job_manager.py:1782`) catches that specific class.
- **`MAX_CONCURRENT_JOBS` defaults to 1.** Conversions are I/O-heavy; the
  dispatcher runs them serially unless host capacity is validated
  (`AGENTS.md` note). Don't assume parallelism in a service.
- **No frontend build step.** `static/js/*.js` are served as-is. ESLint
  config exists (`eslint.config.js`) — keep the style consistent.
- **The image runs as uid 999 (`converter`).** Binaries must be executable by
  a non-root user; install them to a world-readable path like
  `/usr/local/bin` as the existing tools do.
- **chdman is the only archive-aware tool.** If your tool can't decompress its
  inputs straight out of `.zip/.7z/.rar`, block `::` archive paths in
  `convert.py` (as z3ds and dolphin do).
- **Binary path is config, not hardcoded.** Always read
  `settings.<tool>_path` in `__init__` so deployments can relocate/override
  via env.
```
