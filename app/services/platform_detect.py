"""Work out an Image's Platform from the Image itself.

One function, :func:`detect_platform`, shared by the convert panel's Auto
choice and folder Libraries, and (later) the Inbox and the CLI. It never guesses:

1. **Header.** Each tool that takes the file's extension reads its own header
   through ``ToolPlugin.header_platform`` (chdman: PS1/PS2/PSP/Dreamcast/
   Saturn/Sega CD, dolphin: GameCube/Wii, xiso: Xbox/Xbox 360). The registry
   decides who is asked, so a new console is taught here by its tool.
2. **The nearest folder that says.** Walking up from the Image's folder, the
   first folder that either has the user's mapping ("this folder is PSP",
   :mod:`services.platform_mappings`, #304) or a name that is a Platform slug
   a tool declares, or a name RomM, ES-DE or Batocera use for one (``psx``,
   ``gc``, ``megacd``, ...). At the same folder the mapping beats the name. A
   mapping on ``roms/`` therefore names the Images in ``roms/misc/`` but not
   those in ``roms/ps2/``.

The header wins. The folder is only used when no header can be read. A header
that disagrees with the deciding folder, or two headers that disagree with
each other, is a conflict and names no Platform. When nothing answers the
Image is Unsorted.

Results are cached per path and thrown away when the file's size or mtime
changes, or the folder evidence that decides it does.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePath

from logging_setup import get_logger
from models import InputKind
from utils.path_utils import containing_volume_root, is_special_file, match_extension

logger = get_logger("platform_detect")

HEADER = "header"
FOLDER = "folder"
MAPPING = "mapping"
CONFLICT = "conflict"
UNSORTED = "unsorted"

# Platform slug -> every other name for the same console: IGDB's slug where
# RomM's differs, the tools' and the disc-ID parser's own vocabulary, and the
# folder names ES-DE (es_systems.xml) and Batocera use. Each key is RomM's own
# slug (its ``UniversalPlatformSlug`` value, e.g. ``psx`` and ``genesis`` where
# IGDB says ``ps`` and ``genesis-slash-megadrive``), and Detection always
# answers with the key. Every slug a tool declares must appear here, as a key or an alias;
# tests/test_platform_detect.py fails when a new tool brings one that is not.
_ALIASES: dict[str, tuple[str, ...]] = {
    # Sony
    "psx": ("ps", "ps1", "playstation", "sony-playstation"),
    "ps2": ("playstation2", "playstation-2"),
    "psp": ("playstation-portable",),
    "ps3": ("playstation3", "playstation-3"),
    # Nintendo
    "ngc": ("gc", "gamecube", "nintendo-gamecube"),
    "wii": ("nintendo-wii",),
    "wiiu": ("wii-u",),
    "switch": ("nintendo-switch",),
    "3ds": ("n3ds", "nintendo-3ds"),
    "new-nintendo-3ds": ("new3ds",),
    "nds": ("nintendo-ds",),
    "gb": ("gameboy", "game-boy"),
    "gbc": ("gameboy-color", "game-boy-color"),
    "gba": ("gameboy-advance", "game-boy-advance"),
    "nes": ("famicom",),
    "snes": ("sfc", "snesna", "superfamicom", "super-famicom"),
    "n64": ("nintendo-64",),
    "virtualboy": ("vb", "virtual-boy"),
    # Sega
    "dc": ("dreamcast", "sega-dreamcast"),
    "saturn": ("segasaturn", "sega-saturn", "saturnjp"),
    "segacd": ("sega-cd", "megacd", "mega-cd", "megacdjp"),
    "genesis": (
        "genesis-slash-megadrive", "megadrive", "mega-drive", "megadrivejp",
        "segamd", "md",
    ),
    "sms": ("mastersystem", "master-system", "sega-master-system"),
    "gamegear": ("gg", "game-gear"),
    # NEC, SNK, Bandai, Atari, Commodore, Philips, Panasonic, Pioneer
    "turbografx-cd": ("tg-cd", "tgcd", "pcenginecd", "pce-cd", "pcecd"),
    "pc-fx": ("pcfx",),
    "neo-geo-cd": ("neogeocd", "neogeocdjp"),
    "neo-geo-pocket": ("ngp",),
    "neo-geo-pocket-color": ("ngpc",),
    "wonderswan": ("wswan",),
    "wonderswan-color": ("wonderswancolor", "wswanc"),
    "lynx": ("atarilynx", "atari-lynx"),
    "atari2600": ("atari-2600",),
    "atari7800": ("atari-7800",),
    "c64": ("commodore-64",),
    "philips-cd-i": ("cdi", "cdimono1", "cd-i"),
    "3do": ("3do-interactive-multiplayer",),
    "laseractive": (),
    "arcade": ("mame", "fbneo", "naomi", "naomi2", "naomigd", "atomiswave"),
    # Microsoft
    "xbox": ("microsoft-xbox",),
    "xbox360": ("xbox-360",),
}
# What each Platform is called on screen.
_NAMES: dict[str, str] = {
    "psx": "PlayStation",
    "ps2": "PlayStation 2",
    "psp": "PlayStation Portable",
    "ps3": "PlayStation 3",
    "ngc": "GameCube",
    "wii": "Wii",
    "wiiu": "Wii U",
    "switch": "Nintendo Switch",
    "3ds": "Nintendo 3DS",
    "new-nintendo-3ds": "New Nintendo 3DS",
    "nds": "Nintendo DS",
    "gb": "Game Boy",
    "gbc": "Game Boy Color",
    "gba": "Game Boy Advance",
    "nes": "NES",
    "snes": "SNES",
    "n64": "Nintendo 64",
    "virtualboy": "Virtual Boy",
    "dc": "Dreamcast",
    "saturn": "Saturn",
    "segacd": "Sega CD",
    "genesis": "Genesis / Mega Drive",
    "sms": "Master System",
    "gamegear": "Game Gear",
    "turbografx-cd": "TurboGrafx-CD",
    "pc-fx": "PC-FX",
    "neo-geo-cd": "Neo Geo CD",
    "neo-geo-pocket": "Neo Geo Pocket",
    "neo-geo-pocket-color": "Neo Geo Pocket Color",
    "wonderswan": "WonderSwan",
    "wonderswan-color": "WonderSwan Color",
    "lynx": "Lynx",
    "atari2600": "Atari 2600",
    "atari7800": "Atari 7800",
    "c64": "Commodore 64",
    "philips-cd-i": "CD-i",
    "3do": "3DO",
    "laseractive": "LaserActive",
    "arcade": "Arcade",
    "xbox": "Xbox",
    "xbox360": "Xbox 360",
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


def platform_name(slug: str | None) -> str | None:
    """A Platform's display name, or the slug itself when the table has none."""
    if not slug:
        return None
    return _NAMES.get(slug, slug)


