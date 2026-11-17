"""Formats RomM re-identifies by itself, so their re-pin is skipped (#393).

Spike #382 (``docs/ROMM_INTEGRATION_ROADMAP.md``, "3.0 spike findings") found
that RomM 5.4.1, with title-id extraction on, moves a missing ROM onto its
converted file by title id at its next library scan, keeping the ROM id and
every piece of user data, for some (Platform, output format) pairs and not
others: sigil reads no title id from the rest. :data:`REIDENTIFIED` is that
finding as data, keyed by RomM version. A new RomM version that behaves
differently gets its own entry; one with no entry gets today's re-pin.

It matters only with the swap switch off. With it on, the swap
(``services.romm.swap_referrer``) keeps the id for every single-file format,
and this Referrer is not on (``RommLibrary.referrer_enabled``). With it off,
the re-pin row is still written before the conversion (the provider ids are
readable only then, ``services.romm.repin``), and the skip is decided here, at
the delete step, the one place that knows the Source is about to go: RomM
re-identifies only when it never sees the output beside the Source. The
Referrer's change tells the job and the Ledger why (``Applied.note``), and
:func:`note_job_finished` closes the row only once the job has completed with
its Source deleted. Anything less leaves the row pending, so the re-pin runs
as today: an unknown version, a heartbeat that can't be read or doesn't say
title-id extraction is on, a format not on the list, a mode whose output the
registry doesn't know, a Source that is kept.

The format is the output's extension, taken only when the registry says the
row's mode writes it (``registry.mode_output_extensions``), never from the
tool's identity. The Platform is RomM's own slug, which the re-pin row
carries from the source ROM.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from models import JobStatus
from services.referrers import Applied, Phase, Referrer, ReferrerChange, registry
from services.romm.client import heartbeat_version, romm_client
from services.tools import registry as tools

logger = get_logger("romm_reidentify")

# The Referrer's id: what lands in the Ledger's referrers_updated.
REIDENTIFY_REFERRER = "romm_reidentify"

# RomM version -> the (RomM Platform slug, output extension) pairs it moves a
# missing ROM onto by title id, keeping its id. From spike #382; not listed
# for 5.4.1 (no title id read): PS2 CSO and ZSO, PSP ZSO, GameCube WIA and GCZ.
REIDENTIFIED: Mapping[str, frozenset[tuple[str, str]]] = MappingProxyType({
    "5.4.1": frozenset({
        ("ps2", ".chd"),
        ("psp", ".chd"),
        ("psp", ".cso"),
        ("ngc", ".rvz"),
    }),
})


def _title_ids_on(heartbeat: Mapping[str, Any]) -> bool:
    """Whether the heartbeat says RomM reads title ids (sigil). Only an
    explicit ``True`` counts."""
    filesystem = heartbeat.get("FILESYSTEM")
    return isinstance(filesystem, dict) and filesystem.get("TITLE_ID_EXTRACTION_ENABLED") is True


def _output_format(mode: str | None, output_path: str) -> str | None:
    """The output's extension, when the registry says *mode* writes it."""
    if not mode:
        return None
    ext = os.path.splitext(output_path)[1].lower()
    try:
        written = tools.mode_output_extensions(mode)
    except KeyError:
        return None
    return ext if ext in written else None


def reason(
    heartbeat: Mapping[str, Any], platform_slug: str | None, mode: str | None,
    output_path: str,
) -> str | None:
    """Why RomM needs no re-pin for this conversion, or None (re-pin as
    today). *heartbeat* is RomM's ``GET /api/heartbeat``; *platform_slug* is
    RomM's slug for the source ROM's Platform; *mode* is the conversion's."""
    raw = heartbeat_version(dict(heartbeat)) if isinstance(heartbeat, Mapping) else None
    version = str(raw).strip().removeprefix("v") if raw else ""
    pairs = REIDENTIFIED.get(version)
    if not pairs or not platform_slug or not _title_ids_on(heartbeat):
        return None
    fmt = _output_format(mode, output_path)
    if fmt is None or (platform_slug, fmt) not in pairs:
        return None
    return (
        f"RomM {version} re-identifies {platform_slug.upper()} {fmt[1:].upper()} "
        "by its title id at its next library scan, so no metadata re-pin is needed"
    )


def _one_beside(renames: Mapping[str, str]) -> tuple[str, str] | None:
    """``(source, output)`` when *renames* is one file replaced by one file
    in the same folder, the case the spike ran."""
    if len(renames) != 1:
        return None
    ((source, output),) = renames.items()
    if os.path.dirname(os.path.abspath(source)) != os.path.dirname(os.path.abspath(output)):
        return None
    return source, output


class RommReidentifyReferrer(Referrer):
    """RomM's ROM, when RomM keeps it by itself: no change to RomM, only the
    re-pin it no longer needs (see the module docstring). Never fails: a
    question it can't answer means the re-pin runs as today."""

    id = REIDENTIFY_REFERRER
    label = "RomM"
    opt_in = True
    phase = Phase.NETWORK

    def detect(self, image_path: str) -> bool:
        return True

    def plan(self, renames: Mapping[str, str]) -> list[ReferrerChange]:
        pair = _one_beside(renames)
        if pair is None:
            return []
        source, output = pair
        # Imported here: repin imports the job manager, which imports the hook.
        from services.romm import repin

        try:
            row = repin.pending_for(output, source)
            if row is None:
                return []
            why = reason(romm_client.heartbeat(), row.platform_slug, row.mode, output)
        except Exception:
            # Not knowing keeps today's re-pin, and the conversion goes on.
            logger.warning(
                "romm_reidentify: couldn't tell whether RomM re-identifies %s; "
                "the re-pin stays queued", output, exc_info=True,
            )
            return []
        if why is None:
            return []
        return [ReferrerChange(
            referrer=self.id,
            target=f"RomM ROM {row.rom_id}",
            edits=(),
            payload=why,
            summary=why,
            record={"rom_id": row.rom_id, "repin_id": row.row_id},
        )]

    def apply(self, change: ReferrerChange) -> Applied:
        # Nothing to write: the row is closed once the Source is gone
        # (`note_job_finished`).
        return Applied(note=change.payload)


async def note_job_finished(job) -> None:
    """Close the re-pins this Referrer skipped, once *job* completed with its
    Source deleted. Terminal listener (``libraries.attach``). A job that
    failed, was cancelled or kept its Source leaves them pending."""
    if getattr(job, "status", None) != JobStatus.COMPLETED:
        return
    if not getattr(job, "deleted_paths", None):
        return  # the Source was kept: RomM sees both files, so the re-pin is owed
    skipped = [
        (record["repin_id"], record.get("note"))
        for record in getattr(job, "referrers_updated", None) or []
        if record.get("referrer") == REIDENTIFY_REFERRER
        and record.get("status") == "updated" and record.get("repin_id")
    ]
    if not skipped:
        return
    from services.romm import repin

    for row_id, note in skipped:
        try:
            await run_in_threadpool(repin.settle, int(row_id), "done", note)
        except Exception:
            # Left pending: the re-pin runs as today, which changes nothing.
            logger.warning(
                "romm_reidentify: could not close re-pin %s", row_id, exc_info=True,
            )


registry.register(RommReidentifyReferrer())
