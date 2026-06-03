# Adding Platforms and Compression/Decompression Tools

This is a developer guide for extending Compressatorium with **new conversion
tools** (a brand-new compressor binary) and **new platforms / formats** (a new
chdman create mode, or a new file type a tool can handle). It walks the full
vertical slice, from the binary in the Docker image, through the Python service
and tool plugin, the job pipeline, the FastAPI routes, and finally the Svelte
web UI, and shows how to add a tool and a platform in tandem.

It is written against the codebase as it stands today, with six tools already
wired up:

| Tool | Binary | Service module | Plugin | Handles |
|------|--------|----------------|--------|---------|
| **chdman** | `mame-tools` (`/usr/bin/chdman`) | `app/services/chdman.py` | `app/services/tools/chdman.py` | CD/DVD/HD/Raw/LaserDisc disc images to/from `.chd` |
| **dolphin-tool** | `dolphin-emu` (`/usr/local/bin/dolphin-tool`) | `app/services/dolphin_tool.py` | `app/services/tools/dolphin.py` | GameCube/Wii images to/from `.rvz/.wia/.gcz/.iso` |
| **z3ds_compressor** | built from source (`/usr/local/bin/z3ds_compressor`) | `app/services/z3ds_compress.py` | `app/services/tools/z3ds.py` | Nintendo 3DS ROMs to `.zcci/.zcia/.z3ds` |
| **nsz** | `nsz` pip package (on PATH) | `app/services/nsz.py` | `app/services/tools/nsz.py` | Nintendo Switch `.nsp`/`.xci` to/from `.nsz`/`.xcz` |
| **maxcso** | built from source (`/usr/local/bin/maxcso`) | `app/services/maxcso.py` | `app/services/tools/maxcso.py` | PSP/PS2 `.iso` to/from `.cso` (CSO v1/v2) / `.zso` / `.dax` (tool id `cso`) |
| **7z** | `p7zip-full` (`7z` on PATH) | `app/services/romz.py` | `app/services/tools/romz.py` | Handheld ROM `.gb`/`.gbc`/`.gba`/`.nds` to/from `.7z`/`.zip` (tool id `romz`) |

> **`romz` is the "produces archives / reuses an existing binary" example.** It
> needs no Dockerfile build step (the `7z` CLI already ships via `p7zip-full`)
> and reuses `app/services/archive.py`'s `zipfile`/`py7zr` for the read side
> (member listing, info, single-member validation), shelling `7z` only for the
> write/extract/test paths via the shared `SubprocessRunner`. Its `.7z`/`.zip`
> inputs overlap the archive-browse extensions, so its modes set
> `allows_archive_input=False` and `routes/files.py`'s existing `is_archive`
> guard keeps those files classified as browseable archives, not convertible
> sources.

`z3ds` is the cleanest, most self-contained example of "a new tool that handles
a new platform," so this guide uses it as the reference implementation
throughout. When in doubt, **copy what z3ds does.**

> **The `nszip` example below is now real.** This guide was written before
> Switch support existed and uses a *fictional* `nszip` tool as its §5
> walkthrough. That tool now ships for real as **`nsz`** (last row above). The
> walkthrough still teaches the generic pattern, but the real `nsz`
> implementation differs from the sketch in three ways worth knowing:
>
> 1. **Packaging is pip, not a g++ build.** `nsz` is a Python package added to
>    `requirements.txt`; it lands on PATH in the venv. No builder-stage compile,
>    no `.local-bin/` copy. `nsz_path` defaults to the bare name `nsz`.
> 2. **It needs user-supplied `prod.keys`.** Switch content is encrypted, so nsz
>    decrypts (losslessly, reversibly) before compressing. The app ships no
>    keys; the operator mounts their own and sets `SWITCH_KEYS` to the directory
>    holding them (else the app best-effort searches `~/.switch` and the
>    volumes). nsz loads keys at import from `~/.switch/prod.keys` with no
>    `--keys` flag, so the service runs it with a temp `$HOME` symlinked to the
>    resolved key file, and fails fast with a clear message when keys are
>    missing. No other tool takes user keys, so this is the one genuinely new
>    pattern.
> 3. **It has two modes:** `nsz_compress` (`COMPRESS`) and `nsz_decompress`
>    (`EXTRACT`). Verify is scoped to the compressed outputs (`.nsz/.xcz`) via
>    nsz's own `-V`, so delete-on-verify is offered for compress only.
>
> Read `app/services/nsz.py` and `app/services/tools/nsz.py` for the real thing.
>
> **The big idea: there is a tool registry.** Adding a tool used to mean editing
> `if/elif` ladders in ~20 files. It doesn't anymore. The backend has a tool
> registry (`app/services/tools/`) that mirrors the frontend one. You write a
> small plugin class, register it once, and the generic dispatch in
> `job_manager` and `convert.py` picks it up by mode. Most of the old per-tool
> branches are gone. The sections below reflect that.

---

## 1. Architecture overview

A conversion request flows through these layers. Adding a tool/platform means
touching the same layers in the same order:

```
            ┌─────────────────── Web UI (Svelte 5 + Vite, under src/) ───────────────┐
            │  src/lib/tools/registry.js: one TOOLS entry per tool, drives all UI   │
            │  src/lib/api/endpoints.js:  HTTP / SSE / batch-verify client methods   │
            └───────────────────────────────────┬───────────────────────────────────┘
                                                 │  POST /api/jobs   (mode=...)
            ┌────────────────────────────────────▼──────────────────────────────────────┐
            │  Routes (app/routes)                                                        │
            │   convert.py  – validate mode/extension (via registry.spec), enqueue job    │
            │   files.py    – mark files convertible in directory listings + search       │
            │   info.py     – per-tool info + verify (single + batch + SSE) endpoints      │
            └────────────────────────────────────┬──────────────────────────────────────┘
                                                  │  job_manager.create_job(mode=...)
            ┌─────────────────────────────────────▼─────────────────────────────────────┐
            │  Job pipeline (app/services/job_manager.py)                                 │
            │   _queue_job_locked  – registry.for_mode(mode).output_path(...)              │
            │   _process_job       – registry.for_mode(mode).convert(...) / .verify(...)   │
            └─────────────────────────────────────┬─────────────────────────────────────┘
                                                   │  plugin.convert(...) async generator
            ┌──────────────────────────────────────▼────────────────────────────────────┐
            │  Tool plugin (app/services/tools/<tool>.py) + service (app/services/<tool>.py)│
            │   plugin holds ModeSpec rows; service spawns the binary, parses progress     │
            └──────────────────────────────────────┬────────────────────────────────────┘
                                                    │  subprocess
            ┌────────────────────────────────────────▼───────────────────────────────────┐
            │  Binary in the image (Dockerfile) + path in app/config.py                    │
            └──────────────────────────────────────────────────────────────────────────────┘
```

Two supporting layers:

- **`app/models.py`** holds the `ConversionMode` enum (every mode string lives
  here) and the Pydantic info models (`CHDInfo`, `DolphinDiscInfo`, `Z3DSInfo`).
- **`app/config.py`** `Settings` holds the binary path for each tool
  (`chdman_path`, `dolphin_tool_path`, `z3ds_compressor_path`).

### The tool registry

`app/services/tools/` is the heart of the backend. It has five parts:

- **`spec.py`** defines `ModeKind` (`CREATE` / `EXTRACT` / `COPY` / `COMPRESS`)
  and the frozen `ModeSpec` dataclass: per-mode metadata that the routes and
  pipeline read instead of branching on the mode string.
- **`base.py`** defines the `ToolPlugin` protocol (the contract) and a `BaseTool`
  helper that supplies sensible defaults so concrete plugins stay tiny.
- **`registry.py`** defines `ToolRegistry`, the lookup object. It indexes tools
  by id and by mode and answers `for_mode(mode)`, `spec(mode)`,
  `archive_input_extensions()`, `verify_extensions()`, `output_extensions()`,
  `scannable_extensions()` (which drives the library scan / DAT-match
  discovery), and friends.
- **`chdman.py` / `dolphin.py` / `z3ds.py` / `nsz.py` / `maxcso.py` / `romz.py`**
  are the six plugins. Each is a thin `BaseTool` subclass that holds `ModeSpec`
  rows and delegates the real work to the underlying service singleton.
- **`__init__.py`** builds the `registry` singleton and registers all six
  tools. This is the single wiring point.

### The plugin contract: `ModeSpec` and `BaseTool`

A `ModeSpec` row (`spec.py`) describes one mode:

```python
@dataclass(frozen=True)
class ModeSpec:
    mode: str                                 # wire value == a ConversionMode value
    tool_id: str                              # "chdman" | "dolphin" | "z3ds"
    kind: ModeKind                            # CREATE | EXTRACT | COPY | COMPRESS
    label: str                                # UI label
    group: str                                # UI group id
    output_ext: str | None                    # ".chd"/".rvz"/None (None = mapped from input)
    input_extensions: frozenset[str]
    supports_compression: bool = False
    supports_compression_level: bool = False  # dolphin rvz/wia only
    supports_delete_on_verify: bool = False
    allows_archive_input: bool = False        # True for convertible-source modes; see §17
```

A plugin subclasses `BaseTool` and provides:

