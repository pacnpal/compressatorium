"""The Patch action (#428), driven through its HTTP routes on real files.

A person picks a cartridge Image (loose, or inside a zip or 7z) and an IPS or
BPS patch, from a Volume or uploaded to the app's data directory. The app
writes ``Name (Patched).7z`` beside the Image, numbered when that name is
taken, and never changes the Image. The Ledger row names the patch file and
its SHA1. A BPS patch made for another ROM is refused; an IPS patch on a ROM
that isn't Recognized is applied with a warning; a disc Image is refused.

The 7z packer is the app's own (romz's ``7z a``). This machine may have no
``7z``, so the binary is a small stand-in script that writes the archive with
py7zr, the library the app reads it back with.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx
import py7zr
import pytest
import pytest_asyncio
from fastapi import FastAPI

from . import test_folder_library_303 as folder_library_303
from .test_patch_engine_428 import (
    SOURCE_READ,
    TARGET_READ,
    _action,
    _bps,
    _ips,
    _record,
)

_db_fixture = folder_library_303._db_fixture

ROM = bytes(range(256)) * 64  # 16 KiB


def _app() -> FastAPI:
    from routes import patch

    app = FastAPI()
    app.include_router(patch.router, prefix="/api")
    return app


_STUB_7Z = """\
import os, sys
import py7zr
args = sys.argv[1:]
assert args[0] == "a", args
out, src = args[args.index("--") + 1:]
with py7zr.SevenZipFile(out, "w") as archive:
    archive.write(src, os.path.basename(src))
