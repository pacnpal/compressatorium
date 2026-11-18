"""Inbox (#433): the RomM Platform scan after a pass, and the pass summary.

After a pass that placed Images in a RomM Library, RomM is asked to scan only
the Platform folders it added to (``POST /api/tasks/scan`` with
``platform_fs_slugs``, which needs ``tasks.run``; confirmed against RomM
5.4.1, see ``docs/ROMM_INTEGRATION_ROADMAP.md``). A token without that scope
is reported, never retried. Each pass that placed an Image or left one with a
Finding sends one summary through the notify path; a pass that did nothing
sends nothing.

The tick is called directly, with a fake clock, real files and the fake RomM.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from services import db as _db
from services import platform_detect

from config import settings

from . import test_inbox_431 as t431
from .fake_romm import FakeRomm

# The 431 job-queue fixture: records what intake submits; the test sets each
# job's status.
queue_fixture = t431._queue

PS2 = {"id": 3, "slug": "ps2", "fs_slug": "ps2", "name": "PlayStation 2"}
PSX = {"id": 4, "slug": "psx", "fs_slug": "psx", "name": "PlayStation"}
DISC = b"\0" * 4096


@pytest.fixture(name="vol")
def _vol(tmp_path, monkeypatch):
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "config"))
    platform_detect.clear_cache()
    return volume


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path, monkeypatch):
    import asyncio

    from services import inbox, libraries
    from services.inbox import intake
    from services.job_manager import job_manager
    from services.romm import settings as romm_settings

    monkeypatch.setattr(job_manager, "max_concurrent", 1)
    monkeypatch.setattr(job_manager, "_semaphore", asyncio.Semaphore(1))
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    inbox.reset_for_tests()
    intake.reset_for_tests()
    yield
    intake.reset_for_tests()
    inbox.reset_for_tests()
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest.fixture(name="sent")
def _sent(monkeypatch) -> list:
    from services import notifications

    events: list = []
    monkeypatch.setattr(notifications, "notify", events.append)
    return events


@pytest_asyncio.fixture(name="romm")
async def _romm(vol, db):
    """The fake RomM, its library at ``romm/roms/<platform>``."""
    from services.romm import settings as romm_settings

    (vol / "romm" / "roms" / "ps2").mkdir(parents=True)
    (vol / "romm" / "roms" / "psx").mkdir(parents=True)
    fake = FakeRomm(platforms=[PS2, PSX], library=vol / "romm").start()
    await romm_settings.save({
        "url": fake.url, "token": "rmm_x", "library_root": str(vol / "romm"),
    })
    yield fake
    fake.stop()


def _drop(path: Path, data: bytes = DISC) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _romm_inbox(vol: Path):
    from services import inbox

    (vol / "Downloads").mkdir(exist_ok=True)
    return inbox.register(str(vol / "Downloads"), "romm", 0)


def _folder_inbox(vol: Path):
    from services import inbox, libraries

    (vol / "ROMs").mkdir(exist_ok=True)
    library = libraries.register(str(vol / "ROMs"))
    (vol / "Downloads").mkdir(exist_ok=True)
    return inbox.register(str(vol / "Downloads"), library.id, 0)


async def _pass(now: float) -> dict:
    """One pass as the scheduler's tick makes it: poll every Inbox, wait for
    the copies it started, then what runs after a pass."""
    from services.inbox import intake, report

    results = await intake.poll_all(now=now)
    await intake.drain()
    return await report.after_pass(results)


async def _until_placed(count: int = 1) -> dict:
    """Passes until *count* Images were reported placed; the summaries."""
    placed = 0
    summaries = []
    for now in range(6):
        summary = await _pass(now)
        summaries.append(summary)
        placed += sum(len(lib["placed"]) for lib in summary["libraries"].values())
        if placed >= count:
            return summaries
    raise AssertionError(f"only {placed} placed: {summaries}")


# ---- the RomM call --------------------------------------------------------


def test_the_client_asks_romm_to_quick_scan_named_platform_folders(vol, romm):
    from services.romm.client import RommClient

    client = RommClient(base_url=romm.url, token="rmm_x")

    task_id = client.scan_platforms(["ps2", "gb"])

    assert task_id
    assert romm.scans == [{"type": "quick", "platform_fs_slugs": ["ps2", "gb"]}]
    assert romm.platform_scans == [["ps2", "gb"]]


def test_the_fake_refuses_a_scan_without_tasks_run_as_romm_does(vol, romm):
    from services.romm.client import RommClient, RommError

    romm.scopes = {"roms.read", "roms.write"}
    client = RommClient(base_url=romm.url, token="rmm_x")

    with pytest.raises(RommError) as refused:
        client.scan_platforms(["ps2"])

    assert refused.value.status == 403
    assert romm.platform_scans == []


def test_the_fake_refuses_a_misspelt_key_as_romm_does(vol, romm):
    """RomM's ScanPayload forbids extra keys (422), so a typo never falls
    back to a scan of the whole library."""
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{romm.url}/api/tasks/scan", method="POST",
        data=json.dumps({"type": "quick", "platform_slugs": ["ps2"]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=5)

    assert refused.value.code == 422
    assert romm.scans == []


# ---- after a pass -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pass_that_placed_images_in_romm_scans_only_those_platforms(vol, romm):
    _romm_inbox(vol)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    summaries = await _until_placed()

    assert (vol / "romm" / "roms" / "ps2" / "Game.iso").exists()
    assert romm.platform_scans == [["ps2"]]
    assert [s["libraries"]["romm"]["scan"]["status"] for s in summaries
            if "romm" in s["libraries"] and s["libraries"]["romm"]["placed"]] == ["asked"]


@pytest.mark.asyncio
async def test_one_pass_that_placed_in_two_platforms_asks_once_for_both(vol, romm):
    """Two Inboxes of RomM placed in ps2 and psx in the same pass: one scan,
    of both folders, and of nothing else."""
    from services.inbox import report

    ps2 = _drop(vol / "romm" / "roms" / "ps2" / "Game.iso")
    psx = _drop(vol / "romm" / "roms" / "psx" / "Other.iso")
    results = [
        {"inbox_id": 1, "library_id": "romm", "left": [],
         "placed": [{"path": "/in/a/Game.iso", "platform": "ps2", "output": str(ps2)}]},
        {"inbox_id": 2, "library_id": "romm", "left": [],
         "placed": [{"path": "/in/b/Other.iso", "platform": "psx", "output": str(psx)}]},
    ]

    summary = await report.after_pass(results)

    assert romm.platform_scans == [["ps2", "psx"]]
    assert summary["libraries"]["romm"]["scan"]["folders"] == ["ps2", "psx"]


@pytest.mark.asyncio
async def test_a_pass_with_nothing_placed_asks_romm_nothing(vol, romm, sent):
    _romm_inbox(vol)

    for now in range(3):
        await _pass(now)

    assert romm.scans == []
    assert sent == []


@pytest.mark.asyncio
async def test_a_token_without_tasks_run_is_reported_not_retried(vol, romm, sent):
    romm.scopes = {"roms.read", "roms.write"}
    _romm_inbox(vol)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    summaries = await _until_placed()
    for now in range(10, 13):
        await _pass(now)  # later passes that place nothing ask nothing

    scans = [s for s in romm.requests if s[1].startswith("/api/tasks/scan")]
    assert len(scans) == 1
    [outcome] = [s["libraries"]["romm"]["scan"] for s in summaries
                 if s["libraries"].get("romm", {}).get("scan")]
    assert outcome["status"] == "missing_scope"
    assert "tasks.run" in outcome["reason"]
    [event] = [e for e in sent if e.kind == "inbox_pass"]
    assert "tasks.run" in event.body


@pytest.mark.asyncio
async def test_an_output_whose_original_cant_be_archived_is_still_scanned(vol, romm):
    """Codex 4608095510: the output counts as placed the moment it lands in
    the Library; a move to processed/ that fails keeps its Finding (and
    hold) but RomM is still asked to scan the output's Platform."""
    import os

    box = _romm_inbox(vol)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    processed = vol / "Downloads" / "processed"
    processed.mkdir()
    os.chmod(processed, 0o500)  # nothing can be moved in
    try:
        await _until_placed()
    finally:
        os.chmod(processed, 0o700)

    assert (vol / "romm" / "roms" / "ps2" / "Game.iso").exists()
    assert game.exists()
    assert romm.platform_scans == [["ps2"]]
    [finding] = _inbox_findings()
    assert "processed/" in finding["reason"]
    from services.inbox import intake

    assert intake.status(box.id)["held"] == 1


