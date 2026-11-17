"""Unidentified in RomM: the filter, the count and the Hasheous ids (#427).

A RomM Image is Recognized when RomM identified it or when the app's own DAT
or Hasheous match for its file is a hit. RomM's own verdict stays a separate
flag, so a Rule set to "only ROMs RomM could not identify" keeps its meaning
while "only Recognized" now includes the ROMs the app Recognizes.

The RomM ROM listing gains an ``unidentified`` filter, the app's cached match
per row and the counts. It reads only what the match cache already holds;
"Recognize these" is the existing match job. A per-Library switch, off by
default, writes the provider ids a Hasheous hit carries onto the ROM in RomM
after converting it, through the re-pin's metadata update, and only the ids
the ROM doesn't already have.

Everything goes through the HTTP API against :class:`tests.fake_romm.FakeRomm`,
which records the metadata writes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from services import db as _db

from config import settings as app_settings

from .fake_romm import FakeRomm

NGC = {"id": 7, "slug": "ngc", "fs_slug": "ngc", "name": "GameCube"}
KNOWN_ID, HOMEBREW_ID, MYSTERY_ID = 1, 2, 3

# What a Hasheous hit for Homebrew.iso normalises to: the provider ids under
# RomM's own field names.
HOMEBREW_IDS = {"igdb_id": 12296, "tgdb_id": 24326, "hasheous_id": 13633}

# A real Hasheous response, trimmed (tests/test_hasheous.py).
HASHEOUS_RESPONSE = {
    "id": 13633,
    "name": "Jumpman Junior",
    "platform": {"name": "Commodore 64"},
    "signature": {
        "game": {"name": "Jumpman Junior", "year": "1983"},
        "rom": {"name": "Jumpman Junior (1983)(Epyx).bin", "signatureSource": "TOSEC"},
    },
    "metadata": [
        {"source": "IGDB", "id": "12296",
         "link": "https://www.igdb.com/games/jumpman-junior", "status": "Mapped"},
        {"source": "TheGamesDb", "id": "24326",
         "link": "https://thegamesdb.net/game.php?id=24326", "status": "Mapped"},
        {"source": "RetroAchievements", "id": "", "link": "", "status": "NotMapped"},
        {"source": "GiantBomb", "id": "77", "link": "", "status": "NotMapped"},
    ],
}


def _app() -> FastAPI:
    from routes import dat, romm

    app = FastAPI()
    for router in (romm.router, dat.router):
        app.include_router(router, prefix="/api")
    return app


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    """A Volume holding RomM's library folder ``romm/ngc`` and a database."""
    from services.romm import settings as romm_settings

    vol = Path(os.path.realpath(tmp_path)) / "vol"
    (vol / "romm" / "ngc").mkdir(parents=True)
    monkeypatch.setattr(app_settings, "chd_volumes", str(vol))
    monkeypatch.setattr(app_settings, "data_dir", str(tmp_path / "config"))
    monkeypatch.setattr(app_settings, "hasheous_enabled", False)
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    yield vol
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _iso(vol: Path, name: str) -> str:
    path = vol / "romm" / "ngc" / name
    path.write_bytes(name.encode() * 64)
    return str(path)


@pytest_asyncio.fixture(name="romm")
async def _romm(vol: Path):
    """RomM holding three GameCube ROMs: Known (RomM identified it), Homebrew
    (RomM didn't; the app has a cached Hasheous hit) and Mystery (RomM didn't;
    the app has no match for it)."""
    from services.dat_store import dat_store
    from services.romm import settings as romm_settings

    fake = FakeRomm(version="5.4.1", platforms=[NGC], library=vol / "romm").start()
    for name in ("Known.iso", "Homebrew.iso", "Mystery.iso"):
        _iso(vol, name)
    fake.add_rom(KNOWN_ID, NGC, "Known.iso", igdb_id=11)
    fake.add_rom(HOMEBREW_ID, NGC, "Homebrew.iso")
    fake.add_rom(MYSTERY_ID, NGC, "Mystery.iso")
    await romm_settings.save({
        "url": fake.url, "token": "rmm_x", "library_root": str(vol / "romm"),
    })
    await dat_store.set_match(_path(vol, "Homebrew.iso"), _hasheous_hit(
        _path(vol, "Homebrew.iso"), "Homebrew Quest", HOMEBREW_IDS,
    ))
    yield fake
    fake.stop()


@pytest_asyncio.fixture(name="client")
async def _client(romm):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _path(vol: Path, name: str) -> str:
    return str(vol / "romm" / "ngc" / name)


def _hasheous_hit(path: str, game: str, ids: dict) -> dict:
    data = Path(path).read_bytes()
    return {
        "path": path, "matched": True, "match_type": "file_sha1",
        "file_hash": hashlib.sha1(data).hexdigest(), "source": "hasheous",
        "dat_id": None, "dat_name": "Redump", "family": "Redump",
        "game_name": game, "rom_name": Path(path).name, "provider_ids": dict(ids),
    }


