"""A disc-ID read that could not run leaves the CHD unchecked (#490).

``/api/info`` and the library scan's Phase 2 cache the disc-ID outcome,
"nothing found" included, by marking the CHD checked, and a checked CHD is
not read again until its mtime changes. That is right only when the extractor
actually inspected the file. chdman missing or not executable, a chdman crash
or a timeout says nothing about the file, so ``extract_from_chd`` raises
``DiscIdReadFailed`` for those instead of answering None, and both callers
leave the CHD unchecked so the next request or scan tries again.

chdman is faked at the ``SubprocessRunner.run_capture`` seam.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from ._chd_fakes import serve_extractraw
from .test_disc_id import _make_chd_v5, _make_iso

NOT_FOUND = (1, b"", b"Error reading metadata: Metadata not found\n")


@pytest.fixture(name="disc_id")
def _disc_id():
    # The module the app imports (``services.disc_id``), not ``app.services...``.
    from services import disc_id

    return disc_id


def _fake_chdman(monkeypatch, answer):
    """chdman's runner answers every command with ``answer(cmd)``."""
    from services.chdman import chdman_service

    calls: list[list[str]] = []

    async def fake(cmd, **_kwargs):
        calls.append(list(cmd))
        return answer(cmd)

    monkeypatch.setattr(chdman_service.runner, "run_capture", fake)
    return calls


def _missing_chdman(_cmd):
    raise FileNotFoundError(2, "No such file or directory", "chdman")


def _blank_chd(tmp_path, name="game.chd"):
    """Not a CHD v5: only the GDRO ``dumpmeta`` read runs chdman."""
    chd = tmp_path / name
    chd.write_bytes(b"x" * 64)
    return chd


def _dvd_chd(tmp_path, name="game.chd"):
    """A DVD CHD with no disc ID in it: the sector walk runs chdman first."""
    chd = tmp_path / name
    chd.write_bytes(_make_chd_v5(_make_iso({"README.TXT": b"hello"})))
    return chd


# ---------------------------------------------------------------------------
# extract_from_chd: "could not run" raises, "nothing found" is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("make_chd", [_blank_chd, _dvd_chd], ids=["dumpmeta", "extractraw"])
async def test_chdman_that_cannot_be_started_raises(tmp_path, monkeypatch, disc_id, make_chd):
    _fake_chdman(monkeypatch, _missing_chdman)

    with pytest.raises(disc_id.DiscIdReadFailed):
        await disc_id.extract_from_chd(str(make_chd(tmp_path)), "chdman")


@pytest.mark.asyncio
@pytest.mark.parametrize("make_chd", [_blank_chd, _dvd_chd], ids=["dumpmeta", "extractraw"])
@pytest.mark.parametrize("returncode", [126, 127, -11], ids=["not-executable", "not-found", "crash"])
async def test_chdman_that_did_not_run_raises(
    tmp_path, monkeypatch, disc_id, make_chd, returncode,
):
    # 126/127: the nice/ionice wrapper could not exec chdman. Negative: chdman
    # was killed by a signal nobody here sent (a timeout or cancel is None).
    _fake_chdman(monkeypatch, lambda _cmd: (returncode, b"", b"chdman: failed"))

    with pytest.raises(disc_id.DiscIdReadFailed):
        await disc_id.extract_from_chd(str(make_chd(tmp_path)), "chdman")


@pytest.mark.asyncio
async def test_a_dumpmeta_read_that_timed_out_raises(tmp_path, monkeypatch, disc_id):
    # run_capture reports a timeout as a None returncode, with nothing abandoned.
    _fake_chdman(monkeypatch, lambda _cmd: (None, b"", b""))

    with pytest.raises(disc_id.DiscIdReadFailed):
        await disc_id.extract_from_chd(str(_blank_chd(tmp_path)), "chdman")


@pytest.mark.asyncio
@pytest.mark.parametrize("make_chd", [_blank_chd, _dvd_chd], ids=["no-chd-v5", "dvd"])
async def test_a_clean_nothing_found_is_none(tmp_path, monkeypatch, disc_id, make_chd):
    _fake_chdman(monkeypatch, lambda cmd: serve_extractraw(cmd) or NOT_FOUND)

    assert await disc_id.extract_from_chd(str(make_chd(tmp_path)), "chdman") is None


