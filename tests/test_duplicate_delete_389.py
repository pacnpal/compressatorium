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

import asyncio
import functools
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


@pytest.fixture(autouse=True)
def _held_file_paths_in_process(monkeypatch):
    """A path naming a held descriptor, for this process's stubbed tools.

    Production resolves ``/proc/<pid>/fd/<fd>`` only, since a tool's child must
    be able to open it (Linux). The tool stubs here run in this process, so on
    a system without ``/proc`` (macOS) ``/dev/fd/<fd>`` serves them, checked by
    file identity the same way.
    """
    from services.tools import base

    real = base.held_file_path

    def in_process(fd: int) -> str | None:
        found = real(fd)
        if found is not None:
            return found
        candidate = f"/dev/fd/{fd}"
        try:
            held, named = os.fstat(fd), os.stat(candidate)
        except OSError:
            return None
        # macOS's devfs reports its own device for /dev/fd; the inode and
        # size are the held file's.
        same = (held.st_ino, held.st_size) == (named.st_ino, named.st_size)
        return candidate if same else None

    monkeypatch.setattr(base, "held_file_path", in_process)


async def _group(client: httpx.AsyncClient) -> dict:
    found = await d388._duplicates(client)
    assert len(found) == 1, found
    return found[0]


async def _plan(client: httpx.AsyncClient, group: dict, keep: str) -> httpx.Response:
    return await client.post(
        f"/api/findings/{group['id']}/delete-plan",
        json={"fingerprint": group["fingerprint"], "keep": keep},
    )


class _Finished:
    """What a confirmed delete came to: the route's answer when it refused to
    start, else the finished delete job's. A job that completed is 200 with
    its result; one that refused the delete before deleting anything (blocked,
    changed, cancelled) is 409 with its reason."""

    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code, self._body = status_code, body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


async def _start_delete(
    client: httpx.AsyncClient, group: dict, keep: str, delete_paths: list[str],
    *, headers: dict | None = CONFIRM,
) -> httpx.Response:
    return await client.post(
        f"/api/findings/{group['id']}/delete",
        json={"fingerprint": group["fingerprint"], "keep": keep, "delete_paths": delete_paths},
        headers=headers or {},
    )


async def _finished_job(client: httpx.AsyncClient, job_id: str) -> dict:
    from services import duplicate_cleanup

    await duplicate_cleanup.drain()
    response = await client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _delete(
    client: httpx.AsyncClient, group: dict, keep: str, delete_paths: list[str],
    *, headers: dict | None = CONFIRM,
) -> _Finished:
    started = await _start_delete(client, group, keep, delete_paths, headers=headers)
    if started.status_code != 200:
        return _Finished(started.status_code, started.json())
    job = await _finished_job(client, started.json()["job_id"])
    if job["status"] == "completed":
        return _Finished(200, job["result"])
    return _Finished(409, {"detail": job["error_message"] or job["message"], "job": job})


def _paths(plan: dict) -> list[str]:
    return sorted(p for item in plan["items"] for p in item["delete_paths"])