def platforms() -> list[dict[str, str]]:
    """Every Platform with a display name, sorted by name (the mapping picker)."""
    return sorted(
        ({"slug": slug, "name": name} for slug, name in _NAMES.items()),
        key=lambda p: p["name"].lower(),
    )


def known_slug(name: str | None) -> str | None:
    """The Platform slug *name* stands for, or None when no Platform has it.

    A slug is known when it is in the alias table or a registered tool declares
    it. Used to check a Platform id a client submits (a folder Library's Rule
    keys) rather than storing whatever string arrives.
    """
    slug = canonical_slug(name)
    if slug is None:
        return None
    if slug in _ALIASES:
        return slug
    return slug if slug in set(_folder_names(_default_tools()).values()) else None


@dataclass(frozen=True)
class Detection:
    """What :func:`detect_platform` worked out, and how.

    ``platform`` is set only when ``how`` is ``"header"``, ``"mapping"`` or
    ``"folder"``. ``header``, ``folder`` and ``mapped`` keep what each source
    said, so a conflict can be explained ("in the ps2 folder, header says ngc").
    """

    platform: str | None
    how: str
    header: str | None
    folder: str | None
    reason: str
    # The Platform the user's mapping gave the deciding folder, when a
    # mapping is the folder evidence that decides.
    mapped: str | None = None
    # Every header's answer, when two or more disagree.
    headers: tuple[str, ...] = ()
    # The folder evidence that decided (a mapping or a Platform name): the
    # folder's path and the Platform it named. ``folder`` above is only the
    # nearest Platform name, which a nearer mapping may have overruled.
    place: str | None = None
    place_platform: str | None = None

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