| Member | Purpose |
|--------|---------|
| `id`, `display_name`, `binary_path` | identity + binary path (from settings) |
| `modes` | a tuple of `ModeSpec` rows |
| `output_extensions`, `verify_extensions` | produced extensions and verify-accepted extensions. `output_extensions` drives the "output exists" badges **and** the registry-driven library scan / DAT-matching discovery (`registry.scannable_extensions()` = the union of output + verify), so list every extension your tool actually writes (including sidecars like CHDMAN's extractcd `.bin`). |
| `convert(input_path, output_path, mode, *, compression=None, cancel_event=None)` | async generator yielding `{"progress": int, "message": str}`, raising `ConversionCancelled` when `cancel_event` fires |
| `verify(path)` / `verify_stream(path)` | deep integrity check, one-shot and streaming |
| `info(path)` / `info_model(raw, path)` | metadata dict + the Pydantic model it maps to |
| `output_path(mode, input_path, output_dir=None, *, treat_as_stem=False)` | compute the output path |
| `detect_output(input_path)` | optional, returns an `OutputStatus` so the file list can badge "output already exists". May content-validate the candidate before claiming it: `romz` only reports a `.7z`/`.zip` sibling as its output when it's a genuine single-ROM archive (not just any file matching the `Game.gba.7z` naming), so the badge and the source row's verify-from-output flow track real outputs. |
| `verifies_path(path)` | optional per-file refinement of `verify_extensions`. Default (in `BaseTool`) is a plain extension match; override when your tool claims a broad container extension but only handles a subset (`romz` claims `.7z`/`.zip` yet only verifies single-ROM archives). `routes/files.py` materializes the result into `FileEntry.verifiable_by`, which the frontend gates the Verify/Info row-actions on. May do disk I/O — it runs inside the threadpool scan. |
| `active_pids()` | PIDs for the debug heartbeat |
| `post_convert(input_path, output_path, mode)` | optional hook, default no-op |
| `embedded_hashes(path, *, cancel_event=None)` | **optional** DAT-match fast path. Return `(sha1, match_type)` tuples your format already carries / can derive cheaply (so matching skips a full file hash); default `[]` → the caller falls back to a file-level SHA1, which is correct for raw formats. Raise `EmbeddedHashUnavailable` (from `services.tools.base`) when you *should* yield a hash but the attempt failed transiently, so it's recorded as a non-cacheable miss instead of a false negative. |
| `embedded_hash_is_exhaustive` | optional flag (default `False`). Set `True` only when your container bytes can never appear in a DAT (e.g. a recompressed image), so a content-hash miss is definitive and the file-level fallback is skipped; leave `False` if your file's own SHA1 might be indexed. |

`BaseTool` fills in `input_extensions` (the union of every mode's
`input_extensions`), `spec(mode)`, no-op `detect_output` / `post_convert`, the
extension-match `verifies_path` default, and the `embedded_hashes` default
(`[]`, `embedded_hash_is_exhaustive=False`), so a real plugin only overrides
what differs. See `app/services/tools/z3ds.py` for the
smallest complete example (~120 lines, mostly delegation). For one-shot
subprocess work (info / header / hash extraction), reuse the shared
`SubprocessRunner.run_capture()` (cancel/timeout-aware, applies the tool
nice/ioprio policy) rather than re-implementing the spawn loop — see
`app/services/dolphin_tool.py:disc_hashes` for the pattern.

`ConversionCancelled` is defined once in `app/services/subprocess_runner.py` and
re-exported through `services.chdman` and `services.tools.runner`. Import it,
don't define your own.

---

## 2. Two kinds of extension

### Scenario A: a new platform on an *existing* tool

The lightweight case: the binary already ships and the plugin already exists.
You add a mode string or an input extension.

Examples:
- A new chdman create variant. Add a `ConversionMode`, add a `ModeSpec` row to
  `ChdmanTool.modes`, teach the underlying `chdman.get_output_path_for_mode` the
  new suffix if it differs, and surface it in the UI registry.
- Letting chdman accept a new input extension: add it to
  `CHDMAN_CONVERTIBLE_EXTENSIONS` in `app/services/chdman.py` and to the
  relevant `ModeSpec.input_extensions`.

Go to **§4**.

### Scenario B: a brand-new tool (and usually a new platform with it)

A new binary, a new service module, a new plugin, a new `ConversionMode`, new
info/verify endpoints, and new UI wiring. This is the z3ds-shaped case.

Go to **§5**.

### Scenario C: a tool and a platform in tandem

The common real-world request: "support platform X, which needs new binary Y."
It is just **Scenario B**, because adding the tool is what makes the platform
reachable. The platform shows up as:

- the input extensions in `*_CONVERTIBLE_EXTENSIONS` and `ModeSpec.input_extensions`,
- the `ConversionMode` value(s) the tool exposes,
- a UI entry in the registry plus a primary-tool option,
- (optionally) platform-specific flags inside the service's `_build_command`.

Follow §5 end to end. §6 is the concrete tandem checklist.

---

## 3. Inventory: every file a tool/platform touches

This is the exhaustive list. Not every item is required for every change (the
right-hand column says when it applies), but check every row. The order is the
recommended implementation order (bottom of the stack to top). Deeper detail for
the non-obvious rows is in §8 to §14.

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
| 8 | `app/services/<tool>.py` | The underlying service: `_build_command`, the subprocess spawn, progress parsing, cancel handling, the `*_CONVERTIBLE_EXTENSIONS` set, `*_OUTPUT_FORMATS`, and the module singleton. | New tool |
| 9 | `app/services/tools/<tool>.py` | The plugin: a `BaseTool` subclass with `id`, `display_name`, `modes` (`ModeSpec` rows), `output_extensions`, `verify_extensions`, delegating `convert`/`verify`/`info`/`output_path`/`detect_output` to the service. Optionally override `embedded_hashes` (DAT-match fast path) / `embedded_hash_is_exhaustive`; the default falls back to file-level SHA1. | New tool |
| 10 | `app/services/tools/__init__.py` | One line: `registry.register(<Tool>(settings.<tool>_path))` (plus the import). This is the only dispatch wiring. | New tool |
| 11 | `app/models.py` | Add `ConversionMode` value(s); add a `<Tool>Info` model; add `FileEntry` flags (`<tool>_convertible`, `has_<tool>`, `<tool>_ready`, `<tool>_path`). | New mode and/or tool |
| 12 | `app/routes/convert.py` | Usually nothing for output paths or dispatch (the registry handles both). Add an extension-validation block in `plan_job` if your inputs need a specific check, mirroring the z3ds one. | New tool, sometimes |
| 13 | `app/routes/files.py` | Add the per-tool convertibility/output-existence flags in the directory scan **and** in `search_files`, paralleling the z3ds flags. | New tool |
| 14 | `app/routes/info.py` | A `GET /<tool>-info` endpoint, plus one `register_verify_routes(router, registry.get("<tool>"))` call to generate the verify trio. | New tool |
| 15 | `app/services/job_manager.py` | Usually nothing: convert and verify dispatch through the registry. Touch only for special post-processing (disc-id tagging, multi-file sidecars). | Rare |
| 16 | `app/services/disc_id.py` | Add a serial/title parser if the new **disc** platform should get GAME/NAME tags embedded (chdman create modes only). | New disc platform, optional |
| 17 | `app/services/archive.py` | Nothing for a new tool: archive input extensions come from `registry.archive_input_extensions()`. Set `allows_archive_input=True` on your `ModeSpec` to opt in. | Rare |
| 18 | `app/services/dat_*.py` / `app/routes/dat.py` | Touch only if the platform participates in DAT (MAMERedump) hash-matching. | Rare |
| 19 | `migrations/versions/*.py` | New Alembic migration **only** if you add DB-persisted columns/tables (use `scripts/new_migration.sh`). The verification/metadata stores are keyed by path and need no migration for a new tool. | If schema changes |

### 3.3 Frontend (Svelte 5 + Vite: one new entry in the registry)

| # | File | What you do | When |
|---|------|-------------|------|
| 20 | `src/lib/api/endpoints.js` | `get<Tool>Info`, `verify<Tool>`, `verifyBatch<Tool>` client methods alongside the existing ones. | New tool |
| 21 | `src/lib/tools/registry.js` | **One new entry** in the `TOOLS` array. Everything downstream (sidebar, workspace, badges, modals, verify dispatch, SSE URL building) looks up this registry. | New mode and/or tool |
| 22 | `src/styles/tokens.css` | Add a semantic token only if you need a new tool accent / badge color. Most tools reuse existing tokens via `accent: 'var(--badge-<token>)'`. Add it under **both** `:root` and `:root.dark`. | If new visual identity |
| 22a | `src/lib/util/fileIcon.js` | Single ext→icon map (`DISC_EXTS`/`GAME_EXTS`). Add your new file extensions to the right bucket so rows get a sensible icon. `FileRow.svelte` delegates to `iconForEntry()`, so this is the only place to edit. | New extensions |
| 22b | `src/lib/components/panels/FileRow.svelte` | The `convertibleBy` derived value has a **legacy fallback** that rebuilds the tool list from per-tool booleans (`entry.<tool>_convertible`). Add your `<tool>_convertible` line. | New tool |
| 22c | `src/lib/stores/conversion.svelte.js` | `defaultCompressionFor(toolId)` seeds the initial compression value per tool. Return `[]` for a tool with no compression UI (3DS); for a preset/codec dropdown, seed the default option (e.g. CSO returns `['max']`). This is also what the shared **Reset to default** button restores — declare it once here and the button works for your tool for free (it lives in `CompressionPicker.svelte`, gated on the tool having codecs, and calls `conversion.resetCompression()` / `isCompressionDefault`; no per-tool wiring). | New tool |
| 22d | `src/lib/components/views/HelpView.svelte` | The in-app Help page hard-codes the tool blurbs and the per-tool mode reference table. Add your tool + modes. | New tool (docs) |

### 3.4 Tests

| # | File | What you do | When |
|---|------|-------------|------|
| 23 | `tests/test_<tool>_routes.py` | Info + verify endpoint tests (copy `test_z3ds_routes.py`). | New tool |
| 24 | `tests/test_<tool>_service.py` | `convert`/`verify`/cancel/bad-extension tests (copy `test_z3ds_verification_service.py`). | New tool |
| 25 | `tests/test_tool_registry.py` | Assert your modes resolve to your tool and the spec flags are right. Bump the mode count, extend the legacy `_legacy_tool_for_mode` ladder, the `convertible_extensions` union, `tools_for_input`/`tool_for_verify` cases, and the `output_path` + `output_extensions` parametrize lists. | New tool/mode |
| 26 | `tests/test_mode_parity_fixes.py` | Add the mode so single-vs-batch validation parity is enforced; update the delete-on-verify error-message assertions if your compress mode supports it. | New mode |
| 26a | `tests/test_dispatch_routing.py` | Extend the legacy convert/verify dispatch ladder (`_legacy_dispatch_id`) and the patched-tool tuple so your modes route to your service. | New tool |
| 26b | `tests/test_files_outputs_parity.py` | Add your `<tool>_convertible/has_<tool>/<tool>_ready/<tool>_path` keys to `LEGACY_FILEENTRY_KEYS` **and** `LEGACY_SEARCH_KEYS` (they assert the exact legacy key surface). | New tool |
| 26c | `tests/test_archive_conversion_e2e.py` + `tests/test_archive_preference.py` | Add `MATRIX` rows per direction and assert your source exts are in `registry.archive_input_extensions()` (see §17.7). Make parametrize `ids` unique if an extension repeats across modes. | New archive-aware tool |
| 27 | `tests/conftest.py` | **No change for a tool binary.** `conftest.py` is DB-only; it does *not* stub tool binaries. Per-tool route/service tests define their own mocks (monkeypatch `info_routes.<service>` or `app.services.<tool>.asyncio.create_subprocess_exec`). | Rare |

### 3.5 CI / quality gates (must stay green)

| # | File | What it enforces | Action |
|---|------|------------------|--------|
| 28 | `.github/workflows/codacy.yml` | Push/PR/weekly: ruff, eslint, pylint, etc. via Codacy CLI. | New code must pass; see §11 |
| 29 | `.github/workflows/codeql.yml` | Push/PR/weekly CodeQL for Python **and** JS/TS. | Auto-covers new code |
| 30 | `.github/workflows/docker-image.yml` | On release: hadolint, multi-arch build, Trivy scan, SBOM/attestation. | Dockerfile must pass hadolint; see §8/§10 |
| 31 | `.github/workflows/label.yml` + `.github/labeler.yml` | PR auto-labeling by path. | Add a path glob if you want a label |
| 32 | `.github/dependabot.yml` | Dependency bumps. | Only if adding a new ecosystem |
| 33 | `pyproject.toml` | pylint + ruff config (Codacy reads this). | Respect line-length 100, py310 |
| 34 | `.pylintrc`, `.prospector.yaml`, `.bandit` | Python lint/security (bandit flags `shell=True`, predictable tmp). | Follow the no-shell rule (§15) |
| 35 | `eslint.config.js`, `.eslintrc.json`, `.jshintrc` | ESLint for the frontend (`src/`). | New JS and Svelte must pass |
| 36 | `.stylelintrc.json`, `.csslintrc` | CSS lint. | If you edit CSS |
| 37 | `.markdownlint.json` | Markdown lint. | If you add docs |
| 38 | `.codacy.yml`, `.codacy/` | Codacy tool config/excludes. | Rarely |
| 39 | `config/pmd/ruleset.xml` | PMD ruleset (Codacy). | Rarely |

### 3.6 Deployment & docs

| # | File | What you do | When |
|---|------|-------------|------|
| 40 | `docker-compose.yml` / `.cli.yml` / `.multi-volume.yml` | Document/override the new `<TOOL>_PATH` env or CLI mode env if relevant. | If ops needs the knob |
| 41 | `package.json` | The version lives here. A release bumps `package.json` and publishes a GitHub Release; there is no `.version` file or `sync-version.sh` script. | Every release |
| 42 | `README.md` | Supported-formats/tool table, feature list. Also the **Docker Hub** description (published from README by CI). | New tool/platform |
| 43 | `RELEASE_NOTES.md` | Changelog entry. | Every change |
| 44 | `DEPLOYMENT.md`, `DOCKER-COMPOSE.md` | New env vars / volumes / tool requirements. | If deploy surface changes |
| 45 | `AGENTS.md`, `.github/copilot-instructions.md` | Runbook / AI guidance, update if conventions change. | Optional |
| 46 | `app/main.py` | The FastAPI `description=` (shown at `/docs`) names the tools. Add the new tool so it stays accurate. | New tool |

---

## 4. Scenario A: add a platform/mode to an existing tool

Worked example: add a hypothetical **`createcd_audio`** chdman mode (pretend it
produces `.chd` with a platform-specific flag). The same pattern applies to any
new chdman/dolphin sub-format.

### 4.1 Add the mode to the enum: `app/models.py`

```python
class ConversionMode(str, Enum):
    ...
    CREATECD = "createcd"
    CREATECD_AUDIO = "createcd_audio"   # NEW
    ...
```

The string value is what travels over the API and what the registry indexes on.
Pick a value consistent with the tool's existing prefix convention (`create*`,
`extract*`, `dolphin_*`, `z3ds_compress`), because a few places still key off
these prefixes (see §15).