# ----------------------------------------------------------------------
# the suggested keeper
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_smallest_copy_is_suggested_when_none_is_checked(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Among copies whose key was read from their whole content (an ISO's own
    SHA1, an RVZ's rebuilt disc SHA1), the smallest."""
    roms = db / "roms"
    sha1 = d388._disc(roms / "Game.iso", b"game")
    (roms / "Game.rvz").write_bytes(b"RVZ\x01" + os.urandom(128))
    d388._dolphin_answers(monkeypatch, {"Game.rvz": sha1})
    library = await d388._register(client, roms)
    await d388._find(client, library)

    group = await _group(client)
    assert group["keeper"] == str(roms / "Game.rvz")
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
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services.dat_store import dat_store

    roms = db / "roms"
    sha1 = d388._disc(roms / "Game.iso", b"game")
    (roms / "Game.rvz").write_bytes(b"RVZ\x01" + os.urandom(128))
    d388._dolphin_answers(monkeypatch, {"Game.rvz": sha1})
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



# ----------------------------------------------------------------------
# a track whose sheet cannot be read (Codex PRRT_kwDOQ9nULM64Lh-c)
# ----------------------------------------------------------------------


async def _track_group(client, monkeypatch, roms: Path) -> dict:
    """``Game.iso`` and ``Disc (Track 1).bin``, one disc, listed by RomM."""
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Disc (Track 1).bin")
    release = threading.Event()
    release.set()
    d388._romm_images_resolving(
        monkeypatch, release, [roms / "Game.iso", roms / "Disc (Track 1).bin"],
    )
    await d388._find(client, "romm")
    return await _group(client)


async def _blocked_for(client, group: dict, keep: str, words: str) -> None:
    response = await _plan(client, group, keep)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["blocked"] is True
    errors = [e for item in plan["items"] for e in item["errors"]]
    assert any(words in e for e in errors), errors
    assert (await _delete(client, group, keep, _paths(plan))).status_code == 409


@pytest.mark.asyncio
@pytest.mark.skipif(folder_library_303._IS_ROOT, reason="root reads every file")
async def test_a_sheet_that_cannot_be_read_blocks_the_delete(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Whether an unreadable sheet needs the file is unknown: not standalone."""
    roms = db / "romm"
    group = await _track_group(client, monkeypatch, roms)
    sheet = roms / "Disc.cue"
    sheet.write_text('FILE "Disc (Track 1).bin" BINARY\n  TRACK 01 MODE1/2048\n')
    sheet.chmod(0)
    try:
        await _blocked_for(client, group, str(roms / "Game.iso"), "Disc.cue could not be read")
    finally:
        sheet.chmod(0o644)
    assert (roms / "Disc (Track 1).bin").exists()


@pytest.mark.asyncio
async def test_a_sheet_too_large_to_read_blocks_the_delete(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from utils.sheet_text import MAX_SHEET_BYTES

    roms = db / "romm"
    group = await _track_group(client, monkeypatch, roms)
    (roms / "Disc.cue").write_text(
        'FILE "Disc (Track 1).bin" BINARY\n' + "REM x\n" * (MAX_SHEET_BYTES // 6 + 1),
    )
    await _blocked_for(client, group, str(roms / "Game.iso"), "Disc.cue could not be read")
    assert (roms / "Disc (Track 1).bin").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(folder_library_303._IS_ROOT, reason="root lists every folder")
async def test_a_folder_that_cannot_be_listed_blocks_the_delete(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """With the folder unlisted, no sheet in it can be ruled out."""
    roms = db / "romm"
    group = await _track_group(client, monkeypatch, roms)
    roms.chmod(0o311)   # files reachable, the listing refused
    try:
        await _blocked_for(client, group, str(roms / "Game.iso"), "could not be listed")
    finally:
        roms.chmod(0o755)
    assert (roms / "Disc (Track 1).bin").exists()


# ----------------------------------------------------------------------
# a keeper that is not proven (Codex PRRT_kwDOQ9nULM64Lh-l)
# ----------------------------------------------------------------------


async def _chd_and_iso(client, roms: Path) -> tuple[dict, str]:
    """A DVD CHD keyed from its header, smaller than the ISO it equals."""
    sha1 = d388._disc(roms / "Game.iso", b"game")
    await d388._seed_chd(roms / "Game.chd", data_sha1=sha1, sha1="0" * 40)
    library = await d388._register(client, roms)
    await d388._find(client, library)
    return await _group(client), str(roms / "Game.chd")


@pytest.mark.asyncio
async def test_a_header_keyed_copy_never_checked_is_no_keeper(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """Its key is what its header claims, not what its payload decodes to: it
    is not suggested, and keeping it is refused until it passes a check."""
    roms = db / "roms"
    group, chd = await _chd_and_iso(client, roms)
    assert group["keeper"] == str(roms / "Game.iso")

    response = await _plan(client, group, chd)
    assert response.status_code == 409, response.text
    assert "check it first" in response.json()["detail"]
    assert (await _delete(
        client, group, chd, [os.path.realpath(roms / "Game.iso")],
    )).status_code == 409
    assert (roms / "Game.iso").exists()


@pytest.mark.asyncio
async def test_a_copy_that_failed_its_check_is_never_a_keeper(
    db: Path, client: httpx.AsyncClient,
) -> None:
    from services.verification_store import verification_store

    roms = db / "roms"
    sha1 = d388._disc(roms / "Game.iso", b"game")
    await d388._seed_chd(roms / "Game.chd", data_sha1=sha1, sha1="0" * 40)
    await verification_store.mark_verified(str(roms / "Game.chd"))
    await verification_store.mark_failed(str(roms / "Game.chd"))
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)

    assert group["keeper"] == str(roms / "Game.iso")
    response = await _plan(client, group, str(roms / "Game.chd"))
    assert response.status_code == 409
    assert "failed its check" in response.json()["detail"]
    assert (roms / "Game.iso").exists()


@pytest.mark.asyncio
async def test_a_header_keyed_copy_that_passed_its_check_may_be_kept(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Kept only after a full pass under its lock: chdman's verify decodes it
    and the header it then reads must be the group's key (Codex 4593004806)."""
    from services.chdman import chdman_service
    from services.tools import registry
    from services.verification_store import verification_store

    roms = db / "roms"
    sha1 = d388._disc(roms / "Game.iso", b"game")
    await d388._seed_chd(roms / "Game.chd", data_sha1=sha1, sha1="0" * 40)
    await verification_store.mark_verified(str(roms / "Game.chd"))
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    chd = str(roms / "Game.chd")
    assert group["keeper"] == chd and "Intact" in group["keeper_reason"]
    passes: list[str] = []

    async def verify(path, *, cancel_event=None):
        # The pass reads the held file (Codex 4593632748), named by identity.
        passes.append(os.stat(path).st_ino)
        return {"valid": True, "message": "ok"}

    async def info(path, *, cancel_event=None):
        return {"sha1": "0" * 40, "data_sha1": sha1,
                "raw_data": "Metadata: Tag='DVD '  Index=0\n"}

    monkeypatch.setattr(registry.get("chdman"), "verify", verify)
    monkeypatch.setattr(chdman_service, "info", info)

    plan = (await _plan(client, group, chd)).json()
    assert plan["blocked"] is False
    done = await _delete(client, group, chd, _paths(plan))
    assert done.status_code == 200, done.text
    assert passes == [os.stat(roms / "Game.chd").st_ino]
    assert not (roms / "Game.iso").exists()
    assert (roms / "Game.chd").exists()


@pytest.mark.asyncio
async def test_a_kept_chd_that_fails_its_full_pass_deletes_nothing(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services.tools import registry
    from services.verification_store import verification_store

    roms = db / "roms"
    sha1 = d388._disc(roms / "Game.iso", b"game")
    await d388._seed_chd(roms / "Game.chd", data_sha1=sha1, sha1="0" * 40)
    await verification_store.mark_verified(str(roms / "Game.chd"))
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    chd = str(roms / "Game.chd")

    async def verify(path, *, cancel_event=None):
        return {"valid": False, "message": "hunk 12 is damaged"}

    monkeypatch.setattr(registry.get("chdman"), "verify", verify)
    plan = (await _plan(client, group, chd)).json()
    done = await _delete(client, group, chd, _paths(plan))

    assert done.status_code == 200, done.text
    assert all(r["status"] != "deleted" for r in done.json()["results"])
    assert any("could not be read in full" in (r["reason"] or "") for r in done.json()["results"])
    assert (roms / "Game.iso").exists()


# ----------------------------------------------------------------------
# a track a sheet further up names (Codex PRRT_kwDOQ9nULM64L3Le)
# ----------------------------------------------------------------------


async def _nested_track_group(client, monkeypatch, roms: Path, track: str) -> dict:
    """``Game.iso`` and the track at *track* (relative to *roms*), one disc."""
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / track)
    release = threading.Event()
    release.set()
    d388._romm_images_resolving(monkeypatch, release, [roms / "Game.iso", roms / track])
    await d388._find(client, "romm")
    return await _group(client)


def _cue(path: Path, track: str) -> None:
    path.write_text(f'FILE "{track}" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n')


@pytest.mark.asyncio
async def test_a_sheet_one_folder_up_naming_the_track_blocks_it(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    roms = db / "romm"
    group = await _nested_track_group(client, monkeypatch, roms, "tracks/Track.bin")
    _cue(roms / "Disc.cue", "tracks/Track.bin")

    await _blocked_for(client, group, str(roms / "Game.iso"), "is a track of Disc.cue")
    assert (roms / "tracks" / "Track.bin").exists()


@pytest.mark.asyncio
async def test_a_sheet_two_folders_up_naming_the_track_blocks_it(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    roms = db / "romm"
    group = await _nested_track_group(client, monkeypatch, roms, "a/b/Track.bin")
    _cue(roms / "Disc.cue", "a/b/Track.bin")

    await _blocked_for(client, group, str(roms / "Game.iso"), "is a track of Disc.cue")
    assert (roms / "a" / "b" / "Track.bin").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(folder_library_303._IS_ROOT, reason="root reads every file")
async def test_an_unreadable_sheet_further_up_blocks_the_track(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    roms = db / "romm"
    group = await _nested_track_group(client, monkeypatch, roms, "tracks/Track.bin")
    sheet = roms / "Other.cue"
    _cue(sheet, "Other.bin")
    sheet.chmod(0)
    try:
        await _blocked_for(client, group, str(roms / "Game.iso"), "Other.cue could not be read")
    finally:
        sheet.chmod(0o644)
    assert (roms / "tracks" / "Track.bin").exists()


@pytest.mark.asyncio
async def test_a_sheet_further_up_naming_other_files_does_not_block(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    roms = db / "romm"
    group = await _nested_track_group(client, monkeypatch, roms, "tracks/Track.bin")
    _cue(roms / "Other.cue", "Other.bin")
    (roms / "Other.bin").write_bytes(b"\0" * 2048)

    response = await _plan(client, group, str(roms / "Game.iso"))
    assert response.status_code == 200, response.text
    assert response.json()["blocked"] is False, response.json()


# ----------------------------------------------------------------------
# the keeper is read in full before anything goes (Codex 4593004806)
# ----------------------------------------------------------------------


async def _iso_pair(client, roms: Path) -> tuple[dict, str, dict]:
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    return group, keep, (await _plan(client, group, keep)).json()


@pytest.mark.asyncio
async def test_a_keeper_swapped_with_the_same_size_and_mtime_deletes_nothing(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """Size and mtime say nothing about bytes written back with them: the
    keeper's key is computed again from what it holds now."""
    roms = db / "roms"
    group, keep, plan = await _iso_pair(client, roms)
    keeper = roms / "Game.iso"
    st = keeper.stat()
    keeper.write_bytes(os.urandom(st.st_size))
    os.utime(keeper, ns=(st.st_atime_ns, st.st_mtime_ns))

    done = await _delete(client, group, keep, _paths(plan))

    assert (roms / "Copy.iso").exists()
    reasons = [r["reason"] or "" for r in done.json()["results"]] if done.status_code == 200 else [
        done.json()["detail"],
    ]
    assert any("no longer holds this disc" in r for r in reasons), done.text
    assert all(r["status"] != "deleted" for r in done.json().get("results", []))


@pytest.mark.asyncio
async def test_a_keeper_that_cannot_be_read_in_time_deletes_nothing(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services import content_keys

    roms = db / "roms"
    group, keep, plan = await _iso_pair(client, roms)
    release = threading.Event()
    real = content_keys._sha1_of_fd

    def stalls(fd, *, progress):
        release.wait(30)   # the keeper's read, through its held descriptor, never moves
        return real(fd, progress=progress)

    monkeypatch.setattr(content_keys, "_sha1_of_fd", stalls)
    monkeypatch.setattr(content_keys, "STALL_SECONDS", 0.3)
    try:
        done = await _delete(client, group, keep, _paths(plan))
    finally:
        release.set()

    assert done.status_code == 200, done.text
    assert all(r["status"] != "deleted" for r in done.json()["results"])
    assert any("could not be read in full" in (r["reason"] or "") for r in done.json()["results"])
    assert (roms / "Copy.iso").exists()


# ----------------------------------------------------------------------
# a sheet with a refused reference (Codex PRRT_kwDOQ9nULM64MWFq)
# ----------------------------------------------------------------------


def _cue_with_a_refused_reference(path: Path, track: str) -> None:
    """A readable sheet naming *track* and one reference outside its folder."""
    path.write_text(
        f'FILE "{track}" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n'
        'FILE "../Audio.bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n',
    )


@pytest.mark.asyncio
async def test_a_sheet_with_a_refused_reference_beside_the_track_blocks_it(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Its tracks cannot be resolved as one set, so whether it needs the file
    is unknown: never "no sheet owns it"."""
    roms = db / "romm"
    group = await _track_group(client, monkeypatch, roms)
    _cue_with_a_refused_reference(roms / "Disc.cue", "Disc (Track 1).bin")

    await _blocked_for(client, group, str(roms / "Game.iso"), "Disc.cue")
    assert (roms / "Disc (Track 1).bin").exists()


@pytest.mark.asyncio
async def test_a_sheet_with_a_refused_reference_further_up_blocks_the_track(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    roms = db / "romm"
    group = await _nested_track_group(client, monkeypatch, roms, "tracks/Track.bin")
    _cue_with_a_refused_reference(roms / "Disc.cue", "tracks/Track.bin")

    await _blocked_for(client, group, str(roms / "Game.iso"), "Disc.cue")
    assert (roms / "tracks" / "Track.bin").exists()



# ----------------------------------------------------------------------
# review round 5: the batch under the keeper, a sheet that appears late,
# identity compares, conditional settles, a first failed check
# ----------------------------------------------------------------------


async def _three_isos(client, roms: Path) -> tuple[dict, str, dict]:
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy1.iso")
    d388._copy(roms / "Game.iso", roms / "Copy2.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    return group, keep, (await _plan(client, group, keep)).json()


def _statuses(done) -> dict[str, tuple[str, str]]:
    return {
        os.path.basename(r["path"]): (r["status"], r["reason"] or "")
        for r in done.json()["results"]
    }


@pytest.mark.asyncio
async def test_a_keeper_renamed_away_mid_batch_stops_the_batch(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Codex 4593214751: the keeper is held open from its hash to the end of
    the batch, and before each delete its path must still name that file."""
    from services.job_manager import job_manager

    roms = db / "roms"
    group, keep, plan = await _three_isos(client, roms)
    real = job_manager._delete_copy
    calls = {"n": 0}

    async def swap_after_first(path, keeper, *args, **kwargs):
        outcome = await real(path, keeper, *args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            # Another file renamed into the keeper's place, same size and mtime.
            st = os.stat(keeper)
            other = roms / "other.tmp"
            other.write_bytes(os.urandom(st.st_size))
            os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(other, keeper)
        return outcome

    monkeypatch.setattr(job_manager, "_delete_copy", swap_after_first)
    done = await _delete(client, group, keep, _paths(plan))

    assert done.status_code == 200, done.text
    statuses = _statuses(done)
    assert statuses["Copy1.iso"][0] == "deleted"
    assert statuses["Copy2.iso"][0] != "deleted"
    assert "copy to keep" in statuses["Copy2.iso"][1]
    assert (roms / "Copy2.iso").exists()


@pytest.mark.asyncio
async def test_a_keeper_lock_lost_mid_batch_stops_the_batch(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """CodeRabbit 4593240091: a Referrer operation that takes the keeper's lock
    with it leaves the batch without it: nothing more is deleted."""
    from services import job_manager as jm

    roms = db / "roms"
    group, keep, plan = await _three_isos(client, roms)
    real = jm.job_manager._delete_source_set

    async def takes_the_locks(owner_id, **kwargs):
        outcome = await real(owner_id, **kwargs)
        travelling = kwargs.get("travelling_locks")
        if travelling is not None:
            jm._let_go(travelling.take())   # as a lingering Referrer would
        return outcome

    monkeypatch.setattr(jm.job_manager, "_delete_source_set", takes_the_locks)
    done = await _delete(client, group, keep, _paths(plan))

    statuses = _statuses(done)
    assert statuses["Copy1.iso"][0] == "deleted"
    assert statuses["Copy2.iso"][0] != "deleted"
    assert "lock" in statuses["Copy2.iso"][1]
    assert (roms / "Copy2.iso").exists()


@pytest.mark.asyncio
async def test_a_sheet_that_names_a_copy_after_the_plan_keeps_it(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """CodeRabbit 4593240099: the sheet check runs again under the copy's
    locks, right before it is deleted."""
    from services.job_manager import job_manager

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()
    real = job_manager._copy_snapshot_bounded

    async def sheet_lands(path, identity):
        # A sheet naming the copy appears once the delete has re-planned.
        _cue(roms / "Disc.cue", "Copy.iso")
        return await real(path, identity)

    monkeypatch.setattr(job_manager, "_copy_snapshot_bounded", sheet_lands)
    done = await _delete(client, group, keep, _paths(plan))

    assert done.status_code == 200, done.text
    status, reason = _statuses(done)["Copy.iso"]
    assert status != "deleted" and "Disc.cue" in reason
    assert (roms / "Copy.iso").exists()


def _case_insensitive(folder: Path) -> bool:
    probe = folder / "CaseProbe"
    probe.write_text("x")
    try:
        return (folder / "caseprobe").exists()
    finally:
        probe.unlink()


@pytest.mark.asyncio
async def test_a_sheet_naming_the_track_in_another_case_owns_it(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """CodeRabbit 4593240059: on a case-insensitive share the sheet's spelling
    and the file's differ; they are compared by file identity."""
    roms = db / "romm"
    roms.mkdir(parents=True, exist_ok=True)
    if not _case_insensitive(roms):
        pytest.skip("needs a case-insensitive filesystem")
    group = await _track_group(client, monkeypatch, roms)
    _cue(roms / "Disc.cue", "disc (track 1).BIN")

    await _blocked_for(client, group, str(roms / "Game.iso"), "is a track of Disc.cue")


@pytest.mark.asyncio
async def test_settling_never_overwrites_a_newer_finding(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """CodeRabbit 4593240053: the post-delete update applies only to the
    Finding the delete read; a run that changed it meanwhile wins."""
    from fastapi.concurrency import run_in_threadpool
    from services import duplicate_cleanup

    roms = db / "roms"
    group, _keep, _shown = await _three_isos(client, roms)

    await run_in_threadpool(
        duplicate_cleanup._settle, group["id"], {str(roms / "Copy1.iso")}, "stale-fingerprint",
    )
    await run_in_threadpool(
        duplicate_cleanup._settle, group["id"],
        {str(roms / "Copy1.iso"), str(roms / "Copy2.iso")}, "stale-fingerprint",
    )

    after = await _group(client)
    assert after["fingerprint"] == group["fingerprint"]


@pytest.mark.asyncio
async def test_a_first_failed_check_is_recorded_and_bars_the_keeper(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """CodeRabbit review 5929914808: a copy with no record whose first check
    fails is recorded as failed, so it is never kept."""
    from services.verification_store import verification_store

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    await verification_store.mark_failed(str(roms / "Copy.iso"))
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)

    assert group["keeper"] == str(roms / "Game.iso")
    response = await _plan(client, group, str(roms / "Copy.iso"))
    assert response.status_code == 409
    assert "failed its check" in response.json()["detail"]


@pytest.mark.asyncio
async def test_no_cleanup_is_offered_when_every_copy_failed(
    db: Path, client: httpx.AsyncClient,
) -> None:
    from services.verification_store import verification_store

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    for name in ("Game.iso", "Copy.iso"):
        await verification_store.mark_failed(str(roms / name))
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)

    assert group["keeper"] is None
    assert group["deletable"] is False
    assert "failed" in group["not_deletable"]



@pytest.mark.asyncio
async def test_a_missing_track_named_in_another_case_still_owns_the_copy(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """CodeRabbit 4593353319: a track the filesystem reports missing is
    compared by its case-folded path, which only widens what counts as the
    sheet's own, so it fails closed."""
    roms = db / "romm"
    group = await _track_group(client, monkeypatch, roms)
    _cue(roms / "Disc.cue", "DISC (TRACK 1).BIN")
    spelled = os.path.realpath(roms / "DISC (TRACK 1).BIN")
    real_stat = os.stat

    def missing(path, *args, **kwargs):
        if os.fspath(path) == spelled:
            raise FileNotFoundError(2, "No such file", spelled)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", missing)
    await _blocked_for(client, group, str(roms / "Game.iso"), "is a track of Disc.cue")



# ----------------------------------------------------------------------
# review round 7
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_keeper_opened_after_the_bound_is_closed_again(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Codex 4593499336: an open of the keeper that answers after the bound
    gave up hands its descriptor to an undo, which closes it."""
    import time as _time

    from services import job_manager as jm

    roms = db / "roms"
    group, keep, plan = await _iso_pair(client, roms)
    opened: list[int] = []
    real_open = jm.JobManager._open_keeper

    def slow_open(path):
        _time.sleep(0.6)
        fd, snapshot = real_open(path)
        opened.append(fd)
        return fd, snapshot

    monkeypatch.setattr(jm.JobManager, "_open_keeper", staticmethod(slow_open))
    monkeypatch.setattr(jm, "KEPT_DELETE_PROBE_SECONDS", 0.2)
    done = await _delete(client, group, keep, _paths(plan))

    assert done.status_code == 200, done.text
    assert all(r["status"] != "deleted" for r in done.json()["results"])
    assert (roms / "Copy.iso").exists()
    for _ in range(100):
        if opened:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    assert opened, "the slow open never finished"
    keeper_inode = os.stat(keep).st_ino
    try:
        still = os.fstat(opened[0]).st_ino == keeper_inode
    except OSError:
        still = False
    assert not still, "the keeper's descriptor was left open"


@pytest.mark.asyncio
async def test_a_produced_chd_is_kept_only_while_its_latest_check_is_current(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Codex 4593499342: the latest check decides, not the fingerprint the
    conversion recorded. Touched after it, the CHD is blocked; a Verify that
    passes makes it eligible again; changed after that, blocked again."""
    import time as _time

    from services.chdman import chdman_service
    from services.verification_store import verification_store

    roms = db / "roms"
    sha1 = d388._disc(roms / "Game.iso", b"game")
    chd = roms / "Game.chd"
    await d388._seed_chd(chd, data_sha1=sha1, sha1="0" * 40)
    st = chd.stat()
    await verification_store.mark_verified(str(chd), produced_meta={
        "mode": "createdvd", "output": {"size": st.st_size, "mtime_ns": st.st_mtime_ns},
    })

    async def info(path, *, cancel_event=None):
        return {"sha1": "0" * 40, "data_sha1": sha1,
                "raw_data": "Metadata: Tag='DVD '  Index=0\n"}

    monkeypatch.setattr(chdman_service, "info", info)
    library = await d388._register(client, roms)

    async def plan_status() -> int:
        await d388._find(client, library)
        group = await _group(client)
        return (await _plan(client, group, str(chd))).status_code

    assert await plan_status() == 200

    _time.sleep(0.01)
    os.utime(chd, None)                              # touched after its check
    assert await plan_status() == 409

    _time.sleep(0.01)
    await verification_store.refresh_check(str(chd))  # a Verify that passes
    assert await plan_status() == 200

    _time.sleep(0.01)
    os.utime(chd, None)                              # changed again
    assert await plan_status() == 409



# ----------------------------------------------------------------------
# a track reached through a link in another folder (Codex PRRT_kwDOQ9nULM64NUn9)
# ----------------------------------------------------------------------


def _linked_track(roms: Path) -> Path:
    """``disc/Disc.cue`` names ``disc/Track.bin``, a link to
    ``shared/Track.bin``, which holds the same disc as ``Game.iso``."""
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "shared" / "Track.bin")
    (roms / "disc").mkdir(parents=True, exist_ok=True)
    os.symlink(roms / "shared" / "Track.bin", roms / "disc" / "Track.bin")
    _cue(roms / "disc" / "Disc.cue", "Track.bin")
    return roms / "shared" / "Track.bin"


@pytest.mark.asyncio
async def test_a_track_a_sheet_reaches_through_a_link_is_never_deleted(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Walking up from the target cannot find the sheet; the run's record of
    every sheet's track identities can."""
    roms = db / "romm"
    target = _linked_track(roms)
    release = threading.Event()
    release.set()
    d388._romm_images_resolving(
        monkeypatch, release, [roms / "Game.iso", target, roms / "disc" / "Disc.cue"],
    )
    await d388._find(client, "romm")
    group = await _group(client)
    assert d388._names(group) == ["Game.iso", "Track.bin"]

    await _blocked_for(client, group, str(roms / "Game.iso"), "a sheet in the Volumes")
    assert target.exists()


@pytest.mark.asyncio
async def test_a_folder_library_never_lists_a_linked_track_as_an_image(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """The listing itself: a target a sheet reaches through its linked track
    is that sheet's, not an Image of its own, so it is never grouped."""
    roms = db / "roms"
    _linked_track(roms)
    library = await d388._register(client, roms)
    await d388._find(client, library)

    assert await d388._duplicates(client) == []


@pytest.mark.asyncio
async def test_a_run_with_no_record_of_sheet_tracks_offers_no_delete(
    db: Path, client: httpx.AsyncClient,
) -> None:
    from services import db as _db

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    with _db.SessionLocal() as session:
        row = session.get(_db.DuplicateRunRecord, library)
        summary = dict(row.summary)
        summary.pop("sheet_tracks", None)
        row.summary = summary
        session.commit()

    group = await _group(client)
    assert group["deletable"] is False
    assert "sheet" in group["not_deletable"]
    assert (await _plan(client, group, str(roms / "Game.iso"))).status_code == 409



# ----------------------------------------------------------------------
# a container keeper is read through its held descriptor (Codex 4593632748)
# ----------------------------------------------------------------------


async def _chd_keeper(client, roms: Path) -> tuple[dict, str, dict, bytes]:
    """A CHD keeper whose bytes are corrupt but whose metadata claims the
    group's key, checked as Intact, and the ISO it duplicates."""
    from services.verification_store import verification_store

    sha1 = d388._disc(roms / "Game.iso", b"game")
    await d388._seed_chd(roms / "Game.chd", data_sha1=sha1, sha1="0" * 40)
    await verification_store.mark_verified(str(roms / "Game.chd"))
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    chd = str(roms / "Game.chd")
    plan = (await _plan(client, group, chd)).json()
    assert plan["blocked"] is False, plan
    return group, chd, plan, (roms / "Game.iso").read_bytes()


@pytest.mark.asyncio
async def test_a_keeper_swapped_by_name_during_its_full_pass_is_caught(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """A healthy file renamed into the keeper's name for the length of the
    pass, then the corrupt keeper put back: the pass reads the held file, not
    the name, so it hashes the corrupt bytes and the delete is refused."""
    import hashlib

    from services.chdman import chdman_service
    from services.tools import registry

    roms = db / "roms"
    group, chd, plan, healthy = await _chd_keeper(client, roms)
    aside = roms / "aside.tmp"

    async def verify(path, *, cancel_event=None):
        os.rename(chd, aside)                 # the corrupt keeper, still held open
        (roms / "Game.chd").write_bytes(healthy)
        return {"valid": True, "message": "ok"}

    async def info(path, *, cancel_event=None):
        with open(path, "rb") as fh:
            digest = hashlib.sha1(fh.read()).hexdigest()
        os.replace(aside, chd)                # put the corrupt keeper back
        return {"sha1": "0" * 40, "data_sha1": digest,
                "raw_data": "Metadata: Tag='DVD '  Index=0\n"}

    monkeypatch.setattr(registry.get("chdman"), "verify", verify)
    monkeypatch.setattr(chdman_service, "info", info)
    done = await _delete(client, group, chd, _paths(plan))

    assert done.status_code == 200, done.text
    assert all(r["status"] != "deleted" for r in done.json()["results"])
    assert (roms / "Game.iso").exists()


@pytest.mark.asyncio
async def test_a_container_keeper_with_no_path_to_its_held_file_refuses(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Where no path names the held descriptor, the pass is never run on the
    name: the delete is refused for that keeper."""
    from services.tools import base

    roms = db / "roms"
    group, chd, plan, _healthy = await _chd_keeper(client, roms)
    monkeypatch.setattr(base, "held_file_path", lambda fd: None)

    done = await _delete(client, group, chd, _paths(plan))

    assert done.status_code == 200, done.text
    results = done.json()["results"]
    assert all(r["status"] != "deleted" for r in results)
    assert any("held" in (r["reason"] or "") for r in results), results
    assert (roms / "Game.iso").exists()



# ----------------------------------------------------------------------
# a linked sheet the run cannot read (Codex PRRT_kwDOQ9nULM64N2yy)
# ----------------------------------------------------------------------


async def _linked_group_with_sheet(client, monkeypatch, roms: Path, write_sheet) -> dict:
    target = _linked_track(roms)
    write_sheet(roms / "disc" / "Disc.cue")
    release = threading.Event()
    release.set()
    d388._romm_images_resolving(
        monkeypatch, release, [roms / "Game.iso", target, roms / "disc" / "Disc.cue"],
    )
    await d388._find(client, "romm")
    return await _group(client)


async def _no_delete_offered(client, group: dict, keep: str) -> None:
    assert group["deletable"] is False, group
    assert "sheet" in group["not_deletable"], group
    assert (await _plan(client, group, keep)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.skipif(folder_library_303._IS_ROOT, reason="root reads every file")
async def test_an_unreadable_sheet_leaves_the_run_record_incomplete(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """The sheet that reaches the target cannot be read: which files are its
    tracks is unknown, so the run's record is incomplete and no delete is
    offered, never "this sheet owns nothing"."""
    roms = db / "romm"

    def unreadable(sheet: Path) -> None:
        sheet.chmod(0)

    try:
        group = await _linked_group_with_sheet(client, monkeypatch, roms, unreadable)
        await _no_delete_offered(client, group, str(roms / "Game.iso"))
    finally:
        (roms / "disc" / "Disc.cue").chmod(0o644)
    assert (roms / "shared" / "Track.bin").exists()


@pytest.mark.asyncio
async def test_an_oversized_sheet_leaves_the_run_record_incomplete(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from utils.sheet_text import MAX_SHEET_BYTES

    roms = db / "romm"

    def oversized(sheet: Path) -> None:
        sheet.write_text('FILE "Track.bin" BINARY\n' + "REM x\n" * (MAX_SHEET_BYTES // 6 + 1))

    group = await _linked_group_with_sheet(client, monkeypatch, roms, oversized)
    await _no_delete_offered(client, group, str(roms / "Game.iso"))
    assert (roms / "shared" / "Track.bin").exists()



# ----------------------------------------------------------------------
# a sheet in a folder the walk could not list (Codex PRRT_kwDOQ9nULM64OGLk)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(folder_library_303._IS_ROOT, reason="root lists every folder")
async def test_an_unlisted_folder_leaves_the_run_record_incomplete(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """``disc/`` cannot be listed, so its sheet (whose track links to
    ``shared/Track.bin``) is never read. Neither copy is inside ``disc/``, so
    the group is not held: the run's record of sheet tracks must say it is
    incomplete, and name the folder, or the linked track would be deleted."""
    roms = db / "roms"
    target = _linked_track(roms)
    library = await d388._register(client, roms)
    (roms / "disc").chmod(0)
    try:
        await d388._find(client, library)
        group = await _group(client)
        plan = await _plan(client, group, str(roms / "Game.iso"))
    finally:
        (roms / "disc").chmod(0o755)

    assert d388._names(group) == ["Game.iso", "Track.bin"]
    assert group["deletable"] is False, group
    assert "disc/" in group["not_deletable"], group
    assert plan.status_code == 409
    assert target.exists()


@pytest.mark.asyncio
async def test_a_run_that_did_not_list_every_folder_offers_no_delete(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """A run recorded before the rule (folders unlisted, yet a sheet-track
    record) is not taken as complete: the two must agree."""
    from services import db as _db

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    with _db.SessionLocal() as session:
        row = session.get(_db.DuplicateRunRecord, library)
        row.summary = {**row.summary, "unlisted": 1}
        session.commit()

    group = await _group(client)
    assert group["deletable"] is False, group
    assert "list" in group["not_deletable"], group



# ----------------------------------------------------------------------
# sheets anywhere in the Volumes (Codex PRRT_kwDOQ9nULM64OXPI)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sheet_in_another_library_owning_a_track_blocks_the_delete(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """``other/Disc.cue`` (not this Library) names ``other/Track.bin``, a link
    to this Library's ``roms/Track.bin``. ``resolve_track`` accepts targets
    anywhere in the Volumes, so ownership is recorded over the Volumes."""
    roms, other = db / "roms", db / "other"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Track.bin")
    other.mkdir()
    os.symlink(roms / "Track.bin", other / "Track.bin")
    _cue(other / "Disc.cue", "Track.bin")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    assert d388._names(group) == ["Game.iso", "Track.bin"]

    await _blocked_for(client, group, str(roms / "Game.iso"), "a sheet in the Volumes")
    assert (roms / "Track.bin").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(folder_library_303._IS_ROOT, reason="root lists every folder")
async def test_an_unlistable_folder_outside_the_library_offers_no_delete(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """A folder in the Volumes but outside the Library may hold a sheet that
    owns one of its copies: not listed means not ruled out."""
    roms, locked = db / "roms", db / "elsewhere" / "locked"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    locked.mkdir(parents=True)
    library = await d388._register(client, roms)
    locked.chmod(0)
    try:
        await d388._find(client, library)
        group = await _group(client)
        plan = await _plan(client, group, str(roms / "Game.iso"))
    finally:
        locked.chmod(0o755)

    assert group["deletable"] is False, group
    assert "locked" in group["not_deletable"], group
    assert plan.status_code == 409
    assert (roms / "Copy.iso").exists()


@pytest.mark.asyncio
async def test_a_volume_added_after_the_run_makes_its_record_stale(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """The record covers the Volumes it walked: a Volume added since may
    hold a sheet it never read."""
    from config import settings as app_settings

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    assert (await _group(client))["deletable"] is True

    added = db.parent / "vol2"
    added.mkdir()
    monkeypatch.setattr(app_settings, "chd_volumes", f"{db},{added}")
    group = await _group(client)
    assert group["deletable"] is False, group
    assert "Volumes" in group["not_deletable"], group
    assert (await _plan(client, group, str(roms / "Game.iso"))).status_code == 409



@pytest.mark.asyncio
async def test_a_sheet_romm_does_not_list_still_blocks_its_track(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Codex 4594076949: RomM lists ``shared/Track.bin`` and ``Game.iso`` but
    not ``disc/Disc.cue``, whose linked track is that file. Ownership comes
    from the on-disk walk of the Volumes, not RomM's records."""
    roms = db / "romm"
    target = _linked_track(roms)
    release = threading.Event()
    release.set()
    d388._romm_images_resolving(monkeypatch, release, [roms / "Game.iso", target])
    await d388._find(client, "romm")
    group = await _group(client)
    assert d388._names(group) == ["Game.iso", "Track.bin"]

    await _blocked_for(client, group, str(roms / "Game.iso"), "a sheet in the Volumes")
    assert target.exists()



# ----------------------------------------------------------------------
# the confirmed delete is a background job
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_confirmed_delete_runs_as_a_job(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """The route only starts it: its id comes back at once, and the job's
    progress, outcome and per-copy results are the job's."""
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()

    started = await _start_delete(client, group, keep, _paths(plan))
    assert started.status_code == 200, started.text
    assert set(started.json()) == {"job_id"}
    job = await _finished_job(client, started.json()["job_id"])

    assert job["mode"] == "delete_duplicates", job
    assert job["status"] == "completed", job
    assert job["result"]["keep"] == keep
    assert [r["status"] for r in job["result"]["results"]] == ["deleted"]
    assert job["result"]["keeper_changed"] is None
    assert not (roms / "Copy.iso").exists()
    assert await d388._duplicates(client) == []


@pytest.mark.asyncio
async def test_a_second_delete_of_the_same_group_waits_for_the_first(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services import duplicate_cleanup

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()
    gate = asyncio.Event()
    real = duplicate_cleanup._delete

    async def held(*args, **kwargs):
        await gate.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(duplicate_cleanup, "_delete", held)
    first = await _start_delete(client, group, keep, _paths(plan))
    second = await _start_delete(client, group, keep, _paths(plan))
    gate.set()
    await duplicate_cleanup.drain()

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_a_cancelled_delete_job_deletes_nothing(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services import duplicate_cleanup, image_sets
    from services.job_manager import job_manager

    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()
    real = image_sets.volume_sheet_tracks
    started_id: list[str] = []

    def cancelled_during_the_walk(roots, *, progress):
        job_manager._cancelled.add(started_id[0])
        event = job_manager.get_cancel_event(started_id[0])
        if event is not None:
            event.set()
        return real(roots, progress=progress)

    monkeypatch.setattr(image_sets, "volume_sheet_tracks", cancelled_during_the_walk)
    gate = asyncio.Event()
    run = duplicate_cleanup._delete

    async def after_the_id(*args, **kwargs):
        await gate.wait()
        return await run(*args, **kwargs)

    monkeypatch.setattr(duplicate_cleanup, "_delete", after_the_id)
    started = await _start_delete(client, group, keep, _paths(plan))
    started_id.append(started.json()["job_id"])
    gate.set()
    job = await _finished_job(client, started_id[0])

    assert job["status"] == "cancelled", job
    assert (roms / "Copy.iso").exists()


# ----------------------------------------------------------------------
# sheets added after the run (Codex PRRT_kwDOQ9nULM64OuXm)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sheet_added_after_the_run_keeps_its_track(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """The run saw no sheet; ``disc/Disc.cue`` linking to ``shared/Track.bin``
    is added after it, before the delete is confirmed. The delete job reads
    the Volumes' sheets again, so the BIN is kept."""
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "shared" / "Track.bin")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    assert d388._names(group) == ["Game.iso", "Track.bin"]
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()
    assert plan["blocked"] is False, plan

    (roms / "disc").mkdir()
    os.symlink(roms / "shared" / "Track.bin", roms / "disc" / "Track.bin")
    _cue(roms / "disc" / "Disc.cue", "Track.bin")
    done = await _delete(client, group, keep, _paths(plan))

    assert done.status_code == 409, done.text
    assert "a sheet in the Volumes" in done.json()["detail"], done.json()
    assert (roms / "shared" / "Track.bin").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(folder_library_303._IS_ROOT, reason="root lists every folder")
async def test_a_volume_walk_that_cannot_finish_at_delete_time_deletes_nothing(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms, locked = db / "roms", db / "elsewhere"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    locked.mkdir()
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()

    locked.chmod(0)
    try:
        done = await _delete(client, group, keep, _paths(plan))
    finally:
        locked.chmod(0o755)

    assert done.status_code == 409, done.text
    assert "elsewhere" in done.json()["detail"], done.json()
    assert (roms / "Copy.iso").exists()


# ----------------------------------------------------------------------
# a copy replaced since its key was set (Codex PRRT_kwDOQ9nULM64OuXs)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_copy_replaced_with_the_same_size_and_mtime_is_not_deleted(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """Different bytes of the same size, moved into place (a new file), with
    the old mtime put back: size and mtime match, the file is not the one
    that was hashed."""
    roms = db / "roms"
    d388._disc(roms / "Game.iso", b"game")
    d388._copy(roms / "Game.iso", roms / "Copy.iso")
    library = await d388._register(client, roms)
    await d388._find(client, library)
    group = await _group(client)
    keep = str(roms / "Game.iso")
    plan = (await _plan(client, group, keep)).json()

    copy = roms / "Copy.iso"
    before = os.stat(copy)
    data = bytearray(copy.read_bytes())
    data[-1] ^= 0xFF
    other = roms / "other.tmp"
    other.write_bytes(bytes(data))
    os.replace(other, copy)
    os.utime(copy, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (os.stat(copy).st_size, os.stat(copy).st_mtime_ns) == (
        before.st_size, before.st_mtime_ns,
    )

    done = await _delete(client, group, keep, _paths(plan))
    if done.status_code == 200:
        statuses = {r["status"] for r in done.json()["results"]}
        assert statuses == {"stale"}, done.json()
    assert copy.exists()
    assert copy.read_bytes() == bytes(data)


def test_a_key_is_tied_to_the_file_it_was_read_from(db: Path, tmp_path: Path) -> None:
    """The stored identity is the file's device and inode as well as its size
    and mtime: a new file in its place no longer has the key."""
    from services import content_keys

    path = db / "roms" / "Disc.iso"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 64)
    identity = content_keys.identity_of(str(path))
    content_keys.store(str(path), identity, "raw:aa", ".iso")
    assert content_keys.stored({str(path): identity})[str(path)].key == "raw:aa"

    before = os.stat(path)
    other = path.with_suffix(".tmp")
    other.write_bytes(b"y" * 64)
    os.replace(other, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert content_keys.stored({str(path): content_keys.identity_of(str(path))}) == {}


def test_a_copy_moved_into_place_is_refused_under_its_holds(db: Path) -> None:
    """The check the delete step asks under each copy's holds: the copy must
    be the very file its key was read from, not one with the same size and
    mtime moved into its place."""
    from services import content_keys, duplicate_cleanup

    path = db / "roms" / "Copy.iso"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 64)
    content_keys.store(str(path), content_keys.identity_of(str(path)), "raw:aa", ".iso")
    check = functools.partial(
        duplicate_cleanup._copy_under_holds, key="raw:aa", tracks=frozenset(),
    )
    assert check(str(path)) is None

    before = os.stat(path)
    other = path.with_suffix(".tmp")
    other.write_bytes(b"y" * 64)
    os.replace(other, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert "no longer the file its content key was read from" in check(str(path))
