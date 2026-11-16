"""The duplicate finder (#388): content keys, a find-duplicates run and Findings.

A Library's "find duplicates" run is an external job. It lists the Library's
Images, reads a content key for each one that has none stored for its current
size and mtime, then groups the Images by key and records each group as a
``duplicate`` Finding. ``raw:<sha1>`` is the SHA1 of the disc image a file holds:
an ``.iso``/``.bin`` file's own SHA1, a Dolphin container's disc SHA1, a DVD or
HD CHD's data SHA1. Every other format gets no key and is reported as not
checked. Nothing here deletes a file.

Driven through the HTTP routes. Only the outside tools are stubbed: the CHD
metadata store is seeded with what ``chdman info`` would have said, and
dolphin-tool's disc hash is answered in place. The real tools run in
``test_duplicates_388_real.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from services.chd_metadata_store import chd_metadata_store
from services.tools import registry

from . import test_folder_library_303 as folder_library_303

# #303's folder Library fixture (`db`: a fresh database and a Volume).
_db_fixture = folder_library_303._db_fixture


def _app() -> FastAPI:
    from routes import convert, findings, libraries

    app = FastAPI()
    for router in (convert.router, libraries.router, findings.router):
        app.include_router(router, prefix="/api")
    return app


@pytest_asyncio.fixture(name="client")
async def _client(db: Path):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    from services import duplicates

    await duplicates.drain()


def _disc(path: Path, seed: bytes, size: int = 96 * 1024) -> str:
    """A file of *size* bytes made from *seed*; returns its SHA1."""
    path.parent.mkdir(parents=True, exist_ok=True)
    block = hashlib.sha256(seed).digest()
    data = (block * (size // len(block) + 1))[:size]
    path.write_bytes(data)
    return hashlib.sha1(data).hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())


async def _seed_chd(path: Path, *, data_sha1: str, sha1: str, tag: str = "DVD ") -> None:
    """A ``.chd`` whose cached ``chdman info`` says *data_sha1* and *sha1*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MComprHD" + os.urandom(64))
    await chd_metadata_store.set_metadata(str(path), {
        "sha1": sha1, "data_sha1": data_sha1,
        "raw_data": f"SHA1: {sha1}\nData SHA1: {data_sha1}\nMetadata: Tag='{tag}'  Index=0\n",
        "metadata_lines": [f"Tag='{tag}'"],
    })


def _dolphin_answers(monkeypatch, answers: dict[str, str]) -> list[str]:
    """dolphin-tool's disc SHA1 for each file name in *answers*; returns the calls."""
    tool = registry.get("dolphin")
    calls: list[str] = []

    async def disc_hashes(path, *, cancel_event=None):
        calls.append(os.path.basename(path))
        return [answers[os.path.basename(path)]]

    monkeypatch.setattr(tool._service, "disc_hashes", disc_hashes)
    return calls


async def _register(client: httpx.AsyncClient, root: Path) -> str:
    response = await client.post("/api/libraries", json={"path": str(root)})
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _start(client: httpx.AsyncClient, library_id: str) -> str:
    response = await client.post(f"/api/libraries/{library_id}/find-duplicates")
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