### 4.2 Add a `ModeSpec` row: `app/services/tools/chdman.py`

Add a `ModeSpec` to `ChdmanTool.modes`, modeled on the existing `createcd` row:

```python
ModeSpec(
    mode="createcd_audio", tool_id="chdman", kind=ModeKind.CREATE,
    label="Create CD CHD (Audio)", group="create",
    output_ext=".chd", input_extensions=CHDMAN_CONVERTIBLE_EXTENSIONS,
    supports_compression=True, supports_delete_on_verify=True,
    allows_archive_input=True,
),
```

Because `kind=CREATE` and `output_ext=".chd"`, the route validation, output-path
computation, archive-input handling, and delete-on-verify all work off this row
with no further code. Only touch the underlying service if the mode needs
special binary flags (branch in `chdman._build_command`) or a different output
suffix (`chdman.get_output_path_for_mode`).

### 4.3 Validation: `app/routes/convert.py`

Usually **nothing**. `convert.py` reads `registry.spec(mode)` and validates off
the spec flags (`spec.kind`, `spec.allows_archive_input`,
`spec.supports_delete_on_verify`). A new `create*` chdman mode inherits the
create-mode rules (for example the `.chd`-input rejection for `kind == CREATE`).
Add an explicit check only if your mode breaks a spec assumption.

### 4.4 Surface it in the UI: `src/lib/tools/registry.js`

Append one mode entry to the chdman descriptor's `modes` array (the group's
human label is already declared via `groups.create: 'Create'`):

```js
// src/lib/tools/registry.js, inside the chdman TOOLS entry
modes: [
  // …existing entries…
  { mode: 'createcd_audio', kind: 'create', label: 'Create CD CHD (Audio)',
    group: 'create',
    outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
    supportsCompression: true, supportsCompressionLevel: false,
    supportsDeleteOnVerify: true, allowsArchiveInput: true },
],
```

`CHDMAN_SOURCE_EXTS` is a local `const` in `registry.js` (not exported). Because
the mode reuses the chdman tool and `.chd` output, file badges, the conversion
config panel, the info modal, and the verify path all already work. The mode
shows up wherever `registry.modesByGroup('chdman')` or
`registry.specFor('createcd_audio')` is consulted.

### 4.5 Test

Add a case to `tests/test_mode_parity_fixes.py` (validation parity between single
+ batch create), `tests/test_tool_registry.py` (the mode resolves to chdman with
the right flags), and `tests/test_chdman_annotations.py` if relevant.

---

## 5. Scenario B/C: add a brand-new tool (with its platform)

Worked example throughout: a fictional **`nszip`** tool that compresses Nintendo
Switch `.nsp`/`.xci` dumps into `.nsz`/`.xcz`. Replace names as needed. This
mirrors z3ds exactly.

### 5.1 Install the binary: `Dockerfile`

Three patterns exist in the current `Dockerfile` (a three-stage build: `builder`,
`frontend-builder`, runtime). Pick the one that fits:

- **Distro package** (chdman via `mame-tools`, pinned to a `snapshot.debian.org`
  `.deb` with per-arch SHA256 checks).
- **Distro package, best-effort** (dolphin-emu, amd64-only, non-fatal). A wrapper
  script at `/usr/local/bin/dolphin-tool` is created only if the binary exists.
- **Build from source in the `builder` stage** (z3ds), then copy the artifact
  into the runtime image.

For `nszip` built from source, add to the `builder` stage:

```dockerfile
FROM debian:trixie-slim AS builder
RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      git build-essential libzstd-dev ca-certificates && \
    git clone https://github.com/example/nszip.git /tmp/nszip
WORKDIR /tmp/nszip
RUN g++ -O3 src/*.cpp -o nszip -lzstd && chmod +x nszip
```

…and copy it into the runtime stage next to the existing z3ds copy:

```dockerfile
COPY --from=builder /tmp/nszip/nszip /usr/local/bin/nszip
```

If the tool needs a runtime shared lib, add it to the runtime `apt-get install`
list. The `zstd` CLI is already installed (z3ds's `verify_stream` shells out to
`zstd -t`), so if your tool's output is a zstd container you can reuse it.

> Multi-arch: the image builds `linux/amd64` and `linux/arm64`. A source build
> compiles per arch automatically; a downloaded `.deb` needs a per-arch URL/SHA
> like the `mame-tools` block.

### 5.2 Add the binary path setting: `app/config.py`

Add next to the other tool paths (`chdman_path`, `dolphin_tool_path`,
`z3ds_compressor_path`):

```python
nszip_path: str = Field(
    default="/usr/local/bin/nszip", alias="NSZIP_PATH",
)
```

This lets ops override the path/env without code changes, and is what the plugin
passes to its service in `__init__`.

Optionally, add per-tool priority/timeout override fields so ops can diverge from
the shared `COMPRESSATORIUM_TOOL_*` policy for just your tool. They default to
`None` (fall back to the shared `tool_*` setting), and the `subprocess_runner`
helpers resolve them by `<owner>_<key>` (see §5.3). Mirror `nsz`/`z3ds`/`maxcso`:

