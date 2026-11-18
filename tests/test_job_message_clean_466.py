"""A job's status message is cleaned of tool control characters (#466).

#462 cleaned a failed job's ``error_message``, but the ``message`` (the status
line) still held the tool's raw progress: 971 characters of 7z's backspaces on
a patch job whose 7z was killed. Every status message set from a tool's
progress now goes through the same cleaning (``clean_tool_output``) in
JobManager, for external jobs and conversions alike, and a failed job's status
line says why it stopped instead of keeping its last progress text.
"""

from __future__ import annotations

import pytest
from models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus

RAW_7Z = (
    "0%\b\b\b\b    \b\b\b\b  1% + Big Patch Me (Patched).gba\b\b\b\b\b"
    "\x1b[1m  10%\x1b[0m + Big Patch Me (Patched).gba\r"
)
CONTROL = "\b\x1b\r\x00\x07"


def _clean(text: str | None) -> bool:
    return text is not None and not any(ch in text for ch in CONTROL)


@pytest.mark.asyncio
async def test_an_external_jobs_progress_message_is_cleaned() -> None:
    from services.job_manager import job_manager

    job = job_manager.create_external_job("Patch x", ConversionMode.PATCH)
    events = job_manager.subscribe(job.id)
    try:
        await job_manager.update_external_job(job.id, progress=55, message=RAW_7Z)
        event = events.get_nowait()
    finally:
        job_manager.unsubscribe(job.id, events)
        await job_manager.finish_external_job(job.id, success=True)

    assert _clean(job.message), repr(job.message)
    assert "10% + Big Patch Me (Patched).gba" in job.message
    assert "  " not in job.message
    assert event["message"] == job.message


@pytest.mark.asyncio
async def test_a_failed_external_job_does_not_keep_its_progress_as_its_message() -> None:
    from services.job_manager import job_manager

    job = job_manager.create_external_job("Patch x", ConversionMode.PATCH)
    await job_manager.update_external_job(job.id, progress=55, message=RAW_7Z)
    await job_manager.finish_external_job(
        job.id, success=False,
        error_message="The patch stopped on an error: 7z was killed (signal 9)",
    )

    done = job_manager.get_job_for_lookup(job.id)
    assert done.message == "The patch stopped on an error: 7z was killed (signal 9)"
    assert "Big Patch Me" not in done.message


@pytest.mark.asyncio
async def test_a_cancelled_external_jobs_message_is_cleaned() -> None:
    from services.job_manager import job_manager

    job = job_manager.create_external_job("Patch x", ConversionMode.PATCH)
    await job_manager.finish_external_job_cancelled(job.id, message=RAW_7Z)

    assert _clean(job_manager.get_job_for_lookup(job.id).message)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fresh_detect_cache", "tmp_volume")
async def test_a_conversions_progress_message_is_cleaned_and_a_failure_replaces_it(
    e2e_env, monkeypatch,
) -> None:
    """The conversion path: a tool's progress update carries control
    characters, then the tool fails. The status message seen while it ran
    was clean, and the failed job's message is its error, not the progress."""
    from routes import convert as convert_routes

    seen: list[str] = []

    async def raw_then_fail(input_path, output_path, mode, **_kwargs):
        yield {"progress": 40, "message": RAW_7Z, "activity": True}
        seen.append(convert_routes.job_manager.get_job(job.id).message)
        raise RuntimeError("7z was killed (signal 9), most likely out of memory")

    for tool in convert_routes.registry.all():
        monkeypatch.setattr(tool, "convert", raw_then_fail)
    source = e2e_env["tmp_path"] / "rom.iso"
    source.write_bytes(b"PAYLOAD")
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
    ))
    await convert_routes.job_manager._process_job(job.id)

    final = convert_routes.job_manager.get_job_for_lookup(job.id)
    assert final.status == JobStatus.FAILED
    assert seen and _clean(seen[0]), seen
    assert "10% + Big Patch Me (Patched).gba" in seen[0]
    assert final.message == final.error_message
    assert "Big Patch Me" not in final.message
