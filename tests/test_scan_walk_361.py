"""Listing files is bounded by a stall bound and cancellable (#361).

A Volume mount that freezes blocks a directory listing in the kernel. The
metadata scan's walk (``collect_scannable_paths``), the browser's folder
listing and its recursive search ran that listing with no bound, so a frozen
mount held the scan (``_is_scanning``, the ``metadata_scan`` lane) or a request
forever, and Cancel could not stop it. Now each runs on a detached thread
under a stall bound that resets on every entry it reads: a frozen listing
ends at the bound with a warning naming the folder, Cancel ends it at once,
and a large tree that keeps moving is never cut off.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import threading
import time
import zipfile
import zlib

import pytest
from fastapi import BackgroundTasks, HTTPException

BOUND = 5.0     # far past the shrunk stall bound; an unbounded walk fails here
STALL = 0.3


@pytest.fixture(name="frozen")
def _frozen(monkeypatch):
    """``os.scandir``/``os.listdir`` on a folder named ``Frozen`` block on an
    Event that never fires (until teardown), as a frozen mount does."""
    release = threading.Event()
    real_scandir, real_listdir = os.scandir, os.listdir
    hits: list[str] = []

    def is_frozen(path) -> bool:
        return os.path.basename(os.fspath(path)) == "Frozen" and not release.is_set()

    def scandir(path="."):
        if is_frozen(path):
            hits.append(os.fspath(path))
            release.wait(60)
        return real_scandir(path)

    def listdir(path="."):
        if is_frozen(path):
            hits.append(os.fspath(path))
            release.wait(60)
        return real_listdir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    yield hits, release
    release.set()


def _scan_env(tmp_path, monkeypatch, *, stall=STALL):
    from routes import info as info_routes

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(info_routes, "_WALK_STALL_SECONDS", stall, raising=False)

    async def fresh(_path):
        return False

    async def nothing(*_a, **_k):
        return None

    store = info_routes.chd_metadata_store
    monkeypatch.setattr(store, "flush_async", nothing)
    monkeypatch.setattr(store, "is_stale", fresh)
    monkeypatch.setattr(store, "is_disc_id_checked", fresh)
    monkeypatch.setattr(store, "mark_disc_id_checked", nothing)

    async def no_match(_job_id, paths, *, force):
        return len(paths), 0

    monkeypatch.setattr(info_routes, "_scan_phase_dat_match", no_match)

    created: list[str] = []
    original = info_routes.job_manager.create_external_job

    def capture(*args, **kwargs):
        job = original(*args, **kwargs)
        created.append(job.id)
        return job

    monkeypatch.setattr(info_routes.job_manager, "create_external_job", capture)
    return info_routes, created


def _lane_in_use() -> int:
    from services.workload_limiter import workload_limiter

    return workload_limiter.in_use("metadata_scan")


# --- the metadata scan ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_frozen_folder_ends_the_scan_at_the_bound_with_a_warning(
    tmp_path, frozen, monkeypatch, caplog,
):
    hits, _release = frozen
    (tmp_path / "Frozen").mkdir()
    info_routes, created = _scan_env(tmp_path, monkeypatch)

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)

    job = info_routes.job_manager.jobs[created[0]]
    frozen_dir = str(tmp_path / "Frozen")
    assert hits == [frozen_dir]
    assert job.status.value == "failed"
    assert frozen_dir in job.error_message
    assert any(
        r.levelno == logging.WARNING and frozen_dir in r.getMessage() for r in caplog.records
    )
    assert info_routes._is_scanning is False
    assert _lane_in_use() == 0


@pytest.mark.asyncio
async def test_cancel_during_a_frozen_walk_stops_the_scan_at_once(
    tmp_path, monkeypatch,
):
    (tmp_path / "Frozen").mkdir()
    info_routes, created = _scan_env(tmp_path, monkeypatch, stall=30)
    release = threading.Event()
    real_scandir = os.scandir
    loop = asyncio.get_running_loop()
    cancelled_at: list[float] = []

    def scandir(path="."):
        if os.path.basename(os.fspath(path)) == "Frozen" and not release.is_set():
            cancelled_at.append(time.monotonic())
            asyncio.run_coroutine_threadsafe(
                info_routes.job_manager.cancel_job(created[0]), loop,
            )
            release.wait(60)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    try:
        await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)
        ended = time.monotonic()
    finally:
        release.set()

    assert ended - cancelled_at[0] < 1.0
    assert info_routes.job_manager.jobs[created[0]].status.value == "cancelled"
    assert info_routes._is_scanning is False
    assert _lane_in_use() == 0


@pytest.mark.asyncio
async def test_the_next_scan_starts_after_a_stalled_one(tmp_path, frozen, monkeypatch):
    _hits, release = frozen
    (tmp_path / "Frozen").mkdir()
    (tmp_path / "Frozen" / "Game.chd").write_bytes(b"x")
    info_routes, created = _scan_env(tmp_path, monkeypatch)
    await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)
    assert info_routes.job_manager.jobs[created[0]].status.value == "failed"

    release.set()  # the mount answers again
    tasks = BackgroundTasks()
    answer = await info_routes.trigger_metadata_scan(tasks, force=False)
    assert answer["status"] == "started"
    await asyncio.wait_for(tasks.tasks[0].func(*tasks.tasks[0].args), BOUND)

    assert info_routes.job_manager.jobs[created[1]].status.value == "completed"
    assert info_routes._is_scanning is False
    assert _lane_in_use() == 0


@pytest.mark.asyncio
async def test_a_large_tree_that_keeps_moving_is_not_cut_off(tmp_path, monkeypatch):
    """Each folder answers well inside the stall bound, but the whole walk
    takes several bounds: the bound is for no progress, not for the walk."""
    folders = 12
    for index in range(folders):
        folder = tmp_path / f"Disc {index:02d}"
        folder.mkdir()
        (folder / "Game.chd").write_bytes(b"x")
    info_routes, created = _scan_env(tmp_path, monkeypatch)
    real_scandir = os.scandir
    seen: list[str] = []

    def slow_but_alive(path="."):
        time.sleep(STALL / 3)
        result = real_scandir(path)
        seen.append(os.fspath(path))
        return result

    monkeypatch.setattr(os, "scandir", slow_but_alive)
    started = time.monotonic()
    await asyncio.wait_for(info_routes.scan_metadata_task(force=False), BOUND)

    assert time.monotonic() - started > 2 * STALL
    job = info_routes.job_manager.jobs[created[0]]
    assert job.status.value == "completed", job.error_message
    assert len(seen) > folders


# --- the file browser ----------------------------------------------------------


@pytest.fixture(name="files_env")
def _files_env(tmp_path, monkeypatch):
    from routes import files as files_routes
    from routes import volume_guard

    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(volume_guard, "WALK_STALL_SECONDS", STALL, raising=False)
    (tmp_path / "Frozen").mkdir()
    return files_routes


async def _answer_of(call) -> int:
    try:
        await asyncio.wait_for(call, BOUND)
    except HTTPException as exc:
        return exc.status_code
    return 200


@pytest.mark.asyncio
async def test_listing_a_frozen_folder_answers_503(tmp_path, files_env, frozen):
    status = await _answer_of(
        files_env.list_files(path=str(tmp_path / "Frozen"), show_archives=True,
                             summarize_archives=True),
    )
    assert status == 503


@pytest.mark.asyncio
async def test_searching_under_a_frozen_folder_answers_503(tmp_path, files_env, frozen):
    status = await _answer_of(
        files_env.search_files(path=str(tmp_path), recursive=True, include_archives=True),
    )
    assert status == 503


class _SlowEntries:
    """``os.scandir`` on a slow but healthy mount: entries arrive one at a
    time, each well inside the bound; ``first_blocks`` instead blocks before
    the first entry, as a frozen mount does after the open succeeds."""

    def __init__(self, entries, delay, first_blocks=None):
        self._entries, self._delay, self._first_blocks = entries, delay, first_blocks

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        if self._first_blocks is not None:
            gate, self._first_blocks = self._first_blocks, None
            gate.wait(60)
        time.sleep(self._delay)
        return next(self._entries)

    def close(self):
        self._entries.close()


ENTRIES = 12        # entries in the big folder
DELAY = STALL / 4   # each entry well inside the bound; all of them past it


@pytest.fixture(name="slow_folder")
def _slow_folder(tmp_path, monkeypatch):
    """A folder named ``Big`` whose entries arrive slowly: ``os.scandir`` per
    entry, ``os.listdir`` all at once after the same total time."""
    big = tmp_path / "Big"
    big.mkdir()
    names = [f"Game {index:02d}.iso" for index in reversed(range(ENTRIES))]
    for name in names:
        (big / name).write_bytes(b"x")
    real_scandir, real_listdir = os.scandir, os.listdir

    def scandir(path="."):
        if os.path.basename(os.fspath(path)) == "Big":
            return _SlowEntries(real_scandir(path), DELAY)
        return real_scandir(path)

    def listdir(path="."):
        if os.path.basename(os.fspath(path)) == "Big":
            time.sleep(DELAY * ENTRIES)
        return real_listdir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    return big, sorted(names)


@pytest.mark.asyncio
async def test_a_big_folder_that_keeps_answering_is_listed_in_order(
    files_env, slow_folder,
):
    big, names = slow_folder
    listing = await asyncio.wait_for(
        files_env.list_files(path=str(big), show_archives=True, summarize_archives=True),
        BOUND,
    )
    assert [entry.name for entry in listing.entries] == names


@pytest.mark.asyncio
async def test_a_search_through_a_big_folder_that_keeps_answering_completes(
    files_env, slow_folder,
):
    big, names = slow_folder
    found = await asyncio.wait_for(
        files_env.search_files(path=str(big), recursive=True, include_archives=True),
        BOUND,
    )
    assert [row["name"] for row in found["files"]] == names


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["list", "search"])
async def test_a_listing_that_blocks_before_its_first_entry_ends_at_the_bound(
    tmp_path, files_env, monkeypatch, route,
):
    release = threading.Event()
    real_scandir = os.scandir

    def scandir(path="."):
        if os.path.basename(os.fspath(path)) == "Frozen":
            return _SlowEntries(real_scandir(path), 0, first_blocks=release)
        return real_scandir(path)

    (tmp_path / "Frozen" / "Game.iso").write_bytes(b"x")
    monkeypatch.setattr(os, "scandir", scandir)
    if route == "list":
        call = files_env.list_files(
            path=str(tmp_path / "Frozen"), show_archives=True, summarize_archives=True,
        )
    else:
        call = files_env.search_files(path=str(tmp_path), recursive=True, include_archives=True)
    try:
        assert await _answer_of(call) == 503
    finally:
        release.set()


def _big_archive(folder, count=ENTRIES):
    folder.mkdir(exist_ok=True)
    archive = folder / "Pack.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for index in range(count):
            zf.writestr(f"Game {index:02d}.iso", b"x")
    return archive


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["list", "search"])
async def test_an_archive_whose_member_checks_keep_moving_is_not_cut_off(
    tmp_path, files_env, monkeypatch, route,
):
    """Each member's check is quick, all of them together pass the bound."""
    archive = _big_archive(tmp_path / "Packs")
    real = files_env._detect_archive_member_outputs
    checked: list[str] = []

    def slow(entry, archive_dir):
        time.sleep(DELAY)
        checked.append(entry["name"])
        return real(entry, archive_dir)

    monkeypatch.setattr(files_env, "_detect_archive_member_outputs", slow)
    if route == "list":
        call = files_env.list_files(
            path=str(archive.parent), show_archives=True, summarize_archives=True,
        )
    else:
        call = files_env.search_files(
            path=str(archive.parent), recursive=True, include_archives=True,
        )

    assert await _answer_of(call) == 200
    assert len(checked) == ENTRIES


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["list", "search"])
async def test_an_archive_member_check_that_blocks_ends_at_the_bound(
    tmp_path, files_env, monkeypatch, route,
):
    archive = _big_archive(tmp_path / "Packs", count=2)
    release = threading.Event()

    def blocked(_entry, _archive_dir):
        release.wait(60)
        return None, [], [], {}

    monkeypatch.setattr(files_env, "_detect_archive_member_outputs", blocked)
    if route == "list":
        call = files_env.list_files(
            path=str(archive.parent), show_archives=True, summarize_archives=True,
        )
    else:
        call = files_env.search_files(
            path=str(archive.parent), recursive=True, include_archives=True,
        )
    try:
        assert await _answer_of(call) == 503
    finally:
        release.set()