```python
nszip_nice: int | None = Field(default=None, alias="COMPRESSATORIUM_NSZIP_NICE")
nszip_ioprio_class: int | None = Field(default=None, alias="COMPRESSATORIUM_NSZIP_IOPRIO_CLASS")
nszip_ioprio_level: int | None = Field(default=None, alias="COMPRESSATORIUM_NSZIP_IOPRIO_LEVEL")
nszip_verify_timeout: int | None = Field(default=None, alias="COMPRESSATORIUM_NSZIP_VERIFY_TIMEOUT")
# add nszip_info_timeout only if the tool's info() runs a subprocess (chdman/Dolphin do; nsz/z3ds/maxcso don't)
```

### 5.3 Write the service + the plugin

There are two files. The **service** (`app/services/nszip.py`) owns the
subprocess: build the command, spawn it, parse progress, handle cancel. Copy
`app/services/z3ds_compress.py` and adapt. The **plugin**
(`app/services/tools/nszip.py`) is a thin `BaseTool` subclass that holds the
`ModeSpec` rows and delegates to the service. Copy `app/services/tools/z3ds.py`.

The service skeleton:

```python
import asyncio, contextlib, logging, os, shutil, threading, time
from collections.abc import AsyncGenerator
from pathlib import Path

from config import settings
from services.subprocess_runner import ConversionCancelled   # reuse the shared exception
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
        # The shared SubprocessRunner owns the priority policy; pass your tool's
        # owner id so a per-tool override (COMPRESSATORIUM_NSZIP_IOPRIO_*) can
        # diverge from the shared COMPRESSATORIUM_TOOL_IOPRIO_* default.
        cmd = ioprio_prefix("nszip") + cmd
        return cmd

    def active_pids(self):
        with self._pid_lock:
            return list(self._active_pids)

    async def convert(self, input_path, output_path, mode="nszip_compress",
                      *, compression=None, cancel_event=None) -> AsyncGenerator[dict, None]:
        # 1. mkdir -p output dir
        # 2. build cmd, spawn with asyncio.create_subprocess_exec (NEVER shell=True)
        # 3. apply the shared nice level via preexec_fn (posix only):
        #    `from services.subprocess_runner import apply_nice` then
        #    `apply_nice("nszip")` inside the preexec callback
        # 4. stream stdout, parse progress, yield {"progress": int, "message": str}
        # 5. honor cancel_event -> terminate/kill, clean partial output,
        #    raise ConversionCancelled
        # 6. apply a stall timeout via compute_progress_stall_timeout(...)
        # 7. on non-zero exit raise RuntimeError(tail of output)
        # 8. finally yield {"progress": 100, "message": "Compression complete"}
        ...

    async def verify(self, path) -> dict: ...
    async def verify_stream(self, path) -> AsyncGenerator[dict, None]: ...
    def info(self, path) -> dict: ...

    def get_output_path(self, input_path, output_dir=None) -> str:
        input_p = Path(input_path)
        out_ext = NSZIP_OUTPUT_FORMATS.get(input_p.suffix.lower(), ".nsz")
        filename = f"{input_p.stem}{out_ext}"
        return str(Path(output_dir) / filename) if output_dir else str(input_p.parent / filename)


nszip_service = NszipService()   # module-level singleton
```

The plugin skeleton (copy `app/services/tools/z3ds.py`):

```python
from fastapi.concurrency import run_in_threadpool
from models import NszipInfo, OutputStatus
from services.nszip import (
    NSZIP_CONVERTIBLE_EXTENSIONS, NSZIP_OUTPUT_FORMATS, nszip_service,
)
from .base import BaseTool
from .spec import ModeKind, ModeSpec


class NszipTool(BaseTool):
    id = "nszip"
    display_name = "Switch"
    modes = (
        ModeSpec(
            mode="nszip_compress", tool_id="nszip", kind=ModeKind.COMPRESS,
            label="Compress to NSZ/XCZ", group="nszip",
            output_ext=None,  # mapped from the input extension
            input_extensions=frozenset(NSZIP_CONVERTIBLE_EXTENSIONS),
            supports_delete_on_verify=True,
        ),
    )
    output_extensions = frozenset(NSZIP_OUTPUT_FORMATS.values())
    verify_extensions = output_extensions

    def __init__(self, binary_path):
        super().__init__(binary_path)

    def output_path(self, mode, input_path, output_dir=None, *, treat_as_stem=False):
        return nszip_service.get_output_path(input_path, output_dir)

    def convert(self, input_path, output_path, mode, *, compression=None, cancel_event=None):
        return nszip_service.convert(input_path, output_path, mode,
                                     compression=compression, cancel_event=cancel_event)

    async def verify(self, path): return await nszip_service.verify(path)
    def verify_stream(self, path): return nszip_service.verify_stream(path)
    async def info(self, path): return await run_in_threadpool(nszip_service.info, path)
    def info_model(self, raw, path): return NszipInfo(**raw)
    def active_pids(self): return nszip_service.active_pids()
```

**Critical service rules** (all enforced by the existing services, copy them):

- **Never** use `shell=True`. Build an argv list and use
  `asyncio.create_subprocess_exec(cmd[0], *cmd[1:], ...)`. The binary path comes
  from validated settings; inputs are never shell-interpreted.
- **Always** support `cancel_event`: spawn a watcher task that
  `process.terminate()`s (then `kill()`s after a timeout), clean up the partial
  output file, and raise `ConversionCancelled`. See `z3ds_compress.py`.
- **Always** apply a stall timeout via `compute_progress_stall_timeout(...)` so a
  wedged binary can't hang a job forever.
- **Progress** is a 0 to 100 int. If the binary doesn't report percentages,
  estimate from output-file growth like z3ds does, or emit an elapsed-seconds
  heartbeat via the shared `SubprocessRunner` like dolphin does.
- Respect the shared priority policy via the `services.subprocess_runner`
  helpers (`ioprio_prefix(owner)`, `nice_prefix(owner)`, `apply_nice(owner)`,
  `info_timeout(owner)`, `verify_timeout(owner)`). These read the tool-neutral
  `COMPRESSATORIUM_TOOL_*` settings (with optional per-tool
  `COMPRESSATORIUM_<TOOL>_*` overrides) in one place, so you never re-read a
  chdman-named setting in your service.
- Track PIDs so the debug heartbeat can report them.

### 5.4 Register the plugin: `app/services/tools/__init__.py`

This is the single dispatch wiring step. Add the import and one `register` call:

```python
from .nszip import NszipTool
...
registry.register(NszipTool(settings.nszip_path))
```

Once registered, `job_manager` and `convert.py` dispatch your mode through the
registry with no further edits. The registry validates on register: duplicate
ids or mode names raise, and every `ModeSpec.tool_id` must equal the tool's `id`.

### 5.5 Add the mode + info model: `app/models.py`

```python
class ConversionMode(str, Enum):
    ...
    Z3DS_COMPRESS = "z3ds_compress"
    NSZIP_COMPRESS = "nszip_compress"        # NEW
    ...

class NszipInfo(BaseModel):                  # NEW, shape it like Z3DSInfo
    file: str
    size: int
    size_display: str
    format: str | None = None
    extension: str
    compressed: bool
    compression_type: str | None = None
```

Every `ModeSpec.mode` must equal a `ConversionMode` value: the route layer
validates the incoming `request.mode` against `ConversionMode`, so an unlisted
mode never reaches dispatch. Keep a consistent prefix (`nszip_`) if you add a
second mode (for example `NSZIP_EXTRACT = "nszip_extract"`).

### 5.6 The job pipeline: usually nothing

`job_manager._process_job` dispatches convert and verify generically:
`registry.for_mode(job.mode.value).convert(...)` and
`registry.for_mode(job.mode.value).verify(...)`. `_queue_job_locked` computes the
output path via `registry.for_mode(mode.value).output_path(...)`. So once your
plugin is registered, **the pipeline needs no edits** for a standard
compress-style tool.

Touch `job_manager` only for special post-processing. The current special cases
are chdman-specific: GAME/NAME disc-id tagging after `createcd`/`createdvd`, and
`.bin` sidecar handling for multi-track CD inputs. A clean single-file tool like
nszip needs none of that.

### 5.7 Validation + output dispatch: `app/routes/convert.py`

Output paths and the generic spec-flag validation are handled by the registry,
so you usually add **only** an extension-validation block. `plan_job` validates
the input extension per tool; mirror the z3ds block (which checks
`spec.tool_id == "z3ds"` and the input extension against the spec). For nszip:

```python
if spec.tool_id == "nszip":
    ext = Path(file_path).suffix.lower()
    if ext not in spec.input_extensions:
        raise SkipFile(SkipReason.NSZIP_BAD_EXTENSION)   # add the reason to the enum
```

Everything else is spec-driven:

- **Output path:** `_get_output_path` delegates to
  `registry.for_mode(mode).output_path(...)`. No branch to add.
- **Delete-on-verify:** `supports_delete_on_verify(mode)` reads
  `registry.spec(mode).supports_delete_on_verify`. Set the flag on your
  `ModeSpec`, not in `convert.py`.
- **Archive inputs:** the single guard in `plan_job` rejects archive (`::`)
  members for any mode whose `spec.allows_archive_input` is `False` (the
  default). Set it `True` only when the input is a convertible *source*, and the
  archive listing surfaces your members automatically via
  `registry.archive_input_extensions()`. Leave it `False` when the input is an
  *output* class (like chdman extract/copy on `.chd`).

### 5.8 Mark inputs convertible in listings: `app/routes/files.py`

`files.py` annotates each file in directory listings and search results with
per-tool convertibility and output-existence flags so the UI can badge rows and
gate selection. It imports the registry and computes per-tool flags (today
`is_chd_convertible` / `is_dolphin_convertible` / `is_z3ds_convertible` plus
`has_*` output-existence checks).

