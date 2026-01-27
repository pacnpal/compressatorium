import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from fastapi.concurrency import run_in_threadpool


class VerificationStore:
    """Persists CHD verification results across application restarts."""

    def __init__(self, store_path: Optional[str] = None):
        base_path = store_path or os.environ.get("CHD_VERIFICATION_STORE")
        if base_path:
            self._store_path = Path(base_path)
        else:
            default_dir = Path(os.environ.get("CHD_DATA_DIR", "/config"))
            self._store_path = default_dir / "verified_chds.json"
        self._store_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._records: Dict[str, Dict[str, Optional[str]]] = {}
        self._version = 0
        self._last_persisted_version = 0
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
        
        # Reset version after load
        self._version = 0
        self._last_persisted_version = 0

    def _persist(self):
        """
        Synchronously persist records to disk. Should be called in threadpool.
        Acquires _write_lock to serialize writes.
        Checks if persistence is needed by comparing _version with _last_persisted_version.
        """
        # Acquire write lock first to serialize disk operations
        with self._write_lock:
            # Check if we need to write
            with self._lock:
                if self._version <= self._last_persisted_version:
                    return
                snapshot = dict(self._records)
                version_to_write = self._version
            
            # Perform serialization to temp file without holding the main _lock
            tmp_path = self._store_path.with_suffix(f".tmp.{os.getpid()}")
            try:
                with tmp_path.open("w", encoding="utf-8") as fh:
                    json.dump(snapshot, fh, indent=2)
                
                # Critical section: Check version again and replace under lock
                # We only replace if the file on disk corresponds to version_to_write.
                # If _version changed in the meantime, we DO NOT write stale data.
                # We rely on the fact that another _persist task is queued for the newer version.
                with self._lock:
                    if self._version == version_to_write:
                        tmp_path.replace(self._store_path)
                        self._last_persisted_version = version_to_write
                    # If version changed, we discard this write. The next task handles it.
            except Exception:
                if tmp_path.exists():
                    tmp_path.unlink()
                raise
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.realpath(path)

    async def mark_verified(self, chd_path: str, *, source_path: Optional[str] = None):
        # Normalize paths in threadpool to avoid blocking main loop with disk I/O
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        normalized_source = None
        if source_path:
            normalized_source = await run_in_threadpool(self._normalize_path, source_path)

        record = {
            "chd_path": normalized,
            "source_path": normalized_source,
            "verified_at": datetime.utcnow().isoformat() + "Z",
        }
        
        # Update in-memory state
        with self._lock:
            self._records[normalized] = record
            self._version += 1
            
        # Trigger persistence
        await run_in_threadpool(self._persist)

    async def clear(self, chd_path: str):
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        should_persist = False
        
        with self._lock:
            if normalized in self._records:
                del self._records[normalized]
                self._version += 1
                should_persist = True
        
        if should_persist:
            await run_in_threadpool(self._persist)

    async def move(self, old_path: str, new_path: str):
        old_normalized = await run_in_threadpool(self._normalize_path, old_path)
        new_normalized = await run_in_threadpool(self._normalize_path, new_path)
        should_persist = False
        
        with self._lock:
            # Use pop to remove old record
            old_record = self._records.pop(old_normalized, None)
            if old_record:
                # Create a NEW record dict (copy) to avoid mutating shared state 
                new_record = old_record.copy()
                new_record["chd_path"] = new_normalized
                self._records[new_normalized] = new_record
                
                self._version += 1
                should_persist = True
            
        if should_persist:
            await run_in_threadpool(self._persist)

    async def is_verified(self, chd_path: str) -> bool:
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        with self._lock:
            return normalized in self._records

    async def get_record(self, chd_path: str) -> Optional[Dict[str, Optional[str]]]:
        normalized = await run_in_threadpool(self._normalize_path, chd_path)
        with self._lock:
            return self._records.get(normalized)

    def all_records(self):
        with self._lock:
            return list(self._records.values())

    async def prune_missing(self) -> int:
        """Remove cache entries for CHD files that no longer exist."""
        def _check_and_prune():
            # This method runs in threadpool.
            # 1. Snapshot keys safely
            with self._lock:
                keys = list(self._records.keys())
            
            # 2. Check existence (blocking I/O)
            missing = []
            for path in keys:
                if not os.path.exists(path):
                    missing.append(path)
            
            # 3. Remove missing from records with lock AND snapshot for persist
            removed_count = 0
            if missing:
                with self._lock:
                    for path in missing:
                        if path in self._records:
                            del self._records[path]
                    self._version += 1
                removed_count = len(missing)
                
                # 4. Persist
                self._persist()
                
            return removed_count

        return await run_in_threadpool(_check_and_prune)


verification_store = VerificationStore()