class _PacedFile:
    """An archive on a slow but healthy mount: bytes arrive at a steady
    pace (each small read well inside the bound, a whole member table past
    it). With ``gate``, the first read blocks with no bytes instead."""

    PER_KIB = 0.02  # seconds per KiB read

    def __init__(self, real, gate=None):
        self._real, self._gate = real, gate

    def _pace(self, count):
        if self._gate is not None:
            self._gate.wait(60)
        time.sleep(count / 1024 * self.PER_KIB)

    def read(self, size=-1):
        data = self._real.read(size)
        self._pace(len(data))
        return data

    def readinto(self, buffer):
        count = self._real.readinto(buffer)
        self._pace(count or 0)
        return count

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _paced_archives(monkeypatch, gate=None):
    """``io.open`` hands out paced files for ``Pack.zip``; returns the real
    file's reads that went through the wrapper's bounded chunks."""
    from utils import path_utils

    real_open = io.open
    monkeypatch.setattr(path_utils, "READ_CHUNK", 4096, raising=False)

    def paced_open(file, *args, **kwargs):
        handle = real_open(file, *args, **kwargs)
        if isinstance(file, (str, os.PathLike)) and os.fspath(file).endswith("Pack.zip"):
            return _PacedFile(handle, gate)
        return handle

    monkeypatch.setattr(io, "open", paced_open)