@pytest.mark.asyncio
async def test_a_folder_library_asks_no_one_to_scan(vol, romm):
    _folder_inbox(vol)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _until_placed()

    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()
    assert romm.scans == []


# ---- the pass summary ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_poll_reports_the_images_it_left_with_a_finding(vol, db):
    from services import findings, inbox
    from services.inbox import intake

    box = _folder_inbox(vol)
    _drop(vol / "Downloads" / "Mystery.iso")

    await intake.poll(inbox.get(box.id), now=0)
    result = await intake.poll(inbox.get(box.id), now=1)
    again = await intake.poll(inbox.get(box.id), now=2)

    [left] = result["left"]
    assert Path(left["path"]).name == "Mystery.iso"
    assert left["kind"] == findings.INBOX_UNSORTED
    assert "Platform" in left["reason"]
    assert again["left"] == []  # held, not reported twice


@pytest.mark.asyncio
async def test_each_pass_that_did_something_sends_one_summary(vol, db, sent):
    box = _folder_inbox(vol)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")
    _drop(vol / "Downloads" / "Mystery.iso")

    for now in range(6):
        await _pass(now)

    events = [e for e in sent if e.kind == "inbox_pass"]
    assert sum(e.data["placed"] for e in events) == 1
    assert sum(e.data["left"] for e in events) == 1
    assert all(e.data["placed"] or e.data["left"] for e in events)
    text = "\n".join(e.body for e in events)
    assert "Game.iso" in text and "Mystery.iso" in text
    assert box.folder  # the Inbox the files came from


