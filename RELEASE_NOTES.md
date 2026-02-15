# Release Notes

## Unreleased

(No unreleased changes)

---

## v3.0.1 - Clear Queue Feature

### ✨ New Features

- **Clear Queue** - Added a "Clear Queue" button to the job queue header that allows users to cancel all running and queued jobs at once.
    - **API Endpoint** - `DELETE /api/jobs` endpoint exposed for bulk cancellation.
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
