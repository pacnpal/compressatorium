"""Rename single-file Images to their DAT name (#425).

A user picks Images and asks for "Rename to DAT name". The preview lists each
old and new path, the Referrer updates the rename makes (``.m3u``,
gamelist.xml, RomM's ROM) and a reason for every Image it skips. Confirmed, it
runs as an external job through the shared Image rename operation, which
calls the existing Referrer hook: RomM renames the ROM's file itself, so the
ROM keeps its id. A Referrer that fails puts the file and every Referrer
already updated back, and the Image is reported as not renamed.

Driven through the HTTP API on real files: DATs imported through
``/dat/import``, the files matched through ``/dat/match-batch`` (real
hashing, cached as the file browser caches it), a
folder Library registered through ``/libraries``, and RomM played by
:class:`tests.fake_romm.FakeRomm`.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from . import test_folder_library_303 as folder_library_303
from .fake_romm import FakeRomm

# #303's folder Library fixture (`db`: a fresh database and a Volume).
_db_fixture = folder_library_303._db_fixture
_IS_ROOT = folder_library_303._IS_ROOT

CONFIRM = {"X-CHD-Action-Confirm": "rename-file"}
PS2 = {"id": 3, "slug": "ps2", "fs_slug": "ps2", "name": "PlayStation 2"}
ROM_ID = 12


def _app() -> FastAPI:
    from routes import convert, dat, dat_rename, files, findings, libraries

    app = FastAPI()
    for router in (
        convert.router, dat.router, dat_rename.router, files.router,
        findings.router, libraries.router,
    ):
        app.include_router(router, prefix="/api")
    return app


@pytest.fixture(name="sent")
def _sent(monkeypatch) -> list:
    """Every notification the app queues."""
    from services import notifications

    events: list = []
    monkeypatch.setattr(notifications, "notify", events.append)
    return events


@pytest_asyncio.fixture(name="client")
async def _client(db: Path, sent):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    from services import dat_rename

    await dat_rename.drain()


def _disc(path: Path, seed: bytes) -> str:
    """A small file made from *seed*; returns its SHA1."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = hashlib.sha256(seed).digest() * 64
    path.write_bytes(data)
    return hashlib.sha1(data).hexdigest()


def _dat(name: str, games: dict[str, str], *, homepage: str = "") -> str:
    """A DAT naming each SHA1 in *games* (``{game name: sha1}``)."""
    home = f"<homepage>{homepage}</homepage>" if homepage else ""
    body = "".join(
        f'<game name="{_xml(game)}"><rom name="{_xml(game)}.iso" size="4" sha1="{sha1}"/></game>'
        for game, sha1 in games.items()
    )
    return (
        '<?xml version="1.0"?><datafile><header>'
        f"<name>{name}</name><description>{name}</description>{home}</header>"
        f"{body}</datafile>"
    )


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