@pytest.mark.asyncio
async def test_a_pass_that_did_nothing_sends_nothing(vol, db, sent):
    _folder_inbox(vol)

    for now in range(3):
        summary = await _pass(now)
        assert summary["libraries"] == {}

    assert sent == []


@pytest.mark.asyncio
async def test_a_stop_admits_nothing_more_and_lets_started_work_finish(vol, db):
    """Codex 4607809613: once the stop predicate is true (run-rules got
    SIGTERM), the poll admits no more files; what it started finishes."""
    from services import inbox
    from services.inbox import intake

    box = _folder_inbox(vol)
    names = ["A.iso", "B.iso", "C.iso"]
    for name in names:
        _drop(vol / "Downloads" / "ps2" / name)
    await intake.poll_all(now=0)  # seen: settled from the next poll on

    def stopping() -> bool:
        return intake.status(box.id)["in_progress"] >= 1

    await intake.poll_all(now=1, should_stop=stopping)
    await intake.drain()
    result = await intake.poll(inbox.get(box.id), now=2, should_stop=lambda: True)

    placed = sorted(p.name for p in (vol / "ROMs" / "ps2").iterdir())
    assert len(placed) == 1
    assert [Path(p["output"]).name for p in result["placed"]] == placed
    assert sorted(p.name for p in (vol / "Downloads" / "processed" / "ps2").iterdir()) == placed
    left = sorted(p.name for p in (vol / "Downloads" / "ps2").iterdir())
    assert left == sorted(set(names) - set(placed))