def _probes(tool, path: str, is_dir: bool) -> bool:
    """Whether ``tool`` is asked for ``path``'s header.

    A file: when the tool takes its extension. A folder: when the tool takes
    that folder as its input unit (``accepts_directory``, e.g. a PS3 disc
    folder), the directory analogue the rest of the registry already uses.
    """
    if is_dir:
        accepts = getattr(tool, "accepts_directory", None)
        return bool(accepts and accepts(path))
    return match_extension(path, tool.input_extensions) is not None


def _header(path: str, tools: Iterable) -> tuple[set[str], list[str]]:
    found: set[str] = set()
    sources: list[str] = []
    seen: set[int] = set()
    is_dir = os.path.isdir(path)
    for tool in tools:
        if id(tool) in seen:
            continue
        seen.add(id(tool))
        if not _probes(tool, path, is_dir):
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


def _candidate_paths(path: str) -> list[PurePath]:
    """The folders the walk may read for ``path``, nearest first.

    From the Image's own folder up to the root of the Volume that holds it,
    root included, never above: a Volume mounted under ``/mnt/md/...`` must not
    make every Image in it a Genesis Image. Resolved, so a mapping (stored
    under a Library's resolved root) matches an Image reached through a
    symlink too. A path in no configured Volume uses only its own folder.
    """
    parent = PurePath(path).parent
    own = [parent] if parent.name else []
    root = containing_volume_root(path)
    if root is None:
        return own
    resolved = Path(os.path.realpath(path)).parent
    try:
        below = resolved.relative_to(root).parts
    except ValueError:
        return own
    out = [resolved]
    for _part in below:
        out.append(out[-1].parent)
    return out


def _candidate_folders(path: str) -> list[str]:
    """Folder names the walk may read for ``path``, nearest first."""
    return [p.name for p in _candidate_paths(path)]


def _folder_context(path: str) -> tuple[str, str | None]:
    """What ``_candidate_folders`` depends on beyond the path: the resolved
    parent folder and the containing Volume root."""
    root = containing_volume_root(path)
    parent = str(Path(os.path.realpath(path)).parent)
    return parent, None if root is None else str(root)


def _folder(path: str, names: dict[str, str]) -> tuple[str | None, str | None]:
    for part in _candidate_folders(path):
        slug = names.get(part.lower())
        if slug:
            return slug, part
    return None, None


def _mapped_folders() -> dict[str, str]:
    """The user's mappings: absolute folder path -> Platform slug."""
    from services import platform_mappings

    return platform_mappings.mapped_folders()


def _place(
    disk_path: str, names: dict[str, str], mapped: dict[str, str],
) -> tuple[str, str, str, str] | None:
    """The nearest folder above the Image that names a Platform.

    ``(how, slug, folder path, folder name)``: ``how`` is ``"mapping"`` or
    ``"folder"``. Walking up from the Image's folder, the first folder with a
    mapping or a Platform name decides; at the same folder the mapping wins.
    """
    for parent in _candidate_paths(disk_path):
        label = parent.name or str(parent)
        slug = mapped.get(str(parent)) if mapped else None
        if slug:
            return MAPPING, slug, str(parent), label
        slug = names.get(parent.name.lower()) if parent.name else None
        if slug:
            return FOLDER, slug, str(parent), label
    return None


def _decide(path: str, tools: list) -> Detection:
    disk_path, member = _split_member(path)
    headers: set[str] = set()
    sources: list[str] = []
    if not member:
        headers, sources = _header(disk_path, tools)
    names = _folder_names(tools)
    folder, _folder_name = _folder(disk_path, names)
    found = _place(disk_path, names, _mapped_folders())
    place = how = where = mapped = place_path = None
    if found:
        how, place, place_path, label = found
        if how == MAPPING:
            mapped = place
            where = f"you set the {label} folder to {place}"
        else:
            where = f"in the {label} folder"

    decided_by = {"place": place_path, "place_platform": place}
    if len(headers) > 1:
        return Detection(
            None, CONFLICT, None, folder,
            f"headers disagree ({', '.join(sorted(sources))})", mapped,
            tuple(sorted(headers)), **decided_by,
        )
    header = next(iter(headers), None)
    if header and place and header != place:
        return Detection(
            None, CONFLICT, header, folder,
            f"{where}, but the header says {header}", mapped, **decided_by,
        )
    if header:
        return Detection(
            header, HEADER, header, folder, f"header says {header}", mapped, **decided_by,
        )
    if place:
        return Detection(
            place, how, None, folder, f"no readable header; {where}", mapped, **decided_by,
        )
    return Detection(
        None, UNSORTED, None, None,
        "no readable header and no Platform folder",
    )


