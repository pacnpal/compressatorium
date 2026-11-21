"""RomM as a Library: the first implementation of :class:`services.rules.Library`.

Wraps the RomM client. A Platform is a RomM platform (its integer id), an Image
is a ROM record, and an Image's key is the RomM ROM id, so the converted
history stays keyed by ROM id exactly as it was.

The steps only RomM performs reach the engine through the hooks here: the
identity-cleanup gate before a sweep, the metadata snapshot for re-pin after a
batch is queued, and the re-pin backlog reported when the sweep ends.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Callable, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services.platform_detect import canonical_slug
from services.romm import RommError, romm_client
from services.romm import connect as romm_connect
from services.romm import repin as romm_repin
from services.romm import settings as romm_settings

# Importing them registers the RomM Referrers: the swap (#391), and the
# re-pin skip for formats RomM re-identifies by itself (#393).
from services.romm.reidentify import REIDENTIFY_REFERRER
from services.romm.swap_referrer import SWAP_REFERRER
from services.rules.library import (
    CONVERTED_HISTORY,
    ES_FRONTEND_REFERRER,
    GAMELISTS_FOLDER,
    HIDDEN_STAGING,
    RECOGNIZED_FILTER,
    AmbiguousFolder,
    Image,
    LibraryDefaults,
    LibraryUnreadable,
    Placement,
    PlacementUnknown,
    Platform,
    QueuedImage,
    folder_holding,
    placed_in,
)
from services.rules.store import RULES_KEY, STATE_KEY
from services.subprocess_runner import run_detached
from utils.junk import is_junk_entry

# How long checking a folder pick may wait on RomM's catalog (#512).
_PICK_SECONDS = 60


def _count(raw: Any) -> int | None:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _one_folder(name: str) -> bool:
    return name not in ("", ".", "..") and "/" not in name and os.sep not in name


def _choices(listed: Sequence[Platform], slug: str) -> list[tuple[str, int, str]]:
    """RomM's folders for Platform *slug* (#512): ``(folder name, ROMs, the
    name of its record with the most ROMs)`` per folder, by name, records
    naming one folder counted together. A name with a path in it is dropped
    when there are others, so it's never picked over a good one."""
    folders: dict[str, tuple[int, int, str]] = {}
    for p in listed:
        if canonical_slug(p.slug) != slug:
            continue
        name = p.folder or slug
        count = p.rom_count or 0
        total, best, label = folders.get(name, (0, -1, p.name))
        folders[name] = (total + count, max(best, count), p.name if count > best else label)
    out = sorted((name, total, label) for name, (total, _, label) in folders.items())
    good = [c for c in out if _one_folder(c[0])]
    return good or out


def _roms(count: int) -> str:
    return "1 ROM" if count == 1 else f"{count} ROMs"


def _pick(slug: str, choices: list[tuple[str, int, str]]) -> tuple[str, bool, str | None]:
    """The folder name for *slug*'s new Images among *choices*, whether it is
    a guess, and why (#512). See ``RommLibrary.placement``."""
    if not choices:
        return slug, False, None
    if len(choices) == 1:
        return choices[0][0], False, None
    by_name = {name: (count, label) for name, count, label in choices}
    saved = romm_settings.platform_folders().get(slug)
    if saved in by_name:
        name, (count, label) = saved, by_name[saved]
        guessed, reason = False, "your choice"
    else:
        holding = [c for c in choices if c[1]]
        name, count, label = max(choices, key=lambda c: (c[1], c[0] == slug))
        guessed = len(holding) != 1
        if not guessed:
            reason = "the only one with ROMs"
        else:
            reason = "it has the most ROMs" if holding else "none has ROMs yet"
    return name, guessed, f"RomM's {label} folder, {_roms(count)}: {reason}"