@pytest.mark.asyncio
async def test_a_poll_error_is_in_the_pass_summary_once(vol, db, sent):
    """An Inbox that can't be read: every pass's summary carries the error,
    and it is notified once per change, so the server's tick doesn't repeat
    it every minute."""
    import shutil

    box = _folder_inbox(vol)
    downloads = vol / "Downloads"

    shutil.rmtree(downloads)
    first = await _pass(0)
    again = await _pass(1)
    downloads.mkdir()
    fine = await _pass(2)
    shutil.rmtree(downloads)
    back = await _pass(3)

    [error] = first["libraries"][box.library_id]["errors"]
    assert "isn't there" in error
    assert again["libraries"][box.library_id]["errors"] == [error]
    assert fine["libraries"] == {}
    assert back["libraries"][box.library_id]["errors"] == [error]
    events = [e for e in sent if e.kind == "inbox_pass"]
    assert len(events) == 2  # the first time, and again once it came back
    assert "isn't there" in events[0].body


@pytest.mark.asyncio
async def test_a_poll_skipped_or_removed_mid_pass_is_not_an_error(vol, db, sent):
    from services.inbox import report

    summary = await report.after_pass([
        {"inbox_id": 1, "library_id": "folder:1", "placed": [], "left": [],
         "error": code} for code in ("poll_running", "inbox_removed")
    ])

    assert summary["libraries"] == {}
    assert sent == []


# ---- a cancel because the app is stopping isn't the file's fault ------------


async def _ps2_rule_and_game(vol):
    from services import libraries
    from services.rules import sweep

    box = _folder_inbox(vol)
    await sweep.set_rules(
        libraries.get(box.library_id), {"ps2": {"mode": "cso_compress", "enabled": True}},
    )
    return box, _drop(vol / "Downloads" / "ps2" / "Game.iso")


def _inbox_findings() -> list[dict]:
    from services import findings

    return [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS]


@pytest.mark.asyncio
async def test_a_conversion_cancelled_by_a_shutdown_is_retried_not_held(vol, db, queue):
    from models import JobStatus
    from services import inbox
    from services.inbox import intake
    from services.job_manager import CANCEL_SHUTDOWN

    box, game = await _ps2_rule_and_game(vol)
    await intake.poll(inbox.get(box.id), now=0)
    await intake.poll(inbox.get(box.id), now=1)
    queue.jobs["job0"].status = JobStatus.CANCELLED
    queue.jobs["job0"].cancel_reason = CANCEL_SHUTDOWN

    settled = await intake.poll(inbox.get(box.id), now=2)
    seen_again = await intake.poll(inbox.get(box.id), now=3)

    assert settled["left"] == [] and _inbox_findings() == []
    assert game.exists()
    assert intake.status(box.id)["held"] == 0
    # Seen as new, so taken once it has settled again: queued a second time.
    assert seen_again["queued"] == 1
    assert len(queue.submitted) == 2


@pytest.mark.asyncio
async def test_a_conversion_the_user_cancelled_is_still_held(vol, db, queue):
    from models import JobStatus
    from services import inbox
    from services.inbox import intake
    from services.job_manager import CANCEL_REQUESTED

    box, game = await _ps2_rule_and_game(vol)
    await intake.poll(inbox.get(box.id), now=0)
    await intake.poll(inbox.get(box.id), now=1)
    queue.jobs["job0"].status = JobStatus.CANCELLED
    queue.jobs["job0"].cancel_reason = CANCEL_REQUESTED

    for now in (2, 3, 4):
        await intake.poll(inbox.get(box.id), now=now)

    [finding] = _inbox_findings()
    assert "cancelled" in finding["reason"]
    assert len(queue.submitted) == 1  # held, not queued again
    assert game.exists()


