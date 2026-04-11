"""Persistence store for MAME Redump DAT files and hash matching."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from services.dat_parser import parse_dat

logger = logging.getLogger("chd.dat_store")


class DATStore:
    """Persists imported DAT files and hash lookup index."""

    def __init__(self, store_path: str | None = None) -> None:
        base_path = store_path or os.environ.get("CHD_DAT_STORE")
        explicit_path = bool(base_path)
        if base_path:
            self._store_path = Path(base_path)
        else:
            default_dir = Path(os.environ.get("CHD_DATA_DIR", "/config"))
            self._store_path = default_dir / "dat_store.json"
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            if explicit_path:
                raise
            fallback_root = Path(tempfile.gettempdir()) / "compressatorium"
            fallback_root.mkdir(parents=True, exist_ok=True)
            self._store_path = fallback_root / self._store_path.name

        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._dats: dict[str, dict] = {}
        self._hashes_sha1: dict[str, dict] = {}
        self._hashes_md5: dict[str, dict] = {}
        self._matches: dict[str, dict] = {}
        self._version = 0
        self._last_persisted_version = 0
        self._load()

    def _load(self):
        if self._store_path.exists():
            try:
                with self._store_path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        dats = data.get("dats", {})
                        self._dats = dats if isinstance(dats, dict) else {}
                        sha1_hashes = data.get("hashes", {}).get("sha1", {})
                        self._hashes_sha1 = sha1_hashes if isinstance(sha1_hashes, dict) else {}
                        md5_hashes = data.get("hashes", {}).get("md5", {})
                        self._hashes_md5 = md5_hashes if isinstance(md5_hashes, dict) else {}
                        matches = data.get("matches", {})
                        self._matches = matches if isinstance(matches, dict) else {}
            except json.JSONDecodeError:
                backup = self._store_path.with_suffix(".corrupt")
                try:
                    self._store_path.rename(backup)
                except OSError:
                    logger.warning(
                        "dat_store: could not rename corrupt store to %s; clearing state",
                        backup,
                    )
                self._dats = {}
                self._hashes_sha1 = {}
                self._hashes_md5 = {}
                self._matches = {}
        self._version = 0
        self._last_persisted_version = 0

    def _persist(self):
        with self._write_lock:
            with self._lock:
                if self._version <= self._last_persisted_version:
                    return
                snapshot = {
                    "dats": dict(self._dats),
                    "hashes": {
                        "sha1": dict(self._hashes_sha1),
                        "md5": dict(self._hashes_md5),
                    },
                    "matches": dict(self._matches),
                }
                version_to_write = self._version

            tmp_path = self._store_path.with_suffix(f".tmp.{os.getpid()}")
            try:
                with tmp_path.open("w", encoding="utf-8") as fh:
                    json.dump(snapshot, fh, indent=2)

                with self._lock:
                    if self._version == version_to_write:
                        tmp_path.replace(self._store_path)
                        self._last_persisted_version = version_to_write
            except Exception:
                if tmp_path.exists():
                    tmp_path.unlink()
                raise
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()

    async def import_dat(self, source: str) -> dict:
        """Parse and import a DAT file.

        ``source`` may be either a filesystem path to the DAT file (preferred,
        enables true streaming) or a raw XML string.  Returns import summary.
        """
        header, entries = await run_in_threadpool(parse_dat, source)

        dat_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        dat_info = {
            "id": dat_id,
            "name": header.get("name", "Unknown DAT"),
            "description": header.get("description", ""),
            "version": header.get("version", ""),
            "imported_at": now,
            "file_count": len(entries),
        }

        added = 0
        with self._lock:
            self._dats[dat_id] = dat_info
            for entry in entries:
                record = {
                    "dat_id": dat_id,
                    "game_name": entry["game_name"],
                    "rom_name": entry["rom_name"],
                    "size": entry["size"],
                }
                if entry.get("sha1"):
                    self._hashes_sha1[entry["sha1"]] = record
                    added += 1
                if entry.get("md5"):
                    self._hashes_md5[entry["md5"]] = record
                    added += 1
            self._matches.clear()
            self._version += 1

        await run_in_threadpool(self._persist)

        return {
            "id": dat_id,
            "name": dat_info["name"],
            "version": dat_info["version"],
            "file_count": len(entries),
            "hashes_added": added,
            "message": f"Imported {len(entries)} entries from {dat_info['name']}",
        }

    async def import_dat_no_persist(self, source: str) -> dict:
        """Parse and import a DAT file without flushing to disk.

        Identical to :meth:`import_dat` but skips the final ``_persist()``
        call.  Use this inside a bulk-import loop and call :meth:`persist`
        once afterward to achieve O(1) disk writes regardless of the number
        of DATs imported.
        """
        header, entries = await run_in_threadpool(parse_dat, source)

        dat_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        dat_info = {
            "id": dat_id,
            "name": header.get("name", "Unknown DAT"),
            "description": header.get("description", ""),
            "version": header.get("version", ""),
            "imported_at": now,
            "file_count": len(entries),
        }

        added = 0
        with self._lock:
            self._dats[dat_id] = dat_info
            for entry in entries:
                record = {
                    "dat_id": dat_id,
                    "game_name": entry["game_name"],
                    "rom_name": entry["rom_name"],
                    "size": entry["size"],
                }
                if entry.get("sha1"):
                    self._hashes_sha1[entry["sha1"]] = record
                    added += 1
                if entry.get("md5"):
                    self._hashes_md5[entry["md5"]] = record
                    added += 1
            self._matches.clear()
            self._version += 1

        return {
            "id": dat_id,
            "name": dat_info["name"],
            "version": dat_info["version"],
            "file_count": len(entries),
            "hashes_added": added,
            "message": f"Imported {len(entries)} entries from {dat_info['name']}",
        }

    async def persist(self) -> None:
        """Explicitly flush the current in-memory state to disk.

        Use after a bulk-import loop (calling :meth:`import_dat_no_persist`
        for each file) to persist all imported DATs in a single write.
        """
        await run_in_threadpool(self._persist)

    async def delete_dat(self, dat_id: str) -> bool:
        """Delete a DAT and all its hash entries."""
        with self._lock:
            if dat_id not in self._dats:
                return False
            del self._dats[dat_id]
            # Remove hash entries belonging to this DAT
            self._hashes_sha1 = {
                k: v for k, v in self._hashes_sha1.items()
                if v.get("dat_id") != dat_id
            }
            self._hashes_md5 = {
                k: v for k, v in self._hashes_md5.items()
                if v.get("dat_id") != dat_id
            }
            # Remove matches from this DAT
            self._matches = {
                k: v for k, v in self._matches.items()
                if v.get("dat_id") != dat_id
            }
            self._version += 1

        await run_in_threadpool(self._persist)
        return True

    async def delete_dats_bulk(self, dat_ids: list[str]) -> int:
        """Delete multiple DATs and their hash entries in a single disk write.

        Returns the number of DATs actually removed.  Persists once at the
        end, regardless of how many IDs are provided, keeping I/O at O(1).
        """
        if not dat_ids:
            return 0
        dat_id_set = set(dat_ids)
        removed = 0
        with self._lock:
            for dat_id in dat_ids:
                if dat_id in self._dats:
                    del self._dats[dat_id]
                    removed += 1
            if removed:
                self._hashes_sha1 = {
                    k: v for k, v in self._hashes_sha1.items()
                    if v.get("dat_id") not in dat_id_set
                }
                self._hashes_md5 = {
                    k: v for k, v in self._hashes_md5.items()
                    if v.get("dat_id") not in dat_id_set
                }
                self._matches = {
                    k: v for k, v in self._matches.items()
                    if v.get("dat_id") not in dat_id_set
                }
                self._version += 1
        if removed:
            await run_in_threadpool(self._persist)
        return removed

    def list_dats(self) -> list[dict]:
        with self._lock:
            return list(self._dats.values())

    def get_dat_name(self, dat_id: str) -> str:
        """Return the name of the DAT with the given ID, or 'Unknown'."""
        with self._lock:
            dat = self._dats.get(dat_id)
            return dat.get("name", "Unknown") if dat else "Unknown"

    def lookup_sha1(self, sha1: str) -> dict | None:
        with self._lock:
            return self._hashes_sha1.get(sha1.lower())

    def lookup_md5(self, md5: str) -> dict | None:
        with self._lock:
            return self._hashes_md5.get(md5.lower())

    def get_match(self, file_path: str) -> dict | None:
        normalized = os.path.normpath(file_path)
        with self._lock:
            return self._matches.get(normalized)

    async def set_match(self, file_path: str, match: dict):
        normalized = os.path.normpath(file_path)
        with self._lock:
            self._matches[normalized] = match
            self._version += 1
        await run_in_threadpool(self._persist)

    async def set_matches_batch(self, matches: dict[str, dict]):
        """Set multiple match results at once."""
        if not matches:
            return
        resolved = {os.path.normpath(p): m for p, m in matches.items()}
        with self._lock:
            self._matches.update(resolved)
            self._version += 1
        await run_in_threadpool(self._persist)

    def get_matches_batch(self, file_paths: list[str]) -> dict[str, dict | None]:
        result = {}
        with self._lock:
            for path in file_paths:
                normalized = os.path.normpath(path)
                result[path] = self._matches.get(normalized)
        return result

    def get_stats(self) -> dict:
        with self._lock:
            matched = sum(1 for m in self._matches.values() if m.get("matched"))
            unmatched = sum(1 for m in self._matches.values() if not m.get("matched"))
            return {
                "total_dats": len(self._dats),
                "total_sha1_hashes": len(self._hashes_sha1),
                "total_md5_hashes": len(self._hashes_md5),
                "total_matches": matched,
                "total_unmatched": unmatched,
                "total_scanned": len(self._matches),
            }

    def has_dats(self) -> bool:
        with self._lock:
            return len(self._dats) > 0

    async def prune_missing(self) -> int:
        def _check_and_prune():
            with self._lock:
                keys = list(self._matches.keys())
            missing = [p for p in keys if not os.path.exists(p)]
            if missing:
                with self._lock:
                    for path in missing:
                        self._matches.pop(path, None)
                    self._version += 1
                self._persist()
            return len(missing)

        return await run_in_threadpool(_check_and_prune)


dat_store = DATStore()
