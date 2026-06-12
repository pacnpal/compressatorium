# Issue #98 — Plan: cso→chd chaining seam, and PS3 folder→iso

**Status:** Research / design proposal · **Scope:** two of the #98 conversions ·
**Companions:** `docs/DESIGN_tool_plugin_architecture.md`,
`docs/ADDING_PLATFORMS_AND_TOOLS.md`

This is a plan, not an implementation. It introduces two **independent** seams
above the existing plugin/registry contract (tools shell out to a CLI and yield
`{progress, message}`; the seam sits above that and does not rewrite it):

- **Phase 1 — cross-tool chaining** (`ChainSpec` + a synthetic `ChainTool`),
  with **cso→chd** as its first user. No new binary.
- **Phase 2 — directory-as-input** (an `InputKind` seam + a PS3-folder
  detector) with **makeps3iso** as its first user. New binary.

The two phases are orthogonal. A future *folder→iso→chd* would compose Phase 2's
directory input as step 1 of a Phase 1 chain, but neither phase depends on the
other today, and **nothing here is architected toward RPCS3 CHD** (issue
RPCS3 #17997 is an open feature request, opened Jan 2026, not shipped).

## 0. What HEAD already gives us (verified, not assumed)

| Fact | Location |
|------|----------|
| `cso_decompress` mode: `.cso/.zso/.dax` → `.iso`, `kind=EXTRACT`, `supports_delete_on_verify=False`, `allows_archive_input=True` | `app/services/tools/maxcso.py` (`MaxcsoTool`, `tool_id="cso"`) |
| chdman `createcd`/`createdvd` accept `.iso` (`CHDMAN_CONVERTIBLE_EXTENSIONS = {.gdi,.iso,.cue,.bin}`), emit `.chd`, `kind=CREATE`, `supports_delete_on_verify=True`; createdvd injects `-hs 2048` | `app/services/chdman.py`, `app/services/tools/chdman.py` |
| Mode selection createcd vs createdvd is **user-chosen**, no auto-detect | `app/services/tools/chdman.py` |
| `ModeSpec` fields: `mode, tool_id, kind, label, group, output_ext, input_extensions, supports_compression, supports_compression_level, supports_delete_on_verify, allows_archive_input` | `app/services/tools/spec.py` |
| Registry: `for_mode`, `spec`, `mode_specs`, `tools_for_input`, `tool_for_verify`, `*_extensions`, `tools_accepting_archive_member` | `app/services/tools/registry.py` |
| `convert(input_path, output_path, mode, *, compression=None, cancel_event=None)` is the plugin contract; **no work-dir param** | `app/services/tools/base.py`, `runner.py` |
| `_process_job`: archive-extract → `registry.for_mode(mode).convert(...)` → progress mapped 1:1 → single verify gate → delete-on-verify only on success | `app/services/job_manager.py` |
| `ConversionJob`: one `file_path`, `mode`, `output_path`, `temp_dir`; **no input-kind** | `app/models.py` |
| `plan_job` validates `os.path.isfile(file_path)` (or archive `::` member); a directory is rejected as `FILE_NOT_FOUND` | `app/routes/convert.py` |
| **No disk-space / headroom check anywhere** (no `shutil.disk_usage`/`statvfs`) | repo-wide |
| **No multi-step progress weighting** — `job.progress` is one 0–100 stream | `app/services/job_manager.py` |
| Lock manager keys on `sha256(os.path.normpath(path))` — works for a directory path | `app/services/lock_manager.py` |
| Directory rows in listings carry only `name/path/type="directory"`; everything else is `Path(suffix)`-driven | `app/routes/files.py` (`scan_directory`, `_detect_file_outputs`) |
| PSF/SFO container parser already exists (PSP `PARAM.SFO`) — reusable for PS3 `PARAM.SFO` | `app/services/disc_id.py` (`_parse_sfo`, `_SFO_MAGIC`) |
| `SubprocessRunner.run(cmd, *, input_path, output_path, parse_progress, cancel_event, cwd, ...)` is the shared shell-out seam | `app/services/subprocess_runner.py` |

---

## Phase 1 — the cross-tool chaining seam (cso→chd as first user)

### 1.1 How a composite mode is represented — `ChainSpec`

Add two frozen dataclasses to `app/services/tools/spec.py`, alongside `ModeSpec`:

```python
@dataclass(frozen=True)
class ChainStep:
    tool_id: str          # owner of this step's mode (e.g. "cso", "chdman")
    mode: str             # the sub-mode wire value ("cso_decompress", "createdvd")
    weight: float         # progress weight; normalized across steps
    output_ratio: float   # est. (this step's output size) / (chain input size)

@dataclass(frozen=True)
class ChainSpec:
    mode: str                            # composite wire mode, e.g. "cso_to_chd"
    tool_id: str                         # synthetic owner: "chain"
    kind: ModeKind                       # CREATE
    label: str
    group: str
    steps: tuple[ChainStep, ...]         # ordered; first owns input, last owns output
    input_extensions: frozenset[str]     # == step 1's inputs (.cso/.zso/.dax)
    intermediate_exts: tuple[str, ...]   # (".iso",)
    output_ext: str                      # ".chd"  (final)
    verify_step: int = -1                # index whose tool owns the final verify
    supports_delete_on_verify: bool = True   # of the ORIGINAL source, gated on final verify
    allows_archive_input: bool = True        # == step 1's
    supports_compression: bool = False
    supports_compression_level: bool = False
```

**Coexistence with `ModeSpec` in the registry.** `ChainSpec` is made
**structurally compatible** with the fields registry consumers read off a spec
(`mode`, `tool_id`, `kind`, `output_ext`, `input_extensions`,
`supports_delete_on_verify`, `supports_compression`, `allows_archive_input`).
A new **`ChainTool(BaseTool)`** (`app/services/tools/chain.py`) owns the chain
modes the same way `MaxcsoTool` owns its modes: `ChainTool.modes` is a tuple of
`ChainSpec`. Registration in `app/services/tools/__init__.py` happens **after**
the component tools, and `ChainTool` is handed a reference to the `registry` so
it can resolve and drive sub-tools. Result: `registry.for_mode("cso_to_chd")`
returns the `ChainTool`, `registry.spec("cso_to_chd")` returns the `ChainSpec`,
and every existing registry-driven consumer (convert route, job_manager, files
route) keeps working with **no chain-specific branching**. The seam is the new
tool; the consumers are untouched.

### 1.2 The concrete `cso_to_chd` ChainSpec

```python
ChainSpec(
    mode="cso_to_chd",
    tool_id="chain",
    kind=ModeKind.CREATE,
    label="CSO/ZSO/DAX → CHD",
    group="chain",                       # or surfaced under the CSO tool — see open Q1
    steps=(
        ChainStep("cso", "cso_decompress", weight=0.20, output_ratio=2.0),
        ChainStep("chdman", "createdvd",   weight=0.80, output_ratio=1.2),
    ),
    input_extensions=frozenset({".cso", ".zso", ".dax"}),
    intermediate_exts=(".iso",),
    output_ext=".chd",
    verify_step=1,                       # chdman verifies the final .chd
    supports_delete_on_verify=True,
    allows_archive_input=True,
)
```

**Why createdvd is pinned (step 2).** A `.cso`/`.zso`/`.dax` is an ISO/data-only
image — there are **no CD audio tracks to preserve**, so the createcd path
(cue/gdi multitrack + audio) is never the right target. `createdvd` produces a
DVD-type CHD with the existing `-hs 2048` sector size (already the PSP-UMD/PS2-DVD
compatibility default in `chdman.py`), which PPSSPP, PCSX2, and RetroArch read.
This holds **regardless of CD-vs-DVD-sized payload**: createdvd handles a bare
single-track data image of any size, so size does not change the mode. (See open
Q7 on whether to allow a user override.)

### 1.3 Intermediate lifecycle + disk headroom

- **Where the intermediate lives.** `ChainTool.convert` writes the `.iso` into a
  per-job **work dir**. Today `job.temp_dir` is created **only for archive
  input**; a chain on a plain `.cso` has no temp dir. Two coordinated changes:
  1. Add an optional **`work_dir`** kwarg to the `convert(...)` contract
     (`base.py` / `ToolPlugin` Protocol / `runner` passthrough). Default `None`
     → tool falls back to its own `tempfile.mkdtemp`. This is additive; existing
     single-step tools ignore it.
  2. In `job_manager._process_job`, ensure a work dir exists for chain modes
     (reuse `job.temp_dir` when archive extraction already made one, else create
     one) and pass it to `convert(..., work_dir=...)`. The existing
     `_cleanup_temp_dir(job)` already removes it on success/failure/cancel; the
     intermediate `.iso` is cleaned with it. `ChainTool.convert` also deletes
     the intermediate as soon as step 2 finishes (don't hold iso + chd longer
     than necessary).
- **Headroom math.** Peak simultaneous disk = `source(cso) + full(iso) +
  partial(chd)`, which the single-step model never holds. Introduce a shared
  **`app/services/disk.py`** helper, `ensure_headroom(work_dir, required_bytes,
  margin)`, built on `shutil.disk_usage`. Required bytes are derived from the
  `ChainStep.output_ratio` values: `required ≈ input_size * (Σ output_ratio) +
  margin` (for cso→chd: `input * (2.0 + 1.2) + margin`). `ChainTool.convert`
  calls it **before step 1**, raising a clear "insufficient disk space" error
  that surfaces as a normal job failure. This is a **new shared seam** (no disk
  check exists today); single-step tools can opt into it later. (Open Q2: hard
  fail vs warn; margin/setting name.)

### 1.4 Progress aggregation (weighted, single bar)

chd compression dominates cso decompression, so a 50/50 split misreports. The
weights live on `ChainStep` (cso `0.20`, chd `0.80`). Inside
`ChainTool.convert`, each sub-step's 0–100 maps into its slice:

```
aggregate = round( (Σ weight_j for j<i)*100 + weight_i * step_progress_i )
```

The aggregate is **monotonic non-decreasing** and ends at 100 when the last step
completes. `job_manager` sees an ordinary 0–100 stream — **no job_manager
change** for progress. The stall-timeout machinery (`_last_progress_at`) keeps
working because real updates keep flowing. Weights are authored per chain (and
are the obvious knob to tune from observed timings later).

### 1.5 Staged verify + delete-on-verify

- The intermediate `.iso` **cannot** be verified (maxcso verifies compressed
  containers, not the extracted iso — exactly why `cso_decompress` has
  `supports_delete_on_verify=False`). So the chain verifies **only the final
  `.chd`**, via the `verify_step` tool (chdman).
- `ChainTool.verify(path)` delegates to `registry.get(steps[verify_step].tool_id).verify(path)`
  on the final output. The existing verify gate in `_process_job`
  (`registry.for_mode(mode).verify(output_path)` when
  `spec.supports_delete_on_verify` and not an extract mode) therefore drives the
  chain's verify with **no special-casing** — `ChainSpec.supports_delete_on_verify=True`
  and `ChainTool.verify` resolves to chdman on the `.chd`.
- **Delete-on-verify of the original `.cso` gates on the final `.chd` verify**:
  the original is in the existing `delete_snapshot`; it is deleted only after the
  chd verify returns `{"valid": True}`. The intermediate `.iso` is never in the
  snapshot and never verified — it's transient work-dir state, removed by
  cleanup. This reuses the current delete-on-verify safeguards unchanged.

### 1.6 Touched files (described, not coded)

- `app/services/tools/spec.py` — add `ChainStep`, `ChainSpec`.
- `app/services/tools/chain.py` *(new)* — `ChainTool(BaseTool)`: `modes` = chain
  specs; `convert` (orchestrate steps via `registry.for_mode(step.mode).convert`,
  weighted progress, intermediate in `work_dir`, headroom precheck, cleanup);
  `verify`/`verify_stream` (delegate to `verify_step` tool); `output_path`
  (final `.chd` from input stem); `info`/`info_model` (delegate to final tool);
  `detect_output` (sibling `.chd`); `active_pids` (union of in-flight sub-tools).
- `app/services/tools/__init__.py` — register `ChainTool(registry=registry)`
  after component tools.
- `app/services/tools/base.py` + `ToolPlugin` Protocol — add optional `work_dir`
  kwarg to `convert`.
- `app/services/tools/registry.py` — ensure `ChainSpec` flows through
  `mode_specs()`/`spec()` (structural compatibility); optional `chain_specs()`
  helper.
- `app/services/job_manager.py` — create/pass `work_dir` for chain modes; no
  progress or verify changes.
- `app/routes/convert.py` — `plan_job` already resolves output via
  `registry.for_mode(mode).output_path` and validates against
  `spec.input_extensions`; confirm the `ChainSpec` path validates the `.cso`
  input. No structural change expected.
- `app/models.py` — add `cso_to_chd` to `ConversionMode`.
- `app/services/disk.py` *(new)* — `ensure_headroom`.
- `app/config.py` — optional headroom margin / setting (no new binary path —
  chain reuses `maxcso_path` + `chdman_path`).
- `src/lib/tools/registry.js` — surface `cso_to_chd` (new "Pipeline/Chain" card
  **or** a mode under the CSO tool — open Q1); `productPath` → `.chd`.
- `docs/DESIGN_tool_plugin_architecture.md` + `docs/RELEASE_NOTES.md` —
  document the new shared seam and the user-facing mode.

### 1.7 New tests (what would prove it)

- `tests/test_tool_registry.py` — `cso_to_chd` resolves to `ChainTool`;
  `ChainSpec` fields (kind CREATE, output `.chd`, inputs `.cso/.zso/.dax`,
  `supports_delete_on_verify=True`).
- `tests/test_chain_service.py` *(new)* — mock both sub-tools' `convert`
  generators; assert: intermediate `.iso` created in `work_dir` and removed
  after step 2; aggregate progress is monotonic and ends at 100 with the
  documented weighting; final `.chd` produced; `cancel_event` propagates and
  cleans **both** the intermediate and any partial final.
- Headroom test — `ensure_headroom` raises before step 1 when free space < need.
- Verify/delete gate test — original `.cso` deleted only after final `.chd`
  verify passes; intermediate never verified, never in the delete snapshot.

### 1.8 The seam generalizes

Under the same `ChainSpec` mechanism, other #98 requests become chains with no
new machinery: **xci→nsp** (Switch, via the existing nsz/Switch tooling),
**wud/wux→wua** (Wii U, once a wud-extract + wua-pack pair exists), and a
**future folder→iso→chd** if RPCS3 ever ships CHD (Phase 2 directory input as
step 1 + this chain). Each is just a new `ChainSpec` row + its component modes.

---

## Phase 2 — directory input + makeps3iso (folder → iso)

### 2.1 makeps3iso (verified against the upstream repo)

- **Source:** `bucanero/ps3iso-utils`. **License GPL-3.0**, primarily C (95.6%),
  single-source-style, builds with bare gcc, and ships an in-repo Dockerfile.
- **CLI:** `makeps3iso <input_folder> <output.iso>`; `-s` splits output at 4GB
  for FAT32 (`makeps3iso -s <folder> <out.iso>`). No keys required — operates on
  an already-decrypted folder.
- **Round-trip companion:** `extractps3iso` (iso → folder) exists in the same
  repo — usable for an optional verify.
- **arm64:** plain C with a portable Makefile + the repo's own Dockerfile, so it
  builds the same way `maxcso` already builds from source in our multi-stage
  `Dockerfile` (build stage → copy binary to runtime). Flag to confirm at
  implementation: the Makefile has no x86-only asm (a quick read of upstream
  shows plain C, no SIMD), so an arm64 build is expected to be clean.
- **License obligation vs how we ship chdman.** chdman ships from MAME
  (BSD-3-Clause / GPL-2.0+); maxcso is MIT. makeps3iso is **GPL-3.0**, a stricter
  copyleft. We ship an **unmodified upstream binary** built in the Docker image,
  so compliance is: keep it a separate binary (no linking into our app), and
  provide the corresponding source / written offer (pin the upstream ref in the
  Dockerfile, as we already do for maxcso). **Flag:** GPLv3 is a step up from our
  current binaries — confirm it's acceptable to add a GPLv3 binary to the image
  (Open Q3). **Maintenance flag:** upstream's last release is **April 2021**;
  decide whether to pin upstream or a maintained fork (Open Q3).

### 2.2 A first-class "directory is the unit of work" input kind

The codebase keys every input seam off `Path(filename).suffix`. A folder has no
suffix, so add a small **`InputKind`** seam rather than faking an extension:

- **`InputKind` enum** `{FILE, DIRECTORY}` in `spec.py`; add
  `input_kinds: frozenset[InputKind] = frozenset({InputKind.FILE})` to `ModeSpec`
  (default keeps every existing mode FILE — zero behavior change). A directory
  mode declares `{InputKind.DIRECTORY}` and, because there's no extension to
  match, relies on a **detector predicate** instead of `input_extensions`.
- **Plugin contract:** add `accepts_directory(path) -> bool` to `ToolPlugin`
  (default `False` in `BaseTool`). `MakePs3IsoTool` overrides it to run the PS3
  folder detector (§2.4). This is the directory analogue of the
  `ext in input_extensions` check.
- **Registry:** add `tools_for_directory(path)` (analogue of `tools_for_input`)
  that returns tools whose `accepts_directory(path)` is true. Disk I/O lives in
  the detector, so callers run it in the existing threadpool.

### 2.3 What changes, seam by seam

- **Listing (`files.py:scan_directory`, `_detect_file_outputs`).** Today a
  directory row is `name/path/type="directory"`. Add: for each directory, ask
  `registry.tools_for_directory(path)`; if a tool accepts it, annotate the row
  with `convertible_by` + `outputs` (sibling `<folder>.iso`) just like a file
  row. Keep the detector cheap (stat for `PS3_DISC.SFB` / `PARAM.SFO`, read only
  the small SFO header) and inside the threadpool. A directory variant of
  `tools_for_input` (`tools_for_directory`) drives this. (Open Q6: always-on vs
  lazy hydration like archive summaries.)
- **`detect_output` for a folder.** No suffix to swap — derive the sibling from
  the folder **basename**: `<parent>/<folder_name>.iso`. `MakePs3IsoTool.detect_output`
  reports it (in-progress vs ready via `lock_manager.check_file_status`).
- **Output-path derivation.** `output_path` = `output_dir/<folder_basename>.iso`
  (folder name, not a stem swap).
- **Verify gate.** makeps3iso has **no native verify**. Recommended: ship
  **no-verify, `supports_delete_on_verify=False`** — deleting a user-curated
  decrypted folder is destructive and there is no cheap trustworthy verify.
  Optional follow-up (reuses `disc_id.py`'s SFO parser): parse `PARAM.SFO` from
  the **built iso** and confirm its `TITLE_ID` matches the source folder — a
  light readback, not a full round-trip via `extractps3iso`. (Open Q4.)
- **Lock manager.** Locks key on `sha256(normpath(path))`, so a **directory
  path locks fine as-is**. Caveat: a per-file job *inside* the folder hashes to a
  different key and wouldn't be blocked by the folder lock. For folder→iso this
  is acceptable (we only read the folder, and we don't offer per-file conversion
  of PS3 folder contents). Flag as a known limitation (Open Q — acceptable).
- **Job model.** Add `input_kind: str = "file"` to `ConversionJob` (and the
  enqueue path); `file_path` holds the directory path, `output_path` the iso.
  `_process_job` and the verify gate read `input_kind` to skip the file-only
  assumptions. Reuse `temp_dir` if makeps3iso writes to a temp then moves.
- **`plan_job` validation (`convert.py`).** Add an `os.path.isdir` branch: for a
  directory mode, validate `isdir(path)` **and** the PS3 detector instead of
  `isfile(path)` + extension. Today a directory falls through to `FILE_NOT_FOUND`.

### 2.4 The "valid PS3 iso source" detector

The directory analogue of an extension check, in a new `app/services/ps3.py`
(or on the tool), threaded through detect → plan → process:

- **Disc folder:** contains `PS3_GAME/` **and** `PS3_DISC.SFB`.
- **Game folder:** contains a `PARAM.SFO` under a TITLEID directory (or at the
  expected `PS3_GAME/PARAM.SFO` path).

A folder matching either shape is a valid makeps3iso input; anything else is not
offered. The SFO read reuses the existing PSF parser in `disc_id.py`.

### 2.5 Reused (Phase 1 / existing) vs new

- **Reused:** plugin/registry/`ModeSpec` pattern, `SubprocessRunner.run`
  shell-out + `parse_progress`, `BaseTool` delegation, `detect_output`
  mechanism, `lock_manager`, the job pipeline + verify gate, the frontend
  registry entry shape. makeps3iso is a **single-step** tool — it does **not**
  use Phase 1's `ChainSpec`.
- **New:** `InputKind` + `accepts_directory` + `tools_for_directory`; directory
  annotation in `scan_directory`; the PS3 folder detector; the makeps3iso binary
  + Dockerfile build stage + `makeps3iso_path` config field; `input_kind` on the
  job model; no-verify handling.

### 2.6 makeps3iso integration shape (same pattern as existing tools)

- `app/services/makeps3iso.py` *(new)* — service singleton: `convert()` shells
  out via `SubprocessRunner.run(["makeps3iso", folder, out_iso], parse_progress=...)`,
  PID tracking, output-path logic; `info()` (read PARAM.SFO title/id).
- `app/services/tools/makeps3iso.py` *(new)* — `MakePs3IsoTool(BaseTool)`:
  `modes` = one DIRECTORY `ModeSpec` (`folder_to_iso`, output `.iso`,
  `input_kinds={DIRECTORY}`, no delete-on-verify), `accepts_directory`,
  `detect_output`, delegate the rest to the service.
- `app/services/tools/__init__.py` — `registry.register(MakePs3IsoTool(settings.makeps3iso_path))`.
- `app/config.py` — `makeps3iso_path` Field + optional priority/timeout overrides.
- `Dockerfile` — build stage from `bucanero/ps3iso-utils` at a pinned ref
  (mirror the maxcso stage), copy `makeps3iso` (+ `extractps3iso` if verify is
  pursued) into the runtime stage; confirm arm64.
- `src/lib/tools/registry.js`, `app/models.py` (`folder_to_iso` mode + info
  model), tests, `docs/`.

### 2.7 New tests

- Directory detector: disc-folder and game-folder fixtures accepted; a plain
  folder rejected.
- `tools_for_directory` returns `MakePs3IsoTool` for a valid PS3 folder, nothing
  for an arbitrary folder.
- `scan_directory` annotates a PS3 folder row with `convertible_by` + sibling
  `.iso` output.
- makeps3iso service convert (mocked subprocess): argv `makeps3iso <folder>
  <out.iso>`, progress drains to 100, output recorded; cancel cleans partial.
- `plan_job` accepts a valid PS3 directory, rejects a non-PS3 directory.

---

## Open questions for Talor

1. **cso→chd UI placement** — a new "Pipeline/Chain" tool card, or expose
   `cso_to_chd` as an extra mode under the existing CSO tool? (Affects
   `registry.js` + sidebar grouping.)
2. **Headroom policy** — hard-fail before step 1 when free space is short, or
   warn-and-proceed? What margin (fixed MB vs % of input), and the setting name
   (`COMPRESSATORIUM_CHAIN_DISK_MARGIN`?)? Should single-step tools adopt it too?
3. **makeps3iso provenance** — upstream's last release is April 2021. Pin
   upstream `bucanero/ps3iso-utils`, or a maintained fork? And is adding a
   **GPL-3.0** binary to the image acceptable (stricter than our current
   BSD/GPL-2/MIT binaries)?
4. **folder→iso verify** — ship no-verify / no-delete-on-verify (recommended),
   or invest now in the `PARAM.SFO` `TITLE_ID` readback (reusing
   `disc_id.py`) — or a full `extractps3iso` round-trip?
5. **`-s` split flag** — expose 4GB FAT32 splitting as a user option, or always
   emit a single `.iso`? (Depends on whether target volumes are FAT32.)
6. **Directory-listing cost** — run the PS3 detector on every directory row
   during `scan_directory`, or hydrate lazily like archive summaries
   (`/archive-summary`)?
7. **createdvd pin** — keep step 2 pinned to `createdvd` unconditionally
   (recommended; cso is audio-free data), or allow a user override to createcd?
8. **Composite mode naming** — confirm wire value `cso_to_chd`; should the
   intermediate `.iso` ever be surfaced or optionally kept rather than always
   discarded?
