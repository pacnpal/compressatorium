"""RomM connects itself (#532): what this app can learn from RomM instead of
asking the user.

* :func:`discover` finds RomM on first run at the addresses RomM's own
  compose example gives it, asking only the public heartbeat.
* :func:`find_library_root` finds RomM's library among the Volumes from the
  heartbeat's ``FILESYSTEM.FS_PLATFORMS`` (RomM's platform folder names).
* :func:`refresh_layout` reads RomM's folder structure
  (``GET /api/setup/library``) and exclusions (``GET /api/config``), which
  placement (:func:`structure`) and the Rules (:func:`excluded`) use. Those
  two read a cache and never call RomM, so they never block.

Nothing here sends anything but the request itself: no token to a discovered
address, no user detail anywhere.
"""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import os
import threading
import time
from collections import deque
from typing import Any

from logging_setup import get_logger
from services.romm.client import RommClient, RommError, heartbeat_version, romm_client
from services.subprocess_runner import run_detached
from utils.junk import is_junk_entry
from utils.path_utils import is_within_configured_volumes

from config import settings

logger = get_logger("romm_connect")

# RomM's compose example (examples/docker-compose.example.yml) names the
# container ``romm`` and maps host port 80 to its 8080: the container name
# on a shared network, then that host port from inside a container (Docker
# Desktop), then this host.
DISCOVERY_URLS = ("http://romm:8080", "http://host.docker.internal", "http://localhost:8080")
DISCOVER_SECONDS = 3.0

# What the integration's features need of a token: the catalog, plus
# metadata restore, keep-the-ROM-id and Inbox scans.
RECOMMENDED_SCOPES = ("platforms.read", "roms.read", "roms.write", "tasks.run")

# How far below each Volume the library search looks, and how many folders it
# lists at most: bounded, as every probe of a Volume is.
_SEARCH_DEPTH = 3
_SEARCH_FOLDERS = 400
_LIBRARY_SECONDS = 15.0

# ponytail: RomM's layout is re-read at most every 5 minutes; a config.yml
# change shows up after that (or on the next Save or connection test).
_LAYOUT_TTL = 300.0
_LAYOUT_SECONDS = 10.0
# A read where RomM answered neither call is tried again this soon instead.
_LAYOUT_RETRY = 30.0

_layouts: dict[str, dict[str, Any]] = {}
# When each part of a RomM's layout is due to be read again (monotonic).
_due: dict[str, dict[str, float]] = {}
_lock = threading.Lock()


# ---- discovery ------------------------------------------------------------------


async def discover(
    urls: tuple[str, ...] = DISCOVERY_URLS, *, timeout: float = DISCOVER_SECONDS,
) -> dict | None:
    """The first of *urls* that answers as RomM, with its library path when
    the Volumes hold it: ``{"url", "version", "library_root"}``, else None.

    All asked at once, each bounded by *timeout* (a name that doesn't resolve
    can stall in the resolver, past any socket timeout, so the thread is
    detached and abandoned)."""
    async def probe(url: str) -> dict | None:
        client = RommClient(base_url=url, anonymous=True)
        try:
            beat = await asyncio.wait_for(
                run_detached(client.heartbeat, timeout=timeout), timeout,
            )
        except (asyncio.TimeoutError, RommError, OSError):
            return None
        version = heartbeat_version(beat)
        return {"url": url, "version": version, "heartbeat": beat} if version else None

    found = next((hit for hit in await asyncio.gather(*map(probe, urls)) if hit), None)
    if found is None:
        return None
    return {
        "url": found["url"], "version": found["version"],
        "library_root": await suggest_library_root(found["heartbeat"]),
    }


# ---- the library path -------------------------------------------------------------


def fs_platforms(heartbeat: dict) -> list[str]:
    """RomM's platform folder names from a heartbeat, or []."""
    block = heartbeat.get("FILESYSTEM") if isinstance(heartbeat, dict) else None
    names = block.get("FS_PLATFORMS") if isinstance(block, dict) else None
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


async def suggest_library_root(
    heartbeat: dict, *, before: tuple[str, ...] | None = None,
) -> str | None:
    """:func:`find_library_root` for a heartbeat, bounded; None past the bound."""
    names = fs_platforms(heartbeat)
    if not names:
        return None
    try:
        return await asyncio.wait_for(
            run_detached(functools.partial(find_library_root, names, before=before)),
            _LIBRARY_SECONDS,
        )
    except (asyncio.TimeoutError, OSError):
        logger.warning("romm: looking for RomM's library in the Volumes took too long")
        return None