1. Add the model fields. In `app/models.py` `FileEntry`, add
   `nszip_convertible: bool = False` and, for "output already exists" badges,
   `has_nszip: bool = False`, `nszip_ready: bool = False`,
   `nszip_path: str | None = None`, paralleling the z3ds fields.
2. In the directory scan, compute `is_nszip_convertible` from the extension and
   an output-existence check (use your plugin's `detect_output(...)` or an
   inline check modeled on the z3ds path), then set them on the `FileEntry`.
3. Do the same in `search_files`, both in the "is this file interesting" test
   and where it records the flags.

### 5.9 Info + verify endpoints: `app/routes/info.py`

Verify endpoints are **factory-generated**. You don't hand-write them, but there
are **two** edits, not one:

1. **Add a `_VERIFY_CONFIG["<tool>"]` entry** (a `_VerifyRouteConfig` dataclass) near
   the top of `info.py`. It carries the per-tool facts the factory has no other way
   to know: `url_prefix` (e.g. `"cso-"` → `/api/cso-verify`; `""` for chdman),
   `service=lambda: <service_singleton>` (resolved at call time so tests can rebind
   it), the three FastAPI route names (`verify_<tool>` / `_events` / `_batch_events`),
   `bad_ext_detail` (the 400 message), and `verify_error_prefix` (the 500 message).
   The factory reads accepted extensions off `tool.verify_extensions`, so there's no
   per-tool extension constant here.
2. **Add the (2-arg) register call** alongside the existing tool registrations. The
   config is looked up from `_VERIFY_CONFIG` by `tool.id`; it is **not** passed in:

```python
verify_cso, verify_cso_events, verify_cso_batch_events = register_verify_routes(
    router, registry.get("cso"),
)
```

`register_verify_routes(router, tool)` generates the trio (`/api/<tool>-verify`,
`/api/nszip-verify/events`, `/api/nszip-verify-batch/events`) from the plugin's
`verify_extensions`, acquires the global verify lane
(`_acquire_verify_lane_or_429`, bounded by `workload_limiter`), and calls
`verification_store.mark_verified(path)` on success. No per-tool extension
constants are needed; the factory reads them off the plugin.

Info is hand-written per tool. Add a `GET /api/nszip-info` endpoint modeled on
`get_z3ds_info`, returning your `NszipInfo` model, plus its small
`_is_<tool>_info_file(path)` guard and `<TOOL>_INFO_EXTENSIONS` constant (mirror
the z3ds/nsz pair just above the endpoints). No router registration is needed;
`info.router` is already mounted under `/api` in `app/main.py`. Also import your
service singleton at the top of `info.py` so the `_VERIFY_CONFIG` lambda can
resolve it by module global.

### 5.10 Frontend: `src/lib/api/endpoints.js`

Add client methods on the `api` object next to the z3ds ones. The naming
convention is `get<Tool>Info` / `verify<Tool>` / `verifyBatch<Tool>`:

- `getNszipInfo(path)`, like `getZ3DSInfo`.
- `verifyNszip(path, {onProgress})`, single-file SSE. Routes through
  `verifyEventSource` from `sse.js`.
- `verifyBatchNszip(paths, {onProgress, onFileComplete, signal})`, batch SSE.
  Routes through the module-local `runBatchVerify` helper, which wraps
  `sseFetchPost` from `sseFetch.js`.

`createJob` / `createBatchJobs` are generic over `mode`, so no change there: the
registry's submit path passes `mode: 'nszip_compress'`. The verify URL is
**derived automatically** from `verifyPrefix: 'nszip'` in the registry
descriptor; there's no URL map to edit.

### 5.11 Frontend: `src/lib/tools/registry.js`

The frontend is a **Svelte 5 + Vite SPA** under `src/`, driven by a single
declarative tool registry (`src/lib/tools/registry.js`). The registry is the only
place that knows tool identity; there are no `if (tool === ...)` branches
downstream. Adding `nszip` is **one new entry** appended to the `TOOLS` array:

```js
// src/lib/tools/registry.js
{
  id: 'nszip',
  label: 'Switch',
  hint: 'Compress Nintendo Switch dumps (NSP / XCI).',
  // URL segment for /api/{prefix}-verify and /api/{prefix}-verify-batch
  verifyPrefix: 'nszip',
  sourceExts: ['.nsp', '.xci'],
  verifyExts: ['.nsz', '.xcz'],
  modeGroups: ['nszip'],
  groups: { nszip: 'Nintendo Switch' },   // human labels for the tool's group ids
  defaultMode: 'nszip_compress',
  glyph: 'NSW',                           // 2-3 char affordance for sidebar / dashboard
  accent: 'var(--badge-dat-match)',       // CSS color or token
  // compressionStyle / compressionCodecs only if the tool exposes codec choices
  // (chdman uses 'multi', dolphin 'single-with-level'); omit for a fixed compressor.
  modes: [
    { mode: 'nszip_compress', kind: 'compress', label: 'Compress to NSZ/XCZ',
      group: 'nszip',
      outputExt: null,                    // mapped from input extension
      inputExtensions: ['.nsp', '.xci'],
      supportsCompression: false,
      supportsCompressionLevel: false,
      supportsDeleteOnVerify: true,
      allowsArchiveInput: false },
  ],
  getInfo:     (path) => api.getNszipInfo(path),
  verify:      (path, opts) => api.verifyNszip(path, opts),
  verifyBatch: (paths, opts) => api.verifyBatchNszip(paths, opts),
  productPath: (path) => path.replace(/\.nsp$/i, '.nsz').replace(/\.xci$/i, '.xcz'),
},
```

That's the entire frontend change. The tool fields above are the real schema;
chdman additionally carries `prefix`, and tools with codec choices carry
`compressionStyle` / `compressionCodecs`. You do **NOT** need to:

- Edit a `VERIFY_URL` map. `registry.verifyUrl(toolId, kind)` derives both single
  and batch URLs from `verifyPrefix` (`''` for chdman, `'<segment>'` otherwise).
- Edit a `VALID_TOOLS` set. `ui.svelte.js` reads `registry.ids()`.
- Edit a `groupLabel()` switch. Group labels live on `tool.groups`; the lookup is
  `registry.groupLabel(group, toolId)`.
- Edit the Sidebar, Workspace, file list, conversion config, or any modal. They
  call `registry.specFor(mode)`, `registry.forTool(id)`,
  `registry.modesByGroup(id)`, `registry.toolForVerifyPath(path)`, and so on.

> **Build step.** The SPA compiles with Vite. Run `npm run dev` for HMR against
> the FastAPI backend, or `npm run build` to emit `static/index.html` +
> `static/assets/*` (used by FastAPI in production and by the Docker
> `frontend-builder` stage). When editing any `.svelte` file, run it through the
> Svelte MCP `svelte-autofixer` per the project contract.

### 5.12 Tests

Add, modeled on the existing suites:

- `tests/test_nszip_routes.py`, info + verify endpoint behavior (copy
  `tests/test_z3ds_routes.py`).
- `tests/test_nszip_service.py`, `convert`/`verify` happy path, cancel path,
  bad-extension rejection (copy `tests/test_z3ds_verification_service.py`).
- Extend `tests/test_tool_registry.py` so your mode resolves to your tool with
  the right spec flags.
- Extend `tests/test_mode_parity_fixes.py` so single-job and batch-job validation
  stay in lockstep for the new mode.

Run them:

```bash
# from the repo root
pytest -q tests/test_nszip_routes.py tests/test_tool_registry.py tests/test_mode_parity_fixes.py
```

`tests/conftest.py` stubs the binaries, so tests don't need the real tool
installed.

### 5.13 Docs + version

- Update `README.md` (tool table / supported formats) and `RELEASE_NOTES.md`.
- Update the FastAPI app description in `app/main.py` (the `description=` shown
  at `/docs`) so it names the new tool. It lists every tool, so keep it current.
- The version lives in `package.json`. A release bumps it and publishes a GitHub
  Release tagged `vX.Y.Z`, which is what triggers the image build (see §10).

---

## 6. Tandem checklist (copy/paste)

A new tool **is** a new platform, so do these together:

