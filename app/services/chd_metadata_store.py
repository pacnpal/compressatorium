"""
Persistent store for CHD file metadata (chdman info output).
Caches metadata to avoid re-running chdman info on every request.
Uses file modification time (mtime) for cache invalidation.
"""

import asyncio
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from fastapi.concurrency import run_in_threadpool

# Shared executor for async file I/O
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chd_metadata")

# =============================================================================
# MEDIA TYPE DETECTION - Single Source of Truth
# =============================================================================
# Tag patterns that identify DVD media
DVD_TAG_PATTERNS = frozenset(["DVD", "DVD-VIDEO", "DVD-ROM"])

# Tag patterns that identify CD media (checked after DVD patterns)
CD_TAG_PATTERNS = frozenset(["CD", "CD-ROM", "CDROM", "GDROM", "GD-ROM"])

# Metadata prefixes that identify CD media (e.g., CHCD, CHT2, CHTR)
CD_METADATA_PREFIXES = ("CHCD", "CHT2", "CHTR", "CHT")

# CD-specific compression codecs (fallback detection)
CD_COMPRESSION_CODECS = frozenset(["cdzl", "cdzs", "cdlz", "cdfl"])
# =============================================================================


class CHDMetadataStore:
    """Persists CHD metadata across application restarts."""

    def __init__(self, store_path: Optional[str] = None):
        base_path = store_path or os.environ.get("CHD_METADATA_STORE")
        if base_path:
            self._store_path = Path(base_path)
        else:
            default_dir = Path(os.environ.get("CHD_DATA_DIR", "/config"))
            self._store_path = default_dir / "chd_metadata.json"
        self._store_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._records: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        if self._store_path.exists():
            try:
                with self._store_path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        self._records = data
            except json.JSONDecodeError:
                # Corrupted cache, start fresh but keep file for inspection
                backup = self._store_path.with_suffix(".corrupt")
                self._store_path.rename(backup)
                self._records = {}
        else:
            self._records = {}
        
        # Dirty flag for batched updates
        self._dirty = False
        # Version counter for dirty tracking (monotonically increasing)
        self._version = 0
        # Separate lock for serializing file writes
        self._write_lock = threading.Lock()

    def _persist(self):
        """
        Synchronous persist - writes self._records to disk.
        
        Uses version checking to ensure consistent writes while minimizing
        lock hold time. Implements last-write-wins: only commits if version
        hasn't changed since snapshot.
        """
        # Capture version and snapshot under lock
        with self._lock:
            snapshot_version = self._version
            records_snapshot = dict(self._records)
        
        # Do file I/O outside _lock to minimize lock contention
        with self._write_lock:
            tmp_path = self._store_path.with_suffix(f".tmp.{os.getpid()}")
            try:
                with tmp_path.open("w", encoding="utf-8") as fh:
                    json.dump(records_snapshot, fh, indent=2)
                
                # Version-gated replace: only commit if version still matches
                with self._lock:
                    if self._version == snapshot_version:
                        tmp_path.replace(self._store_path)
                        self._dirty = False
                    # else: discard stale write, newer snapshot will be written
            finally:
                # Clean up temp file if it still exists (stale or failed)
                if tmp_path.exists():
                    tmp_path.unlink()

    async def _persist_async(self):
        """Non-blocking persist using thread pool executor.
        
        Uses the same pattern as sync _persist:
        1. Snapshot under _lock
        2. Write to temp under _write_lock
        3. Version-gated replace under _lock (only commits if version matches)
        """
        loop = asyncio.get_event_loop()
        
        # Capture version and snapshot under lock (fast, in-memory)
        with self._lock:
            snapshot_version = self._version
            records_copy = dict(self._records)
        
        def do_write():
            # File I/O under _write_lock only (not holding _lock)
            with self._write_lock:
                tmp_path = self._store_path.with_suffix(f".tmp.{os.getpid()}")
                try:
                    with tmp_path.open("w", encoding="utf-8") as fh:
                        json.dump(records_copy, fh, indent=2)
                    
                    # Version-gated replace: only commit if version still matches
                    with self._lock:
                        if self._version == snapshot_version:
                            tmp_path.replace(self._store_path)
                            self._dirty = False
                            return True
                        # else: discard stale write, newer snapshot will be written
                        return False
                finally:
                    # Clean up temp file if it still exists (stale or failed)
                    if tmp_path.exists():
                        tmp_path.unlink()
        
        try:
            await loop.run_in_executor(_executor, do_write)
        except Exception:
            # On failure, dirty flag remains True so next flush will retry
            raise

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.realpath(path)

    @staticmethod
    def extract_media_type(info: dict) -> Optional[str]:
        """
        Extract media type (dvd/cd) from CHD metadata.
        
        Uses patterns defined in MEDIA_TYPE_PATTERNS constants.
        Returns 'dvd' if Tag contains DVD, 'cd' if it contains CD patterns.
        """
        raw_data = info.get("raw_data", "")

        tag_values = set()
        metadata_tags = set()

        # Look for metadata lines containing Tag info
        # Example patterns:
        # - "Metadata: Tag:GDROM"
        # - "Metadata: CHCD, Tag: CD-ROM"
        # - "Tag: DVD-VIDEO"
        for line in raw_data.splitlines():
            if not line:
                continue
            for match in re.finditer(r"Tag\s*[:=]\s*([^,]+)", line, re.IGNORECASE):
                tag_values.add(match.group(1).strip().upper())
            meta_match = re.search(r"^\s*Metadata:\s*([^,]+)", line, re.IGNORECASE)
            if meta_match:
                metadata_tags.add(meta_match.group(1).strip().upper())

        metadata_lines = info.get("metadata_lines")
        if isinstance(metadata_lines, list):
            for entry in metadata_lines:
                if not isinstance(entry, str):
                    continue
                metadata_tags.add(entry.strip().upper())

        def matches_dvd(value: str) -> bool:
            """Check if value matches any DVD pattern."""
            normalized = value.upper()
            return any(pat in normalized for pat in DVD_TAG_PATTERNS)

        def matches_cd(value: str) -> bool:
            """Check if value matches any CD pattern."""
            normalized = re.sub(r"[^A-Z0-9]", "", value.upper())
            return any(pat.replace("-", "") in normalized for pat in CD_TAG_PATTERNS)

        # Check tag values for DVD first (higher priority)
        for tag_value in tag_values:
            if matches_dvd(tag_value):
                return "dvd"
        for tag_value in tag_values:
            if matches_cd(tag_value):
                return "cd"

        # Check metadata tags for DVD first
        for meta_value in metadata_tags:
            if matches_dvd(meta_value):
                return "dvd"
        for meta_value in metadata_tags:
            # Check CD metadata prefixes (CHCD, CHT2, CHTR, etc.)
            if any(meta_value.startswith(prefix) for prefix in CD_METADATA_PREFIXES):
                return "cd"
            if matches_cd(meta_value):
                return "cd"

        # Fallback: check for common patterns in metadata field
        metadata = info.get("metadata", "")
        if isinstance(metadata, str):
            metadata_upper = metadata.upper()
            if any(pat in metadata_upper for pat in DVD_TAG_PATTERNS):
                return "dvd"
            if any(pat.replace("-", "") in metadata_upper for pat in CD_TAG_PATTERNS):
                return "cd"
            if any(metadata_upper.startswith(prefix) for prefix in CD_METADATA_PREFIXES):
                return "cd"

        # Additional heuristic: check compression type for CD-specific codecs
        compression = info.get("compression", "")
        if isinstance(compression, str):
            if any(codec in compression.lower() for codec in CD_COMPRESSION_CODECS):
                return "cd"
        
        return None

    async def get_metadata(self, chd_path: str) -> Optional[dict]:
        """Get cached metadata for a CHD file."""
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return None
            return record.get("metadata")

    async def get_media_type(self, chd_path: str) -> Optional[str]:
        """Get just the media type (dvd/cd) for a CHD file."""
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return None
            return record.get("media_type")

    async def is_stale(self, chd_path: str) -> bool:
        """Check if cached metadata is stale (file modified since caching)."""
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        
        try:
            current_mtime = await run_in_threadpool(os.path.getmtime, normalized)
        except OSError:
            # File doesn't exist or not accessible - treat as stale
            return True
        
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return True
            cached_mtime = record.get("mtime")
            if cached_mtime is None:
                return True
            return current_mtime != cached_mtime

    async def set_metadata(self, chd_path: str, info: dict, persist: bool = True) -> dict:
        """
        Cache metadata for a CHD file.
        
        Args:
            chd_path: Path to the CHD file
            info: Full chdman info output dict
            persist: If True, immediately persist to disk. Set to False for batch updates.
            
        Returns:
            The record that was stored (includes extracted media_type)
        """
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        
        try:
            mtime = await run_in_threadpool(os.path.getmtime, normalized)
        except OSError:
            mtime = None
        
        # CPU bound, but fast enough to run on event loop usually. 
        # If very complex, could offload, but extract_media_type is regex/string ops.
        media_type = self.extract_media_type(info)
        
        record = {
            "chd_path": normalized,
            "metadata": info,
            "media_type": media_type,
            "mtime": mtime,
            "cached_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        
        should_persist = False
        with self._lock:
            self._records[normalized] = record
            self._dirty = True
            self._version += 1  # Track modifications for async persist
            should_persist = persist
        
        if should_persist:
            await self._persist_async()
        
        return record

    async def clear(self, chd_path: str):
        """Remove cached metadata for a CHD file."""
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        should_persist = False
        with self._lock:
            if normalized in self._records:
                del self._records[normalized]
                self._version += 1
                self._dirty = True
                should_persist = True
        
        if should_persist:
            await self._persist_async()

    async def move(self, old_path: str, new_path: str):
        """Update cache when a CHD file is renamed/moved."""
        old_normalized = await run_in_threadpool(self._normalize_path, old_path)
        new_normalized = await run_in_threadpool(self._normalize_path, new_path)
        
        # Pre-fetch mtime for new path to minimize lock time and ensure atomicity of the swap
        try:
            new_mtime = await run_in_threadpool(os.path.getmtime, new_normalized)
        except OSError:
            new_mtime = None

        should_persist = False
        with self._lock:
            # Atomic check-and-move
            if old_normalized in self._records:
                record = self._records.pop(old_normalized)
                
                # Update record structure
                record["chd_path"] = new_normalized
                record["mtime"] = new_mtime
                self._records[new_normalized] = record
                
                self._version += 1
                self._dirty = True
                should_persist = True
        
        if should_persist:
            await self._persist_async()

    async def get_batch(self, chd_paths: list) -> Dict[str, dict]:
        """Get cached metadata for multiple CHD files at once."""
        result = {}
        # Normalize all paths first (offloaded)
        # We can do this in parallel or serial. Serial is fine for now as it's run_in_threadpool.
        # Actually, let's just do it one by one or mapped.
        normalized_map = {}
        for path in chd_paths:
            normalized_map[path] = await run_in_threadpool(self._normalize_path, path)

        with self._lock:
            for original_path, normalized in normalized_map.items():
                record = self._records.get(normalized)
                if record is not None:
                    result[original_path] = {
                        "media_type": record.get("media_type"),
                        "cached": True,
                    }
        return result

    async def get_full_info(self, chd_path: str) -> tuple[Optional[dict], Optional[str]]:
        """Get both metadata and media_type in one call."""
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return None, None
            return record.get("metadata"), record.get("media_type")

    def all_records(self):
        """Return all cached records."""
        with self._lock:
            return list(self._records.values())

    async def prune_missing(self) -> int:
        """Remove cache entries for CHD files that no longer exist."""
        def _check_and_prune():
            # 1. Snapshot keys safely
            with self._lock:
                keys = list(self._records.keys())
            
            # 2. Check existence (blocking I/O)
            removed = []
            for path in keys:
                if not os.path.exists(path):
                    removed.append(path)
            
            # 3. Remove missing from records with lock AND snapshot for persist
            should_persist = False
            if removed:
                with self._lock:
                    for path in removed:
                        if path in self._records:
                            del self._records[path]
                    self._version += 1
                    self._dirty = True
                    should_persist = True
            
            # 4. Return whether we need to persist and the count
            return removed, should_persist

        removed, should_persist = await run_in_threadpool(_check_and_prune)
        
        if should_persist:
            await self._persist_async()
        
        return len(removed)

    def is_dirty(self) -> bool:
        """Check if there are unpersisted changes."""
        with self._lock:
            return self._dirty

    async def flush_async(self):
        """Persist any dirty changes asynchronously."""
        if self.is_dirty():
            await self._persist_async()

    def flush(self):
        """Persist any dirty changes synchronously."""
        should_persist = False
        with self._lock:
            if self._dirty:
                should_persist = True
        
        if should_persist:
            self._persist()


# Singleton instance
chd_metadata_store = CHDMetadataStore()
