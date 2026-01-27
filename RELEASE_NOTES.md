# Release Notes

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
