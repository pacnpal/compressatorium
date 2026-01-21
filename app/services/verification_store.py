import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional


class VerificationStore:
    """Persists CHD verification results across application restarts."""

    def __init__(self, store_path: Optional[str] = None):
        base_path = store_path or os.environ.get("CHD_VERIFICATION_STORE")
        if base_path:
            self._store_path = Path(base_path)
        else:
            default_dir = Path(os.environ.get("CHD_DATA_DIR", "app/data"))
            self._store_path = default_dir / "verified_chds.json"
        self._store_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._records: Dict[str, Dict[str, Optional[str]]] = {}
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

    def _persist(self):
        tmp_path = self._store_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(self._records, fh, indent=2)
        tmp_path.replace(self._store_path)

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.realpath(path)

    def mark_verified(self, chd_path: str, *, source_path: Optional[str] = None):
        normalized = self._normalize_path(chd_path)
        record = {
            "chd_path": normalized,
            "source_path": self._normalize_path(source_path) if source_path else None,
            "verified_at": datetime.utcnow().isoformat() + "Z"
        }
        with self._lock:
            self._records[normalized] = record
            self._persist()

    def clear(self, chd_path: str):
        normalized = self._normalize_path(chd_path)
        with self._lock:
            if normalized in self._records:
                del self._records[normalized]
                self._persist()

    def move(self, old_path: str, new_path: str):
        old_normalized = self._normalize_path(old_path)
        new_normalized = self._normalize_path(new_path)
        with self._lock:
            record = self._records.pop(old_normalized, None)
            if record is None:
                return
            record["chd_path"] = new_normalized
            self._records[new_normalized] = record
            self._persist()

    def is_verified(self, chd_path: str) -> bool:
        normalized = self._normalize_path(chd_path)
        with self._lock:
            return normalized in self._records

    def get_record(self, chd_path: str) -> Optional[Dict[str, Optional[str]]]:
        normalized = self._normalize_path(chd_path)
        with self._lock:
            return self._records.get(normalized)

    def all_records(self):
        with self._lock:
            return list(self._records.values())

    def prune_missing(self) -> int:
        removed = []
        with self._lock:
            for path in list(self._records.keys()):
                if not os.path.exists(path):
                    removed.append(path)
            if removed:
                for path in removed:
                    del self._records[path]
                self._persist()
        return len(removed)


verification_store = VerificationStore()
