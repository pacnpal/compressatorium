"""A numbered split part is guarded by its logical owner's lock (codex 4577940381).

A folder_to_iso job in another process (a CLI run) holds the cross-process
lock on the logical ``Game.iso`` only; the parts it writes (``Game.iso.0``,
``.1`` ... made as it goes) have no lock file of their own. The mutation guard
(``services.mutation_guard.locked_for_mutation``, used by the web's rename and
delete guard and the job's delete check) also asks the owner's lock, found by
name through the registry, so a part made after the lock was taken is covered.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from services import lock_manager as lock_module
from services.lock_manager import LockManager
from services.mutation_guard import locked_for_mutation


@pytest.fixture(name="building")
def _building(tmp_path, monkeypatch):
    """Another process (manager A) holds the logical Game.iso output lock."""
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(lock_module.lock_manager, "_lock_dir", str(tmp_path / "locks"))
    other = LockManager()
    other._lock_dir = str(tmp_path / "locks")
    logical = tmp_path / "Game.iso"
    assert other.acquire_lock(str(logical))
    (tmp_path / "Game.iso.0").write_bytes(b"part 0")
    (tmp_path / "save.0").write_bytes(b"a save")
    (tmp_path / "Other.bin").write_bytes(b"other")
    yield tmp_path, logical
    other.release_lock(str(logical))


def test_a_part_of_an_output_another_process_writes_is_guarded(building):
    vol, logical = building
    assert locked_for_mutation(str(vol / "Game.iso.0")) == str(logical)
    # Not there when the lock was taken: still covered, by name.
    assert locked_for_mutation(str(vol / "Game.iso.3")) == str(logical)


def test_unrelated_and_non_split_names_are_not(building):
    vol, _ = building
    assert locked_for_mutation(str(vol / "save.0")) is None
    assert locked_for_mutation(str(vol / "Other.bin")) is None
    assert locked_for_mutation(str(vol / "Game.iso.x")) is None


@pytest.mark.asyncio
async def test_the_web_guard_refuses_a_part_and_allows_the_rest(building):
    from routes.files import _assert_path_not_in_use

    vol, _ = building
    for part in ("Game.iso.0", "Game.iso.3"):
        with pytest.raises(HTTPException) as caught:
            await _assert_path_not_in_use(str(vol / part))
        assert caught.value.status_code == 409
    await _assert_path_not_in_use(str(vol / "save.0"))
    await _assert_path_not_in_use(str(vol / "Other.bin"))


@pytest.mark.asyncio
async def test_the_delete_check_refuses_a_part_another_process_writes(building):
    from services.job_manager import job_manager
    from utils.delete_plan import build_delete_snapshot

    vol, _ = building
    part = str(vol / "Game.iso.0")
    snapshot = build_delete_snapshot(str(vol / "Other.bin"))
    fingerprints = dict(snapshot["fingerprints"])
    fingerprints[part] = build_delete_snapshot(part)["fingerprints"][part]
    with pytest.raises(RuntimeError, match="locked by an active conversion"):
        await job_manager._check_delete_paths("job-x", [part], fingerprints, str(vol / "o"))
