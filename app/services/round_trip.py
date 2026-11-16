"""The Round-trip check (#298): does the output hold the same content as its Source?

Shared by every tool: it asks the mode's tool for a content digest of the
Source and of the output (``ToolPlugin.content_digest``) and compares them. A
tool only supplies the digest; the bounding, the cancel handling and the
verdict live here, so no tool re-implements them.

The outcome never deletes anything itself. The job pipeline reads it and keeps
the Source on anything but a pass.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass

from logging_setup import get_logger
from services.tools import registry
from services.tools.base import ContentDigest, ContentDigestUnavailable, ToolPlugin

logger = get_logger("round_trip")


def round_trip_applies(mode: str, *, delete_on_verify: bool, container_only: bool) -> bool:
    """Whether a job with these settings runs the Round-trip before deleting.

    The one answer for every caller: the job pipeline's delete check, and what
    a Rule's Preview says will run (#302). Only a job that deletes its Source,
    in a mode whose tool has a digest, without the ``container_only`` opt-out.
    The capability is read from the registry, so a mode that gains a digest
    changes the answer everywhere at once.
    """
    return (
        delete_on_verify
        and not container_only
        and registry.mode_supports_round_trip(mode)
    )


@dataclass(frozen=True)
class RoundTripOutcome:
    """``passed`` the digests matched; ``cancelled`` Cancel was pressed, so
    nothing was proven either way; otherwise ``message`` says why the Source
    must be kept."""

    passed: bool
    cancelled: bool = False
    message: str = ""
    # A side did not finish within its verify bound: nothing was judged. The
    # job keeps the Source either way; a retried delete (#329) counts it as
    # an attempt rather than a failed check.
    timed_out: bool = False


class _DigestTimedOut(ContentDigestUnavailable):
    """A digest that did not finish within its verify bound."""


async def _digest(
    tool: ToolPlugin,
    path: str,
    cancel_event: asyncio.Event | None,
    within: Sequence[str] | None = None,
) -> ContentDigest:
    """One side's digest, under the same bound as a verify of that file."""
    try:
        bound = await tool.verify_timeout(path, cancel_event=cancel_event)
        digest = await asyncio.wait_for(
            tool.content_digest(path, cancel_event=cancel_event, within=within),
            timeout=bound or None,
        )
    except ContentDigestUnavailable:
        raise
    except asyncio.TimeoutError:
        raise _DigestTimedOut(
            f"hashing {os.path.basename(path)} did not finish in time",
        ) from None
    except Exception as e:  # any failure means "not proven"
        raise ContentDigestUnavailable(
            f"could not hash {os.path.basename(path)}: {e}",
        ) from e
    if digest is None:
        raise ContentDigestUnavailable(
            f"{tool.display_name} has no content digest for "
            f"{os.path.basename(path)}",
        )
    return digest


async def _source_problem(
    tool: ToolPlugin,
    path: str,
    cancel_event: asyncio.Event | None,
    within: Sequence[str] | None = None,
) -> str | None:
    """The tool's own check of the Source, under the same bound as a digest,
    with the same trusted root(s) the Source's digest gets. Returns the
    problem, and whether it is that the check did not finish in time."""
    try:
        bound = await tool.verify_timeout(path, cancel_event=cancel_event)
        problem = await asyncio.wait_for(
            tool.source_problem(path, cancel_event=cancel_event, within=within),
            timeout=bound or None,
        )
        return problem, False
    except asyncio.TimeoutError:
        return f"checking {os.path.basename(path)} did not finish in time", True
    except Exception as e:  # any failure means "not proven"
        logger.warning("Round-trip could not check the Source %s", path, exc_info=True)
        return f"could not check {os.path.basename(path)}: {e}", False


async def round_trip(
    tool: ToolPlugin,
    source_path: str,
    output_path: str,
    *,
    cancel_event: asyncio.Event | None = None,
    within: Sequence[str] | None = None,
) -> RoundTripOutcome:
    """Compare the content ``source_path`` and ``output_path`` decode to.

    ``source_path`` is the file the converter read (an extracted archive
    member, not the ``archive::member`` pseudo-path). Read one side at a time:
    both are usually on the same volume, and two full reads at once only
    contend for it.

    ``within`` is the job's trusted root(s) for the Source (its extraction
    folder for an archive member, ``None`` for the Volumes) and is passed to
    the Source side only. The output always lives in a Volume, so its side
    gets ``None``; a tool that extracts the output into scratch reads that
    scratch under its own ``within``.
    """
    cancelled = RoundTripOutcome(passed=False, cancelled=True)

    def _is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    problem, timed_out = await _source_problem(tool, source_path, cancel_event, within)
    if _is_cancelled():
        return cancelled
    if problem:
        logger.warning("Round-trip refused the Source %s: %s", source_path, problem)
        return RoundTripOutcome(
            passed=False, message=f"Round-trip failed: {problem}. Source kept.",
            timed_out=timed_out,
        )
    try:
        source = await _digest(tool, source_path, cancel_event, within)
        if _is_cancelled():
            return cancelled
        output = await _digest(tool, output_path, cancel_event)
    except ContentDigestUnavailable as e:
        if _is_cancelled():
            return cancelled
        logger.warning("Round-trip could not run for %s: %s", output_path, e)
        return RoundTripOutcome(
            passed=False, message=f"Round-trip could not run: {e}. Source kept.",
            timed_out=isinstance(e, _DigestTimedOut),
        )
    if _is_cancelled():
        return cancelled
    try:
        same = source.same_content(output)
    except ValueError as e:
        return RoundTripOutcome(
            passed=False, message=f"Round-trip could not compare: {e}. Source kept.",
        )
    if same:
        return RoundTripOutcome(passed=True)
    logger.warning(
        "Round-trip mismatch: %s %s=%s, output %s=%s",
        source_path, source.kind, source.value, output_path, output.value,
    )
    reason = output.mismatch_reason or source.mismatch_reason
    if reason:
        # The tool knows why its digests can differ (#301); say that rather
        # than a bare "content differs", which reads like damage.
        return RoundTripOutcome(passed=False, message=f"Round-trip failed: {reason}.")
    return RoundTripOutcome(
        passed=False,
        message=(
            "Round-trip failed: the output's content differs from the Source. "
            "Source kept."
        ),
    )
