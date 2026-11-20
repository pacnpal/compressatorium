"""The Library interface: what the Rule engine needs from a set of Images.

A Library is a set of Images that Rules run against (see ``CONTEXT.md``). The
engine in :mod:`services.rules.sweep` knows nothing about where a Library's
Images come from. It asks the Library for the Images of one Platform, filters
and orders them, and queues conversions. Anything only one kind of Library
does (RomM's metadata snapshot, for one) happens in that Library's hooks.

RomM is the first Library (:mod:`services.romm.library`). A folder Library is
one root folder inside a Volume, and its Image key is the path relative to that
root.

Blocking methods (``platforms``, ``images``, ``Image.path``) may touch the
network or a mount. The engine calls them off the event loop.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

# Capabilities a Library can declare. They say what a Library supports, so a
# Rules editor offers only what applies and the engine refuses the rest (see
# `duplicate_actions`).
#
# Images say whether they are Recognized (``Image.recognized``) and carry the
# Library's own verdict (``Image.identified``), so a Rule's only_matched /
# only_unmatched filters mean something.
RECOGNIZED_FILTER = "recognized_filter"
# A Rule may use the ``overwrite`` and ``rename`` duplicate policies. Both need
# the converted-history memory, keyed by Image key, to stay idempotent.
CONVERTED_HISTORY = "converted_history"
# An Image whose conversion failed is not queued again until its file changes
# (size or mtime). Without it a sweep re-queues a failing Image every run,
# which is what RomM's Rules have always done.
FAILED_HOLD = "failed_hold"
# The Library's own scanner indexes what appears beside an Image, so a
# conversion must not write there until its output is checked: in-progress
# output goes to a hidden staging folder and is placed only once its checks
# pass (``services.staging``, #398). RomM 5.4.1 indexes even dot-prefixed
# files, but skips dot-prefixed folders.
HIDDEN_STAGING = "hidden_staging"

# The opt-in Referrer a Library's "used by an EmulationStation frontend"
# setting turns on (#386): ``referrer_enabled`` answers True for it then.
ES_FRONTEND_REFERRER = "gamelist"
# Its option naming the ES-DE gamelists folder (#387), in referrer_options.
GAMELISTS_FOLDER = "gamelists_folder"


class AmbiguousFolder(Exception):
    """More than one folder holds a file, so which one it belongs to can't be told."""

    def __init__(self, path: str, folders: list[str]) -> None:
        names = ", ".join(sorted(os.path.basename(f) for f in folders))
        super().__init__(f"more than one Platform folder holds {path} ({names})")
        self.folders = folders


def folder_holding(parent: str, path: str) -> str | None:
    """The folder directly in *parent* that holds the file at *path*, spelled
    as *parent* lists it, or None when none does (#387).

    Which folder holds it is decided on resolved paths, so a symlinked folder
    (``ps2 -> actual/PlayStation2``) holds what its target holds; its name is
    the one listed. When several do (an alias, ``alias -> ps2``, or folders
    nested through symlinks), the answer would depend on the listing's order,
    so it raises :class:`AmbiguousFolder` instead (Codex PRRT_kwDOQ9nULM64Ki7x).
    Blocking; raises OSError when *parent* can't be listed.
    """
    real = os.path.realpath(path)
    holding = []
    with os.scandir(parent) as listing:
        for entry in listing:
            if entry.is_dir() and real.startswith(
                os.path.realpath(entry.path).rstrip(os.sep) + os.sep,
            ):
                holding.append(entry.path)
    if len(holding) > 1:
        raise AmbiguousFolder(path, holding)
    return holding[0] if holding else None


def log_prefix(library: Any) -> str:
    """What a log line about *library*'s Rules starts with (#477).

    Its own ``log_prefix`` when it sets one (RomM: ``romm_auto``), else
    ``f"{kind}_library"`` (a folder: ``folder_library``), else ``library``.
    Optional on the contract, so a Library that predates it still logs.
    """
    own = getattr(library, "log_prefix", None)
    if own:
        return str(own)
    kind = getattr(library, "kind", None)
    return f"{kind}_library" if kind else "library"