@pytest.mark.asyncio
async def test_a_chd_header_that_cannot_be_read_raises(tmp_path, monkeypatch, disc_id):
    def unreadable(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(disc_id, "read_chd_header", unreadable)
    _fake_chdman(monkeypatch, lambda _cmd: NOT_FOUND)

    with pytest.raises(disc_id.DiscIdReadFailed):
        await disc_id.extract_from_chd(str(_blank_chd(tmp_path)), "chdman")


# ---------------------------------------------------------------------------
# /api/info
# ---------------------------------------------------------------------------


@pytest.fixture(name="info_env")
def _info_env(tmp_path, monkeypatch):
    """``/api/info`` for a cached, never disc-ID-checked CHD; records the cache writes."""
    from routes import info as info_routes

    store = info_routes.chd_metadata_store
    marked: list[str] = []
    updated: list[tuple] = []
    monkeypatch.setattr(store, "get_disc_id_info", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(store, "is_disc_id_checked", AsyncMock(return_value=False))
    monkeypatch.setattr(store, "mark_disc_id_checked", AsyncMock(side_effect=marked.append))
    monkeypatch.setattr(
        store, "update_disc_id_info",
        AsyncMock(side_effect=lambda *a, **_k: updated.append(a)),
    )
    monkeypatch.setattr(store, "is_stale", AsyncMock(return_value=False))
    monkeypatch.setattr(
        store, "get_full_info",
        AsyncMock(return_value=({"raw_data": "", "sha1": "abc123", "hunk_size": "4096"}, "dvd")),
    )
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    # chdman counts as ready whatever this host has installed (#488 gate):
    # these tests fake its runs at the run_capture seam.
    monkeypatch.setattr(info_routes, "_require_info_ready", AsyncMock())
    return info_routes, marked, updated


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    FileNotFoundError(2, "No such file or directory", "chdman"),
    RuntimeError("chdman crashed"),
], ids=["chdman-missing", "crash"])
async def test_info_leaves_a_chd_unchecked_when_the_extractor_raises(
    tmp_path, monkeypatch, info_env, caplog, error,
):
    info_routes, marked, updated = info_env
    monkeypatch.setattr(info_routes, "disc_id_extract_from_chd", AsyncMock(side_effect=error))
    chd = _blank_chd(tmp_path)

    with caplog.at_level(logging.WARNING):
        info = await info_routes.get_chd_info(path=str(chd))

    # The rest of the CHD info is still answered, without a disc ID.
    assert info.sha1 == "abc123" and info.hunk_size == "4096"
    assert info.media_type == "dvd"
    assert info.game_id is None and info.title is None
    assert not marked, "a CHD the extractor never inspected must stay eligible for a retry"
    assert not updated, "and nothing may be cached about its disc ID"
    assert any(
        "game.chd" in r.getMessage()
        for r in caplog.records if r.levelno >= logging.WARNING
    ), "the failure is logged at warning, not swallowed"


@pytest.mark.asyncio
async def test_info_leaves_a_chd_unchecked_when_chdman_cannot_run(
    tmp_path, monkeypatch, info_env,
):
    # End to end through the real extractor: the GDRO read cannot start chdman.
    info_routes, marked, updated = info_env
    _fake_chdman(monkeypatch, _missing_chdman)

    info = await info_routes.get_chd_info(path=str(_blank_chd(tmp_path)))

    assert info.sha1 == "abc123" and info.game_id is None
    assert not marked and not updated


@pytest.mark.asyncio
async def test_info_marks_a_clean_nothing_found_checked(tmp_path, monkeypatch, info_env):
    info_routes, marked, updated = info_env
    _fake_chdman(monkeypatch, lambda cmd: serve_extractraw(cmd) or NOT_FOUND)
    chd = _dvd_chd(tmp_path)

    info = await info_routes.get_chd_info(path=str(chd))

    assert info.game_id is None
    assert marked == [str(chd)], "an inspected CHD with no disc ID is cached as checked"
    assert updated == [(str(chd), None, None)]


# ---------------------------------------------------------------------------
# The library scan's Phase 2
# ---------------------------------------------------------------------------


def _scan_env(tmp_path, monkeypatch):
    """A scan of ``tmp_path`` with Phase 1 answered; records the disc-ID marks."""
    from routes import info as info_routes

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    async def info(_path):
        return {"raw_data": "Metadata:     Tag='DVD '  Index=0  Length=0 bytes"}

    async def set_metadata(_path, _info, persist=False):
        return {"media_type": "dvd"}

    store = info_routes.chd_metadata_store
    marked: list[str] = []
    monkeypatch.setattr(info_routes.chdman_service, "info", info)
    monkeypatch.setattr(store, "set_metadata", set_metadata)
    monkeypatch.setattr(store, "flush_async", AsyncMock())
    monkeypatch.setattr(store, "is_stale", AsyncMock(return_value=False))
    monkeypatch.setattr(store, "is_disc_id_checked", AsyncMock(return_value=False))
    monkeypatch.setattr(store, "mark_disc_id_checked", AsyncMock(side_effect=marked.append))
    monkeypatch.setattr(store, "update_disc_id_info", AsyncMock())

    created: list[str] = []
    original = info_routes.job_manager.create_external_job

    def capture(*args, **kwargs):
        job = original(*args, **kwargs)
        created.append(job.id)
        return job

    monkeypatch.setattr(info_routes.job_manager, "create_external_job", capture)
    return info_routes, created, marked


@pytest.mark.asyncio
async def test_scan_leaves_chds_unchecked_when_chdman_cannot_run(tmp_path, monkeypatch):
    chds = sorted(str(_blank_chd(tmp_path, name)) for name in ("a.chd", "b.chd"))
    info_routes, created, marked = _scan_env(tmp_path, monkeypatch)
    calls = _fake_chdman(monkeypatch, _missing_chdman)

    await info_routes.scan_metadata_task(force=False)

    tried = sorted({cmd[cmd.index("-i") + 1] for cmd in calls if "dumpmeta" in cmd})
    assert tried == chds, "a read that could not run does not stop the scan"
    assert not marked, "neither CHD was inspected, so neither may be marked checked"
    assert info_routes.job_manager.jobs[created[0]].status.value == "completed"


@pytest.mark.asyncio
async def test_scan_marks_a_clean_nothing_found_checked(tmp_path, monkeypatch):
    chds = sorted(str(_dvd_chd(tmp_path, name)) for name in ("a.chd", "b.chd"))
    info_routes, _created, marked = _scan_env(tmp_path, monkeypatch)
    _fake_chdman(monkeypatch, lambda cmd: serve_extractraw(cmd) or NOT_FOUND)

    await info_routes.scan_metadata_task(force=False)

    assert sorted(marked) == chds
