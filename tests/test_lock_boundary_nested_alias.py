"""Lock walks stop at the outermost Volume root, not the deepest (Codex 4578823857).

With Volumes ``/mnt`` and ``/games -> /mnt/MyGame/PS3_GAME``, the deepest root
holding ``PS3_GAME/PARAM.SFO`` is the ``/games`` alias. That is the right
boundary for Platform inference (folder names above an inner Volume say
nothing), and the wrong one for a lock walk: a CLI packing ``/mnt/MyGame``
holds its folder lock *above* the inner root, so a walk that stopped there let
``DELETE /api/files/delete`` remove a file under the held lock. The two
purposes have their own functions: ``platform_boundary_root`` (deepest) and
``lock_boundary_root`` (outermost, every overlapping root considered).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from services.lock_manager import LockManager
from utils import path_utils


@pytest.fixture(name="nested")
def _nested(tmp_path, monkeypatch):
    from routes import files as files_routes
    from services.lock_manager import lock_manager
    from starlette.datastructures import Headers

    mnt = tmp_path / "mnt"
    game = mnt / "MyGame"
    (game / "PS3_GAME").mkdir(parents=True)
    sfo = game / "PS3_GAME" / "PARAM.SFO"
    sfo.write_bytes(b"sfo")
    games = tmp_path / "games"
    games.symlink_to(game / "PS3_GAME", target_is_directory=True)
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(files_routes.settings, "chd_volumes", f"{mnt},{games}")
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(mnt))
    monkeypatch.setattr(lock_manager, "_lock_dir", str(lock_dir))
    monkeypatch.setattr(lock_manager, "_ready", False)
    cli = LockManager()                      # another process on the same lock dir
    cli._lock_dir = str(lock_dir)

    def request(action):
        return SimpleNamespace(headers=Headers({"x-chd-action-confirm": action}))

    return SimpleNamespace(
        routes=files_routes, mnt=mnt, game=game, games=games, sfo=sfo, cli=cli,
        request=request,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["through-alias", "through-mnt"])
async def test_a_delete_under_a_folder_locked_above_the_inner_root_is_refused(
    nested, spelling,
):
    from fastapi import HTTPException

    n = nested
    path = n.games / "PARAM.SFO" if spelling == "through-alias" else n.sfo
    assert n.cli.acquire_dir_lock(str(n.game))
    try:
        with pytest.raises(HTTPException) as exc:
            await n.routes.delete_file(n.request("delete-file"), path=str(path), recursive=False)
        assert exc.value.status_code == 409
        assert n.sfo.exists()
    finally:
        n.cli.release_dir_lock(str(n.game))


def test_the_two_boundaries_differ_on_purpose(nested):
    n = nested
    # Platform inference stops at the inner alias root...
    assert path_utils.platform_boundary_root(str(n.sfo)) == (n.game / "PS3_GAME").resolve()
    # ...a lock walk goes up to the outermost root, past the held folder.
    for spelling in (n.sfo, n.games / "PARAM.SFO"):
        assert path_utils.lock_boundary_root(str(spelling)) == n.mnt.resolve()


def test_a_lock_walk_from_the_alias_finds_the_held_folder(nested):
    n = nested
    assert n.cli.acquire_dir_lock(str(n.game))
    try:
        from services.lock_manager import lock_manager

        assert lock_manager.folder_locked_above(str(n.games / "PARAM.SFO")) == str(
            n.game.resolve(),
        )
    finally:
        n.cli.release_dir_lock(str(n.game))
