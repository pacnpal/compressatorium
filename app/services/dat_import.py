"""Import many DATs at once, and sync Redump's DATs in one click (#524).

* :func:`import_files` takes loose ``.dat``/``.xml`` files and zips of them:
  No-Intro's daily pack (hundreds of DATs) or a Redump per-system zip. The
  upload route and ``compressatorium dat import`` both use it.
* :func:`sync_redump` downloads ``redump.org/datfile/<system>/`` for the disc
  Platforms the user's Libraries hold, one request at a time, cached. Verified
  HTTPS is tried first (certificate and host name checked). redump.org serves
  no HTTPS, so when that connection can't be made at all (refused,
  unreachable, timed out) the same path is asked over plain HTTP: the owner's
  choice (#556), superseding the HTTPS-only rule from Codex PR #527. A
  certificate that doesn't check out never falls back. Plain HTTP is allowed
  for redump.org only.

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
import ssl
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
# One batch upload's whole body, held while it arrives (upload_stream).
MAX_BATCH_SIZE = MAX_ZIP_SIZE

# A generic client name: never the owner's name, email or host.
USER_AGENT = "compressatorium-dat-sync/1.0"
# Asked over verified HTTPS first, then plain HTTP only when HTTPS can't
# connect at all (_fetch, #556). The one host anything here may ask in plaintext.
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


def too_large(cap: int) -> str:
    """The per-file error for a file over *cap* bytes."""
    return f"too large (max {cap // 2**20} MB)"


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
            try:
                too_big = os.path.getsize(path) > MAX_DAT_SIZE
            except OSError as exc:
                errors.append({"file": label, "error": f"can't read it: {exc}"})
                continue
            if too_big:
                errors.append({"file": label, "error": too_large(MAX_DAT_SIZE)})
                continue
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
    sources: list[tuple[str, str]], invalidated: set[str], transport: str = "manual",
) -> tuple[list[dict], list[dict]]:
    imported: list[dict] = []
    errors: list[dict] = []
    for label, path in sources:
        try:
            result = await _store().import_dat(
                path, invalidated=invalidated, replace=True, transport=transport,
            )
        except (ValueError, OSError) as exc:
            # An unreadable file (PermissionError) is that file's error, never
            # the batch's (CodeRabbit PR #527).
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


def redump_url(system: str, scheme: str = "https") -> str:
    """The URL asked for *system*: *scheme* is the transport actually used."""
    return f"{scheme}://{REDUMP_HOST}{redump_path(system)}"


def _connect_redump() -> tuple[http.client.HTTPConnection, str]:
    """A connection to redump.org and its scheme. Blocking.

    Verified HTTPS first. Only when that connection can't be made at all
    (refused, unreachable, timed out) is plain HTTP used (#556). A certificate
    or host name that doesn't check out fails closed: something answered on
    443, and asking it again in plaintext would hand it the download.
    """
    conn = http.client.HTTPSConnection(
        REDUMP_HOST, timeout=_HTTP_TIMEOUT, context=ssl.create_default_context(),
    )
    try:
        conn.connect()
        return conn, "https"
    except ssl.SSLError as exc:  # CertificateError is an SSLError too
        conn.close()
        raise ValueError(
            f"redump.org's HTTPS certificate didn't check out ({exc}); "
            "tried HTTPS only, not retried over plain HTTP"
        ) from None
    except OSError as exc:
        conn.close()
        https_error = exc
    logger.warning("redump sync: no HTTPS from redump.org (%s); asking over plain HTTP",
                   https_error)
    conn = http.client.HTTPConnection(REDUMP_HOST, timeout=_HTTP_TIMEOUT)
    try:
        conn.connect()
    except OSError as exc:
        conn.close()
        raise ValueError(
            f"can't reach redump.org over HTTPS ({https_error}) or plain HTTP ({exc})"
        ) from None
    return conn, "http"


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
    the one fixed host (:func:`_connect_redump`: verified HTTPS, else plain
    HTTP when HTTPS can't connect) with the same headers and caps either way,
    and follows no redirect, so nothing but redump.org's ``/datfile/`` is ever
    asked. Blocking.
    """
    headers = {"User-Agent": USER_AGENT}
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]
    conn, scheme = _connect_redump()
    transport = "HTTPS" if scheme == "https" else "plain HTTP"
    try:
        try:
            conn.request("GET", redump_path(system), headers=headers)
            resp = conn.getresponse()
        except OSError as exc:  # connected, then dropped: no fallback
            raise ValueError(f"redump.org dropped the request over {transport}: {exc}") from None
        if resp.status == 304:
            return None, {}
        if resp.status != 200:
            raise ValueError(f"redump.org answered {resp.status} {resp.reason} for {system} "
                             f"over {transport}")
        path = os.path.join(dest, f"{system}.zip")
        save_capped(resp, path, redump_url(system, scheme), MAX_ZIP_SIZE)
        return path, {
            "etag": resp.headers.get("ETag"),
            "last_modified": resp.headers.get("Last-Modified"),
            # Recorded with each DAT: one over plain HTTP never vouches for a
            # delete (services.recognized).
            "transport": scheme,
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
    # The DATs installed now, by id: a DAT of the same name imported by hand
    # replaces the synced one under a new id (Codex + CodeRabbit PR #527).
    installed = {d["id"] for d in await run_in_threadpool(_store().list_dats)}
    before = await _snapshot() if rematch else []
    invalidated: set[str] = set()
    results: list[dict] = []
    imported_any = False
    asked = False

    with tempfile.TemporaryDirectory(prefix="redump-") as work:
        for system in systems:
            entry = dict(cache.get(system) or {})
            present = bool(entry.get("dat_ids")) and set(entry["dat_ids"]) <= installed
            if present and not force and _fresh(entry):
                results.append({"system": system, "status": "up_to_date"})
                continue
            if asked:
                await asyncio.sleep(REQUEST_GAP_SECONDS)
            asked = True
            # A DAT that didn't come over HTTPS is asked for unconditionally:
            # a 304 can't show its bytes were genuine, a full answer can.
            trusted = entry.get("transport") == "https"
            try:
                path, meta = await run_in_threadpool(
                    _fetch, system, entry if present and trusted else {}, work,
                )
                digest = await run_in_threadpool(_sha256, path) if path else None
                if digest is None or (present and digest == entry.get("sha256")):
                    if digest is not None and meta.get("transport") == "https" and not trusted:
                        # The same bytes over HTTPS prove the earlier plain-HTTP
                        # download genuine; never the reverse.
                        await _store().upgrade_to_https(entry["dat_ids"])
                        entry.update({k: v for k, v in meta.items() if v is not None})
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
                # A fetch that doesn't say how it arrived counts as plain HTTP.
                imported, errors = await _import_each(
                    sources, invalidated, meta.get("transport") or "http",
                )
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
                "dat_ids": [d["id"] for d in imported],
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
    # ``changed``: some DAT went in, even for a system that failed part-way,
    # so the UI refreshes its DAT list and matches (CodeRabbit PR #527).
    return {"systems": results, **counts, "changed": imported_any, "message": message}


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