async def _listing(client, **params) -> dict:
    reply = await client.get("/api/romm/roms", params={"platform_id": 7, **params})
    assert reply.status_code == 200, reply.text
    return reply.json()


def _by_name(listing: dict) -> dict[str, dict]:
    return {e["name"]: e for e in listing["entries"]}


# ---- Hasheous provider ids ----------------------------------------------------


def test_the_hasheous_normaliser_keeps_provider_ids_as_romm_fields() -> None:
    from services import hasheous
    from services.romm import METADATA_ID_FIELDS

    record = hasheous._normalize(HASHEOUS_RESPONSE)

    # Mapped entries only, under RomM's own field names; the record's own id
    # is RomM's hasheous_id.
    assert record["provider_ids"] == HOMEBREW_IDS
    assert set(hasheous.PROVIDER_ID_FIELDS.values()) <= set(METADATA_ID_FIELDS)
    # The links stay as they were.
    assert [link["source"] for link in record["metadata_links"]] == ["IGDB", "TheGamesDb"]


# ---- the listing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_listing_flags_romm_identification_and_the_apps_match(client, vol):
    listing = await _listing(client)

    rows = _by_name(listing)
    assert rows["Known.iso"]["romm_identified"] is True
    assert rows["Homebrew.iso"]["romm_identified"] is False
    assert rows["Homebrew.iso"]["dat_match"]["game_name"] == "Homebrew Quest"
    assert rows["Homebrew.iso"]["dat_match"]["matched"] is True
    assert rows["Mystery.iso"]["romm_identified"] is False
    assert rows["Mystery.iso"]["dat_match"] is None
    # Known (RomM) and Homebrew (the app) are Recognized; two RomM couldn't
    # identify, and the app Recognizes one of them.
    assert listing["romm"] == {
        "unidentified": 2, "recognized": 2, "recognized_unidentified": 1,
    }


@pytest.mark.asyncio
async def test_the_unidentified_filter_lists_only_what_romm_could_not_identify(client, vol):
    from routes import dat as dat_routes
    from services.dat_store import dat_store

    with patch.object(dat_routes, "schedule_match_job") as schedule:
        listing = await _listing(client, unidentified="true")

    assert sorted(_by_name(listing)) == ["Homebrew.iso", "Mystery.iso"]
    assert listing["romm"] == {
        "unidentified": 2, "recognized": 1, "recognized_unidentified": 1,
    }
    # It reads what the app already has and never starts hashing.
    schedule.assert_not_called()
    assert dat_store.get_match(_path(vol, "Mystery.iso")) is None


@pytest.mark.asyncio
async def test_recognize_these_starts_the_match_job_for_the_filtered_rows(client, vol):
    from services.dat_store import dat_store

    mystery = _path(vol, "Mystery.iso")
    sha1 = hashlib.sha1(Path(mystery).read_bytes()).hexdigest()
    await dat_store.import_dat(
        '<?xml version="1.0"?><datafile><header><name>Nintendo - GameCube</name>'
        "<description>Redump</description></header>"
        f'<game name="Mystery Game"><rom name="Mystery Game.iso" size="{Path(mystery).stat().st_size}"'
        f' sha1="{sha1}"/></game></datafile>',
    )
    rows = (await _listing(client, unidentified="true"))["entries"]

    started = await client.post(
        "/api/dat/match-batch/job", json={"paths": [r["path"] for r in rows]},
    )

    assert started.status_code == 200, started.text
    assert started.json()["status"] == "started"
    listing = await _listing(client, unidentified="true")
    assert _by_name(listing)["Mystery.iso"]["dat_match"]["game_name"] == "Mystery Game"
    assert listing["romm"]["recognized"] == 2


# ---- Rules ----------------------------------------------------------------------


async def _candidates(client, rule: dict) -> list[str]:
    saved = await client.put("/api/romm/rules", json={"rules": {"7": {
        "mode": "dolphin_rvz", "enabled": True, **rule,
    }}})
    assert saved.status_code == 200, saved.text
    preview = await client.post("/api/romm/auto-convert/preview", json={})
    assert preview.status_code == 200, preview.text
    (platform,) = preview.json()["platforms"]
    return sorted(platform.get("candidates") or [])


@pytest.mark.asyncio
async def test_only_recognized_includes_what_the_app_recognizes(client):
    assert await _candidates(client, {"only_matched": True}) == ["Homebrew.iso", "Known.iso"]


@pytest.mark.asyncio
async def test_only_unidentified_keeps_meaning_what_romm_could_not_identify(client):
    assert await _candidates(client, {"only_unmatched": True}) == [
        "Homebrew.iso", "Mystery.iso",
    ]