def duplicate_actions(library: Library) -> tuple[str, ...]:
    """The duplicate policies *library*'s Rules may use.

    ``skip`` is idempotent from the destination alone, so every Library has
    it. ``overwrite`` and ``rename`` need the converted history to stop, so
    they need :data:`CONVERTED_HISTORY`.
    """
    from services.rules.rules import DUPLICATE_ACTIONS

    if CONVERTED_HISTORY in library.capabilities:
        return DUPLICATE_ACTIONS
    return ("skip",)


class UnsupportedPolicy(ValueError):
    """A submitted Rule asks for a duplicate policy its Library cannot honour."""

    def __init__(self, platform_id: str, policy: str, allowed: tuple[str, ...]) -> None:
        super().__init__(f"{policy!r} is not available here; allowed: {', '.join(allowed)}")
        self.platform_id = platform_id
        self.policy = policy
        self.allowed = allowed

    def detail(self) -> dict:
        return {
            "error": "duplicate_action_unsupported",
            "platform_id": self.platform_id,
            "duplicate_action": self.policy,
            "allowed": list(self.allowed),
        }


def refuse_unsupported_policies(library: Library, raw: Any) -> None:
    """Raise :class:`UnsupportedPolicy` for a submitted Rule *library* cannot honour.

    For submissions. A stored blob is read leniently instead: ``normalize_rule``
    falls an unsupported policy back to ``skip``.
    """
    if not isinstance(raw, Mapping):
        return
    allowed = duplicate_actions(library)
    for platform_id, rule in raw.items():
        if not isinstance(rule, Mapping):
            continue
        policy = rule.get("duplicate_action")
        if policy is not None and policy not in allowed:
            raise UnsupportedPolicy(str(platform_id), str(policy), allowed)


class LibraryUnreadable(Exception):
    """The Library could not list a Platform's Images right now.

    ``code`` is what the sweep reports for the Platform (``"unreadable"`` by
    default). A Library raises a subclass with its own code when the reason is
    worth telling apart (a folder Library: ``root_unavailable``,
    ``listing_in_progress``).
    """

    code = "unreadable"


@dataclass(frozen=True)
class Platform:
    """One Platform of a Library.

    ``id`` is the Library's own id for it as a string (RomM's platform id,
    or a folder Library's folder slug). ``slug`` is the console slug the tool
    registry knows (``ngc``, ``ps2``), used to check a mode fits the Platform.
    ``folder`` is the Library's own name for its folder on disk, where that
    can differ from the slug (RomM's ``fs_slug``); None otherwise (#430).
    ``rom_count`` is how many Images the Library's own record says it holds
    (RomM's ``rom_count``, #512); None when the Library keeps no count.
    """

    id: str
    slug: str | None
    name: str
    folder: str | None = None
    rom_count: int | None = None


class Image:
    """One Image as a Library reports it.

    ``key`` identifies the Image inside its Library and is what the converted
    history is keyed by (RomM: the ROM id; a folder Library: the path relative
    to its root). ``name`` is the file name the Rule's name filters match,
    ``title`` what the ``name`` order sorts by. ``added_at`` is any number that
    grows as Images are added, newest largest.

    ``recognized`` says whether the Image is Recognized: True or False when the
    Library knows, None when it cannot say. A Rule's ``only_matched`` filter
    reads it. A folder Library reads its cached DAT or Hasheous match; RomM
    counts an Image Recognized when RomM identified it (a metadata-provider
    match, often by file name) or the app's own DAT or Hasheous match for its
    file is a hit (#427).

    ``identified`` is the Library's own verdict, which a Rule's
    ``only_unmatched`` filter reads. It defaults to ``recognized``; RomM sets
    it to RomM's "identified" status alone, so "only ROMs RomM could not
    identify" keeps meaning exactly that once the app Recognizes some of them.

    ``path`` is the local path, or None when the Image has none here. A Library
    that can only work it out by touching the disk passes ``resolve_path``
    instead, and the first read of ``path`` calls it once. That read can block,
    so the engine makes it off the event loop. ``recognized`` can be lazy the
    same way (``resolve_recognized``): the sweep reads it only for an Image the
    name and size filters kept, inside the bounded candidate probe, so an
    Image those filters exclude never touches the disk for it (Codex on PR
    #442). ``identified`` is never lazy; pass it when ``recognized`` is.

    ``record`` is the Library's own record for the Image, for its hooks.
    """

    __slots__ = (
        "_path", "_recognized", "_resolve_path", "_resolve_recognized", "added_at",
        "identified", "key", "name", "platform", "record", "size", "title",
    )

    def __init__(
        self,
        *,
        key: Hashable,
        name: str,
        size: int = 0,
        added_at: float = 0,
        recognized: bool | None = None,
        identified: bool | None = None,
        platform: str | None = None,
        title: str | None = None,
        path: str | None = None,
        resolve_path: Callable[[], str | None] | None = None,
        resolve_recognized: Callable[[], bool | None] | None = None,
        record: Any = None,
    ) -> None:
        self.key = key
        self.name = name
        self.size = size
        self.added_at = added_at
        self._recognized = recognized
        self._resolve_recognized = resolve_recognized
        self.identified = recognized if identified is None else identified
        self.platform = platform
        self.title = title if title is not None else name
        self._path = path
        self._resolve_path = resolve_path
        self.record = record

    @property
    def path(self) -> str | None:
        if self._resolve_path is not None:
            self._path = self._resolve_path()
            self._resolve_path = None
        return self._path

    @property
    def recognized(self) -> bool | None:
        if self._resolve_recognized is not None:
            self._recognized = self._resolve_recognized()
            self._resolve_recognized = None
        return self._recognized

    def __repr__(self) -> str:
        return f"Image(key={self.key!r}, name={self.name!r})"