def _structure(root: str, name: str) -> tuple[str, str | None]:
    """Where the Platform folder *name* sits in RomM's structure under *root*,
    and why it's a guess when it is one (see ``RommLibrary.placement``).
    RomM's own ``filesystem.structure`` when RomM declared it (#532), else
    read off the disk. Blocking."""
    declared = romm_connect.structure()
    if declared is not None:
        return _declared(root, name, *declared), None
    if not romm_connect.structure_known():
        # Not "no structure declared": RomM hasn't been asked successfully.
        # Retryable, not a refusal: an Inbox tries the file again on a later
        # poll instead of holding it for good (Codex PRRT_kwDOQ9nULM66SC3A).
        raise LibraryUnreadable(
            "RomM's folder structure couldn't be read yet; the next pass tries again",
        )
    own = os.path.join(root, name)
    if os.path.isdir(os.path.join(root, "roms")):
        return os.path.join(root, "roms", name), None
    if os.path.isdir(os.path.join(own, "roms")):
        return os.path.join(own, "roms"), None
    if os.path.isdir(os.path.join(own, "bios")):
        return (
            os.path.join(own, "roms"),
            f"guessed RomM's per-platform structure: {name} has a bios folder and no roms yet",
        )
    if os.path.isdir(own):
        return own, None
    # No folder for this Platform yet: follow the other Platform folders
    # already there (Codex PR #516). Creating ``<root>/roms`` beside them
    # would flip ``RommClient.local_path``'s layout and lose their records.
    others = _platform_dirs(root)
    if any(_holds_structure(os.path.join(root, d)) for d in others):
        return (
            os.path.join(own, "roms"),
            f"guessed RomM's per-platform structure, {name}/roms, like its other platforms",
        )
    if others:
        return own, f"guessed {name} beside RomM's other platform folders"
    return (
        os.path.join(root, "roms", name),
        f"guessed RomM's default structure, roms/{name}: there's no roms folder yet",
    )


def _declared(
    root: str, name: str, before: tuple[str, ...], levels: tuple[str | None, ...],
) -> str:
    """The folder RomM's declared structure puts *name*'s games in (#532).

    ``roms/...`` is read the way ``RommClient.local_path`` maps RomM's paths:
    a root with no ``roms`` folder but Platform folders in it is RomM's
    ``roms`` folder itself. Refuses a structure with a folder per group of
    games (``{category}``): which one a new game goes in isn't this app's
    call."""
    if None in levels:
        raise PlacementUnknown(
            f"RomM's folder structure, {romm_connect.layout().get('structure_template')}, "
            "keeps games in folders of their own under each platform, so this app "
            "can't tell which one a new game goes in",
        )
    return os.path.join(_prefix(root, before), name, *levels)


def _prefix(root: str, before: tuple[str, ...]) -> str:
    """The folder RomM's declared structure keeps the Platform folders in:
    *before* under *root*, a leading ``roms`` dropped when *root* is RomM's
    ``roms`` folder itself (see :func:`_declared`)."""
    if before[:1] == ("roms",) and not os.path.isdir(os.path.join(root, "roms")) \
            and _platform_dirs(root):
        before = before[1:]
    return os.path.join(root, *before)


def _holds_structure(folder: str) -> bool:
    return any(os.path.isdir(os.path.join(folder, d)) for d in ("roms", "bios"))


def _platform_dirs(root: str) -> list[str]:
    """The folders directly under *root* that can be Platform folders. A
    root ``bios`` folder is RomM's standard structure, never a Platform."""
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    return [
        e for e in entries
        if e != "bios" and not is_junk_entry(e) and os.path.isdir(os.path.join(root, e))
    ]


# Where RomM kept its Rules and run state before #296, unprefixed. The store
# reads them once, rewrites them under the "romm:" keys and deletes them.
LEGACY_RULES_KEY = "romm.rules"
LEGACY_STATE_KEY = "romm.auto_state"

# How long telling which Platform folders an Inbox pass added to may take.
_INTAKE_STEP_SECONDS = 60

logger = get_logger("romm_auto")


def rom_identified(rom: dict) -> bool:
    """Whether RomM identified this ROM record.

    RomM reports it two ways depending on version; either is authoritative.
    Without ``is_identified``, any provider id counts -- every provider, not a
    hand-picked subset: a ROM matched only through RetroAchievements or
    Hasheous is still identified, and checking three of the nine fields made
    ``only_matched`` skip it.
    """
    identified = rom.get("is_identified")
    if identified is None:
        identified = bool(romm_repin.metadata_ids(rom))
    return bool(identified)


