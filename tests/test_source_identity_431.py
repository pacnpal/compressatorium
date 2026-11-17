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
