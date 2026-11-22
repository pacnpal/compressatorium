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
    from routes import dat, libraries, romm

    app = FastAPI()
    for router in (romm.router, dat.router, libraries.router):
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


def test_an_id_that_only_looks_numeric_is_dropped_not_raised() -> None:
    """CodeRabbit on PR #442: ``"²".isdigit()`` is True but ``int("²")``
    raises, which would fail the whole lookup."""
    from services import hasheous

    response = dict(HASHEOUS_RESPONSE, metadata=[
        {"source": "IGDB", "id": "²", "link": "x", "status": "Mapped"},
        {"source": "TheGamesDb", "id": "24326", "link": "y", "status": "Mapped"},
    ])

    assert hasheous._normalize(response)["provider_ids"] == {
        "hasheous_id": 13633, "tgdb_id": 24326,
    }


def test_an_id_too_long_to_convert_is_dropped_not_raised() -> None:
    """CodeRabbit on PR #442: 5,000 ASCII digits pass both checks, and then
    ``int()`` refuses them (Python's integer-string limit)."""
    from services import hasheous

    response = dict(HASHEOUS_RESPONSE, metadata=[
        {"source": "IGDB", "id": "1" * 5000, "link": "x", "status": "Mapped"},
        {"source": "TheGamesDb", "id": "24326", "link": "y", "status": "Mapped"},
    ])

    assert hasheous._normalize(response)["provider_ids"] == {
        "hasheous_id": 13633, "tgdb_id": 24326,
    }


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
    saved = await client.put("/api/libraries/romm/rules", json={"rules": {"7": {
        "mode": "dolphin_rvz", "enabled": True, **rule,
    }}})
    assert saved.status_code == 200, saved.text
    preview = await client.post("/api/libraries/romm/preview", json={})
    assert preview.status_code == 200, preview.text
    (platform,) = preview.json()["platforms"]
    return sorted(platform.get("candidates") or [])


@pytest.mark.asyncio
async def test_only_recognized_includes_what_the_app_recognizes(client):
    assert await _candidates(client, {"only_matched": True}) == ["Homebrew.iso", "Known.iso"]


@pytest.mark.asyncio
async def test_a_filtered_out_rom_is_never_mapped_onto_the_mount(client, romm, vol, monkeypatch):
    """Codex on PR #442: listing mapped every unidentified ROM onto its path to
    read the match cache, so one ROM on a stalled mount held the whole
    Platform even when the Rule's name filter excludes it. Whether a ROM is
    Recognized is now worked out only when a filter asks, after the name and
    size filters."""
    import threading
    import time

    from services.romm import romm_client

    _iso(vol, "Skip.iso")
    romm.add_rom(4, NGC, "Skip.iso")
    mapped: list[str] = []
    stalled = threading.Event()
    real = romm_client.local_path

    def local_path(rom):
        mapped.append(rom.get("fs_name"))
        if rom.get("fs_name") == "Skip.iso":
            stalled.wait(5)  # a mount that stopped answering
        return real(rom)

    monkeypatch.setattr(romm_client, "local_path", local_path)
    started = time.monotonic()
    try:
        candidates = await _candidates(
            client, {"only_matched": True, "exclude_pattern": "^Skip"},
        )
    finally:
        stalled.set()

    assert candidates == ["Homebrew.iso", "Known.iso"]
    assert "Skip.iso" not in mapped
    assert time.monotonic() - started < 4


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
async def test_a_zip_output_is_found_by_its_path_not_its_container_hash(client, romm, vol):
    """Codex on PR #442: RomM hashes a zip by its contents, so the output's own
    SHA-1 never finds it. The settle pass joins on the path instead."""
    import zipfile

    await _switch(client, True)
    planned = await _plan(client, vol, "Homebrew.iso", mode="romz_zip")
    output = Path(_path(vol, "Homebrew.iso.zip"))
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("Homebrew.iso", Path(_path(vol, "Homebrew.iso")).read_bytes())
    romm.add_rom(44, NGC, "Homebrew.iso.zip")

    await _settle(client)

    assert planned["recorded"] == 1
    assert romm.metadata_writes == [
        (44, {"igdb_id": "12296", "tgdb_id": "24326", "hasheous_id": "13633"}),
    ]