@dataclass(frozen=True)
class ImageListing:
    """Every Image of a Library, and what the listing could not see (#388).

    ``unlisted`` names the folders it could not look into and ``unreadable``
    the Image files it found but could not read. Not seen is not gone: a
    caller that removes something because it was not listed (a duplicate
    group) keeps whatever touches these. A Library that lists from its own
    records (RomM) leaves both empty.
    """

    images: list[Image]
    unlisted: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()


@dataclass(frozen=True)
class Placement:
    """Where a Library puts a Platform's Images (#430): ``Library.placement``.

    ``folder`` is absolute and inside a Volume. ``exists`` is False for a
    Platform folder that isn't there yet: whoever places an Image creates it.

    ``guessed`` is True when the folder is a best guess the user may want to
    change (#512): RomM's default structure where its structure can't be read
    off the disk, or one of several RomM folders for the Platform that hold
    ROMs. ``why`` says in plain words how the folder was picked, or None when
    there was nothing to pick. ``choices`` are the folder names the Library
    could have picked from, each with its ROM count, when there are several
    (RomM's records for one Platform); empty otherwise.
    """

    platform: str
    folder: str
    exists: bool
    guessed: bool = False
    why: str | None = None
    choices: tuple[tuple[str, int], ...] = ()


class PlacementUnknown(Exception):
    """Where a Platform's Images go can't be told, so nothing may be placed.

    The message says why (two folders a folder Library can't choose between,
    a folder name with a path in it, a folder outside every Volume, a folder
    detection would read as another Platform). ``choices`` and ``picked``
    are the Platform's folder choices and the one picked, when the Library
    had several to pick from (Codex PR #516), so a bad pick can be changed.
    """

    def __init__(
        self, message: str = "", *,
        choices: tuple[tuple[str, int], ...] = (), picked: str | None = None,
    ) -> None:
        super().__init__(message)
        self.choices = choices
        self.picked = picked


@dataclass(frozen=True)
class QueuedImage:
    """One Image a sweep just queued, as handed to ``Library.after_queue``."""

    image: Image
    source: str
    destination: str
    pre: str


