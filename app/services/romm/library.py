"""RomM as a Library: the first implementation of :class:`services.rules.Library`.

Wraps the RomM client. A Platform is a RomM platform (its integer id), an Image
is a ROM record, and an Image's key is the RomM ROM id, so the converted
history stays keyed by ROM id exactly as it was.

The steps only RomM performs reach the engine through the hooks here: the
identity-cleanup gate before a sweep, the metadata snapshot for re-pin after a
batch is queued, and the re-pin backlog reported when the sweep ends.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Sequence
from types import MappingProxyType
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services.platform_detect import canonical_slug
from services.romm import RommError, romm_client
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

# Where RomM kept its Rules and run state before #296, unprefixed. The store
# reads them once, rewrites them under the "romm:" keys and deletes them.
LEGACY_RULES_KEY = "romm.rules"
LEGACY_STATE_KEY = "romm.auto_state"

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


def image_from_rom(rom: dict) -> Image:
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
        resolve_path=lambda: romm_client.local_path(rom),
        record=rom,
    )
    return image


class RommLibrary(LibraryDefaults):
    """The RomM instance configured in the app, as a Library."""

    id = "romm"
    kind = "romm"
    name = "RomM"
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
            )
            for p in catalog if isinstance(p, dict) and p.get("id") is not None
        ]

    def images(self, platform_id: str) -> list[Image]:
        try:
            roms = romm_client.roms(int(platform_id))
        except RommError as exc:
            raise LibraryUnreadable(str(exc)) from exc
        return [image_from_rom(rom) for rom in roms]

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
        roms = os.path.join(root, "roms")
        return folder_holding(roms if os.path.isdir(roms) else root, path)

    def placement(
        self, platform: str, *, known: Sequence[Platform] | None = None,
    ) -> Placement:
        """Where *platform*'s Images go in RomM's own folder structure (#430).

        The folder is named by the RomM platform's ``fs_slug`` (its folder on
        disk, which the operator may have renamed), or by the slug itself for
        a Platform RomM has no record of yet, as RomM names a new one. Where
        that folder sits is read off the disk, at the directory level as
        ``RommClient.local_path`` maps ROMs:

        * ``<root>/roms`` exists: RomM's standard structure,
          ``<root>/roms/<folder>``.
        * ``<root>/<folder>/roms`` exists: the other structure,
          ``<root>/<folder>/roms``.
        * ``<root>/<folder>`` exists with neither ``roms`` nor ``bios`` in
          it: the root is RomM's ``roms`` folder itself, ``<root>/<folder>``.

        Anything else can't be told (RomM 5.3 declares its structure in a
        setting this app doesn't read), so it refuses, as it does for two RomM
        platforms with the slug or a folder name with a path in it. Blocking:
        asks RomM for its platforms (unless *known* already holds them) and
        stats a few folders.
        """
        root = self.root
        if not root:
            raise PlacementUnknown("RomM's library folder isn't set")
        slug = canonical_slug(platform)
        if slug is None:
            raise PlacementUnknown(f"{platform!r} is not a Platform")
        listed = self.platforms() if known is None else known
        records = [p for p in listed if canonical_slug(p.slug) == slug]
        if len(records) > 1:
            ids = ", ".join(p.id for p in records)
            raise PlacementUnknown(f"RomM has more than one {slug} platform ({ids})")
        name = (records[0].folder if records else None) or slug
        if name in (".", "..") or "/" in name or os.sep in name:
            raise PlacementUnknown(f"RomM's folder name {name!r} for {slug} isn't one folder")
        own = os.path.join(root, name)
        if os.path.isdir(os.path.join(root, "roms")):
            folder = os.path.join(root, "roms", name)
        elif os.path.isdir(os.path.join(own, "roms")):
            folder = os.path.join(own, "roms")
        elif os.path.isdir(own) and not os.path.isdir(os.path.join(own, "bios")):
            folder = own
        else:
            raise PlacementUnknown(
                f"RomM's folder structure can't be told: {root} has no roms folder, "
                f"and no {name} folder with its ROMs yet",
            )
        return placed_in(folder, slug)

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