# ---- the Hasheous id write --------------------------------------------------------


async def _switch(client, on: bool) -> dict:
    reply = await client.put("/api/romm/settings", json={"hasheous_write_ids": on})
    assert reply.status_code == 200, reply.text
    return reply.json()


async def _plan(client, vol, name: str, mode: str = "dolphin_rvz") -> dict:
    reply = await client.post("/api/romm/repin/plan", json={
        "paths": [_path(vol, name)], "mode": mode, "platform_id": 7,
    })
    assert reply.status_code == 200, reply.text
    return reply.json()


def _romm_scans_output(romm: FakeRomm, vol: Path, name: str, rom_id: int, **fields) -> None:
    """The conversion wrote *name* and RomM's scan added it as ROM *rom_id*."""
    Path(_path(vol, name)).write_bytes(b"converted " + name.encode())
    romm.add_rom(rom_id, NGC, name, **fields)


async def _settle(client) -> dict:
    reply = await client.post("/api/romm/repin")
    assert reply.status_code == 200, reply.text
    return reply.json()


@pytest.mark.asyncio
async def test_the_switch_is_off_by_default(client):
    reply = await client.get("/api/romm/settings")
    assert reply.json()["hasheous_write_ids"] is False
    assert (await _switch(client, True))["hasheous_write_ids"] is True


@pytest.mark.asyncio
async def test_with_the_switch_off_nothing_is_written(client, romm, vol):
    planned = await _plan(client, vol, "Homebrew.iso")
    _romm_scans_output(romm, vol, "Homebrew.rvz", 40)
    await _settle(client)

    assert planned["recorded"] == 0
    assert romm.metadata_writes == []


@pytest.mark.asyncio
async def test_with_the_switch_on_a_conversion_writes_the_hasheous_ids(client, romm, vol):
    await _switch(client, True)

    planned = await _plan(client, vol, "Homebrew.iso")
    # RomM's scan of the output found a TheGamesDB id on its own.
    _romm_scans_output(romm, vol, "Homebrew.rvz", 40, tgdb_id=999)
    await _settle(client)

    assert planned["recorded"] == 1
    # Only the ids the ROM doesn't already have.
    assert romm.metadata_writes == [(40, {"igdb_id": "12296", "hasheous_id": "13633"})]
    assert romm.roms[40]["tgdb_id"] == 999


@pytest.mark.asyncio
async def test_ids_romm_already_has_are_never_replaced(client, romm, vol):
    """Known carries RomM's own igdb_id: the re-pin carries that one across,
    and Hasheous adds only what RomM had not set."""
    from services.dat_store import dat_store

    await dat_store.set_match(_path(vol, "Known.iso"), _hasheous_hit(
        _path(vol, "Known.iso"), "Known Game", HOMEBREW_IDS,
    ))
    await _switch(client, True)

    await _plan(client, vol, "Known.iso")
    _romm_scans_output(romm, vol, "Known.rvz", 41)
    await _settle(client)

    assert romm.metadata_writes == [
        (41, {"igdb_id": "11", "tgdb_id": "24326", "hasheous_id": "13633"}),
    ]


@pytest.mark.asyncio
async def test_a_dat_safe_output_still_gets_the_hasheous_ids(client, romm, vol):
    """RomM re-matches a CHD by its hash, but it never matched this ROM, so
    only the Hasheous ids can identify the output."""
    await _switch(client, True)

    planned = await _plan(client, vol, "Homebrew.iso", mode="createdvd")
    _romm_scans_output(romm, vol, "Homebrew.chd", 42)
    await _settle(client)

    assert planned["recorded"] == 1
    assert romm.metadata_writes == [
        (42, {"igdb_id": "12296", "tgdb_id": "24326", "hasheous_id": "13633"}),
    ]


@pytest.mark.asyncio
async def test_a_kept_rom_id_still_gets_the_hasheous_ids(client, romm, vol):
    """With the swap the ROM keeps its id: closing its re-pin as done must not
    drop the Hasheous ids still owed to it."""
    from services.romm import repin as romm_repin

    await _switch(client, True)
    await _plan(client, vol, "Homebrew.iso")
    output = _path(vol, "Homebrew.rvz")
    # The swap renamed ROM 2's file to the output; RomM rehashed it.
    Path(output).write_bytes(b"converted")
    os.remove(_path(vol, "Homebrew.iso"))
    FakeRomm._set_name(romm.roms[HOMEBREW_ID], "Homebrew.rvz")
    romm._rehash(romm.roms[HOMEBREW_ID])
    romm_repin.settle_swapped(output, HOMEBREW_ID)

    await _settle(client)

    assert romm.metadata_writes == [
        (HOMEBREW_ID, {"igdb_id": "12296", "tgdb_id": "24326", "hasheous_id": "13633"}),
    ]