async def _finished(client: httpx.AsyncClient, job_id: str, *, seconds: float = 20) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = (await client.get(f"/api/jobs/{job_id}")).json()
        if job["status"] in ("completed", "failed", "cancelled"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {job}")


async def _find(client: httpx.AsyncClient, library_id: str) -> dict:
    job = await _finished(client, await _start(client, library_id))
    assert job["status"] == "completed", job
    return job


async def _duplicates(client: httpx.AsyncClient) -> list[dict]:
    listed = (await client.get("/api/findings")).json()
    return [row for row in listed["findings"] if row["kind"] == "duplicate"]


async def _last_run(client: httpx.AsyncClient, library_id: str) -> dict:
    listed = (await client.get("/api/findings")).json()
    runs = {run["library_id"]: run for run in listed["duplicate_runs"]}
    return runs[library_id]


def _names(finding: dict) -> list[str]:
    return sorted(os.path.basename(copy["path"]) for copy in finding["cause"]["copies"])


# ----------------------------------------------------------------------
# grouping
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_disc_in_three_containers_is_one_finding(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    roms = db / "roms"
    sha1 = _disc(roms / "Game.iso", b"game")
    _copy(roms / "Game.iso", roms / "backup" / "Game (copy).iso")
    (roms / "Game.rvz").write_bytes(b"RVZ\x01" + os.urandom(128))
    _dolphin_answers(monkeypatch, {"Game.rvz": sha1})
    # A createdvd CHD: its data SHA1 is the image's SHA1; the overall one is not.
    await _seed_chd(roms / "Game.chd", data_sha1=sha1, sha1="0" * 40)
    _disc(roms / "Other.iso", b"other")

    library = await _register(client, roms)
    await _find(client, library)

    found = await _duplicates(client)
    assert len(found) == 1
    group = found[0]
    assert group["library_id"] == library
    assert group["image_key"] == f"raw:{sha1}"
    assert _names(group) == ["Game (copy).iso", "Game.chd", "Game.iso", "Game.rvz"]
    copies = {os.path.basename(c["path"]): c for c in group["cause"]["copies"]}
    # Each copy's path, container and size.
    assert copies["Game.iso"]["path"] == str(roms / "Game.iso")
    assert copies["Game.iso"]["container"] == ".iso"
    assert copies["Game.iso"]["size"] == (roms / "Game.iso").stat().st_size
    assert copies["Game.rvz"]["container"] == ".rvz"
    assert copies["Game.chd"]["container"] == ".chd"
    assert copies["Game (copy).iso"]["image_key"] == "backup/Game (copy).iso"
    assert "4 copies" in group["reason"]
    # Read-only: every file is still there.
    for name in ("Game.iso", "Game.rvz", "Game.chd", "Other.iso"):
        assert (roms / name).exists()


@pytest.mark.asyncio
async def test_a_chd_overall_sha1_is_never_a_key(db: Path, client: httpx.AsyncClient) -> None:
    roms = db / "roms"
    sha1 = _disc(roms / "Game.iso", b"game")
    # The overall SHA1 happens to equal the ISO's; the data SHA1 does not.
    await _seed_chd(roms / "Game.chd", data_sha1="1" * 40, sha1=sha1)

    library = await _register(client, roms)
    await _find(client, library)

    assert await _duplicates(client) == []


@pytest.mark.asyncio
async def test_cd_chds_and_containers_with_no_image_hash_are_not_checked(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    sha1 = _disc(roms / "Game.iso", b"game")
    # A CD CHD's data SHA1 covers 2448-byte frames, never the raw image: even
    # equal to the ISO's it groups with nothing.
    await _seed_chd(roms / "Disc.chd", data_sha1=sha1, sha1="2" * 40, tag="CHT2")
    _disc(roms / "Game.cso", b"cso")
    _copy(roms / "Game.cso", roms / "Game (copy).cso")

    library = await _register(client, roms)
    await _find(client, library)

    assert await _duplicates(client) == []
    run = await _last_run(client, library)
    assert run["not_checked"] == {".chd": 1, ".cso": 2}
    assert run["keyed"] == 1


@pytest.mark.asyncio
async def test_a_second_run_reads_only_the_keys_it_is_missing(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    roms = db / "roms"
    sha1 = _disc(roms / "Game.iso", b"game")
    _copy(roms / "Game.iso", roms / "Copy.iso")
    (roms / "Game.rvz").write_bytes(b"RVZ\x01" + os.urandom(128))
    calls = _dolphin_answers(monkeypatch, {"Game.rvz": sha1})

    library = await _register(client, roms)
    await _find(client, library)
    assert (await _last_run(client, library))["computed"] == 3

    await _find(client, library)
    assert (await _last_run(client, library))["computed"] == 0
    assert calls == ["Game.rvz"]
    assert len((await _duplicates(client))[0]["cause"]["copies"]) == 3

    # A changed file loses its key: the copy is now another disc.
    _disc(roms / "Copy.iso", b"changed")
    os.utime(roms / "Copy.iso", ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    await _find(client, library)
    assert (await _last_run(client, library))["computed"] == 1
    assert _names((await _duplicates(client))[0]) == ["Game.iso", "Game.rvz"]


@pytest.mark.asyncio
async def test_the_finding_goes_away_once_one_copy_is_left(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    _disc(roms / "Game.iso", b"game")
    _copy(roms / "Game.iso", roms / "Copy.iso")
    _disc(roms / "Other.iso", b"other")

    library = await _register(client, roms)
    await _find(client, library)
    assert len(await _duplicates(client)) == 1

    (roms / "Copy.iso").unlink()
    # The Library's next listing settles it, before any run.
    assert (await client.post("/api/findings/check")).status_code == 200
    assert await _duplicates(client) == []

    _copy(roms / "Game.iso", roms / "Copy.iso")
    await _find(client, library)
    assert len(await _duplicates(client)) == 1
    (roms / "Copy.iso").unlink()
    await _find(client, library)
    assert await _duplicates(client) == []


@pytest.mark.asyncio
async def test_staging_files_and_outputs_being_written_are_never_grouped(
    db: Path, client: httpx.AsyncClient,
) -> None:
    from services.lock_manager import lock_manager

    roms = db / "roms"
    _disc(roms / "Game.iso", b"game")
    # RomM's converter staging a copy.
    _copy(roms / "Game.iso", roms / ".romm_tmp_Game.iso")
    _copy(roms / "Game.iso", roms / ".romm_tmp_dir" / "Game.iso")
    # A conversion writing a copy right now.
    _copy(roms / "Game.iso", roms / "Writing.iso")
    _disc(roms / "Other.iso", b"other")
    assert lock_manager.acquire_lock(str(roms / "Writing.iso"), allow_existing=True)
    try:
        library = await _register(client, roms)
        await _find(client, library)
    finally:
        lock_manager.release_lock(str(roms / "Writing.iso"))

    assert await _duplicates(client) == []
    assert (await _last_run(client, library))["busy"] == 1


# ----------------------------------------------------------------------
# the run: an external job, bounded, cancellable
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_run_is_an_external_job_never_a_conversion(
    db: Path, client: httpx.AsyncClient,
) -> None:
    from services.job_manager import _is_conversion_job, job_manager

    roms = db / "roms"
    _disc(roms / "Game.iso", b"game")
    library = await _register(client, roms)

    job_id = await _start(client, library)
    job = await _finished(client, job_id)

    assert job["mode"] == "find_duplicates"
    assert job["status"] == "completed"
    assert not _is_conversion_job(job_manager.get_job(job_id))


@pytest.mark.asyncio
async def test_a_run_can_be_cancelled_and_writes_no_findings(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services import content_keys

    roms = db / "roms"
    _disc(roms / "Game.iso", b"game")
    _copy(roms / "Game.iso", roms / "Copy.iso")
    release = threading.Event()
    real = content_keys._sha1_of

    def held(path, *, progress):
        while not release.wait(0.01):
            progress()
        return real(path, progress=progress)

    monkeypatch.setattr(content_keys, "_sha1_of", held)
    library = await _register(client, roms)
    job_id = await _start(client, library)
    await asyncio.sleep(0.2)

    assert (await client.delete(f"/api/jobs/{job_id}")).json() == {"status": "cancelled"}
    job = await _finished(client, job_id)
    release.set()

    assert job["status"] == "cancelled"
    assert await _duplicates(client) == []


@pytest.mark.asyncio
async def test_a_file_whose_read_stalls_costs_that_file_only(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services import content_keys

    roms = db / "roms"
    _disc(roms / "Game.iso", b"game")
    _copy(roms / "Game.iso", roms / "Copy.iso")
    _disc(roms / "Stuck.iso", b"stuck")
    release = threading.Event()
    real = content_keys._sha1_of

    def stalls(path, *, progress):
        if path.endswith("Stuck.iso"):
            release.wait(30)   # a read that never moves
        return real(path, progress=progress)

    monkeypatch.setattr(content_keys, "_sha1_of", stalls)
    monkeypatch.setattr(content_keys, "STALL_SECONDS", 0.3)
    library = await _register(client, roms)
    try:
        await _find(client, library)
    finally:
        release.set()

    assert _names((await _duplicates(client))[0]) == ["Copy.iso", "Game.iso"]
    run = await _last_run(client, library)
    assert run["unreadable"] == 1
    assert run["computed"] == 2


@pytest.mark.asyncio
async def test_a_second_run_of_one_library_is_refused_while_one_runs(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services import content_keys

    roms = db / "roms"
    _disc(roms / "Game.iso", b"game")
    release = threading.Event()
    real = content_keys._sha1_of

    def held(path, *, progress):
        while not release.wait(0.01):
            progress()
        return real(path, progress=progress)

    monkeypatch.setattr(content_keys, "_sha1_of", held)
    library = await _register(client, roms)
    job_id = await _start(client, library)
    try:
        again = await client.post(f"/api/libraries/{library}/find-duplicates")
        assert again.status_code == 409
        assert again.json()["detail"]["job_id"] == job_id
    finally:
        release.set()
    assert (await _finished(client, job_id))["status"] == "completed"
    assert (await client.post(f"/api/libraries/{library}/find-duplicates")).status_code == 200


@pytest.mark.asyncio
async def test_an_unknown_library_is_404(db: Path, client: httpx.AsyncClient) -> None:
    response = await client.post("/api/libraries/folder:99/find-duplicates")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_removing_the_library_drops_its_groups_and_last_run(
    db: Path, client: httpx.AsyncClient,
) -> None:
    roms = db / "roms"
    _disc(roms / "Game.iso", b"game")
    _copy(roms / "Game.iso", roms / "Copy.iso")
    library = await _register(client, roms)
    await _find(client, library)
    assert len(await _duplicates(client)) == 1

    assert (await client.delete(f"/api/libraries/{library}")).status_code == 200

    listed = (await client.get("/api/findings")).json()
    assert [row for row in listed["findings"] if row["kind"] == "duplicate"] == []
    assert listed["duplicate_runs"] == []


# ----------------------------------------------------------------------
# the tool contract
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_labelled_raw_image_hashes_make_a_key(tmp_path: Path) -> None:
    """A tool's raw image SHA1 comes from its labelled hashes: a tool with no
    raw label (maxcso, nsz, jwud, z3ds) has none, whatever it embeds."""
    for tool_id in ("cso", "nsz", "jwud", "z3ds"):
        tool = registry.get(tool_id)
        assert tool.raw_image_hash_labels == frozenset()
        assert await tool.raw_image_sha1(str(tmp_path / "x.bin")) is None
    assert registry.get("dolphin").raw_image_hash_labels == frozenset({"dolphin_disc_sha1"})
    # chd_sha1 (the overall SHA1) is never one.
    assert registry.get("chdman").raw_image_hash_labels == frozenset({"chd_data_sha1"})