MEMBERS = 800       # about 46 KiB of member table: ~0.9 s at the pace above


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["list", "search"])
async def test_an_archive_whose_member_table_streams_slowly_is_not_cut_off(
    tmp_path, files_env, monkeypatch, route,
):
    from services import archive_members

    archive_members.clear_cache()
    archive = _big_archive(tmp_path / "Packs", count=MEMBERS)
    _paced_archives(monkeypatch)
    if route == "list":
        call = files_env.list_files(
            path=str(archive.parent), show_archives=True, summarize_archives=True,
        )
    else:
        call = files_env.search_files(
            path=str(archive.parent), recursive=True, include_archives=True,
        )

    assert await _answer_of(call) == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["list", "search"])
async def test_an_archive_read_that_blocks_with_no_bytes_ends_at_the_bound(
    tmp_path, files_env, monkeypatch, route,
):
    from services import archive_members

    archive_members.clear_cache()
    archive = _big_archive(tmp_path / "Packs", count=2)
    gate = threading.Event()
    _paced_archives(monkeypatch, gate)
    if route == "list":
        call = files_env.list_files(
            path=str(archive.parent), show_archives=True, summarize_archives=True,
        )
    else:
        call = files_env.search_files(
            path=str(archive.parent), recursive=True, include_archives=True,
        )
    try:
        assert await _answer_of(call) == 503
    finally:
        gate.set()