@pytest.mark.asyncio
async def test_a_copy_cancelled_by_a_shutdown_is_retried_not_held(vol, db):
    import asyncio

    from models import ConversionMode
    from services import inbox
    from services.inbox import intake
    from services.job_manager import CANCEL_SHUTDOWN

    box = _folder_inbox(vol)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    async with intake._checking():
        await intake.poll(inbox.get(box.id), now=0)
        await intake.poll(inbox.get(box.id), now=1)
        for _ in range(100):  # staged, now waiting for the lock
            if t431._slots(vol / "ROMs" / "ps2"):
                break
            await asyncio.sleep(0.02)
        [job] = [
            j for j in intake.job_manager.jobs.values()
            if j.mode == ConversionMode.INBOX_COPY and j.status == "processing"
        ]
        await intake.job_manager.cancel_all_jobs(reason=CANCEL_SHUTDOWN)
    await intake.drain()
    await intake.poll(inbox.get(box.id), now=2)
    await intake.poll(inbox.get(box.id), now=3)
    await intake.drain()

    assert _inbox_findings() == []
    assert not game.exists()
    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()


def test_a_cancel_records_why(monkeypatch):
    """The shared marker: a cancel the user asks for is ``requested`` (the
    default); the shutdown path says ``shutdown``. The first reason stays."""
    import asyncio

    from models import ConversionJob, ConversionMode, JobStatus
    from services.job_manager import CANCEL_REQUESTED, CANCEL_SHUTDOWN, job_manager

    from collections import OrderedDict
    from datetime import datetime, timezone

    monkeypatch.setattr(job_manager, "jobs", OrderedDict())

    def job(job_id: str) -> ConversionJob:
        made = ConversionJob(
            id=job_id, file_path=f"/x/{job_id}.iso", filename=f"{job_id}.iso",
            mode=ConversionMode.INBOX_COPY, status=JobStatus.PROCESSING,
            created_at=datetime.now(timezone.utc),
        )
        job_manager.jobs[job_id] = made
        return made

    one, two = job("cancel-one"), job("cancel-two")

    async def cancel():
        await job_manager.cancel_job(one.id)
        await job_manager.cancel_all_jobs(reason=CANCEL_SHUTDOWN)

    asyncio.run(cancel())

    assert one.cancel_reason == CANCEL_REQUESTED
    assert two.cancel_reason == CANCEL_SHUTDOWN


def test_the_inbox_summary_is_a_registered_event():
    from services import notifications

    assert "inbox_pass" in {e["key"] for e in notifications.event_list()}


@pytest.mark.asyncio
async def test_a_scan_romm_refuses_is_reported_and_not_asked_again(vol, romm, sent):
    """A library scan already running (409): said in the summary, not retried."""
    romm.scan_reply = (409, "A library scan is already running")
    _romm_inbox(vol)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    summaries = await _until_placed()
    for now in range(10, 13):
        await _pass(now)

    assert len([p for _m, p in romm.requests if p.startswith("/api/tasks/scan")]) == 1
    [outcome] = [s["libraries"]["romm"]["scan"] for s in summaries
                 if s["libraries"].get("romm", {}).get("scan")]
    assert outcome["status"] == "not_asked"
    assert "already running" in outcome["reason"]


@pytest.mark.asyncio
async def test_the_scheduler_tick_runs_the_after_pass_on_what_it_polled(vol, db, monkeypatch):
    import asyncio

    from services import libraries
    from services.inbox import intake, report
    from services.rules import sweep

    polled = [{"inbox_id": 1, "library_id": "folder:1", "placed": [], "left": []}]
    seen = []

    async def poll_all(**_kwargs):
        return polled

    async def after_pass(results):
        seen.append(results)
        raise asyncio.CancelledError  # stop the loop after one tick

    real_sleep = asyncio.sleep
    monkeypatch.setattr(intake, "poll_all", poll_all)
    monkeypatch.setattr(report, "after_pass", after_pass)
    monkeypatch.setattr(sweep.asyncio, "sleep", lambda _s: real_sleep(0))

    with pytest.raises(asyncio.CancelledError):
        await libraries.run_forever()

    assert seen == [polled]