def app_verdicts(paths: list[str]) -> dict[str, dict | None]:
    """The app's own cached DAT or Hasheous match for each local path (#427).

    Reads the match cache and never hashes (``routes.dat.cached_matches``);
    None for a path with no usable row. A cache that can't be read gives None
    for every path: unknown, never "not Recognized". Blocking.
    """
    from routes.dat import cached_matches

    try:
        return cached_matches(paths)
    except Exception:  # the match cache is a hint here, never a failure
        logger.warning("romm: could not read the match cache", exc_info=True)
        return dict.fromkeys(paths)


def image_from_rom(rom: dict, *, owned_elsewhere: Callable[[str], bool] | None = None) -> Image:
    """One RomM ROM record as an Image.

    ``title`` is RomM's display name (what the ``name`` order sorts by), and
    ``name`` the file name (what the name filters match). RomM numbers ROMs as
    it adds them, so the ROM id is its added order.

    ``identified`` is RomM's own "identified" status, which the
    ``only_unmatched`` filter reads; it needs no disk. ``recognized`` is True
    when RomM identified the ROM or the app's own cached match for its file is
    a hit (#427): what the ``only_matched`` filter reads. RomM always gives a
    verdict, so it is never unknown: no hit from either reads as not
    Recognized. For a ROM RomM did not identify it is worked out the first
    time it is read, since that maps the record onto the mount: the sweep reads
    it only for a ROM its name and size filters kept, inside the bounded
    candidate probe, so a ROM those filters exclude costs no disk at all
    (Codex on PR #442). The path is lazy for the same reason.

    *owned_elsewhere* answers, for the mapped path, whether another Library
    owns it (#514); such a ROM has no path here, so RomM's Rules leave it
    alone. Asked only when the path is read, so it stays lazy (Codex PR #518).
    """
    identified = rom_identified(rom)

    def recognized_by_app() -> bool:
        path = image.path
        if not path:
            return False
        match = app_verdicts([path]).get(path)
        return bool(match and match.get("matched"))

    image = Image(
        key=rom.get("id"),
        name=rom.get("fs_name") or rom.get("name") or "",
        title=rom.get("name") or rom.get("fs_name") or "",
        size=rom.get("fs_size_bytes") or 0,
        added_at=rom.get("id") or 0,
        recognized=True if identified else None,
        resolve_recognized=None if identified else recognized_by_app,
        identified=identified,
        platform=rom.get("platform_slug"),
        # Mapping the record onto the mount stats it, so it waits until the
        # sweep reads `path` off the event loop.
        resolve_path=lambda: _owned_path(romm_client.local_path(rom), owned_elsewhere),
        record=rom,
    )
    return image


def _owned_path(path: str | None, owned_elsewhere: Callable[[str], bool] | None) -> str | None:
    if path and owned_elsewhere is not None and owned_elsewhere(path):
        return None
    return path


def scan_refusal(exc: RommError) -> str:
    """Why RomM didn't take a scan, from its status, never its body (#433)."""
    if exc.status == 409:
        return "RomM refused it: a library scan is already running or queued"
    if exc.status == 503:
        return "RomM has no scan worker running, so it couldn't queue the scan"
    if exc.status == 401:
        return "RomM rejected the API token"
    if exc.status is not None:
        return f"RomM answered HTTP {exc.status} to the scan"
    return "RomM couldn't be reached to ask for the scan"