"""


@pytest.fixture(name="env")
def _env(db: Path, tmp_path: Path, monkeypatch):
    """A Volume (``db``), a data directory outside it and the stand-in 7z."""
    from services.romz import romz_service

    from config import settings

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "data_dir", str(data))
    monkeypatch.setattr(settings, "temp_dir", str(data / "temp"))
    stub = tmp_path / "bin" / "7z"
    stub.parent.mkdir()
    stub.write_text(f"#!{sys.executable}\n{_STUB_7Z}")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(romz_service, "sevenzip_path", str(stub))
    return db


@pytest_asyncio.fixture(name="client")
async def _client(env: Path):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _ips_fix() -> tuple[bytes, bytes]:
    """An IPS patch for ROM and the ROM it makes."""
    patch = _ips(_record(16, b"PATCHED!"))
    target = bytearray(ROM)
    target[16:24] = b"PATCHED!"
    return patch, bytes(target)


def _bps_fix(made_for: bytes = ROM) -> tuple[bytes, bytes]:
    target = made_for[:100] + b"translated" + made_for[110:]
    actions = (
        _action(SOURCE_READ, 100) + _action(TARGET_READ, 10) + b"translated"
        + _action(SOURCE_READ, len(made_for) - 110)
    )
    return _bps(made_for, target, actions), target


async def _run(client: httpx.AsyncClient, body: dict) -> dict:
    """Start a patch job and wait for it: the finished job as the API shows it."""
    from services import patch_bench

    started = await client.post("/api/patch", json=body)
    assert started.status_code == 200, started.text
    await patch_bench.drain()
    from services.job_manager import job_manager

    job = job_manager.get_job_for_lookup(started.json()["job_id"])
    assert job is not None
    return job


async def _ledger_row(job) -> dict:
    from services import ledger

    await ledger.record(job)
    [row] = await ledger.recent(1)
    return row


def _member(archive: Path) -> tuple[str, bytes]:
    with tempfile.TemporaryDirectory() as out, py7zr.SevenZipFile(archive, "r") as z:
        names = z.getnames()
        assert len(names) == 1, names
        z.extractall(path=out)
        return names[0], (Path(out) / names[0]).read_bytes()


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# ----------------------------------------------------------------------
# a loose ROM
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_loose_rom_is_patched_beside_itself_and_never_changed(
    env: Path, client: httpx.AsyncClient,
) -> None:
    rom = _write(env / "roms" / "snes" / "Game.sfc", ROM)
    before = rom.stat()
    patch_bytes, target = _ips_fix()
    patch = _write(env / "patches" / "Fix.ips", patch_bytes)

    job = await _run(client, {"image_path": str(rom), "patch_path": str(patch)})

    assert job.status.value == "completed", job.error_message
    out = rom.parent / "Game (Patched).7z"
    assert job.output_path == str(out)
    name, data = _member(out)
    assert name == "Game (Patched).sfc"
    assert data == target
    # The original is the same file, unchanged.
    assert rom.read_bytes() == ROM
    after = rom.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (
        before.st_ino, before.st_mtime_ns, before.st_size,
    )
    # Nothing else was left beside it.
    assert sorted(p.name for p in rom.parent.iterdir()) == ["Game (Patched).7z", "Game.sfc"]


@pytest.mark.asyncio
async def test_the_ledger_names_the_image_the_patch_file_and_its_sha1(
    env: Path, client: httpx.AsyncClient,
) -> None:
    rom = _write(env / "roms" / "Game.sfc", ROM)
    patch_bytes, target = _bps_fix()
    patch = _write(env / "patches" / "Translation.bps", patch_bytes)

    job = await _run(client, {"image_path": str(rom), "patch_path": str(patch)})
    row = await _ledger_row(job)

    assert row["mode"] == "patch"
    assert row["status"] == "completed"
    assert row["source_path"] == str(rom)
    assert row["output_path"] == str(rom.parent / "Game (Patched).7z")
    assert row["extra_inputs"] == [
        {"kind": "patch", "path": str(patch), "sha1": _sha1(patch_bytes)},
    ]
    assert row["input_bytes"] == len(ROM)
    assert row["output_bytes"] == (rom.parent / "Game (Patched).7z").stat().st_size
    # The packed archive was read back and holds the patched ROM.
    assert row["check_passed"] == "intact"
    assert row["source_deleted"] is False
    assert _member(rom.parent / "Game (Patched).7z")[1] == target


@pytest.mark.asyncio
async def test_a_taken_name_gets_a_number_and_the_earlier_copy_is_kept(
    env: Path, client: httpx.AsyncClient,
) -> None:
    rom = _write(env / "roms" / "Game.gba", ROM)
    earlier = _write(env / "roms" / "Game (Patched).7z", b"an earlier patched copy")
    patch = _write(env / "Fix.ips", _ips_fix()[0])

    job = await _run(client, {"image_path": str(rom), "patch_path": str(patch)})

    assert job.status.value == "completed", job.error_message
    assert job.output_path == str(env / "roms" / "Game (Patched) (2).7z")
    assert earlier.read_bytes() == b"an earlier patched copy"
    job = await _run(client, {"image_path": str(rom), "patch_path": str(patch)})
    assert job.output_path == str(env / "roms" / "Game (Patched) (3).7z")


# ----------------------------------------------------------------------
# a ROM inside an archive
# ----------------------------------------------------------------------


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _7z(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(data, name)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("pack", [_zip, _7z])
async def test_a_rom_inside_a_zip_or_7z_is_patched_beside_the_archive(
    env: Path, client: httpx.AsyncClient, pack,
) -> None:
    suffix = ".zip" if pack is _zip else ".7z"
    archive = pack(env / "roms" / f"Game{suffix}", {"Game.gba": ROM})
    archived = archive.read_bytes()
    patch_bytes, target = _bps_fix()
    patch = _write(env / "Fix.bps", patch_bytes)

    job = await _run(client, {"image_path": str(archive), "patch_path": str(patch)})

    assert job.status.value == "completed", job.error_message
    out = archive.parent / "Game (Patched).7z"
    assert job.output_path == str(out)
    assert _member(out) == ("Game (Patched).gba", target)
    assert archive.read_bytes() == archived


@pytest.mark.asyncio
async def test_a_named_member_of_an_archive_is_the_one_patched(
    env: Path, client: httpx.AsyncClient,
) -> None:
    archive = _zip(env / "Pack.zip", {"readme.txt": b"hi", "Hack/Base.nes": ROM})
    patch = _write(env / "Fix.ips", _ips_fix()[0])

    job = await _run(
        client, {"image_path": f"{archive}::Hack/Base.nes", "patch_path": str(patch)},
    )

    assert job.status.value == "completed", job.error_message
    assert job.output_path == str(env / "Base (Patched).7z")
    assert _member(env / "Base (Patched).7z") == ("Base (Patched).nes", _ips_fix()[1])


@pytest.mark.asyncio
async def test_an_archive_holding_several_files_needs_its_member_named(
    env: Path, client: httpx.AsyncClient,
) -> None:
    archive = _zip(env / "Pack.zip", {"A.gba": ROM, "B.gba": ROM})
    patch = _write(env / "Fix.ips", _ips_fix()[0])

    job = await _run(client, {"image_path": str(archive), "patch_path": str(patch)})

    assert job.status.value == "failed"
    assert "more than one file" in job.error_message
    assert sorted(p.name for p in env.iterdir()) == ["Fix.ips", "Pack.zip"]


@pytest.mark.asyncio
async def test_the_archive_limits_apply_to_an_archived_rom(
    env: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from config import settings

    monkeypatch.setattr(settings, "archive_max_member_size", 1024)
    archive = _zip(env / "Game.zip", {"Game.gba": ROM})
    patch = _write(env / "Fix.ips", _ips_fix()[0])

    job = await _run(client, {"image_path": str(archive), "patch_path": str(patch)})

    assert job.status.value == "failed"
    assert "CHD_ARCHIVE_MAX_MEMBER_SIZE" in job.error_message
    assert not (env / "Game (Patched).7z").exists()


# ----------------------------------------------------------------------
# what the patch proves
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bps_patch_made_for_another_rom_is_refused_and_writes_nothing(
    env: Path, client: httpx.AsyncClient,
) -> None:
    rom = _write(env / "Game.sfc", ROM)
    patch = _write(env / "Fix.bps", _bps_fix(made_for=b"\x00" * len(ROM))[0])

    job = await _run(client, {"image_path": str(rom), "patch_path": str(patch)})

    assert job.status.value == "failed"
    assert "isn't the one this patch was made for" in job.error_message
    assert sorted(p.name for p in env.iterdir()) == ["Fix.bps", "Game.sfc"]
    row = await _ledger_row(job)
    assert row["status"] == "failed"
    assert row["output_path"] is None


@pytest.mark.asyncio
async def test_an_ips_patch_on_a_rom_that_isnt_recognized_warns_but_applies(
    env: Path, client: httpx.AsyncClient,
) -> None:
    rom = _write(env / "Game.sfc", ROM)
    patch = _write(env / "Fix.ips", _ips_fix()[0])

    job = await _run(client, {"image_path": str(rom), "patch_path": str(patch)})

    assert job.status.value == "completed"
    assert "isn't Recognized" in (job.warning or "")
    assert (env / "Game (Patched).7z").exists()


@pytest.mark.asyncio
async def test_an_ips_patch_on_a_recognized_rom_has_no_warning(
    env: Path, client: httpx.AsyncClient,
) -> None:
    from services.dat_store import dat_store

    dat = _write(env.parent / "snes.dat", (
        '<?xml version="1.0"?><datafile><header><name>Nintendo - SNES</name></header>'
        f'<game name="Game"><rom name="Game.sfc" size="{len(ROM)}" sha1="{_sha1(ROM)}"/>'
        "</game></datafile>"
    ).encode())
    await dat_store.import_dat(str(dat))
    rom = _write(env / "Game.sfc", ROM)
    patch = _write(env / "Fix.ips", _ips_fix()[0])

    job = await _run(client, {"image_path": str(rom), "patch_path": str(patch)})

    assert job.status.value == "completed", job.error_message
    assert job.warning is None


@pytest.mark.asyncio
async def test_a_bps_patch_needs_no_warning_its_checksums_prove_it(
    env: Path, client: httpx.AsyncClient,
) -> None:
    rom = _write(env / "Game.sfc", ROM)
    patch = _write(env / "Fix.bps", _bps_fix()[0])

    job = await _run(client, {"image_path": str(rom), "patch_path": str(patch)})

    assert job.status.value == "completed", job.error_message
    assert job.warning is None


# ----------------------------------------------------------------------
# refused before a job starts
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Disc.iso", "Disc.cue", "Disc.chd", "Disc.rvz", "Disc.cso"])
async def test_a_disc_image_is_refused_with_a_reason(
    env: Path, client: httpx.AsyncClient, name: str,
) -> None:
    image = _write(env / name, b"\x00" * 64)
    patch = _write(env / "Fix.ips", _ips_fix()[0])

    response = await client.post(
        "/api/patch", json={"image_path": str(image), "patch_path": str(patch)},
    )

    assert response.status_code == 400
    assert "disc Image" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_disc_image_inside_an_archive_is_refused(
    env: Path, client: httpx.AsyncClient,
) -> None:
    archive = _zip(env / "Disc.zip", {"Disc.iso": b"\x00" * 64})
    patch = _write(env / "Fix.ips", _ips_fix()[0])

    named = await client.post(
        "/api/patch", json={"image_path": f"{archive}::Disc.iso", "patch_path": str(patch)},
    )
    assert named.status_code == 400
    assert "disc Image" in named.json()["detail"]

    job = await _run(client, {"image_path": str(archive), "patch_path": str(patch)})
    assert job.status.value == "failed"
    assert "disc Image" in job.error_message


@pytest.mark.asyncio
async def test_an_image_or_patch_outside_every_volume_is_refused(
    env: Path, client: httpx.AsyncClient, tmp_path: Path,
) -> None:
    inside_rom = _write(env / "Game.sfc", ROM)
    inside_patch = _write(env / "Fix.ips", _ips_fix()[0])
    outside_rom = _write(tmp_path / "elsewhere" / "Game.sfc", ROM)
    outside_patch = _write(tmp_path / "elsewhere" / "Fix.ips", _ips_fix()[0])

    for body in (
        {"image_path": str(outside_rom), "patch_path": str(inside_patch)},
        {"image_path": str(inside_rom), "patch_path": str(outside_patch)},
    ):
        response = await client.post("/api/patch", json=body)
        assert response.status_code == 400, body
        assert "Volume" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_patch_through_a_symlink_out_of_the_volume_is_not_read(
    env: Path, client: httpx.AsyncClient, tmp_path: Path,
) -> None:
    rom = _write(env / "Game.sfc", ROM)
    real = _write(tmp_path / "elsewhere" / "Fix.ips", _ips_fix()[0])
    link = env / "Fix.ips"
    os.symlink(real, link)

    response = await client.post(
        "/api/patch", json={"image_path": str(rom), "patch_path": str(link)},
    )

    assert response.status_code == 400
    assert not (env / "Game (Patched).7z").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"patch_path": "PATCH"},  # no Image
    {"image_path": "ROM"},  # no patch
    {"image_path": "ROM", "patch_path": "PATCH", "upload_id": "x.ips"},  # both kinds
    {"image_path": "ROM", "patch_path": "README"},  # not .ips or .bps
    {"image_path": "ROM", "upload_id": "../../etc/passwd"},
    {"image_path": "ROM", "upload_id": "missing.ips"},
    {"image_path": "ROM", "upload_id": "0123456789abcdef-Gone.ips"},  # never uploaded
    {"image_path": "ROM.rar", "patch_path": "PATCH"},  # neither zip nor 7z
])
async def test_a_request_that_names_no_usable_image_or_patch_is_refused(
    env: Path, client: httpx.AsyncClient, body: dict,
) -> None:
    _write(env / "Game.sfc", ROM)
    _write(env / "Game.rar", b"Rar!")
    _write(env / "Fix.ips", _ips_fix()[0])
    _write(env / "README.txt", b"hi")
    names = {
        "ROM": str(env / "Game.sfc"), "ROM.rar": str(env / "Game.rar"),
        "PATCH": str(env / "Fix.ips"), "README": str(env / "README.txt"),
    }
    resolved = {k: names.get(v, v) for k, v in body.items()}

    response = await client.post("/api/patch", json=resolved)

    assert response.status_code in (400, 422), response.text


@pytest.mark.asyncio
async def test_no_job_starts_when_the_7z_packer_is_missing(
    env: Path, client: httpx.AsyncClient, monkeypatch, tmp_path: Path,
) -> None:
    from services.romz import romz_service

    monkeypatch.setattr(romz_service, "sevenzip_path", str(tmp_path / "no-such-7z"))
    rom = _write(env / "Game.sfc", ROM)
    patch = _write(env / "Fix.ips", _ips_fix()[0])

    response = await client.post(
        "/api/patch", json={"image_path": str(rom), "patch_path": str(patch)},
    )

    assert response.status_code == 503
    assert "7z" in response.json()["detail"]


# ----------------------------------------------------------------------
# an uploaded patch
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_uploaded_patch_is_kept_in_the_data_directory_and_applied(
    env: Path, client: httpx.AsyncClient,
) -> None:
    from config import settings

    rom = _write(env / "Game.sfc", ROM)
    patch_bytes, target = _bps_fix()

    uploaded = await client.post(
        "/api/patch/upload", files={"file": ("My Translation.bps", patch_bytes)},
    )
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    stored = Path(settings.data_dir) / "patches" / body["upload_id"]
    assert stored.read_bytes() == patch_bytes
    assert body["name"] == "My Translation.bps"
    assert body["sha1"] == _sha1(patch_bytes)

    job = await _run(client, {"image_path": str(rom), "upload_id": body["upload_id"]})

    assert job.status.value == "completed", job.error_message
    assert _member(env / "Game (Patched).7z")[1] == target
    row = await _ledger_row(job)
    assert row["extra_inputs"] == [
        {"kind": "patch", "path": str(stored), "sha1": _sha1(patch_bytes)},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["..IPS", "../../etc/Fix.ips", "dir\\Fix:1.IPS", "Ünï code.ips"])
async def test_an_upload_with_an_awkward_name_is_kept_under_a_plain_one(
    env: Path, client: httpx.AsyncClient, name: str,
) -> None:
    from config import settings

    rom = _write(env / "Game.sfc", ROM)
    uploaded = await client.post("/api/patch/upload", files={"file": (name, _ips_fix()[0])})
    assert uploaded.status_code == 200, uploaded.text
    upload_id = uploaded.json()["upload_id"]
    assert "/" not in upload_id and upload_id.endswith(".ips")
    assert (Path(settings.data_dir) / "patches" / upload_id).is_file()

    job = await _run(client, {"image_path": str(rom), "upload_id": upload_id})

    assert job.status.value == "completed", job.error_message


@pytest.mark.asyncio
async def test_an_upload_over_the_size_limit_is_refused_and_not_kept(
    env: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from config import settings

    monkeypatch.setattr(settings, "patch_max_patch_size", 1000)
    response = await client.post(
        "/api/patch/upload", files={"file": ("Big.ips", b"PATCH" + b"\x00" * 2000)},
    )

    assert response.status_code == 413
    folder = Path(settings.data_dir) / "patches"
    assert not folder.exists() or list(folder.iterdir()) == []


@pytest.mark.asyncio
async def test_an_upload_that_isnt_ips_or_bps_is_refused(
    env: Path, client: httpx.AsyncClient,
) -> None:
    named = await client.post("/api/patch/upload", files={"file": ("Fix.xdelta", b"PATCH")})
    assert named.status_code == 400
    lying = await client.post("/api/patch/upload", files={"file": ("Fix.ips", b"not a patch")})
    assert lying.status_code == 400


# The cap is held while the body arrives, not after it has been spooled
# (codex 4605274134): a body of many times the cap is read only up to the cap
# and a little multipart framing, whether it declares its length or not.
_BOUNDARY = "patchboundary"
_BIG = 8 * 1024 * 1024
_SLICE = 64 * 1024


class _Body:
    """A multipart upload of *size* bytes, handed over a slice at a time;
    ``sent`` is how much of it the server has pulled."""

    def __init__(self, name: str, size: int) -> None:
        self.head = (
            f"--{_BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + b"PATCH"
        self.tail = f"\r\n--{_BOUNDARY}--\r\n".encode()
        self.size = size
        self.sent = 0

    @property
    def length(self) -> int:
        return len(self.head) + self.size - 5 + len(self.tail)

    async def __aiter__(self):
        self.sent += len(self.head)
        yield self.head
        left = self.size - 5
        while left > 0:
            chunk = b"\x00" * min(_SLICE, left)
            left -= len(chunk)
            self.sent += len(chunk)
            yield chunk
        self.sent += len(self.tail)
        yield self.tail


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
async def test_an_oversized_upload_is_cut_off_as_it_arrives(
    env: Path, client: httpx.AsyncClient, monkeypatch, tmp_path: Path, declared: bool,
) -> None:
    from config import settings

    monkeypatch.setattr(settings, "patch_max_patch_size", 1000)
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    body = _Body("Big.ips", _BIG)
    headers = {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"}
    if declared:
        headers["Content-Length"] = str(body.length)

    response = await client.post("/api/patch/upload", content=body, headers=headers)

    assert response.status_code == 413, response.text
    # Read no further than the cap and its framing: never the whole body.
    assert body.sent <= 1000 + 128 * 1024
    if declared:
        assert body.sent == 0  # refused on its declared length alone
    folder = Path(settings.data_dir) / "patches"
    assert not folder.exists() or list(folder.iterdir()) == []
    assert list(spool.iterdir()) == []


@pytest.mark.asyncio
async def test_an_upload_of_exactly_the_cap_is_kept(
    env: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from config import settings

    patch_bytes = _ips_fix()[0]
    monkeypatch.setattr(settings, "patch_max_patch_size", len(patch_bytes))
    uploaded = await client.post("/api/patch/upload", files={"file": ("Fix.ips", patch_bytes)})

    assert uploaded.status_code == 200, uploaded.text
    stored = Path(settings.data_dir) / "patches" / uploaded.json()["upload_id"]
    assert stored.read_bytes() == patch_bytes


# ----------------------------------------------------------------------
# a cancel while the archive is being placed
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancel_during_placement_still_records_the_placed_archive(
    env: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """Placing is irreversible, so a cancel that lands while the link is made
    waits for it, and the job records what was placed (codex 4605274145)."""
    import asyncio
    import threading
    import time

    from services import patch_bench
    from services.job_manager import job_manager

    rom = _write(env / "roms" / "Game.sfc", ROM)
    patch = _write(env / "Fix.ips", _ips_fix()[0])
    loop = asyncio.get_running_loop()
    started = threading.Event()
    job_id: dict = {}
    real_place = patch_bench._place

    def slow_place(staged: str, folder: str, stem: str) -> str:
        started.wait(5)
        asyncio.run_coroutine_threadsafe(job_manager.cancel_job(job_id["id"]), loop).result(5)
        time.sleep(0.3)  # the cancel is in before the link is made
        return real_place(staged, folder, stem)

    monkeypatch.setattr(patch_bench, "_place", slow_place)
    response = await client.post(
        "/api/patch", json={"image_path": str(rom), "patch_path": str(patch)},
    )
    assert response.status_code == 200, response.text
    job_id["id"] = response.json()["job_id"]
    started.set()
    await patch_bench.drain()
    job = job_manager.get_job_for_lookup(job_id["id"])

    out = rom.parent / "Game (Patched).7z"
    assert out.is_file()
    assert job.status.value == "completed", job.message
    assert job.output_path == str(out)
    assert "nothing was written" not in (job.message or "")
    row = await _ledger_row(job)
    assert row["output_path"] == str(out)
    assert row["output_bytes"] == out.stat().st_size