# path -> ((size, mtime_ns, header dependencies), Detection). Bounded, oldest
# evicted first.
_CACHE_MAX = 8192
_cache: OrderedDict[str, tuple[tuple, Detection]] = OrderedDict()
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def detect_uncached(path: str, *, tools: Iterable | None = None) -> Detection:
    """:func:`detect_platform` without the shared cache, for a caller that keeps its own.

    A folder Library keys its own cache on (path, size, mtime) for every file
    it holds, which the bounded LRU below cannot do for a large Library.
    """
    return _decide(path, list(tools) if tools is not None else _default_tools())


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
    if is_special_file(disk_path):
        # A FIFO, socket or device: opening one can block forever (a FIFO with
        # no writer), stranding the threadpool worker. Checked before any
        # header probe or open, here at the one entry point every caller uses.
        return Detection(None, UNSORTED, None, None, "not a regular file or folder")
    if not _member and os.path.isdir(disk_path):
        # A folder Image (a PS3 disc folder) is judged by files below it, which
        # its own size and mtime do not track. Probe it fresh every time.
        return _decide(path, tool_list)
    key = detection_key(path, st, tools=tool_list)
    if key is None:
        # A file the header probe needs is missing or unreadable (a .bin still
        # copying in). Answer now, but remember nothing: the next call re-reads.
        return _decide(path, tool_list)
    # `detection_key` already carries the probe set. The shared cache adds the
    # folder walk's context, which the file's own stat does not track: the
    # resolved parent folder (a retargeted folder symlink) and the Volume root
    # the walk stops at. A folder Library's cache keys on `detection_key`
    # alone: it walks a resolved root without following symlinks, so a path's
    # folder context cannot change under it, and resolving it per Image per
    # walk would cost a Volume resolution for every file.
    key = (*key, _folder_context(path))
    with _cache_lock:
        hit = _cache.get(path)
        if hit is not None and hit[0] == key:
            _cache.move_to_end(path)
            return hit[1]
    result = _decide(path, tool_list)
    with _cache_lock:
        _cache[path] = (key, result)
        _cache.move_to_end(path)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return result


def detection_key(
    path: str, st: os.stat_result, *, tools: Iterable | None = None,
) -> tuple | None:
    """What a cached Detection for *path* is valid for, or None when it must not be cached.

    The file's size and mtime (*st*, the caller's stat of it), those of
    every other file its header probe reads (``ToolPlugin.header_reads``: a
    sheet's tracks), the probe set itself (the same file asked of other tools
    can answer differently; the tool objects are compared by identity and kept
    alive by the key, so an id is never reused), and the folder evidence that
    decides it (the user's mapping or a Platform folder name, by source and
    path). None when one of the files cannot be stat'ed. The shared cache here
    and a folder Library's own cache both key on it, so a changed track, probe
    set or mapping re-detects the Image in either.
    """
    tool_list = list(tools) if tools is not None else _default_tools()
    disk_path, member = _split_member(path)
    deps = () if member else _header_dependencies(disk_path, tool_list)
    if deps is None:
        return None
    # The deciding folder evidence by path and source: where a mapping sits
    # relative to a Platform-named folder changes the answer, so a name alone
    # would not do. With no mapping anywhere the folder evidence is the path's
    # own, already what the key is for: skip the walk (a resolve) on every
    # stat-only rewalk.
    mapped = _mapped_folders()
    found = _place(disk_path, _folder_names(tool_list), mapped) if mapped else None
    return (st.st_size, st.st_mtime_ns, deps, tuple(tool_list), found[:3] if found else None)


