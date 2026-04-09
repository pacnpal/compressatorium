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

logger = logging.getLogger("chd.dat_sync")

# GitHub API paths for DAT files within the MAMERedump repo.
_DAT_DIRS = ["MAME Redump", "MAME Redump/MAME"]

# Timeout for individual HTTP requests (seconds).
_HTTP_TIMEOUT = 30


class DATSyncService:
    """Fetches MAME Redump DAT files from GitHub and imports them."""

    def __init__(self, state_path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._syncing = False
        self._cancel = False
        self._progress: dict = {}
        self._state_path: Path | None = None
        self._repo: str | None = None
        self._token: str | None = None
        self._state: dict = {}
        self._explicit_state_path = state_path

    def _ensure_init(self) -> None:
        """Lazy init that defers settings import until first use."""
        if self._repo is not None:
            return
        try:
            from config import settings
        except ImportError:
            from app.config import settings
        self._repo = settings.mameredump_repo
        # Optional GitHub PAT — raises the unauthenticated rate limit from
        # 60 req/hour to 5 000 req/hour.  Set MAMEREDUMP_GITHUB_TOKEN in the
        # container environment to use it.
        self._token = os.environ.get("MAMEREDUMP_GITHUB_TOKEN") or None
        data_dir = os.environ.get("CHD_DATA_DIR", "/config")
        if self._explicit_state_path:
            self._state_path = Path(self._explicit_state_path)
        else:
            self._state_path = Path(data_dir) / "dat_sync.json"
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # Persistent state (tracks last sync to avoid redundant re-imports)
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            if self._state_path.exists():
                with self._state_path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("dat_sync: failed to load state: %s", exc)
        return {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(f".tmp.{os.getpid()}")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=2)
            tmp.replace(self._state_path)
        except OSError as exc:
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
            url += f"?ref={ref}"
        return url

    def _raw_url(self, path: str, ref: str = "main") -> str:
        encoded = urllib.parse.quote(path, safe="/")
        return f"https://raw.githubusercontent.com/{self._repo}/{ref}/{encoded}"

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
        headers = {"User-Agent": "compressatorium-dat-sync/1.0"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            fd, tmp_path = tempfile.mkstemp(suffix=".dat")
            try:
                with os.fdopen(fd, "wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
            except Exception:
                os.unlink(tmp_path)
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
            self._progress = {"status": "starting", "files_total": 0, "files_imported": 0, "errors": []}

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
        existing_dats = dat_store.list_dats()
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
                "dat_sync: state reports tag %s already synced, but DAT store is empty; forcing re-sync",
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
        # Deletion of existing DATs is deferred until ALL new files have been
        # successfully imported.  Any failure (download error, rate-limit,
        # outage, etc.) leaves the previous working DAT set fully intact.
        imported = 0
        errors: list[str] = []
        for i, file_info in enumerate(all_files):
            if self._is_cancelled():
                return self._cancelled_result(tag)

            name = file_info["name"]
            self._update_progress(
                status="importing",
                current_file=name,
                files_imported=imported,
                file_index=i,
            )

            tmp_path = None
            try:
                tmp_path = await run_in_threadpool(
                    self._download_dat, file_info["path"], tag,
                )
                await dat_store.import_dat(tmp_path)
                imported += 1
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

        # Only delete old DATs after all new ones import without error, so a
        # partial sync never wipes the previously working set.
        if not errors:
            for dat in existing_dats:
                await dat_store.delete_dat(dat["id"])
            if existing_dats:
                logger.info(
                    "dat_sync: cleared %d existing DATs after full successful sync",
                    len(existing_dats),
                )
        elif existing_dats:
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

        self._update_progress(
            status="complete",
            files_imported=imported,
            errors=errors,
        )
        logger.info("dat_sync: complete — %d imported, %d errors", imported, len(errors))

        return {
            "status": "complete",
            "tag": tag,
            "files_imported": imported,
            "files_total": len(all_files),
            "errors": errors,
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
