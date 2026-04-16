# Release Notes

## v3.7.3 - DAT self-heal fires automatically on startup

### 🐛 Bug Fixes

- **Stale DATs now repair themselves on server restart.** v3.7.2 taught `DATSyncService` to skip its `already_synced` fast-path whenever any DAT had `file_count=0`, but the self-heal only ran when `sync()` was explicitly called — via the UI sync button or `POST /api/dat/sync`. Users who had never clicked sync stayed stuck with 67 empty DATs (the classic pre-#49 state: every CD/softlist `<disk>` DAT showing 0 entries while Nintendo GameCube + Wii — the two `<rom>`-based ones — were healthy). Startup auto-sync previously gated on `MAMEREDUMP_AUTO_SYNC=true` AND an empty DAT store, so it never fired for someone who already had 69 DATs sitting in broken state.

  Startup now has a second trigger: whenever any DAT has `file_count=0`, a background sync kicks off unconditionally (no env-var gate, no empty-store gate). The service's existing self-heal logic then rebuilds the DAT set in place using the current parser. Fresh-install behaviour (`MAMEREDUMP_AUTO_SYNC=true` + empty store) is unchanged.

### 🧪 Tests

- `test_has_stale_dats_empty_store_returns_false` / `_all_healthy_returns_false` / `_detects_zero_count` — unit tests for the new `DATStore.has_stale_dats()` existence check.

### 📁 Files Changed

- `app/services/dat_store.py` — new `has_stale_dats()` method (single-row SQL existence check, cheap enough for startup).
- `app/main.py` — startup auto-sync split into two independent triggers; shared task-spawn helper extracted.
- `tests/test_db_store_operations.py` — three new tests for `has_stale_dats()`.

### ⚠️ Upgrade Notes

- **No action required.** Restart the server once and stale DATs heal within ~60 s (given network access to the MAMERedump repo). If GitHub is unreachable the sync errors into the log and state is preserved — identical to the existing manual-sync behaviour.

---

## v3.7.2 - Self-healing DAT re-sync after the softlist &lt;disk&gt; parser fix

### 🐛 Bug Fixes

- **DATs with only `<disk>` hash entries no longer stay stuck at 0 entries after an upgrade.** The v3.7.0 parser (PR #49) added `<disk>` support so MAME-Redump softlist DATs for disc-based systems (Amiga CD / CD32 / CDTV, Bandai Pippin, Konami FireBeat / System 573, Sega Naomi / Chihiro / Lindbergh, Atari Jaguar CD, IBM PC, etc.) import correctly. However, anyone who ran the DAT sync *before* that parser change ended up with DAT rows whose `file_count=0` because the old parser silently ignored `<disk>` elements, and the `DATSyncService` treated the (cached, correct) tag as "already synced" — trapping users in the stale state.

  The sync service now self-heals: if `last_sync_tag` matches but any existing DAT has `file_count=0`, the fast-path is skipped and the DAT set is rebuilt in place. The old DATs are only deleted after the new set is fully imported, so there's never a window with zero DATs.

### ✨ New Features

- **`POST /api/dat/sync` accepts `"force": true`** — bypasses the `already_synced` fast-path unconditionally, useful when operators want to rebuild the DAT set for any reason (repo-side fixes, local DB corruption, etc.). Default remains `false`.

### 🧪 Tests

- `test_sync_auto_forces_when_any_dat_has_zero_entries` — regression test for the self-heal path.
- `test_sync_force_bypasses_already_synced_guard` — covers the explicit `force=True` opt-in.
- `test_sync_already_synced_requires_nonzero_file_counts` — the fast-path still fires when every DAT is healthy.

### 📁 Files Changed

- `app/routes/dat.py` — `SyncRequest` gains `force: bool = False`; route forwards it to the service.
- `app/services/dat_sync.py` — `sync()` / `_do_sync()` take `force`; the fast-path skips when any DAT has `file_count=0`.
- `tests/test_dat_sync.py`, `tests/test_dat_routes.py` — new regressions + existing assertions updated for the new kwarg.

### ⚠️ Upgrade Notes

- **No action required.** On next sync click the service notices the stale state and rebuilds automatically. If you prefer to trigger it explicitly, POST `/api/dat/sync` with `{"force": true}`.

---

## v3.7.1 - Color-coded stdout logs

### ✨ New Features

- **ANSI-colored log levels on stdout** — Log lines now highlight `%(levelname)s` with an SGR color so severity is easy to skim in `docker logs` output: `DEBUG` dim, `INFO` green, `WARNING` yellow, `ERROR` red, `CRITICAL` bold red. Message bodies are left uncolored so copy-paste stays clean.
- **`LOG_COLOR` env var** — Controls the stream-handler color policy. Values:
  - `always` (**default**) — colored stdout out of the box, so Docker users see colors without any configuration. `docker logs` does not allocate a TTY, so this was the right default rather than `auto`.
  - `auto` — color iff stdout is a TTY and the `NO_COLOR` env var (<https://no-color.org>) is unset.
  - `never` — disable entirely.
  Invalid values log a warning and fall back to `auto`.
- **File logs are never colored** — `LOG_PATH` output always uses the plain formatter regardless of `LOG_COLOR`, keeping `grep` / log-aggregator pipelines intact.

### 📁 Files Changed

- `app/main.py` — added `ColorFormatter`, `_resolve_color()`, wired into `configure_logging()`.
- `app/config.py` — added `log_color: str` setting.
- `README.md` — `LOG_COLOR` row in the env-var table.
- `tests/test_logging_config.py` — +14 tests: default value, resolver TTY/`NO_COLOR` logic, formatter round-trip, end-to-end colored stream, and a guarantee that the file handler never emits ANSI escapes even with `LOG_COLOR=always`.

---

## v3.7.0 - Unified SQLite Store, Alembic Migrations, and Wider DAT Matching

### ✨ New Features

- **Unified SQLite persistence layer** — The four legacy JSON stores (`dat_store.json`, `chd_metadata.json`, `verified_chds.json`, `dat_sync.json`) are consolidated into a single `compressatorium.db` SQLite file backed by SQLAlchemy 2.0. Tuned for this workload: WAL journal mode, `synchronous=NORMAL`, `foreign_keys=ON`, 30s `busy_timeout`. Composite primary keys let sha1/md5 coexist; foreign keys give cascade-on-DAT-delete for hashes and set-null semantics for cached matches so UI history survives DAT removal.
- **One-shot JSON → SQLite migration on startup** — On first boot against the new release, legacy JSON stores are imported into the DB and the source files are renamed to `*.migrated.bak` (never deleted). Migration is isolated per store: a failure in one does not block the others, leaves that store's JSON untouched, rolls the transaction back, and retries on the next startup. Corrupt JSON is renamed to `*.corrupt` and logged loudly.
- **Alembic schema versioning** — Schema is driven by Alembic migrations (`migrations/versions/0001_baseline_schema.py`) rather than `create_all`. Pre-Alembic databases (anyone who ran the SQLite-migration build before Alembic landed) are detected by baseline-table presence and auto-stamped to `0001` before upgrade — existing rows are preserved untouched.
- **DAT match badge widened to raw disc images with bounded hashing** — The match badge now evaluates raw `.iso`/`.bin`/`.cue`/`.gdi` sources in addition to CHDs, with a bounded hashing window so large images can't stall the scan. Softlist `<disk>` elements are parsed as part of DAT matching, and matches run as a background job so the UI stays responsive.
- **`:beta` Docker tag for pre-releases** — Pre-release GitHub releases now publish a moving `:beta` tag (in addition to the semver tag) on both Docker Hub and GHCR; stable releases continue to publish `:latest`. Consumers opting in to betas pull `pacnpal/compressatorium:beta`. See the README "Opting in to beta updates" section for the data-loss warning — running a beta against a production data volume can irreversibly migrate the DB, and downgrading to `:latest` afterwards is not supported.

### 🐛 Bug Fixes

- **DAT match no longer leaks `OSError` internals to clients** (#56) — `_match_single_file` previously surfaced filesystem error detail (paths, errno strings) in the API response. It now returns a generic failure code and logs the detail server-side.

### 🧪 Tests

A comprehensive test suite has been added around the new SQLite feature — 26 new tests across 5 files covering every documented invariant end-to-end:

- **`tests/test_db_engine_pragmas.py`** — verifies WAL, `synchronous=NORMAL`, `busy_timeout ≥ 30s`, and `foreign_keys=ON` are actually applied on every checked-out connection (SQLite PRAGMAs are per-connection), and that FK enforcement raises `IntegrityError` on orphaned inserts.
- **`tests/test_db_startup_integration.py`** — replays the exact `startup_event` ordering: fresh disk, full legacy migration, pre-Alembic + JSON coexistence, and cross-restart idempotency.
- **`tests/test_db_store_operations.py`** — DAT cascade-delete wipes hashes; DAT delete sets cached match `dat_id` to `NULL` (not delete); `_set_matches_batch_sync` round-trips at the 899/900/901/1800 boundary against SQLite's 999-bind-parameter limit; `VerificationStore._mark_sync` is idempotent; CHDMetadata JSON column survives unicode, nested structures, and emoji; `DATSyncState` singleton never duplicates.
- **`tests/test_db_migration_edge_cases.py`** — backup-filename collision preserves the source JSON, mid-migration rollback preserves JSON and doesn't block other stores, unicode + ~1800-char paths round-trip, empty collections migrate cleanly, orphaned match `dat_id` becomes `NULL` rather than failing, and truncated JSON bodies land as `.corrupt` (not `.migrated.bak`).
- **`tests/test_verification_store.py`** — added a concurrent-writer test (two asyncio tasks × 200 writes each) proving WAL + busy_timeout end-to-end through `mark_verified`.
- **`tests/test_db_migration.py` and `tests/test_alembic_migrations.py`** (introduced during the beta cycle) — cover the five no-data-loss migration invariants and the five Alembic invariants (fresh DB, pre-Alembic stamp-then-upgrade, idempotency, ORM drift guard, called-before-init guard).

Total suite: 270 tests, all green.

### 🛠 CI / Infrastructure

- **Docker workflow** — `docker-image.yml` now emits `:beta` for pre-releases and `:latest` only for stable releases. Semver tags (`X.Y.Z`, `X.Y.Z-beta-N`, `X.Y`, `X` for non-v0.x) and `sha-<short>` tags continue to be emitted for all releases.
- **Code scanning noise reduced** — Project-wide linter configs expanded, Codacy bandit engine disabled, and remaining flagged false positives silenced with inline rationale.
- **Dependabot** — bumped `actions/stale` 5→10, `docker/build-push-action` 7.0.0→7.1.0, `docker/login-action` 3.7.0→4.1.0, `actions/labeler` 4.3.0→6.0.1, `actions/attest-build-provenance`, and `globals` 17.4.0→17.5.0.

### 📁 Files Added / Changed (high-level)

- `app/services/db.py` (new) — engine init, PRAGMA hook, Alembic bootstrap (`apply_migrations`), `init_and_migrate`, per-store migrators.
- `app/services/dat_store.py`, `verification_store.py`, `chd_metadata_store.py`, `dat_sync.py` — re-pointed at the SQLAlchemy session factory; chunked upserts where bulk operations cross SQLite's 999-bind-parameter limit.
- `migrations/` (new) — `alembic.ini`, `env.py`, `versions/0001_baseline_schema.py`.
- `app/main.py` — `startup_event` initialises the DB, runs Alembic, then `init_and_migrate` before any store is touched.
- `.github/workflows/docker-image.yml` — `:beta` tag for pre-releases.
- `README.md` — "Available Tags" table expanded with `:beta` and pre-release semver entries; new "Opting in to beta updates" section with data-loss warning.
- `tests/` — 5 new DB test modules plus shared `conftest.py` with sample JSON payloads and the `reset_db_engine` fixture.

### ⚠️ Upgrade Notes

- **Back up your `data_dir`** before upgrading, especially if you have a large `dat_store.json` — this is a one-way migration. The legacy JSON is preserved as `*.migrated.bak` on successful import, but the new DB is the source of truth after restart.
- **Downgrading to v3.6.x** after `compressatorium.db` has accepted writes is not supported. If you need to roll back, restore the `.migrated.bak` files (remove the suffix) and delete `compressatorium.db` before starting the older image.
- **Read-only `/config` volumes** — the DB falls back to `$TMPDIR/compressatorium/compressatorium.db` if `data_dir` is unwritable, matching the legacy JSON-store fallback. Set `CHD_DB_PATH` explicitly if you want a different location.

---

## v3.3.2 - CD CHD Sector Extraction Fix

### 🐛 Bug Fixes

- **PS1 serial extraction from CD CHDs** — `_extract_from_chd_sectors` previously returned `None` for all CD-based CHDs (unit_bytes=2352/2448), meaning PS1 game serials could only be extracted from companion `.bin`/`.cue` files or if those CHDs were already tagged at conversion time.  The function now probes both **Mode 2 Form 1** (24-byte header) and **Mode 1** (16-byte header) sector framing when reading 2352- or 2448-byte CD sectors, allowing it to walk the embedded ISO 9660 filesystem directly — exactly as libchdr-based emulators (PCSX2, AetherSX2, NetherSX2) do.  For CHDs created from Mode 2 Form 1 BIN/CUE images (the most common PS1 format), extraction now succeeds without requiring a companion source file.

### 🧪 Tests

- Added `test_extract_from_chd_sectors_ps1_cd_mode1` — verifies PS1 serial extraction from a CD CHD with 2352-byte Mode 1 sectors.
- Added `test_extract_from_chd_sectors_ps1_cd_mode2` — verifies PS1 serial extraction from a CD CHD with 2352-byte Mode 2 Form 1 sectors.
- Added `test_extract_from_chd_sectors_cd_no_recognizable_content` — confirms that a CD CHD with no ISO 9660 filesystem still returns `None` gracefully.
- Replaced the now-obsolete `test_extract_from_chd_sectors_non_dvd_returns_none` test (which verified the old early-return shortcut) with the three new tests above.

### 📁 Files Changed

- `app/services/disc_id.py` — `_extract_from_chd_sectors` extended to handle CD CHDs; docstrings updated throughout
- `tests/test_disc_id.py` — Three new CD CHD extraction tests; module docstring updated

---

## v3.3.1 - Structured Logging with LOGLEVEL

### ✨ New Features

- **`LOGLEVEL` environment variable** — Replaces the removed `CHD_DEBUG` flag. Set to `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` to control log verbosity. Defaults to `INFO` so useful operational logs are visible out of the box without any configuration. Legacy `CHD_DEBUG=true` is still honoured and maps to `LOGLEVEL=DEBUG` for backwards compatibility.
- **`LOG_PATH`** — Replaces the removed `CHD_DEBUG_LOG_PATH`. Optionally write logs to a file in addition to stdout, at any log level (not just debug). Legacy `CHD_DEBUG_LOG_PATH` is still read for backwards compatibility.

### 🔍 Enhanced Metadata Scan Logging

Metadata scans now emit detailed, structured `INFO`-level logs covering every stage, so users can fully understand what the application is doing by inspecting logs:

- **Scan start** — Logs `force` mode, number of volumes, and their paths.
- **Discovery** — Logs how many CHD files were found across how many volumes; warns on any missing/inaccessible volume.
- **Phase 1 (metadata extraction)** — Logs each file with a `[n/total]` counter as its `chdman info` metadata is extracted and cached. Logs success per file; warns on failure.
- **Phase 1 summary** — Reports how many files were refreshed vs already up-to-date.
- **Phase 2 (disc ID tagging)** — Logs when each previously-unchecked CHD is scanned for a GAME/NAME tag. When a disc ID is successfully embedded into the CHD file, logs the filename and extracted `game_id`. When no disc ID can be found, logs that the file was marked as checked.
- **Phase 2 summary** — Reports already-checked, newly-checked, and embedded tag counts.
- **Flush** — Logs when the metadata store is being persisted and when it completes.
- **Final summary** — Reports total metadata refreshed, disc IDs embedded, and total elapsed time.

Disc ID embedding operations inside `disc_id.py` (strategies 2–4) are also promoted from `DEBUG` to `INFO` so the source of each embedded tag (CHD sectors, GDRO, or companion file) is visible in normal operation.

### 🛠 Internal Changes

- The background maintenance loop (stuck-job detection, stale lock cleanup) now always runs, not only when `LOGLEVEL=DEBUG`. It was previously gated behind `CHD_DEBUG=true`, meaning stuck-job recovery silently didn't operate in production.

### 📁 Files Changed

- `app/config.py` — Removed `debug`/`CHD_DEBUG`; added `log_level`/`LOGLEVEL` (default `INFO`) and `log_path`/`LOG_PATH`
- `app/main.py` — `configure_logging()` parses `LOGLEVEL` instead of the old boolean flag
- `app/services/job_manager.py` — Maintenance loop always starts; removed `settings.debug` gate
- `app/routes/info.py` — Comprehensive structured INFO logging throughout `scan_metadata_task`
- `app/services/disc_id.py` — Embedding log calls promoted from `DEBUG` to `INFO`
- `README.md`, `DEPLOYMENT.md`, `DOCKER-COMPOSE.md` — Updated env var tables

---

## v3.3.0 - Game ID & Title Extraction for CD and DVD CHDs

### ✨ New Features

- **Game ID & Title in CHD Inspector** — PS1, PS2, PSP, and Dreamcast game serials are extracted from CHD sector data (via `SYSTEM.CNF`, `PARAM.SFO`, and `IP.BIN`) and displayed in the CHD info modal. Human-readable titles (e.g. "Patapon", "DEAD OR ALIVE 2") are shown when available; the serial is used as a fallback title.
- **CHD metadata tagging at conversion time** — When a CD or DVD CHD is created, the game serial is embedded as a `GAME` tag and the title as a `NAME` tag inside the CHD file itself, making it readable by emulator frontends and database scrapers.
- **Retroactive game ID tagging** — The metadata scan (Phase 2) now loops over all CHDs and embeds `GAME`/`NAME` tags into any file that doesn't have them yet. Already-tagged and previously-scanned files are skipped efficiently.
- **Persistent disc-ID cache** — Extracted game IDs and titles are stored in the metadata cache (`chd_metadata.json`). The `/api/info` endpoint reads from the cache first; `chdman dumpmeta` subprocesses are only spawned on a cache miss, and "nothing found" results are also cached to prevent repeated subprocess calls for unsupported discs.

### 🔬 Implementation Details

- `app/services/disc_id.py` — New service implementing:
  - `_CHDReader` — minimal CHD v5 sector reader (ZLIB, LZMA, ZSTD, NONE, MINI compression)
  - `extract_from_source()` — PS1/PS2 `.iso`/`.bin`/`.cue`/`.gdi` serial extractor
  - `extract_from_chd()` — four-strategy extractor (embedded GAME tag → sector read → Dreamcast GDRO → companion source files)
  - `ensure_disc_id_embedded()` — retroactive CHD tagger called during metadata scan
- `app/services/chd_metadata_store.py` — Added `get_disc_id_info()`, `update_disc_id_info()`, `is_disc_id_checked()`, and `mark_disc_id_checked()`; `set_metadata()` now preserves `game_id`, `title`, `disc_id_checked`, and `disc_id_checked_mtime` during Phase 1 metadata refreshes
- `app/routes/info.py` — `/api/info` uses the metadata cache; CHDs already scanned (Phase 2 or prior `/api/info` call) with no game ID skip re-extraction
- `app/models.py` — `CHDInfo` model extended with optional `game_id` and `title` fields
- `static/js/app.js` — CHD info modal shows "Game ID" and "Title" rows when values are present

### 🧪 Tests

- 12 new tests covering `_CHDReader` MINI hunk decoding, `_extract_cue` (including missing-file fallback and multi-file fallback), source-file extraction, CHD sector extraction, metadata store disc-ID methods, and retroactive embedding

### 📁 Files Changed

- `app/services/disc_id.py` — New service (source extractor + CHD reader + retroactive tagger)
- `app/services/job_manager.py` — Embeds `GAME`/`NAME` tags at conversion time
- `app/services/chd_metadata_store.py` — Disc-ID cache methods; `set_metadata()` field preservation
- `app/routes/info.py` — `/api/info` disc-ID cache lookup + Phase 2 retroactive tagging
- `app/models.py` — `CHDInfo.game_id` / `CHDInfo.title` fields
- `static/js/app.js` — Game ID and Title rows in CHD info modal
- `tests/test_disc_id.py` — Comprehensive disc-ID extraction tests
- `tests/test_metadata.py` — Metadata store disc-ID caching tests

---

## v3.2.3 - Batched Notifications, Deferred UI Updates & Job Index

### 🎨 UI / UX

- **Batched terminal notifications** - Completed, failed, and cancelled job notifications are now aggregated per flush cycle. Instead of one toast per job, a single summary toast is shown (e.g. "Completed 5 jobs", "2 jobs failed"). Individual filenames are still shown when only one job completes.
- **Batched verified-CHD set updates** - `setVerifiedCHDs` is called once per flush with all added/removed paths collected during the batch, eliminating per-job Set cloning.
- **Deferred UI updates during dropdown interaction** - A `deferJobUiUpdatesRef` flag pauses job-driven React re-renders while a `<select>` dropdown (mode, filter, page-size) is focused or has its menu open. This prevents the dropdown from closing mid-selection when an SSE update or poll cycle triggers a state change. Dropdowns set the flag on `focus`/`mousedown` and clear it on `blur`/`change`.
- **Capped placeholder rows** - Optimistic "creating" placeholders are capped at `MAX_VISIBLE_CREATING_PLACEHOLDERS` (100). For larger batches, remaining jobs are counted but not rendered, with an info toast showing the total queued count.

### ⚙️ Reliability & Performance

- **Job index map** - `applyQueuedJobUpdates` now builds a lazy `Map<jobId, index>` via `ensureJobIndex()` on first lookup, replacing O(n) `findIndex` per update with O(1) map lookups. The index is maintained as jobs are inserted or replaced.
- **Extracted `applyPolledJobs` helper** - The poll-interval and initial-fetch merge logic is deduplicated into a single `applyPolledJobs(serverJobs)` function. It also checks `deferJobUiUpdatesRef` to skip state updates while a dropdown is open.
- **Stuck-state polling guard** - `checkStuckStatus` responses are silently discarded when `deferJobUiUpdatesRef` is active, preventing spurious stuck-state banner flickers during dropdown interaction.
- **New-job insertion order** - Hydrated jobs arriving via SSE for unknown IDs are now appended (`push`) instead of prepended (`unshift`), maintaining chronological order and avoiding unnecessary array shifts.

### 📁 Files Changed

- `static/js/app.js` - All changes above: batched notifications, deferred UI flag, `ensureJobIndex`, `applyPolledJobs`, placeholder cap, dropdown `onFocus`/`onBlur`/`onMouseDown` handlers

---

## v3.2.2 - Search View Snapshot & Auto-Return

### ✨ New Features

- **Search view snapshot / restore** - Before "Search All" runs, the current file-list state (entries, archive path, selection, page) is captured. The "← File List" button restores this snapshot exactly, preserving scroll position context instead of re-fetching the directory.
- **Auto-return to file list** - After a successful conversion from search results, the UI automatically restores the pre-search file-list view when `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` is `true` (default). The setting is served from `/api/version` and respected by the frontend at runtime.
- **New config setting** - `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` (default `true`) with legacy alias `CHD_SEARCH_AUTO_RETURN_TO_FILE_LIST` via Pydantic `AliasChoices`.

### 🐞 Bug Fixes

- **Conversion return values** - `executeConversion` and `maybeConfirmDeletePlan` now return `boolean` success indicators, enabling callers to decide post-conversion behavior (e.g. auto-return).
- **Snapshot invalidation** - Pre-search snapshot is cleared on volume switch and directory navigation so stale state is never restored.

### 📖 Documentation

- **README** - Added `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` and legacy alias to env var table.
- **DEPLOYMENT.md** - Added new env var to deployment reference.
- **DOCKER-COMPOSE.md** - Added new env var to compose reference.

### 🧪 Tests

- **Search auto-return config** - New `test_search_auto_return_default_and_legacy_alias` verifies the setting defaults to `true` and respects the legacy `CHD_` alias.

### 📁 Files Changed

- `app/config.py` - `search_auto_return_to_file_list` field with `AliasChoices`
- `app/routes/info.py` - `/api/version` response includes `search_auto_return_to_file_list`
- `static/js/app.js` - `capturePreSearchView` / `restorePreSearchView`, auto-return logic, boolean conversion returns, "← File List" button
- `README.md`, `DEPLOYMENT.md`, `DOCKER-COMPOSE.md` - Env var docs
- `tests/test_volume_discovery.py` - Config default + legacy alias test

---

## v3.2.1 - CI Release Notes from Commit Log

### ⚙️ CI / CD

- **Auto-generated release body** - The `create-release` job now checks out the repo with full history and tags, derives the previous semver tag, and builds a changelog from `git log --no-merges` between the two tags. The result is written to `RELEASE_BODY.md` and passed via `body_path` instead of relying on GitHub's `generate_release_notes`.
- **Previous-tag derivation** - New `previous_tag` output in the `release_meta` step finds the most recent `v*.*.*` tag excluding the current one, enabling accurate commit-range changelogs.
- **Full changelog link** - The generated release body includes a GitHub compare URL (`previous_tag...current_tag`) for easy diff browsing.
- **Fallback handling** - If no previous tag exists (first release) or no non-merge commits are found, sensible defaults are emitted instead of an empty body.

### 📁 Files Changed

- `.github/workflows/docker-image.yml` - Added checkout step with `fetch-depth: 0` and `fetch-tags: true`, commit-log changelog generation, `body_path` release body

---

## v3.2.0 - Job Tabs & Queue Pagination

### ✨ New Features

- **Job tabs** - The jobs panel is now split into three tabs: **Queue** (queued + processing + creating), **Completed**, and **Failed/Cancelled**. Each tab shows its own count. The "Failed/Cancelled" tab auto-hides when empty and auto-switches back to Queue if emptied while active.
- **Job pagination** - Jobs within each tab are paginated with configurable page-size (10 / 25 / 50 / 100 / All), page navigation buttons, and a "Showing X–Y of Z" summary. Current page is clamped when the list shrinks.

### 🎨 UI / UX

- **Tab bar styling** - Pill-shaped tab buttons with uppercase labels, accent-color active state, hover highlights, and full-width equal-sizing on mobile (≤768 px).
- **Job header count** - The "Jobs" heading now displays the combined total across all tabs rather than the raw `jobs.length`.
- **Empty-state messaging** - Each tab shows contextual empty titles and help text (e.g. "Select files and click Convert" for Queue, "Successfully completed jobs will appear here" for Completed).

### 📁 Files Changed

- `static/js/app.js` - Job tab state, `displayedJobs` / `queueJobs` / `completedJobs` / `issueJobs` memos, `jobsPagination` / `paginatedJobs` computed values, tab bar and pagination controls, contextual empty-state props
- `static/css/style.css` - `.job-tabs`, `.job-tab`, `.job-tab.active`, `.job-tab:hover` styles + mobile responsive rules

---

## v3.1.1 - SSE Batching, Verify Concurrency & Test Coverage

### 🐞 Bug Fixes

- **Verify concurrency default** - `MAX_VERIFY_CONCURRENCY` default changed from `2` to `1` to match serial-first processing policy, preventing unexpected parallel verify workloads on low-resource hosts.

### 🎨 UI / UX

- **SSE job-update batching** - Rapid Server-Sent Events are now coalesced in a micro-batch window (`JOB_UPDATE_BATCH_WINDOW_MS`) before applying to React state. Only the latest update per job is kept, drastically reducing re-renders during high-throughput conversions.
- **Lazy state cloning** - `setJobs` updater uses an `ensureMutable()` helper that clones the jobs array at most once per flush, avoiding unnecessary allocations when no fields actually changed.
- **Flush-on-terminal** - Terminal events (`complete`, `error`, `cancelled`) force an immediate flush of the pending batch so the UI reflects final state without waiting for the batch timer.

### 📖 Documentation

- **README** - Updated `MAX_VERIFY_CONCURRENCY` default from `2` to `1` in the environment variable table.

### 🧪 Tests

- **Serial concurrency defaults** - New `test_concurrency_defaults_are_serial` in `tests/test_volume_discovery.py` asserts both `max_concurrent_jobs` and `max_verify_concurrency` default to `1`.

### 📁 Files Changed

- `app/config.py` - `max_verify_concurrency` default `2` → `1`
- `static/js/app.js` - SSE micro-batching, lazy array cloning, flush-on-terminal
- `README.md` - Env var table correction
- `tests/test_volume_discovery.py` - Serial-default assertion test

---

## v3.1.0 - Volume Discovery, Queue Controls & UI Refresh

### ✨ New Features

- **Automatic volume discovery** - When `COMPRESSATORIUM_VOLUMES` is not set, the app discovers mounted libraries from `COMPRESSATORIUM_MOUNT_ROOT/*` at startup. Mount-point children are preferred over plain directories; results are cached for stable runtime behavior.
- **Queue-wide cancellation** - New `POST /api/jobs/cancel-all` endpoint with `X-CHD-Action-Confirm: cancel-all-jobs` header guard. A confirmation modal in the UI prevents accidental bulk cancellation.
- **Clear completed jobs** - New `DELETE /api/jobs/completed` endpoint with `X-CHD-Action-Confirm: clear-completed-jobs` header guard. Adds a "Clear Done" button with a confirmation modal showing the count of jobs to remove.
- **Entrypoint volume discovery** - `entrypoint.sh` now runs the same discover-volumes logic at container startup and logs whether volumes are explicit or auto-discovered, with proper comma-delimited iteration and whitespace trimming.

### 🐞 Bug Fixes

- **Serial queue enforcement** - When `MAX_CONCURRENT_JOBS=1`, the dispatcher now awaits `_run_job()` inline instead of spawning a detached task, preventing race-condition parallel processing.
- **Serial verify default** - `MAX_VERIFY_CONCURRENCY` now defaults to `1` so verification workloads are single-lane unless explicitly increased.
- **Concurrency invariant logging** - `_run_job()` now logs an error if the count of processing jobs exceeds `max_concurrent`, providing fast detection of scheduling bugs.
- **Job lookup continuity** - Recently deleted jobs are archived in memory (TTL 15 min, max 2000 entries) so frontend polling does not immediately 404 after a job is cleared. Archive timestamps refresh on access.
- **z3ds cancellation propagation** - `z3ds_compress.convert()` now catches and re-raises `ConversionCancelled` before the generic `Exception` handler, ensuring clean cancellation logging.
- **Progress update deduplication** - SSE event handler skips `setJobs` when all tracked fields are identical, eliminating unnecessary React re-renders.
- **File-list auto-refresh paused during work** - Auto-refresh interval is suppressed while any jobs are creating, queued, or processing, preventing disruptive list flickers mid-batch.

### 🎨 UI / UX

- **Header and footer logo** - `logo.png` is displayed in the header alongside the title and in the app footer.
- **Favicon** - `index.html` now uses `favicon.ico` from `/static/images/`.
- **Cache-busting** - `app.js` is loaded with a `?v=<version>` query parameter sourced from the backend, and the index response sets `Cache-Control: no-store` to avoid stale JS after deploys.
- **Completion refresh debounce** - File-list refresh after job completion is debounced to reduce churn during rapid batch completions.
- **Progress render throttle** - Per-job progress rendering is throttled so the UI stays responsive during fast SSE bursts.
- **Stale progress ref cleanup** - `progressRenderAtRef` entries are pruned on terminal events and during `mergeJobs`, preventing memory leaks from long sessions.
- **Clear Done confirmation modal** - New `ClearDoneModal` component shows job count and guards accidental clears. Button shows a spinner (`clearingCompletedJobs` state) during the API call.

### ⚙️ Reliability & Maintenance

- **Environment variable standardization** - Preferred env names are `COMPRESSATORIUM_MOUNT_ROOT` and `COMPRESSATORIUM_VOLUMES`. Legacy `CHD_MOUNT_ROOT` / `CHD_VOLUMES` remain supported via Pydantic `AliasChoices`.
- **Startup volume caching** - `Settings.scan_data_mounts_on_startup()` snapshots discovered volumes once at boot so the runtime volume list is stable even if mount points change later.
- **Data mount root config** - New `data_mount_root` setting (default `/data`) controls where the auto-discovery scan looks.

### 📖 Documentation

- **README** - Updated batch conversion docs, added cancel-all / clear-done mentions, documented confirmation headers on destructive API actions, added new 3DS verify endpoints, updated environment variable table with `COMPRESSATORIUM_*` names and legacy aliases, updated example docker-compose snippet.
- **DEPLOYMENT.md** - Refreshed for new environment variable names.
- **DOCKER-COMPOSE.md** - Refreshed for new environment variable names.
- **AGENTS.md** - New agent runbook covering dev, test, Docker, queue API, version sync, and CI workflows.
- **Docker Compose files** - All three compose files (`docker-compose.yml`, `docker-compose.multi-volume.yml`, `docker-compose.cli.yml`) updated from `CHD_VOLUMES` to `COMPRESSATORIUM_MOUNT_ROOT=/data`.
- **run_dev.sh** - Updated to use `COMPRESSATORIUM_MOUNT_ROOT` / `COMPRESSATORIUM_VOLUMES` with fallback to legacy names.

### 🧪 Tests

- **Volume discovery** - New `tests/test_volume_discovery.py` covering explicit volumes, auto-discovery from children, and startup cache stability.
- **Cancel-all confirmation header** - Test that `cancel_all_jobs` rejects requests missing the confirmation header with `400`.
- **Clear-completed confirmation header** - Test that `delete_completed_jobs` rejects requests missing the confirmation header with `400`.
- **Archived job lookup** - Tests for archive retrieval after delete, timestamp refresh on access, and route-level lookup returning archived jobs.
- **Serial dispatcher concurrency** - End-to-end test proving `max_concurrent=1` never exceeds one simultaneous conversion.
- **z3ds / Dolphin test fixtures** - Added `data_mount_root` monkeypatch to `test_z3ds_routes.py`, `test_metadata.py`, `test_dolphin_routes.py`, and `test_mode_parity_fixes.py` to satisfy the new required setting.

### 📁 Files Changed

- `app/config.py` - Volume discovery engine, `data_mount_root`, `AliasChoices`, startup scan cache
- `app/main.py` - Index `Cache-Control: no-store`, version query param on `app.js`
- `app/routes/convert.py` - Cancel-all and clear-completed confirmation header guards, client-IP logging
- `app/services/job_manager.py` - Archived jobs subsystem, serial dispatch, concurrency invariant check
- `app/services/z3ds_compress.py` - `ConversionCancelled` catch-and-reraise
- `entrypoint.sh` - `discover_volumes()` function, volume logging, whitespace-safe iteration
- `static/index.html` - Logo, favicon, cache-busted script tag
- `static/css/style.css` - Header/footer logo styles
- `static/js/app.js` - ClearDoneModal, progress throttle, completion debounce, auto-refresh guard
- `static/js/api.js` - `deleteCompletedJobs` with confirmation header
- `docker-compose.yml`, `docker-compose.multi-volume.yml`, `docker-compose.cli.yml` - `COMPRESSATORIUM_MOUNT_ROOT`
- `run_dev.sh` - New env var names with legacy fallback
- `Dockerfile` - Minor build layer update
- `README.md`, `DEPLOYMENT.md`, `DOCKER-COMPOSE.md` - Doc refresh
- `AGENTS.md` - New agent runbook
- `tests/test_mode_parity_fixes.py` - 6 new test cases + fixture updates
- `tests/test_volume_discovery.py` - New volume discovery tests
- `tests/test_dolphin_routes.py`, `tests/test_z3ds_routes.py`, `tests/test_metadata.py` - Fixture updates

---

## v3.0.1 - Clear Queue Feature

### ✨ New Features

- **Clear Queue** - Added a "Clear Queue" button to the job queue header that allows users to cancel all running and queued jobs at once.
    - **API Endpoint** - `POST /api/jobs/cancel-all` endpoint exposed for bulk cancellation.
    - **UI Integration** - Prominently displayed button in the queue header for quick access.

---

## v3.0.0 - Nintendo 3DS Support & Docker Compose Overhaul

### ✨ New Features

- **Nintendo 3DS Support** - Native support for compressing `.cci`, `.cia`, and `.3ds` ROMs using `z3ds_compress`.
    - **New Tool Option** - Select **3DS** from the main tool selector to access 3DS compression modes.
    - **Supported Formats** - Compress `.cci`, `.cia`, and `.3ds` files to `.zcci`, `.zcia`, and `.z3ds`.
    - **Smart Detection** - Automatically identifies 3DS ROMs and filters the file list.
- **Docker Compose Overhaul** - Complete restructuring of Docker Compose configurations for better usability and deployment flexibility.
    - `docker-compose.yml` - Standard single-volume setup.
    - `docker-compose.multi-volume.yml` - Template for multiple volume mounts.
    - `docker-compose.cli.yml` - Dedicated CLI batch processing configuration.

### ⚠️ Breaking Changes

- **ISO Handling Policy** - The "ISO Handling" setting no longer defaults to Dolphin.
    - **Explicit Selection Required** - Users must now explicitly choose between "CHDMAN" (for PS2/DVD) or "Dolphin" (for GameCube/Wii) when processing `.iso` files.
    - **UI Validation** - The interface prevents conversion of ISO files until a handler is selected, preventing accidental invalid conversions.

### 🐞 Bug Fixes

- **Delete-on-verify messaging** - Corrected messaging for z3ds mode delete-on-verify operations in `static/js/app.js`.
- **Lock Manager** - Fixed `ensure_lock_manager` usage in `services/job_manager.py` to prevent race conditions during z3ds detection.
- **Async Info Method** - Fixed `info()` method in `strategies/z3ds.py` to be properly synchronous within the `run_in_threadpool` wrapper, resolving potential event loop blocking issues.
- **Output Path Logic** - Fixed `treat_as_stem` logic in `get_output_path_for_mode` (routes/convert.py) to correctly handle file extensions.
- **Cancellation Handling** - Standardized usage of `ConversionCancelled` exception in `services/job_manager.py` for reliable job cancellation.
- **Archive Size Checks** - Fixed archive size limit checks in `services/archive.py`.
- **Return Type Consistency** - Improved return type consistency across internal API methods in `routes/info.py`.
- **UI Accessibility** - Increased warning text size and improved color contrast for better readability in `static/css/style.css`.
- **ISO Handling Validation** - Added strict check for `iso_handling` parameter in `routes/convert.py`, rejecting requests where it is null.

### ⚙️ Reliability & Maintenance

- **Periodic Lock Cleanup** - Added `cleanup_stale_locks_periodic` to `JobManager` (services/job_manager.py), running every 10 debug heartbeats (approx. 5 minutes) to automatically remove stale lock files.
- **Z3DS Metadata Optimization** - Added `has_z3ds` and `z3ds_convertible` flags to file search responses in `routes/files.py` to optimize frontend filtering.
- **Conversion Queue Backpressure** - Added queue depth limiting for job creation endpoints:
    - New `MAX_QUEUE_DEPTH` guard applies to `/api/jobs` and `/api/jobs/batch`.
    - Requests now return HTTP `429` when queued + processing jobs exceed configured capacity.
- **Workload Lane Concurrency Controls** - Added lane-specific limits to reduce cross-workload contention:
    - `MAX_VERIFY_CONCURRENCY` caps concurrent CHD/Dolphin/3DS verify workflows.
    - `MAX_METADATA_SCAN_CONCURRENCY` caps concurrent metadata scan tasks.
    - Verify endpoints now fail fast with HTTP `429` when verify capacity is saturated.
- **Adaptive Stall Timeouts** - Conversion stall detection is now size-aware:
    - Uses baseline `CHD_PROGRESS_TIMEOUT`.
    - Adds `CHD_PROGRESS_TIMEOUT_PER_GIB` seconds per GiB of input.
    - Enforces upper bound with `CHD_PROGRESS_TIMEOUT_CAP`.

### 🔧 Technical Details

- **Z3DS Integration** - Implemented `Z3DS_INFO_EXTENSIONS` and `Z3DS_VERIFY_EXTENSIONS` constants for centralized file type management.
- **Path Helper Methods** - Added `_is_z3ds_info_file` and `_is_z3ds_verify_file` helpers in `routes/info.py` for consistent file type checking.
- **Type Hinting** - Updated type hints in `services/chdman.py` and `services/dolphin_tool.py` for better code quality and static analysis.
- **Refactoring** - Extracted `needsIsoSelection` computed variable in `static/js/app.js` for better maintainability.
- **Timeout Policy Helper** - Added `services/timeout_policy.py` to centralize adaptive stall-timeout computation.
- **Workload Limiter Service** - Added `services/workload_limiter.py` to coordinate verify and metadata scan lane capacity.
- **Queue Depth API** - Added `get_queue_depth()` in `services/job_manager.py` for backpressure checks in convert routes.
- **Regression Coverage** - Added tests for queue-capacity `429`, verify-lane `429`, and adaptive timeout math.


### 🛡️ Deployment & Security

- **New Deployment Guide** - `DEPLOYMENT.md` covers security best practices, resource limits, and production hardening.
- **Docker Documentation** - `DOCKER-COMPOSE.md` provides a quick reference for common commands and troubleshooting.
- **Security Audit** - verified path traversal protections, secret scanning, and container security.

### 📁 Files Changed

- `static/js/app.js` - Added 3DS tool logic and frontend integration.
- `app/services/z3ds_compress.py` - New service for 3DS compression.
- `app/routes/info.py` - Fixed async info method patterns.
- `app/routes/convert.py` - Fixed output path logic.
- `README.md` - Added 3DS documentation and Docker Compose sections.
- `DEPLOYMENT.md` - New deployment guide.
- `DOCKER-COMPOSE.md` - New Docker Compose reference.
- `docker-compose*.yml` - New compose files.
- `app/config.py` - Added queue/lane controls and adaptive timeout settings.
- `app/services/timeout_policy.py` - New adaptive stall-timeout helper.
- `app/services/workload_limiter.py` - New lane limiter for verify + metadata scan workloads.
- `app/services/job_manager.py` - Added queue depth accessor for backpressure.
- `app/services/chdman.py`, `app/services/dolphin_tool.py`, `app/services/z3ds_compress.py` - Switched conversion stall checks to adaptive timeout policy.
- `tests/test_timeout_policy.py` - Added adaptive-timeout unit tests.
- `tests/test_mode_parity_fixes.py` - Added conversion queue backpressure tests.
- `tests/test_dolphin_routes.py` - Added verify-lane saturation (`429`) test.

---

## v2.0.1 - Mobile-Responsive Design

### 🎨 UI/UX Improvements

- **Mobile-responsive Web UI** - Complete mobile optimization with card-based file list layout, touch-friendly controls (44-48px minimum touch targets), and single-column layout for screens under 768px.
- **Responsive breakpoints** - Added media queries at 480px, 768px, 900px, and 1200px for seamless experience across all devices.
- **Touch-optimized controls** - All interactive elements meet WCAG accessibility standards with proper touch target sizing.
- **Card-based file list** - On mobile, file list converts from table layout to vertical cards with better information hierarchy.
- **Full-width inputs** - Form controls, dropdowns, and buttons span full width on mobile for easier interaction.
- **Vertical stacking** - ISO handling options, toolbar elements, and compression options stack vertically on mobile.
- **Modal improvements** - Modals now use 95% viewport width on mobile with proper scrolling (90vh max-height).
- **Screenshots documentation** - Added responsive design screenshots to README showcasing desktop, tablet, and mobile views.

### 🔧 Technical Details

- Pure CSS solution with no JavaScript changes required
- Zero breaking changes to desktop functionality
- 627 lines of responsive CSS added
- Desktop layout (3-column) fully preserved for screens ≥1200px

### 📁 Files Changed

- `static/css/style.css` - Added comprehensive mobile-responsive styles with multiple breakpoints
- `README.md` - Added Screenshots section with responsive design examples
- `docs-desktop-view.png`, `docs-tablet-view.png`, `docs-mobile-view.png` - Added documentation screenshots

### ✨ New Features

- **Archive delete-on-verify** - Archive inputs can now delete the entire archive after a successful conversion + verification, with an explicit warning in the delete plan.

---

## v1.2.1 - Archive Safety Limits & Timeout Controls

### ✨ New Features

- **Archive safety limits** - Configure maximum archive entries, per-member size, and total extraction size with `CHD_ARCHIVE_MAX_ENTRIES`, `CHD_ARCHIVE_MAX_MEMBER_SIZE`, and `CHD_ARCHIVE_MAX_TOTAL_SIZE`.
- **Archive truncation metadata** - File listing/search responses now report when archive listings are truncated by safety limits.
- **Verification timeouts** - New `CHD_VERIFY_TIMEOUT` and `CHD_VERIFY_PROGRESS_TIMEOUT` allow you to stop long-running or stalled `chdman verify` operations.

### 🛡️ Safety Improvements

- **Output directory validation** - Output directories are trimmed and rejected if empty, preventing accidental writes to invalid paths.
- **Safe temp cleanup** - Temporary directories are only removed if they are within expected temp locations.
- **Chdman info timeout** - `CHD_INFO_TIMEOUT` prevents `chdman info` from hanging indefinitely.

### 🐞 Bug Fixes

- **Archive enumeration errors** - Directory scans skip problematic entries instead of failing entire requests.
- **Output path creation** - Output directories are only created when a directory component exists.

### 📁 Files Changed

- `app/config.py` - Added archive and timeout configuration values
- `app/models.py` - Archive truncation metadata
- `app/routes/convert.py` - Output directory validation and reuse
- `app/routes/files.py` - Archive truncation metadata + safe scanning
- `app/services/archive.py` - Archive limits + truncated listings
- `app/services/chdman.py` - Timeout handling for info/verify
- `app/services/concurrency_manager.py` - Ticket lock handling improvements
- `app/services/job_manager.py` - Safe temp cleanup checks
- `README.md` - Archive limit and timeout documentation

---

## v1.2.0 - Delete-on-Verify & Safer File Ops

### ✨ New Features

- **Delete-on-verify** - Optional post-conversion verification that deletes the original source only after a successful CHD verify (create/copy modes).
- **Delete plan confirmation** - New `/api/jobs/delete-plan` endpoint + UI modal showing exactly which files will be removed before conversion starts.
- **Track-aware deletes** - `.cue`/`.gdi` companion tracks are included in the delete plan and removed as a set.

### 🛡️ Safety Improvements

- **Snapshot + fingerprint validation** - Delete plans are revalidated at completion and must match original fingerprints before any deletion.
- **In-use protection** - File delete/rename operations are blocked while a path is used by an active job (including cue/gdi track files).
- **Lock hygiene** - Hash-based lock filenames and startup cleanup for stale file locks.
- **Cancel-safe** - If a cancel occurs after verify, deletion is skipped.

### 🎛️ UI/UX

- **Always-visible Select All** checkbox with indeterminate state.
- **Conversion panel refresh** with clearer post-conversion options and copy-mode warnings.

### 📁 Files Changed

- `app/utils/delete_plan.py` - Track parsing, delete plan snapshotting, safety checks
- `app/services/job_manager.py` - Delete-on-verify orchestration + safety validation
- `app/routes/convert.py` - Delete plan endpoint and request validation
- `app/routes/files.py` - In-use path blocking for rename/delete
- `app/services/lock_manager.py` - Lock directory management and cleanup
- `static/js/app.js` - Delete-on-verify UI + Select All + layout updates
- `static/js/api.js` - Delete plan API
- `static/css/style.css` - Toolbar/options layout styling
- `README.md` - Feature and API docs

---

## v1.1.5 - Archive Conversion Safety & Stall Watchdog

### 🐞 Bug Fixes

- **Archive member selection** - When both `.cue`/`.gdi` and `.bin` exist in the same archive folder, `.bin` entries are now suppressed. This prevents conversions from starting with an incomplete input set (missing TOC/track layout), which can stall `chdman` and never reach completion.
- **Batch dedupe by output path** - Batch job creation now keeps only one job per output CHD and prefers `.cue`/`.gdi` > `.iso` > `.bin` when multiple archive members map to the same output. This avoids duplicate work, conflicting locks, and stuck jobs.
- **Stall watchdog** - New `CHD_PROGRESS_TIMEOUT` fails a conversion if both progress and output size stay unchanged for the configured period (default 600s). The job is marked failed with a clear error instead of lingering at 99%.

### 📁 Files Changed

- `app/services/archive.py` - Prefer `.cue`/`.gdi` over `.bin` for archive listings
- `app/routes/convert.py` - Deduplicate batch jobs by output path and prioritize safe inputs
- `app/services/chdman.py` - Conversion stall detection with timeout and clear failure message
- `app/config.py` - New `CHD_PROGRESS_TIMEOUT` setting
- `README.md` - Archive behavior and timeout docs
- `DOCKER-COMPOSE.md` / `DEPLOYMENT.md` - Added `CHD_PROGRESS_TIMEOUT`

---

## v1.1.4 - Python 3.8 Compatibility Fix

### 🐞 Bug Fix

- **Conversion completion regression** - On Python 3.8, the new `list[str]` annotation in `app/services/chdman.py` raises `TypeError: 'type' object is not subscriptable` at runtime. That exception happens inside the conversion generator before the "complete" event is emitted, so jobs never transition to `completed` on the frontend even if `chdman` finishes. The annotation is now `typing.List[str]` to keep Python 3.8 compatibility.
- **Guardrail test** - Added a test that fails if `list[...]` annotations appear in `chdman.py` without `from __future__ import annotations`, preventing this regression.

### 📁 Files Changed

- `app/services/chdman.py` - Python 3.8-safe annotation for output buffering
- `tests/test_chdman_annotations.py` - Regression test for annotation compatibility

---

## v1.1.1 - Async I/O & Reliability Improvements

### 🔧 Internal Improvements

- **Async I/O Refactor** - Filesystem operations on request paths (info, files, stores) now offload to threadpool, preventing event loop blocking
- **Version-Gated Persistence** - Metadata and verification stores implement last-write-wins with version checks to prevent stale overwrites
- **Lock Order Consistency** - Eliminated potential deadlocks between sync and async persistence paths
- **Timezone-Aware Timestamps** - Replaced deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)`
- **Concurrency Tests** - Added test coverage for concurrent metadata/verification store writes

### 📁 Files Changed

- `app/services/chd_metadata_store.py` - Async persistence, version-gated replace
- `app/services/verification_store.py` - Async I/O, version-gated replace  
- `app/services/job_manager.py` - Timezone-aware timestamps
- `app/routes/info.py` - Threadpool offloading for filesystem checks
- `app/routes/files.py` - Async filesystem operations
- `tests/test_metadata.py` - Concurrency tests
- `tests/test_verification_store.py` - Concurrency tests
- `walkthrough.md` - Updated async safety documentation

---

# Release Notes - v1.0.0

## 🎉 Major Release: CHD Metadata Caching & Version System

This release introduces significant new features including intelligent CHD metadata caching, a unified version system, and enhanced UI capabilities.

---

## ✨ New Features

### CHD Metadata Caching System
- **Persistent metadata cache** - CHD file metadata is now cached to disk, avoiding repeated `chdman info` calls
- **Automatic cache invalidation** - Uses file modification time (mtime) to detect stale entries
- **Media type detection** - Automatically identifies DVD vs CD media types from CHD metadata
- **Background metadata scanning** - New "Scan Metadata" button triggers async scanning of all volumes
- **Batch metadata API** - Frontend can request metadata for multiple CHDs in a single call

### Version Management
- **Single source of truth** - Version now managed via `.version` file at project root
- **API endpoint** - New `/api/version` endpoint returns current version
- **Footer display** - Version shown in application footer with GitHub link
- **CI integration** - Docker workflow reads version from `.version` for image tags
- **Sync script** - `scripts/sync-version.sh` keeps package.json in sync

### UI Enhancements
- **File type filtering** - Filter file list by CHD files, archives, or disc images
- **Shift-click selection** - Select ranges of files with Shift+Click
- **Media type badges** - CHD files display DVD/CD badge when metadata is cached
- **Selection pruning** - Selected files are pruned when filter hides them

---

## 🔧 Technical Improvements

### Performance
- **Non-blocking persistence** - Metadata cache writes use thread pool to avoid blocking event loop
- **Batched updates** - Background scan accumulates changes and writes once at end
- **Threaded filesystem traversal** - `os.walk` runs in thread pool during metadata scans

### Concurrency Safety
- **Version-tracked writes** - Prevents stale async writes from overwriting newer data
- **Lock ordering** - Consistent lock acquisition order prevents deadlocks
- **Safe background tasks** - Fire-and-forget tasks wrapped with error logging

### Error Handling
- **Graceful version fallback** - Returns "0.0.0" if version file unreadable
- **Import compatibility** - Version endpoint works with both `uvicorn main:app` and `uvicorn app.main:app`

---

## 📁 Files Changed

- `.version` - New version source of truth
- `scripts/sync-version.sh` - New version sync utility
- `app/services/chd_metadata_store.py` - New metadata caching service
- `app/routes/info.py` - Added metadata and version endpoints
- `app/main.py` - Dynamic version reading
- `app/models.py` - Added MetadataBatchRequest model
- `static/js/app.js` - File filtering, shift-select, badges
- `static/js/api.js` - Metadata and version API methods
- `static/css/style.css` - Badge and filter styling
- `static/index.html` - Added useMemo hook
- `.github/workflows/docker-image.yml` - Version-based tagging
- `package.json` - Updated version

---

## 🔄 Upgrade Notes

This release is backwards compatible. The metadata cache will be built automatically as CHD files are accessed or when "Scan Metadata" is clicked.