def _header_dependencies(path: str, tools: Iterable) -> tuple | None:
    """``(path, size, mtime_ns)`` of every other file a header probe reads.

    Asked of the tools that would probe ``path`` (``ToolPlugin.header_reads``,
    a track sheet's tracks). None when one of them cannot be stat'ed.
    """
    seen: set[str] = set()
    deps = []
    for tool in tools:
        if match_extension(path, tool.input_extensions) is None:
            continue
        reads = getattr(tool, "header_reads", None)
        try:
            listed = reads(path) if reads else []
        except Exception:  # like a probe, a dependency list must never break detection
            logger.debug("header_reads %s failed for %s", tool.id, path, exc_info=True)
            listed = None
        if listed is None:
            # The tool cannot tell what its probe reads (an unreadable sheet):
            # answer, but cache nothing.
            return None
        for dep in listed:
            if dep in seen:
                continue
            seen.add(dep)
            try:
                st = os.stat(dep)
            except OSError:
                return None
            deps.append((dep, st.st_size, st.st_mtime_ns))
    return tuple(sorted(deps))


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


def _auto_candidates(platform: str, tools_registry) -> list:
    """Create-direction modes that may be ``platform``'s Auto target."""
    found = []
    for spec in tools_registry.mode_specs():
        if not spec.auto_target:
            continue
        if getattr(spec.kind, "value", spec.kind) not in _TARGET_KINDS:
            continue
        claimed = {
            canonical_slug(s)
            for s in (*tools_registry.mode_platform_slugs(spec.mode), *spec.auto_platform_slugs)
        }
        if platform in claimed:
            found.append(spec)
    return found


def _extension_match(spec, path: str, reads) -> str | None:
    """The extension of ``path`` that ``spec`` reads, by name only.

    A directory mode (makeps3iso's folder->iso) reads folders, not suffixes:
    it answers ``""`` for a directory on disk and None for anything else.
    """
    disk_path, member = _split_member(path)
    if InputKind.DIRECTORY in spec.input_kinds:
        return "" if not member and os.path.isdir(disk_path) else None
    name = path.split("::", 1)[1] if member else disk_path
    return match_extension(name, reads)


def _can_read(spec, path: str, tools_registry) -> bool:
    """Whether ``spec``'s command can read ``path``, past its extension.

    An archive member needs a mode that takes archive members; a file on disk
    asks the tool's ``converts_path`` and a folder its ``accepts_directory``.
    May touch the disk (a header or a sibling), so it runs last.

    ``converts_path`` refines the tool's own ``input_extensions``, so it is
    asked only about a file those cover: chdman's extract and copy modes read
    ``.chd``, which chdman leaves out of its tool-wide inputs on purpose.
    """
    disk_path, member = _split_member(path)
    if member:
        return bool(spec.allows_archive_input)
    tool = tools_registry.for_mode(spec.mode)
    if InputKind.DIRECTORY in spec.input_kinds:
        return bool(tool.accepts_directory(disk_path))
    if match_extension(disk_path, tool.input_extensions) is None:
        return True
    return bool(tool.converts_path(disk_path))


def _best_reader(path: str, specs, tools_registry, reads_of) -> tuple:
    """``(spec, matched extension)`` of the most specific reader of ``path``.

    The most specific extension match wins (``.nkit.iso`` over ``.iso``), then
    the smallest read set, then the order of ``specs`` (registration order).
    ``(None, None)`` when no spec can read the file.
    """
    best, best_matched = None, None
    best_key: tuple[int, int] | None = None
    for spec in specs:
        reads = reads_of(spec)
        matched = _extension_match(spec, path, reads)
        if matched is None:
            continue
        key = (-len(matched), len(reads))
        if best_key is not None and key >= best_key:
            continue
        if not _can_read(spec, path, tools_registry):
            continue
        best, best_matched, best_key = spec, matched, key
    return best, best_matched


def default_mode(path: str, platform: str, tools_registry) -> str | None:
    """The default target mode for an Image of ``platform`` at ``path``.

    Candidates are the create-direction modes that claim the Platform
    (``platform_slugs`` or ``auto_platform_slugs``), may be an Auto default
    (``ModeSpec.auto_target``) and whose command can read this file
    (``ToolRegistry.mode_reads_extensions`` and the tool's ``converts_path``).
    Among them the most specific extension match wins (``.nkit.iso`` over
    ``.iso``), then the smallest read set (the most specific reader: a PS2
    ``.iso`` goes to createdvd, which reads only ``.iso``, not createcd), then
    registration order. None when no candidate reads the file.
    """
    best, _ = _best_reader(
        path, _auto_candidates(platform, tools_registry), tools_registry,
        lambda spec: tools_registry.mode_reads_extensions(spec.mode),
    )
    return best.mode if best else None


