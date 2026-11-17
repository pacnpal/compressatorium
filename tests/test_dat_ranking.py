"""The DAT store keeps every DAT's match, with DAT families and a ranking (#424).

Driven the way the app is: DATs imported through ``/dat/import``, files matched
through ``/dat/match`` and ``/dat/match-batch``, the ranking set through
``/dat/ranking``. Hashing is stubbed (``compute_file_sha1``) so a tiny file can
stand in for any Image.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.routes import dat as dat_routes
from services import hasheous

SHARED_SHA1 = "ab" * 20


def _dat_xml(name: str, game: str, *, homepage: str = "", sha1: str = SHARED_SHA1) -> str:
    home = f"<homepage>{homepage}</homepage>" if homepage else ""
    return f"""<?xml version="1.0"?>
<datafile>
  <header><name>{name}</name><description>{name}</description>{home}</header>
  <game name="{game}">
    <rom name="{game}.bin" size="4" sha1="{sha1}"/>
  </game>
</datafile>
"""


def _upload(content: str, filename: str = "x.dat"):
    encoded = content.encode("utf-8")
    upload = MagicMock()
    upload.filename = filename
    upload.read = AsyncMock(side_effect=[encoded, b""])
    return upload


@pytest.fixture
def store(tmp_path, monkeypatch):
    from services.dat_store import DATStore
    s = DATStore(store_path=str(tmp_path / "dat.db"))
    monkeypatch.setattr(dat_routes, "dat_store", s)
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda _p: True)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1", AsyncMock(return_value=SHARED_SHA1),
    )
    return s


async def _import(xml: str) -> dict:
    return await dat_routes.import_dat(file=_upload(xml))


async def _match(path) -> dict:
    return await dat_routes.match_file(dat_routes.MatchRequest(path=str(path)))


def _file(tmp_path, name: str):
    target = tmp_path / name
    target.write_bytes(b"data")
    return target


# ---------------------------------------------------------------------------
# Every DAT's match is kept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_dats_sharing_a_hash_both_keep_their_match(store):
    first = await _import(_dat_xml("Set A", "Game A"))
    second = await _import(_dat_xml("Set B", "Game B"))

    record = store.lookup_sha1(SHARED_SHA1)
    by_dat = {m["dat_id"]: m["game_name"] for m in record["matches"]}
    assert by_dat == {first["id"]: "Game A", second["id"]: "Game B"}

    # Deleting the later import leaves the earlier one's match in place.
    await dat_routes.delete_dat(second["id"])
    record = store.lookup_sha1(SHARED_SHA1)
    assert [m["game_name"] for m in record["matches"]] == ["Game A"]


# ---------------------------------------------------------------------------
# DAT families
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_imported_dat_gets_a_family_from_its_header(store):
    await _import(_dat_xml("Sony - PlayStation", "G1", homepage="redump.org"))
    await _import(_dat_xml("Nintendo - Game Boy", "G2", homepage="No-Intro"))
    await _import(_dat_xml("Commodore Amiga - Games (TOSEC-v2023)", "G3"))
    await _import(_dat_xml("MAME 0.260", "G4"))
    await _import(_dat_xml("My own set", "G5"))

    families = {d["name"]: d["family"] for d in store.list_dats()}
    assert families == {
        "Sony - PlayStation": "Redump",
        "Nintendo - Game Boy": "No-Intro",
        "Commodore Amiga - Games (TOSEC-v2023)": "TOSEC",
        "MAME 0.260": "MAME",
        "My own set": "other",
    }


def test_a_hasheous_hit_takes_its_family_from_the_signature_source():
    def hit(source):
        return hasheous._normalize(
            {"id": 1, "name": "G", "signature": {"rom": {"signatureSource": source}}},
        )

    assert hit("NoIntros")["family"] == "No-Intro"
    assert hit("Redump")["family"] == "Redump"
    assert hit("TOSEC")["family"] == "TOSEC"
    assert hit("MAMEArcade")["family"] == "MAME"
    assert hit("RetroAchievements")["family"] == "other"


# ---------------------------------------------------------------------------
# Default ranking: Redump first for discs, No-Intro first for cartridges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_ranking_names_a_disc_from_redump_and_a_cartridge_from_no_intro(
    store, tmp_path,
):
    await _import(_dat_xml("Nintendo - Game Boy", "Cart Name", homepage="No-Intro"))
    await _import(_dat_xml("Sony - PlayStation", "Disc Name", homepage="redump.org"))

    disc = await _match(_file(tmp_path, "game.iso"))
    cart = await _match(_file(tmp_path, "game.gb"))

    assert (disc["game_name"], disc["family"]) == ("Disc Name", "Redump")
    assert (cart["game_name"], cart["family"]) == ("Cart Name", "No-Intro")
    assert "conflict" not in disc and "conflict" not in cart
    # Every match is still listed, best first.
    assert [m["game_name"] for m in disc["matches"]] == ["Disc Name", "Cart Name"]


@pytest.mark.asyncio
async def test_disc_or_cartridge_comes_from_the_tool_registry(store, tmp_path, monkeypatch):
    await _import(_dat_xml("Nintendo - Game Boy", "Cart Name", homepage="No-Intro"))
    await _import(_dat_xml("Sony - PlayStation", "Disc Name", homepage="redump.org"))
    monkeypatch.setattr(dat_routes.registry, "disc_extensions", lambda: (".gb",))

    assert (await _match(_file(tmp_path, "game.gb")))["game_name"] == "Disc Name"
    assert (await _match(_file(tmp_path, "game.iso")))["game_name"] == "Cart Name"


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_unranked_dats_that_disagree_report_a_conflict_naming_both(store, tmp_path):
    a = await _import(_dat_xml("Set A", "Game A"))
    b = await _import(_dat_xml("Set B", "Game B"))
    path = _file(tmp_path, "game.gb")

    result = await _match(path)

    assert result["matched"] is True
    assert {c["dat_id"] for c in result["conflict"]} == {a["id"], b["id"]}
    assert {c["dat_name"] for c in result["conflict"]} == {"Set A", "Set B"}

    # The shared answer for an Image, read from the match cache.
    await dat_routes.match_batch(dat_routes.MatchBatchRequest(paths=[str(path)]))
    ranked = store.ranked_match_for_image(os.path.realpath(path))
    assert ranked.top is None
    assert {c["game_name"] for c in ranked.conflict} == {"Game A", "Game B"}


@pytest.mark.asyncio
async def test_equally_ranked_dats_that_agree_on_the_name_are_no_conflict(store, tmp_path):
    await _import(_dat_xml("Set A", "Same Game"))
    await _import(_dat_xml("Set B", "Same Game"))

    result = await _match(_file(tmp_path, "game.gb"))

    assert result["game_name"] == "Same Game"
    assert "conflict" not in result


# ---------------------------------------------------------------------------
# The user's ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_users_ranking_decides_and_persists(store, tmp_path):
    a = await _import(_dat_xml("Set A", "Game A"))
    b = await _import(_dat_xml("Set B", "Game B"))
    path = _file(tmp_path, "game.gb")
    # Cached before the ranking exists: it must be re-ranked when read.
    await dat_routes.match_batch(dat_routes.MatchBatchRequest(paths=[str(path)]))

    saved = await dat_routes.put_dat_ranking(
        dat_routes.DatRankingRequest(order=[b["id"], a["id"]]),
    )
    assert [d["id"] for d in saved["dats"]] == [b["id"], a["id"]]
    assert saved["custom"] is True

    hashed = dat_routes.compute_file_sha1
    hashed.reset_mock()
    cached = await dat_routes.match_batch(dat_routes.MatchBatchRequest(paths=[str(path)]))
    hashed.assert_not_called()
    result = cached["results"][str(path)]
    assert (result["game_name"], result["dat_id"]) == ("Game B", b["id"])
    assert "conflict" not in result
    assert store.ranked_match_for_image(os.path.realpath(path)).top["game_name"] == "Game B"

    # Persisted: a fresh store on the same database reads the same order.
    from services.dat_store import DATStore
    reopened = DATStore(session_factory=store._session_factory)
    assert [d["id"] for d in reopened.get_ranking()["dats"]] == [b["id"], a["id"]]
    assert (await dat_routes.get_dat_ranking())["custom"] is True

    # Reset goes back to the family defaults, so the tie is a conflict again.
    await dat_routes.delete_dat_ranking()
    assert (await _match(path)).get("conflict")


@pytest.mark.asyncio
async def test_a_ranking_naming_an_unknown_or_repeated_dat_is_refused(store):
    a = await _import(_dat_xml("Set A", "Game A"))

    for order in ([a["id"], "nope"], [a["id"], a["id"]]):
        with pytest.raises(HTTPException) as exc:
            await dat_routes.put_dat_ranking(dat_routes.DatRankingRequest(order=order))
        assert exc.value.status_code == 400
    assert (await dat_routes.get_dat_ranking())["custom"] is False


@pytest.mark.asyncio
async def test_a_reimported_dat_keeps_its_place_in_the_ranking(store):
    """The MAMERedump sync replaces each DAT with a fresh id; the rank follows
    the set (its name and family), not the id."""
    a = await _import(_dat_xml("Set A", "Game A"))
    b = await _import(_dat_xml("Set B", "Game B"))
    await dat_routes.put_dat_ranking(dat_routes.DatRankingRequest(order=[b["id"], a["id"]]))

    b2 = await _import(_dat_xml("Set B", "Game B"))
    await dat_routes.delete_dat(b["id"])

    assert [d["id"] for d in (await dat_routes.get_dat_ranking())["dats"]] == [
        b2["id"], a["id"],
    ]