```
BINARY
[ ] Dockerfile: build/install binary into runtime image (+ runtime libs)
[ ] config.py: add <tool>_path Field with env alias

SERVICE (app/services/<tool>.py)
[ ] *_CONVERTIBLE_EXTENSIONS, *_OUTPUT_FORMATS, module-level singleton
[ ] convert(): exec (no shell), progress yields, cancel_event, stall timeout,
    nice/ionice, PID tracking, ConversionCancelled, final 100%
[ ] verify() + verify_stream(), info(), get_output_path()

PLUGIN (app/services/tools/<tool>.py)
[ ] BaseTool subclass: id, display_name, modes (ModeSpec rows),
    output_extensions, verify_extensions
[ ] delegate convert/verify/verify_stream/info/info_model/output_path/active_pids
[ ] optional: detect_output() for "output exists" badges
[ ] optional: verifies_path() to refine verify_extensions per-file when the
    tool over-claims a container extension (drives FileEntry.verifiable_by)
[ ] optional: embedded_hashes()/embedded_hash_is_exhaustive for the DAT-match
    fast path (default falls back to file-level SHA1)
[ ] optional: allows_archive_input=True on source modes (both registries) — see §17

REGISTER (app/services/tools/__init__.py)
[ ] registry.register(<Tool>(settings.<tool>_path))

MODE + MODELS (app/models.py)
[ ] ConversionMode.<TOOL>_<ACTION> with a consistent prefix
[ ] <Tool>Info model (if it has an info modal)
[ ] FileEntry: <tool>_convertible / has_<tool> / <tool>_ready / <tool>_path

ROUTES
[ ] convert.py: SkipReason.<TOOL>_BAD_EXTENSION + _SKIP_HTTP entry + is_<tool>
    flag + extension-validation block in plan_job
[ ] files.py: compute flags in the directory scan + search_files
[ ] info.py: _VERIFY_CONFIG["<tool>"] entry + register_verify_routes(...) +
    /<tool>-info + _is_<tool>_info_file + service import

PIPELINE (app/services/job_manager.py)
[ ] usually nothing (registry dispatches convert + verify)
[ ] only for special post-processing (disc-id tags, multi-file sidecars)

FRONTEND
[ ] src/lib/api/endpoints.js: get<Tool>Info, verify<Tool>, verifyBatch<Tool>
[ ] src/lib/tools/registry.js: one new entry in the TOOLS array
[ ] src/lib/util/fileIcon.js + FileRow.svelte: ext→icon + convertibleBy legacy line
[ ] src/lib/stores/conversion.svelte.js: defaultCompressionFor branch
[ ] src/lib/components/views/HelpView.svelte: tool blurb + mode table rows
[ ] src/styles/tokens.css: --badge-<tool> (only if new accent), both :root + .dark

TESTS + DOCS
[ ] tests/test_<tool>_routes.py, tests/test_<tool>_service.py, registry, mode-parity
[ ] tests/conftest.py: binary stub fixture
[ ] README / RELEASE_NOTES / package.json version bump
[ ] app/main.py: add the tool to the FastAPI description= string (shown at /docs)

PERIPHERAL (don't forget)
[ ] .dockerignore: confirm nothing new is excluded
[ ] requirements.txt: new pip deps (if any)
[ ] .local-bin/<binary>: local-dev copy (from-source tools)
[ ] entrypoint.sh: CLI-mode loop (only if headless batch support wanted)
[ ] disc_id.py: serial/title parser (only for new disc platforms w/ tagging)
[ ] migrations/: new Alembic rev (only if persisting new columns/tables)
[ ] docker-compose*.yml + DEPLOYMENT.md + DOCKER-COMPOSE.md: env/volume docs
[ ] lint clean: ruff, pylint, eslint, hadolint (Dockerfile), markdownlint
[ ] CI awareness: hadolint + Trivy gate the release build; CodeQL scans new code
```

---

## 8. The Docker image in depth: `Dockerfile`, `.dockerignore`, deps

The image is a **three-stage** multi-arch build on `debian:trixie-slim`: a
`builder` stage (compiles from-source binaries), a `frontend-builder` stage on
`node:lts-slim` (runs `npm ci && npm run build` for the Svelte UI), and the
runtime stage (no Node).

**Build args & arch.** `ARG TARGETARCH` (set in the runtime stage) is
`amd64`/`arm64`. A from-source build (g++) compiles per-arch automatically. A
downloaded `.deb`/binary needs a **per-arch URL + SHA256**, like the `mame-tools`
block which branches on `TARGETARCH` and verifies with `sha256sum -c`. Reproduce
that pattern for any pinned download.

**Builder stage:** install toolchain (`build-essential`, `libzstd-dev`, …),
`git clone`, compile, `chmod +x`. Add your build here.

**Runtime stage:**
- Add any runtime shared libraries to the `apt-get install` list. The CLI `zstd`
  is already present (z3ds verify uses `zstd -t`); `ionice`/`nice` come from
  `util-linux`.
- `COPY --from=builder /tmp/<tool>/<binary> /usr/local/bin/<binary>` next to the
  z3ds copy.
- Optional/best-effort installs (a tool that only exists on one arch) should
  follow the dolphin-emu pattern: install with `|| echo WARNING` and create a
  wrapper only `if command -v` succeeds. The service must then tolerate a missing
  binary gracefully (surface a clear runtime error).
- The image runs as **uid/gid 999 (`converter`)**. Install to a world-readable
  path (`/usr/local/bin`) and `chmod +x`.
- `HEALTHCHECK` only probes the web UI; no change needed.

**hadolint** runs in CI (`docker-image.yml` `lint` job) with
`failure-threshold: error` and `ignore: DL3008,DL3013`. Keep your `RUN` layers
clean (combine `apt-get update && install`, `rm -rf /var/lib/apt/lists/*`, pin
where the existing file pins).

**`.dockerignore`:** `app/`, `static/`, `migrations/`, `entrypoint.sh`,
`requirements.txt` are copied in. `tests/` and most `*.md` (except `README.md`)
are excluded, so this guide and your service tests never enter the image. If you
reference a new top-level file from the Dockerfile, confirm it isn't ignored.

**Python deps:** anything your service `import`s that isn't stdlib or already in
`requirements.txt` must be added there (picked up by `pip install -r` in the
runtime stage). Dev/test-only deps go in `requirements-dev.txt`.

**Local dev (`run_dev.sh`):** it bootstraps a `.venv` and runs uvicorn against
your host. From-source binaries are kept in `.local-bin/` (the repo ships
`.local-bin/z3ds_compressor`) so the app works without Docker. Drop your compiled
binary there and point `<TOOL>_PATH` at it (or add it to `PATH`) for local
testing.

---

## 9. Headless CLI mode: `entrypoint.sh`

`entrypoint.sh` supports `CHD_MODE=cli` (batch-convert a volume with no web UI;
see `docker-compose.cli.yml`). This path is **independent of the Python app**: it
shells out to `chdman` directly and currently only knows `createcd`/`createdvd`
over `*.gdi *.iso *.cue`, validating `CHDMAN_MODE` against that allow-list with a
`case` statement.

If your new tool/platform should be usable headlessly:
- Extend the `CHDMAN_MODE` case statement (or add a parallel `CONVERT_TOOL` env)
  to accept your mode.
- Add a globbing loop for your input extensions and invoke your binary, mirroring
  the existing `for i in *.gdi *.iso *.cue` block.
- Skip-if-output-exists logic (`[[ -e "${i%.*}.<ext>" ]]`) should match your
  output suffix.

If headless mode is out of scope, skip this: the web UI path (`exec uvicorn
main:app …`) already dispatches your tool via the Python pipeline.

The same file handles **PUID/PGID remap** and the privilege drop to `converter`
(`exec gosu converter`). No changes needed there.

---

## 10. CI/CD & GitHub workflows

| Workflow | Trigger | Relevance to a new tool |
|----------|---------|-------------------------|
| `docker-image.yml` | **release published** | Builds/pushes multi-arch image to Docker Hub + GHCR, runs **hadolint** (Dockerfile must pass), **Trivy** CRITICAL/HIGH scan (a vulnerable new dep can fail this), generates SBOM + provenance attestation, and **publishes `README.md` as the Docker Hub description**. Also syncs `package.json` from the release tag. |
| `codacy.yml` | push, PR, weekly | Runs the Codacy CLI (ruff, eslint, pylint, bandit, etc.). New Python/JS must lint clean. Honors `pyproject.toml`, `.pylintrc`, `eslint.config.js`, `.codacy.yml`. |
| `codeql.yml` | push, PR, weekly | CodeQL SAST for **python** and **javascript-typescript**. New code is scanned automatically, no config change, but don't introduce flagged patterns (for example command injection, another reason for the no-`shell=True` rule). |
| `label.yml` + `labeler.yml` | PR | Path-based auto-labels. Add a glob if you want your area labeled. |
| `stale.yml` | schedule | Marks stale issues/PRs. Irrelevant. |
| `dependabot.yml` | schedule | Dependency PRs. Add an ecosystem entry only if you introduce a new manifest. |

Key takeaways for a contributor:
- **The image only builds on a GitHub Release.** There is no build-on-push. To
  ship your tool you (or a maintainer) publish a release tagged `vX.Y.Z`; CI then
  builds `linux/amd64,linux/arm64` and tags `latest`/`beta` accordingly.
- **Trivy can block a release** if a new system/python package pulls a
  CRITICAL/HIGH CVE (`ignore-unfixed: true` softens this). Prefer pinned, patched
  packages; the `mame-tools` snapshot pin is the model.
- **Dockerfile changes must pass hadolint** locally before you push
  (`hadolint Dockerfile`).

---

## 11. Lint & quality gates you must satisfy

Run these before pushing (they mirror Codacy):

```bash
# from the repo root
ruff check app tests            # style/lint, config in pyproject.toml ([tool.ruff], py310)
pylint app                      # config in .pylintrc / pyproject.toml ([tool.pylint])
pytest -q tests                 # full suite (binaries are stubbed in conftest)
npm run lint                    # ESLint over JS + .svelte, eslint.config.js
hadolint Dockerfile             # if you touched the Dockerfile
```

Conventions baked into the configs that affect new tool code:

- **Line length 100** (`pyproject.toml [tool.pylint.format]`, ruff `py310`).
- **`bandit`** (`.bandit`) flags `subprocess` with `shell=True` and predictable
  temp paths. The services use `create_subprocess_exec` with argv lists and
  document the temp-dir reasoning in `config.py`. Follow suit or bandit/CodeQL
  will flag you.
- **Broad `except Exception`** at subprocess boundaries is allowed by
  `.pylintrc`/`pyproject.toml` *if* you log at the call site (review enforces
  this). The existing services do `logger.exception(...)`.
- **Import order** is owned by ruff/isort (pylint's `wrong-import-order` is
  disabled). Keep imports sorted.
- Markdown is linted by `.markdownlint.json`; CSS by `.stylelintrc.json`.

---

## 12. Database, migrations & persistence

The app uses **SQLite via SQLAlchemy + Alembic** (`app/services/db.py`,
`migrations/`). On startup, the `lifespan` handler in `app/main.py` calls
`apply_migrations()` (defined in the db module) to bring the schema to head and
then imports any legacy JSON stores.

For a **new tool**, you almost certainly need **no migration**, because the
persistence layers are generic over file paths:

