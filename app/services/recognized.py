"""The Recognized proof (#423): is the output a dump some DAT or Hasheous knows?

The delete proof of a mode whose output can't be Round-tripped against its
Source (``DeleteProof.RECOGNIZED``: PSP PBP -> ISO, 3DS CIA <-> CCI). Shared
by every such mode: the job pipeline asks it once the output's container check
has passed, and a retried delete asks it again. It hashes the output (SHA-1,
streamed on a detached thread that a cancel stops, under the caller's bound),
looks the hash up in the local DATs, then in Hasheous when that is switched
on, and passes only on a hit.

A DAT that came over plain HTTP (the Redump sync's fallback, #557) never
vouches: it could have been swapped on the way. It still
names and matches files everywhere else; here its entries are set aside, so
an output only it knows is a miss that says so, and the Source stays. A DAT
imported by hand, fetched over HTTPS, or recorded before the transport was
(NULL, migration 0028) vouches as before.

The outcome never deletes anything. A miss, a lookup that couldn't run or a
hash that didn't finish all keep the Source, and the job still completes,
saying "converted, source kept: output not Recognized".
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services import hasheous
from services.dat_store import PLAIN_HTTP, dat_store
from services.hasheous import HasheousUnavailable
from services.stream_digest import hash_files
from services.subprocess_runner import ReadCancelled
from sqlalchemy.exc import SQLAlchemyError

logger = get_logger("recognized")

# What a job whose output was not Recognized says, word for word (#420).
NOT_RECOGNIZED = "converted, source kept: output not Recognized"


@dataclass(frozen=True)
class RecognizedOutcome:
    """``passed`` the output's SHA-1 is in a DAT (``dat_name``) or Hasheous;
    ``cancelled`` Cancel was pressed, so nothing was proven; otherwise
    ``message`` says why the Source stays."""

    passed: bool
    dat_name: str | None = None
    game_name: str | None = None
    source: str | None = None  # "dat" or "hasheous"
    cancelled: bool = False
    # The hash or a lookup did not finish: nothing was judged.
    timed_out: bool = False
    message: str = ""


def _kept(why: str = "", *, timed_out: bool = False) -> RecognizedOutcome:
    message = f"{NOT_RECOGNIZED} ({why})" if why else NOT_RECOGNIZED
    return RecognizedOutcome(passed=False, message=message, timed_out=timed_out)


def _may_vouch(match: dict) -> bool:
    """Whether a DAT match may prove a delete: never one that came over plain
    HTTP, nor a Redump DAT whose transport was never recorded, which may have
    come over the fallback (migration 0028 marks those
    ``http`` already, this holds whatever wrote the row)."""
    transport = match.get("transport")
    if transport == PLAIN_HTTP:
        return False
    return transport is not None or match.get("family") != "Redump"


def _local_hit(sha1: str, path: str) -> tuple[dict | None, list[str]]:
    """The top-ranked local DAT record for *sha1* that may vouch for a delete,
    or None, and the names of the DATs set aside because they came over plain
    HTTP. Blocking."""
    record = dat_store.lookup_sha1(sha1)
    if not record:
        return None, []
    matches = record.get("matches") or [record]
    trusted = [m for m in matches if _may_vouch(m)]
    untrusted = sorted({m.get("dat_name") or "?" for m in matches if m not in trusted})
    if not trusted:
        return None, untrusted
    # Ranked as everywhere else (DESIGN 3.3.5.2), so the Ledger names the DAT
    # the user put first. Any DAT knowing the hash Recognizes it; a ranking
    # conflict only changes which name is shown.
    ranked = dat_store.with_current_ranking(
        {"matched": True, **record, **trusted[0], "matches": trusted}, path,
    )
    return ranked or None, untrusted


def _over_http(names: list[str]) -> str:
    """Why a match only untrusted DATs make keeps the Source."""
    return (f"only {', '.join(names)} knows it, and that came or may have come "
            "over plain HTTP, so it can't vouch for a delete")


async def recognize(
    path: str,
    *,
    cancel_event: asyncio.Event | None = None,
    timeout: float | None = None,
) -> RecognizedOutcome:
    """Hash *path* and look it up: local DATs first, then Hasheous.

    *timeout* bounds the hash, as the output's verify is bounded (None or 0:
    no bound). Hasheous bounds its own requests and backs off while it is
    unreachable.
    """
    name = os.path.basename(path)

    def _is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    try:
        sha1 = await asyncio.wait_for(
            hash_files([path], algorithm="sha1", cancel_event=cancel_event),
            timeout=timeout or None,
        )
    except ReadCancelled:
        return RecognizedOutcome(passed=False, cancelled=True)
    except asyncio.TimeoutError:
        return _kept(f"hashing {name} did not finish in time", timed_out=True)
    except OSError as exc:
        logger.warning("Could not hash %s for the Recognized proof: %s", path, exc)
        return _kept(f"could not hash {name}: {exc}")
    if _is_cancelled():
        return RecognizedOutcome(passed=False, cancelled=True)

    try:
        local, over_http = await run_in_threadpool(_local_hit, sha1, path)
    except (SQLAlchemyError, RuntimeError, OSError) as exc:
        # DATs that could not be read prove nothing: the Source stays.
        logger.warning(
            "Could not read the DATs for the Recognized proof of %s", path, exc_info=True,
        )
        return _kept(f"the DATs could not be read: {exc}")
    if local is not None:
        return RecognizedOutcome(
            passed=True, dat_name=local.get("dat_name"),
            game_name=local.get("game_name"), source="dat",
        )
    if _is_cancelled():
        return RecognizedOutcome(passed=False, cancelled=True)
    # Only plain-HTTP DATs know it: as if no DAT did, Hasheous may still vouch.
    why = _over_http(over_http) if over_http else ""
    if not hasheous.enabled():
        return _kept(why)
    try:
        remote = await hasheous.lookup(sha1)
    except HasheousUnavailable as exc:
        logger.warning("Hasheous unavailable for the Recognized proof of %s: %s", path, exc)
        return _kept(f"Hasheous could not be reached: {exc}", timed_out=True)
    if _is_cancelled():
        return RecognizedOutcome(passed=False, cancelled=True)
    if remote is None:
        return _kept(why)
    return RecognizedOutcome(
        passed=True, dat_name=remote.get("dat_name") or "Hasheous",
        game_name=remote.get("game_name"), source="hasheous",
    )
