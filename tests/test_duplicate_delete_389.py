"""The duplicate finder's keeper and delete (#389).

Each duplicate group (#388) gets a suggested keeper (Intact first, then
Recognized, then the smallest) with the reason. The other copies can be
deleted, and only through the existing delete plan: the plan lists every file
that would go, the delete needs the confirmation header and the very list the
plan showed, and it runs the job's own delete step under its locks. Every
copy is checked again when the delete is confirmed, against what the Finding
showed: one gone, changed or no longer holding the group's key refuses it.
Afterwards the Finding shrinks, or clears once one copy is left.

Driven through the Findings routes, as #388's tests are.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import httpx
import pytest

from . import test_duplicates_388 as d388
from . import test_folder_library_303 as folder_library_303

_db_fixture = folder_library_303._db_fixture
_client = d388._client

CONFIRM = {"X-CHD-Action-Confirm": "delete-file"}


async def _group(client: httpx.AsyncClient) -> dict:
    found = await d388._duplicates(client)
    assert len(found) == 1, found
    return found[0]


async def _plan(client: httpx.AsyncClient, group: dict, keep: str) -> httpx.Response:
    return await client.post(
        f"/api/findings/{group['id']}/delete-plan",
        json={"fingerprint": group["fingerprint"], "keep": keep},
    )


async def _delete(
    client: httpx.AsyncClient, group: dict, keep: str, delete_paths: list[str],
    *, headers: dict | None = CONFIRM,
) -> httpx.Response:
    return await client.post(
        f"/api/findings/{group['id']}/delete",
        json={"fingerprint": group["fingerprint"], "keep": keep, "delete_paths": delete_paths},
        headers=headers or {},
    )


def _paths(plan: dict) -> list[str]:
    return sorted(p for item in plan["items"] for p in item["delete_paths"])


# ----------------------------------------------------------------------
# the suggested keeper
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_smallest_copy_is_suggested_when_none_is_checked(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    sha1 = d388._disc(roms / "Game.iso", b"game")
    await d388._seed_chd(roms / "Game.chd", data_sha1=sha1, sha1="0" * 40)
    library = await d388._register(client, roms)
    await d388._find(client, library)

    group = await _group(client)
    assert group["keeper"] == str(roms / "Game.chd")
    assert "smallest" in group["keeper_reason"]
    assert group["deletable"] is True


@pytest.mark.asyncio
async def test_an_intact_copy_comes_before_a_smaller_one(
    db: Path, client: httpx.AsyncClient,
) -> None:
    from services.verification_store import verification_store

    roms = db / "roms"
    sha1 = d388._disc(roms / "Game.iso", b"game")
    await d388._seed_chd(roms / "Game.chd", data_sha1=sha1, sha1="0" * 40)
    await verification_store.mark_verified(str(roms / "Game.iso"))
    library = await d388._register(client, roms)
    await d388._find(client, library)

    group = await _group(client)
    assert group["keeper"] == str(roms / "Game.iso")
    assert "Intact" in group["keeper_reason"]


@pytest.mark.asyncio
async def test_a_recognized_copy_comes_before_a_smaller_one(
    db: Path, client: httpx.AsyncClient,
) -> None:
    from services.dat_store import dat_store

    roms = db / "roms"
    sha1 = d388._disc(roms / "Game.iso", b"game")
    await d388._seed_chd(roms / "Game.chd", data_sha1=sha1, sha1="0" * 40)
    await dat_store.set_match(str(roms / "Game.iso"), {
        "path": str(roms / "Game.iso"), "matched": True, "game_name": "Game",
        "match_type": "file_sha1", "file_hash": sha1,
    })
    library = await d388._register(client, roms)
    await d388._find(client, library)

    group = await _group(client)
    assert group["keeper"] == str(roms / "Game.iso")
    assert "Recognized" in group["keeper_reason"]


# ----------------------------------------------------------------------
# the delete: plan, confirmation, then the job's own delete step
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_other_copies_go_only_after_the_confirmed_plan(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    d388._copy(roms / "Game.iso", roms / "backup" / "Third.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")

    response = await _plan(client, group, keep)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["blocked"] is False
    assert plan["keep"] == keep
    assert _paths(plan) == sorted([
        os.path.realpath(roms / "Copy.iso"), os.path.realpath(roms / "backup" / "Third.iso"),
    ])
    # The plan alone deletes nothing.
    assert (roms / "Copy.iso").exists()

    # No confirmation header: refused, nothing deleted.
    refused = await _delete(client, group, keep, _paths(plan), headers={})
    assert refused.status_code == 400
    assert (roms / "Copy.iso").exists()
    # Not the list the plan showed: refused.
    partial = await _delete(client, group, keep, _paths(plan)[:1])
    assert partial.status_code == 409
    assert (roms / "Copy.iso").exists()

    done = await _delete(client, group, keep, _paths(plan))
    assert done.status_code == 200, done.text
    assert {r["status"] for r in done.json()["results"]} == {"deleted"}
    assert (roms / "Game.iso").exists()
    assert not (roms / "Copy.iso").exists()
    assert not (roms / "backup" / "Third.iso").exists()
    # One copy left: no duplicate any more.
    assert await d388._duplicates(client) == []


@pytest.mark.asyncio
async def test_a_copy_that_cannot_go_now_keeps_its_place_in_the_finding(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """A copy a job holds is kept (the delete step's own lock check); the
    others go, and the Finding shows what is left."""
    from services.lock_manager import lock_manager

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    d388._copy(roms / "Game.iso", roms / "Held.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()

    assert lock_manager.acquire_lock(str(roms / "Held.iso"), allow_existing=True)
    try:
        done = await _delete(client, group, keep, _paths(plan))
    finally:
        lock_manager.release_lock(str(roms / "Held.iso"))

    assert done.status_code == 200, done.text
    statuses = {os.path.basename(r["path"]): r["status"] for r in done.json()["results"]}
    assert statuses == {"Copy.iso": "deleted", "Held.iso": "reader"}
    assert not (roms / "Copy.iso").exists()
    assert (roms / "Held.iso").exists()
    after = await _group(client)
    assert d388._names(after) == ["Game.iso", "Held.iso"]


@pytest.mark.asyncio
async def test_a_copy_changed_since_the_finding_refuses_the_delete(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()

    # Another disc in its place, after the plan was shown.
    d388._disc(roms / "Copy.iso", b"another disc")
    done = await _delete(client, group, keep, _paths(plan))

    assert done.status_code == 409, done.text
    assert (roms / "Copy.iso").exists()


@pytest.mark.asyncio
async def test_a_keeper_gone_or_changed_refuses_the_delete(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()

    d388._disc(roms / "Game.iso", b"keeper changed")
    done = await _delete(client, group, keep, _paths(plan))

    assert done.status_code == 409, done.text
    assert (roms / "Copy.iso").exists()


@pytest.mark.asyncio
async def test_a_finding_that_changed_since_it_was_shown_is_refused(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    stale = {**group, "fingerprint": "0" * 32}

    assert (await _plan(client, stale, str(roms / "Game.iso"))).status_code == 409
    assert (await _delete(
        client, stale, str(roms / "Game.iso"), [os.path.realpath(roms / "Copy.iso")],
    )).status_code == 409
    assert (roms / "Copy.iso").exists()


@pytest.mark.asyncio
async def test_the_keeper_must_be_one_of_the_copies(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)

    assert (await _plan(client, group, str(roms / "Other.iso"))).status_code == 400


# ----------------------------------------------------------------------
# what is never offered
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(folder_library_303._IS_ROOT, reason="root reads every folder")
async def test_a_held_group_is_not_offered_for_delete(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """A group the last run kept as it was (a copy it could not see) is not
    proven now: no delete."""
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "sub" / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    (roms / "sub").chmod(0)
    try:
        await d388._find(client, library)
        group = await _group(client)
        plan = await _plan(client, group, str(roms / "Game.iso"))
    finally:
        (roms / "sub").chmod(0o755)

    assert group["deletable"] is False
    assert group["not_deletable"]
    assert plan.status_code == 409
    assert (roms / "sub" / "Copy.iso").exists()


@pytest.mark.asyncio
async def test_a_group_that_is_not_hash_equal_is_not_offered(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """Only a content-hash key proves the copies equal; any other kind of
    group (a "possibly the same disc" one, later) is never deleted from."""
    from fastapi.concurrency import run_in_threadpool
    from services import findings

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._disc(roms / "Other.iso", b"other")
    library = await d388._register(client, roms)
    copies = [
        {"image_key": name, "path": str(roms / name), "container": ".iso",
         "size": (roms / name).stat().st_size,
         "mtime_ns": (roms / name).stat().st_mtime_ns}
        for name in ("Game.iso", "Other.iso")
    ]
    await run_in_threadpool(findings.observe, library, findings.Observed(
        kind=findings.DUPLICATE, image_key="name:game", path=copies[0]["path"],
        reason="2 copies named alike", cause={"key": "name:game", findings.COPIES: copies},
    ))

    group = await _group(client)
    assert group["deletable"] is False
    assert (await _plan(client, group, copies[0]["path"])).status_code == 409


@pytest.mark.asyncio
async def test_a_track_of_a_cue_is_never_offered_on_its_own(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """A `.bin` a sheet beside it lists is that disc's track: deleting it
    alone would break the disc, so the plan is blocked."""
    roms = db / "romm"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Disc (Track 1).bin")
    (roms / "Disc.cue").write_text(
        'FILE "Disc (Track 1).bin" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n',
    )
    release = threading.Event()
    release.set()
    d388._romm_images_resolving(
        monkeypatch, release, [roms / "Game.iso", roms / "Disc (Track 1).bin"],
    )
    await d388._find(client, "romm")
    group = await _group(client)

    response = await _plan(client, group, str(roms / "Game.iso"))
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["blocked"] is True
    errors = [e for item in plan["items"] for e in item["errors"]]
    assert any("Disc.cue" in e for e in errors), errors
    assert (await _delete(
        client, group, str(roms / "Game.iso"), _paths(plan),
    )).status_code == 409
    assert (roms / "Disc (Track 1).bin").exists()


@pytest.mark.asyncio
async def test_a_playlist_naming_a_deleted_copy_is_pointed_at_the_keeper(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """The delete step's Referrer hook runs as for a conversion: the plan
    lists the playlist change, and the playlist names the keeper afterwards."""
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    (roms / "Game.m3u").write_text("Copy.iso\n")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")

    plan = (await _plan(client, group, keep)).json()
    changes = [c for item in plan["items"] for c in item["referrer_changes"]]
    assert [os.path.basename(c["target"]) for c in changes] == ["Game.m3u"]

    done = await _delete(client, group, keep, _paths(plan))
    assert done.status_code == 200, done.text
    assert (roms / "Game.m3u").read_text().splitlines() == ["Game.iso"]
    assert not (roms / "Copy.iso").exists()
