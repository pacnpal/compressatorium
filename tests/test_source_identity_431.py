"""A queued conversion that carries its Source's identity (#431, Codex 4604325922).

The job pipeline's shared check: a conversion may be queued with the identity
its Source had when it was chosen (device, inode, size, mtime) and the folder it
must stay under. Right before the tool runs, under the job's own Source lock,
the worker re-reads the Source beneath that folder without following a link,
and refuses the job, with the reason, when it isn't that file any more or is
outside the Volumes. An Inbox queues its conversions this way, so swapping its
folder for a link while a job waits converts nothing from outside the Inbox.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from .test_default_compression_345 import _env
from .test_platform_detect import _gamecube

pytestmark = pytest.mark.usefixtures("fresh_detect_cache")
# #345's "env" fixture: a Volume at tmp_path, an empty queue, nothing saved,
# and each dolphin-tool command captured at its runner.
ENV_FIXTURE = _env


async def _queue(env, game: str, root: Path):
    from models import ConversionMode
    from services.job_manager import job_manager
    from utils import path_utils

    identity = path_utils.source_identity(game, str(root))
    out = env["tmp"] / "ROMs"
    out.mkdir(exist_ok=True)
    [job] = await job_manager.create_batch_jobs(
        [game], ConversionMode.DOLPHIN_RVZ, output_dir=str(out),
        source_identities={game: identity},
    )
    return job


@pytest.mark.asyncio
async def test_a_source_still_the_one_queued_is_converted(env) -> None:
    from models import JobStatus
    from services.job_manager import job_manager

    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    job = await _queue(env, game, inbox)

    await job_manager._process_job(job.id)

    assert job_manager.jobs[job.id].status == JobStatus.COMPLETED, job.error_message
    assert len(env["commands"]) == 1


@pytest.mark.asyncio
async def test_a_folder_swapped_for_a_link_after_queueing_is_refused(env) -> None:
    from models import JobStatus
    from services.job_manager import job_manager

    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    job = await _queue(env, game, inbox)
    protected = _gamecube(env["tmp"] / "Protected" / "Game.iso")
    shutil.rmtree(inbox / "gc")
    os.symlink(env["tmp"] / "Protected", inbox / "gc")

    await job_manager._process_job(job.id)

    ended = job_manager.jobs[job.id]
    assert ended.status == JobStatus.FAILED
    assert "Source" in (ended.error_message or "")
    assert env["commands"] == []  # the tool never ran
    assert Path(protected).exists()
    assert not list((env["tmp"] / "ROMs").glob("*.rvz"))


@pytest.mark.asyncio
async def test_a_source_replaced_after_queueing_is_refused(env) -> None:
    from models import JobStatus
    from services.job_manager import job_manager

    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    job = await _queue(env, game, inbox)
    os.unlink(game)
    _gamecube(Path(game))  # the same name and bytes, another file

    await job_manager._process_job(job.id)

    assert job_manager.jobs[job.id].status == JobStatus.FAILED
    assert env["commands"] == []



@pytest.mark.asyncio
async def test_a_folder_swapped_after_the_check_still_converts_the_bound_source(
    env, monkeypatch,
) -> None:
    """Codex 4605281757 (P1), ruled: the Source is bound before anything else
    runs. Swapping its folder for a link right after the check (here, as the
    job sizes its Source) can't redirect the converter: it reads the staged
    link of the file that was checked, and nothing outside is read."""
    from models import JobStatus
    from services.job_manager import job_manager
    from services.tools import registry

    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    original = Path(game).read_bytes()
    job = await _queue(env, game, inbox)
    outside = env["tmp"].parent / f"{env['tmp'].name}-outside"
    outside.mkdir()
    (outside / "Game.iso").write_bytes(b"NOT THE INBOX'S" + bytes(0x440))
    real_sizing = job_manager._source_bytes_bounded

    async def swap_then_size(job_, cancel_event):
        os.rename(inbox / "gc", inbox / "gc-moved")
        os.symlink(outside, inbox / "gc")
        return await real_sizing(job_, cancel_event)

    monkeypatch.setattr(job_manager, "_source_bytes_bounded", swap_then_size)
    read = {}
    service = registry.get("dolphin")._service

    async def capture(cmd, *, input_path, output_path, **kwargs):
        read["path"] = input_path
        read["bytes"] = Path(input_path).read_bytes()
        Path(output_path).write_bytes(b"converted output")
        yield {"progress": 100, "message": "Conversion complete"}

    monkeypatch.setattr(service._runner, "run", capture)

    await job_manager._process_job(job.id)

    assert job_manager.jobs[job.id].status == JobStatus.COMPLETED, job.error_message
    assert read["bytes"] == original
    assert ".compressatorium-bind-" in read["path"]
    assert not os.path.exists(read["path"])  # the binding is gone afterwards
    assert not list(inbox.glob(".compressatorium-bind-*"))
    assert (inbox / "gc-moved" / "Game.iso").read_bytes() == original



def _no_links(monkeypatch):
    """A filesystem without hard links: the bind falls back to a copy."""
    import errno

    from utils import path_utils

    def refuse(*_a, **_k):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(path_utils.os, "link", refuse)


@pytest.mark.asyncio
async def test_a_slow_bind_copy_that_keeps_moving_finishes(env, monkeypatch) -> None:
    """Codex 4606061281, ruled: the bind's copy fallback is bounded by its
    progress, not a fixed deadline: a copy slower overall than the bound but
    never stalled finishes and converts."""
    import time as clock

    from models import JobStatus
    from services import job_manager as job_manager_module
    from services.job_manager import job_manager
    from utils import path_utils

    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    job = await _queue(env, game, inbox)
    _no_links(monkeypatch)
    monkeypatch.setattr(job_manager_module, "_BIND_STALL_SECONDS", 0.3)
    monkeypatch.setattr(path_utils, "_BIND_CHUNK", 128)
    real_read = path_utils._read_chunk

    def slow(fd, size):
        clock.sleep(0.05)  # 0x440 bytes in 128-byte chunks: ~0.5s in all
        return real_read(fd, size)

    monkeypatch.setattr(path_utils, "_read_chunk", slow)

    await job_manager._process_job(job.id)

    assert job_manager.jobs[job.id].status == JobStatus.COMPLETED, job.error_message
    assert len(env["commands"]) == 1
    assert not list(inbox.glob(".compressatorium-bind-*"))


@pytest.mark.asyncio
async def test_a_bind_copy_that_stalls_fails_and_leaves_nothing(env, monkeypatch) -> None:
    import asyncio
    import time as clock

    from models import JobStatus
    from services import job_manager as job_manager_module
    from services.job_manager import job_manager
    from utils import path_utils

    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    job = await _queue(env, game, inbox)
    _no_links(monkeypatch)
    monkeypatch.setattr(job_manager_module, "_BIND_STALL_SECONDS", 0.3)
    real_read = path_utils._read_chunk

    def stalls(fd, size):
        clock.sleep(1.0)
        return real_read(fd, size)

    monkeypatch.setattr(path_utils, "_read_chunk", stalls)

    await job_manager._process_job(job.id)

    ended = job_manager.jobs[job.id]
    assert ended.status == JobStatus.FAILED
    assert "bound" in (ended.error_message or "")
    assert env["commands"] == []
    for _ in range(60):  # the abandoned copy stops at its next chunk
        if not list(inbox.glob(".compressatorium-bind-*")):
            break
        await asyncio.sleep(0.05)
    assert not list(inbox.glob(".compressatorium-bind-*"))