@pytest.mark.asyncio
async def test_a_stalled_cache_read_records_no_ids_and_does_not_hang(
    client, romm, vol, monkeypatch,
):
    """Codex on PR #442: the fill lookup resolves each source on the mount. A
    mount that stops answering must not hold the plan; it records no ids."""
    import threading

    from services.romm import repin as romm_repin

    release = threading.Event()

    def stalled(_sources):
        release.wait(30)
        return {}

    monkeypatch.setattr(romm_repin, "_hasheous_fills_blocking", stalled)
    monkeypatch.setattr(romm_repin, "FILL_LOOKUP_SECONDS", 0.2)
    await _switch(client, True)
    try:
        planned = await _plan(client, vol, "Homebrew.iso")
    finally:
        release.set()

    assert planned["recorded"] == 0


def _legacy_hit(vol: Path, name: str) -> dict:
    """A Hasheous hit cached before #427: no ``provider_ids``."""
    hit = _hasheous_hit(_path(vol, name), "Homebrew Quest", {})
    hit.pop("provider_ids")
    return hit


@pytest.fixture(name="hasheous_on")
def _hasheous_on(monkeypatch):
    from services import hasheous

    monkeypatch.setattr(app_settings, "hasheous_enabled", True)
    monkeypatch.setattr(app_settings, "hasheous_base_url", "https://hasheous.example")
    hasheous._clear_cooldown()
    yield
    hasheous._clear_cooldown()


@pytest.mark.asyncio
async def test_a_hit_cached_without_ids_is_matched_again_once_ids_are_wanted(
    client, romm, vol, hasheous_on,
):
    """Codex on PR #442: a Hasheous hit cached before the normaliser kept the
    ids carries none, and a cached hit is always reused, so turning the switch
    on wrote nothing for it. Once ids are wanted, the match job looks it up
    again; the listing still counts it Recognized meanwhile."""
    from routes import dat as dat_routes
    from services import hasheous
    from services.dat_store import dat_store

    homebrew = _path(vol, "Homebrew.iso")
    await dat_store.set_match(homebrew, _legacy_hit(vol, "Homebrew.iso"))
    await _switch(client, True)
    assert _by_name(await _listing(client))["Homebrew.iso"]["dat_match"]["matched"] is True

    with patch.object(dat_routes.hasheous, "lookup", return_value=hasheous._normalize(
        HASHEOUS_RESPONSE,
    )):
        started = await client.post("/api/dat/match-batch/job", json={"paths": [homebrew]})

    assert started.json()["status"] == "started", started.text
    assert dat_store.get_match(homebrew)["provider_ids"] == HOMEBREW_IDS
    await _plan(client, vol, "Homebrew.iso")
    _romm_scans_output(romm, vol, "Homebrew.rvz", 43)
    await _settle(client)
    assert romm.metadata_writes == [
        (43, {"igdb_id": "12296", "tgdb_id": "24326", "hasheous_id": "13633"}),
    ]


@pytest.mark.asyncio
async def test_a_hit_cached_without_ids_is_reused_while_ids_are_not_wanted(
    client, romm, vol, hasheous_on,
):
    from services.dat_store import dat_store

    homebrew = _path(vol, "Homebrew.iso")
    await dat_store.set_match(homebrew, _legacy_hit(vol, "Homebrew.iso"))

    started = await client.post("/api/dat/match-batch/job", json={"paths": [homebrew]})

    assert started.json()["status"] == "idle", started.text


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


@pytest.mark.asyncio
async def test_switching_off_stops_pending_hasheous_writes(client, romm, vol):
    """CodeRabbit on PR #442: a row recorded while the switch was on must not
    write Hasheous ids once it is off. RomM's own carried ids still are, and
    the row still settles."""
    from services.dat_store import dat_store

    await dat_store.set_match(_path(vol, "Known.iso"), _hasheous_hit(
        _path(vol, "Known.iso"), "Known Game", HOMEBREW_IDS,
    ))
    await _switch(client, True)
    await _plan(client, vol, "Known.iso")
    await _plan(client, vol, "Homebrew.iso")
    await _switch(client, False)
    _romm_scans_output(romm, vol, "Known.rvz", 45)
    _romm_scans_output(romm, vol, "Homebrew.rvz", 46)

    settled = await _settle(client)

    assert romm.metadata_writes == [(45, {"igdb_id": "11"})]
    assert settled["pending"] == 0