# --------------------------------------------------------------------------- #
# A named format: `compressatorium convert --to <format>` (#306)
# --------------------------------------------------------------------------- #


def _parse_target(target: str, tools_registry) -> tuple[str | None, str | None]:
    """``(format, None)`` for a format such as ``chd``, ``(None, mode)`` for a mode id."""
    key = target.strip().lower()
    if any(spec.mode == key for spec in tools_registry.mode_specs()):
        return None, key
    return (key if key.startswith(".") else f".{key}"), None


def target_formats(tools_registry) -> list[str]:
    """Every format some Auto-eligible mode writes (``.chd``, ``.rvz``, ...).

    Read from each mode's ``output_path`` over its own inputs, so a tool whose
    output extension follows its input (z3ds, nsz) is covered without a list.
    A compound output (romz's ``Game.gba.7z``) also lists its last extension,
    so ``7z`` and ``zip`` name the archive whatever the ROM inside; a name
    that is not an extension (chdman copy's ``Game_copy.chd``) is left out.
    """
    found: set[str] = set()
    for spec in tools_registry.mode_specs():
        if not spec.auto_target:
            continue
        tool = tools_registry.for_mode(spec.mode)
        for ext in tools_registry.mode_reads_extensions(spec.mode) or {""}:
            probe = f"/compressatorium-format-probe/Game{ext}"
            try:
                out = tool.output_path(spec.mode, probe)
            except (KeyError, ValueError, OSError):
                continue
            stem = "/compressatorium-format-probe/Game"
            suffix = out[len(stem):].lower() if out.startswith(stem) else ""
            if suffix.startswith(".") and len(suffix) > 1:
                found.add(suffix)
                found.add(os.path.splitext(suffix)[1])
    return sorted(found)


def _writes(spec, path: str, fmt: str, tools_registry) -> bool:
    out = _target_output(path, spec.mode, None, tools_registry)
    return out is not None and match_extension(out, {fmt}) is not None


def target_reads_extensions(target: str, tools_registry) -> frozenset[str]:
    """Every extension some mode for ``target`` can read (#306).

    A mode id: what that mode's command reads (``mode_reads_extensions``). A
    format: the union over the Auto-eligible modes ``format_candidates``
    considers. A folder walk for ``--to`` lists the files with these, so a
    mode reading what its tool leaves out of its inputs (chdman's extract
    modes and ``.chd``) still finds them; ``format_candidates`` then decides
    per file.
    """
    _fmt, mode = _parse_target(target, tools_registry)
    if mode is not None:
        return frozenset(tools_registry.mode_reads_extensions(mode))
    found: set[str] = set()
    for spec in tools_registry.mode_specs():
        if spec.auto_target:
            found |= tools_registry.mode_reads_extensions(spec.mode)
    return frozenset(found)


def format_candidates(path: str, target: str, tools_registry) -> list:
    """The modes that could turn ``path`` into ``target``, Platform aside.

    ``target`` is a format (``chd``, ``.rvz``) or a mode id (``createcd``). A
    format matches every Auto-eligible mode whose command reads the Image and
    whose output has that extension; a mode id matches itself when its
    command reads the Image (``mode_reads_extensions``: createdvd takes a
    ``.cue`` by hand but cannot read one). Empty means the Image is not in
    scope for ``target``.
    """
    fmt, mode = _parse_target(target, tools_registry)
    if mode is not None:
        spec = tools_registry.spec(mode)
        reads = tools_registry.mode_reads_extensions(mode)
        if _extension_match(spec, path, reads) is None:
            return []
        return [spec] if _can_read(spec, path, tools_registry) else []
    found = []
    for spec in tools_registry.mode_specs():
        if not spec.auto_target:
            continue
        reads = tools_registry.mode_reads_extensions(spec.mode)
        if _extension_match(spec, path, reads) is None:
            continue
        if not _writes(spec, path, fmt, tools_registry):
            continue
        if _can_read(spec, path, tools_registry):
            found.append(spec)
    return found


