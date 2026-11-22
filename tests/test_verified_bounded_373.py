"""Routes that check every stored record are bounded (#373).

``GET /api/verified`` pruned the verification store by statting every
record's output in the shared threadpool, then checked each one's Volume
containment the same way: a frozen mount held the request with no answer.
The same held for ``/api/verified/lookup``, the CHD badge lookup
(``POST /api/chd-metadata``). Each now runs on a
detached thread under the no-progress bound, reporting each record, and
answers 503 like the other bounded routes when a Volume stops answering.
A record whose file can't be checked is kept: only a confirmed absence
prunes it.
"""
from __future__ import annotations

import asyncio
import errno
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

BOUND = 5.0     # far past the shrunk stall bound; an unbounded route fails here
STALL = 0.3


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    from routes import volume_guard
    from services import db as _db

    from config import settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    volume = tmp_path / "vol"
    (volume / "Frozen").mkdir(parents=True)
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    monkeypatch.setattr(settings, "data_mount_root", str(volume))
    monkeypatch.setattr(volume_guard, "WALK_STALL_SECONDS", STALL, raising=False)
    yield volume
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


class _Mount:
    """A mount that freezes on ``freeze()`` and answers again on ``set()``."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.frozen = False

    def freeze(self) -> None:
        self.frozen = True

    def set(self) -> None:
        self.release.set()


@pytest.fixture(name="frozen")
def _frozen(monkeypatch):
    """Once ``freeze()`` is called, ``os.stat``/``os.lstat`` of anything under
    a ``Frozen`` folder block until ``set()`` or teardown, as a frozen mount
    does. Records are written before it freezes."""
    mount = _Mount()
    release = mount.release
    real_stat, real_lstat = os.stat, os.lstat

    def is_frozen(path) -> bool:
        return (
            mount.frozen
            and isinstance(path, (str, os.PathLike))
            and f"{os.sep}Frozen{os.sep}" in os.fspath(path)
            and not release.is_set()
        )

    def stat(path, *args, **kwargs):
        if is_frozen(path):
            release.wait(60)
        return real_stat(path, *args, **kwargs)

    def lstat(path, *args, **kwargs):
        if is_frozen(path):
            release.wait(60)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "lstat", lstat)
    yield mount
    release.set()


def _file(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return str(path)


async def _status(call) -> int:
    try:
        await asyncio.wait_for(call, BOUND)
    except HTTPException as exc:
        return exc.status_code
    return 200


@pytest.mark.asyncio
async def test_a_frozen_record_answers_503_within_the_bound_and_is_kept(vol, frozen):
    from routes import info
    from services.verification_store import verification_store

    healthy = _file(vol / "ps2" / "Game.chd")
    stuck = _file(vol / "Frozen" / "Stuck.chd")
    await verification_store.mark_verified(healthy)
    await verification_store.mark_verified(stuck)
    frozen.freeze()

    started = time.monotonic()
    assert await _status(info.list_verified()) == 503
    assert time.monotonic() - started < BOUND

    frozen.set()  # the mount answers again
    assert await verification_store.get_record(stuck) is not None
    listed = await asyncio.wait_for(info.list_verified(), BOUND)
    assert sorted(listed["verified"]) == sorted([healthy, stuck])


@pytest.mark.asyncio
async def test_other_requests_stay_fast_while_one_is_stuck(vol, frozen):
    from fastapi.concurrency import run_in_threadpool
    from routes import info
    from services.verification_store import verification_store

    healthy = _file(vol / "ps2" / "Game.chd")
    await verification_store.mark_verified(healthy)
    await verification_store.mark_verified(_file(vol / "Frozen" / "Stuck.chd"))
    frozen.freeze()

    stuck = asyncio.ensure_future(_status(info.list_verified()))
    await asyncio.sleep(STALL / 3)
    started = time.monotonic()
    assert await asyncio.wait_for(run_in_threadpool(lambda: 1), 1.0) == 1
    looked_up = await asyncio.wait_for(
        info.lookup_verified(info.VerifiedLookup(paths=[healthy])), 1.0,
    )
    assert looked_up["verified"] == [healthy]
    assert time.monotonic() - started < 1.0
    assert await stuck == 503


@pytest.mark.asyncio
async def test_a_lookup_of_a_frozen_record_answers_503(vol, frozen):
    from routes import info
    from services.verification_store import verification_store

    stuck = _file(vol / "Frozen" / "Stuck.chd")
    await verification_store.mark_verified(stuck)
    frozen.freeze()

    assert await _status(info.lookup_verified(info.VerifiedLookup(paths=[stuck]))) == 503


@pytest.mark.asyncio
async def test_a_badge_lookup_of_a_frozen_file_answers_503(vol, frozen):
    from models import MetadataBatchRequest
    from routes import info

    stuck = _file(vol / "Frozen" / "Stuck.chd")
    request = MetadataBatchRequest(paths=[_file(vol / "ps2" / "Game.chd"), stuck])
    frozen.freeze()

    assert await _status(info.get_chd_metadata_batch(request)) == 503


@pytest.mark.asyncio
async def test_a_record_whose_stat_fails_is_kept_not_pruned(vol, monkeypatch):
    """EIO says nothing about whether the file is there: only a confirmed
    absence prunes a record."""
    from routes import info
    from services.verification_store import verification_store

    sick = str(vol / "ps2" / "Sick.chd")      # not created: stat must not decide
    gone = str(vol / "ps2" / "Gone.chd")
    await verification_store.mark_verified(sick)
    await verification_store.mark_verified(gone)
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path).startswith(sick):
            raise OSError(errno.EIO, "Input/output error", path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)

    await asyncio.wait_for(info.list_verified(), BOUND)

    assert await verification_store.get_record(sick) is not None
    assert await verification_store.get_record(gone) is None


@pytest.mark.asyncio
async def test_a_large_healthy_store_still_answers(vol):
    from routes import info
    from services.verification_store import verification_store

    paths = [_file(vol / "lib" / f"Game {index:04d}.chd") for index in range(3000)]
    for path in paths:
        verification_store._mark_sync(path, None, None, "intact")

    listed = await asyncio.wait_for(info.list_verified(), 60)

    assert len(listed["verified"]) == 3000