@pytest.mark.asyncio
async def test_a_sweep_record_does_not_supersede_a_plan_made_during_its_lookup(
    client, romm, vol, monkeypatch,
):
    """CodeRabbit on PR #442: the sweep's re-pin took its plan generation after
    awaiting the Hasheous lookup, so a manual plan recorded in that window for
    the same destination was superseded by the older sweep."""
    from services.romm import repin as romm_repin
    from services.romm.library import romm_library
    from services.rules.library import Image, QueuedImage
    from services.tools import registry

    await _switch(client, True)
    rom = dict(romm.roms[KNOWN_ID])
    source = _path(vol, "Known.iso")
    destination = _path(vol, "Known.rvz")
    real_fills = romm_repin.hasheous_fills
    manual: list[int] = []

    async def fills_while_a_plan_lands(sources):
        # A manual submit plans the same destination while the sweep waits.
        manual.append(romm_repin.record(
            rom, destination, {"igdb_id": 99}, "dolphin_rvz", "", source,
        ))
        return await real_fills(sources)

    monkeypatch.setattr(romm_repin, "hasheous_fills", fills_while_a_plan_lands)
    image = Image(key=KNOWN_ID, name="Known.iso", record=rom)
    summary: dict = {}
    result = {"repins_recorded": 0}
    await romm_library.after_queue(
        {"mode": "dolphin_rvz"}, registry.spec("dolphin_rvz"),
        [QueuedImage(image=image, source=source, destination=destination, pre="")],
        summary, result,
    )

    assert manual and manual[0]
    assert summary["repins_recorded"] == 0
    with _db.SessionLocal() as session:
        (row,) = session.query(_db.RommRepin).filter_by(state="pending").all()
        assert row.id == manual[0]


@pytest.mark.asyncio
async def test_the_swap_fallback_never_waits_on_a_stalled_lookup(
    client, romm, vol, monkeypatch,
):
    """CodeRabbit on PR #442: the fallback re-pin read the Hasheous ids inside
    a step that is never abandoned, so a stalled mount held it forever. The
    lookup is bounded and done first; on a timeout RomM's own ids are still
    recorded."""
    import threading
    import time
    from types import SimpleNamespace

    from services.romm import repin as romm_repin
    from services.romm import swap_referrer

    release = threading.Event()

    def stalled(_sources):
        release.wait(30)
        return {}

    monkeypatch.setattr(romm_repin, "_hasheous_fills_blocking", stalled)
    monkeypatch.setattr(romm_repin, "FILL_LOOKUP_SECONDS", 0.2)
    await _switch(client, True)
    plan = SimpleNamespace(
        binding=None, rom=dict(romm.roms[KNOWN_ID]),
        output=_path(vol, "Known.rvz"), source=_path(vol, "Known.iso"),
    )
    started = time.monotonic()
    try:
        await swap_referrer._fall_back(plan, "RomM ROM 1")
    finally:
        release.set()

    assert time.monotonic() - started < 5
    with _db.SessionLocal() as session:
        (row,) = session.query(_db.RommRepin).filter_by(state="pending").all()
        assert row.metadata_ids == {"igdb_id": 11}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity_change", "binding"])
async def test_the_swap_fallback_records_nothing_if_romm_changes_during_the_lookup(
    client, romm, vol, monkeypatch, change,
):
    """CodeRabbit on PR #442: the fallback checked RomM's binding before the
    Hasheous lookup and not after it, so a settings change during the lookup
    could record the old instance's ids against the new one."""
    from types import SimpleNamespace

    from services.referrers import ReferrerError
    from services.romm import repin as romm_repin
    from services.romm import swap_referrer

    live = {"value": True}

    async def lookup_while_romm_changes(_sources):
        if change == "identity_change":
            monkeypatch.setattr(swap_referrer, "_identity_changes", 1)
        else:
            live["value"] = False
        return {}

    monkeypatch.setattr(romm_repin, "hasheous_fills", lookup_while_romm_changes)
    await _switch(client, True)
    plan = SimpleNamespace(
        binding=SimpleNamespace(still_live=lambda: live["value"]),
        rom=dict(romm.roms[KNOWN_ID]),
        output=_path(vol, "Known.rvz"), source=_path(vol, "Known.iso"),
    )

    with pytest.raises(ReferrerError):
        await swap_referrer._fall_back(plan, "RomM ROM 1")

    with _db.SessionLocal() as session:
        assert session.query(_db.RommRepin).count() == 0
