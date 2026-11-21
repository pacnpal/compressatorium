"""Import many DATs at once, and sync Redump's DATs in one click (#524).

* :func:`import_files` takes loose ``.dat``/``.xml`` files and zips of them:
  No-Intro's daily pack (hundreds of DATs) or a Redump per-system zip. The
  upload route and ``compressatorium dat import`` both use it.
* :func:`sync_redump` downloads ``http://redump.org/datfile/<system>/`` for the
  disc Platforms the user's Libraries hold, one request at a time, cached.

DATs are metadata only. Nothing here ever asks for a ROM, a key (``/keys/``,
``/dkeys/``) or a BIOS set (``-bios``). No-Intro is upload-only: its site is
gated behind a terms page and a form, so it is not scripted.

A zip is read under the same rules as an archive an Inbox takes in
(:meth:`ArchiveService.check_inbox_source`: entry count, declared sizes,
links, absolute and ``..`` paths, the expansion ratio) and the extract path's
member-name rules (:meth:`ArchiveService.member_segments`). The bytes actually
read are counted too (:class:`services.archive._UnpackBudget`), each DAT at
most :data:`MAX_DAT_SIZE`. A member's name never reaches the disk: each DAT is
copied to ``<n>.dat`` in a private temp folder.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import os
import tempfile
import threading
import urllib.parse
import zipfile
import zlib
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services.archive import _UnpackBudget, archive_service
from services.dat_sync import save_capped
from utils.junk import is_junk_path

logger = get_logger("dat_import")

DAT_SUFFIXES = (".dat", ".xml")
# The cap one DAT has always had (the single upload, the MAMERedump sync).
MAX_DAT_SIZE = 100 * 1024 * 1024
# An uploaded or downloaded zip of DATs. No-Intro's daily pack is tens of MB.
MAX_ZIP_SIZE = 1024 * 1024 * 1024

# A generic client name: never the owner's name, email or host.
USER_AGENT = "compressatorium-dat-sync/1.0"
# http only: redump.org refuses https connections.
REDUMP_HOST = "redump.org"
# A Platform slug (services.platform_detect) -> Redump's system slug. Redump
# catalogues discs, so a cartridge Platform has no entry.
REDUMP_SYSTEMS: dict[str, str] = {
    "psx": "psx",
    "ps2": "ps2",
    "ps3": "ps3",
    "psp": "psp",
    "ngc": "gc",
    "wii": "wii",
    "dc": "dc",
    "saturn": "ss",
    "segacd": "mcd",
    "turbografx-cd": "pce",
    "pc-fx": "pc-fx",
    "neo-geo-cd": "ngcd",
    "philips-cd-i": "cdi",
    "3do": "3do",
    "xbox": "xbox",
    "xbox360": "xbox360",
}
# Politeness: the pause between two requests, the timeout of one, and how
# long a synced system is left alone before it is asked again.
REQUEST_GAP_SECONDS = 2.0
_HTTP_TIMEOUT = 30
FRESH_FOR = timedelta(hours=24)
CACHE_KEY = "redump_sync"

# One Redump sync at a time, in this process (a threading lock, so it holds
# whichever event loop the caller runs on).
_sync_running = threading.Lock()


class SyncRunning(RuntimeError):
    """A Redump sync is already running."""


# --------------------------------------------------------------------------- #
# Zips of DATs
# --------------------------------------------------------------------------- #


def extract_dats(zip_path: str, dest_dir: str) -> list[tuple[str, str]]:
    """Copy each DAT in the zip at *zip_path* into *dest_dir*: ``(member, path)``.

    Other members (a readme) are skipped. The whole zip is refused, with
    :class:`ArchiveSafetyRefused` or ``ValueError``, when any member breaks
    the rules in the module docstring. Blocking.
    """
    out: list[tuple[str, str]] = []
    with open(zip_path, "rb") as src:
        archive_service.check_inbox_source(src, zip_path)
        _, _, ratio = archive_service.inbox_limits()
        budget = _UnpackBudget(
            max_member=MAX_DAT_SIZE, max_ratio=ratio, packed=os.fstat(src.fileno()).st_size,
        )
        src.seek(0)
        with zipfile.ZipFile(src) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            for info in infos:
                archive_service.member_segments(info.filename)
            for info in infos:
                name = info.filename
                if is_junk_path(name) or not name.lower().endswith(DAT_SUFFIXES):
                    continue
                if info.flag_bits & 0x1:
                    raise ValueError(f"{name} is encrypted")
                budget.declare(name, info.file_size, info.compress_size)
                path = os.path.join(dest_dir, f"{len(out)}.dat")
                with zf.open(info) as member, open(path, "xb") as fh:
                    while chunk := member.read(65536):
                        budget.take(name, len(chunk))
                        fh.write(chunk)
                out.append((name, path))
    return out


def _expand(files: list[tuple[str, str]], work: str) -> tuple[list[tuple[str, str]], list[dict]]:
    """``(label, path)`` files -> the DATs to import and the files refused. Blocking."""
    sources: list[tuple[str, str]] = []
    errors: list[dict] = []
    for label, path in files:
        lower = path.lower()
        if lower.endswith(".zip"):
            try:
                members = extract_dats(path, tempfile.mkdtemp(dir=work))
            except (ValueError, zipfile.BadZipFile, OSError, zlib.error, EOFError) as exc:
                # A corrupt member (bad deflate data) is this zip's refusal,
                # never the batch's failure (Codex PR #527).
                errors.append({"file": label, "error": f"can't read the zip: {exc}"})
                continue
            if not members:
                errors.append({"file": label, "error": "no .dat or .xml inside"})
            sources.extend((f"{label}/{member}", p) for member, p in members)
        elif lower.endswith(DAT_SUFFIXES):
            sources.append((label, path))
        else:
            errors.append({"file": label, "error": "not a .dat, .xml or .zip file"})
    return sources, errors


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


def _store():
    from services import dat_store

    return dat_store.dat_store


async def _snapshot() -> list[str]:
    return await run_in_threadpool(_store().list_match_paths)


async def _import_each(
    sources: list[tuple[str, str]], invalidated: set[str],
) -> tuple[list[dict], list[dict]]:
    imported: list[dict] = []
    errors: list[dict] = []
    for label, path in sources:
        try:
            result = await _store().import_dat(path, invalidated=invalidated, replace=True)
        except ValueError as exc:
            errors.append({"file": label, "error": str(exc)})
            continue
        imported.append({**result, "file": label})
    return imported, errors


async def _rematch(before: list[str], invalidated: set[str], source: str) -> None:
    # Lazy: routes.dat imports this module.
    from routes.dat import rematch_after_dat_change

    await rematch_after_dat_change([*before, *invalidated], source=source)


def _summary(imported: list[dict], errors: list[dict]) -> dict:
    message = f"Imported {len(imported)} DAT{'s' if len(imported) != 1 else ''}"
    if errors:
        message += f", {len(errors)} refused"
    return {"imported": imported, "errors": errors, "message": message}


async def import_files(files: list[tuple[str, str]], *, rematch: bool = True) -> dict:
    """Import ``(label, path)`` files: each a ``.dat``/``.xml``, or a ``.zip`` of them.

    Each DAT replaces an earlier import of itself. One bad DAT is reported in
    ``errors`` and the rest still import. The files already matched are
    re-matched once, after all of them (*rematch*; the CLI has no server to
    run that job).
    """
    before = await _snapshot() if rematch else []
    invalidated: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="dat-import-") as work:
        sources, refused = await run_in_threadpool(_expand, files, work)
        imported, errors = await _import_each(sources, invalidated)
    if imported and rematch:
        await _rematch(before, invalidated, "import_dats")
    return _summary(imported, [*refused, *errors])


# --------------------------------------------------------------------------- #
# Redump
# --------------------------------------------------------------------------- #


def redump_path(system: str) -> str:
    return f"/datfile/{urllib.parse.quote(system, safe='')}/"


def redump_url(system: str) -> str:
    return f"http://{REDUMP_HOST}{redump_path(system)}"


def present_platforms() -> set[str]:
    """The Platform slugs in the user's Libraries (RomM and folders). Blocking.

    A Library that can't be read right now is skipped: this only chooses what
    to download.
    """
    from services.libraries import all_libraries
    from services.platform_detect import canonical_slug
    from services.platform_mappings import MappingsUnavailable
    from services.rules.library import LibraryUnreadable

    found: set[str] = set()
    for library in all_libraries():
        try:
            if not library.configured():
                continue
            found.update(canonical_slug(p.slug) for p in library.platforms() if p.slug)
        except (LibraryUnreadable, MappingsUnavailable, OSError) as exc:
            logger.warning("redump sync: could not list %s: %s", library.id, exc)
    return found


def _fetch(system: str, cached: dict, dest: str) -> tuple[str | None, dict]:
    """GET one Redump DAT into *dest*: its path (None on 304) and cache headers.

    Conditional on *cached*'s ``etag``/``last_modified`` when given. Talks to
    the one fixed host and follows no redirect, so nothing but redump.org's
    ``/datfile/`` is ever asked. Blocking.
    """
    headers = {"User-Agent": USER_AGENT}
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]
    conn = http.client.HTTPConnection(REDUMP_HOST, timeout=_HTTP_TIMEOUT)
    try:
        conn.request("GET", redump_path(system), headers=headers)
        resp = conn.getresponse()
        if resp.status == 304:
            return None, {}
        if resp.status != 200:
            raise ValueError(f"redump.org answered {resp.status} {resp.reason} for {system}")
        path = os.path.join(dest, f"{system}.zip")
        save_capped(resp, path, redump_url(system), MAX_ZIP_SIZE)
        return path, {
            "etag": resp.headers.get("ETag"),
            "last_modified": resp.headers.get("Last-Modified"),
        }
    finally:
        conn.close()


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fresh(entry: dict) -> bool:
    try:
        synced = datetime.fromisoformat(entry["synced_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return _now() - synced < FRESH_FOR


async def sync_redump(
    platforms: Iterable[str] | None = None, *, force: bool = False, rematch: bool = True,
) -> dict:
    """Download and import Redump's DAT for each disc Platform present.

    *platforms* defaults to :func:`present_platforms`. A system synced in the
    last :data:`FRESH_FOR` whose DATs are still imported is not asked again
    unless *force*; when asked again the request is conditional, and an
    unchanged download is not re-imported. Raises :class:`SyncRunning`.
    """
    if not _sync_running.acquire(blocking=False):
        raise SyncRunning("A Redump sync is already running")
    try:
        return await _sync_redump(platforms, force=force, rematch=rematch)
    finally:
        _sync_running.release()


async def _sync_redump(platforms, *, force: bool, rematch: bool) -> dict:
    from services.platform_detect import canonical_slug
    from services.preferences_store import preferences_store

    if platforms is None:
        platforms = await run_in_threadpool(present_platforms)
    slugs = {canonical_slug(p) for p in platforms if p}
    systems = sorted({REDUMP_SYSTEMS[s] for s in slugs if s in REDUMP_SYSTEMS})
    cache: dict = dict(await preferences_store.get(CACHE_KEY) or {})
    names = {d["name"] for d in await run_in_threadpool(_store().list_dats)}
    before = await _snapshot() if rematch else []
    invalidated: set[str] = set()
    results: list[dict] = []
    imported_any = False
    asked = False

    with tempfile.TemporaryDirectory(prefix="redump-") as work:
        for system in systems:
            entry = dict(cache.get(system) or {})
            present = bool(entry.get("dats")) and set(entry["dats"]) <= names
            if present and not force and _fresh(entry):
                results.append({"system": system, "status": "up_to_date"})
                continue
            if asked:
                await asyncio.sleep(REQUEST_GAP_SECONDS)
            asked = True
            try:
                path, meta = await run_in_threadpool(
                    _fetch, system, entry if present else {}, work,
                )
                digest = await run_in_threadpool(_sha256, path) if path else None
                if digest is None or (present and digest == entry.get("sha256")):
                    entry["synced_at"] = _now().isoformat()
                    cache[system] = entry
                    results.append({"system": system, "status": "up_to_date"})
                    continue
                if not zipfile.is_zipfile(path):
                    # A bare DAT rather than a zip of one.
                    os.replace(path, path[:-4] + ".dat")
                    path = path[:-4] + ".dat"
                sources, refused = await run_in_threadpool(
                    _expand, [(f"{system}.zip", path)], work,
                )
                imported, errors = await _import_each(sources, invalidated)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                logger.warning("redump sync: %s failed: %s", system, exc)
                results.append({"system": system, "status": "failed", "error": str(exc)})
                continue
            imported_any = imported_any or bool(imported)
            problems = [*refused, *errors]
            if problems or not imported:
                error = "; ".join(p["error"] for p in problems) or "no DAT inside"
                results.append({"system": system, "status": "failed", "error": error})
                continue
            cache[system] = {
                **meta, "sha256": digest, "synced_at": _now().isoformat(),
                "dats": [d["name"] for d in imported],
            }
            results.append({
                "system": system, "status": "imported", "dats": cache[system]["dats"],
            })

    await preferences_store.put(CACHE_KEY, cache)
    if imported_any and rematch:
        await _rematch(before, invalidated, "redump_sync")
    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in ("imported", "up_to_date", "failed")}
    if not systems:
        message = "None of your Platforms has a Redump DAT"
    else:
        message = (f"Redump: {counts['imported']} updated, {counts['up_to_date']} up to date"
                   + (f", {counts['failed']} failed" if counts["failed"] else ""))
    return {"systems": results, **counts, "message": message}


def status_blocking(cache: dict) -> dict:
    """What the Sync Redump button shows: the systems it would fetch. Blocking."""
    from services.platform_detect import platform_name

    slugs = present_platforms()
    systems = [
        {"platform": s, "name": platform_name(s), "system": REDUMP_SYSTEMS[s],
         "synced_at": (cache.get(REDUMP_SYSTEMS[s]) or {}).get("synced_at")}
        for s in sorted(slugs) if s in REDUMP_SYSTEMS
    ]
    return {"running": _sync_running.locked(), "systems": systems}
