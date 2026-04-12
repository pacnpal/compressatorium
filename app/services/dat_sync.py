"""Service for syncing MAME Redump DAT files from GitHub."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session, sessionmaker

from services import db as _db

logger = logging.getLogger("chd.dat_sync")

# GitHub API paths for DAT files within the MAMERedump repo.
_DAT_DIRS = ["MAME Redump", "MAME Redump/MAME"]

# Timeout for individual HTTP requests (seconds).
_HTTP_TIMEOUT = 30

# Maximum size (bytes) allowed for a single DAT file download.
# Matches the 100 MB cap enforced for manual DAT uploads.
_MAX_DAT_SIZE = 100 * 1024 * 1024


class DATSyncService:
    """Fetches MAME Redump DAT files from GitHub and imports them."""

    def __init__(
        self,
        state_path: str | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._syncing = False
        self._cancel = False
        self._progress: dict = {}
        self._state_path: Path | None = None
        self._repo: str | None = None
        self._token: str | None = None
        self._state: dict = {}
        self._explicit_state_path = state_path
        # Optional private sessionmaker, for tests that want an isolated DB.
        self._session_factory: sessionmaker[Session] | None = session_factory

    def _ensure_init(self) -> None:
        """Lazy init that defers settings import until first use.

        Guarded by a dedicated init lock so that concurrent callers block
        until all fields are fully populated.  The ``_initialized`` flag is
        only flipped to ``True`` after every field has been set, so a second
        caller that races past the first check will re-enter the lock and
        wait rather than proceeding with partially-initialised state.
        """
        if self._initialized:
            return
        with self._init_lock:
            # Re-check inside the lock in case another thread just finished.
            if self._initialized:
                return
            try:
                from config import settings
            except ImportError:
                from app.config import settings
            repo = settings.mameredump_repo
            token = os.environ.get("MAMEREDUMP_GITHUB_TOKEN") or None
            if self._explicit_state_path:
                state_path = Path(self._explicit_state_path)
            else:
                state_path = Path(settings.data_dir) / "dat_sync.json"
            # Assign all fields before setting the sentinel so that any
            # thread waiting on _init_lock sees a fully-initialised object.
            self._repo = repo
            self._token = token
            self._state_path = state_path
            self._state = self._load_state()
            self._initialized = True

    # ------------------------------------------------------------------
    # Persistent state (tracks last sync to avoid redundant re-imports)
    # ------------------------------------------------------------------

    def _session(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        if _db.SessionLocal is None:
            raise RuntimeError(
                "DATSyncService: db.SessionLocal not initialized — call "
                "db.init_engine() before using the service.",
            )
        return _db.SessionLocal()

    def _load_state(self) -> dict:
        """Load sync state from the DB (``dat_sync_state`` singleton row)."""
        try:
            with self._session() as session:
                row = session.get(_db.DATSyncState, 1)
                if row is None:
                    return {}
                return {
                    "last_sync_tag": row.last_sync_tag or "",
                    "last_sync_at": row.last_sync_at or "",
                    "last_sync_files": row.last_sync_files or 0,
                }
        except Exception as exc:
            logger.warning("dat_sync: failed to load state: %s", exc)
            return {}

    def _save_state(self) -> None:
        """Upsert sync state into the singleton ``dat_sync_state`` row."""
        try:
            with self._session() as session:
                row = session.get(_db.DATSyncState, 1)
                if row is None:
                    row = _db.DATSyncState(
                        id=1,
                        last_sync_tag=self._state.get("last_sync_tag") or None,
                        last_sync_at=self._state.get("last_sync_at") or None,
                        last_sync_files=int(self._state.get("last_sync_files", 0) or 0),
                    )
                    session.add(row)
                else:
                    row.last_sync_tag = self._state.get("last_sync_tag") or None
                    row.last_sync_at = self._state.get("last_sync_at") or None
                    row.last_sync_files = int(self._state.get("last_sync_files", 0) or 0)
                session.commit()
        except Exception as exc:
            logger.warning("dat_sync: failed to save state: %s", exc)

    # ------------------------------------------------------------------
    # Public status interface
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        self._ensure_init()
        with self._lock:
            return {
                "syncing": self._syncing,
                "progress": dict(self._progress),
                "last_sync_tag": self._state.get("last_sync_tag", ""),
                "last_sync_at": self._state.get("last_sync_at", ""),
                "last_sync_files": self._state.get("last_sync_files", 0),
                "error": self._progress.get("error", ""),
            }

    @property
    def is_syncing(self) -> bool:
        with self._lock:
            return self._syncing

    def cancel(self) -> bool:
        with self._lock:
            if not self._syncing:
                return False
            self._cancel = True
            return True

    # ------------------------------------------------------------------
    # GitHub API helpers
    # ------------------------------------------------------------------

    def _github_api_url(self, path: str, ref: str | None = None) -> str:
        encoded = urllib.parse.quote(path, safe="/")
        url = f"https://api.github.com/repos/{self._repo}/contents/{encoded}"
        if ref:
            url += f"?{urllib.parse.urlencode({'ref': ref})}"
        return url

    def _raw_url(self, path: str, ref: str = "main") -> str:
        encoded_path = urllib.parse.quote(path, safe="/")
        encoded_ref = urllib.parse.quote(ref, safe="")
        return f"https://raw.githubusercontent.com/{self._repo}/{encoded_ref}/{encoded_path}"

    @staticmethod
    def _require_https(url: str) -> None:
        """Raise ValueError if *url* does not use the https scheme."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise ValueError(f"Only https URLs are permitted; got scheme '{parsed.scheme}'")

    def _fetch_json(self, url: str) -> list | dict:
        self._require_https(url)
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "compressatorium-dat-sync/1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            return json.loads(resp.read().decode("utf-8"))

    def _fetch_latest_tag(self) -> str:
        """Return the most recent release tag from the repo."""
        url = f"https://api.github.com/repos/{self._repo}/releases/latest"
        try:
            data = self._fetch_json(url)
            tag = data.get("tag_name", "")
            if tag:
                return tag
        except urllib.error.HTTPError:
            pass
        # Fallback: list tags and pick the first (most recent).
        url = f"https://api.github.com/repos/{self._repo}/tags?per_page=1"
        tags = self._fetch_json(url)
        if tags and isinstance(tags, list):
            return tags[0].get("name", "main")
        return "main"

    def _list_dat_files(self, directory: str, ref: str) -> list[dict]:
        """List .dat files in a repo directory."""
        url = self._github_api_url(directory, ref=ref)
        try:
            contents = self._fetch_json(url)
        except urllib.error.HTTPError as exc:
            logger.warning("dat_sync: failed to list %s: %s", directory, exc)
            return []
        if not isinstance(contents, list):
            return []
        return [
            {"name": item["name"], "path": item["path"], "size": item.get("size", 0)}
            for item in contents
            if item.get("type") == "file" and item["name"].lower().endswith(".dat")
        ]

    def _download_dat(self, path: str, ref: str) -> str:
        """Download a DAT file to a temp file and return the temp path."""
        url = self._raw_url(path, ref=ref)
        self._require_https(url)
        # Do NOT send the GitHub token to raw.githubusercontent.com — the PAT
        # is only needed for api.github.com rate-limiting, and sending it to
        # third-party CDN hosts needlessly widens the token exposure surface.
        headers = {"User-Agent": "compressatorium-dat-sync/1.0"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            # Validate Content-Length before reading (if the header is present).
            cl = resp.headers.get("Content-Length", "").strip()
            if cl.isdigit() and int(cl) > _MAX_DAT_SIZE:
                raise ValueError(
                    f"DAT Content-Length {cl} bytes for {path} exceeds limit of {_MAX_DAT_SIZE}"
                )
            fd, tmp_path = tempfile.mkstemp(suffix=".dat")
            bytes_written = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        bytes_written += len(chunk)
                        if bytes_written > _MAX_DAT_SIZE:
                            raise ValueError(
                                f"DAT download for {path} exceeded size limit of"
                                f" {_MAX_DAT_SIZE} bytes mid-stream"
                            )
                        fh.write(chunk)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        return tmp_path

    # ------------------------------------------------------------------
    # Core sync logic
    # ------------------------------------------------------------------

    async def sync(self, tag: str | None = None) -> dict:
        """Run a full DAT sync from the MAMERedump GitHub repo.

        Returns a summary dict. Raises RuntimeError if already syncing.
        """
        self._ensure_init()
        with self._lock:
            if self._syncing:
                raise RuntimeError("Sync already in progress")
            self._syncing = True
            self._cancel = False
            self._progress = {
                "status": "starting",
                "files_total": 0,
                "files_imported": 0,
                "errors": [],
            }

        try:
            return await self._do_sync(tag)
        except Exception as exc:
            self._update_progress(status="error", error=str(exc))
            raise
        finally:
            with self._lock:
                self._syncing = False

    @staticmethod
    def _get_dat_store():
        try:
            from services.dat_store import dat_store
        except ImportError:
            from app.services.dat_store import dat_store
        return dat_store

    async def _do_sync(self, tag: str | None) -> dict:
        dat_store = self._get_dat_store()

        # Resolve tag.
        if not tag:
            self._update_progress(status="resolving version")
            tag = await run_in_threadpool(self._fetch_latest_tag)
            logger.info("dat_sync: resolved latest tag: %s", tag)

        # Check if already synced to this tag, but only fast-path if DATs are present.
        existing_dats = await run_in_threadpool(dat_store.list_dats)
        if self._state.get("last_sync_tag") == tag:
            if existing_dats:
                with self._lock:
                    self._progress["status"] = "already_synced"
                return {
                    "status": "already_synced",
                    "tag": tag,
                    "message": f"Already synced to {tag}",
                }
            logger.warning(
                "dat_sync: state reports tag %s already synced,"
                " but DAT store is empty; forcing re-sync",
                tag,
            )

        # List all DAT files before touching existing data.
        self._update_progress(status="listing files")
        all_files: list[dict] = []
        for directory in _DAT_DIRS:
            if self._is_cancelled():
                return self._cancelled_result(tag)
            files = await run_in_threadpool(self._list_dat_files, directory, tag)
            all_files.extend(files)

        with self._lock:
            self._progress["files_total"] = len(all_files)

        if not all_files:
            self._update_progress(status="error", error="No DAT files found in repository")
            return {"status": "error", "tag": tag, "message": "No DAT files found"}

        logger.info("dat_sync: found %d DAT files for tag %s", len(all_files), tag)

        # Download and import each DAT.
        # Track the IDs of newly imported DATs so they can be rolled back if
        # any errors occur, keeping the DAT store consistent (no mixed sets).
        # Deletion of existing DATs is deferred until ALL new files have been
        # successfully imported with zero errors.
        imported = 0
        errors: list[str] = []
        new_dat_ids: list[str] = []
        for i, file_info in enumerate(all_files):
            if self._is_cancelled():
                # Discard any DATs staged in this run before aborting.
                await dat_store.discard_pending()
                return self._cancelled_result(tag)

            name = file_info["name"]
            self._update_progress(
                status="importing",
                current_file=name,
                files_imported=imported,
                file_index=i,
            )

            # Reject files that exceed the per-file size cap before downloading.
            file_size = file_info.get("size", 0)
            if file_size > _MAX_DAT_SIZE:
                err_msg = f"{name}: file too large ({file_size} bytes, max {_MAX_DAT_SIZE})"
                errors.append(err_msg)
                logger.warning("dat_sync: skipping oversized file %s", err_msg)
                continue

            tmp_path = None
            try:
                tmp_path = await run_in_threadpool(
                    self._download_dat, file_info["path"], tag,
                )
                result = await dat_store.import_dat_no_persist(tmp_path)
                new_dat_ids.append(result["id"])
                imported += 1
                self._update_progress(
                    status="importing",
                    current_file=name,
                    files_imported=imported,
                    file_index=i,
                )
            except Exception as exc:
                err_msg = f"{name}: {exc}"
                errors.append(err_msg)
                logger.warning("dat_sync: failed to import %s: %s", name, exc)
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        if not errors:
            # Full success: commit all staged DATs atomically, then remove the
            # old set. New DATs are visible before old ones are removed, but
            # there is never a window where neither exists.
            old_ids = [d["id"] for d in existing_dats]
            await dat_store.persist()
            if old_ids:
                deleted = await dat_store.delete_dats_bulk(old_ids)
                if deleted:
                    logger.info(
                        "dat_sync: cleared %d existing DATs after full successful sync",
                        deleted,
                    )
        else:
            # Partial failure: discard all staged (uncommitted) new DATs so the
            # store stays in a clean, known-good state (previous working set intact).
            await dat_store.discard_pending()
            if new_dat_ids:
                logger.warning(
                    "dat_sync: discarded %d staged new DATs due to %d error(s)",
                    len(new_dat_ids),
                    len(errors),
                )
            if existing_dats:
                logger.warning(
                    "dat_sync: %d error(s) during sync; preserving %d existing DATs",
                    len(errors),
                    len(existing_dats),
                )

        # Persist sync state.  Only record last_sync_tag when the sync
        # completed with zero errors and all files were imported, so that the
        # already_synced fast-path is not triggered prematurely and the next
        # run can retry any missing/failed files.
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if not errors and imported == len(all_files):
            self._state = {
                "last_sync_tag": tag,
                "last_sync_at": now,
                "last_sync_files": imported,
            }
        else:
            # Partial sync: record progress but omit tag so retry is possible.
            self._state = {
                "last_sync_at": now,
                "last_sync_files": imported,
            }
        await run_in_threadpool(self._save_state)

        final_status = "complete_with_errors" if errors else "complete"
        error_summary = f"Sync completed with {len(errors)} error(s)" if errors else ""

        self._update_progress(
            status=final_status,
            files_imported=imported,
            errors=errors,
            error=error_summary,
        )
        logger.info("dat_sync: %s — %d imported, %d errors", final_status, imported, len(errors))

        return {
            "status": final_status,
            "tag": tag,
            "files_imported": imported,
            "files_total": len(all_files),
            "errors": errors,
            "error": error_summary,
            "message": f"Synced {imported}/{len(all_files)} DATs from MAME Redump {tag}",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_progress(self, **kwargs) -> None:
        with self._lock:
            self._progress.update(kwargs)

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._cancel

    def _cancelled_result(self, tag: str) -> dict:
        self._update_progress(status="cancelled")
        return {"status": "cancelled", "tag": tag, "message": "Sync cancelled"}


dat_sync_service = DATSyncService()