def _rar3_block(kind, flags, body=b""):
    """One RAR 2.9 block: CRC16 (low half of CRC32), type, flags, size, body."""
    head = struct.pack("<BHH", kind, flags, 7 + len(body)) + body
    return struct.pack("<H", zlib.crc32(head) & 0xFFFF) + head


def _rar3_volume(name, data, *, first, more):
    """One volume of a stored (method 0x30) multi-volume RAR 2.9 archive."""
    main_flags = 0x0001 | 0x0010 | (0x0100 if first else 0)   # VOLUME|NEWNUMBERING|FIRST
    encoded = name.encode()
    file_body = struct.pack(
        "<LLBLLBBHL", len(data), len(data), 3, zlib.crc32(data), 0, 29, 0x30,
        len(encoded), 0o100644 << 16,
    ) + encoded
    return (
        b"Rar!\x1a\x07\x00"
        + _rar3_block(0x73, main_flags, b"\x00" * 6)
        + _rar3_block(0x74, 0x8000, file_body) + data
        + _rar3_block(0x7B, 0x0001 if more else 0)                 # NEXT_VOLUME
    )


def _two_volume_rar(folder):
    """``Pack.part1.rar`` holds ``Disc 1.iso``, ``Pack.part2.rar`` ``Disc 2.iso``."""
    folder.mkdir(exist_ok=True)
    first = folder / "Pack.part1.rar"
    first.write_bytes(_rar3_volume("Disc 1.iso", b"one", first=True, more=True))
    (folder / "Pack.part2.rar").write_bytes(
        _rar3_volume("Disc 2.iso", b"two", first=False, more=False),
    )
    return first