async def _import(client: httpx.AsyncClient, xml: str) -> str:
    response = await client.post(
        "/api/dat/import", files={"file": ("set.dat", xml.encode(), "text/xml")},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _match(client: httpx.AsyncClient, *paths: Path) -> None:
    """Match *paths* the way the file browser does, which caches the result."""
    response = await client.post("/api/dat/match-batch", json={"paths": [str(p) for p in paths]})
    assert response.status_code == 200, response.text


async def _register(client: httpx.AsyncClient, root: Path, **settings) -> str:
    response = await client.post("/api/libraries", json={"path": str(root)})
    assert response.status_code == 200, response.text
    library_id = response.json()["id"]
    if settings:
        patched = await client.patch(f"/api/libraries/{library_id}", json=settings)
        assert patched.status_code == 200, patched.text
    return library_id


async def _preview(client: httpx.AsyncClient, *paths: Path) -> dict:
    response = await client.post(
        "/api/files/dat-rename/preview", json={"paths": [str(p) for p in paths]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _by_path(preview: dict) -> dict[str, dict]:
    return {item["path"]: item for item in preview["items"]}


async def _run(client: httpx.AsyncClient, preview: dict, *, headers=CONFIRM) -> dict:
    """Confirm *preview* as shown and wait for its job; the finished job."""
    from services import dat_rename

    items = [{"path": i["path"], "new_path": i["new_path"]} for i in preview["items"]]
    started = await client.post("/api/files/dat-rename", json={"items": items}, headers=headers)
    assert started.status_code == 200, started.text
    await dat_rename.drain()
    job = await client.get(f"/api/jobs/{started.json()['job_id']}")
    assert job.status_code == 200, job.text
    return job.json()


def _result(job: dict) -> dict[str, dict]:
    return {r["path"]: r for r in job["result"]["results"]}


ES_GAMELIST = (
    '<?xml version="1.0"?>\n<gameList>\n'
    "\t<game>\n\t\t<path>./game.iso</path>\n\t\t<name>Ico</name>\n\t</game>\n"
    "</gameList>\n"
)


# ----------------------------------------------------------------------
# a folder Library: preview, then the job
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_preview_names_the_image_from_its_dat_and_lists_each_referrer_update(
    db: Path, client: httpx.AsyncClient,
) -> None:
    ps2 = db / "roms" / "ps2"
    game = ps2 / "game.iso"
    sha1 = _disc(game, b"ico")
    (ps2 / "games.m3u").write_text("game.iso\n")
    (ps2 / "gamelist.xml").write_text(ES_GAMELIST)
    await _register(client, db / "roms", es_frontend=True)
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)

    preview = await _preview(client, game)

    (item,) = preview["items"]
    assert item["status"] == "rename", item
    assert item["new_path"] == str(ps2 / "Ico (USA).iso")
    assert item["dat"]["game_name"] == "Ico (USA)"
    assert item["dat"]["dat_name"] == "Sony - PlayStation 2"
    changes = {os.path.basename(c["target"]): c for c in item["referrer_changes"]}
    assert set(changes) == {"games.m3u", "gamelist.xml"}
    assert changes["games.m3u"]["edits"] == [{"old": "game.iso", "new": "Ico (USA).iso"}]
    assert changes["gamelist.xml"]["entries"] == 1
    assert preview["renames"] == 1 and preview["skipped"] == 0
    # A preview changes nothing.
    assert sorted(p.name for p in ps2.iterdir()) == ["game.iso", "gamelist.xml", "games.m3u"]


@pytest.mark.asyncio
async def test_a_confirmed_rename_runs_as_a_job_and_its_referrers_follow(
    db: Path, client: httpx.AsyncClient, sent: list,
) -> None:
    from services.dat_store import dat_store
    from services.verification_store import verification_store

    ps2 = db / "roms" / "ps2"
    game = ps2 / "game.iso"
    sha1 = _disc(game, b"ico")
    before = game.read_bytes()
    (ps2 / "games.m3u").write_text("game.iso\n")
    (ps2 / "gamelist.xml").write_text(ES_GAMELIST)
    await _register(client, db / "roms", es_frontend=True)
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    await verification_store.mark_verified(str(game))

    job = await _run(client, await _preview(client, game))

    assert job["status"] == "completed", job
    assert job["mode"] == "dat_rename"
    renamed = ps2 / "Ico (USA).iso"
    result = _result(job)[str(game)]
    assert result["status"] == "renamed", result
    assert result["new_path"] == str(renamed)
    assert {r["referrer"] for r in result["referrers"]} == {"m3u", "gamelist"}
    assert not game.exists()
    assert renamed.read_bytes() == before
    assert (ps2 / "games.m3u").read_text() == "Ico (USA).iso\n"
    assert "<path>./Ico (USA).iso</path>" in (ps2 / "gamelist.xml").read_text()
    # What the app knew about the file follows it.
    assert dat_store.get_match(str(renamed))["game_name"] == "Ico (USA)"
    assert dat_store.get_match(str(game)) is None
    assert (await verification_store.get_record(str(renamed))) is not None
    # One summary through the notify path.
    (event,) = [e for e in sent if e.kind == "rename_finished"]
    assert "1 renamed" in event.title
    assert event.data["renamed"] == 1


@pytest.mark.asyncio
async def test_the_file_extension_is_kept_and_unsafe_characters_are_replaced(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    game = roms / "zelda.gba"
    sha1 = _disc(game, b"zelda")
    await _register(client, roms)
    await _import(client, _dat(
        "Nintendo - Game Boy Advance", {'Zelda: A "Link" <Past>? / Four|Swords*': sha1},
        homepage="No-Intro",
    ))
    await _match(client, game)

    item = _by_path(await _preview(client, game))[str(game)]

    assert item["new_path"] == str(roms / "Zelda_ A _Link_ _Past__ _ Four_Swords_.gba"), item


def test_one_rule_makes_a_dat_name_a_safe_file_name() -> None:
    from utils.path_utils import safe_file_name

    assert safe_file_name("Game: Part 1") == "Game_ Part 1"
    assert safe_file_name('a<b>c"d/e\\f|g?h*i') == "a_b_c_d_e_f_g_h_i"
    assert safe_file_name("Tab\there") == "Tab_here"
    # Windows drops trailing dots and spaces; the rule never leaves them.
    assert safe_file_name("Game (USA). ") == "Game (USA)"
    for nothing in ("", "  ", ".", "..", "..."):
        assert safe_file_name(nothing) is None
    assert safe_file_name("x" * 256) is None


@pytest.mark.asyncio
async def test_the_preview_skips_what_it_cannot_rename_with_a_reason(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    unknown = roms / "unknown.iso"
    _disc(unknown, b"unknown")
    named = roms / "Ico (USA).iso"
    ico = _disc(named, b"ico")
    taken = roms / "other.iso"
    other = _disc(taken, b"other")
    _disc(roms / "Other (USA).iso", b"something else")
    await _register(client, roms)
    await _import(client, _dat(
        "Sony - PlayStation 2", {"Ico (USA)": ico, "Other (USA)": other}, homepage="redump.org",
    ))
    await _match(client, unknown, named, taken)

    preview = await _preview(client, unknown, named, taken)

    items = _by_path(preview)
    assert {p: i["status"] for p, i in items.items()} == {
        str(unknown): "skip", str(named): "skip", str(taken): "skip",
    }
    assert "not Recognized" in items[str(unknown)]["reason"]
    assert "already" in items[str(named)]["reason"]
    assert "Other (USA).iso already exists" in items[str(taken)]["reason"]
    assert all(i["new_path"] is None for i in items.values())
    assert preview["renames"] == 0 and preview["skipped"] == 3


@pytest.mark.asyncio
async def test_two_images_the_dat_gives_one_name_are_not_both_renamed_to_it(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    first, second = roms / "a.iso", roms / "b.iso"
    sha1 = _disc(first, b"same")
    _disc(second, b"same")
    await _register(client, roms)
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, first, second)

    items = _by_path(await _preview(client, first, second))

    assert items[str(first)]["new_path"] == str(roms / "Ico (USA).iso")
    assert items[str(second)]["status"] == "skip"
    assert "a.iso takes that name too" in items[str(second)]["reason"]


@pytest.mark.asyncio
async def test_a_multi_file_image_and_its_tracks_are_left_for_later(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    track = roms / "game.bin"
    sha1 = _disc(track, b"track")
    sheet = roms / "game.cue"
    sheet.write_text('FILE "game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
    await _register(client, roms)
    await _import(client, _dat("Sony - PlayStation", {"Game (USA)": sha1}, homepage="redump.org"))
    await _match(client, track)

    items = _by_path(await _preview(client, sheet, track))

    assert items[str(sheet)]["status"] == "skip"
    assert "more than one file" in items[str(sheet)]["reason"]
    assert items[str(track)]["status"] == "skip"
    assert "track of game.cue" in items[str(track)]["reason"]


# ----------------------------------------------------------------------
# a conflict between two unranked DATs
# ----------------------------------------------------------------------


async def _rename_conflicts(client: httpx.AsyncClient) -> list[dict]:
    listed = (await client.get("/api/findings")).json()
    return [row for row in listed["findings"] if row["kind"] == "rename_conflict"]


@pytest.mark.asyncio
async def test_two_unranked_dats_that_disagree_leave_it_unnamed_with_a_finding(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    game = roms / "game.iso"
    sha1 = _disc(game, b"game")
    await _register(client, roms)
    first = await _import(client, _dat("Set One", {"Name One": sha1}))
    second = await _import(client, _dat("Set Two", {"Name Two": sha1}))
    await _match(client, game)

    item = _by_path(await _preview(client, game))[str(game)]

    assert item["status"] == "skip"
    assert "Set One" in item["reason"] and "Set Two" in item["reason"]
    (finding,) = await _rename_conflicts(client)
    assert finding["path"] == str(game)
    assert {m["dat_name"] for m in finding["cause"]["matches"]} == {"Set One", "Set Two"}
    assert "Set One" in finding["reason"] and "Set Two" in finding["reason"]

    # Ranking the DATs resolves it: the Finding clears, and the top DAT names it.
    ranked = await client.put("/api/dat/ranking", json={"order": [second, first]})
    assert ranked.status_code == 200, ranked.text
    assert await _rename_conflicts(client) == []
    item = _by_path(await _preview(client, game))[str(game)]
    assert item["new_path"] == str(roms / "Name Two.iso"), item


# ----------------------------------------------------------------------
# the job
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_run_needs_the_confirmation_header(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    game = roms / "game.iso"
    _disc(game, b"game")
    response = await client.post(
        "/api/files/dat-rename", json={"items": [{"path": str(game), "new_path": None}]},
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_the_job_skips_an_image_whose_new_name_was_taken_since_the_preview(
    db: Path, client: httpx.AsyncClient, sent: list,
) -> None:
    roms = db / "roms"
    game = roms / "game.iso"
    sha1 = _disc(game, b"ico")
    await _register(client, roms)
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    preview = await _preview(client, game)
    _disc(roms / "Ico (USA).iso", b"arrived meanwhile")

    job = await _run(client, preview)

    assert job["status"] == "completed", job
    result = _result(job)[str(game)]
    assert result["status"] == "skipped", result
    assert "already exists" in result["reason"]
    assert game.exists()
    assert (roms / "Ico (USA).iso").read_bytes() != game.read_bytes()
    (event,) = [e for e in sent if e.kind == "rename_finished"]
    assert event.data["skipped"] == 1 and event.data["renamed"] == 0


@pytest.mark.asyncio
@pytest.mark.skipif(_IS_ROOT, reason="root writes a read-only file anyway")
async def test_a_failing_referrer_puts_the_file_and_every_updated_referrer_back(
    db: Path, client: httpx.AsyncClient,
) -> None:
    ps2 = db / "roms" / "ps2"
    game = ps2 / "game.iso"
    sha1 = _disc(game, b"ico")
    (ps2 / "games.m3u").write_text("game.iso\n")
    gamelist = ps2 / "gamelist.xml"
    gamelist.write_text(ES_GAMELIST)
    await _register(client, db / "roms", es_frontend=True)
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    preview = await _preview(client, game)
    # The playlist is updated first; then the gamelist can't be written.
    os.chmod(gamelist, 0o444)
    try:
        job = await _run(client, preview)
    finally:
        os.chmod(gamelist, 0o644)

    assert job["status"] == "completed", job
    result = _result(job)[str(game)]
    assert result["status"] == "not_renamed", result
    assert "read-only" in result["reason"]
    assert game.exists() and not (ps2 / "Ico (USA).iso").exists()
    assert (ps2 / "games.m3u").read_text() == "game.iso\n"
    assert gamelist.read_text() == ES_GAMELIST


# ----------------------------------------------------------------------
# a RomM Library: the ROM is renamed through RomM and keeps its id
# ----------------------------------------------------------------------


@pytest_asyncio.fixture(name="romm")
async def _romm(db: Path):
    """The fake RomM holding ROM 12 for ``romm/ps2/game.iso``, swap on."""
    from services.romm import settings as romm_settings

    platform = db / "romm" / "ps2"
    platform.mkdir(parents=True)
    _disc(platform / "game.iso", b"ico")
    (platform / "games.m3u").write_text("game.iso\n")
    fake = FakeRomm(platforms=[PS2], library=db / "romm").start()
    fake.add_rom(ROM_ID, PS2, "game.iso", igdb_id=4242)
    await romm_settings.save({
        "url": fake.url, "token": "rmm_x", "library_root": str(db / "romm"),
        "swap_keep_rom_id": True,
    })
    yield fake
    fake.stop()


async def _romm_game(client: httpx.AsyncClient, db: Path) -> Path:
    game = db / "romm" / "ps2" / "game.iso"
    sha1 = hashlib.sha1(game.read_bytes()).hexdigest()
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    return game


@pytest.mark.asyncio
async def test_a_romm_rom_is_renamed_through_romm_and_keeps_its_id(
    db: Path, client: httpx.AsyncClient, romm: FakeRomm,
) -> None:
    game = await _romm_game(client, db)
    platform = game.parent

    preview = await _preview(client, game)
    item = _by_path(preview)[str(game)]
    summaries = [c.get("summary") for c in item["referrer_changes"]]
    assert f"RomM: rename ROM {ROM_ID} to Ico (USA).iso" in summaries, item
    assert romm.renames == []

    job = await _run(client, preview)

    result = _result(job)[str(game)]
    assert result["status"] == "renamed", result
    assert romm.renames == [(ROM_ID, "Ico (USA).iso")]
    rom = romm.roms[ROM_ID]
    assert rom["fs_name"] == "Ico (USA).iso" and rom["igdb_id"] == 4242
    assert romm.scans == []
    assert sorted(p.name for p in platform.iterdir() if not p.name.startswith(".")) == [
        "Ico (USA).iso", "games.m3u", "games.m3u.bak",
    ]
    assert (platform / "games.m3u").read_text() == "Ico (USA).iso\n"
    (record,) = [r for r in result["referrers"] if r["referrer"] == "romm_swap"]
    assert record["rom_id"] == ROM_ID and record["kept_rom_id"] is True


@pytest.mark.asyncio
async def test_a_romm_refusal_leaves_the_file_and_the_playlist_where_they_were(
    db: Path, client: httpx.AsyncClient, romm: FakeRomm,
) -> None:
    game = await _romm_game(client, db)
    platform = game.parent
    preview = await _preview(client, game)
    romm.rename_reply = (409, "Can't rename: Ico (USA).iso already exists")

    job = await _run(client, preview)

    result = _result(job)[str(game)]
    assert result["status"] == "not_renamed", result
    assert "RomM refused the rename" in result["reason"]
    assert game.exists() and not (platform / "Ico (USA).iso").exists()
    assert romm.roms[ROM_ID]["fs_name"] == "game.iso"
    # The playlist was updated before RomM was asked; it is put back.
    assert (platform / "games.m3u").read_text() == "game.iso\n"


@pytest.mark.asyncio
async def test_a_romm_library_without_keep_the_rom_id_is_not_renamed(
    db: Path, client: httpx.AsyncClient, romm: FakeRomm,
) -> None:
    from services.romm import settings as romm_settings

    await romm_settings.save({"swap_keep_rom_id": False})
    game = await _romm_game(client, db)

    item = _by_path(await _preview(client, game))[str(game)]

    assert item["status"] == "skip"
    assert "Keep the ROM id" in item["reason"]
    assert romm.renames == []
