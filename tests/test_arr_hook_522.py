"""The *arr webhook and "scan this path now" (#522).

Sonarr, Radarr and ROMarr POST their webhook JSON to ``/api/hooks/arr``;
scripts POST ``{"path": ...}`` to ``/api/scan``. Each path is rewritten
(Autoscan's from/to), checked inside a Volume, and handed to the Inbox that
holds it, which takes it on its next poll without waiting out the settle.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from . import test_inbox_431 as t431

_vol = t431._vol
_db_fixture = t431._db_fixture
CONFIRM = {"X-Chd-Action-Confirm": "scan"}


# ---- reading the payload ---------------------------------------------------


@pytest.mark.parametrize("body, expected", [
    # Sonarr: one episode, and a season pack.
    ({"eventType": "Download", "episodeFile": {"path": "/tv/a.mkv"}}, ["/tv/a.mkv"]),
    ({"eventType": "Download", "episodeFiles": [{"path": "/tv/a"}, {"path": "/tv/b"}]},
     ["/tv/a", "/tv/b"]),
    # Radarr.
    ({"eventType": "Download", "movieFile": {"path": "/m/x.iso"}}, ["/m/x.iso"]),
    # ROMarr, or anything shaped like the *arrs: any *File / *Files.
    ({"eventType": "Download", "romFile": {"path": "/r/g.iso"}}, ["/r/g.iso"]),
    ({"eventType": "Import", "gameFiles": [{"path": "/r/g.cue"}]}, ["/r/g.cue"]),
    # A script.
    ({"path": "/d/g.iso"}, ["/d/g.iso"]),
    ({"paths": ["/d/a.iso", "/d/a.iso", "/d/b.iso"]}, ["/d/a.iso", "/d/b.iso"]),
    # Nothing a file.
    ({"eventType": "Download", "movieFile": {"size": 3}}, []),
])
def test_the_paths_are_read_from_every_arr_shape(body, expected):
    from services.inbox import trigger

    assert trigger.paths_in(body) == expected


@pytest.mark.parametrize("event", ["Grab", "Health", "Rename", "MovieDelete"])
def test_events_that_import_nothing_are_ignored(event):
    from services.inbox import trigger

    assert not trigger.imports(
        {"eventType": event, "movieFile": {"path": "/m/x.iso"}},
    )


# ---- rewriting --------------------------------------------------------------


def test_the_longest_matching_rewrite_wins_on_whole_folders_only():
    from services.inbox import trigger

    rules = [
        {"from": "/downloads", "to": "/data/downloads"},
        {"from": "/downloads/roms", "to": "/vol/roms-in"},
    ]
    assert trigger.rewrite("/downloads/roms/ps2/g.iso", rules) == "/vol/roms-in/ps2/g.iso"
    assert trigger.rewrite("/downloads/tv/a.mkv", rules) == "/data/downloads/tv/a.mkv"
    assert trigger.rewrite("/downloads2/g.iso", rules) == "/downloads2/g.iso"
    assert trigger.rewrite("/downloads", rules) == "/data/downloads"


@pytest.mark.parametrize("rules", [
    [{"from": "downloads", "to": "/data"}],
    [{"from": "/downloads", "to": "data"}],
    [{"from": "/a/../b", "to": "/data"}],
    [{"from": "/", "to": "/data"}],
    [{"from": "/a", "to": "/x"}, {"from": "/a/", "to": "/y"}],
])
def test_a_bad_rewrite_is_refused(rules):
    from services.inbox import trigger

    with pytest.raises(ValueError):
        trigger.checked_rewrites(rules)


def test_a_rewrite_is_suggested_from_the_volumes(vol):
    from services.inbox import trigger

    (vol / "downloads" / "roms").mkdir(parents=True)
    (vol / "downloads" / "roms" / "g.iso").write_bytes(b"x")

    suggestion = trigger.suggest("/downloads/roms/g.iso")

    assert suggestion == {"from": "/downloads", "to": str(vol / "downloads")}
    assert trigger.suggest("/nowhere/at/all.iso") is None


# ---- intake ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_path_marked_ready_is_taken_on_the_first_poll(vol, db):
    from services.inbox import intake

    library = t431._library(vol)
    box = t431._inbox(vol, library, settle=3600)
    game = t431._drop(vol / "Downloads" / "ps2" / "Game.iso")
    other = t431._drop(vol / "Downloads" / "ps2" / "Other.iso")

    intake.mark_ready(box.id, str(game))
    await t431._poll(box, now=0)

    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()
    assert not game.exists()
    # Only what was named skips the settle.
    assert other.exists()
    assert not (vol / "ROMs" / "ps2" / "Other.iso").exists()


@pytest.mark.asyncio
async def test_a_folder_marked_ready_takes_everything_below_it(vol, db):
    from services.inbox import intake

    library = t431._library(vol)
    box = t431._inbox(vol, library, settle=3600)
    t431._drop(vol / "Downloads" / "ps2" / "A.iso")
    t431._drop(vol / "Downloads" / "ps2" / "B.iso")

    intake.mark_ready(box.id, str(vol / "Downloads" / "ps2"))
    await t431._poll(box, now=0)

    assert (vol / "ROMs" / "ps2" / "A.iso").exists()
    assert (vol / "ROMs" / "ps2" / "B.iso").exists()


# ---- the routes ------------------------------------------------------------


def _app() -> FastAPI:
    from routes import hooks

    app = FastAPI()
    app.include_router(hooks.router, prefix="/api")
    return app


@pytest_asyncio.fixture(name="client")
async def _client(db):
    from services.inbox import trigger

    trigger.reset_for_tests()
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await trigger.drain()
    trigger.reset_for_tests()


@pytest.mark.asyncio
async def test_the_test_event_answers_ok(client):
    reply = await client.post("/api/hooks/arr", json={"eventType": "Test"}, headers=CONFIRM)

    assert reply.status_code == 200
    assert reply.json()["test"] is True


@pytest.mark.asyncio
async def test_the_confirm_header_is_required_even_for_the_test(client):
    for url, body in (("/api/hooks/arr", {"eventType": "Test"}), ("/api/scan", {"path": "/x"})):
        reply = await client.post(url, json=body)
        assert reply.status_code == 400
        assert "confirmation header" in reply.json()["detail"]


@pytest.mark.asyncio
async def test_a_sonarr_import_is_rewritten_and_taken_at_once(client, vol):
    from services.inbox import intake

    library = t431._library(vol)
    box = t431._inbox(vol, library, settle=3600)
    game = t431._drop(vol / "Downloads" / "ps2" / "Game.iso")
    saved = await client.put(
        "/api/hooks/arr/rewrites",
        json={"rewrites": [{"from": "/downloads", "to": str(vol / "Downloads")}]},
    )
    assert saved.status_code == 200

    reply = await client.post("/api/hooks/arr", headers=CONFIRM, json={
        "eventType": "Download", "episodeFile": {"path": "/downloads/ps2/Game.iso"},
    })

    assert reply.status_code == 202
    [taken] = reply.json()["accepted"]
    assert taken == {"path": "/downloads/ps2/Game.iso", "local": str(game), "inbox_id": box.id}
    from services.inbox import trigger

    await trigger.drain()
    await intake.drain()
    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()
    recent = (await client.get("/api/hooks/arr")).json()["recent"]
    assert recent[0]["path"] == "/downloads/ps2/Game.iso"
    assert recent[0]["inbox_id"] == box.id


@pytest.mark.asyncio
async def test_a_rewrite_out_of_the_volumes_is_refused(client, vol, tmp_path):
    library = t431._library(vol)
    t431._inbox(vol, library)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "g.iso").write_bytes(b"x")
    await client.put(
        "/api/hooks/arr/rewrites", json={"rewrites": [{"from": "/d", "to": str(outside)}]},
    )

    reply = await client.post("/api/scan", headers=CONFIRM, json={"path": "/d/g.iso"})

    body = reply.json()
    assert body["accepted"] == []
    assert "Volume" in body["skipped"][0]["problem"]
    assert (outside / "g.iso").exists()


@pytest.mark.asyncio
async def test_a_path_in_no_inbox_is_skipped_with_why_and_a_suggestion(client, vol):
    library = t431._library(vol)
    t431._inbox(vol, library)
    t431._drop(vol / "Downloads" / "g.iso")

    reply = await client.post("/api/hooks/arr/check", json={"path": "/Downloads/g.iso"})

    body = reply.json()
    assert body["inbox_id"] is None
    assert body["problem"]
    assert body["suggestion"] == {"from": "/Downloads", "to": str(vol / "Downloads")}


@pytest.mark.asyncio
async def test_the_check_names_the_inbox_for_a_good_path(client, vol):
    library = t431._library(vol)
    box = t431._inbox(vol, library)
    game = t431._drop(vol / "Downloads" / "g.iso")

    body = (await client.post("/api/hooks/arr/check", json={"path": str(game)})).json()

    assert body == {
        "path": str(game), "local": str(game), "inbox_id": box.id,
        "library_id": library.id, "problem": None, "suggestion": None,
    }


@pytest.mark.asyncio
async def test_a_poll_already_running_is_waited_for_not_dropped(vol, db, monkeypatch):
    from services.inbox import intake, trigger

    library = t431._library(vol)
    box = t431._inbox(vol, library, settle=3600)
    t431._drop(vol / "Downloads" / "ps2" / "Game.iso")
    monkeypatch.setattr(trigger, "_RETRY_SECONDS", 0.01)
    intake._state(box.id).polling = True

    async def release():
        await asyncio.sleep(0.05)
        intake._state(box.id).polling = False

    trigger.reset_for_tests()
    result = await trigger.process(
        [str(vol / "Downloads" / "ps2" / "Game.iso")], source="scan",
    )
    await asyncio.gather(release(), trigger.drain())
    await intake.drain()

    assert result["accepted"]
    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()
    assert Path(vol / "Downloads" / "processed").exists()


@pytest.mark.asyncio
async def test_naming_a_sheets_track_takes_the_whole_sheet(vol, db):
    from services.inbox import intake

    library = t431._library(vol)
    box = t431._inbox(vol, library, settle=3600)
    t431._drop(
        vol / "Downloads" / "psx" / "Game.cue",
        b'FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n',
    )
    track = t431._drop(vol / "Downloads" / "psx" / "Game.bin")

    intake.mark_ready(box.id, str(track))
    await t431._poll(box, now=0)

    assert (vol / "ROMs" / "psx" / "Game.cue").exists()
    assert (vol / "ROMs" / "psx" / "Game.bin").exists()


@pytest.mark.asyncio
async def test_a_folder_marked_ready_does_not_hurry_files_written_after(vol, db):
    """Codex PR #526: the folder's marker covers what was finished when it
    was named; a download still being written there afterwards settles as
    usual."""
    import os
    import time

    from services.inbox import intake

    library = t431._library(vol)
    box = t431._inbox(vol, library, settle=3600)
    t431._drop(vol / "Downloads" / "ps2" / "A.iso")
    intake.mark_ready(box.id, str(vol / "Downloads" / "ps2"))
    late = t431._drop(vol / "Downloads" / "ps2" / "Late.iso")
    later = time.time() + 60
    os.utime(late, (later, later))

    await t431._poll(box, now=0)

    assert (vol / "ROMs" / "ps2" / "A.iso").exists()
    assert late.exists()
    assert not (vol / "ROMs" / "ps2" / "Late.iso").exists()


@pytest.mark.asyncio
async def test_two_sheets_named_by_their_tracks_are_both_taken_at_once(vol, db):
    """Codex PR #526: matching one sheet consumes only its own tracks'
    markers."""
    from services.inbox import intake

    library = t431._library(vol)
    box = t431._inbox(vol, library, settle=3600)
    tracks = []
    for name in ("A", "B"):
        t431._drop(
            vol / "Downloads" / "psx" / f"{name}.cue",
            f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'.encode(),
        )
        tracks.append(t431._drop(vol / "Downloads" / "psx" / f"{name}.bin"))
    for track in tracks:
        intake.mark_ready(box.id, str(track))

    await t431._poll(box, now=0)

    assert (vol / "ROMs" / "psx" / "A.cue").exists()
    assert (vol / "ROMs" / "psx" / "B.cue").exists()


@pytest.mark.asyncio
async def test_a_folder_marked_ready_waits_for_a_sheets_late_track(vol, db):
    """Codex PR #526: every file of the Image must predate the marker, not
    only the sheet."""
    import os
    import time

    from services.inbox import intake

    library = t431._library(vol)
    box = t431._inbox(vol, library, settle=3600)
    t431._drop(
        vol / "Downloads" / "psx" / "Game.cue",
        b'FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n',
    )
    track = t431._drop(vol / "Downloads" / "psx" / "Game.bin")
    intake.mark_ready(box.id, str(vol / "Downloads" / "psx"))
    later = time.time() + 60
    os.utime(track, (later, later))

    await t431._poll(box, now=0)

    assert not (vol / "ROMs" / "psx" / "Game.cue").exists()
    assert track.exists()


@pytest.mark.asyncio
async def test_taking_a_path_in_reads_no_folder_on_the_event_loop(vol, db, monkeypatch):
    """Codex PR #526: the marker's path is resolved in the bounded check,
    never on the event loop."""
    import os
    import threading

    from services.inbox import trigger

    library = t431._library(vol)
    t431._inbox(vol, library, settle=3600)
    game = t431._drop(vol / "Downloads" / "ps2" / "Game.iso")
    main = threading.current_thread()
    real = os.path.realpath
    on_loop: list[str] = []

    def watched(path, *args, **kwargs):
        if threading.current_thread() is main:
            on_loop.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", watched)
    result = await trigger.process([str(game)], source="test")
    monkeypatch.setattr(os.path, "realpath", real)

    assert result["accepted"]
    assert on_loop == []


@pytest.mark.asyncio
async def test_a_file_replaced_after_its_marker_settles_as_usual(vol, db):
    """Codex PR #526: the folder and the file were both named; once the file
    is taken, a new download at the same name is not hurried by either."""
    import os
    import time

    from services.inbox import intake

    library = t431._library(vol)
    box = t431._inbox(vol, library, settle=3600)
    game = t431._drop(vol / "Downloads" / "ps2" / "Game.iso")
    intake.mark_ready(box.id, str(vol / "Downloads" / "ps2"))
    intake.mark_ready(box.id, str(game))
    await t431._poll(box, now=0)
    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()
    (vol / "ROMs" / "ps2" / "Game.iso").unlink()  # so only readiness decides

    again = t431._drop(vol / "Downloads" / "ps2" / "Game.iso", b"a new download")
    later = time.time() + 60
    os.utime(again, (later, later))
    await t431._poll(box, now=1)

    assert again.exists()
