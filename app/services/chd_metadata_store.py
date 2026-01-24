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
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

# Shared executor for async file I/O
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chd_metadata")


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
        """Synchronous persist - use _persist_async for non-blocking writes."""
        with self._write_lock:
            # Use unique temp file to avoid conflicts
            tmp_path = self._store_path.with_suffix(f".tmp.{os.getpid()}")
            try:
                with tmp_path.open("w", encoding="utf-8") as fh:
                    json.dump(self._records, fh, indent=2)
                tmp_path.replace(self._store_path)
                self._dirty = False
            finally:
                # Clean up temp file if it still exists
                if tmp_path.exists():
                    tmp_path.unlink()

    async def _persist_async(self):
        """Non-blocking persist using thread pool executor."""
        loop = asyncio.get_event_loop()
        # Capture version and snapshot under lock
        with self._lock:
            snapshot_version = self._version
            records_copy = dict(self._records)
        
        def do_write():
            # Acquire locks in correct order: _lock then _write_lock
            # This matches the order used by sync _persist (called with _lock held)
            with self._lock:
                # Re-check version under lock
                if self._version != snapshot_version:
                    return False  # Stale, skip write
                
                with self._write_lock:
                    # Use unique temp file to avoid conflicts
                    tmp_path = self._store_path.with_suffix(f".tmp.{os.getpid()}")
                    try:
                        with tmp_path.open("w", encoding="utf-8") as fh:
                            json.dump(records_copy, fh, indent=2)
                        tmp_path.replace(self._store_path)
                        # Clear dirty here while we still hold _lock
                        self._dirty = False
                        return True
                    finally:
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
        
        Looks at the 'metadata' field or raw_data for Tag information.
        Returns 'dvd' if Tag contains DVD, 'cd' if it contains CD/CHT patterns.
        """
        raw_data = info.get("raw_data", "")
        
        # Look for metadata lines containing Tag info
        # Example patterns:
        # - "Metadata: Tag:GDROM"
        # - "Metadata: CHCD, Tag: CD-ROM"
        # - "Tag: DVD-VIDEO"
        
        tag_match = re.search(r"Tag:\s*([^,]+)", raw_data, re.IGNORECASE)
        if tag_match:
            tag_value = tag_match.group(1).upper()
            if "DVD" in tag_value:
                return "dvd"
            if "CD" in tag_value or "GDROM" in tag_value:
                return "cd"
        
        # Fallback: check for common patterns in metadata field
        metadata = info.get("metadata", "")
        if isinstance(metadata, str):
            metadata_upper = metadata.upper()
            if "DVD" in metadata_upper:
                return "dvd"
            if "CD" in metadata_upper or "GDROM" in metadata_upper:
                return "cd"
        
        # Additional heuristic: check compression type for CD-specific codecs
        compression = info.get("compression", "")
        if isinstance(compression, str):
            cd_codecs = ["cdzl", "cdzs", "cdlz", "cdfl"]
            if any(codec in compression.lower() for codec in cd_codecs):
                return "cd"
        
        return None

    def get_metadata(self, chd_path: str) -> Optional[dict]:
        """Get cached metadata for a CHD file."""
        normalized = self._normalize_path(chd_path)
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return None
            return record.get("metadata")

    def get_media_type(self, chd_path: str) -> Optional[str]:
        """Get just the media type (dvd/cd) for a CHD file."""
        normalized = self._normalize_path(chd_path)
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return None
            return record.get("media_type")

    def is_stale(self, chd_path: str) -> bool:
        """Check if cached metadata is stale (file modified since caching)."""
        normalized = self._normalize_path(chd_path)
        
        try:
            current_mtime = os.path.getmtime(normalized)
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

    def set_metadata(self, chd_path: str, info: dict, persist: bool = True) -> dict:
        """
        Cache metadata for a CHD file.
        
        Args:
            chd_path: Path to the CHD file
            info: Full chdman info output dict
            persist: If True, immediately persist to disk. Set to False for batch updates.
            
        Returns:
            The record that was stored (includes extracted media_type)
        """
        normalized = self._normalize_path(chd_path)
        
        try:
            mtime = os.path.getmtime(normalized)
        except OSError:
            mtime = None
        
        media_type = self.extract_media_type(info)
        
        record = {
            "chd_path": normalized,
            "metadata": info,
            "media_type": media_type,
            "mtime": mtime,
            "cached_at": datetime.utcnow().isoformat() + "Z",
        }
        
        with self._lock:
            self._records[normalized] = record
            self._dirty = True
            self._version += 1  # Track modifications for async persist
            if persist:
                self._persist()
        
        return record

    def clear(self, chd_path: str):
        """Remove cached metadata for a CHD file."""
        normalized = self._normalize_path(chd_path)
        with self._lock:
            if normalized in self._records:
                del self._records[normalized]
                self._version += 1
                self._dirty = True
                self._persist()

    def move(self, old_path: str, new_path: str):
        """Update cache when a CHD file is renamed/moved."""
        old_normalized = self._normalize_path(old_path)
        new_normalized = self._normalize_path(new_path)
        with self._lock:
            record = self._records.pop(old_normalized, None)
            if record is None:
                return
            record["chd_path"] = new_normalized
            # Update mtime for new path
            try:
                record["mtime"] = os.path.getmtime(new_normalized)
            except OSError:
                record["mtime"] = None
            self._records[new_normalized] = record
            self._version += 1
            self._dirty = True
            self._persist()

    def get_batch(self, chd_paths: list) -> Dict[str, dict]:
        """Get cached metadata for multiple CHD files at once."""
        result = {}
        with self._lock:
            for path in chd_paths:
                normalized = self._normalize_path(path)
                record = self._records.get(normalized)
                if record is not None:
                    result[path] = {
                        "media_type": record.get("media_type"),
                        "cached": True,
                    }
        return result

    def all_records(self):
        """Return all cached records."""
        with self._lock:
            return list(self._records.values())

    def prune_missing(self) -> int:
        """Remove cache entries for CHD files that no longer exist."""
        removed = []
        with self._lock:
            for path in list(self._records.keys()):
                if not os.path.exists(path):
                    removed.append(path)
            if removed:
                for path in removed:
                    del self._records[path]
                self._version += 1
                self._dirty = True
                self._persist()
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
        with self._lock:
            if self._dirty:
                self._persist()


# Singleton instance
chd_metadata_store = CHDMetadataStore()
