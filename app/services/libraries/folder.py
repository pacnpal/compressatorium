"""A folder as a Library: the second implementation of :class:`services.rules.Library`.

One root folder inside a Volume. The files under it are grouped into Images
(:func:`services.image_sets.group_images`): a ``.cue`` or ``.gdi`` owns the
tracks it references, a split dump's first part owns the rest, and playlists
are never Images. Each Image's Platform comes from Platform detection
(:func:`services.platform_detect.detect_uncached`), so an Image in a ``psx``
folder, or with a PS1 header, belongs to the ``psx`` Platform.

* A Platform's id is its slug (``psx``, ``ngc``); its name is the display name.
* An Image's key is its path relative to the root, with ``/`` separators.
* Conflicted and Unsorted Images belong to no Platform, so no Rule sees them
  and no sweep queues them. Nor does a file that cannot be read.
* ``recognized`` is the cached DAT or Hasheous match for the file, read in one
  query per listing. Unknown (None) when nothing usable is cached or the cache
  cannot be read; the engine's Recognized filters pass neither way on None.
  Nothing is hashed here.

**One walk.** A walk of the root is kept for :data:`SCAN_TTL_SECONDS`, so a
sweep over several ruled Platforms, and the listing that preceded it, share
it. Each file's sheet members are cached on the Library, keyed by path and
checked against size and mtime, and its detection against
``platform_detect.detection_key`` (the file and the tracks its header probe
reads, the same key the shared cache uses); both are pruned to the files the
last walk saw. A repeat walk of a large Library is a stat per file and reads no
headers.

**No disk on the event loop, no latch.** :meth:`FolderLibrary.configured` is
true: it answers on the event loop, so it touches no disk, and it must not
remember one bad walk. Every walk checks the root against the configured
Volumes itself, on the worker thread, and a root that is gone or unreachable
(a mount blip) fails that one listing with ``root_unavailable``; the next
sweep walks and checks again. Each candidate is still checked against the
Volumes before anything is queued.

**One walk at a time.** A second walk of the same Library while one is still
running fails at once (``LibraryUnreadable``, "previous listing still
running") instead of queueing behind it. On a mount that stopped answering
that caps the stuck threads at one per Library, and every later sweep releases
the sweep lock immediately. Settings (name, schedule switch, cap) change in
place and never wait for a walk.

**Findings.** Every walk (never a listing served from the last one) hands
the Findings store what it saw (:func:`services.findings.reconcile`): a
Platform conflict per conflicted Image, an Unsorted Finding per Image whose
Platform nobody could work out, and one per Image file the walk could not
read. A walk that fails (the root is gone) records nothing, and a subfolder
that does not list leaves the Findings under it as they are (dismissal
included), so a mount blip clears no Finding. Findings never move or delete
an Image.

Folder Rules support the skip policy only (no ``CONVERTED_HISTORY``), and a
failed Image waits until its file changes (``FAILED_HOLD``).
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from functools import cache
from typing import Any

from logging_setup import get_logger
from services import findings, image_sets, platform_detect
from services.platform_detect import CONFLICT, Detection
from services.platform_mappings import MappingsUnavailable
from services.rules.library import (
    ES_FRONTEND_REFERRER,
    FAILED_HOLD,
    GAMELISTS_FOLDER,
    RECOGNIZED_FILTER,
    AmbiguousFolder,
    Image,
    ImageListing,
    LibraryDefaults,
    LibraryUnreadable,
    Placement,
    PlacementUnknown,
    Platform,
    QueuedImage,
    folder_holder,
    placed_in,
)
from services.tools import registry
from utils.junk import is_junk_entry
from utils.path_utils import (
    is_within_configured_volumes,
    match_extension,
    platform_boundary_root,
)

logger = get_logger("folder_library")

ID_PREFIX = "folder:"

# A folder Library's layout (#430): where a Platform's Images go.
# ES-DE and Batocera: <root>/<platform>, named as ES-DE names the system.
ESDE = "esde"
# RomM's standard structure: <root>/roms/<platform>, named by RomM's slug.
ROMM = "romm"
# Every Image in the root; the header decides its Platform.
FLAT = "flat"
LAYOUTS = (ESDE, ROMM, FLAT)

# How long one walk serves later listings. Long enough to cover a sweep over
# every ruled Platform; short enough that the Rules view is current.
SCAN_TTL_SECONDS = 30.0


class ListingInProgress(LibraryUnreadable):
    """Another walk of this Library is still running; try again shortly."""

    code = "listing_in_progress"


class RootUnavailable(LibraryUnreadable):
    """The root is gone, or not inside a configured Volume right now (a mount blip)."""

    code = "root_unavailable"


class MappingsUnreadable(LibraryUnreadable):
    """The folder Platform mappings could not be read, so no Platform is decided."""

    code = "mappings_unavailable"


@dataclass(frozen=True)
class Entry:
    """One Image found under the root (its primary file)."""

    key: str
    path: str
    size: int
    mtime: float
    detection: Detection


@dataclass
class Scan:
    """What one walk of the root found."""

    entries: list[Entry] = field(default_factory=list)
    # Image files and folders that could not be read. Never queued.
    unreadable: int = 0
    # The Image files among them, by path.
    unreadable_files: list[str] = field(default_factory=list)
    # Folders under the root that could not be listed: the walk did not see
    # into them, so their Findings are left alone.
    unwalked: list[str] = field(default_factory=list)
    taken_at: float = 0.0
    # The Library's generation when the walk started (`FolderLibrary.invalidate`).
    generation: int = 0
    # True when the generation moved during the walk (a mapping was set): its
    # Findings were not written and a fresh walk was asked for.
    stale: bool = False
    # How many finished jobs had changed files under the root when the walk
    # started (`FolderLibrary.note_paths_changed`, #353). A later one makes
    # this walk out of date for listings, not for its Findings.
    files_generation: int = 0

    def by_platform(self) -> dict[str, list[Entry]]:
        out: dict[str, list[Entry]] = {}
        for entry in self.entries:
            if entry.detection.platform:
                out.setdefault(entry.detection.platform, []).append(entry)
        return out


def _folder_generation(names: set[str]) -> str:
    """One folder's listing as a short marker: a digest of its sorted file names.

    Built once per folder per walk and shared by every file in it, so a cached
    membership compares this marker in constant time instead of the whole
    listing per file. A file name never holds NUL, so the join is unambiguous.
    """
    listing = "\0".join(sorted(names)).encode("utf-8", "surrogateescape")
    return hashlib.sha256(listing).hexdigest()


def _image_extensions() -> tuple[str, ...]:
    """Every extension a registered tool reads or writes."""
    return tuple(sorted(
        set(registry.convertible_extensions()) | set(registry.output_extensions())
    ))


def _image_count(folder: str) -> int:
    """How many Image files are under *folder*. Blocking; an unreadable one is 0."""
    extensions = _image_extensions()
    # ponytail: walks the folder each time; read the last Scan if placement gets hot.
    return sum(
        name.lower().endswith(extensions)
        for _dir, _subdirs, names in os.walk(folder)
        for name in names
    )


def _recognized(paths: list[str]) -> dict[str, bool | None]:
    """The cached DAT or Hasheous verdict for each path, in one query.

    True for a cached match from either source, False for a cached miss that
    is still valid for the lookup sources now configured, None when there is
    no usable row or the cache cannot be read. Never hashes.
    """
    if not paths:
        return {}
    # The usability rule lives with the match routes that write these rows;
    # imported lazily, as dat_sync does, to keep the import graph one-way.
    from routes.dat import cached_matches

    try:
        cached = cached_matches(paths)
    except Exception:  # the match cache is a hint here, never a failure
        # Unknown, not "not Recognized": the engine's filters pass neither way
        # on None, so an only_unmatched Rule queues nothing rather than all.
        logger.warning("folder_library: could not read the match cache", exc_info=True)
        return dict.fromkeys(paths)
    return {
        path: None if cached.get(path) is None else bool(cached[path].get("matched"))
        for path in paths
    }


class FolderLibrary(LibraryDefaults):
    """A folder registered as a Library."""

    kind = "folder"
    capabilities = frozenset({RECOGNIZED_FILTER, FAILED_HOLD})
    legacy_preference_keys = MappingProxyType({})

    def __init__(
        self, *, record_id: int, root: str, name: str,
        schedule_enabled: bool = False, max_per_sweep: int = 25,
        es_frontend: bool = False, gamelists_folder: str | None = None,
        layout: str | None = None,
    ) -> None:
        self.record_id = record_id
        self.id = f"{ID_PREFIX}{record_id}"
        self.root = root
        self.name = name
        self._schedule_enabled = schedule_enabled
        self._max_per_sweep = max_per_sweep
        # Used by an EmulationStation frontend (#386): turns the gamelist.xml
        # Referrer on for this Library's Images.
        self._es_frontend = bool(es_frontend)
        # Its ES-DE gamelists folder (#387), or None.
        self._gamelists_folder = gamelists_folder or None
        # Where a Platform's Images go (#430); one of LAYOUTS.
        self.layout = layout if layout in LAYOUTS else ESDE
        # Held for a whole walk. Only ever acquired without blocking (`scan`).
        self._lock = threading.Lock()
        self._scan: Scan | None = None
        # Bumped when something the last walk relied on changed (a mapping),
        # so the next listing walks again instead of serving it.
        self._generation = 0
        # Bumped by every finished job that changed a file under the root
        # (#353). Kept apart from `_generation`, which also decides whether a
        # walk's Findings were overtaken: a busy night of conversions must
        # make the next listing walk again, not refuse listings as stale.
        self._files_generation = 0
        # The Rule engine's generation for this Library when this object was
        # built (#303). Removing the Library moves it, so a walk of this
        # object that finishes, or even starts, after the removal records no
        # Findings. A re-registered Library is a new object with a new token.
        from services.rules import sweep as rules_sweep

        self._token = rules_sweep.library_generation(self.id)
        # The follow-up walk `_schedule_rewalk` started, if any (tests join it).
        self._rewalk_thread: threading.Thread | None = None
        # Serialises a settings change (database row, then this object), so
        # two concurrent edits leave both saying the same thing. Never the walk
        # lock: a settings edit must not wait for a walk.
        self.settings_lock = threading.Lock()
        # path -> (size, mtime_ns, value), for the files the last walk saw.
        # path -> (platform_detect.detection_key, Detection).
        self._detections: dict[str, tuple[tuple, Detection]] = {}
        # path -> ((size, mtime_ns, the folder's listing marker), members).
        self._members: dict[str, tuple[tuple, list[str]]] = {}

    def __repr__(self) -> str:
        return f"FolderLibrary(id={self.id!r}, root={self.root!r})"

    def apply_settings(
        self, *, name: str | None = None, schedule_enabled: bool | None = None,
        max_per_sweep: int | None = None, es_frontend: bool | None = None,
        gamelists_folder: str | None = None, layout: str | None = None,
    ) -> None:
        """Change this Library's settings in place. Never waits for a walk.
        A *gamelists_folder* of ``""`` clears it."""
        if name is not None:
            self.name = name
        if schedule_enabled is not None:
            self._schedule_enabled = bool(schedule_enabled)
        if max_per_sweep is not None:
            self._max_per_sweep = int(max_per_sweep)
        if es_frontend is not None:
            self._es_frontend = bool(es_frontend)
        if gamelists_folder is not None:
            self._gamelists_folder = gamelists_folder or None
        if layout in LAYOUTS:
            self.layout = layout

    def referrer_enabled(self, referrer_id: str) -> bool:
        """The gamelist.xml Referrer is on when the Library is used by an
        EmulationStation frontend (#386); every other opt-in one is off."""
        return referrer_id == ES_FRONTEND_REFERRER and self._es_frontend

    def referrer_options(self, referrer_id: str) -> dict:
        """The gamelist Referrer's ES-DE gamelists folder, when one is set (#387)."""
        if referrer_id == ES_FRONTEND_REFERRER and self._gamelists_folder:
            return {GAMELISTS_FOLDER: self._gamelists_folder}
        return {}

    def _platforms_parent(self) -> str:
        """The folder whose subfolders are the Platform folders, by layout."""
        return os.path.join(self.root, "roms") if self.layout == ROMM else self.root

    def platform_folder(self, path: str) -> str | None:
        """The Platform folder holding *path* (#387): the folder directly under
        the root that holds it, or, when that is the root's ``roms`` folder,
        the one directly under ``roms`` (RomM style, #430).

        Whatever the layout: the layout decides where new Images go, and
        changing it moves nothing, so an Image already in ``<root>/ps2`` is
        still in its Platform folder after a switch to RomM style (Codex
        PRRT_kwDOQ9nULM64es7B). None for an Image directly in the root or in
        ``roms``.
        """
        return self.platform_folders()(path)

    def platform_folders(self) -> Callable[[str], str | None]:
        """:meth:`platform_folder` for many paths: the root (and its ``roms``
        folder) is listed once, not once per Image."""
        in_root = folder_holder(self.root)
        below = cache(folder_holder)

        def platform_folder(path: str) -> str | None:
            holding = in_root(path)
            if holding is not None and os.path.basename(holding) == "roms":
                return below(holding)(path)
            return holding

        return platform_folder

    def placement(
        self, platform: str, *, known: Sequence[Platform] | None = None,
    ) -> Placement:
        """Where *platform*'s Images go, by this Library's layout (#430).
        *known* is unused: the folders themselves say where a Platform is.

        Flat: the root. Otherwise a folder directly under the root (ES-DE
        style) or under ``roms`` (RomM style) that already names the Platform,
        by the user's mapping or a name detection reads, spelled as listed;
        else a new one named as ES-DE (or RomM) names it. Two folders that
        both name it: the one holding the most Images, marked ``guessed``
        (#515). Blocking (lists one folder, or walks the candidates).
        """
        slug = platform_detect.known_slug(platform)
        if slug is None:
            raise PlacementUnknown(f"{platform!r} is not a Platform")
        try:
            mapped = platform_detect.mapping_snapshot()
        except MappingsUnavailable as exc:
            raise PlacementUnknown(str(exc)) from exc
        if self.layout == FLAT:
            return placed_in(self.root, slug, mapped=mapped)
        parent = self._platforms_parent()
        try:
            found = platform_detect.platform_folders(parent, slug, mapped=mapped)
        except OSError as exc:
            raise PlacementUnknown(f"{parent} can't be listed ({exc})") from exc
        if len(found) > 1:
            # A best guess, not a refusal (#515): the folder already holding
            # the most Images, ties by name. Marked so the UI says it guessed.
            folder = max(sorted(found), key=_image_count)
            return replace(placed_in(folder, slug, mapped=mapped), guessed=True)
        if found:
            folder = found[0]
        else:
            name = slug if self.layout == ROMM else platform_detect.esde_folder_name(slug)
            folder = os.path.join(parent, name)
        return placed_in(folder, slug, mapped=mapped)

    def is_current(self) -> bool:
        """Still a registered Library (#303's token). Asked under the Findings store's lock."""
        from services.rules import sweep as rules_sweep

        return rules_sweep.library_generation(self.id) == self._token

    def invalidate(self) -> None:
        """The last walk is out of date; the next listing walks again. Never waits."""
        self._generation += 1

    def walk_generation(self) -> int:
        """Moves with every :meth:`invalidate` (a mapping) and every job that changed files."""
        return self._generation + self._files_generation

    def note_paths_changed(self, paths: Iterable[str]) -> None:
        """Drop the walk when a finished job changed a path under this root (#353).

        Lexical, against the root as registered (resolved), so it touches no
        disk on the event loop; dropping a walk too often only costs a walk.
        """
        root = self.root.rstrip(os.sep)
        for path in paths:
            if not path:
                continue
            disk_path = os.path.normpath(path.split("::", 1)[0])
            if disk_path == root or disk_path.startswith(root + os.sep):
                self._files_generation += 1
                return

    # ---- the walk ----

    def _files(self) -> tuple[list[tuple[str, os.stat_result]], int, list[str], list[str]]:
        """Every regular, readable file with an Image extension, and the unreadable ones.

        ``(files, how many files and folders could not be read, the unreadable
        Image files, the folders that could not be listed)``.
        """
        root = self.root
        # Checked on every walk, never remembered: a mount that blips fails
        # this listing only.
        if not is_within_configured_volumes(root, treat_archives=False):
            raise RootUnavailable(f"{root} is not reachable inside the configured Volumes")
        if not os.path.isdir(root):
            raise RootUnavailable(f"{root} is not a folder")
        from services import libraries

        # Another Library's root inside this one owns its files (#514): not
        # walked, so neither Rules, Findings nor duplicates here see them.
        inner = libraries.inner_folders(self)
        extensions = _image_extensions()
        files: list[tuple[str, os.stat_result]] = []
        unreadable = 0
        unreadable_files: list[str] = []
        unwalked: list[str] = []
        root_errors: list[OSError] = []

        def on_error(exc: OSError) -> None:
            nonlocal unreadable
            if os.path.normpath(getattr(exc, "filename", "") or "") == os.path.normpath(root):
                root_errors.append(exc)
            else:
                unreadable += 1
                if getattr(exc, "filename", None):
                    unwalked.append(os.fspath(exc.filename))

        for current, dirs, names in os.walk(root, followlinks=False, onerror=on_error):
            # Junk (OS metadata, RomM's `.romm_tmp_*` staging) is neither
            # walked into nor listed: the same predicate the file browser uses.
            dirs[:] = sorted(
                d for d in dirs if not is_junk_entry(d) and not (
                    inner and libraries.is_inner(
                        os.path.relpath(os.path.join(current, d), root), inner,
                    )
                )
            )
            for name in sorted(n for n in names if not is_junk_entry(n)):
                path = os.path.join(current, name)
                # A split output's numbered parts (`Game.iso.0`, `.1`) carry no
                # Image extension; they are listed, and `image_members` folds
                # them into one Image on the first part.
                if (
                    match_extension(name, extensions) is None
                    and registry.split_owner(path) is None
                ):
                    continue
                try:
                    st = os.lstat(path)
                except OSError:
                    unreadable += 1
                    unreadable_files.append(path)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if not os.access(path, os.R_OK):
                    unreadable += 1
                    unreadable_files.append(path)
                    continue
                files.append((path, st))
        if root_errors:
            raise LibraryUnreadable(str(root_errors[0]))
        return files, unreadable, unreadable_files, unwalked

    def _walk(self) -> Scan:
        """Walk the root once, group the files into Images and detect each Platform."""
        # What this walk's detections are valid for: a mapping set while it
        # runs bumps the generation, and its Findings are then not written.
        walk_generation = self._generation
        files_generation = self._files_generation
        files, unreadable, unreadable_files, unwalked = self._files()
        stats = dict(files)
        known = {os.path.normpath(p): st for p, st in files}
        members_cache: dict[str, tuple[tuple, list[str]]] = {}
        detections: dict[str, tuple[tuple, Detection]] = {}
        # A split set's members depend on which sibling parts exist
        # (game_part2.wud added later), not on part 1's own stat, so the
        # folder's listing is part of the cache key: as one marker per folder,
        # built once per walk, so the compare per file is constant-time.
        names_by_folder: dict[str, set[str]] = {}
        for path, _st in files:
            folder, name = os.path.split(path)
            names_by_folder.setdefault(folder, set()).add(name)
        generation = {
            folder: _folder_generation(names)
            for folder, names in names_by_folder.items()
        }

        def members(path: str) -> list[str]:
            st = stats[path]
            key = (st.st_size, st.st_mtime_ns, generation[os.path.dirname(path)])
            hit = self._members.get(path)
            if hit is not None and hit[0] == key:
                value = hit[1]
            else:
                value = image_sets.image_members(path)
            members_cache[path] = (key, value)
            return value

        scan = Scan(
            unreadable=unreadable, unreadable_files=unreadable_files,
            unwalked=unwalked, generation=walk_generation,
            files_generation=files_generation,
        )
        root = self.root
        # The folder context the folder-name fallback reads beyond the file's
        # own stat: where the root really is (an ancestor renamed and replaced
        # by a symlink keeps the Library path and every file stat, while the
        # folders around the Image change) and the Volume root the fallback
        # stops at (`platform_boundary_root`, the same boundary detection
        # uses). Resolved once per walk, not per Image: the walk does not
        # follow symlinks below the root, so every path under it shares this.
        context = (os.path.realpath(root), str(platform_boundary_root(root) or ""))
        # One read of the folder Platform mappings for the whole walk: every
        # Image's cache key and decision see the same evidence. A read that
        # fails stops the walk (nothing is listed for Rules and no Finding is
        # written) rather than deciding without the mappings.
        try:
            mapped = platform_detect.mapping_snapshot()
        except MappingsUnavailable as exc:
            raise MappingsUnreadable(str(exc)) from exc
        for path in image_sets.group_images([p for p, _ in files], members=members):
            st = stats[path]
            # Detection's own key: the file and the tracks its header probe
            # reads, so a changed track re-detects its sheet, plus the walk's
            # folder context. None (a track missing) means answer now and
            # remember nothing.
            key = platform_detect.detection_key(path, st, mapped=mapped)
            if key is not None:
                key = (*key, context)
            hit = self._detections.get(path)
            if key is not None and hit is not None and hit[0] == key:
                detection = hit[1]
            else:
                detection = platform_detect.detect_uncached(path, mapped=mapped)
            if key is not None:
                detections[path] = (key, detection)
            scan.entries.append(Entry(
                key=self._key(path),
                path=path,
                # The whole Image (a sheet and its tracks), so size filters
                # and the size orders judge the set, not a 67-byte .cue.
                size=image_sets.image_size(path, members=members, known=known),
                mtime=st.st_mtime, detection=detection,
            ))
        # An unreadable file a readable sheet lists is that sheet's track, not
        # an Image of its own, so it gets no Finding (CONTEXT.md).
        owned = {
            os.path.normpath(member)
            for _key, listed in members_cache.values() for member in listed
        }
        scan.unreadable_files = [
            p for p in scan.unreadable_files if os.path.normpath(p) not in owned
        ]
        # Only what this walk saw, so the caches are as large as the Library.
        self._members = members_cache
        self._detections = detections
        # The reuse window starts once the walk is ready, not when it began:
        # a walk whose detection outlasts SCAN_TTL_SECONDS is still shared.
        scan.taken_at = time.monotonic()
        self._record_findings(scan)
        return scan

    def _key(self, path: str) -> str:
        return Path(os.path.relpath(path, self.root)).as_posix()

    def _findings(self, scan: Scan) -> list[findings.Observed]:
        """What this walk noticed that needs a person: conflicts and Unsorted Images."""
        out = []
        for entry in scan.entries:
            found = entry.detection
            if found.platform:
                continue
            # The evidence that decided, and only that: a Platform folder name
            # a nearer mapping overruled is no part of the cause.
            cause = {
                "how": found.how, "header": found.header,
                "folder": self._key(found.place) if found.place else None,
                "folder_platform": found.place_platform, "mapped": found.mapped,
            }
            if found.headers:
                # Which headers disagree: a different pair is a new cause.
                cause["headers"] = list(found.headers)
            kind = findings.PLATFORM_CONFLICT if found.how == CONFLICT else findings.UNSORTED
            out.append(findings.Observed(
                kind=kind, image_key=entry.key, path=entry.path,
                reason=found.reason, cause=cause,
            ))
        for path in scan.unreadable_files:
            out.append(findings.Observed(
                kind=findings.UNSORTED, image_key=self._key(path), path=path,
                reason="the file could not be read", cause={"how": "unreadable"},
                # A file that was readable before (and may have a dismissed
                # Finding) keeps what it has: not read is not changed.
                only_if_new=True,
            ))
        return out

    def _record_findings(self, scan: Scan) -> None:
        """Hand the Findings store what this walk saw. A failure costs the Findings only.

        Written only while nothing the walk relied on has changed: asked under
        the store's write lock, a removed Library writes nothing, and a walk
        that a mapping change overtook writes nothing and marks itself stale
        so :meth:`scan` asks for a fresh walk.
        """
        observed = self._findings(scan)

        def current() -> bool:
            if not self.is_current():
                return False
            if self._generation != scan.generation:
                scan.stale = True
                return False
            return True

        try:
            findings.reconcile(
                self.id, observed,
                keep_under=[self._key(path) for path in scan.unwalked],
                current=current,
            )
            # A check Rule's "not Intact" (#305) for an Image that is gone
            # (converted away, deleted): no walk or check would see it again.
            findings.clear_gone(self.id, findings.NOT_INTACT, current=current)
            # A duplicate group (#388) with one copy left is no duplicate,
            # whether or not a find-duplicates run comes again.
            findings.clear_gone(self.id, findings.DUPLICATE, current=current)
        except Exception:
            # The walk is still good for Rules; the Findings view is behind
            # until the next walk. Logged, not swallowed.
            logger.warning(
                "folder_library: could not record the Findings of %s", self.root,
                exc_info=True,
            )

    def _is_stale(self, scan: Scan) -> bool:
        """A mapping changed during this walk (before or after its Findings write)."""
        return scan.stale or scan.generation != self._generation

    def scan(self, *, refresh: bool = False, rewalk_if_stale: bool = True) -> Scan:
        """The latest walk, taking a new one when asked or when it is stale. Blocking.

        Raises :class:`LibraryUnreadable` at once when another walk of this
        Library is still running, rather than waiting behind it. A walk that a
        mapping change overtook is still returned (the listing and Findings
        path); it starts a follow-up walk unless *rewalk_if_stale* is False,
        for a caller that walks again itself (:meth:`images`).
        """
        if not self._lock.acquire(blocking=False):
            raise ListingInProgress(
                f"previous listing of {self.root} still running",
            )
        try:
            generation = self._generation
            fresh = (
                self._scan is not None
                and self._scan.generation == generation
                and self._scan.files_generation == self._files_generation
                and time.monotonic() - self._scan.taken_at < SCAN_TTL_SECONDS
            )
            walked = None
            if refresh or not fresh:
                walked = self._scan = self._walk()
            scan = self._scan
        finally:
            self._lock.release()
        if walked is not None and rewalk_if_stale and self._is_stale(walked):
            # A mapping changed under this walk, before its Findings were
            # written (they were dropped) or after (they are out of date).
            # Started after the lock is released, so the follow-up walk is not
            # refused as "still running".
            self._schedule_rewalk()
        return scan

    def _schedule_rewalk(self) -> None:
        """Walk again on a daemon thread, as #303's walks run: never a pool worker."""
        def rewalk() -> None:
            try:
                self.scan(refresh=True)
            except (LibraryUnreadable, OSError) as exc:
                # Another walk took over (it sees the new mapping), or the root
                # blipped: the next listing or sweep walks again.
                logger.info("folder_library: follow-up walk of %s: %s", self.root, exc)

        thread = threading.Thread(
            target=rewalk, name=f"folder-rewalk-{self.record_id}", daemon=True,
        )
        self._rewalk_thread = thread
        thread.start()

    def _games_folder(
        self, entries: list[Entry], platform_folder: Callable[[str], str | None],
    ) -> tuple[str | None, str | None]:
        """Where most of a console's games are, relative to the root (#541):
        their console folder, whatever the layout says new ones get, and a
        note when that can't be told. ``(None, None)`` when they sit in the
        root itself.

        A game two folders claim (a symlinked alias, ``AmbiguousFolder``)
        counts for neither: that check still refuses to pick, but only this
        console's folder is left unknown, never the whole listing.
        """
        held: Counter = Counter()
        ambiguous = None
        for entry in entries:
            try:
                folder = platform_folder(entry.path)
            except AmbiguousFolder as exc:
                ambiguous = str(exc)
                continue
            # None is the root: games sitting there count too, so most games in
            # the root open the root.
            held[folder or None] += 1
        if not held:
            return None, ambiguous
        folder = held.most_common(1)[0][0]
        if folder is None:
            return None, None
        return os.path.relpath(folder, self.root).replace(os.sep, "/"), None

    def summary(self, *, refresh: bool = False) -> dict[str, Any]:
        """Detected Platforms with Image counts, and the Images left out."""
        scan = self.scan(refresh=refresh)
        by_platform = scan.by_platform()
        platform_folder = self.platform_folders() if by_platform else None
        unsorted = conflicted = 0
        for entry in scan.entries:
            if entry.detection.platform:
                continue
            if entry.detection.how == CONFLICT:
                conflicted += 1
            else:
                unsorted += 1
        return {
            "platforms": [
                {
                    "id": slug, "slug": slug,
                    "name": platform_detect.platform_name(slug), "images": len(entries),
                    **dict(zip(
                        ("folder", "folder_note"), self._games_folder(entries, platform_folder),
                    )),
                }
                for slug, entries in sorted(by_platform.items())
            ],
            "unsorted": unsorted,
            "conflicted": conflicted,
            "unreadable": scan.unreadable,
        }

    # ---- the Library interface ----

    def configured(self) -> bool:
        """True. Called on the event loop, so no disk; and never latched.

        Whether the root is reachable is each walk's question (off the loop):
        a bad answer fails that sweep with ``root_unavailable``, and the next
        sweep asks again. See the module docstring.
        """
        return True

    def canonical_platform_id(self, raw: Any) -> str | None:
        if not isinstance(raw, str) or ":" in raw:
            return None
        return platform_detect.known_slug(raw)

    def platform_value(self, platform_id: str) -> str:
        return platform_id

    def platforms(self) -> list[Platform]:
        return [
            Platform(id=p["id"], slug=p["slug"], name=p["name"])
            for p in self.summary()["platforms"]
        ]

    def _scan_for_rules(self) -> Scan:
        """A walk no mapping change overtook, for Rules. Blocking.

        A stale walk is walked once more. If that one is stale too, the
        listing fails with the transient ``listing_in_progress`` (the sweep
        skips this Library for the tick and retries on the next), and a
        follow-up walk is started for the Findings. Rules never see Platform
        assignments a mapping already changed (PR #322).
        """
        scan = self.scan(rewalk_if_stale=False)
        if not self._is_stale(scan):
            return scan
        scan = self.scan(refresh=True, rewalk_if_stale=False)
        if not self._is_stale(scan):
            return scan
        self._schedule_rewalk()
        raise ListingInProgress(
            f"Platform mappings changed while {self.root} was listed; listing it again",
        )

    def images(self, platform_id: str) -> list[Image]:
        try:
            scan = self._scan_for_rules()
        except OSError as exc:
            raise LibraryUnreadable(str(exc)) from exc
        entries = scan.by_platform().get(platform_id, [])
        recognized = _recognized([e.path for e in entries])
        return [
            Image(
                key=e.key,
                name=os.path.basename(e.path),
                title=e.key,
                size=e.size,
                added_at=e.mtime,
                recognized=recognized.get(e.path),
                platform=platform_id,
                path=e.path,
                record=e,
            )
            for e in entries
        ]

    def all_images(self) -> ImageListing:
        """Every Image a fresh walk finds, Unsorted and Conflicted ones included (#388).

        The same walk as every listing: junk (OS metadata, RomM's
        ``.romm_tmp_*`` staging) is never listed, and a sheet's tracks belong
        to their sheet. The walk's own record of what it could not see goes
        with them: the folders it could not list
        (``Scan.unwalked``) and the Image files it could not read
        (``Scan.unreadable_files``), as the walk's Findings already use them.
        Raises :class:`ListingInProgress` while another walk runs.
        """
        try:
            scan = self.scan(refresh=True)
        except OSError as exc:
            raise LibraryUnreadable(str(exc)) from exc
        return ImageListing(
            images=[
                Image(
                    key=e.key, name=os.path.basename(e.path), title=e.key, size=e.size,
                    added_at=e.mtime, platform=e.detection.platform, path=e.path, record=e,
                )
                for e in scan.entries
            ],
            unlisted=tuple(scan.unwalked),
            unreadable=tuple(scan.unreadable_files),
        )

    def rule_defaults(self) -> dict[str, Any]:
        return {}

    def max_per_sweep(self) -> int:
        return int(self._max_per_sweep)

    def schedule_enabled(self) -> bool:
        return bool(self._schedule_enabled)

    # ---- hooks: a folder Library has no steps of its own ----

    def sweep_blocked(self) -> str | None:
        return None

    def summary_fields(self) -> dict[str, Any]:
        return {}

    async def after_queue(
        self, rule: dict, spec: Any, queued: list[QueuedImage],
        summary: dict, result: dict,
    ) -> None:
        return None

    async def sweep_extras(self) -> dict[str, Any]:
        return {}