class Library(Protocol):
    """What the Rule engine needs from a Library."""

    # Stable id. Stored Rule and state keys are ``f"{id}:{platform_id}"``.
    id: str
    # What kind of Library this is (``"romm"``, ``"folder"``), its display
    # name, and its root folder here (None when it has none). For the API and
    # the Rules view; the engine never reads them.
    kind: str
    name: str
    root: str | None
    # Optional ``log_prefix``: what a log line about this Library's Rules
    # starts with (#477). Read through :func:`log_prefix`, which falls back to
    # ``f"{kind}_library"`` (``folder_library``); RomM sets ``romm_auto``.
    # A set of the capability names above.
    capabilities: frozenset[str]
    # Where an earlier release kept this Library's Rules or state without the
    # Library prefix, as ``{engine key: old preference key}``. Read once, moved
    # under the prefixed keys, then deleted. Empty for a new kind of Library.
    legacy_preference_keys: Mapping[str, str]

    def configured(self) -> bool:
        """Whether the Library can be swept at all right now."""

    def canonical_platform_id(self, raw: Any) -> str | None:
        """The canonical string form of a Platform id, or None if *raw* is not one."""

    def platform_value(self, platform_id: str) -> Any:
        """A Platform id as this Library's API reports it. Also its sort order."""

    def platforms(self) -> list[Platform]:
        """Every Platform in the Library. Blocking."""

    def images(self, platform_id: str) -> list[Image]:
        """The Images of one Platform. Blocking. Raises :class:`LibraryUnreadable`."""

    def all_images(self) -> ImageListing:
        """Every Image in the Library, whatever its Platform, and what the
        listing could not see. Blocking.

        What a find-duplicates run groups (#388): a copy of a disc is a copy
        whether or not its Platform is known. Raises :class:`LibraryUnreadable`.
        """

    def rule_defaults(self) -> Mapping[str, Any]:
        """Overrides for the engine's Rule defaults (interval, cap, verify, delete)."""

    def max_per_sweep(self) -> int:
        """The most a single sweep of this Library may queue, over all Platforms."""

    def schedule_enabled(self) -> bool:
        """The master switch: whether the scheduler sweeps this Library."""

    # ---- hooks for steps only this Library performs ----

    async def before_pass(self, *, dry_run: bool) -> None:
        """Called by ``run-rules`` (#307) before it sweeps this Library.

        The work the server does at startup or in its background loops that a
        sweep relies on: load saved settings, and, on a real pass, the
        maintenance only this Library has. A dry run must write nothing. A
        Library with nothing to do inherits the no-op from
        :class:`LibraryDefaults`. Errors propagate: ``run-rules`` reports them
        for this Library and goes on.
        """

    def sweep_blocked(self) -> str | None:
        """An error code when the Library must not be swept now, else None."""

    def excluded(self, name: str) -> bool:
        """Whether the Library ignores a file or game folder named *name*
        (#532): RomM's ``exclude:`` settings. The sweep skips such an Image,
        and a Rule whose outputs it would ignore. In memory, never blocks."""

    def referrer_enabled(self, referrer_id: str) -> bool:
        """Whether this Library's settings turn on the opt-in Referrer *referrer_id* (#383).

        Asked by the Referrer registry for each opt-in Referrer (an always-on
        one, like ``.m3u``, is never asked). Reads settings already in memory,
        so it never blocks. If it raises, the hook can't tell what to update
        and keeps the Source.
        """

    def referrer_options(self, referrer_id: str) -> Mapping[str, Any]:
        """This Library's settings for the Referrer *referrer_id* (#387).

        Read by a Referrer's ``for_library`` once the Referrer is on: for the
        gamelist Referrer, ``{"gamelists_folder": path}`` when an ES-DE
        gamelists folder is set. In memory, never blocks.
        """

    def platform_folder(self, path: str) -> str | None:
        """The Platform folder holding the file at *path*, or None (#387).

        Spelled as listed under the Library (a symlinked folder keeps its own
        name), so its name is the Platform's name a frontend knows. None when
        no Platform folder holds *path* (it sits directly in the root, or
        outside it); raises :class:`AmbiguousFolder` when more than one does.
        Blocking (lists a folder, resolves paths): off the loop.
        """

    def placement(
        self, platform: str, *, known: Sequence[Platform] | None = None,
    ) -> Placement:
        """Where Images of *platform* (a Platform slug, as detection names it)
        go in this Library (#430): the inverse of :meth:`platform_folder`.

        *known* is this Library's :meth:`platforms`, when the caller already
        listed them (placing every Platform of a listing): a Library that
        needs its own Platform records reads them from there instead of
        listing again (Codex PRRT_kwDOQ9nULM64es7O). Omitted, it lists them
        itself if it needs them.

        A folder that isn't there yet comes back with ``exists`` False;
        nothing here creates it. Detecting an Image placed there gives
        *platform* back, or names no Platform (a flat Library, where the
        header decides): never another one. Raises :class:`PlacementUnknown`
        when it can't be told, and :class:`LibraryUnreadable` when the
        Library can't be read. Blocking (lists folders, may ask RomM).
        """

    async def after_intake(self, placed: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        """Called after an Inbox pass placed Images in this Library (#433):
        RomM is asked to scan those Platform folders. Returns what happened
        for the pass summary, or None. Never raises."""

    def summary_fields(self) -> Mapping[str, Any]:
        """Extra keys every per-Platform summary starts with (and the result too)."""

    async def after_queue(
        self, rule: dict, spec: Any, queued: list[QueuedImage],
        summary: dict, result: dict,
    ) -> None:
        """Called once a Platform's batch is accepted by the queue."""

    async def sweep_extras(self) -> Mapping[str, Any]:
        """Extra keys added to a sweep's result when it ends."""


def placed_in(folder: str, platform: str, *, mapped: Mapping[str, str] | None = None) -> Placement:
    """*folder* as the :class:`Placement` of *platform*'s Images, once it's
    checked (#430). Every Library's ``placement`` ends here.

    Fails closed with :class:`PlacementUnknown`: a folder that isn't inside a
    Volume, or whose containment can't be told; a file where the folder would
    go; a folder whose own name or mapping, or a nearer folder above it,
    would make detection name another Platform for an Image placed in it.
    *mapped* is the mapping snapshot the caller chose the folder from.
    Blocking.
    """
    from services import platform_detect
    from services.platform_mappings import MappingsUnavailable
    from utils.path_utils import volume_containment

    if volume_containment(folder) is not True:
        raise PlacementUnknown(f"{folder} isn't inside a Volume, or that can't be told")
    if os.path.lexists(folder) and not os.path.isdir(folder):
        raise PlacementUnknown(f"{folder} is a file, not a folder")
    try:
        says = platform_detect.placed_platform(folder, mapped=mapped)
    except MappingsUnavailable as exc:
        raise PlacementUnknown(str(exc)) from exc
    if says is not None and says != platform:
        raise PlacementUnknown(
            f"an Image in {folder} would be detected as {says}, not {platform}",
        )
    return Placement(platform=platform, folder=folder, exists=os.path.isdir(folder))


class LibraryDefaults:
    """Defaults for the optional parts of the :class:`Library` contract.

    A Library class inherits this so that a hook it has no use for is a no-op,
    rather than a missing attribute that fails a caller such as ``run-rules``.
    Only hooks with a safe "nothing to do" answer belong here; everything
    else in the Protocol a Library must implement itself.
    """

    async def before_pass(self, *, dry_run: bool) -> None:
        """No upkeep before ``run-rules`` sweeps this Library."""

    async def after_intake(self, placed: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        """Nothing to tell anyone after an Inbox pass placed *placed* here (#433).

        *placed* is what the pass reported placed in this Library, each
        ``{"path", "platform", "output"}``. A Library that has someone to tell
        (RomM, which scans the Platform folders) overrides this and returns
        what happened, ``{"status", "folders", "reason"}``, for the pass
        summary. Never raises.
        """
        return None

    def excluded(self, name: str) -> bool:
        """A Library ignores no file by its name (#532)."""
        return False

    def referrer_enabled(self, referrer_id: str) -> bool:
        """No opt-in Referrer is on until a Library's settings say so (#383)."""
        return False

    def referrer_options(self, referrer_id: str) -> Mapping[str, Any]:
        """No Referrer settings (#387)."""
        return {}

    def platform_folder(self, path: str) -> str | None:
        """The folder directly under the root that holds *path* (#387), as a
        folder Library's Platform folders are laid out."""
        root = getattr(self, "root", None)
        return folder_holding(root, path) if root else None

    def all_images(self) -> ImageListing:
        """Every Platform's Images, one Platform after another (#388).

        A Library that also knows Images it cannot place on a Platform (a
        folder Library's Unsorted ones), or folders and files its listing
        could not read, overrides this to report them too.
        """
        out: list[Image] = []
        for platform in self.platforms():
            out.extend(self.images(platform.id))
        return ImageListing(images=out)

    def walk_generation(self) -> int:
        """Moves when what :meth:`images` lists is out of date (#353).

        The sweep reads it before listing and again before it records a run:
        a run whose listing went out of date in between (a conversion added an
        Image) is not recorded, so its Rule stays due. A Library that lists
        live every time never moves it.
        """
        return 0

    def note_paths_changed(self, paths: Iterable[str]) -> None:
        """A finished job changed *paths*; drop anything listed from before. Never waits.

        Called on the event loop for every finished job
        (``libraries.note_job_finished``), so it touches no disk. A Library
        that lists live every time has nothing to drop.
        """
