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
async def test_a_track_is_never_renamed_alone(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """A track the DAT recognizes is still its sheet's: it is renamed with
    the sheet (#426), never on its own. The sheet, unmatched here, is not
    Recognized."""
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
    assert "not Recognized" in items[str(sheet)]["reason"]
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
async def test_a_failing_referrer_puts_the_file_and_every_updated_referrer_back(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """The playlist is updated (observed), then the gamelist fails. The
    playlist goes back to exactly what it held: its dangling entry that
    already named the new name is not touched (Codex 4602560232)."""
    from services.referrers import ReferrerError
    from services.referrers.gamelist import GamelistReferrer

    ps2 = db / "roms" / "ps2"
    game = ps2 / "game.iso"
    sha1 = _disc(game, b"ico")
    playlist = ps2 / "games.m3u"
    playlist.write_text("game.iso\nIco (USA).iso\n")
    gamelist = ps2 / "gamelist.xml"
    gamelist.write_text(ES_GAMELIST)
    await _register(client, db / "roms", es_frontend=True)
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    preview = await _preview(client, game)
    seen: list[str] = []

    def fails_after_the_playlist(self, change):
        seen.append(playlist.read_text())
        raise ReferrerError(change.target, "is read-only")

    monkeypatch.setattr(GamelistReferrer, "apply", fails_after_the_playlist)
    job = await _run(client, preview)

    assert seen == ["Ico (USA).iso\nIco (USA).iso\n"], seen
    assert job["status"] == "completed", job
    result = _result(job)[str(game)]
    assert result["status"] == "not_renamed", result
    assert "read-only" in result["reason"]
    assert game.exists() and not (ps2 / "Ico (USA).iso").exists()
    assert playlist.read_text() == "game.iso\nIco (USA).iso\n"
    assert gamelist.read_text() == ES_GAMELIST


@pytest.mark.asyncio
async def test_a_destination_taken_during_the_hook_leaves_it_alone_and_puts_referrers_back(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Another file takes the new name while the hook runs: the rename can't
    place the Image, the newcomer is untouched, and the playlist goes back
    without the Image ever being moved onto it (CodeRabbit 4602644886)."""
    from services import image_rename

    roms = db / "roms"
    game = roms / "game.iso"
    sha1 = _disc(game, b"ico")
    (roms / "games.m3u").write_text("game.iso\n")
    await _register(client, roms)
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    preview = await _preview(client, game)
    real_hook = image_rename.referrers.update_before_source_removal

    async def then_taken(*args, **kwargs):
        outcome = await real_hook(*args, **kwargs)
        (roms / "Ico (USA).iso").write_bytes(b"someone else's")
        return outcome

    monkeypatch.setattr(image_rename.referrers, "update_before_source_removal", then_taken)
    job = await _run(client, preview)

    result = _result(job)[str(game)]
    assert result["status"] == "not_renamed", result
    assert game.exists()
    assert (roms / "Ico (USA).iso").read_bytes() == b"someone else's"
    assert (roms / "games.m3u").read_text() == "game.iso\n"


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


# ----------------------------------------------------------------------
# review findings (Codex on #441)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancel_mid_way_through_the_file_phase_puts_the_updated_playlist_back(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """The playlist is updated, then the gamelist write stalls and the job is
    cancelled. The phase's worker is settled and its completed update rolled
    back, so the playlist never names a file the Image never became."""
    import asyncio
    import threading

    from services import dat_rename
    from services.referrers.base import check_write
    from services.referrers.gamelist import GamelistReferrer

    ps2 = db / "roms" / "ps2"
    game = ps2 / "game.iso"
    sha1 = _disc(game, b"ico")
    (ps2 / "games.m3u").write_text("game.iso\n")
    (ps2 / "gamelist.xml").write_text(ES_GAMELIST)
    await _register(client, db / "roms", es_frontend=True)
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    preview = await _preview(client, game)
    entered, release = threading.Event(), threading.Event()

    def stalled(self, change):
        entered.set()
        release.wait(10)
        check_write(change.target)  # the job stopped waiting: nothing is written

    monkeypatch.setattr(GamelistReferrer, "apply", stalled)
    items = [{"path": i["path"], "new_path": i["new_path"]} for i in preview["items"]]
    started = await client.post("/api/files/dat-rename", json={"items": items}, headers=CONFIRM)
    job_id = started.json()["job_id"]
    assert await asyncio.to_thread(entered.wait, 10)
    assert (ps2 / "games.m3u").read_text() == "Ico (USA).iso\n"
    cancelled = await client.delete(f"/api/jobs/{job_id}")
    assert cancelled.status_code == 200, cancelled.text
    threading.Timer(0.3, release.set).start()
    await dat_rename.drain()

    job = (await client.get(f"/api/jobs/{job_id}")).json()
    # Cancelled during its only Image: the job ends cancelled, not completed
    # (Codex 4603430454).
    assert job["status"] == "cancelled", job
    assert job["result"]["cancelled"] is True
    result = _result(job)[str(game)]
    assert result["status"] == "not_renamed", result
    assert game.exists() and not (ps2 / "Ico (USA).iso").exists()
    assert (ps2 / "games.m3u").read_text() == "game.iso\n"
    assert (ps2 / "gamelist.xml").read_text() == ES_GAMELIST


@pytest.mark.asyncio
async def test_an_image_reached_through_a_symlinked_folder_keeps_its_dat_match(
    db: Path, client: httpx.AsyncClient,
) -> None:
    from services.dat_store import dat_store

    discs = db / "roms" / "discs"
    sha1 = _disc(discs / "game.iso", b"ico")
    (db / "roms" / "alias").symlink_to(discs, target_is_directory=True)
    listed = db / "roms" / "alias" / "game.iso"
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    # Matched the way the file browser does, which caches it under the real path.
    await _match(client, listed)

    job = await _run(client, await _preview(client, listed))

    assert _result(job)[str(listed)]["status"] == "renamed", job
    assert (discs / "Ico (USA).iso").exists()
    assert dat_store.get_match(str(discs / "Ico (USA).iso"))["game_name"] == "Ico (USA)"
    assert dat_store.get_match(str(discs / "game.iso")) is None


@pytest.mark.asyncio
async def test_a_lock_that_fails_releases_the_locks_already_taken(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services.lock_manager import LockFailed, lock_manager

    roms = db / "roms"
    game = roms / "game.iso"
    sha1 = _disc(game, b"ico")
    await _register(client, roms)
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    preview = await _preview(client, game)
    real_acquire = lock_manager.acquire_lock

    def failing(path, *, allow_existing=False):
        if path.endswith("Ico (USA).iso"):
            raise LockFailed(path, OSError(13, "Permission denied"))
        return real_acquire(path, allow_existing=allow_existing)

    monkeypatch.setattr(lock_manager, "acquire_lock", failing)
    job = await _run(client, preview)

    result = _result(job)[str(game)]
    assert result["status"] == "not_renamed", result
    assert "Could not lock" in result["reason"]
    assert game.exists()
    assert not lock_manager.is_locked(str(game))


@pytest.mark.asyncio
async def test_a_compound_extension_is_kept_whole(db: Path, client: httpx.AsyncClient) -> None:
    roms = db / "roms"
    game = roms / "metroid.nkit.iso"
    sha1 = _disc(game, b"metroid")
    await _import(client, _dat("Nintendo - GameCube", {"Metroid Prime (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)

    item = _by_path(await _preview(client, game))[str(game)]

    assert item["new_path"] == str(roms / "Metroid Prime (USA).nkit.iso"), item


@pytest.mark.asyncio
async def test_a_track_a_sheet_reaches_through_a_link_elsewhere_is_not_renamed_alone(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """No sheet beside or above the track lists it, but one in another folder
    reaches it through a symlink: the Volume-wide sheet walk finds it."""
    tracks = db / "roms" / "tracks"
    track = tracks / "game.bin"
    sha1 = _disc(track, b"track")
    sheets = db / "sheets"
    sheets.mkdir()
    (sheets / "link").symlink_to(tracks, target_is_directory=True)
    (sheets / "game.cue").write_text(
        'FILE "link/game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
    )
    await _import(client, _dat("Sony - PlayStation", {"Game (USA)": sha1}, homepage="redump.org"))
    await _match(client, track)

    item = _by_path(await _preview(client, track))[str(track)]

    assert item["status"] == "skip", item
    assert "track" in item["reason"]


@pytest.mark.asyncio
async def test_a_parent_swapped_for_a_link_after_the_check_never_moves_a_file_outside(
    db: Path, client: httpx.AsyncClient, monkeypatch, tmp_path: Path,
) -> None:
    """The Image's folder is swapped for a symlink to a folder outside every
    Volume after it was checked. The move is bound to the folder that was
    checked, so the file outside is never touched (Codex 4602569670)."""
    from services import image_rename

    ps2 = db / "roms" / "ps2"
    game = ps2 / "game.iso"
    sha1 = _disc(game, b"ico")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "game.iso").write_bytes(b"not in a Volume")
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    preview = await _preview(client, game)
    real_hook = image_rename.referrers.update_before_source_removal

    async def swapped(*args, **kwargs):
        outcome = await real_hook(*args, **kwargs)
        ps2.rename(db / "roms" / "ps2-real")
        ps2.symlink_to(outside, target_is_directory=True)
        return outcome

    monkeypatch.setattr(image_rename.referrers, "update_before_source_removal", swapped)
    await _run(client, preview)

    assert sorted(p.name for p in outside.iterdir()) == ["game.iso"]
    assert (outside / "game.iso").read_bytes() == b"not in a Volume"


@pytest.mark.asyncio
async def test_one_image_that_fails_unexpectedly_doesnt_hide_the_others(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services import image_sets

    roms = db / "roms"
    good, bad = roms / "good.iso", roms / "bad.iso"
    ico = _disc(good, b"ico")
    other = _disc(bad, b"other")
    await _import(client, _dat(
        "Sony - PlayStation 2", {"Ico (USA)": ico, "Other (USA)": other}, homepage="redump.org",
    ))
    await _match(client, good, bad)
    real_members = image_sets.image_members

    def members(path):
        if path.endswith("bad.iso"):
            raise OSError(5, "Input/output error")
        return real_members(path)

    monkeypatch.setattr(image_sets, "image_members", members)
    preview = await _preview(client, good, bad)
    items = _by_path(preview)
    assert items[str(good)]["status"] == "rename"
    assert items[str(bad)]["status"] == "skip" and "Input/output error" in items[str(bad)]["reason"]

    job = await _run(client, preview)

    assert job["status"] == "completed", job
    results = _result(job)
    assert results[str(good)]["status"] == "renamed"
    assert results[str(bad)]["status"] == "skipped"


@pytest.mark.asyncio
async def test_a_failing_conflict_settle_never_breaks_the_findings_list(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services import dat_rename

    def broken() -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(dat_rename, "settle_conflicts", broken)

    response = await client.get("/api/findings")

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_a_cancel_while_romm_renames_settles_romm_before_the_rename_ends(
    db: Path, client: httpx.AsyncClient, romm: FakeRomm,
) -> None:
    """The job is cancelled while RomM's rename request is in flight, and
    RomM writes a sanitized name. The swap still reads RomM back and puts the
    old name back, so the rename ends with the Image, RomM and the playlist
    as they were (CodeRabbit 4602644910)."""
    import asyncio
    import threading

    from services import dat_rename

    game = await _romm_game(client, db)
    platform = game.parent
    preview = await _preview(client, game)
    entered, release = threading.Event(), threading.Event()

    def hold_the_rename(method, path):
        if method == "PUT" and not entered.is_set():
            entered.set()
            release.wait(10)

    romm.on_request = hold_the_rename
    romm.rename_as = "Ico_(USA).iso"
    items = [{"path": i["path"], "new_path": i["new_path"]} for i in preview["items"]]
    started = await client.post("/api/files/dat-rename", json={"items": items}, headers=CONFIRM)
    job_id = started.json()["job_id"]
    assert await asyncio.to_thread(entered.wait, 10)
    await client.delete(f"/api/jobs/{job_id}")
    release.set()
    await dat_rename.drain()

    job = (await client.get(f"/api/jobs/{job_id}")).json()
    result = _result(job)[str(game)]
    assert result["status"] == "not_renamed", result
    assert romm.roms[ROM_ID]["fs_name"] == "game.iso"
    assert game.exists()
    assert (platform / "games.m3u").read_text() == "game.iso\n"


# ----------------------------------------------------------------------
# second review round (CodeRabbit follow-ups on #441)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stalled_conflict_settle_is_never_started_twice(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """A settle stuck on a Volume keeps its worker; every Findings load and
    every ranking save while it runs answers within the bound and starts no
    other one, so stalled requests can't hang or use up the shared detached
    slots (CodeRabbit 4602978437, Codex 4603430802). One settle, shared by
    both routes."""
    import asyncio
    import threading

    from services import dat_rename

    release = threading.Event()
    calls: list[int] = []

    def stuck() -> int:
        calls.append(1)
        release.wait(10)
        return 0

    monkeypatch.setattr(dat_rename, "settle_conflicts", stuck)
    monkeypatch.setattr(dat_rename, "SETTLE_SECONDS", 0.1)
    try:
        for _ in range(2):
            assert (await client.get("/api/findings")).status_code == 200
            saved = await asyncio.wait_for(client.put("/api/dat/ranking", json={"order": []}), 5)
            assert saved.status_code == 200, saved.text
            reset = await asyncio.wait_for(client.delete("/api/dat/ranking"), 5)
            assert reset.status_code == 200, reset.text
        assert calls == [1]
    finally:
        release.set()
    for _ in range(50):
        if not dat_rename.settling_conflicts():
            break
        await asyncio.sleep(0.02)
    assert (await client.get("/api/findings")).status_code == 200
    assert calls == [1, 1]


@pytest.mark.asyncio
async def test_a_folder_open_that_answers_late_closes_its_folders(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Opening the Image's folders outlasts its bound, so the rename stops
    waiting; when the open answers later its folder descriptors are closed,
    never leaked (Codex 4603430442)."""
    import asyncio
    import time

    from services import image_rename

    roms = db / "roms"
    game = roms / "game.iso"
    sha1 = _disc(game, b"ico")
    await _import(client, _dat("Sony - PlayStation 2", {"Ico (USA)": sha1}, homepage="redump.org"))
    await _match(client, game)
    preview = await _preview(client, game)
    opened: list = []
    real_open = image_rename._Files.open_blocking

    def slow(renames, **kwargs):
        time.sleep(0.6)
        files = real_open(renames, **kwargs)
        opened.append((files, list(files._fds.values())))
        return files

    monkeypatch.setattr(image_rename._Files, "open_blocking", staticmethod(slow))
    monkeypatch.setattr(image_rename, "OPEN_SECONDS", 0.2)
    job = await _run(client, preview)

    result = _result(job)[str(game)]
    assert result["status"] == "not_renamed", result
    assert game.exists()
    for _ in range(100):
        if opened:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    ((files, fds),) = opened
    assert fds and files._fds == {}


@pytest.mark.asyncio
async def test_a_romm_request_still_running_after_the_wait_is_settled_before_deciding(
    db: Path, client: httpx.AsyncClient, romm: FakeRomm, monkeypatch,
) -> None:
    """The job is cancelled while RomM's rename is held, and the hook gives
    up on it. RomM then renames after the rename's own wait for handed-off
    requests has passed. Nothing is decided until the request has ended: the
    file, RomM and the playlist all end on the new name, and the records
    follow (CodeRabbit 4602980637)."""
    import asyncio
    import threading

    from services import dat_rename, image_rename
    from services import referrers as referrers_module
    from services.dat_store import dat_store

    game = await _romm_game(client, db)
    platform = game.parent
    preview = await _preview(client, game)
    monkeypatch.setattr(referrers_module, "CANCEL_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(image_rename, "HANDED_OFF_SECONDS", 0.2)
    entered, release = threading.Event(), threading.Event()

    def hold_the_rename(method, path):
        if method == "PUT" and not entered.is_set():
            entered.set()
            release.wait(10)

    romm.on_request = hold_the_rename
    items = [{"path": i["path"], "new_path": i["new_path"]} for i in preview["items"]]
    started = await client.post("/api/files/dat-rename", json={"items": items}, headers=CONFIRM)
    job_id = started.json()["job_id"]
    assert await asyncio.to_thread(entered.wait, 10)
    await client.delete(f"/api/jobs/{job_id}")
    # Past the hook's grace and the rename's own wait, then RomM answers.
    threading.Timer(1.0, release.set).start()
    await dat_rename.drain()

    job = (await client.get(f"/api/jobs/{job_id}")).json()
    result = _result(job)[str(game)]
    assert result["status"] == "renamed", result
    assert romm.roms[ROM_ID]["fs_name"] == "Ico (USA).iso"
    assert (platform / "Ico (USA).iso").exists() and not game.exists()
    assert (platform / "games.m3u").read_text() == "Ico (USA).iso\n"
    assert dat_store.get_match(str(platform / "Ico (USA).iso"))["game_name"] == "Ico (USA)"


@pytest.mark.asyncio
async def test_a_cancel_while_romm_puts_the_old_name_back_reads_romm_back(
    db: Path, client: httpx.AsyncClient, romm: FakeRomm,
) -> None:
    """RomM writes a sanitized name, the swap asks for the old one back, and
    the job is cancelled while that request is in flight. The swap still
    reads RomM back once it has answered (CodeRabbit 4602987587)."""
    import asyncio
    import threading

    from services import dat_rename

    game = await _romm_game(client, db)
    platform = game.parent
    preview = await _preview(client, game)
    entered, release = threading.Event(), threading.Event()
    puts: list[int] = []

    def hold_the_put_back(method, path):
        if method == "PUT":
            puts.append(1)
            if len(puts) == 2:
                entered.set()
                release.wait(10)

    romm.on_request = hold_the_put_back
    romm.rename_as = "Ico_(USA).iso"
    items = [{"path": i["path"], "new_path": i["new_path"]} for i in preview["items"]]
    started = await client.post("/api/files/dat-rename", json={"items": items}, headers=CONFIRM)
    job_id = started.json()["job_id"]
    assert await asyncio.to_thread(entered.wait, 10)
    await client.delete(f"/api/jobs/{job_id}")
    release.set()
    await dat_rename.drain()

    job = (await client.get(f"/api/jobs/{job_id}")).json()
    result = _result(job)[str(game)]
    assert result["status"] == "not_renamed", result
    assert romm.roms[ROM_ID]["fs_name"] == "game.iso"
    assert (platform / "games.m3u").read_text() == "game.iso\n"
    # After the second rename (the put-back), RomM was read back.
    calls = [(m, p.split("?", 1)[0]) for m, p in romm.requests]
    second_put = [i for i, c in enumerate(calls) if c[0] == "PUT"][1]
    assert ("GET", f"/api/roms/{ROM_ID}") in calls[second_put + 1:], calls