def find_library_root(
    names: list[str], *, before: tuple[str, ...] | None = None,
) -> str | None:
    """The folder in the Volumes that holds RomM's platform folders *names*,
    as RomM's library root, or None. Blocking.

    A folder scores by how many of *names* it holds the way RomM's library
    does, then by how many it holds directly (a library path set to RomM's
    ``roms`` folder itself). The way is RomM's declared structure when given
    (*before*, the folders above the platform folder: ``games`` for
    ``games/{platform}/{game}``), else either of RomM's defaults
    (``roms/<name>``, or ``<name>/roms``). Highest wins, the shallowest on a
    tie."""
    wanted = {n for n in names if n and "/" not in n and n not in (".", "..")}
    if not wanted:
        return None
    best, best_score = None, (0, 0)
    for folder in _folders(settings.volumes):
        score = _score(folder, wanted) if before is None else _declared_score(
            folder, wanted, before,
        )
        if score > best_score:
            best, best_score = folder, score
    return best if best and is_within_configured_volumes(best) else None


def _folders(volumes: list[str]):
    """The Volumes and the folders below them, breadth first, bounded."""
    queue = deque((v, 0) for v in volumes)
    seen = 0
    while queue and seen < _SEARCH_FOLDERS:
        folder, depth = queue.popleft()
        seen += 1
        yield folder
        if depth >= _SEARCH_DEPTH:
            continue
        for name in sorted(_subdirs(folder)):
            if not name.startswith(".") and not is_junk_entry(name):
                queue.append((os.path.join(folder, name), depth + 1))


def _subdirs(folder: str) -> set[str]:
    try:
        with os.scandir(folder) as entries:
            return {e.name for e in entries if e.is_dir(follow_symlinks=False)}
    except OSError:
        return set()


def _declared_score(folder: str, wanted: set[str], before: tuple[str, ...]) -> tuple[int, int]:
    """:func:`_score` for RomM's declared *before*: the names under
    ``<folder>/<before>``. Under ``roms/...`` only, a folder holding them
    directly is RomM's ``roms`` folder itself, as ``RommLibrary`` maps it."""
    held = len(_subdirs(os.path.join(folder, *before)) & wanted)
    if before[:1] == ("roms",) and not held:
        return 0, len(_subdirs(folder) & wanted)
    return held, 0


def _score(folder: str, wanted: set[str]) -> tuple[int, int]:
    held = _subdirs(folder) & wanted
    nested = len(_subdirs(os.path.join(folder, "roms")) & wanted)
    nested = max(nested, sum(os.path.isdir(os.path.join(folder, n, "roms")) for n in held))
    return nested, len(held)


# ---- RomM's layout: structure and exclusions ----------------------------------------


def parse_structure(template: str) -> tuple[tuple[str, ...], tuple[str | None, ...]] | None:
    """RomM's ``filesystem.structure`` template as ``(folders before the
    platform folder, levels between it and the game)``, a level None where
    the template names a macro (a folder whose name this app can't know).
    None for a template this can't read. RomM's syntax:
    ``backend/config/config_manager.py``, ``parse_structure_template``."""
    parts = [p.strip() for p in template.strip().split("/") if p.strip()]
    if len(parts) < 2 or parts[-1] != "{game}" or "{platform}" not in parts:
        return None
    if any(p in (".", "..") for p in parts):
        return None
    at = parts.index("{platform}")
    before, levels = parts[:at], parts[at + 1:-1]
    if any(_is_macro(p) for p in before) or "{platform}" in levels or "{game}" in levels:
        return None
    return tuple(before), tuple(None if _is_macro(p) else p for p in levels)


def _is_macro(part: str) -> bool:
    return "{" in part or "}" in part


def _lower_exts(values: Any) -> tuple[str, ...]:
    return tuple(
        str(v).lower().lstrip(".") for v in values or () if isinstance(v, str) and v.strip()
    )


def _names(values: Any) -> tuple[str, ...]:
    return tuple(v for v in values or () if isinstance(v, str) and v.strip())