def _serves(spec, platform: str, tools_registry) -> bool:
    """Whether ``spec`` may convert an Image of ``platform``.

    The Auto test (``platform_slugs`` or ``auto_platform_slugs``), kept
    conservative like ``mode_allows_platform``: a mode that names no Platform,
    or a Platform no mode names, rules nothing out.
    """
    def claimed(s) -> set:
        return {
            canonical_slug(slug)
            for slug in (*tools_registry.mode_platform_slugs(s.mode), *s.auto_platform_slugs)
        }

    own = claimed(spec)
    if not own or platform in own:
        return True
    return not any(platform in claimed(s) for s in tools_registry.mode_specs())


serves_platform = _serves


def format_route(
    path: str, target: str, platform: str | None, tools_registry,
) -> tuple[str | None, str | None]:
    """``(mode, None)`` for how ``path`` becomes ``target``, or ``(None, reason)``.

    Auto's choice restricted to the modes that write the format: the
    Image's Platform (when one was worked out) narrows the candidates, then
    ``_best_reader`` ranks them exactly as ``default_mode`` does. An Image whose
    most specific reader already reads the format is left alone ("Already
    CHD"), the ``auto_route`` rule. A mode id is taken as given once it reads
    the Image and serves its Platform.
    """
    fmt, mode = _parse_target(target, tools_registry)
    candidates = format_candidates(path, target, tools_registry)
    if not candidates:
        name = path.split("::", 1)[-1]
        ext = os.path.splitext(name)[1].lower() or name
        if mode is not None:
            return None, f"{mode} does not read {ext} files"
        return None, f"No {fmt} target reads {ext} files"
    if platform:
        served = [s for s in candidates if _serves(s, platform, tools_registry)]
        if not served:
            if mode is not None:
                return None, f"{mode} does not convert {platform} Images"
            return None, f"No {fmt} target for {platform} Images"
        candidates = served
    if mode is not None:
        return mode, None
    best, matched = _best_reader(
        path, candidates, tools_registry,
        lambda spec: tools_registry.mode_reads_extensions(spec.mode),
    )
    if best is None:
        return None, f"No {fmt} target reads this Image"
    if matched and matched.lower() == fmt:
        return None, f"Already {fmt.lstrip('.').upper()}"
    return best.mode, None


def auto_reads(path: str, tools_registry) -> bool:
    """Whether any mode Auto may pick reads ``path``'s extension.

    A cheap, name-only test for walking a tree: a ``.bin`` beside its ``.cue``
    or a finished ``.chd`` is not an Image Auto would plan, so the CLI does
    not report it (#306).
    """
    for spec in tools_registry.mode_specs():
        if not spec.auto_target:
            continue
        if getattr(spec.kind, "value", spec.kind) not in _TARGET_KINDS:
            continue
        reads = tools_registry.mode_reads_extensions(spec.mode)
        if _extension_match(spec, path, reads) is not None:
            return True
    return False


def auto_route(path: str, platform: str, tools_registry) -> tuple[str | None, str | None]:
    """``(mode, None)`` for where Auto sends this Image, or ``(None, reason)``.

    ``default_mode`` picks the target. The Image is then left alone when the
    target writes the format it is already in (an RVZ under ``dolphin_rvz``):
    the mode's ``output_ext`` matching the input name says so, for any tool.
    Only a file's name (or an archive member's) says that; a folder's name
    says nothing about what it holds, so a PS3 folder named ``Game.iso`` is
    still built into an ISO. Blocking (a stat); call it off the event loop.
    """
    mode = default_mode(path, platform, tools_registry)
    if mode is None:
        return None, _no_target_reason(path, platform, tools_registry)
    output_ext = tools_registry.spec(mode).output_ext
    member = "::" in path
    name = path.split("::", 1)[-1]
    if (
        output_ext
        and match_extension(name, {output_ext}) is not None
        and (member or not os.path.isdir(path))
    ):
        return None, f"Already {output_ext.lstrip('.').upper()}"
    return mode, None


def _no_target_reason(path: str, platform: str, tools_registry) -> str:
    """Why an Image with a Platform has no target: the Platform, or the file."""
    if not _auto_candidates(platform, tools_registry):
        return f"No tool converts {platform} Images"
    name = path.split("::", 1)[-1]
    if "::" not in path and os.path.isdir(path):
        return f"No {platform} target takes this folder"
    ext = os.path.splitext(name)[1].lower() or name
    return f"No {platform} target reads {ext} files"


