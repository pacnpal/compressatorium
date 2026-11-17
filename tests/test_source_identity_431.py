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
    assert os.path.basename(os.path.dirname(read["path"])).startswith("bind_")
    assert not os.path.exists(read["path"])  # the binding is gone afterwards
    assert not _bindings(env)
    assert (inbox / "gc-moved" / "Game.iso").read_bytes() == original



def _bindings(env) -> list:
    """The bound-Source folders left in the app's work area."""
    work = env["tmp"] / "temp"
    return list(work.glob("bind_*")) if work.is_dir() else []


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
    assert not _bindings(env)


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
        if not _bindings(env):
            break
        await asyncio.sleep(0.05)
    assert not _bindings(env)



@pytest.mark.asyncio
async def test_a_cancel_during_the_identity_preflight_releases_everything(
    env, monkeypatch, tmp_path,
) -> None:
    """CodeRabbit 4606147979, ruled: the identity check and the bind run inside
    the job's full finalization region, so a cancel there lets go of the
    output locks and the cross-process slot, not just the Source's."""
    import asyncio

    from models import JobStatus
    from services import job_manager as job_manager_module
    from services.concurrency_manager import ConcurrencyManager
    from services.job_manager import job_manager
    from services.lock_manager import lock_manager

    shared = ConcurrencyManager(1, str(tmp_path / "locks"))
    monkeypatch.setattr(job_manager_module, "concurrency_manager", shared)
    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    job = await _queue(env, game, inbox)
    entered = asyncio.Event()

    async def hangs(_job):
        entered.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(job_manager, "_source_identity_problem_bounded", hangs)
    run = asyncio.ensure_future(job_manager._process_job(job.id))
    await asyncio.wait_for(entered.wait(), 5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert job_manager.jobs[job.id].status == JobStatus.CANCELLED
    assert not lock_manager.is_locked(job_manager.jobs[job.id].output_path)
    other = ConcurrencyManager(1, str(tmp_path / "locks"))
    assert await asyncio.wait_for(other.acquire("next"), 5)  # the slot is free
    other.release("next")


def test_unbinding_never_follows_a_swapped_folder(tmp_path, monkeypatch) -> None:
    """Outside-diff note on bind cleanup: the bound folder is cleaned through
    the descriptors kept from bind time, and removed from its parent only
    while it is still that folder; a folder swapped in under its name keeps
    everything in it."""
    from config import settings
    from utils import path_utils

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    inbox = tmp_path / "Downloads"
    game = Path(_gamecube(inbox / "gc" / "Game.iso"))
    identity = path_utils.source_identity(str(game), str(inbox))
    into = tmp_path / "work" / "bind_test"
    into.mkdir(parents=True)
    bound = path_utils.bind_source(str(game), identity, str(into))
    os.rename(bound.path, tmp_path / "moved-bind")
    impostor = Path(bound.path)
    impostor.mkdir()
    (impostor / "keep.txt").write_bytes(b"not the bind's")

    bound.close()

    assert (impostor / "keep.txt").read_bytes() == b"not the bind's"
    assert not list((tmp_path / "moved-bind").iterdir())
    assert game.exists()


# ---- round ten: Codex on f90d9826 ----------------------------------------


@pytest.mark.asyncio
async def test_the_bound_copy_lives_where_no_inbox_writer_can_reach(env, monkeypatch) -> None:
    """Codex 4606562350 (P1), ruled: the Source is bound under the job's own
    work folder in the app's work area, never in the Inbox. A writer that
    renames every entry of the Inbox (the old private-folder spot included)
    and puts a link to a folder of its own in its place, once the job is
    bound, can't make the converter read anything but the bound copy."""
    from config import settings
    from models import JobStatus
    from services.job_manager import job_manager
    from services.tools import registry

    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    original = Path(game).read_bytes()
    job = await _queue(env, game, inbox)
    outside = env["tmp"].parent / f"{env['tmp'].name}-secret"
    outside.mkdir()
    (outside / "Game.iso").write_bytes(b"ROOT ONLY" + bytes(0x440))
    real_sizing = job_manager._source_bytes_bounded

    async def writer_swaps_everything(job_, cancel_event):
        for entry in list(inbox.iterdir()):
            os.rename(entry, inbox / f"moved-{entry.name}")
            os.symlink(outside, entry)
        return await real_sizing(job_, cancel_event)

    monkeypatch.setattr(job_manager, "_source_bytes_bounded", writer_swaps_everything)
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
    work = Path(settings.temp_dir).resolve()
    assert Path(read["path"]).resolve().is_relative_to(work)
    assert not Path(read["path"]).resolve().is_relative_to(inbox.resolve())
    assert not os.path.exists(read["path"])  # the binding is gone afterwards


@pytest.mark.asyncio
async def test_a_source_written_to_during_its_conversion_publishes_nothing(
    env, monkeypatch,
) -> None:
    """Codex 4606539513, ruled: a hard-linked Source a resumed download
    appends to mid-conversion is caught before any output is published: the
    staged output is discarded, the job fails saying the Source changed, and
    the Source is left as it is."""
    from models import JobStatus
    from services.job_manager import job_manager
    from services.tools import registry

    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    job = await _queue(env, game, inbox)
    service = registry.get("dolphin")._service

    async def appended_meanwhile(cmd, *, input_path, output_path, **kwargs):
        with open(game, "ab") as resumed:
            resumed.write(b"more of the download")
        Path(output_path).write_bytes(b"converted output")
        yield {"progress": 100, "message": "Conversion complete"}

    monkeypatch.setattr(service._runner, "run", appended_meanwhile)

    await job_manager._process_job(job.id)

    ended = job_manager.jobs[job.id]
    assert ended.status == JobStatus.FAILED
    assert "the source changed while it was being converted" in (ended.error_message or "")
    assert not os.path.exists(ended.output_path)
    roms = env["tmp"] / "ROMs"
    assert [p.name for p in roms.rglob("*") if p.is_file()] == []
    assert Path(game).read_bytes().endswith(b"more of the download")


@pytest.mark.asyncio
async def test_a_cancel_during_a_slow_bind_copy_ends_the_job_promptly(
    env, monkeypatch, tmp_path,
) -> None:
    """Codex 4606539525, ruled: the bind's copy hears the job's cancel. A
    cancel while it is copying ends the job CANCELLED at once and lets go of
    its locks and its slot; the copy stops and removes what it made."""
    import asyncio
    import threading
    import time as clock

    from models import JobStatus
    from services import job_manager as job_manager_module
    from services.concurrency_manager import ConcurrencyManager
    from services.job_manager import job_manager
    from services.lock_manager import lock_manager
    from utils import path_utils

    shared = ConcurrencyManager(1, str(tmp_path / "locks"))
    monkeypatch.setattr(job_manager_module, "concurrency_manager", shared)
    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    job = await _queue(env, game, inbox)
    _no_links(monkeypatch)
    monkeypatch.setattr(path_utils, "_BIND_CHUNK", 128)
    copying = threading.Event()
    real_read = path_utils._read_chunk

    def slow(fd, size):
        copying.set()
        clock.sleep(0.2)  # about 2s for the whole Image
        return real_read(fd, size)

    monkeypatch.setattr(path_utils, "_read_chunk", slow)
    run = asyncio.ensure_future(job_manager._process_job(job.id))
    for _ in range(100):
        if copying.is_set():
            break
        await asyncio.sleep(0.02)
    assert copying.is_set()
    started = clock.monotonic()
    assert await job_manager.cancel_job(job.id)
    await asyncio.wait_for(run, 5)

    assert clock.monotonic() - started < 1.0
    ended = job_manager.jobs[job.id]
    assert ended.status == JobStatus.CANCELLED
    assert env["commands"] == []
    assert not lock_manager.is_locked(ended.output_path)
    other = ConcurrencyManager(1, str(tmp_path / "locks"))
    assert await asyncio.wait_for(other.acquire("next"), 5)
    other.release("next")


@pytest.mark.asyncio
async def test_deleting_a_finished_job_whose_binding_is_still_held_answers(
    env, monkeypatch,
) -> None:
    """Codex 4606539536, ruled: an archived snapshot leaves out the job's
    runtime-only state (the bound Source's descriptors and lock), so deleting
    a finished job while its locks are still being let go answers, never a
    500, and the live job keeps its own."""
    import asyncio

    from app.routes import convert as convert_routes
    from models import JobStatus
    from services.job_manager import job_manager

    inbox = env["tmp"] / "Downloads"
    game = _gamecube(inbox / "gc" / "Game.iso")
    job = await _queue(env, game, inbox)
    paused, go_on = asyncio.Event(), asyncio.Event()
    real_let_go = job_manager._let_go_in_background

    async def paused_let_go(*args, **kwargs):
        paused.set()
        await go_on.wait()
        return await real_let_go(*args, **kwargs)

    monkeypatch.setattr(job_manager, "_let_go_in_background", paused_let_go)
    run = asyncio.ensure_future(job_manager._process_job(job.id))
    await asyncio.wait_for(paused.wait(), 5)
    live = job_manager.jobs[job.id]
    assert live.status == JobStatus.COMPLETED
    assert live._bound is not None

    answer = await convert_routes.cancel_job(job.id)

    assert answer == {"status": "deleted"}
    assert live._bound is not None  # the live job's own, untouched
    archived = job_manager.get_job_for_lookup(job.id)
    assert archived is not None and archived._bound is None
    go_on.set()
    await asyncio.wait_for(run, 5)