class RommLibrary(LibraryDefaults):
    """The RomM instance configured in the app, as a Library."""

    id = "romm"
    kind = "romm"
    name = "RomM"
    # The prefix RomM's Rule lines have always had (#477).
    log_prefix = "romm_auto"
    capabilities = frozenset({RECOGNIZED_FILTER, CONVERTED_HISTORY, HIDDEN_STAGING})
    legacy_preference_keys = MappingProxyType(
        {RULES_KEY: LEGACY_RULES_KEY, STATE_KEY: LEGACY_STATE_KEY},
    )

    @property
    def root(self) -> str | None:
        """RomM's library folder as mounted here, or None when unset."""
        return romm_client.library_root or None

    def configured(self) -> bool:
        return bool(romm_client.configured and romm_client.library_root)

    def decision_identity(self) -> tuple:
        """What names this Library besides its root: the RomM instance. A
        pending delete (#329) is decided against both."""
        return (romm_client.base_url,)

    def canonical_platform_id(self, raw: Any) -> str | None:
        try:
            return str(int(raw))
        except (TypeError, ValueError):
            return None

    def platform_value(self, platform_id: str) -> int:
        return int(platform_id)

    def platforms(self) -> list[Platform]:
        try:
            catalog = romm_client.platforms()
        except RommError as exc:
            raise LibraryUnreadable(str(exc)) from exc
        return [
            Platform(
                id=str(p["id"]),
                slug=p.get("slug"),
                name=p.get("display_name") or p.get("name") or str(p["id"]),
                # Its folder on disk, which the operator may have renamed (#430).
                folder=p.get("fs_slug"),
                rom_count=_count(p.get("rom_count")),
            )
            for p in catalog if isinstance(p, dict) and p.get("id") is not None
            # A NAS metadata folder RomM scanned as a platform (``@eaDir``,
            # #512) is never one: never offered, placed or swept.
            and not is_junk_entry(p.get("fs_slug") or "")
        ]

    def images(self, platform_id: str) -> list[Image]:
        # Imported here: services.libraries imports this module.
        from services import libraries

        try:
            roms = romm_client.roms(int(platform_id))
        except RommError as exc:
            raise LibraryUnreadable(str(exc)) from exc
        # A folder Library inside RomM's folder owns its files (#514), so
        # RomM's Rules leave them alone: such a ROM has no path here. Asked
        # when its path is read, inside the bounded candidate probe, never
        # for every ROM while listing (Codex PR #518); `local_path` answers it
        # resolved.
        inner = libraries.inner_folders(self)
        if not inner:
            return [image_from_rom(rom) for rom in roms]
        real_root = os.path.realpath(self.root)

        def owned_elsewhere(path: str) -> bool:
            return libraries.is_inner(os.path.relpath(path, real_root), inner)

        return [image_from_rom(rom, owned_elsewhere=owned_elsewhere) for rom in roms]

    def rule_defaults(self) -> dict[str, Any]:
        cfg = romm_settings.effective()
        return {
            "interval_minutes": cfg.get("auto_convert_interval_minutes"),
            "max_per_run": cfg.get("auto_convert_max_per_run"),
            "verify_after": cfg.get("verify_after_convert", False),
            "delete_on_verify": cfg.get("delete_source_after_verify", False),
        }

    def max_per_sweep(self) -> int:
        return int(romm_settings.effective().get("auto_convert_max_per_run", 25))

    def excluded(self, name: str) -> bool:
        """RomM's ``exclude:`` settings ignore files named *name* (#532)."""
        return romm_connect.excluded(name)

    def excluded_extension(self, ext: str) -> bool:
        """RomM's ``exclude:`` settings ignore every ``.<ext>`` file (#532)."""
        return romm_connect.excluded_extension(ext)

    async def refresh(self) -> None:
        """Re-read RomM's folder structure and exclusions, at most every few
        minutes (#532)."""
        await romm_connect.refresh_layout()

    def schedule_enabled(self) -> bool:
        return bool(romm_settings.effective().get("auto_convert"))

    # ---- hooks ----

    async def before_pass(self, *, dry_run: bool) -> None:
        """What the server does for RomM that ``run-rules`` needs (#307).

        Loads the saved RomM settings (the server does at startup). On a real
        pass it also finishes an identity cleanup a crash left owed (startup
        does that too; otherwise every sweep is refused with
        ``identity_cleanup_pending``) and runs one pass of the re-pin settler,
        so ROMs converted by an earlier pass get their metadata back without a
        server running. Re-pins recorded by this pass wait for the next one.
        """
        await romm_settings.load()
        if dry_run:
            return
        from routes import romm as romm_routes

        await romm_routes.replay_identity_cleanup()
        await romm_routes.settle_once()

    async def after_intake(self, placed: list[dict]) -> dict | None:
        """Ask RomM to scan the Platform folders an Inbox pass added to (#433).

        One quick scan of just those folders (``scan_platforms``), named by
        their folder on disk, so a Platform RomM has no row for yet is found
        too. Asked once per pass and never retried: a token without
        ``tasks.run`` is reported as ``missing_scope``, and any other refusal
        (a library scan already running, no scan worker, RomM down) as
        ``not_asked``; RomM's own next scan finds the files either way.
        """
        if not self.configured():
            return None
        outputs = [p["output"] for p in placed if p.get("output")]
        try:
            folders = await asyncio.wait_for(
                run_detached(self._platform_folders, outputs), _INTAKE_STEP_SECONDS,
            )
        except (asyncio.TimeoutError, OSError, AmbiguousFolder) as exc:
            logger.warning("romm: couldn't tell which Platforms an Inbox pass added to: %s", exc)
            why = str(exc) or "it took too long"
            return {
                "status": "not_asked", "folders": [],
                "reason": f"couldn't tell which Platform folders the Images went in: {why}",
            }
        if not folders:
            return None
        try:
            task_id = await run_in_threadpool(romm_client.scan_platforms, folders)
        except RommError as exc:
            logger.warning("romm: the scan of %s after an Inbox pass was refused: %s",
                           ", ".join(folders), exc)
            if exc.status == 403:
                return {
                    "status": "missing_scope", "folders": folders,
                    "reason": "RomM's API token lacks the tasks.run scope, so RomM wasn't "
                    "asked to scan; give the token tasks.run, or let RomM's own scan find "
                    "the new games",
                }
            return {"status": "not_asked", "folders": folders, "reason": scan_refusal(exc)}
        return {"status": "asked", "folders": folders, "task_id": task_id}

    def _platform_folders(self, outputs: list[str]) -> list[str]:
        """The Platform folder names (RomM's ``fs_slug``) holding *outputs*,
        sorted, each once. Blocking (lists RomM's library folder)."""
        names = set()
        for output in outputs:
            folder = self.platform_folder(output)
            if folder:
                names.add(os.path.basename(folder.rstrip(os.sep)))
        return sorted(names)

    def sweep_blocked(self) -> str | None:
        """Refuse a sweep while a change of RomM instance is half cleaned up.

        A change of RomM instance or library root has been installed but the
        cleanup that belongs with it has not finished -- the process died in
        between, or the startup replay failed transiently. Until it does,
        `converted` still holds the PREVIOUS library's history, and a sweep
        reading it decides a ROM of the *new* library is already converted
        because a same-named file was, somewhere else. Under skip that silently
        omits it forever; the other policies reconvert on a record that
        describes nothing here.

        Refused rather than retried here: the re-pin settler retries the
        cleanup on its own tick, under the locks it requires, and a sweep
        holding the sweep lock is the one caller that cannot take them.
        """
        if self.configured() and not romm_connect.exclusions_known():
            # RomM's exclusions were never read (#532): a sweep now would
            # convert what RomM ignores. Deferred, not guessed. (Placement
            # waits for the structure on its own: `_structure`.)
            return "romm_layout_unread"
        if not romm_settings.cleanup_owed():
            return None
        logger.warning(
            "romm_auto: refusing to sweep while a RomM identity cleanup is "
            "still owed; the previous instance's history is still live",
        )
        return "identity_cleanup_pending"

    def referrer_enabled(self, referrer_id: str) -> bool:
        """The gamelist.xml Referrer is on when RomM's settings say the library
        is used by an EmulationStation frontend (#386), and the RomM swap when
        the "keep the ROM id" switch is on (#390, #391). The re-pin skip for
        formats RomM re-identifies by itself is on while re-pinning is on and
        the swap is off (#393): with the swap on, the swap keeps the id. Every
        other opt-in one is off. Reads the settings in memory."""
        cfg = romm_settings.effective()
        if referrer_id == REIDENTIFY_REFERRER:
            return bool(cfg.get("repin_enabled")) and not cfg.get("swap_keep_rom_id")
        setting = {
            ES_FRONTEND_REFERRER: "es_frontend",
            SWAP_REFERRER: "swap_keep_rom_id",
        }.get(referrer_id)
        return setting is not None and bool(cfg.get(setting))

    def platform_folder(self, path: str) -> str | None:
        """The Platform folder holding *path* in either RomM layout (#387):
        ``<root>/roms/<platform>/...`` when the root holds a ``roms`` folder
        (the same directory-level test ``RommClient.local_path`` makes), else
        ``<root>/<platform>/...`` (``<root>/<platform>/roms/...`` included).
        Spelled as listed; None when no Platform folder holds it."""
        root = self.root
        if not root:
            return None
        declared = romm_connect.structure()
        if declared is not None:
            # Under the declared prefix (#532): `games/{platform}/{game}`
            # holds its Platform folders in `games`.
            return folder_holding(_prefix(root, declared[0]), path)
        roms = os.path.join(root, "roms")
        return folder_holding(roms if os.path.isdir(roms) else root, path)

    def placement(
        self, platform: str, *, known: Sequence[Platform] | None = None,
    ) -> Placement:
        """Where *platform*'s Images go in RomM's own folder structure (#430).

        The folder is named by a RomM platform record's ``fs_slug`` (its
        folder on disk, which the operator may have renamed), or by the slug
        itself for a Platform RomM has no record of yet, as RomM names a new
        one. The records are *known* when given (the placements listing hands
        each row its own record, so each shows its own folder), else RomM's
        whole catalog.

        RomM often keeps several records for one Platform (``famicom`` beside
        ``nes``, #512). Their folders are the choices, and the pick is, in
        order: the user's saved pick (``romm_settings.platform_folders``); the
        only folder holding ROMs (RomM's ``rom_count``); else the one with the
        most ROMs (then the one named as the Platform), marked ``guessed``.

        Where that folder sits is RomM's own ``filesystem.structure`` when
        RomM declared it (5.3+, read by ``services.romm.connect``, #532), not
        a guess. Otherwise it is read off the disk, at the directory level as
        ``RommClient.local_path`` maps ROMs:

        * ``<root>/roms`` exists: RomM's standard structure,
          ``<root>/roms/<folder>``.
        * ``<root>/<folder>/roms`` exists: the other structure,
          ``<root>/<folder>/roms``.
        * ``<root>/<folder>/bios`` alone: the other structure before its
          first ROM, ``<root>/<folder>/roms``, marked ``guessed``.
        * ``<root>/<folder>`` exists with neither ``roms`` nor ``bios`` in
          it: the root is RomM's ``roms`` folder itself, ``<root>/<folder>``.
        * No folder for it yet: beside the other Platform folders under the
          root, ``<root>/<folder>/roms`` when one holds ``roms`` or ``bios``,
          else ``<root>/<folder>``, never a new ``<root>/roms`` that would
          flip ``RommClient.local_path``'s layout (Codex PR #516); with none,
          RomM's default, ``<root>/roms/<folder>``. Marked ``guessed``.

        Refuses a folder name with a path in it, and whatever ``placed_in``
        refuses. Blocking: asks RomM for its platforms (unless *known* already
        holds them) and stats a few folders.
        """
        root = self.root
        if not root:
            raise PlacementUnknown("RomM's library folder isn't set")
        slug = canonical_slug(platform)
        if slug is None:
            raise PlacementUnknown(f"{platform!r} is not a Platform")
        listed = self.platforms() if known is None else known
        choices = _choices(listed, slug)
        name, guessed, why = _pick(slug, choices)
        if not _one_folder(name):
            raise PlacementUnknown(f"RomM's folder name {name!r} for {slug} isn't one folder")
        offered = tuple((n, count) for n, count, _ in choices) if len(choices) > 1 else ()
        folder, structure = _structure(root, name)
        try:
            placed = placed_in(folder, slug)
        except PlacementUnknown as exc:
            # Keep the choices, so a pick that can't take Images can be
            # changed (Codex PR #516).
            raise PlacementUnknown(str(exc), choices=offered, picked=name) from exc
        return replace(
            placed,
            guessed=guessed or structure is not None,
            why="; ".join(part for part in (why, structure) if part) or None,
            choices=offered,
        )

    async def pick_folder(self, platform: str, folder: str | None) -> None:
        """Save *folder* (one of RomM's folder names for *platform*) as where
        its new Images go (#512), or forget the pick when None. Refuses
        anything that isn't one of those folders with
        :class:`PlacementUnknown`; asks RomM for its platforms, bounded."""
        slug = canonical_slug(platform)
        if slug is None:
            raise PlacementUnknown(f"{platform!r} is not a Platform")
        if folder is not None:
            if not _one_folder(folder):
                raise PlacementUnknown(f"{folder!r} isn't one folder")
            listed = await asyncio.wait_for(run_detached(self.platforms), _PICK_SECONDS)
            names = [name for name, _, _ in _choices(listed, slug)]
            if folder not in names:
                raise PlacementUnknown(
                    f"{folder!r} isn't one of RomM's {slug} folders ({', '.join(names)})",
                )
        await romm_settings.pick_platform_folder(slug, folder)

    def referrer_options(self, referrer_id: str) -> dict:
        """The gamelist Referrer's ES-DE gamelists folder from RomM's settings,
        when one is set (#387). In memory."""
        folder = romm_settings.effective().get(GAMELISTS_FOLDER)
        if referrer_id == ES_FRONTEND_REFERRER and folder:
            return {GAMELISTS_FOLDER: folder}
        return {}

    def summary_fields(self) -> dict[str, Any]:
        return {"repins_recorded": 0}

    async def after_queue(
        self, rule: dict, spec: Any, queued: list[QueuedImage],
        summary: dict, result: dict,
    ) -> None:
        """Snapshot RomM's metadata for formats it cannot hash-match.

        Without it an automatic RVZ sweep destroys exactly the metadata the
        re-pin feature exists to protect. Only called once the batch is
        queued: recording before the queue call would leave pending rows
        behind for conversions that never ran, and those rows re-pin whatever
        later lands on that path. The records the sweep already holds carry
        the provider ids, so this costs no extra catalog fetch.
        """
        # Asked once per platform, not per ROM: whether this target format
        # keeps RomM's DAT match is a property of the mode. Honours the same
        # `repin_enabled` switch the manual path reads.
        cfg = romm_settings.effective()
        # RomM's own ids only across a format that loses its match; the
        # Hasheous ids the ROM lacks (#427) whatever the format, when that
        # switch is on. Both go through the same row.
        carry = romm_repin.mode_needs_repin(spec.output_ext)
        write_hasheous = romm_repin.hasheous_write_on()
        if not (bool(cfg.get("repin_enabled", True)) and (carry or write_hasheous)):
            summary["repins_recorded"] = 0
            return
        # This batch's age, fixed before the lookup is awaited: a manual plan
        # recorded for one of these destinations meanwhile is newer, and
        # `record()` must not let this one supersede it (CodeRabbit on PR #442).
        generation = await run_in_threadpool(romm_repin.plan_generation, None)
        fills = await romm_repin.hasheous_fills({
            item.source: item.image.record for item in queued if item.image.record
        }) if write_hasheous else {}
        repin_count = 0
        for item in queued:
            rom = item.image.record
            ids = romm_repin.row_ids(
                romm_repin.metadata_ids(rom) if rom and carry else {},
                fills.get(item.source, {}),
            )
            if not ids:
                continue
            try:
                # The fingerprint comes from candidate inspection, not from
                # now: on an idle queue a fast conversion can finish between
                # the batch being accepted and this call, and stating the
                # destination here would record the *finished* output as the
                # pre-conversion state -- after which the settler sees nothing
                # change and abandons the row, losing exactly the metadata this
                # exists to protect.
                if await run_in_threadpool(
                    functools.partial(
                        romm_repin.record, rom, item.destination, ids,
                        rule["mode"], item.pre, item.source, generation=generation,
                    ),
                ):
                    repin_count += 1
            except Exception:  # bookkeeping only; see below
                # A write that fails here (a locked database, a full volume)
                # costs this ROM its metadata, and nothing more. It must not
                # reach the sweep's queue handler: that marks the platform
                # `queue_failed` and skips the provenance record, while the
                # jobs it is reporting as failed are running -- so the next
                # sweep would queue every one of them again.
                logger.warning(
                    "romm_auto: could not record the re-pin for %s",
                    item.source, exc_info=True,
                )
                summary["repins_failed"] = summary.get("repins_failed", 0) + 1
        summary["repins_recorded"] = repin_count
        result["repins_recorded"] += repin_count

    async def sweep_extras(self) -> dict[str, Any]:
        # The authoritative backlog for the badge, not the sum of what this
        # run recorded: `record()` supersedes the pending row for a destination
        # rather than stacking one, so a sweep that re-records a retried
        # conversion adds rows without changing the total.
        return {"pending_repins": await run_in_threadpool(romm_repin.count_pending)}


romm_library = RommLibrary()
