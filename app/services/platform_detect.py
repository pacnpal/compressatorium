"""Work out an Image's Platform from the Image itself.

One function, :func:`detect_platform`, shared by the convert panel's Auto
choice and (later) folder Libraries, the Inbox and the CLI. It never guesses:

1. **Header.** Each tool that takes the file's extension reads its own header
   through ``ToolPlugin.header_platform`` (chdman: PS1/PS2/PSP/Dreamcast/
   Saturn/Sega CD, dolphin: GameCube/Wii, xiso: Xbox/Xbox 360). The registry
   decides who is asked, so a new console is taught here by its tool.
2. **Folder name.** The nearest parent folder whose name is a Platform slug a
   tool declares, or a name RomM, ES-DE or Batocera use for one (``psx``,
   ``gc``, ``megacd``, ...).

The header wins. A folder name is only used when no header can be read. A
header that disagrees with the folder, or two headers that disagree with each
other, is a conflict and names no Platform. When nothing answers the Image is
Unsorted.

Results are cached per path and thrown away when the file's size or mtime
changes.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePath

from logging_setup import get_logger
from utils.path_utils import match_extension

logger = get_logger("platform_detect")

HEADER = "header"
FOLDER = "folder"
CONFLICT = "conflict"
UNSORTED = "unsorted"

# Platform slug (RomM's, which is IGDB's) -> other names for the same console:
# the tools' and disc-ID parser's own vocabulary, and the folder names ES-DE
# and Batocera use. A slug a tool declares that is not listed here stands for
# itself.
_ALIASES: dict[str, tuple[str, ...]] = {
    "ps": ("psx", "ps1", "playstation", "sony-playstation"),
    "ps2": ("playstation2", "playstation-2"),
    "psp": ("playstation-portable",),
    "ps3": ("playstation3", "playstation-3"),
    "ngc": ("gc", "gamecube", "nintendo-gamecube"),
    "wii": ("nintendo-wii",),
    "wiiu": ("wii-u",),
    "dc": ("dreamcast", "sega-dreamcast"),
    "saturn": ("segasaturn", "sega-saturn"),
    "segacd": ("sega-cd", "megacd", "mega-cd"),
    "xbox": ("microsoft-xbox",),
    "xbox360": ("xbox-360",),
    "switch": ("nintendo-switch",),
}
_CANONICAL: dict[str, str] = {
    alias: slug for slug, aliases in _ALIASES.items() for alias in (slug, *aliases)
}


def canonical_slug(name: str | None) -> str | None:
    """The Platform slug a name stands for, or None for an empty name."""
    if not name:
        return None
    key = name.strip().lower()
    return _CANONICAL.get(key, key) or None


@dataclass(frozen=True)
class Detection:
    """What :func:`detect_platform` worked out, and how.

    ``platform`` is set only when ``how`` is ``"header"`` or ``"folder"``.
    ``header`` and ``folder`` keep what each source said, so a conflict can be
    explained ("in the ps2 folder, header says ngc").
    """

    platform: str | None
    how: str
    header: str | None
    folder: str | None
    reason: str

    @property
    def decided(self) -> bool:
        return self.platform is not None


def _default_tools():
    from services.tools import registry
    return registry.all()


def _folder_names(tools: Iterable) -> dict[str, str]:
    names = dict(_CANONICAL)
    for tool in tools:
        for slug in tool.platform_slugs:
            names.setdefault(slug.lower(), canonical_slug(slug))
    return names


def _split_member(path: str) -> tuple[str, bool]:
    """``(path on disk, is_archive_member)`` for ``archive.zip::disc.iso``."""
    if "::" in path:
        return path.split("::", 1)[0], True
    return path, False


def _header(path: str, tools: Iterable) -> tuple[set[str], list[str]]:
    found: set[str] = set()
    sources: list[str] = []
    seen: set[int] = set()
    for tool in tools:
        if id(tool) in seen:
            continue
        seen.add(id(tool))
        if match_extension(path, tool.input_extensions) is None:
            continue
        try:
            raw = tool.header_platform(path)
        except Exception:  # a probe must never break detection
            logger.debug("header probe %s failed for %s", tool.id, path, exc_info=True)
            continue
        slug = canonical_slug(raw)
        if slug:
            found.add(slug)
            sources.append(f"{tool.id}={slug}")
    return found, sources


def _folder(path: str, names: dict[str, str]) -> tuple[str | None, str | None]:
    for part in reversed(PurePath(path).parent.parts):
        slug = names.get(part.lower())
        if slug:
            return slug, part
    return None, None


def _decide(path: str, tools: list) -> Detection:
    disk_path, member = _split_member(path)
    headers: set[str] = set()
    sources: list[str] = []
    if not member:
        headers, sources = _header(disk_path, tools)
    folder, folder_name = _folder(disk_path, _folder_names(tools))

    if len(headers) > 1:
        return Detection(
            None, CONFLICT, None, folder,
            f"headers disagree ({', '.join(sorted(sources))})",
        )
    header = next(iter(headers), None)
    if header and folder and header != folder:
        return Detection(
            None, CONFLICT, header, folder,
            f"in the {folder_name} folder, but the header says {header}",
        )
    if header:
        return Detection(header, HEADER, header, folder, f"header says {header}")
    if folder:
        return Detection(
            folder, FOLDER, None, folder,
            f"no readable header; in the {folder_name} folder",
        )
    return Detection(
        None, UNSORTED, None, None,
        "no readable header and no Platform folder",
    )


# path -> (size, mtime_ns, Detection). Bounded, oldest evicted first.
_CACHE_MAX = 8192
_cache: OrderedDict[str, tuple[int, int, Detection]] = OrderedDict()
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def detect_platform(path: str, *, tools: Iterable | None = None) -> Detection:
    """The Platform of the Image at ``path``, and how it was decided.

    ``path`` may be an archive member (``archive.zip::disc.iso``); its header
    is not read, so only the folder can name it. Blocking (a few small reads);
    call it off the event loop. ``tools`` defaults to every registered tool.
    """
    tool_list = list(tools) if tools is not None else _default_tools()
    disk_path, _member = _split_member(path)
    try:
        st = os.stat(disk_path)
    except OSError:
        return Detection(None, UNSORTED, None, None, "file could not be read")
    key = (st.st_size, st.st_mtime_ns)
    with _cache_lock:
        hit = _cache.get(path)
        if hit is not None and hit[:2] == key:
            _cache.move_to_end(path)
            return hit[2]
    result = _decide(path, tool_list)
    with _cache_lock:
        _cache[path] = (*key, result)
        _cache.move_to_end(path)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return result


# --------------------------------------------------------------------------- #
# Auto: each Image to its Platform's default target
# --------------------------------------------------------------------------- #

# The forward direction: modes that turn a source into the target format.
_TARGET_KINDS = frozenset({"create", "compress"})


@dataclass(frozen=True)
class AutoTarget:
    """Where Auto sends one Image, or why it leaves the Image alone."""

    file_path: str
    detection: Detection
    mode: str | None
    tool_id: str | None
    compression: str | None
    skip_reason: str | None


def default_mode(path: str, platform: str, tools_registry) -> str | None:
    """The default target mode for an Image of ``platform`` at ``path``.

    The first create-direction mode the registry lists for the Platform that
    takes this file. When several take it, the most specific input extension
    wins (``.nkit.iso`` over ``.iso``), registration order breaking ties: the
    same rule as ``ToolRegistry.tool_for_verify``. None when no mode serves
    the Platform for this file type.
    """
    disk_path, member = _split_member(path)
    best: str | None = None
    best_len = 0
    for spec in tools_registry.mode_specs():
        if getattr(spec.kind, "value", spec.kind) not in _TARGET_KINDS:
            continue
        claimed = {
            canonical_slug(s) for s in tools_registry.mode_platform_slugs(spec.mode)
        }
        if platform not in claimed:
            continue
        name = path.split("::", 1)[1] if member else disk_path
        matched = match_extension(name, spec.input_extensions)
        if matched is None or len(matched) <= best_len:
            continue
        if member and not spec.allows_archive_input:
            continue
        if not member and not tools_registry.for_mode(spec.mode).converts_path(disk_path):
            continue
        best, best_len = spec.mode, len(matched)
    return best


def _skip_reason(detection: Detection) -> str:
    if detection.how == CONFLICT:
        return f"Platform conflict: {detection.reason}"
    return f"Unsorted: {detection.reason}"


def plan_auto(
    paths: Iterable[str],
    *,
    compression_prefs: dict | None = None,
    tools_registry=None,
) -> list[AutoTarget]:
    """Work out each Image's Platform and default target, without queueing.

    ``compression_prefs`` is the saved per-tool compression (the convert
    panel's remembered setting, keyed by tool id). A mode that takes no
    compression gets None, and so does a tool with nothing saved, which means
    the tool's own default. Blocking; call it off the event loop.
    """
    if tools_registry is None:
        from services.tools import registry as tools_registry
    prefs = compression_prefs or {}
    tools = tools_registry.all()
    plan: list[AutoTarget] = []
    for path in paths:
        detection = detect_platform(path, tools=tools)
        if not detection.decided:
            plan.append(AutoTarget(path, detection, None, None, None,
                                   _skip_reason(detection)))
            continue
        mode = default_mode(path, detection.platform, tools_registry)
        if mode is None:
            plan.append(AutoTarget(
                path, detection, None, None, None,
                f"No {detection.platform} target takes this file type",
            ))
            continue
        spec = tools_registry.spec(mode)
        compression = None
        if spec.supports_compression or spec.supports_compression_level:
            compression = prefs.get(spec.tool_id) or None
        plan.append(AutoTarget(path, detection, mode, spec.tool_id, compression, None))
    return plan
