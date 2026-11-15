"""Create .m3u holds shared input locks on the discs it lists (#306 LockManager).

A delete-on-verify conversion of a selected disc takes exclusive delete holds
on its Source paths and waits for shared holders. Holding the discs while the
playlist is validated and published means such a conversion either finished
first (the selection then fails: the disc is gone) or waits, then sees the
new playlist in its Referrer scan. No special case for .m3u.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models import PlaylistCreateRequest
from app.routes import files as files_routes
from app.services import disc_playlist
from app.services.lock_manager import LockManager


def _manager(lock_dir: Path) -> LockManager:
    manager = LockManager()
    manager._lock_dir = str(lock_dir)
    return manager


@pytest.fixture(name="env")
def _env(tmp_path: Path, monkeypatch):
    vol = tmp_path / "vol"
    vol.mkdir()
    locks = tmp_path / "locks"
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(vol))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(vol))
    server = _manager(locks)  # this process
    other = _manager(locks)   # another process (a conversion, the CLI)
    monkeypatch.setattr(disc_playlist, "lock_manager", server)
    paths = []
    for n in ("Game (Disc 1).cue", "Game (Disc 2).cue"):
        (vol / n).write_text("x")
        paths.append(str(vol / n))
    return vol, paths, server, other


def test_a_disc_held_for_deletion_refuses_with_busy_and_writes_nothing(env):
    vol, paths, _server, other = env
    assert other.acquire_lock(paths[1], allow_existing=True)  # a conversion's delete hold
    with pytest.raises(disc_playlist.PlaylistBusy, match="conversion is replacing"):
        disc_playlist.create_playlist(paths, "Game.m3u")
    assert not (vol / "Game.m3u").exists()


def test_the_busy_refusal_releases_the_locks_it_took(env):
    _vol, paths, _server, other = env
    assert other.acquire_lock(paths[1], allow_existing=True)
    with pytest.raises(disc_playlist.PlaylistBusy):
        disc_playlist.create_playlist(paths, "Game.m3u")
    other.release_lock(paths[1])
    # Disc 1 was locked before disc 2 refused; it must be free again.
    assert other.acquire_lock(paths[0], allow_existing=True)


def test_creation_holds_the_discs_while_it_publishes(env, monkeypatch):
    vol, paths, _server, other = env
    seen = []
    real = disc_playlist.write_new_bytes

    def publish(target, data, **kw):
        seen.append([other.acquire_lock(p, allow_existing=True) for p in paths])
        return real(target, data, **kw)

    monkeypatch.setattr(disc_playlist, "write_new_bytes", publish)
    disc_playlist.create_playlist(paths, "Game.m3u")
    assert seen == [[False, False]]  # a delete hold is refused mid-creation
    assert (vol / "Game.m3u").exists()
    assert all(other.acquire_lock(p, allow_existing=True) for p in paths)  # released after


def test_locks_are_released_when_validation_refuses(env):
    _vol, paths, _server, other = env
    with pytest.raises(disc_playlist.PlaylistError):
        disc_playlist.create_playlist(paths[:1], "Game.m3u")  # one disc: refused
    assert other.acquire_lock(paths[0], allow_existing=True)


@pytest.mark.asyncio
async def test_the_route_answers_busy_with_409(env, monkeypatch):
    import services.disc_playlist as routed  # the module the route imports

    _vol, paths, server, other = env
    monkeypatch.setattr(routed, "lock_manager", server)
    assert other.acquire_lock(paths[0], allow_existing=True)
    with pytest.raises(HTTPException) as exc:
        await files_routes.playlist_create(
            PlaylistCreateRequest(paths=paths, file_name="Game.m3u"),
        )
    assert exc.value.status_code == 409
    assert "conversion is replacing" in exc.value.detail