def _read_exclusions(client: RommClient, layout: dict[str, Any]) -> bool:
    """RomM's ``exclude:`` settings into *layout*; whether RomM answered.
    Blocking."""
    try:
        config = client.config(timeout=_LAYOUT_SECONDS)
    except RommError as exc:
        logger.info("romm: couldn't read RomM's exclusions: %s", exc)
        return False
    layout["excluded_exts"] = (
        _lower_exts(config.get("EXCLUDED_SINGLE_EXT"))
        + _lower_exts(config.get("DEFAULT_EXCLUDED_EXTENSIONS"))
    )
    layout["excluded_names"] = (
        _names(config.get("EXCLUDED_SINGLE_FILES"))
        + _names(config.get("EXCLUDED_MULTI_FILES"))
        + _names(config.get("DEFAULT_EXCLUDED_FILES"))
    )
    return True


def _read_structure(client: RommClient, layout: dict[str, Any]) -> bool:
    """RomM's folder structure into *layout*; whether RomM answered (a 404
    counts: a RomM before 5.3 has no structure setting). Blocking."""
    try:
        template = client.library_structure(timeout=_LAYOUT_SECONDS)
    except RommError as exc:
        logger.info("romm: couldn't read RomM's folder structure: %s", exc)
        if exc.status != 404:
            return False
        template = None
    layout["structure_template"] = template
    layout["structure"] = parse_structure(template) if template else None
    return True


# Each part of the layout, its reader, and the key saying it was read once.
_PARTS = {
    "exclusions": (_read_exclusions, "exclusions_read"),
    "structure": (_read_structure, "structure_read"),
}


def refresh_layout_now(client: RommClient | None = None, *, force: bool = False) -> dict:
    """Read each part of RomM's layout that is due: read in the last few
    minutes, it isn't; a part RomM didn't answer is due again after
    `_LAYOUT_RETRY`, without re-reading the other. Blocking."""
    client = client or romm_client
    base = client.base_url
    if not base:
        return {}
    now = time.monotonic()
    with _lock:
        cached = _layouts.get(base, {})
        due = dict(_due.get(base, {}))
    stale = [p for p in _PARTS if force or due.get(p, 0.0) <= now]
    if not stale:
        return cached
    layout = dict(cached)
    for part in stale:
        read, flag = _PARTS[part]
        answered = read(client, layout)
        if answered:
            layout[flag] = True
        due[part] = time.monotonic() + (_LAYOUT_TTL if answered else _LAYOUT_RETRY)
    with _lock:
        _layouts[base] = layout
        _due[base] = due
    return layout


async def refresh_layout(client: RommClient | None = None, *, force: bool = False) -> None:
    """:func:`refresh_layout_now`, off the event loop and bounded."""
    try:
        await asyncio.wait_for(
            run_detached(refresh_layout_now, client, force=force), 2 * _LAYOUT_SECONDS + 5,
        )
    except (asyncio.TimeoutError, OSError):
        logger.info("romm: reading RomM's layout took too long")


def layout(base: str | None = None) -> dict[str, Any]:
    """The layout of the RomM at *base* (the configured one by default) as
    last read, {} before then. In memory."""
    with _lock:
        return _layouts.get(base or romm_client.base_url, {})


def exclusions_known() -> bool:
    """Whether RomM's ``exclude:`` settings were ever read for the configured
    RomM: until they are, "nothing excluded" is not known. In memory."""
    return bool(layout().get("exclusions_read"))


def structure_known(base: str | None = None) -> bool:
    """Whether RomM's folder structure was ever read (or RomM said it has
    none) for the RomM at *base*, the configured one by default: until it
    is, "none declared" is not known. In memory."""
    return bool(layout(base).get("structure_read"))


def structure(base: str | None = None) -> tuple[tuple[str, ...], tuple[str | None, ...]] | None:
    """RomM's declared folder structure (:func:`parse_structure`) at *base*,
    the configured RomM by default, or None when it isn't known. In memory."""
    return layout(base).get("structure")


def excluded_extension(ext: str) -> bool:
    """Whether RomM's ``exclude:`` settings ignore every file ending in
    ``.<ext>``, whatever its name. In memory."""
    wanted = ext.lower().lstrip(".")
    return bool(wanted) and wanted in layout().get("excluded_exts", ())


def excluded(name: str) -> bool:
    """Whether RomM's ``exclude:`` settings make it ignore a file or game
    folder named *name*, as RomM matches them (``exclude_single_files`` in
    ``backend/handler/filesystem/base_handler.py``). In memory."""
    current = layout()
    lower = name.lower()
    if any(lower.endswith(f".{ext}") for ext in current.get("excluded_exts", ())):
        return True
    return any(
        name == pattern or fnmatch.fnmatchcase(name, pattern)
        for pattern in current.get("excluded_names", ())
    )


def reset_for_tests() -> None:
    with _lock:
        _layouts.clear()
        _due.clear()