- **`verification_store`** records "this output path was verified" keyed by path.
  Your verify endpoints call `verification_store.mark_verified(path)` and it just
  works for any extension.
- **`chd_metadata_store`** caches CHD-specific metadata; a non-CHD tool doesn't
  use it.

You need a migration **only** if you add new persisted columns/tables (for
example a tool-specific metadata cache). In that case:

```bash
./scripts/new_migration.sh "add nszip metadata table"
# edit the generated migrations/versions/000X_*.py (upgrade/downgrade)
pytest -q tests/test_alembic_migrations.py tests/test_db_migration.py
```

`migrations/env.py` and `migrations/versions/0001_baseline_schema.py` are the
references. `tests/test_alembic_migrations.py` validates head consistency.

---

## 13. Supporting services: disc_id, archive, DAT

These are **disc-format-specific** and usually irrelevant to a non-disc tool, but
matter when your *platform* is a disc image processed by chdman.

- **`app/services/disc_id.py`** extracts a game serial/title (for example
  `SLUS-20312`) from PS1/PS2/PSP/Dreamcast sources and embeds GAME/NAME tags into
  the CHD after `createcd`/`createdvd` (driven from `job_manager._process_job`).
  To make a **new disc platform** get tags, add a parser branch in
  `extract_from_source` and a normalizer like `_normalize_ps_serial`. Not needed
  for RVZ/3DS/etc.
- **`app/services/archive.py`** lists the inputs that can be converted from
  inside a `.zip/.7z/.rar`. It no longer hardcodes a tool-specific set:
  `ArchiveService` reads `registry.archive_input_extensions()` (the union of
  `input_extensions` for every mode with `allows_archive_input=True`), with the
  module-level `CONVERTIBLE_EXTENSIONS` kept only as a fallback. So once your
  mode opts in via `allows_archive_input=True`, its members are surfaced
  automatically; there's nothing to edit here. You still own extraction +
  related-file handling in `job_manager._process_job` for multi-file formats
  (single-file inputs like `.3ds`/`.rvz` need nothing extra). Output paths for
  archive members flow through `archive_service._output_name_for_member`, which
  preserves the original extension so input-derived outputs (for example z3ds
  `.3ds` to `.z3ds`) map correctly. **§17** walks the whole archive-input path
  end to end.
- **DAT / MAMERedump** (`app/services/dat_*.py`, `app/routes/dat.py`,
  `app/services/file_hasher.py`) is for hash-matching dumps against DAT
  databases. Touch only if your platform participates in DAT verification.

---

## 14. End-to-end "in tandem" example map

For the `nszip` (Nintendo Switch) tandem example used in §5, here is every edit
on one screen:

```
Dockerfile                          builder: clone+g++ nszip; runtime: COPY binary, libs
.dockerignore                       (verify nothing new is excluded)
requirements.txt                    (only if service imports a new pip pkg)
.local-bin/nszip                    local-dev binary for run_dev.sh
entrypoint.sh                       (optional) CLI-mode loop for .nsp/.xci
app/config.py                       nszip_path Field (NSZIP_PATH)
app/services/nszip.py               NszipService + NSZIP_CONVERTIBLE_EXTENSIONS + singleton
app/services/tools/nszip.py         NszipTool plugin (BaseTool + ModeSpec rows)
app/services/tools/__init__.py      registry.register(NszipTool(settings.nszip_path))
app/models.py                       ConversionMode.NSZIP_COMPRESS; NszipInfo; FileEntry flags
app/routes/convert.py               extension-validation block in plan_job (if needed)
app/routes/files.py                 flags in the directory scan + search_files
app/routes/info.py                  _VERIFY_CONFIG entry + register_verify_routes(get("nszip")) + /nszip-info + _is_nszip_info_file + service import
app/routes/convert.py               SkipReason + _SKIP_HTTP + is_<tool> flag + plan_job validation
app/services/job_manager.py         usually nothing (registry dispatches)
src/lib/api/endpoints.js            getNszipInfo, verifyNszip, verifyBatchNszip
src/lib/tools/registry.js           one new entry in TOOLS
src/lib/util/fileIcon.js            add new exts to DISC_EXTS/GAME_EXTS
src/lib/components/panels/FileRow.svelte   icon ext list + convertibleBy legacy line
src/lib/stores/conversion.svelte.js defaultCompressionFor branch (return [] for no-codec)
src/lib/components/views/HelpView.svelte   tool blurb + mode reference rows
src/styles/tokens.css               (only if new badge classes; both :root + .dark)
tests/test_nszip_routes.py          info+verify endpoint tests
tests/test_nszip_service.py         convert/verify/cancel/bad-ext tests
tests/test_tool_registry.py         mode resolves to nszip; counts + ext unions + matrices
tests/test_dispatch_routing.py      extend convert/verify legacy dispatch ladder
tests/test_files_outputs_parity.py  add <tool>_* keys to LEGACY_*_KEYS sets
tests/test_mode_parity_fixes.py     add nszip_compress to the parity matrix
tests/test_archive_conversion_e2e.py + test_archive_preference.py  archive matrix (if archive-aware)
tests/conftest.py                   DB-only; no tool-binary stub (mocks live per-test-file)
docker-compose*.yml                 (optional) NSZIP_PATH / CLI env docs
package.json                        version bump (release)
app/main.py                         add the tool to the FastAPI description= (shown at /docs)
README.md / RELEASE_NOTES.md        supported-formats table + changelog
DEPLOYMENT.md / DOCKER-COMPOSE.md    new env var, if any
```

---

## 15. Gotchas & conventions

- **The registry dispatches; don't add ladders.** Convert and verify run through
  `registry.for_mode(mode)`; output paths through the plugin's `output_path()`;
  validation through `registry.spec(mode)` flags. The old `_convert_service`
  if/elif ladders are gone. Add behavior by setting `ModeSpec` flags, not by
  branching on the mode string.
- **A few prefixes are still load-bearing.** Some code still branches on
  `mode.startswith("create"|"extract"|"dolphin_")` and on equality with
  `z3ds_compress` for special-casing (disc-id tagging, `.bin` sidecars,
  compression nuances). Choose a clear, unique prefix and grep for prefix checks
  when you add a mode that doesn't fit an existing family.
- **Single-job and batch endpoints must validate identically.** `create_job` and
  `create_batch_jobs` in `convert.py` share `plan_job`, and
  `tests/test_mode_parity_fixes.py` exists to catch drift. Keep them in lockstep.
- **The verify lane is global.** All verification across all tools shares
  `MAX_VERIFY_CONCURRENCY` via `workload_limiter`. The verify-route factory
  acquires the token for you (`_acquire_verify_lane_or_429`); don't bypass it.
- **Reuse `ConversionCancelled`.** It lives in
  `app/services/subprocess_runner.py` (re-exported via `services.chdman` and
  `services.tools.runner`). The pipeline's cancel handling in
  `job_manager._process_job` catches that specific class.
- **`MAX_CONCURRENT_JOBS` defaults to 1.** Conversions are I/O-heavy; the
  dispatcher runs them serially unless host capacity is validated. Don't assume
  parallelism in a service.
- **The frontend has a build step.** The Svelte 5 + Vite source lives in `src/`;
  `npm run build` emits the bundle into `static/`. Run `npm run lint` (ESLint
  flat config in `eslint.config.js`) and keep the style consistent.
- **The image runs as uid 999 (`converter`).** Binaries must be executable by a
  non-root user; install them to a world-readable path like `/usr/local/bin`.
- **Most convertible-source modes are archive-aware.** chdman *create*, Dolphin,
  3DS, and Switch (nsz) all accept members straight out of `.zip/.7z/.rar`; only
  chdman *extract/copy* stay opted out, because they act on a finished `.chd`
  (an output, not a convertible source). See **§17** for how to wire archive
  input into a new tool. If your tool genuinely can't read its inputs from inside
  an archive, leave `allows_archive_input=False` on its `ModeSpec` (the default)
  and the single guard in `plan_job` blocks archive (`::`) members automatically.
- **Binary path is config, not hardcoded.** Always read `settings.<tool>_path`
  (passed into the plugin, stored on the service in `__init__`) so deployments
  can relocate/override via env.

---

## 16. Tools that need user-supplied keys or secrets

Some platforms encrypt their content (Nintendo Switch is the first here). To
compress that content meaningfully a tool has to decrypt it first, which means
it needs cryptographic keys. Those keys are copyrighted and console-specific:
**the app must never ship them, and neither should the tool.** The operator
supplies their own, dumped from hardware they own, for content they own.

`nsz` (Switch) is the reference implementation. If you add another tool with the
same shape (keys, DRM, firmware blobs, account tokens), follow this pattern.

### 16.1 Never ship the secret

- **`.gitignore`** every common key/secret filename so a stray copy can't be
  committed (`prod.keys`, `*.keys`, `keys.txt`, `.switch/`, …).
- **`.dockerignore`** the same names so they can't be baked into the image even
  if present in the build context.
- **Never log the secret's contents.** Log the *path* at most. Review your
  service's debug logging for this before merging.
- The image runs as uid 999; the operator mounts the secret **read-only**, and
  it only needs to be world-readable, never writable.

### 16.2 Detection: one env var is the source of truth, with best-effort fallback

Add a single setting that points at the secret. Prefer a **directory** the
operator mounts (matches how most key tools already organize files) over a
single file path:

```python
# app/config.py
switch_keys_dir: str | None = Field(default=None, alias="SWITCH_KEYS")
```

The service resolves the actual file with a `resolved_keys_file()` /
`keys_available()` pair (see `app/services/nsz.py`):

- When the env var **is set**, it is authoritative: look only inside that
  directory; if the key isn't there, report unavailable (don't silently fall
  back, or the operator can't tell their config is wrong).