@pytest.fixture(name="rar_members")
def _rar_members():
    from services import archive_members

    if not archive_members.HAS_RAR:
        pytest.skip("rarfile is not installed")
    archive_members.clear_cache()
    yield archive_members
    archive_members.clear_cache()


@pytest.mark.parametrize("with_progress", [False, True])
def test_a_multi_volume_rar_lists_every_volume(tmp_path, rar_members, with_progress):
    first = _two_volume_rar(tmp_path / "Packs")
    ticks: list[None] = []
    progress = (lambda *_a: ticks.append(None)) if with_progress else None

    members = rar_members.read_archive_members(str(first), progress)

    assert sorted(m.name for m in members) == ["Disc 1.iso", "Disc 2.iso"]
    assert bool(ticks) is with_progress


def test_a_rar_whose_next_volume_is_missing_is_never_cached(tmp_path, rar_members):
    first = _two_volume_rar(tmp_path / "Packs")
    second = first.with_name("Pack.part2.rar")
    kept = second.read_bytes()
    second.unlink()

    with pytest.raises(Exception, match="volume"):
        rar_members.read_archive_members(str(first), lambda *_a: None)

    second.write_bytes(kept)  # the first volume is unchanged: same cache key
    members = rar_members.read_archive_members(str(first))
    assert sorted(m.name for m in members) == ["Disc 1.iso", "Disc 2.iso"]


@pytest.mark.asyncio
async def test_a_multi_volume_rar_is_listed_whole_by_the_browser(
    tmp_path, files_env, rar_members,
):
    first = _two_volume_rar(tmp_path / "Packs")
    listing = await asyncio.wait_for(
        files_env.list_files(path=str(first.parent), show_archives=True,
                             summarize_archives=True),
        BOUND,
    )
    by_name = {entry.name: entry for entry in listing.entries}
    assert by_name["Pack.part1.rar"].archive_items == 2

    found = await asyncio.wait_for(
        files_env.search_files(path=str(first.parent), recursive=True,
                               include_archives=True),
        BOUND,
    )
    members = sorted(
        row["name"] for row in found["archives"] if row["archive_path"] == str(first)
    )
    assert members == ["Disc 1.iso", "Disc 2.iso"]


# --- the shared helper ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_stall_bound_resets_on_progress_and_names_where_it_stopped():
    from services.subprocess_runner import Stalled, run_detached_within_stall_bound

    stop = threading.Event()

    def walk(*, progress):
        for index in range(8):
            progress(f"folder {index}")
            time.sleep(STALL / 4)
        progress("the last folder")
        stop.wait(60)

    try:
        with pytest.raises(Stalled) as caught:
            await asyncio.wait_for(
                run_detached_within_stall_bound(
                    walk, what="Listing", stall=STALL, report_progress=True,
                ),
                BOUND,
            )
    finally:
        stop.set()
    assert caught.value.where == "the last folder"
    assert "the last folder" in str(caught.value)
