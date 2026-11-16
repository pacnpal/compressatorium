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

from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# Capabilities a Library can declare. They say what a Library supports, so a
# Rules editor offers only what applies and the engine refuses the rest (see
# `duplicate_actions`).
#
# Images carry the Library's own identification (``Image.recognized``), so a
# Rule's only_matched / only_unmatched filters mean something.
RECOGNIZED_FILTER = "recognized_filter"
# A Rule may use the ``overwrite`` and ``rename`` duplicate policies. Both need
# the converted-history memory, keyed by Image key, to stay idempotent.
CONVERTED_HISTORY = "converted_history"
# An Image whose conversion failed is not queued again until its file changes
# (size or mtime). Without it a sweep re-queues a failing Image every run,
# which is what RomM's Rules have always done.
FAILED_HOLD = "failed_hold"


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
    """

    id: str
    slug: str | None
    name: str


class Image:
    """One Image as a Library reports it.

    ``key`` identifies the Image inside its Library and is what the converted
    history is keyed by (RomM: the ROM id; a folder Library: the path relative
    to its root). ``name`` is the file name the Rule's name filters match,
    ``title`` what the ``name`` order sorts by. ``added_at`` is any number that
    grows as Images are added, newest largest.

    ``recognized`` is the Library's own identification of the Image: True or
    False when it knows, None when it cannot say. It is what a Rule's
    ``only_matched`` / ``only_unmatched`` filters read. For RomM it is RomM's
    "identified" status (a metadata-provider match, often by file name), not
    Recognized in the glossary sense of a DAT or Hasheous hash match. It
    stands in until DAT-based recognition lands; code that needs a hash match
    must not read this field as one.

    ``path`` is the local path, or None when the Image has none here. A Library
    that can only work it out by touching the disk passes ``resolve_path``
    instead, and the first read of ``path`` calls it once. That read can block,
    so the engine makes it off the event loop.

    ``record`` is the Library's own record for the Image, for its hooks.
    """

    __slots__ = (
        "_path", "_resolve_path", "added_at", "key", "name", "platform",
        "recognized", "record", "size", "title",
    )

    def __init__(
        self,
        *,
        key: Hashable,
        name: str,
        size: int = 0,
        added_at: float = 0,
        recognized: bool | None = None,
        platform: str | None = None,
        title: str | None = None,
        path: str | None = None,
        resolve_path: Callable[[], str | None] | None = None,
        record: Any = None,
    ) -> None:
        self.key = key
        self.name = name
        self.size = size
        self.added_at = added_at
        self.recognized = recognized
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

    def __repr__(self) -> str:
        return f"Image(key={self.key!r}, name={self.name!r})"


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

    def referrer_enabled(self, referrer_id: str) -> bool:
        """Whether this Library's settings turn on the opt-in Referrer *referrer_id* (#383).

        Asked by the Referrer registry for each opt-in Referrer (an always-on
        one, like ``.m3u``, is never asked). Reads settings already in memory,
        so it never blocks. If it raises, the hook can't tell what to update
        and keeps the Source.
        """

    def summary_fields(self) -> Mapping[str, Any]:
        """Extra keys every per-Platform summary starts with (and the result too)."""

    async def after_queue(
        self, rule: dict, spec: Any, queued: list[QueuedImage],
        summary: dict, result: dict,
    ) -> None:
        """Called once a Platform's batch is accepted by the queue."""

    async def sweep_extras(self) -> Mapping[str, Any]:
        """Extra keys added to a sweep's result when it ends."""


class LibraryDefaults:
    """Defaults for the optional parts of the :class:`Library` contract.

    A Library class inherits this so that a hook it has no use for is a no-op,
    rather than a missing attribute that fails a caller such as ``run-rules``.
    Only hooks with a safe "nothing to do" answer belong here; everything
    else in the Protocol a Library must implement itself.
    """

    async def before_pass(self, *, dry_run: bool) -> None:
        """No upkeep before ``run-rules`` sweeps this Library."""

    def referrer_enabled(self, referrer_id: str) -> bool:
        """No opt-in Referrer is on until a Library's settings say so (#383)."""
        return False

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
