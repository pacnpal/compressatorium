"""Every DAT's match is kept, DATs have families, and a ranking picks (#424)."""

import pytest
from services import hasheous
from services.dat_parser import dat_family
from services.dat_store import DATStore

from app.routes import dat as dat_routes

SHA1 = "ab" * 20


def _dat(name: str, game: str, *, homepage: str = "", sha1: str = SHA1) -> str:
    return f"""<?xml version="1.0"?>
<datafile>
  <header><name>{name}</name><description>{name}</description>
    <homepage>{homepage}</homepage></header>
  <game name="{game}"><rom name="{game}.bin" size="4" sha1="{sha1}"/></game>
</datafile>"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = DATStore(store_path=str(tmp_path / "dats.db"))
    monkeypatch.setattr(dat_routes, "dat_store", s)
    return s


async def _import(store: DATStore, *args, **kwargs) -> str:
    return (await store.import_dat(_dat(*args, **kwargs)))["id"]


@pytest.mark.parametrize(("texts", "family"), [
    (("Sony - PlayStation", "redump.org"), "redump"),
    (("Nintendo - Game Boy", "No-Intro"), "no-intro"),
    (("Commodore Amiga - Games (TOSEC-v2024)",), "tosec"),
    (("MAMERedump",), "mame"),
    (("NoIntros",), "no-intro"),
    (("My own set",), "other"),
    ((None,), "other"),
])
def test_family_is_read_from_header_text(texts, family):
    assert dat_family(*texts) == family


@pytest.mark.asyncio
async def test_two_dats_sharing_a_hash_keep_both_matches(store):
    redump = await _import(store, "Sony - PlayStation", "Disc Name", homepage="redump.org")
    nointro = await _import(store, "Sony - PlayStation", "Cart Name", homepage="No-Intro")
    families = {d["id"]: d["family"] for d in store.list_dats()}
    assert families == {redump: "redump", nointro: "no-intro"}

    # Redump first for a disc, No-Intro first for a cartridge, by default.
    disc = store.lookup_sha1(SHA1, disc=True)
    cart = store.lookup_sha1(SHA1, disc=False)
    assert (disc["dat_id"], disc["game_name"], disc["family"]) == (redump, "Disc Name", "redump")
    assert (cart["dat_id"], cart["game_name"]) == (nointro, "Cart Name")
    assert "conflict" not in disc and "conflict" not in cart

    # Deleting one DAT leaves the other's match standing.
    await store.delete_dat(nointro)
    assert store.lookup_sha1(SHA1)["dat_id"] == redump


@pytest.mark.asyncio
async def test_unordered_dats_that_disagree_report_a_conflict_until_ranked(store):
    first = await _import(store, "Set A", "Name A")
    second = await _import(store, "Set B", "Name B")
    record = store.lookup_sha1(SHA1)
    assert {c["dat_id"] for c in record["conflict"]} == {first, second}
    assert {c["game_name"] for c in record["conflict"]} == {"Name A", "Name B"}
    ranking = store.get_ranking()["cartridge"]
    assert [r["rank"] for r in ranking] == [1, 1]

    store.set_ranking({"cartridge": [first, second, "gone"]})
    record = store.lookup_sha1(SHA1)
    assert record["game_name"] == "Name A" and "conflict" not in record
    assert [r["id"] for r in store.get_ranking()["cartridge"]] == [first, second]
    # The disc ranking is separate and still unordered.
    assert "conflict" in store.lookup_sha1(SHA1, disc=True)


@pytest.mark.asyncio
async def test_same_name_in_two_unordered_dats_is_no_conflict(store):
    await _import(store, "Set A", "Same")
    await _import(store, "Set B", "Same")
    assert "conflict" not in store.lookup_sha1(SHA1)


@pytest.mark.asyncio
async def test_match_api_ranks_by_disc_extension_and_reorder_reranks_the_cache(
    store, tmp_path, monkeypatch,
):
    redump = await _import(store, "PS", "Disc Name", homepage="redump.org")
    nointro = await _import(store, "PS", "Cart Name", homepage="No-Intro")
    iso = str(tmp_path / "game.iso")
    rom = str(tmp_path / "game.gba")
    assert dat_routes.is_disc_image(iso) and not dat_routes.is_disc_image(rom)

    candidates = [(SHA1, "file_sha1")]
    disc = await dat_routes._local_lookup_match(iso, candidates)
    cart = await dat_routes._local_lookup_match(rom, candidates)
    assert disc["game_name"] == "Disc Name" and disc["source"] == "dat"
    assert cart["game_name"] == "Cart Name"

    await store.set_match(iso, disc)
    await dat_routes.put_dat_ranking(
        dat_routes.DATRankingRequest(disc=[nointro, redump]),
    )
    cached = store.get_match(iso)
    assert (cached["dat_id"], cached["game_name"]) == (nointro, "Cart Name")


def test_hasheous_hit_takes_its_signature_source_as_family():
    record = hasheous._normalize({
        "name": "Game",
        "signature": {"rom": {"signatureSource": "Redump", "name": "Game.cue"}},
    })
    assert record["family"] == "redump"