- When it is **unset**, do a best-effort search: first the tool's standard
  locations (`~/.switch`, `~/.config/...`) as cheap `os.path.isfile` checks, then
  a **bounded recursive walk** of the configured game volumes (`settings.volumes`)
  and the data dir. Wrap volume access in `try/except` so key discovery can never
  break a job.

**Bound the recursive walk.** A full unbounded walk of multi-TB volumes could
stall startup, so cap the directories visited (`_MAX_KEY_SEARCH_DIRS`), prune
junk dirs with the shared `utils.junk.is_junk_entry`, stop at the first hit, and
log a warning if the cap is reached telling the operator to set the env var. The
walk only runs as a fallback: when keys sit in a standard location or
`SWITCH_KEYS` is set, the cheap path returns first.

### 16.3 Supplying the secret to the binary

Check how the binary actually consumes keys before wiring this up; it is easy
to get wrong, and unit tests that mock the subprocess won't catch it. **Run the
real `--help`.** Two cases:

- **The binary takes a flag** (e.g. `--keys /path`): just append it in
  `_build_command`.
- **The binary loads keys at import/startup from a fixed location** (nsz reads
  `~/.switch/prod.keys` via `$HOME` and has *no* `--keys` flag. It even
  `input()`-prompts and exits if none is found, which hangs under a pipe): you
  cannot pass a flag. Instead run the child with a throwaway `$HOME` whose
  `.switch/prod.keys` is a symlink to the resolved key file. See
  `NszService._keys_home()`. Pass the modified env via `create_subprocess_exec(...,
  env={**os.environ, "HOME": tmp})` and clean the temp dir up in `finally`.

### 16.4 Fail safe, with an actionable message

Guard **before** spawning. If keys aren't available, raise a `RuntimeError`
whose message tells the operator exactly what to do (which env var, where to put
the file). The job fails cleanly with that text instead of the binary dying with
a cryptic error. Verify needs the same guard.

### 16.5 Gate the UI so the tool is hidden without keys

A tool that can never run without keys shouldn't clutter the UI. The backend
exposes availability and the frontend hides unavailable tools entirely:

- **Backend:** `GET /api/tools` (in `app/routes/info.py`) returns
  `{"available": [...], "unavailable": [...]}`. A tool lands in `unavailable`
  when its readiness check fails (for nsz, `keys_available()` is false).
- **Frontend:** `App.svelte` fetches it on mount and calls
  `ui.applyToolAvailability(available)`, which stores `ui.hiddenTools`. Sidebar
  and the dashboard derive their tool list as
  `registry.all().filter((t) => !ui.hiddenTools.has(t.id))`, and the active tool
  falls back to a visible one if it gets hidden. No registry edits needed; this
  is generic over tool id.

### 16.6 Copyright posture

State it plainly in the docs (README has a "Legal note"): the project ships no
keys, firmware, or copyrighted content; the operator provides their own for
hardware and content they own. Keep dual-use framing honest: this compresses
backups the user already owns; it is not a circumvention tool, and the format
preserves the original protection measures.

### 16.7 Testing without the real secret

- **Unit tests** mock `create_subprocess_exec` and a dummy key file on disk, so
  they exercise the wiring (argv, `_keys_home`, the missing-keys guard,
  availability gating) without real keys. See `tests/test_nsz_service.py` and
  `tests/test_nsz_routes.py`.
- **A real round-trip test** (`tests/test_nsz_roundtrip.py`) runs the actual
  binary through the service on operator-supplied inputs and **skips** when they
  aren't present, so CI stays green. Inputs live in a git-ignored
  `testdata/<platform>/` scratch dir (only its README is tracked); the operator
  drops their keys and a sample dump there or points env vars at them.

---

## 17. Tools that read inputs from archives (ZIP/7z/RAR)

Users keep dumps inside `.zip`/`.7z`/`.rar` archives, so every tool that takes a
*convertible source* can convert a member straight out of the archive without a
manual unzip first. Today chdman *create*, Dolphin, 3DS, and Switch (nsz) all
support this; only chdman *extract/copy* opt out, because their input is a
finished `.chd` (an output, not a source).

The pipeline is **tool-agnostic and registry-driven**: a member arrives as a
`"<archive>::<member>"` pseudo-path, the job layer extracts it to a real temp
file before your `convert()` ever runs, and a single flag decides whether your
mode participates. You almost never write archive-specific code; you set one
flag and (for multi-file formats only) declare your sidecars. nsz is the
reference: enabling it was literally two flag flips plus tests (see
`app/services/tools/nsz.py` and the §17.7 test matrix).

### 17.1 Opt in: one flag, in both registries

Set `allows_archive_input=True` on each `ModeSpec` whose input is a convertible
*source*, in **both** the Python spec and the JS registry (they have no automated
cross-language parity test, so keep them in sync by hand):

```python
# app/services/tools/<tool>.py
ModeSpec(mode="nszip_compress", tool_id="nszip", kind=ModeKind.COMPRESS,
         ..., allows_archive_input=True),
```

```js
// src/lib/tools/registry.js, in your tool's modes[]
{ mode: 'nszip_compress', kind: 'compress', ..., allowsArchiveInput: true },
```

That is the entire opt-in for a single-file format. Everything below is either
free or only applies to multi-file (CD) inputs.

### 17.2 What you get for free

Once the flag is `True`, the registry and job pipeline do the rest:

- **Listing.** `ArchiveService` surfaces convertible members by reading
  `registry.archive_input_extensions()` (the union of `input_extensions` over
  every mode with `allows_archive_input=True`). Your extensions appear in archive
  browse results automatically; there is nothing to register in `archive.py`.
- **Validation.** The single guard in `plan_job` (`convert.py`) rejects `::`
  members for any mode whose `spec.allows_archive_input` is `False`, and accepts
  them otherwise. Per-tool extension checks use `_input_extension(file_path)`,
  which is archive-aware (it reads the member's extension, not `.zip`), so your
  existing validation block needs no change.
- **Extraction + cleanup.** `job_manager._process_job` splits the pseudo-path,
  calls `archive_service.extract_file(archive, member)` to drop the member into a
  private temp dir, hands the **real on-disk path** to your `convert()`, and
  removes the temp dir when the job ends. Your service sees an ordinary file
  path; it never needs to know it came from an archive.

### 17.3 Output paths for archive members

The output is computed *before* extraction, from the member name, via
`archive_service._output_name_for_member(member)`. That helper flattens
subdirectories but **preserves the original extension** (`games/cart.nsp` ->
`games_cart.nsp`), then passes it to your plugin's
`output_path(mode, name, dir, treat_as_stem=True)`.

Because the flattened name keeps its real extension, your existing
`get_output_path_for_mode` maps it exactly like an on-disk file — there is no
separate archive branch to write. z3ds and nsz both ignore `treat_as_stem` for
this reason; they just look up `suffix` in their `*_OUTPUT_FORMATS` map. Accept
the `treat_as_stem` kwarg for interface parity and move on. (`_output_stem_for_member`,
which *drops* the extension, exists only for chdman's always-`.chd` existing-output
badging — don't route input-derived outputs through it.)

### 17.4 Multi-file (CD) inputs: declare your sidecars

Single-file formats (`.nsp`, `.xci`, `.3ds`, `.rvz`, `.iso`, …) need nothing
beyond the flag. A format whose "file" is really several files — a `.cue`/`.gdi`
that references `.bin`/track files — must extract its siblings too, or the
converter sees a dangling reference. `job_manager` handles this by calling
`archive_service.extract_related_files(archive, member, temp_dir)`, which
early-returns for everything except `.cue`/`.gdi` today. If you add a new
multi-file source, extend that helper (parse the manifest, extract referenced
members into the same temp dir). Switch/3DS/Dolphin are all single-file, so this
does not apply to them.

### 17.5 When *not* to opt in

Leave `allows_archive_input=False` (the default) when the mode's input is an
**output class**, not a convertible source — e.g. chdman `extract`/`copy`, which
operate on a finished `.chd`. Re-reading a `.chd` from an archive is a recompress
target, not a conversion source, so the guard rejects it (and
`tests/test_archive_conversion_e2e.py::test_archive_chd_member_rejected_for_recompress`
locks that in). Same logic for any "decompress an already-final artifact" mode.

### 17.6 Delete-on-verify from an archive

If your mode also sets `supports_delete_on_verify=True`, the source-deletion
snapshot is archive-aware: `build_delete_plan` calls
`utils.path_utils.strip_archive_path`, so the plan targets the archive container
on disk, never a (non-deletable) member inside it. chdman *create* and z3ds
already pair both flags; nsz *compress* now does too. No extra work — just don't
assume the source is a plain file in your own code.

### 17.7 Testing

Two registry-driven suites cover the whole path; extend both:

- **`tests/test_archive_conversion_e2e.py`** — add a row per direction to the
  `MATRIX` (input ext, mode, expected output ext). The fixture stubs every
  tool's `convert`, then drives the real route -> `plan_job` -> real extraction
  from a real on-disk `.zip` -> stubbed convert -> output-naming -> temp cleanup.
  This proves the member was genuinely extracted to a temp file and that the
  output lands next to the archive with the input-derived extension. Because
  `convert` is stubbed, no real binary (or keys, for nsz) is needed.
- **`tests/test_archive_preference.py`** — assert your source extensions are in
  `registry.archive_input_extensions()` (and that output-only extensions like
  `.chd` are *not*).

```python
# tests/test_archive_conversion_e2e.py — MATRIX rows for a Switch-shaped tool
(".nsp", ConversionMode.NSZ_COMPRESS,   ".nsz"),
(".xci", ConversionMode.NSZ_COMPRESS,   ".xcz"),
(".nsz", ConversionMode.NSZ_DECOMPRESS, ".nsp"),
(".xcz", ConversionMode.NSZ_DECOMPRESS, ".xci"),
```

Run them:

```bash
# from the repo root
pytest -q tests/test_archive_conversion_e2e.py tests/test_archive_preference.py
```

