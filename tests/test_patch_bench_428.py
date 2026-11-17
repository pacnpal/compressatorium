"""The patch bench (#428), through the HTTP API on real files.

A patch job reads a cartridge Image (loose or archived) and an IPS/BPS file,
and writes ``Name (Patched).7z`` beside the Image with romz's 7z packer. The
original never changes, and the job's Ledger row names the patch file and its
SHA1. When no ``7z`` is on the PATH, a stand-in that writes the archive with
py7zr takes its place; with one, the real binary runs.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx
import py7zr
import pytest
import pytest_asyncio
from fastapi import FastAPI

from config import settings as app_settings

from . import test_folder_library_303 as folder_library_303
from .test_patch_engine_428 import _action, _bps

_db_fixture = folder_library_303._db_fixture

ROM = bytes(range(256)) * 64  # 16 KiB


@pytest.fixture(autouse=True)
def _env(db: Path, tmp_path: Path, monkeypatch):
    from services.job_manager import job_manager
    from services.romz import romz_service

    monkeypatch.setattr(app_settings, "data_dir", str(tmp_path / "config"))
    monkeypatch.setattr(app_settings, "temp_dir", str(tmp_path / "config" / "temp"))
    sevenzip = shutil.which("7z")
    if sevenzip is None:
        stub = tmp_path / "bin" / "7z"
        stub.parent.mkdir()
        stub.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, py7zr\n"
            "args = sys.argv[sys.argv.index('--') + 1:]\n"
            "with py7zr.SevenZipFile(args[0], 'w') as z:\n"
            "    z.write(args[1], os.path.basename(args[1]))\n"
        )
        stub.chmod(0o755)
        sevenzip = str(stub)
    monkeypatch.setattr(romz_service, "sevenzip_path", sevenzip)
    finished: list = []
    monkeypatch.setattr(job_manager, "_terminal_listeners", [finished.append])
    return finished


@pytest_asyncio.fixture(name="client")
async def _client(db: Path):
    from routes import convert, ledger, patch

    app = FastAPI()
    for router in (convert.router, patch.router, ledger.router):
        app.include_router(router, prefix="/api")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _patch(client, image: str, patch: str) -> dict:
    """Start a patch and wait for its job; the finished job, as the API serves it."""
    from services import ledger, patch_bench
    from services.job_manager import job_manager

    started = await client.post("/api/patch", json={"image_path": image, "patch_path": patch})
    assert started.status_code == 200, started.text
    await patch_bench.drain()
    job_id = started.json()["job_id"]
    # The Ledger's own terminal listener, as main.py registers it.
    await ledger.record(job_manager.get_job(job_id))
    response = await client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _members(archive: Path) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as out:
        with py7zr.SevenZipFile(archive) as z:
            z.extractall(out)
        return {p.name: p.read_bytes() for p in Path(out).iterdir()}


def _target() -> bytes:
    return ROM[:100] + b"PATCHED!" + ROM[108:]


def _bps_patch(path: Path) -> str:
    # Made for ROM: SourceRead, TargetRead, SourceRead.
    actions = _action(0, 100) + _action(1, 8, b"PATCHED!") + _action(0, len(ROM) - 108)
    path.write_bytes(_bps(ROM, _target(), actions))
    return str(path)


def _ips_patch(path: Path) -> str:
    record = (100).to_bytes(3, "big") + (8).to_bytes(2, "big") + b"PATCHED!"
    path.write_bytes(b"PATCH" + record + b"EOF")
    return str(path)


async def _ledger_row(client) -> dict:
    rows = (await client.get("/api/ledger")).json()["items"]
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.asyncio
async def test_a_bps_patch_writes_a_patched_7z_and_a_ledger_row(db: Path, client) -> None:
    rom = db / "gba" / "Game.gba"
    rom.parent.mkdir()
    rom.write_bytes(ROM)
    patch = _bps_patch(db / "fix.bps")

    job = await _patch(client, str(rom), patch)

    assert job["status"] == "completed", job
    out = db / "gba" / "Game (Patched).7z"
    assert job["output_path"] == str(out)
    assert _members(out) == {"Game (Patched).gba": _target()}
    assert rom.read_bytes() == ROM
    row = await _ledger_row(client)
    assert row["mode"] == "patch"
    assert row["source_path"] == str(rom)
    assert row["output_path"] == str(out)
    assert row["extra_inputs"] == [
        {"path": patch, "sha1": hashlib.sha1(Path(patch).read_bytes()).hexdigest()},
    ]


@pytest.mark.asyncio
async def test_a_taken_name_gets_a_number(db: Path, client) -> None:
    rom = db / "Game.gba"
    rom.write_bytes(ROM)
    taken = db / "Game (Patched).7z"
    taken.write_bytes(b"an earlier patched copy")

    job = await _patch(client, str(rom), _bps_patch(db / "fix.bps"))

    assert job["status"] == "completed", job
    assert taken.read_bytes() == b"an earlier patched copy"
    assert _members(db / "Game (Patched) (2).7z") == {"Game (Patched) (2).gba": _target()}


@pytest.mark.asyncio
async def test_a_bps_patch_for_another_rom_is_refused(db: Path, client) -> None:
    rom = db / "Game.gba"
    rom.write_bytes(ROM[::-1])

    job = await _patch(client, str(rom), _bps_patch(db / "fix.bps"))

    assert job["status"] == "failed"
    assert "another ROM" in job["error_message"]
    assert sorted(p.name for p in db.iterdir()) == ["Game.gba", "fix.bps"]
    assert (await _ledger_row(client))["status"] == "failed"


@pytest.mark.asyncio
async def test_an_ips_patch_on_an_unrecognized_image_warns(db: Path, client) -> None:
    rom = db / "Game.gba"
    rom.write_bytes(ROM)

    job = await _patch(client, str(rom), _ips_patch(db / "fix.ips"))

    assert job["status"] == "completed", job
    assert "isn't Recognized" in job["warning"]
    assert _members(db / "Game (Patched).7z") == {"Game (Patched).gba": _target()}


@pytest.mark.asyncio
async def test_an_image_inside_a_zip_is_patched(db: Path, client) -> None:
    archive = db / "Game.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Game.gba", ROM)
    before = archive.read_bytes()
    patch = _ips_patch(db / "fix.ips")

    as_member = await _patch(client, f"{archive}::Game.gba", patch)
    as_archive = await _patch(client, str(archive), patch)

    assert as_member["status"] == as_archive["status"] == "completed"
    assert _members(db / "Game (Patched).7z") == {"Game (Patched).gba": _target()}
    assert _members(db / "Game (Patched) (2).7z") == {"Game (Patched) (2).gba": _target()}
    assert archive.read_bytes() == before


@pytest.mark.asyncio
async def test_an_archived_image_over_the_archive_limits_is_not_unpacked(
    db: Path, client, monkeypatch,
) -> None:
    archive = db / "Game.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Game.gba", ROM)
    monkeypatch.setattr(app_settings, "archive_max_member_size", 1024)

    job = await _patch(client, f"{archive}::Game.gba", _ips_patch(db / "fix.ips"))

    assert job["status"] == "failed"
    assert "max size" in job["error_message"]
    assert not (db / "Game (Patched).7z").exists()


@pytest.mark.asyncio
async def test_a_disc_image_is_refused_with_a_reason(db: Path, client) -> None:
    disc = db / "Game.iso"
    disc.write_bytes(ROM)
    response = await client.post(
        "/api/patch", json={"image_path": str(disc), "patch_path": _ips_patch(db / "fix.ips")},
    )
    assert response.status_code == 400
    assert "disc Image" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_uploaded_patch_is_kept_in_the_data_directory(db: Path, client, tmp_path) -> None:
    rom = db / "Game.gba"
    rom.write_bytes(ROM)
    data = Path(_bps_patch(tmp_path / "fix.bps"))
    uploaded = await client.post(
        "/api/patch/upload", files={"file": ("fix.bps", data.read_bytes())},
    )
    assert uploaded.status_code == 200, uploaded.text
    path = uploaded.json()["path"]
    assert path.startswith(str(tmp_path / "config" / "patches"))

    job = await _patch(client, str(rom), path)

    assert job["status"] == "completed", job
    assert _members(db / "Game (Patched).7z") == {"Game (Patched).gba": _target()}


@pytest.mark.asyncio
async def test_a_patch_over_its_size_limit_is_refused(db: Path, client, monkeypatch) -> None:
    monkeypatch.setattr(app_settings, "patch_max_size", 16)
    uploaded = await client.post(
        "/api/patch/upload", files={"file": ("big.ips", b"PATCH" + b"\x00" * 64)},
    )
    assert uploaded.status_code == 400
    assert "CHD_PATCH_MAX_SIZE" in uploaded.json()["detail"]
    rom = db / "Game.gba"
    rom.write_bytes(ROM)
    response = await client.post(
        "/api/patch", json={"image_path": str(rom), "patch_path": _ips_patch(db / "big.ips")},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_a_patch_outside_the_volumes_is_refused(db: Path, client, tmp_path) -> None:
    rom = db / "Game.gba"
    rom.write_bytes(ROM)
    response = await client.post(
        "/api/patch",
        json={"image_path": str(rom), "patch_path": _ips_patch(tmp_path / "elsewhere.ips")},
    )
    assert response.status_code == 404


def test_publish_without_hard_links_never_replaces_a_taken_name(tmp_path, monkeypatch) -> None:
    """With no hard links, a name another job claimed first is skipped, not replaced."""
    import os

    from services import patch_bench

    def no_links(*_a, **_k):
        raise OSError("hard links not supported")

    monkeypatch.setattr(os, "link", no_links)
    taken = tmp_path / "Game (Patched).7z"
    taken.write_bytes(b"first job")
    packed = tmp_path / ".partial"
    packed.write_bytes(b"second job")
    final = patch_bench._publish(str(packed), str(tmp_path), "Game", "Game (Patched)")
    assert final != str(taken)
    assert taken.read_bytes() == b"first job"
    assert Path(final).read_bytes() == b"second job"
    assert not packed.exists()


@pytest.mark.asyncio
async def test_a_patch_waits_for_a_conversion_slot(db: Path, client, tmp_path, monkeypatch) -> None:
    """A patch takes a slot like a conversion, so MAX_CONCURRENT_JOBS=1 holds it back."""
    import asyncio

    from services import patch_bench
    from services.concurrency_manager import ConcurrencyManager

    manager = ConcurrencyManager(1, str(tmp_path / "locks"))
    monkeypatch.setattr(patch_bench, "concurrency_manager", manager)
    assert await manager.acquire("a-conversion")
    rom = db / "gba" / "Game.gba"
    rom.parent.mkdir()
    rom.write_bytes(ROM)
    started = await client.post(
        "/api/patch", json={"image_path": str(rom), "patch_path": _bps_patch(db / "fix.bps")},
    )
    assert started.status_code == 200, started.text
    await asyncio.sleep(0.5)
    out = db / "gba" / "Game (Patched).7z"
    assert not out.exists()

    manager.release("a-conversion")
    await patch_bench.drain()
    assert _members(out) == {"Game (Patched).gba": _target()}


def test_a_failed_publish_removes_its_claim(tmp_path, monkeypatch) -> None:
    """Without hard links, a replace that fails leaves no empty archive behind."""
    import os

    from services import patch_bench

    def no_links(*_a, **_k):
        raise OSError("hard links not supported")

    def broken_replace(*_a, **_k):
        raise OSError("I/O error")

    monkeypatch.setattr(os, "link", no_links)
    monkeypatch.setattr(os, "replace", broken_replace)
    packed = tmp_path / ".partial"
    packed.write_bytes(b"job")
    with pytest.raises(OSError, match="I/O error"):
        patch_bench._publish(str(packed), str(tmp_path), "Game", "Game (Patched)")
    assert not (tmp_path / "Game (Patched).7z").exists()


@pytest.mark.asyncio
async def test_an_abandoned_rom_write_still_has_its_workspace_removed(
    db: Path, client, tmp_path, monkeypatch,
) -> None:
    """A write that fails part way (as a cancel or stall would leave it) leaves no workspace."""
    from services import patch_bench

    def failing_write(work: str, target: bytes, name: str) -> str:
        (Path(work) / name).write_bytes(target[:10])
        raise OSError("disk full")

    monkeypatch.setattr(patch_bench, "_write_rom", failing_write)
    rom = db / "gba" / "Game.gba"
    rom.parent.mkdir()
    rom.write_bytes(ROM)
    job = await _patch(client, str(rom), _bps_patch(db / "fix.bps"))
    assert job["status"] == "failed", job
    temp = Path(app_settings.temp_dir)
    assert not any(temp.glob("patch-*")), list(temp.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("archived", [False, True])
async def test_an_input_swapped_for_a_symlink_while_waiting_is_not_read(
    db: Path, client, tmp_path, monkeypatch, archived: bool,
) -> None:
    """The Image is checked when the request arrives; swapped for a symlink to a
    file outside the Volume while the job waits for a slot, it is not read."""
    import asyncio

    from services import patch_bench
    from services.concurrency_manager import ConcurrencyManager

    manager = ConcurrencyManager(1, str(tmp_path / "locks"))
    monkeypatch.setattr(patch_bench, "concurrency_manager", manager)
    assert await manager.acquire("a-conversion")
    outside = tmp_path / "outside"
    outside.mkdir()
    if archived:
        image = db / "Game.zip"
        with zipfile.ZipFile(image, "w") as z:
            z.writestr("Game.gba", ROM)
        with zipfile.ZipFile(outside / "Game.zip", "w") as z:
            z.writestr("Game.gba", b"SECRET" * 4096)
        request = f"{image}::Game.gba"
    else:
        image = db / "Game.gba"
        image.write_bytes(ROM)
        (outside / "Game.gba").write_bytes(b"SECRET" * 4096)
        request = str(image)
    started = await client.post(
        "/api/patch", json={"image_path": request, "patch_path": _ips_patch(db / "fix.ips")},
    )
    assert started.status_code == 200, started.text
    await asyncio.sleep(0.3)
    image.unlink()
    image.symlink_to(outside / image.name)
    manager.release("a-conversion")
    await patch_bench.drain()

    from services.job_manager import job_manager
    job = job_manager.get_job(started.json()["job_id"])
    assert job.status.value == "failed", job.status
    assert not list(db.glob("*Patched*"))
