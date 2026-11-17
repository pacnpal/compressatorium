"""DAT families and the ranking that decides which DAT names an Image (#424).

A hash can be known to several imported DATs. The DAT store keeps every one of
those matches; this module decides which of them names the Image:

* every DAT has a **family** (:data:`FAMILIES`), read from its header or, for a
  Hasheous hit, from the hit's signature source;
* the user's **ranking** is one ordered list of DATs (``dats.rank``). A DAT the
  user has not placed falls back to its family's place in
  :data:`DEFAULT_FAMILY_ORDER`, which differs for disc and cartridge Images:
  Redump first for discs, No-Intro first for cartridges. Whether an Image is a
  disc comes from the tool registry's disc extensions;
* the best-ranked match names the Image. When the best rank is shared by
  matches that give different names, nothing names it: that is a
  **conflict**, and it lists every disagreeing match.

Pure functions only; :class:`services.dat_store.DATStore` reads the ranks and
calls them, and ``DATStore.ranked_match_for_image`` is the one shared answer
callers use.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from services.tools import registry
from utils.path_utils import match_extension

FAMILIES: tuple[str, ...] = ("Redump", "No-Intro", "TOSEC", "MAME", "other")

DISC = "disc"
CARTRIDGE = "cartridge"

#: Where an unranked DAT sits, by family, for each kind of Image.
DEFAULT_FAMILY_ORDER: dict[str, tuple[str, ...]] = {
    DISC: ("Redump", "No-Intro", "TOSEC", "MAME", "other"),
    CARTRIDGE: ("No-Intro", "Redump", "TOSEC", "MAME", "other"),
}

# Checked in this order: a "MAME Redump" set is a Redump disc set first.
_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Redump", re.compile(r"redump", re.IGNORECASE)),
    ("No-Intro", re.compile(r"no[-_ ]?intro", re.IGNORECASE)),
    ("TOSEC", re.compile(r"tosec", re.IGNORECASE)),
    ("MAME", re.compile(r"\bmame", re.IGNORECASE)),
)

#: The fields of a match record that say which DAT gave which name.
_RECORD_KEYS = ("dat_id", "dat_name", "family", "game_name", "rom_name")
#: Which of the Image's hashes the record matched, and in which domain (a CHD
#: offers ``chd_sha1`` and ``chd_data_sha1``, and different DATs can know
#: different ones). Carried only when known, so the winner's hash becomes the
#: payload's ``file_hash``/``match_type`` and a freshness check compares
#: within the right domain.
_HASH_KEYS = ("file_hash", "match_type")


def dat_family(*texts: str | None) -> str:
    """The family a DAT belongs to, from any of its header texts.

    Takes the header's name, description, homepage, url and author (a No-Intro
    and a Redump DAT share the "Company - System" name shape, so the name alone
    often cannot tell), or a Hasheous signature source such as ``NoIntros``.
    """
    joined = " ".join(t for t in texts if t)
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(joined):
            return family
    return "other"


def image_kind(path: str) -> str:
    """:data:`DISC` when the tool registry calls *path*'s type a disc, else cartridge."""
    if match_extension(path, registry.disc_extensions()) is not None:
        return DISC
    return CARTRIDGE


@dataclass(frozen=True)
class RankedMatch:
    """Which DAT names an Image.

    ``top`` is the record that names it, or ``None`` when nothing does: either
    no DAT matched, or the best rank is shared by matches that disagree, in
    which case ``conflict`` holds each of them. ``matches`` is every match,
    best first.
    """

    top: dict | None
    conflict: tuple[dict, ...] = ()
    matches: tuple[dict, ...] = field(default_factory=tuple)


def _sort_key(
    record: Mapping, kind: str, ranks: Mapping[str, tuple[int | None, str]],
) -> tuple[int, int]:
    rank, family = ranks.get(record.get("dat_id") or "", (None, record.get("family")))
    if rank is not None:
        return (0, rank)
    order = DEFAULT_FAMILY_ORDER[kind]
    family = family if family in order else "other"
    return (1, order.index(family))


def rank_matches(
    matches: Iterable[Mapping],
    *,
    kind: str,
    ranks: Mapping[str, tuple[int | None, str]],
) -> RankedMatch:
    """Order *matches* by the ranking and say which one names the Image.

    ``ranks`` maps each existing DAT id to ``(rank, family)``; a record whose
    local DAT is no longer in it is dropped (its DAT was deleted). A record
    with no ``dat_id`` (a Hasheous hit) is kept and ranked by its family.
    """
    kept = [
        dict(m) for m in matches
        if not m.get("dat_id") or m.get("dat_id") in ranks
    ]
    if not kept:
        return RankedMatch(top=None)
    # Stable: equal keys keep the store's own order (oldest import first).
    kept.sort(key=lambda m: _sort_key(m, kind, ranks))
    best = _sort_key(kept[0], kind, ranks)
    tied = [m for m in kept if _sort_key(m, kind, ranks) == best]
    if len({m.get("game_name") for m in tied}) > 1:
        return RankedMatch(top=None, conflict=tuple(tied), matches=tuple(kept))
    return RankedMatch(top=kept[0], matches=tuple(kept))


def record_of(match: Mapping) -> dict:
    """The DAT-identity part of a match record (what ``matches`` lists)."""
    record = {key: match.get(key) for key in _RECORD_KEYS}
    record.update({key: match[key] for key in _HASH_KEYS if match.get(key)})
    return record


def apply_ranking(
    payload: dict, *, kind: str, ranks: Mapping[str, tuple[int | None, str]],
) -> dict:
    """*payload* (a match result) with the current ranking applied.

    Only a local DAT hit carrying ``matches`` is re-ranked; any other payload
    (a miss, a Hasheous hit, a row cached before #424) is returned unchanged.
    The top-level identity fields name the winner; on a conflict they name the
    first of the tied matches (so an existing badge still reads something) and
    ``conflict`` lists them all. Callers that act on the name must use
    :func:`ranked_match` rather than the top-level fields.
    """
    if not payload.get("matched") or not payload.get("matches"):
        return payload
    ranked = rank_matches(payload["matches"], kind=kind, ranks=ranks)
    if not ranked.matches:
        return payload
    out = dict(payload)
    out["matches"] = [record_of(m) for m in ranked.matches]
    shown = ranked.top or ranked.conflict[0]
    out.update(record_of(shown))
    if ranked.conflict:
        out["conflict"] = [record_of(m) for m in ranked.conflict]
    else:
        out.pop("conflict", None)
    return out


def ranked_match(payload: dict | None) -> RankedMatch:
    """Read the answer back out of a payload :func:`apply_ranking` produced."""
    if not payload or not payload.get("matched"):
        return RankedMatch(top=None)
    matches = tuple(payload.get("matches") or (record_of(payload),))
    if payload.get("conflict"):
        return RankedMatch(top=None, conflict=tuple(payload["conflict"]), matches=matches)
    return RankedMatch(top=record_of(payload), matches=matches)