def _skip_reason(detection: Detection) -> str:
    if detection.how == CONFLICT:
        return f"Platform conflict: {detection.reason}"
    return f"Unsorted: {detection.reason}"


def _target_output(path: str, mode: str, output_dir: str | None, tools_registry) -> str | None:
    """Where ``mode`` would write ``path``'s output, before duplicate handling.

    The same resolution ``plan_job`` makes: an archive member is named from
    its flattened member name and lands beside the archive. None when the tool
    cannot say (the Image is then not checked for a collision).
    """
    tool = tools_registry.for_mode(mode)
    try:
        if "::" in path:
            from services.archive import archive_service

            archive, member = path.split("::", 1)
            name = archive_service._output_name_for_member(member)
            out = tool.output_path(
                mode, name, output_dir or os.path.dirname(archive), treat_as_stem=True,
            )
        else:
            out = tool.output_path(mode, path, output_dir)
    except (KeyError, ValueError, OSError):
        return None
    return os.path.normcase(os.path.abspath(out))


def plan_auto(
    paths: Iterable[str],
    *,
    compression_prefs: dict | None = None,
    output_dir: str | None = None,
    ready_tool_ids: Iterable[str] | None = None,
    tools_registry=None,
) -> list[AutoTarget]:
    """Work out each Image's Platform and default target, without queueing.

    ``compression_prefs`` is the saved per-tool compression (the convert
    panel's remembered setting, keyed by tool id). A mode that takes no
    compression gets None, and so does a tool with nothing saved, which means
    the tool's own default. Two Images whose targets would write the same
    output (``game.iso`` and ``game.nkit.iso`` both to ``game.rvz``) cannot
    both convert. The one the batch path would keep (``input_priority``, then
    the earlier in ``paths``) keeps it; the other is skipped with a reason.
    ``ready_tool_ids`` is what ``ToolRegistry.ready_tool_ids()`` answered (the
    caller awaits it; this function is blocking). An Image whose target tool is
    not in it is skipped with the tool's ``not_ready_reason``. None skips the
    check. Blocking; call it off the event loop.
    """
    if tools_registry is None:
        from services.tools import registry as tools_registry
    ready = set(ready_tool_ids) if ready_tool_ids is not None else None
    plan = _plan_targets(paths, compression_prefs or {}, tools_registry, ready)
    from services.output_conflicts import input_priority

    claimed: dict[str, int] = {}
    for index, target in enumerate(plan):
        if not target.mode:
            continue
        out = _target_output(target.file_path, target.mode, output_dir, tools_registry)
        if out is None:
            continue
        holder = claimed.get(out)
        if holder is None:
            claimed[out] = index
            continue
        # The batch path's rule: the higher input_priority keeps the output
        # (.cue/.gdi over .iso over .bin), ties to the earlier selection.
        keep, lose = holder, index
        if input_priority(target.file_path) > input_priority(plan[holder].file_path):
            keep, lose = index, holder
            claimed[out] = index
        winner = plan[keep].file_path
        plan[lose] = replace(
            plan[lose], mode=None, tool_id=None, compression=None,
            skip_reason=(
                f"Same output as {os.path.basename(winner.split('::')[-1])} "
                f"({os.path.basename(out)})"
            ),
        )
    return plan


def _plan_targets(paths, prefs: dict, tools_registry, ready) -> list[AutoTarget]:
    tools = tools_registry.all()
    plan: list[AutoTarget] = []
    for path in paths:
        detection = detect_platform(path, tools=tools)
        if not detection.decided:
            plan.append(AutoTarget(path, detection, None, None, None,
                                   _skip_reason(detection)))
            continue
        mode, reason = auto_route(path, detection.platform, tools_registry)
        if mode is None:
            plan.append(AutoTarget(path, detection, None, None, None, reason))
            continue
        spec = tools_registry.spec(mode)
        if ready is not None and spec.tool_id not in ready:
            tool = tools_registry.get(spec.tool_id)
            plan.append(AutoTarget(
                path, detection, None, None, None,
                f"{tool.display_name} isn't ready: {tool.not_ready_reason}",
            ))
            continue
        compression = None
        if spec.supports_compression or spec.supports_compression_level:
            compression = prefs.get(spec.tool_id) or None
        plan.append(AutoTarget(path, detection, mode, spec.tool_id, compression, None))
    return plan
