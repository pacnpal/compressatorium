"""RomM as a Library: the first implementation of :class:`services.rules.Library`.

Wraps the RomM client. A Platform is a RomM platform (its integer id), an Image
is a ROM record, and an Image's key is the RomM ROM id, so the converted
history stays keyed by ROM id exactly as it was.

The steps only RomM performs reach the engine through the hooks here: the
identity-cleanup gate before a sweep, the metadata snapshot for re-pin after a
batch is queued, and the re-pin backlog reported when the sweep ends.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services.romm import RommError, romm_client
from services.romm import repin as romm_repin
from services.romm import settings as romm_settings

# Importing it registers the RomM swap Referrer (#391).
from services.romm.swap_referrer import SWAP_REFERRER
from services.rules.library import (
    CONVERTED_HISTORY,
    ES_FRONTEND_REFERRER,
    HIDDEN_STAGING,
    RECOGNIZED_FILTER,
    Image,
    LibraryDefaults,
    LibraryUnreadable,
    Platform,
    QueuedImage,
)
from services.rules.store import RULES_KEY, STATE_KEY

# Where RomM kept its Rules and run state before #296, unprefixed. The store
# reads them once, rewrites them under the "romm:" keys and deletes them.
LEGACY_RULES_KEY = "romm.rules"
LEGACY_STATE_KEY = "romm.auto_state"

logger = get_logger("romm_auto")


def image_from_rom(rom: dict) -> Image:
    """One RomM ROM record as an Image.

    ``title`` is RomM's display name (what the ``name`` order sorts by), and
    ``name`` the file name (what the name filters match). RomM numbers ROMs as
    it adds them, so the ROM id is its added order.

    ``recognized`` carries RomM's own "identified" status, which is what the
    ``only_matched`` filter has always read. It is a stand-in: RomM identifies
    by metadata provider, often by file name, while Recognized in the glossary
    means a DAT or Hasheous hash match. See ``Image``.
    """
    # RomM reports identification two ways depending on version; treat either
    # as authoritative and fall back to "unknown" rather than filtering blind.
    identified = rom.get("is_identified")
    if identified is None:
        # Every provider, not a hand-picked subset: a ROM matched only through
        # RetroAchievements or Hasheous is still identified, and checking three
        # of the nine fields made `only_matched` skip it.
        identified = bool(romm_repin.metadata_ids(rom))
    return Image(
        key=rom.get("id"),
        name=rom.get("fs_name") or rom.get("name") or "",
        title=rom.get("name") or rom.get("fs_name") or "",
        size=rom.get("fs_size_bytes") or 0,
        added_at=rom.get("id") or 0,
        recognized=bool(identified),
        platform=rom.get("platform_slug"),
        # Mapping the record onto the mount stats it, so it waits until the
        # sweep reads `path` off the event loop.
        resolve_path=lambda: romm_client.local_path(rom),
        record=rom,
    )


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
        the "keep the ROM id" switch is on (#390, #391); every other opt-in
        one is off. Reads the settings in memory."""
        setting = {
            ES_FRONTEND_REFERRER: "es_frontend",
            SWAP_REFERRER: "swap_keep_rom_id",
        }.get(referrer_id)
        return setting is not None and bool(romm_settings.effective().get(setting))

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
        if not (
            bool(cfg.get("repin_enabled", True))
            and romm_repin.mode_needs_repin(spec.output_ext)
        ):
            summary["repins_recorded"] = 0
            return
        repin_count = 0
        for item in queued:
            rom = item.image.record
            ids = romm_repin.metadata_ids(rom) if rom else {}
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
                    romm_repin.record, rom, item.destination, ids,
                    rule["mode"], item.pre, item.source,
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
