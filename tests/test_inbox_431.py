"""Inbox (#431): intake of loose files into a Library.

A folder registered as an Inbox feeds one Library. Each poll, a file whose
size and mtime have stayed the same for the settle time gets its Platform from
``detect_platform``, goes into the Library's placement folder (converted under
that Platform's enabled Rule, or copied as it is when there is none), and the
original moves to ``processed/`` inside the Inbox. Nothing is ever deleted.
Unsorted, conflicting and failed files stay with a Finding.

The tick is called directly, with a fake clock and real files.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from services import db as _db
from services import findings, platform_detect

from config import settings

DISC = b"\0" * 4096


@pytest.fixture(name="vol")
def _vol(tmp_path, monkeypatch):
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    platform_detect.clear_cache()
    return volume


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path, monkeypatch):
    import asyncio

    from services import inbox, libraries
    from services.inbox import intake
    from services.job_manager import job_manager
    from services.romm import settings as romm_settings

    # The conversion slots an Inbox copy shares, fresh for each test.
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


def _library(vol: Path):
    from services import libraries

    (vol / "ROMs").mkdir(exist_ok=True)
    return libraries.register(str(vol / "ROMs"))


def _inbox(vol: Path, library, settle: int = 60):
    from services import inbox

    (vol / "Downloads").mkdir(exist_ok=True)
    return inbox.register(str(vol / "Downloads"), library.id, settle)


def _drop(path: Path, data: bytes = DISC) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _poll(box, now: float) -> dict:
    from services import inbox
    from services.inbox import intake

    result = await intake.poll(inbox.get(box.id), now=now)
    await intake.drain()
    return result


def _two_global_slots(monkeypatch, where: Path) -> None:
    """Two cross-process slots too (an Inbox copy takes one, #431), in a lock
    folder of the test's own."""
    from services import job_manager as job_manager_module
    from services.concurrency_manager import ConcurrencyManager

    monkeypatch.setattr(
        job_manager_module, "concurrency_manager",
        ConcurrencyManager(2, str(where.parent / "locks-2")),
    )


def _inbox_findings() -> list[dict]:
    return [f for f in findings.list_findings() if f["kind"] in findings.INBOX_KINDS]


# ---- saving an Inbox -------------------------------------------------------


def test_an_inbox_is_stored_with_its_folder_library_and_settle_time(vol, db):
    from services import inbox

    library = _library(vol)
    (vol / "Downloads").mkdir()

    saved = inbox.register(str(vol / "Downloads"), library.id)
    inbox.reset_for_tests()  # read back from the database
    stored = inbox.get(saved.id)

    assert stored.folder == str((vol / "Downloads").resolve())
    assert stored.library_id == library.id
    assert stored.settle_seconds == 60
    assert inbox.describe(stored)["processed"] == 0


@pytest.mark.parametrize("where, status", [
    ("outside", 403),
    ("inside_library", 409),
    ("holding_library", 409),
    ("missing_library", 404),
])
def test_saving_refuses_a_folder_that_could_loop_or_escape(vol, db, tmp_path, where, status):
    from services import inbox

    library = _library(vol)
    folder = {
        "outside": tmp_path / "elsewhere",
        "inside_library": vol / "ROMs" / "incoming",
        "holding_library": vol,
        "missing_library": vol / "Downloads",
    }[where]
    folder.mkdir(exist_ok=True)
    library_id = "folder:999" if where == "missing_library" else library.id

    with pytest.raises(inbox.InboxError) as refused:
        inbox.register(str(folder), library_id)

    assert refused.value.status == status
    assert inbox.all_inboxes() == []


def test_saving_refuses_a_folder_that_already_has_an_inbox(vol, db):
    from services import inbox

    library = _library(vol)
    _inbox(vol, library)
    (vol / "Downloads" / "nested").mkdir()

    for folder in (vol / "Downloads", vol / "Downloads" / "nested"):
        with pytest.raises(inbox.InboxError) as refused:
            inbox.register(str(folder), library.id)
        assert refused.value.status == 409
    assert len(inbox.all_inboxes()) == 1


# ---- one poll tick ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_growing_file_is_left_alone(vol, db):
    library = _library(vol)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    with game.open("ab") as more:
        more.write(b"still downloading")
    await _poll(box, now=61)

    assert game.exists()
    assert not (vol / "ROMs" / "ps2").exists()
    assert not (vol / "Downloads" / "processed").exists()


@pytest.mark.asyncio
async def test_a_file_is_not_taken_before_the_settle_time(vol, db):
    library = _library(vol)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    result = await _poll(box, now=59)

    assert result["waiting"] == 1
    assert game.exists()


@pytest.mark.asyncio
async def test_a_settled_file_is_copied_in_and_the_original_moves_to_processed(vol, db):
    """No Rule for the Platform: the Image goes in as it is, into the folder
    the Library's layout names, and the original moves to processed/."""
    from services import inbox

    library = _library(vol)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso", b"disc bytes" * 100)

    await _poll(box, now=0)
    await _poll(box, now=61)
    result = await _poll(box, now=122)

    placed = vol / "ROMs" / "ps2" / "Game.iso"
    assert placed.read_bytes() == b"disc bytes" * 100
    assert not game.exists()
    assert (vol / "Downloads" / "processed" / "ps2" / "Game.iso").read_bytes() == (
        b"disc bytes" * 100
    )
    assert inbox.describe(inbox.get(box.id))["processed"] == 1
    assert result["waiting"] == 0
    # No staging left behind, and nothing else in the Library.
    assert sorted(p.name for p in (vol / "ROMs" / "ps2").iterdir()) == ["Game.iso"]
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_a_poll_reports_what_it_placed(vol, db):
    from services import inbox
    from services.inbox import intake

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await intake.poll(inbox.get(box.id), now=0)
    await intake.poll(inbox.get(box.id), now=1)
    await intake.drain()
    result = await intake.poll(inbox.get(box.id), now=2)

    assert [(p["platform"], Path(p["output"]).name) for p in result["placed"]] == [
        ("ps2", "Game.iso"),
    ]


@pytest.mark.asyncio
async def test_an_unsorted_file_stays_with_a_finding(vol, db):
    library = _library(vol)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=61)

    assert game.exists()
    assert not (vol / "Downloads" / "processed").exists()
    [finding] = _inbox_findings()
    assert finding["kind"] == findings.INBOX_UNSORTED
    assert finding["library_id"] == library.id
    assert finding["path"] == str(game.resolve())
    assert "Platform" in finding["reason"]


@pytest.mark.asyncio
async def test_processed_is_never_read(vol, db):
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    old = _drop(vol / "Downloads" / "processed" / "ps2" / "Old.iso")

    for now in (0, 61, 122):
        await _poll(box, now=now)

    assert old.exists()
    assert not (vol / "ROMs" / "ps2").exists()
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_a_second_tick_changes_nothing(vol, db):
    library = _library(vol)
    box = _inbox(vol, library)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")
    unsorted = _drop(vol / "Downloads" / "Mystery.iso")

    await _poll(box, now=0)
    await _poll(box, now=61)
    before = sorted(str(p) for p in vol.rglob("*"))
    held = _inbox_findings()

    # However many ticks, well past the settle time: the held file is not
    # tried again and the placed one is gone from the Inbox.
    for now in (122, 183, 244):
        result = await _poll(box, now=now)
        assert result["queued"] == result["copying"] == result["held"] == 0
        assert result["waiting"] == 0

    assert sorted(str(p) for p in vol.rglob("*")) == before
    assert _inbox_findings() == held
    assert unsorted.exists()


@pytest.mark.asyncio
async def test_a_name_already_in_the_library_is_never_replaced(vol, db):
    library = _library(vol)
    box = _inbox(vol, library)
    existing = _drop(vol / "ROMs" / "ps2" / "Game.iso", b"the Library's own")
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso", b"a new download")

    await _poll(box, now=0)
    await _poll(box, now=61)

    assert existing.read_bytes() == b"the Library's own"
    assert game.read_bytes() == b"a new download"
    [finding] = _inbox_findings()
    assert finding["kind"] == findings.INBOX_FAILED
    assert "already in the Library" in finding["reason"]
    assert sorted(p.name for p in (vol / "ROMs" / "ps2").iterdir()) == ["Game.iso"]


@pytest.mark.asyncio
async def test_a_held_file_is_taken_again_once_it_changes(vol, db):
    library = _library(vol)
    box = _inbox(vol, library)
    _drop(vol / "ROMs" / "ps2" / "Game.iso", b"the Library's own")
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso", b"a new download")
    await _poll(box, now=0)
    await _poll(box, now=61)

    game.rename(vol / "Downloads" / "ps2" / "Game (Rev 1).iso")
    await _poll(box, now=122)
    await _poll(box, now=183)

    assert (vol / "ROMs" / "ps2" / "Game (Rev 1).iso").read_bytes() == b"a new download"
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_a_name_taken_in_processed_gets_a_numbered_folder(vol, db):
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "processed" / "ps2" / "Game.iso", b"an older one")
    _drop(vol / "Downloads" / "ps2" / "Game.iso", b"a new one")

    await _poll(box, now=0)
    await _poll(box, now=1)

    assert (vol / "Downloads" / "processed" / "ps2" / "Game.iso").read_bytes() == (
        b"an older one"
    )
    assert (vol / "Downloads" / "processed" / "ps2" / "Game (2)" / "Game.iso").read_bytes() == (
        b"a new one"
    )


# ---- under a Rule ----------------------------------------------------------


@pytest.fixture(name="queue")
def _queue(monkeypatch):
    """The job queue, recording what intake submits; each job's status is set
    by the test."""
    from services.inbox import intake
    from services.tools import registry

    submitted: list[dict] = []
    jobs: dict[str, SimpleNamespace] = {}

    async def create_batch_jobs(paths, mode, **kwargs):
        job = SimpleNamespace(
            id=f"job{len(jobs)}", status="queued", mode=mode, file_path=paths[0],
            output_path=kwargs["output_paths"][paths[0]], error_message=None, platform=None,
        )
        jobs[job.id] = job
        submitted.append({"paths": paths, "mode": mode, **kwargs})
        return [job]

    async def ready(_tool):
        return True

    monkeypatch.setattr(intake.job_manager, "create_batch_jobs", create_batch_jobs)
    monkeypatch.setattr(intake.job_manager, "get_job_for_lookup", jobs.get)
    monkeypatch.setattr(registry, "tool_is_ready", ready)
    return SimpleNamespace(submitted=submitted, jobs=jobs)


async def _ps2_rule(library):
    from services.rules import sweep

    await sweep.set_rules(library, {"ps2": {"mode": "cso_compress", "enabled": True}})


@pytest.mark.asyncio
async def test_an_enabled_rule_converts_through_the_job_queue_into_the_placement_folder(
    vol, db, queue,
):
    from models import ConversionMode, JobStatus
    from services import inbox

    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    result = await _poll(box, now=61)

    assert result["queued"] == 1
    [job] = queue.submitted
    assert job["paths"] == [str(game.resolve())]
    assert job["mode"] == ConversionMode.CSO_COMPRESS
    assert job["output_dir"] == str(vol / "ROMs" / "ps2")
    assert (vol / "ROMs" / "ps2").is_dir()
    # The output gets the Intact check, and nothing is ever deleted.
    assert job["verify_after"] is True
    assert not job.get("delete_on_verify")
    assert game.exists()  # not moved while it converts

    queue.jobs["job0"].status = JobStatus.COMPLETED
    await _poll(box, now=122)

    assert not game.exists()
    assert (vol / "Downloads" / "processed" / "ps2" / "Game.iso").exists()
    assert inbox.get(box.id).processed_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status, says", [
    ("failed", "conversion failed"),
    ("cancelled", "cancelled"),
])
async def test_a_failed_or_cancelled_conversion_stays_with_a_finding(
    vol, db, queue, status, says,
):
    from models import JobStatus

    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    await _poll(box, now=0)
    await _poll(box, now=61)

    queue.jobs["job0"].status = JobStatus(status)
    queue.jobs["job0"].error_message = "chdman said no"
    await _poll(box, now=122)
    await _poll(box, now=183)

    assert game.exists()
    assert len(queue.submitted) == 1  # held, not queued again
    [finding] = _inbox_findings()
    assert finding["kind"] == findings.INBOX_FAILED
    assert says in finding["reason"]


@pytest.mark.asyncio
async def test_a_disabled_rule_copies_the_image_in_as_it_is(vol, db, queue):
    from services.rules import sweep

    library = _library(vol)
    await sweep.set_rules(library, {"ps2": {"mode": "cso_compress", "enabled": False}})
    box = _inbox(vol, library)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=61)

    assert queue.submitted == []
    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()


# ---- the API ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_api_saves_lists_and_removes_an_inbox_with_its_status(vol, db):
    from routes import libraries as library_routes

    library = _library(vol)
    (vol / "Downloads").mkdir()
    saved = await library_routes.create_inbox(
        library.id,
        library_routes.InboxRequest(folder=str(vol / "Downloads"), settle_seconds=30),
    )

    listed = await library_routes.list_inboxes(library.id)

    assert [i["id"] for i in listed["inboxes"]] == [saved["id"]]
    row = listed["inboxes"][0]
    assert row["settle_seconds"] == 30
    assert row["processed"] == 0
    assert row["last_poll"] is None
    assert row["waiting"] == 0

    await library_routes.delete_inbox(library.id, saved["id"])
    assert (await library_routes.list_inboxes(library.id))["inboxes"] == []
    assert (vol / "Downloads").is_dir()


@pytest.mark.asyncio
async def test_the_api_refuses_an_inbox_inside_its_library(vol, db):
    from fastapi import HTTPException
    from routes import libraries as library_routes

    library = _library(vol)
    (vol / "ROMs" / "incoming").mkdir()

    with pytest.raises(HTTPException) as refused:
        await library_routes.create_inbox(
            library.id, library_routes.InboxRequest(folder=str(vol / "ROMs" / "incoming")),
        )
    assert refused.value.status_code == 409


@pytest.mark.asyncio
async def test_removing_a_library_removes_its_inboxes(vol, db):
    from services import inbox, libraries

    library = _library(vol)
    _inbox(vol, library)

    await libraries.remove(library.id)

    assert inbox.all_inboxes() == []
    assert (vol / "Downloads").is_dir()


@pytest.mark.asyncio
async def test_the_scheduler_tick_polls_every_inbox(vol, db, monkeypatch):
    """Polls run from the existing sweep loop, not a loop of their own."""
    import asyncio

    from services import libraries
    from services.inbox import intake
    from services.rules import sweep

    polled = []

    async def poll_all(**_kwargs):
        polled.append(True)
        raise asyncio.CancelledError  # stop the loop after one tick

    real_sleep = asyncio.sleep
    monkeypatch.setattr(intake, "poll_all", poll_all)
    monkeypatch.setattr(sweep.asyncio, "sleep", lambda _s: real_sleep(0))

    with pytest.raises(asyncio.CancelledError):
        await libraries.run_forever()

    assert polled == [True]


@pytest.mark.asyncio
async def test_a_held_file_that_leaves_the_inbox_has_its_finding_cleared(vol, db):
    library = _library(vol)
    box = _inbox(vol, library)
    unsorted = _drop(vol / "Downloads" / "Mystery.iso")
    await _poll(box, now=0)
    await _poll(box, now=61)
    assert len(_inbox_findings()) == 1

    unsorted.unlink()
    await _poll(box, now=122)

    assert _inbox_findings() == []


# ---- the Libraries view ----------------------------------------------------


def test_the_libraries_view_lists_inboxes_with_their_status():
    root = Path(__file__).resolve().parents[1]
    view = (root / "src/lib/components/views/RulesView.svelte").read_text()
    panel = (root / "src/lib/components/views/LibraryInboxes.svelte").read_text()
    endpoints = (root / "src/lib/api/endpoints.js").read_text()
    findings_view = (root / "src/lib/components/views/FindingsView.svelte").read_text()
    assert "<LibraryInboxes" in view
    assert "api.listInboxes" in panel and "api.createInbox" in panel
    for status in ("last_poll", "waiting", "in_progress", "held", "processed"):
        assert f"row.{status}" in panel
    assert "/inboxes`" in endpoints
    for kind in (
        "inbox_unsorted", "inbox_conflict", "inbox_dat_conflict", "inbox_failed",
    ):
        assert f"{kind}:" in findings_view
    shots = (root / "shots.yml").read_text()
    readme = (root / "README.md").read_text()
    for theme in ("light", "dark"):
        assert f"output: docs/screenshots/rules-inboxes-{theme}.png" in shots
        assert f"docs/screenshots/rules-inboxes-{theme}.png" in readme


# ---- no Inbox around or inside any Library ---------------------------------


@pytest.mark.parametrize("where", ["inside", "holding"])
def test_saving_refuses_an_inbox_overlapping_another_library(vol, db, where):
    """Intake moves what it takes to processed/: an Inbox in or around another
    Library would take that Library's Images out of it."""
    from services import inbox, libraries

    library = _library(vol)
    (vol / "Other" / "games").mkdir(parents=True)
    other = libraries.register(str(vol / "Other" / "games"))
    folder = vol / "Other" / "games" / "in" if where == "inside" else vol / "Other"
    folder.mkdir(exist_ok=True)

    with pytest.raises(inbox.InboxError) as refused:
        inbox.register(str(folder), library.id)

    assert refused.value.status == 409
    assert other.name in str(refused.value)


def test_a_library_cant_be_registered_around_or_inside_an_inbox(vol, db):
    from services import libraries

    library = _library(vol)
    _inbox(vol, library)
    (vol / "Downloads" / "games").mkdir()

    for folder in (vol / "Downloads", vol / "Downloads" / "games"):
        with pytest.raises(libraries.LibraryPathError) as refused:
            libraries.register(str(folder))
        assert refused.value.status == 409
        assert "Inbox" in str(refused.value)


@pytest.mark.asyncio
async def test_an_inbox_whose_library_now_overlaps_it_takes_nothing(vol, db, monkeypatch):
    """Fail closed at poll time too: a Library root that moved over the Inbox
    since it was saved (RomM's root changed) stops intake."""
    from services import libraries

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    monkeypatch.setattr(libraries.get(library.id), "root", str(vol))

    await _poll(box, now=0)
    result = await _poll(box, now=1)

    assert "can't be inside its Library" in result["error"]
    assert game.exists()
    assert not (vol / "Downloads" / "processed").exists()


# ---- review round one (Codex on #443) --------------------------------------


async def _converted(vol, queue, box):
    """A ps2 Image whose conversion intake queued and the queue then completed."""
    from models import JobStatus

    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    await _poll(box, now=0)
    await _poll(box, now=61)
    queue.jobs["job0"].status = JobStatus.COMPLETED
    return game


@pytest.mark.asyncio
async def test_a_parent_swapped_for_a_link_never_moves_a_file_outside_the_inbox(vol, db, queue):
    """the move to processed/ is anchored to the Inbox, so a
    folder swapped for a symlink after the conversion can't redirect it."""
    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = await _converted(vol, queue, box)
    # The same size and mtime as the file taken, so only the anchoring (not
    # the fingerprint) can tell it isn't the Inbox's.
    taken = game.stat()
    protected = _drop(vol / "Protected" / "Game.iso", DISC)
    os.utime(protected, ns=(taken.st_atime_ns, taken.st_mtime_ns))
    game.unlink()
    (vol / "Downloads" / "ps2").rmdir()
    (vol / "Downloads" / "ps2").symlink_to(vol / "Protected")

    await _poll(box, now=122)

    assert protected.exists()
    assert not (vol / "Downloads" / "processed" / "ps2" / "Game.iso").exists()
    [finding] = _inbox_findings()
    assert "couldn't be moved to processed/" in finding["reason"]


@pytest.mark.asyncio
async def test_a_source_replaced_after_its_conversion_stays_in_the_inbox(vol, db, queue):
    """the Library holds the old contents, so a replaced
    source is not archived as processed; it settles and is taken again."""
    from services import inbox

    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = await _converted(vol, queue, box)
    game.write_bytes(b"a different download entirely")

    await _poll(box, now=122)

    assert game.read_bytes() == b"a different download entirely"
    assert not (vol / "Downloads" / "processed").exists()
    assert inbox.get(box.id).processed_count == 0


@pytest.mark.asyncio
async def test_a_numbered_split_image_is_taken_as_one(vol, db):
    """Game.iso.0 and .1 are one Image, listed by its parts."""
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    first = _drop(vol / "Downloads" / "ps3" / "Game.iso.0", b"part one")
    second = _drop(vol / "Downloads" / "ps3" / "Game.iso.1", b"part two")

    await _poll(box, now=0)
    await _poll(box, now=1)

    assert (vol / "ROMs" / "ps3" / "Game.iso.0").read_bytes() == b"part one"
    assert (vol / "ROMs" / "ps3" / "Game.iso.1").read_bytes() == b"part two"
    assert not first.exists() and not second.exists()
    assert (vol / "Downloads" / "processed" / "ps3" / "Game.iso.1").exists()


@pytest.mark.asyncio
async def test_a_finding_for_a_file_removed_across_a_restart_is_cleared(vol, db):
    """the held paths live in memory; the stored Findings are
    reconciled against each listing, so a restart doesn't strand one."""
    from services.inbox import intake

    library = _library(vol)
    box = _inbox(vol, library)
    unsorted = _drop(vol / "Downloads" / "Mystery.iso")
    _drop(vol / "Downloads" / "Other.iso")
    await _poll(box, now=0)
    await _poll(box, now=61)
    assert {Path(f["path"]).name for f in _inbox_findings()} == {"Mystery.iso", "Other.iso"}

    intake.reset_for_tests()  # a restart
    unsorted.unlink()
    await _poll(box, now=122)

    assert {Path(f["path"]).name for f in _inbox_findings()} == {"Other.iso"}


def test_saving_an_inbox_waits_for_a_library_registration(vol, db, monkeypatch):
    """Inbox and Library saves share one registration lock,
    so neither can pass its overlap check while the other is inserting."""
    from services import inbox, libraries

    library = _library(vol)
    (vol / "Downloads").mkdir()
    monkeypatch.setattr(inbox, "_REGISTER_WAIT_SECONDS", 0.05)

    with libraries.registration_lock(), pytest.raises(inbox.InboxError) as refused:
        inbox.register(str(vol / "Downloads"), library.id)

    assert refused.value.status == 409
    assert inbox.all_inboxes() == []


@pytest.mark.asyncio
async def test_the_rules_source_check_runs_off_the_event_loop(vol, db, queue, monkeypatch):
    """whether the Rule's mode takes the file can read its
    header (xiso), so it runs on a worker thread, bounded."""
    import threading

    from services.rules import sweep

    seen_on = []
    real = sweep._accepts_source

    def accepts(*args):
        seen_on.append(threading.current_thread() is threading.main_thread())
        return real(*args)

    monkeypatch.setattr(sweep, "_accepts_source", accepts)
    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=61)

    assert seen_on == [False]


@pytest.mark.asyncio
async def test_a_mode_that_cant_verify_is_not_asked_to(vol, db, queue, monkeypatch):
    """the Intact check runs where the mode supports it
    (the registry says), never forced onto one that can't."""
    from services.tools import registry

    monkeypatch.setattr(registry, "mode_supports_verify", lambda mode: False)
    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=61)

    [job] = queue.submitted
    assert job["verify_after"] is False


# ---- review round two (Codex on #443) --------------------------------------


@pytest.mark.asyncio
async def test_the_copys_verifier_is_chosen_off_the_event_loop(vol, db, monkeypatch):
    """picking the tool that checks a staged copy can open
    the file (romz lists an archive's members), so it runs on a worker thread."""
    import threading

    from services.tools import registry

    seen_on = []
    real = registry.check_tool

    def check_tool(*args, **kwargs):
        seen_on.append(threading.current_thread() is threading.main_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(registry, "check_tool", check_tool)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=1)

    assert seen_on == [False]
    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()


@pytest.mark.asyncio
async def test_a_source_changed_while_it_is_copied_is_not_placed(vol, db, monkeypatch):
    """a source rewritten in place mid-copy yields a copy
    that reads back as written but is neither version; it's discarded and the
    source left to settle again."""
    from services import inbox
    from services.inbox import intake

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso", b"A" * 64)
    monkeypatch.setattr(intake, "_CHUNK", 16)
    real_hasher = intake.new_hasher
    rewritten = []

    def hasher(algorithm):
        inner = real_hasher(algorithm)

        class Rewrites:
            def update(self, data):
                if not rewritten:
                    rewritten.append(True)
                    with open(game, "r+b") as f:  # in place, same size
                        f.seek(48)
                        f.write(b"B" * 16)
                    taken = game.stat()
                    os.utime(game, ns=(taken.st_atime_ns, taken.st_mtime_ns + 10**9))
                inner.update(data)

            def hexdigest(self):
                return inner.hexdigest()

        return Rewrites()

    monkeypatch.setattr(intake, "new_hasher", hasher)

    await _poll(box, now=0)
    await _poll(box, now=1)

    assert rewritten
    assert not (vol / "ROMs" / "ps2" / "Game.iso").exists()
    assert game.read_bytes() == b"A" * 48 + b"B" * 16
    assert not (vol / "Downloads" / "processed").exists()
    assert inbox.get(box.id).processed_count == 0
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_a_copy_tells_its_library_before_the_job_ends(vol, db, monkeypatch):
    """a finished inbox_copy has no output path for
    note_job_finished, so intake tells the Library itself, before the job is
    announced finished: its walk is dropped and the new Image is listed."""
    from models import ConversionMode, JobStatus
    from services import libraries
    from services.inbox import intake

    library = libraries.get(_library(vol).id)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")
    told = []
    real = library.note_paths_changed

    def note(paths):
        copies = [
            j for j in intake.job_manager.jobs.values() if j.mode == ConversionMode.INBOX_COPY
        ]
        told.append((list(paths), [j.status for j in copies]))
        real(paths)

    monkeypatch.setattr(library, "note_paths_changed", note)
    before = library.walk_generation()

    await _poll(box, now=0)
    await _poll(box, now=1)

    placed = str(vol / "ROMs" / "ps2" / "Game.iso")
    assert told and told[-1][0] == [placed]
    assert told[-1][1][-1] == JobStatus.PROCESSING
    assert library.walk_generation() != before


def test_two_finishes_at_once_both_count(vol, db):
    """two Images finishing together (a copy and a
    conversion) both read the count before either wrote it; an atomic
    increment keeps both."""
    import threading

    from services import inbox
    from sqlalchemy import event

    library = _library(vol)
    box = _inbox(vol, library)
    barrier = threading.Barrier(2)

    def both_read_first(_session):
        try:
            barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass

    event.listen(_db.SessionLocal, "before_commit", both_read_first)
    try:
        threads = [threading.Thread(target=inbox.note_processed, args=(box.id,)) for _ in "ab"]
        for each in threads:
            each.start()
        for each in threads:
            each.join()
    finally:
        event.remove(_db.SessionLocal, "before_commit", both_read_first)

    assert inbox.get(box.id).processed_count == 2
    inbox.reset_for_tests()
    assert inbox.get(box.id).processed_count == 2


@pytest.mark.asyncio
async def test_a_sheet_naming_a_track_outside_its_folder_is_held(vol, db):
    """a cue whose track the confined resolver refuses
    (``../tracks/Game.bin``) has no members it can name, so it would be copied
    without its track; it's held with a Finding instead."""
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "tracks" / "Game.bin", b"track")
    sheet = _drop(
        vol / "Downloads" / "psx" / "Game.cue",
        b'FILE "../tracks/Game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n',
    )

    await _poll(box, now=0)
    await _poll(box, now=1)

    assert sheet.exists()
    assert not (vol / "ROMs" / "psx" / "Game.cue").exists()
    held = [f for f in _inbox_findings() if Path(f["path"]).name == "Game.cue"]
    assert [f["kind"] for f in held] == [findings.INBOX_FAILED]
    assert "track" in held[0]["reason"]


# ---- review round three (CodeRabbit on #443) --------------------------------


def test_settle_time_saves_publish_in_commit_order(vol, db, monkeypatch):
    """a save that committed first can't publish last
    over a later one: the write and its publication are one step."""
    import threading

    from services import inbox

    library = _library(vol)
    box = _inbox(vol, library)
    first_in, second_done = threading.Event(), threading.Event()
    real = inbox._replace
    calls = []

    def replace(inbox_id, **changes):
        calls.append(changes)
        if len(calls) == 1:  # the first save, committed, not yet published
            first_in.set()
            second_done.wait(timeout=0.5)
        return real(inbox_id, **changes)

    monkeypatch.setattr(inbox, "_replace", replace)
    first = threading.Thread(target=inbox.update, args=(box.id,), kwargs={"settle_seconds": 0})
    first.start()
    first_in.wait(timeout=5)
    second = threading.Thread(
        target=lambda: (inbox.update(box.id, settle_seconds=90), second_done.set()),
    )
    second.start()
    first.join()
    second.join()

    stored = inbox.get(box.id).settle_seconds
    inbox.reset_for_tests()
    assert stored == inbox.get(box.id).settle_seconds == 90


@pytest.mark.asyncio
async def test_a_file_swapped_in_during_the_move_is_put_back(vol, db, queue, monkeypatch):
    """the entry actually moved must be the one
    checked; a replacement that took its name meanwhile goes back."""
    from services import inbox
    from services.inbox import intake

    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = await _converted(vol, queue, box)
    real = intake._processed_folder

    def swap_then_open(*args):
        fresh = game.with_name("fresh.tmp")
        fresh.write_bytes(DISC)  # the same size: only its inode differs
        taken = game.stat()
        os.utime(fresh, ns=(taken.st_atime_ns, taken.st_mtime_ns))
        os.replace(fresh, game)
        return real(*args)

    monkeypatch.setattr(intake, "_processed_folder", swap_then_open)

    await _poll(box, now=122)

    assert game.exists()
    assert not (vol / "Downloads" / "processed" / "ps2" / "Game.iso").exists()
    assert inbox.get(box.id).processed_count == 0


def test_a_late_staged_copy_closes_its_slot(vol, db):
    """a copy that finishes after its awaiter gave up
    raises at its last progress report and closes its slot."""
    from services import inbox
    from services.inbox import intake
    from services.subprocess_runner import ProgressAbandoned

    library = _library(vol)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    flight = intake._Flight(
        fingerprint="", files=[str(game.resolve())], platform="ps2",
        identities=intake._identities(inbox.get(box.id), [str(game.resolve())]),
    )
    folder = vol / "ROMs" / "ps2"

    def progress(*where):
        if not where:  # given up on by the time the copy is done
            raise ProgressAbandoned("the caller stopped waiting")

    with pytest.raises(ProgressAbandoned):
        intake._stage_copy(inbox.get(box.id), flight, str(folder), progress)

    assert not any((folder / ".compressatorium-stage").glob("slot-*"))


@pytest.mark.asyncio
async def test_a_slow_placement_is_seen_through(vol, db, monkeypatch):
    """a placement past its bound may still land, so
    intake keeps waiting for its outcome rather than failing the copy."""
    import time as clock

    from services import inbox, staging
    from services.inbox import intake

    monkeypatch.setattr(intake, "_PLACE_SECONDS", 0.05)
    real = staging.StagedOutput.place

    def slow_place(self, names):
        clock.sleep(0.3)
        return real(self, names)

    monkeypatch.setattr(staging.StagedOutput, "place", slow_place)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=1)

    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()
    assert (vol / "Downloads" / "processed" / "ps2" / "Game.iso").exists()
    assert inbox.get(box.id).processed_count == 1
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_a_copy_waits_for_a_shared_conversion_slot(vol, db):
    """An Inbox copy counts toward the
    same max_concurrent as conversions. With the one slot taken (a conversion
    running), the copy waits, then runs once it's free, and gives it back."""
    import asyncio

    from services.inbox import intake
    from services.job_manager import job_manager

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")
    await job_manager._semaphore.acquire()  # a conversion holds the slot

    from services import inbox

    await intake.poll(inbox.get(box.id), now=0)
    await intake.poll(inbox.get(box.id), now=1)
    await asyncio.sleep(0.2)
    assert not (vol / "ROMs" / "ps2" / "Game.iso").exists()

    job_manager._semaphore.release()  # the conversion ends
    await intake.drain()

    assert (vol / "ROMs" / "ps2" / "Game.iso").exists()
    assert job_manager._semaphore._value == 1


@pytest.mark.asyncio
async def test_the_concurrency_invariant_counts_a_copy_holding_a_slot(vol, db):
    """The #377 invariant counts what holds a max_concurrent slot: every
    conversion, and now an Inbox copy holding one; a check run still none."""
    from models import ConversionMode
    from services.job_manager import job_manager

    check = job_manager.create_external_job("Check run", ConversionMode.CHECK)
    copy = job_manager.create_external_job("Game.iso", ConversionMode.INBOX_COPY)
    try:
        assert job_manager.slots_in_use() == 0
        assert await job_manager.hold_slot(copy.id) is True
        assert job_manager.slots_in_use() == 1
        with pytest.raises(ValueError):
            await job_manager.hold_slot(check.id)
        job_manager.release_slot(copy.id)
        job_manager.release_slot(copy.id)  # idempotent
        assert job_manager.slots_in_use() == 0
        assert job_manager._semaphore._value == 1
    finally:
        job_manager.jobs.pop(check.id, None)
        job_manager.jobs.pop(copy.id, None)


@pytest.mark.asyncio
async def test_removing_a_library_waits_for_a_registration(vol, db):
    """removal takes the registration lock across its
    Inbox cleanup and the delete, so an Inbox save can't slip in between."""
    import asyncio

    from services import libraries

    library = _library(vol)
    with libraries.registration_lock():
        removing = asyncio.ensure_future(libraries.remove(library.id))
        await asyncio.sleep(0.2)
        assert libraries.get(library.id) is not None
    assert await removing is True
    assert libraries.get(library.id) is None


@pytest.mark.asyncio
async def test_a_verifier_that_must_stop_stops_the_pass(vol, db, monkeypatch):
    """a check whose verifier was abandoned with a
    reader on the storage (stop) places nothing, and no further copy reads
    in that pass; the next pass goes on."""
    from services.inbox import intake
    from services.rules import check as checks
    from services.tools import registry

    library = _library(vol)
    _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "A.iso")
    _drop(vol / "Downloads" / "ps2" / "B.iso")
    monkeypatch.setattr(registry, "check_tool", lambda *_a, **_k: SimpleNamespace(id="fake"))
    stop = [True]
    verified = []

    async def verify(check, cancel_event):
        verified.append(check.path)
        return checks.INTACT, "", stop[0]

    monkeypatch.setattr(checks, "_verify", verify)

    await intake.poll_all(now=0)
    await intake.poll_all(now=1)
    await intake.drain()

    assert len(verified) == 1
    assert not list((vol / "ROMs").rglob("*.iso"))
    assert _inbox_findings() == []

    stop[0] = False
    for now in (2, 3, 4):
        await intake.poll_all(now=now)
        await intake.drain()
    assert sorted(p.name for p in (vol / "ROMs" / "ps2").glob("*.iso")) == ["A.iso", "B.iso"]


def test_a_save_keeps_a_draft_typed_while_it_was_out():
    """the folder field is cleared only when the
    Library and the draft are still the ones that were saved."""
    panel = (
        Path(__file__).resolve().parents[1] / "src/lib/components/views/LibraryInboxes.svelte"
    ).read_text()
    assert "if (libraryId === id && folder === submitted) folder = '';" in panel
    assert "\n      folder = '';\n" not in panel


# ---- review round four (CodeRabbit follow-ups on #443) ----------------------


def _slots(folder: Path) -> list[Path]:
    return list((folder / ".compressatorium-stage").glob("slot-*"))


@pytest.mark.asyncio
async def test_a_cancel_after_the_last_report_still_closes_the_slot(vol, db, monkeypatch):
    """a cancel that lands after the copy's final
    progress report, before its awaiter takes the slot, never leaks it."""
    import asyncio
    import time as clock

    from models import ConversionMode
    from services.inbox import intake

    loop = asyncio.get_running_loop()
    real = intake._stage_copy

    def cancel_then_return(*args, **kwargs):
        result = real(*args, **kwargs)
        [job] = [
            j for j in intake.job_manager.jobs.values()
            if j.mode == ConversionMode.INBOX_COPY and j.status == "processing"
        ]
        loop.call_soon_threadsafe(intake.job_manager.get_cancel_event(job.id).set)
        clock.sleep(0.3)  # the awaiter gives up meanwhile
        return result

    monkeypatch.setattr(intake, "_stage_copy", cancel_then_return)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=1)
    for _ in range(50):
        if not _slots(vol / "ROMs" / "ps2"):
            break
        await asyncio.sleep(0.05)

    assert not _slots(vol / "ROMs" / "ps2")
    assert not (vol / "ROMs" / "ps2" / "Game.iso").exists()


@pytest.mark.asyncio
async def test_a_cancelled_check_that_must_stop_still_stops_the_pass(vol, db, monkeypatch):
    """A cancelled verdict with a reader still on
    the storage stops the pass as any stop does."""
    from services.inbox import intake
    from services.rules import check as checks
    from services.tools import registry

    library = _library(vol)
    _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "A.iso")
    _drop(vol / "Downloads" / "ps2" / "B.iso")
    monkeypatch.setattr(registry, "check_tool", lambda *_a, **_k: SimpleNamespace(id="fake"))
    verified = []

    async def verify(check, cancel_event):
        verified.append(check.path)
        return checks.CANCELLED, "", True

    monkeypatch.setattr(checks, "_verify", verify)

    await intake.poll_all(now=0)
    await intake.poll_all(now=1)
    await intake.drain()

    assert len(verified) == 1
    assert intake._storage_stop is True


@pytest.mark.asyncio
async def test_a_stop_reaches_a_copy_already_running_beside_it(vol, db, monkeypatch):
    """With two slots, a copy staged beside the one
    whose check stops is checked against the stop under the same lock, so it
    neither verifies nor places."""
    import asyncio
    import threading

    from services.inbox import intake
    from services.job_manager import job_manager
    from services.rules import check as checks
    from services.tools import registry

    monkeypatch.setattr(job_manager, "max_concurrent", 2)
    monkeypatch.setattr(job_manager, "_semaphore", asyncio.Semaphore(2))
    _two_global_slots(monkeypatch, vol)
    library = _library(vol)
    _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "A.iso")
    _drop(vol / "Downloads" / "ps2" / "B.iso")
    monkeypatch.setattr(registry, "check_tool", lambda *_a, **_k: SimpleNamespace(id="fake"))
    staged = threading.Event()
    real_stage = intake._stage_copy

    def stage(inbox, flight, folder, progress):
        result = real_stage(inbox, flight, folder, progress)
        if os.path.basename(flight.files[0]) == "B.iso":
            staged.set()
        return result

    monkeypatch.setattr(intake, "_stage_copy", stage)
    verified = []

    async def verify(check, cancel_event):
        verified.append(os.path.basename(check.path))
        for _ in range(100):  # the other copy finishes staging meanwhile
            if staged.is_set():
                break
            await asyncio.sleep(0.02)
        return checks.INTACT, "", True

    monkeypatch.setattr(checks, "_verify", verify)

    await intake.poll_all(now=0)
    await intake.poll_all(now=1)
    await intake.drain()

    assert staged.is_set()
    assert len(verified) == 1
    assert not list((vol / "ROMs").rglob("*.iso"))


@pytest.mark.asyncio
async def test_cancelling_a_slot_wait_never_leaks_the_slot(vol, db):
    """a hold_slot cancelled while the slot is taken
    leaves no acquisition behind to take it later."""
    import asyncio

    from models import ConversionMode
    from services.job_manager import job_manager

    copy = job_manager.create_external_job("Game.iso", ConversionMode.INBOX_COPY)
    try:
        await job_manager._semaphore.acquire()  # a conversion holds the slot
        waiting = asyncio.ensure_future(job_manager.hold_slot(copy.id, asyncio.Event()))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        job_manager._semaphore.release()  # the conversion ends
        await asyncio.sleep(0.05)

        assert job_manager._semaphore._value == 1
        assert job_manager.slots_in_use() == 0
    finally:
        job_manager.jobs.pop(copy.id, None)


def test_a_name_replaced_between_link_and_unlink_is_never_deleted(tmp_path, monkeypatch):
    """the link fallback removes the source name only
    while it is still the file it linked; a replacement there stays."""
    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace", lambda *_a: False)
    src, dst = tmp_path / "src.iso", tmp_path / "dst.iso"
    src.write_bytes(b"original")
    real_link = os.link

    def link_then_replace(a, b, **kwargs):
        real_link(a, b, **kwargs)
        replacement = tmp_path / "new.tmp"
        replacement.write_bytes(b"a replacement")
        os.replace(replacement, src)

    monkeypatch.setattr(path_utils.os, "link", link_then_replace)

    path_utils.place_without_clobbering(str(src), str(dst))

    assert dst.read_bytes() == b"original"
    assert src.read_bytes() == b"a replacement"


def test_the_no_replace_rename_is_preferred_over_link_and_unlink(tmp_path, monkeypatch):
    """Where the kernel has one, the move is one atomic no-replace rename."""
    from utils import path_utils

    if path_utils._noreplace_rename_call() is None:
        pytest.skip("no no-replace rename on this platform")

    def no_link(*_a, **_k):
        raise AssertionError("linked instead of renaming")

    monkeypatch.setattr(path_utils.os, "link", no_link)
    src, dst = tmp_path / "src.iso", tmp_path / "dst.iso"
    src.write_bytes(b"original")

    path_utils.place_without_clobbering(str(src), str(dst))

    assert dst.read_bytes() == b"original"
    assert not src.exists()
    taken = tmp_path / "taken.iso"
    taken.write_bytes(b"there first")
    src.write_bytes(b"again")
    with pytest.raises(FileExistsError):
        path_utils.place_without_clobbering(str(src), str(taken))
    assert taken.read_bytes() == b"there first"


def test_a_put_back_that_fails_is_reported(tmp_path, monkeypatch, caplog):
    """move_anchored no longer swallows a failed put
    back: it is logged and raised as MoveRollbackFailed."""
    import logging

    from utils import path_utils

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "Game.iso").write_bytes(b"checked")
    expected = path_utils.file_snapshot((a / "Game.iso").stat())
    real = path_utils.place_without_clobbering
    calls = []

    def swap_then_refuse(src, dst, **kwargs):
        calls.append(src)
        if len(calls) == 1:
            real(src, dst, **kwargs)
            (b / "Game.iso").unlink()
            (b / "Game.iso").write_bytes(b"swapped in")  # a different inode
            return
        raise PermissionError("can't put it back")

    monkeypatch.setattr(path_utils, "place_without_clobbering", swap_then_refuse)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        with caplog.at_level(logging.ERROR), pytest.raises(path_utils.MoveRollbackFailed):
            path_utils.move_anchored("Game.iso", src_fd, dst_fd, expected)
    finally:
        os.close(src_fd)
        os.close(dst_fd)
    assert any("couldn't be put back" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_failed_put_back_in_processed_is_held_with_a_finding(vol, db, queue, monkeypatch):
    """A move to processed/ whose put-back failed is reported on the Image,
    not taken as a quietly changed file."""
    from services.inbox import intake
    from utils import path_utils

    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    await _converted(vol, queue, box)

    def refuse(*_a, **_k):
        raise path_utils.MoveRollbackFailed(errno.EIO, "Game.iso couldn't be put back")

    monkeypatch.setattr(intake, "move_anchored", refuse)

    await _poll(box, now=122)

    [finding] = _inbox_findings()
    assert "couldn't be put back" in finding["reason"]


# ---- review round five (CodeRabbit follow-ups on #443) ----------------------


def _two_folders(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "Game.iso").write_bytes(b"original")
    return a, b


def _quarantined(folder) -> list:
    return [p.name for p in folder.iterdir() if p.name.startswith(".compressatorium-q-")]


def test_a_replacement_between_link_and_quarantine_is_put_back(tmp_path, monkeypatch):
    """Where there is no atomic no-replace rename, a user's file moves by link,
    quarantine rename and check (#431): a file put
    under the name between the link and the quarantine rename is put back,
    and the file moved stays moved. Nothing is deleted."""
    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)
    real_link = os.link
    calls = []

    def link_then_replace(src, dst, **kwargs):
        real_link(src, dst, **kwargs)
        calls.append(src)
        if len(calls) == 1:  # the move's link, not the put-back
            (a / "new.tmp").write_bytes(b"a replacement")
            os.replace(a / "new.tmp", a / "Game.iso")

    monkeypatch.setattr(path_utils.os, "link", link_then_replace)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        path_utils.place_without_clobbering(
            "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
            identity_sensitive=True,
        )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert (b / "Game.iso").read_bytes() == b"original"
    assert (a / "Game.iso").read_bytes() == b"a replacement"
    assert _quarantined(a) == []


def test_a_replacement_after_the_quarantine_rename_is_left_alone(tmp_path, monkeypatch):
    """A file put under the name after the quarantine rename, before the
    check, is never touched; the moved file's quarantine name goes."""
    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)
    real_rename = os.rename

    def rename_then_recreate(src, dst, **kwargs):
        real_rename(src, dst, **kwargs)
        (a / "Game.iso").write_bytes(b"a new download")

    monkeypatch.setattr(path_utils.os, "rename", rename_then_recreate)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        path_utils.place_without_clobbering(
            "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
            identity_sensitive=True,
        )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert (b / "Game.iso").read_bytes() == b"original"
    assert (a / "Game.iso").read_bytes() == b"a new download"
    assert _quarantined(a) == []


def test_a_replacement_that_cant_be_put_back_is_kept_and_named(tmp_path, monkeypatch):
    """When the name is taken again before the replacement can go back, the
    replacement stays under its quarantine name, and MoveRollbackFailed
    names it."""
    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)
    real_link, real_rename = os.link, os.rename
    state = {"links": 0}

    def link(src, dst, **kwargs):
        state["links"] += 1
        real_link(src, dst, **kwargs)
        if state["links"] == 1:
            (a / "new.tmp").write_bytes(b"a replacement")
            os.replace(a / "new.tmp", a / "Game.iso")

    def rename_then_recreate(src, dst, **kwargs):
        real_rename(src, dst, **kwargs)
        (a / "Game.iso").write_bytes(b"made again")

    monkeypatch.setattr(path_utils.os, "link", link)
    monkeypatch.setattr(path_utils.os, "rename", rename_then_recreate)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        with pytest.raises(path_utils.MoveRollbackFailed) as failed:
            path_utils.place_without_clobbering(
                "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                identity_sensitive=True,
            )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    [kept] = _quarantined(a)
    assert kept in str(failed.value)
    # A private folder of the app's own, holding the replacement.
    assert [p.read_bytes() for p in (a / kept).iterdir()] == [b"a replacement"]
    assert (a / "Game.iso").read_bytes() == b"made again"
    assert (b / "Game.iso").read_bytes() == b"original"


@pytest.mark.asyncio
async def test_a_copy_paused_before_its_first_open_sees_the_stop(vol, db, monkeypatch):
    """with two slots, a copy that reached its first
    file open before the other's check stopped checks the stop there, and
    opens nothing."""
    import asyncio
    import time as clock

    from services.inbox import intake
    from services.job_manager import job_manager
    from services.rules import check as checks
    from services.tools import registry

    monkeypatch.setattr(job_manager, "max_concurrent", 2)
    monkeypatch.setattr(job_manager, "_semaphore", asyncio.Semaphore(2))
    _two_global_slots(monkeypatch, vol)
    library = _library(vol)
    _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "A.iso")
    _drop(vol / "Downloads" / "ps2" / "B.iso")
    monkeypatch.setattr(registry, "check_tool", lambda *_a, **_k: SimpleNamespace(id="fake"))
    real_may_open = intake._may_open
    opened = []

    def may_open(name):
        if name == "B.iso" and not opened:
            opened.append("paused")
            for _ in range(250):  # until the other copy's check has stopped
                if intake._stopped():
                    break
                clock.sleep(0.02)
        answer = real_may_open(name)
        opened.append(name)
        return answer

    monkeypatch.setattr(intake, "_may_open", may_open)
    verified = []

    async def verify(check, cancel_event):
        verified.append(os.path.basename(check.path))
        return checks.INTACT, "", True

    monkeypatch.setattr(checks, "_verify", verify)

    await intake.poll_all(now=0)
    await intake.poll_all(now=1)
    await intake.drain()

    assert verified == ["A.iso"]
    assert "B.iso" not in opened
    assert not list((vol / "ROMs").rglob("*.iso"))


@pytest.mark.asyncio
async def test_a_copy_cancelled_while_it_waits_to_place_places_nothing(vol, db):
    """a copy cancelled while it waits for the
    check-and-place lock (no tool checks a PS2 .iso) doesn't place."""
    import asyncio

    from models import ConversionMode
    from services.inbox import intake

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    from services import inbox

    async with intake._checking():
        await intake.poll(inbox.get(box.id), now=0)
        await intake.poll(inbox.get(box.id), now=1)
        for _ in range(100):  # staged, now waiting for the lock
            if _slots(vol / "ROMs" / "ps2"):
                break
            await asyncio.sleep(0.02)
        [job] = [
            j for j in intake.job_manager.jobs.values()
            if j.mode == ConversionMode.INBOX_COPY and j.status == "processing"
        ]
        await intake.job_manager.cancel_job(job.id)
    await intake.drain()

    assert not (vol / "ROMs" / "ps2" / "Game.iso").exists()
    assert game.exists()
    [finding] = _inbox_findings()
    assert "cancelled" in finding["reason"]


@pytest.mark.asyncio
async def test_a_slow_move_to_processed_is_seen_through(vol, db, monkeypatch):
    """a move to processed/ past its bound may still land;
    intake waits for its outcome and counts it, rather than holding the
    Image with a Finding that then vanishes."""
    import time as clock

    from services import inbox
    from services.inbox import intake

    monkeypatch.setattr(intake, "_MOVE_SECONDS", 0.05)
    real = intake._move_to_processed

    def slow_move(*args):
        clock.sleep(0.3)
        return real(*args)

    monkeypatch.setattr(intake, "_move_to_processed", slow_move)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    result = await _poll(box, now=1)
    later = await _poll(box, now=2)

    assert (vol / "Downloads" / "processed" / "ps2" / "Game.iso").exists()
    assert inbox.get(box.id).processed_count == 1
    assert [p["platform"] for p in result["placed"] + later["placed"]] == ["ps2"]
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_a_copy_waits_for_another_job_writing_its_output(vol, db):
    """a copy takes the shared output locks for what it
    places; one another job holds (a conversion writing that name) leaves the
    Image for the next poll, untouched and with no Finding."""
    from services.lock_manager import lock_manager

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    target = vol / "ROMs" / "ps2" / "Game.iso"
    target.parent.mkdir(parents=True)
    assert lock_manager.acquire_lock(str(target))
    try:
        await _poll(box, now=0)
        await _poll(box, now=1)
        assert not target.exists()
        assert game.exists()
        assert _inbox_findings() == []
    finally:
        lock_manager.release_lock(str(target))

    for now in (2, 3):
        await _poll(box, now=now)
    assert target.exists()
    assert not lock_manager.is_locked(str(target))


@pytest.mark.asyncio
async def test_a_conversion_is_queued_with_its_sources_identity(vol, db, queue):
    """intake hands the conversion the Source's identity and
    the Inbox it must stay under, which the job checks before its tool runs
    (tests/test_source_identity_431.py)."""
    from services import inbox

    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=61)

    [job] = queue.submitted
    identity = job["source_identities"][str(game.resolve())]
    assert identity["root"] == inbox.get(box.id).folder
    taken = game.stat()
    assert identity["snapshot"] == {
        "size": taken.st_size, "mtime_ns": taken.st_mtime_ns,
        "inode": taken.st_ino, "device": taken.st_dev,
    }


@pytest.mark.asyncio
async def test_an_open_in_flight_when_the_stop_lands_reads_nothing(vol, db, monkeypatch):
    """A copy past `_may_open()` but not yet in
    `os.open()` when another copy's check stops may finish that one open, but
    the stop is checked again as soon as it returns: the file is closed
    without a byte read, and the copy ends deferred."""
    import asyncio
    import threading
    import time as clock

    from services.inbox import intake
    from services.job_manager import job_manager
    from services.rules import check as checks
    from services.tools import registry

    monkeypatch.setattr(job_manager, "max_concurrent", 2)
    monkeypatch.setattr(job_manager, "_semaphore", asyncio.Semaphore(2))
    _two_global_slots(monkeypatch, vol)
    library = _library(vol)
    _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "A.iso")
    _drop(vol / "Downloads" / "ps2" / "B.iso")
    monkeypatch.setattr(registry, "check_tool", lambda *_a, **_k: SimpleNamespace(id="fake"))
    real_may_open = intake._may_open
    b_thread = []
    b_checks = []

    def may_open(name):
        answer = real_may_open(name)
        if name == "B.iso":
            b_checks.append(name)
        # The check right before the file's own open (the first is before the
        # slot is made): past it, not yet opened.
        if name == "B.iso" and len(b_checks) == 2 and not b_thread:
            b_thread.append(threading.get_ident())
            for _ in range(250):
                if intake._stopped():
                    break
                clock.sleep(0.02)
        return answer

    monkeypatch.setattr(intake, "_may_open", may_open)
    real_fdopen = os.fdopen
    b_reads = []

    def fdopen(fd, *args, **kwargs):
        if b_thread and threading.get_ident() == b_thread[0]:
            b_reads.append(fd)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(intake.os, "fdopen", fdopen)
    verified = []

    async def verify(check, cancel_event):
        verified.append(os.path.basename(check.path))
        for _ in range(250):  # the other copy is between its check and its open
            if b_thread:
                break
            await asyncio.sleep(0.02)
        return checks.INTACT, "", True

    monkeypatch.setattr(checks, "_verify", verify)

    await intake.poll_all(now=0)
    await intake.poll_all(now=1)
    await intake.drain()

    assert b_thread, "the second copy never reached its open"
    assert verified == ["A.iso"]
    assert b_reads == []
    assert not list((vol / "ROMs").rglob("*.iso"))



# ---- review round six (CodeRabbit follow-ups on #443) -----------------------


def test_the_quarantine_is_a_private_folder_no_name_can_collide_with(tmp_path, monkeypatch):
    """The source is quarantined into a fresh
    folder of its own (mode 0700, same filesystem), not under a name in the
    shared folder: a name already there is never overwritten, and the
    quarantined entry is reachable only inside the private folder."""
    import stat as st

    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)
    taken = a / ".compressatorium-q-taken"
    taken.write_bytes(b"someone else's")
    # The private folder is made through the parent's descriptor (0700, never
    # an existing name): its first random name collides with an entry there.
    names = iter([".compressatorium-q-taken", ".compressatorium-q-fresh"])
    monkeypatch.setattr(path_utils, "short_temp_name", lambda **_k: next(names))
    real_rename = os.rename
    seen = {}

    def rename(src, dst, **kwargs):
        real_rename(src, dst, **kwargs)
        private = a / ".compressatorium-q-fresh"
        seen["mode"] = st.S_IMODE(private.stat().st_mode)
        seen["inside"] = sorted(p.name for p in private.iterdir())
        seen["shared"] = sorted(p.name for p in a.iterdir())

    monkeypatch.setattr(path_utils.os, "rename", rename)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        path_utils.place_without_clobbering(
            "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
            identity_sensitive=True,
        )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert taken.read_bytes() == b"someone else's"
    assert seen["mode"] == 0o700
    assert seen["inside"] == ["Game.iso"]
    assert "Game.iso" not in seen["shared"]
    assert (b / "Game.iso").read_bytes() == b"original"
    assert sorted(p.name for p in a.iterdir()) == [".compressatorium-q-taken"]


@pytest.mark.asyncio
async def test_a_stop_before_the_read_back_open_reads_nothing(vol, db, monkeypatch):
    """the read-back of the staged copy is an open like
    any other: a stop that lands between its check and its open closes it
    unread, and the copy ends deferred."""
    from services.inbox import intake

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")
    real_may_open = intake._may_open
    checks_seen = []

    def may_open(name):
        answer = real_may_open(name)
        checks_seen.append(name)
        if len(checks_seen) == 3:  # the read-back's check: a stop lands now
            intake._set_stop(True)
        return answer

    monkeypatch.setattr(intake, "_may_open", may_open)

    await _poll(box, now=0)
    await _poll(box, now=1)

    assert len(checks_seen) == 3
    assert not (vol / "ROMs" / "ps2" / "Game.iso").exists()
    assert (vol / "Downloads" / "ps2" / "Game.iso").exists()
    assert _inbox_findings() == []



# ---- review round seven (Codex on #443) -------------------------------------


@pytest.mark.asyncio
async def test_images_that_share_a_track_are_held_together(vol, db):
    """Two sheets naming one track are held, both,
    with a Finding naming the other; nothing is taken or moved."""
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    sheet = b'FILE "Track.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
    a = _drop(vol / "Downloads" / "psx" / "A.cue", sheet)
    b = _drop(vol / "Downloads" / "psx" / "B.cue", sheet)
    track = _drop(vol / "Downloads" / "psx" / "Track.bin", b"track")

    await _poll(box, now=0)
    await _poll(box, now=1)
    await _poll(box, now=2)

    assert a.exists() and b.exists() and track.exists()
    assert not (vol / "ROMs" / "psx").exists()
    assert not (vol / "Downloads" / "processed").exists()
    held = {Path(f["path"]).name: f["reason"] for f in _inbox_findings()}
    assert set(held) == {"A.cue", "B.cue"}
    assert "B.cue" in held["A.cue"] and "A.cue" in held["B.cue"]
    assert "shares files" in held["A.cue"]


@pytest.mark.asyncio
async def test_output_locks_taken_after_their_wait_gave_up_are_released(vol, db, monkeypatch):
    """An output-lock acquisition that outlives its
    bound hands what it took back once it finishes; no lock is left held."""
    import asyncio
    import time as clock

    from services.inbox import intake
    from services.lock_manager import lock_manager

    monkeypatch.setattr(intake, "_LOCK_SECONDS", 0.05)
    real = lock_manager.acquire_all

    def slow(wanted):
        clock.sleep(0.3)
        return real(wanted)

    monkeypatch.setattr(lock_manager, "acquire_all", slow)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    await _poll(box, now=1)
    for _ in range(50):
        if not lock_manager._locks:
            break
        await asyncio.sleep(0.05)

    assert not lock_manager._locks
    assert not (vol / "ROMs" / "ps2" / "Game.iso").exists()


@pytest.mark.asyncio
async def test_a_copy_waits_for_the_global_slot_another_process_holds(
    vol, db, monkeypatch, tmp_path,
):
    """Hold_slot takes the cross-process slot a
    conversion takes too, so a CLI process holding the only one makes the
    copy wait; ending gives both back."""
    import asyncio

    from models import ConversionMode
    from services import job_manager as job_manager_module
    from services.concurrency_manager import ConcurrencyManager
    from services.job_manager import job_manager

    shared = ConcurrencyManager(1, str(tmp_path / "locks"))
    other = ConcurrencyManager(1, str(tmp_path / "locks"))  # the CLI process
    monkeypatch.setattr(job_manager_module, "concurrency_manager", shared)
    assert await other.acquire("cli-job")
    copy = job_manager.create_external_job("Game.iso", ConversionMode.INBOX_COPY)
    try:
        waiting = asyncio.ensure_future(job_manager.hold_slot(copy.id, asyncio.Event()))
        await asyncio.sleep(0.5)
        assert not waiting.done()

        other.release("cli-job")
        assert await asyncio.wait_for(waiting, 5) is True
        job_manager.release_slot(copy.id)
        assert await asyncio.wait_for(other.acquire("cli-job-2"), 5)
        other.release("cli-job-2")
    finally:
        job_manager.jobs.pop(copy.id, None)



# ---- review round eight (Codex on #443) -------------------------------------


@pytest.mark.asyncio
async def test_tracks_beside_a_sheet_that_cant_be_read_are_held(vol, db):
    """A sheet that can't be read whole (here, over
    the size cap) owns nobody, so the files beside it that it could name are
    held with a Finding naming it, and none is taken."""
    from utils.sheet_text import MAX_SHEET_BYTES

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    sheet = (
        b'FILE "Track.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
        + b"REM " + b"x" * MAX_SHEET_BYTES + b"\n"
    )
    cue = _drop(vol / "Downloads" / "psx" / "Game.cue", sheet)
    track = _drop(vol / "Downloads" / "psx" / "Track.bin", b"track")

    await _poll(box, now=0)
    await _poll(box, now=1)
    await _poll(box, now=2)

    assert cue.exists() and track.exists()
    assert not (vol / "ROMs" / "psx").exists()
    held = {Path(f["path"]).name: f["reason"] for f in _inbox_findings()}
    assert "Track.bin" in held
    assert "Game.cue" in held["Track.bin"]



# ---- review round nine (CodeRabbit on #443) ---------------------------------


@pytest.mark.asyncio
async def test_a_poll_whose_inbox_is_removed_meanwhile_starts_no_copy(vol, db, monkeypatch):
    """Removing the Inbox while a poll awaits
    its listing revokes that poll's state; it admits no new copy."""
    from models import ConversionMode
    from services import inbox
    from services.inbox import intake

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")
    await _poll(box, now=0)
    real_list = intake._list

    def removed_while_listing(*args, **kwargs):
        listing = real_list(*args, **kwargs)
        intake.forget(box.id)  # the Inbox is removed meanwhile
        return listing

    monkeypatch.setattr(intake, "_list", removed_while_listing)
    before = [j.id for j in intake.job_manager.jobs.values() if j.mode == ConversionMode.INBOX_COPY]

    await intake.poll(inbox.get(box.id), now=1)
    await intake.drain()

    after = [j.id for j in intake.job_manager.jobs.values() if j.mode == ConversionMode.INBOX_COPY]
    assert after == before
    assert not (vol / "ROMs" / "ps2" / "Game.iso").exists()
    assert (vol / "Downloads" / "ps2" / "Game.iso").exists()


@pytest.mark.asyncio
async def test_a_slow_move_after_a_conversion_never_holds_up_the_poll(vol, db, queue, monkeypatch):
    """Moving a converted original to
    processed/ is a tracked flight the poll doesn't wait on; its real outcome
    is reconciled when it finishes."""
    import time as clock

    from services import inbox
    from services.inbox import intake

    monkeypatch.setattr(intake, "_MOVE_SECONDS", 0.05)
    real = intake._move_to_processed

    def slow_move(*args):
        clock.sleep(0.6)
        return real(*args)

    monkeypatch.setattr(intake, "_move_to_processed", slow_move)
    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = await _converted(vol, queue, box)

    started = clock.monotonic()
    await intake.poll(inbox.get(box.id), now=122)
    took = clock.monotonic() - started
    await intake.drain()

    assert took < 0.4
    assert not game.exists()
    assert (vol / "Downloads" / "processed" / "ps2" / "Game.iso").exists()
    assert inbox.get(box.id).processed_count == 1


@pytest.mark.asyncio
async def test_removing_a_library_gives_up_on_a_registration_that_wont_end(vol, db, monkeypatch):
    """Removal waits a bounded time for the
    registration lock, then answers a retryable 409 with nothing cleaned up."""
    from services import inbox, libraries

    library = _library(vol)
    _inbox(vol, library)
    monkeypatch.setattr(libraries, "REGISTER_WAIT_SECONDS", 0.1)

    with libraries.registration_lock():
        with pytest.raises(libraries.LibraryPathError) as busy:
            await libraries.remove(library.id)

    assert busy.value.status == 409
    assert libraries.get(library.id) is not None
    assert len(inbox.for_library(library.id)) == 1


def test_the_quarantine_is_set_up_before_anything_is_published(tmp_path, monkeypatch):
    """The private folder is made before the
    destination is linked, so a failure making it publishes nothing."""
    import errno

    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)

    def no_space(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(path_utils, "_private_folder", no_space)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        with pytest.raises(OSError):
            path_utils.place_without_clobbering(
                "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                identity_sensitive=True,
            )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert not (b / "Game.iso").exists()
    assert (a / "Game.iso").read_bytes() == b"original"


def test_a_failure_after_the_link_unpublishes_the_destination(tmp_path, monkeypatch):
    """A step after the link that fails while the source is still in place
    (its quarantine rename) unlinks the destination it published, after
    checking it is that inode, and raises."""
    import errno

    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)

    def eio(*_a, **_k):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(path_utils.os, "rename", eio)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        with pytest.raises(OSError) as failed:
            path_utils.place_without_clobbering(
                "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                identity_sensitive=True,
            )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert not isinstance(failed.value, path_utils.PlacementCommitted)
    assert not (b / "Game.iso").exists()
    assert (a / "Game.iso").read_bytes() == b"original"
    assert _quarantined(a) == []


def test_a_failure_once_the_file_has_moved_says_the_placement_was_committed(
    tmp_path, monkeypatch,
):
    """A step that fails after the source is in quarantine raises
    PlacementCommitted: the file is at the destination, and the caller
    (the Image rename's rollback) must treat it as moved."""
    import errno

    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)
    real_stat = os.stat
    calls = []

    def stat_fails_in_quarantine(path, *args, **kwargs):
        calls.append(path)
        if path == "Game.iso" and kwargs.get("dir_fd") is not None and len(calls) > 1:
            raise OSError(errno.EIO, "Input/output error")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(path_utils.os, "stat", stat_fails_in_quarantine)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        with pytest.raises(path_utils.PlacementCommitted):
            path_utils.place_without_clobbering(
                "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                identity_sensitive=True,
            )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert (b / "Game.iso").read_bytes() == b"original"


def test_the_inbox_screenshot_requires_its_own_inbox():
    """after a 409 the shot lists the Library's
    Inboxes and requires the expected folder, and waits for its row."""
    shots = (Path(__file__).resolve().parents[1] / "shots.yml").read_text()
    block = shots[shots.index("rules-inboxes-light"):shots.index("rules-inboxes-dark")]
    assert "/inboxes`" in block and "listed.inboxes" in block
    assert "downloads" in block and "status row" in block


# ---- round ten: CodeRabbit follow-ups on f90d9826 -------------------------


def test_a_failed_unpublish_after_a_failed_quarantine_says_it_is_committed(
    tmp_path, monkeypatch,
):
    """The quarantine rename fails and taking
    the destination back fails too. Both names are left, so the move is
    answered PlacementCommitted (dst still published), the first failure
    chained, never swallowed."""
    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)

    def rename_fails(*_a, **_k):
        raise OSError(errno.EIO, "Input/output error (rename)")

    def unlink_fails(*_a, **_k):
        raise OSError(errno.EROFS, "Read-only file system (unlink)")

    monkeypatch.setattr(path_utils.os, "rename", rename_fails)
    monkeypatch.setattr(path_utils.os, "unlink", unlink_fails)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        with pytest.raises(path_utils.PlacementCommitted) as committed:
            path_utils.place_without_clobbering(
                "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                identity_sensitive=True,
            )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert "rename" in str(committed.value.__cause__)
    assert (b / "Game.iso").read_bytes() == b"original"
    assert (a / "Game.iso").read_bytes() == b"original"


@pytest.mark.asyncio
async def test_removing_an_inbox_waits_for_a_copy_being_admitted(vol, db, monkeypatch):
    """A copy's revoked check and its
    scheduling are one step under the state's lock, so a removal on another
    thread can't land between them."""
    import threading

    from services import inbox
    from services.inbox import intake

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")
    await _poll(box, now=0)
    real_track = intake._track
    seen: dict = {}

    def removed_while_admitting(*args, **kwargs):
        remover = threading.Thread(target=intake.forget, args=(box.id,))
        remover.start()
        remover.join(0.3)
        seen["removal_waited"] = remover.is_alive()
        result = real_track(*args, **kwargs)
        seen["remover"] = remover
        return result

    monkeypatch.setattr(intake, "_track", removed_while_admitting)
    await intake.poll(inbox.get(box.id), now=1)
    seen["remover"].join(5)
    await intake.drain()

    assert seen["removal_waited"] is True


@pytest.mark.asyncio
async def test_a_conversion_queued_as_its_inbox_is_removed_is_cancelled(
    vol, db, queue, monkeypatch,
):
    """An Inbox removed while its conversion is
    being queued has the jobs just made cancelled while they are still
    queued, and the Source is left alone."""
    from services import inbox
    from services.inbox import intake

    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    await _poll(box, now=0)
    real_create = intake.job_manager.create_batch_jobs
    cancelled: list[tuple[str, str]] = []

    async def removed_while_queueing(*args, **kwargs):
        jobs = await real_create(*args, **kwargs)
        intake.forget(box.id)
        return jobs

    async def cancel_job(job_id):
        job = queue.jobs[job_id]
        cancelled.append((job_id, job.status))
        job.status = "cancelled"
        return True

    monkeypatch.setattr(intake.job_manager, "create_batch_jobs", removed_while_queueing)
    monkeypatch.setattr(intake.job_manager, "cancel_job", cancel_job)
    result = await intake.poll(inbox.get(box.id), now=61)

    assert cancelled == [("job0", "queued")]
    assert result["queued"] == 0
    assert game.read_bytes() == DISC
    assert not (vol / "Downloads" / "processed").exists()


def _run_inbox_shot(listed: list[str]) -> subprocess.CompletedProcess:
    """Run the Inbox screenshot's script under node with a stubbed browser:
    the Library registered at ``/real/vol/roms`` (its root resolved by the
    backend; the Volume says ``/vol``), the Inbox save answering 409, and
    *listed* as this Library's Inboxes, each with its status row."""
    import yaml

    shots = yaml.safe_load((Path(__file__).resolve().parents[1] / "shots.yml").read_text())
    script = next(s for s in shots if "rules-inboxes-light" in s["url"])["javascript"]
    harness = """
const listed = JSON.parse(process.argv[1]);
const json = (data, status = 200) => ({ ok: status < 300, status, json: async () => data });
globalThis.location = { search: '?s=rules-inboxes-light', hash: '' };
globalThis.localStorage = { setItem() {} };
globalThis.StorageEvent = class { constructor() {} };
globalThis.window = { dispatchEvent() {} };
globalThis.setTimeout = (fn) => Promise.resolve().then(fn);
globalThis.fetch = async (url, opts = {}) => {
  if (url === '/api/volumes') return json([{ name: 'ROM Library', path: '/vol' }]);
  if (url === '/api/libraries' && opts.method === 'POST') return json({}, 409);
  const library = { kind: 'folder', id: 'lib1', root: '/real/vol/roms' };
  if (url === '/api/libraries') return json([library]);
  if (url.endsWith('/inboxes') && opts.method === 'POST') return json({}, 409);
  if (url.endsWith('/inboxes')) return json({ inboxes: listed.map(folder => ({ folder })) });
  throw new Error('unexpected fetch ' + url);
};
const rows = listed.map(folder => ({
  querySelector: sel => (sel === '.where code' ? { textContent: folder }
    : sel === '.status' ? {} : null),
}));
const panel = { querySelectorAll: () => rows, scrollIntoView() {} };
globalThis.document = { querySelector: sel => (sel === '.inboxes' ? panel : null) };
eval(require('fs').readFileSync(process.argv[2], 'utf8'))
  .then(() => console.log('captured'), e => { console.log('refused: ' + e.message); });
"""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        shot = Path(scratch) / "shot.js"
        shot.write_text(script)
        return subprocess.run(
            ["node", "-e", harness, json.dumps(listed), str(shot)],
            capture_output=True, text=True, timeout=30, check=False,
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")
def test_the_inbox_shot_refuses_another_inbox_named_downloads():
    """The shot compares the full canonical
    folder (resolved as the backend resolves it), so another Inbox whose
    basename is also ``downloads`` is never taken for the expected one."""
    refused = _run_inbox_shot(["/real/other/downloads"])
    assert refused.stdout.strip().startswith("refused"), refused.stdout + refused.stderr

    captured = _run_inbox_shot(["/real/other/downloads", "/real/vol/downloads"])
    assert captured.stdout.strip() == "captured", captured.stdout + captured.stderr


def test_a_cleanup_that_cant_stat_the_destination_says_it_is_committed(
    tmp_path, monkeypatch,
):
    """When the quarantine rename fails and the
    cleanup can't stat the destination (anything but ENOENT), it can't tell
    whether dst is still published: PlacementCommitted, both failures
    chained, so a caller rolls it back."""
    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)
    real_stat = os.stat
    renamed = []

    def rename_fails(*_a, **_k):
        renamed.append(True)
        raise OSError(errno.EIO, "Input/output error (rename)")

    def stat_fails_after(path, *args, **kwargs):
        if renamed:
            raise OSError(errno.EIO, "Input/output error (stat)")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(path_utils.os, "rename", rename_fails)
    monkeypatch.setattr(path_utils.os, "stat", stat_fails_after)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        with pytest.raises(path_utils.PlacementCommitted) as committed:
            path_utils.place_without_clobbering(
                "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                identity_sensitive=True,
            )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert "rename" in str(committed.value.__cause__)
    assert "stat" in str(committed.value.__context__)
    assert (b / "Game.iso").read_bytes() == b"original"


def _rule_output(vol, library) -> str:
    """Where the ps2 Rule's conversion of Downloads/ps2/Game.iso writes."""
    from services.tools import registry

    tool = registry.for_mode("cso_compress")
    return tool.output_path(
        "cso_compress", str(vol / "Downloads" / "ps2" / "Game.iso"), str(vol / "ROMs" / "ps2"),
    )


@pytest.mark.asyncio
async def test_a_destination_only_locked_by_another_job_is_tried_again(vol, db, queue):
    """A destination name another job holds right
    now, with nothing on disk, is a retry: no Finding, the file isn't marked
    done, and the next poll queues it once the lock is gone."""
    from services.lock_manager import lock_manager

    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    (vol / "ROMs" / "ps2").mkdir(parents=True)
    output = _rule_output(vol, library)
    _drop(vol / "Downloads" / "ps2" / "Game.iso")
    assert lock_manager.acquire_lock(output)
    try:
        await _poll(box, now=0)
        busy = await _poll(box, now=61)
    finally:
        lock_manager.release_lock(output)

    assert busy["queued"] == busy["held"] == 0
    assert _inbox_findings() == []
    assert not os.path.exists(output)

    again = await _poll(box, now=62)

    assert again["queued"] == 1
    assert queue.submitted[0]["output_paths"] == {
        str(vol / "Downloads" / "ps2" / "Game.iso"): output,
    }


@pytest.mark.asyncio
async def test_a_destination_already_on_disk_stays_taken(vol, db, queue):
    """only a real output on disk is already taken: held
    with a Finding, never queued."""
    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    output = _rule_output(vol, library)
    _drop(Path(output), b"the Library's own")
    _drop(vol / "Downloads" / "ps2" / "Game.iso")

    await _poll(box, now=0)
    result = await _poll(box, now=61)

    assert result["held"] == 1 and result["queued"] == 0
    [finding] = _inbox_findings()
    assert "already in the Library" in finding["reason"]
    assert Path(output).read_bytes() == b"the Library's own"


# ---- round twelve: CodeRabbit on 93bda823 --------------------------------


def test_a_move_whose_landing_cant_be_checked_says_it_is_committed(tmp_path, monkeypatch):
    """CodeRabbit outside-diff note (path_utils move_anchored): the move
    landed, then the check of what landed fails. The file is at its new name
    and its old name gone, so it is PlacementCommitted, the failure chained,
    never a plain error a caller would take for nothing moved."""
    from utils import path_utils

    a, b = _two_folders(tmp_path)
    real_stat_at = path_utils.stat_at
    calls = []

    def fails_after_the_move(name, folder):
        calls.append(name)
        if len(calls) == 2:
            raise OSError(errno.EIO, "Input/output error (stat)")
        return real_stat_at(name, folder)

    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        expected = path_utils.file_snapshot(real_stat_at("Game.iso", src_fd))
        monkeypatch.setattr(path_utils, "stat_at", fails_after_the_move)
        with pytest.raises(path_utils.PlacementCommitted) as committed:
            path_utils.move_anchored("Game.iso", src_fd, dst_fd, expected)
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert "stat" in str(committed.value.__cause__)
    assert (b / "Game.iso").read_bytes() == b"original"
    assert not (a / "Game.iso").exists()


@pytest.mark.asyncio
async def test_a_removal_cancelled_before_its_worker_starts_lets_go_of_the_lock(
    vol, db, monkeypatch,
):
    """The registration lock is let go of by
    the delete's worker once it has started, else by the request: a removal
    cancelled while waiting for a worker leaves no lock behind."""
    import asyncio

    from services import libraries

    library = _library(vol)
    waiting = asyncio.Event()

    async def no_worker_free(func, *args, **kwargs):
        waiting.set()
        await asyncio.sleep(30)  # waiting for a threadpool worker

    monkeypatch.setattr(libraries, "run_in_threadpool", no_worker_free)
    removal = asyncio.ensure_future(libraries.remove(library.id))
    await asyncio.wait_for(waiting.wait(), 5)
    removal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await removal

    assert libraries._register_lock.acquire(blocking=False)  # not left held
    libraries._register_lock.release()
    (vol / "More").mkdir()
    assert libraries.register(str(vol / "More")) is not None


def test_a_take_that_left_both_names_is_put_back_and_not_moved(vol, monkeypatch):
    """A PlacementCommitted that left both
    names (the Source never moved; the slot holds a second link to it) isn't
    a move. The second link is taken back once checked to be the same file,
    and take says it failed, so the swap fails cleanly."""
    from services import staging
    from utils import path_utils

    folder = vol / "ps2"
    folder.mkdir()
    source = folder / "Game.chd"
    source.write_bytes(b"the Source")
    slot = staging.open_slot(str(folder), parking=True)

    def linked_then_failed(name, target, *, src_dir_fd, dst_dir_fd, **_k):
        os.link(name, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        raise path_utils.PlacementCommitted(errno.EIO, "dst couldn't be taken back")

    monkeypatch.setattr(staging, "place_without_clobbering", linked_then_failed)
    try:
        with pytest.raises(OSError) as failed:
            slot.take("Game.chd", "Game.iso")
        assert not isinstance(failed.value, path_utils.PlacementCommitted)
        assert slot.contents() == []
    finally:
        slot.close()

    assert source.read_bytes() == b"the Source"


def test_a_take_that_moved_but_couldnt_clean_up_is_a_move(vol, monkeypatch):
    """The Source really moved and only the quarantine's cleanup failed: take
    answers with the parked path, as before."""
    from services import staging
    from utils import path_utils

    folder = vol / "ps2"
    folder.mkdir()
    (folder / "Game.chd").write_bytes(b"the Source")
    slot = staging.open_slot(str(folder), parking=True)

    def moved_then_failed(name, target, *, src_dir_fd, dst_dir_fd, **_k):
        os.rename(name, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        raise path_utils.PlacementCommitted(errno.EIO, "cleanup failed")

    monkeypatch.setattr(staging, "place_without_clobbering", moved_then_failed)
    try:
        parked = slot.take("Game.chd", "Game.iso")
        assert Path(parked).read_bytes() == b"the Source"
        assert slot.contents() == ["Game.iso"]
    finally:
        slot.keep()
    assert not (folder / "Game.chd").exists()


# ---- round thirteen: CodeRabbit on db9e530f ------------------------------


def test_a_cleanup_that_cant_stat_the_destination_itself_says_it_is_committed(
    tmp_path, monkeypatch,
):
    """The cleanup's stat of the destination (not
    the source) failing is PlacementCommitted too."""
    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)
    real_stat = os.stat
    renamed = []
    dst_fd_box = {}

    def rename_fails(*_a, **_k):
        renamed.append(True)
        raise OSError(errno.EIO, "Input/output error (rename)")

    def dst_stat_fails(path, *args, **kwargs):
        if renamed and kwargs.get("dir_fd") == dst_fd_box["fd"]:
            raise OSError(errno.EIO, "Input/output error (dst stat)")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(path_utils.os, "rename", rename_fails)
    monkeypatch.setattr(path_utils.os, "stat", dst_stat_fails)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    dst_fd_box["fd"] = dst_fd
    try:
        with pytest.raises(path_utils.PlacementCommitted) as committed:
            path_utils.place_without_clobbering(
                "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                identity_sensitive=True,
            )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert "rename" in str(committed.value.__cause__)
    assert "dst stat" in str(committed.value.__context__)


def test_a_source_replaced_before_a_failed_quarantine_leaves_the_original_committed(
    tmp_path, monkeypatch,
):
    """An Inbox writer replaces the source after
    the link, then the quarantine rename fails. The destination still holds
    the original (its only name now): PlacementCommitted, never a plain
    error, and neither file is touched."""
    from utils import path_utils

    monkeypatch.setattr(path_utils, "_rename_noreplace_at", lambda *_a: False)
    a, b = _two_folders(tmp_path)

    def replaced_then_fails(*_a, **_k):
        os.unlink(a / "Game.iso")
        (a / "Game.iso").write_bytes(b"the writer's")
        raise OSError(errno.EIO, "Input/output error (rename)")

    monkeypatch.setattr(path_utils.os, "rename", replaced_then_fails)
    src_fd, dst_fd = os.open(a, os.O_RDONLY), os.open(b, os.O_RDONLY)
    try:
        with pytest.raises(path_utils.PlacementCommitted):
            path_utils.place_without_clobbering(
                "Game.iso", "Game.iso", src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                identity_sensitive=True,
            )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    assert (b / "Game.iso").read_bytes() == b"original"
    assert (a / "Game.iso").read_bytes() == b"the writer's"


@pytest.mark.asyncio
async def test_a_committed_move_whose_name_is_taken_again_is_kept_and_reported(
    vol, db, queue, monkeypatch,
):
    """A move to processed/ that committed while
    the Inbox name was taken by a replacement is never moved back over it:
    the original stays in processed/ (its only name), and the Finding names
    both paths."""
    from services.inbox import intake
    from utils import path_utils

    library = _library(vol)
    await _ps2_rule(library)
    box = _inbox(vol, library)
    game = await _converted(vol, queue, box)
    real_move = intake.move_anchored
    calls = []

    def second_commits_after_a_writer(name, src, dst, expected, **kwargs):
        calls.append(name)
        real_move(name, src, dst, expected, **kwargs)
        if name == "Game.iso":
            # A writer puts a new file under the name just vacated, then
            # something fails after the move landed.
            game.write_bytes(b"the writer's")
            raise path_utils.PlacementCommitted(errno.EIO, "cleanup failed")

    monkeypatch.setattr(intake, "move_anchored", second_commits_after_a_writer)

    await _poll(box, now=122)

    processed = vol / "Downloads" / "processed" / "ps2" / "Game.iso"
    assert processed.read_bytes() == DISC  # the original, kept there
    assert game.read_bytes() == b"the writer's"  # the replacement, untouched
    [finding] = [f for f in _inbox_findings() if f["kind"] == findings.INBOX_FAILED]
    assert str(processed) in finding["reason"]
    assert str(game) in finding["reason"]


# ---- round thirteen: Codex on db9e530f -----------------------------------


def _oversized_sheet(track: str) -> bytes:
    from utils.sheet_text import MAX_SHEET_BYTES

    return (
        f'FILE "{track}" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'.encode()
        + b"REM " + b"x" * MAX_SHEET_BYTES + b"\n"
    )


@pytest.mark.asyncio
async def test_a_track_below_an_unreadable_sheet_is_held_too(vol, db):
    """A sheet that can't be read holds every loose
    track-type file in its folder and every folder below it."""
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    _drop(vol / "Downloads" / "psx" / "Game.cue", _oversized_sheet("tracks/Track.bin"))
    track = _drop(vol / "Downloads" / "psx" / "tracks" / "Track.bin", b"track")

    for now in (0, 1, 2):
        await _poll(box, now=now)

    assert track.read_bytes() == b"track"
    assert not (vol / "ROMs" / "psx").exists()
    held = {Path(f["path"]).name: f["reason"] for f in _inbox_findings()}
    assert "Game.cue" in held["Track.bin"]


@pytest.mark.asyncio
async def test_a_sheet_hold_lifts_once_the_other_sheet_is_gone(vol, db):
    """An ownership hold remembers the sheets that
    caused it; once B.cue is removed, A.cue is taken without waiting to
    change, and its stale Finding goes."""
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    sheet = b'FILE "Track.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
    _drop(vol / "Downloads" / "psx" / "A.cue", sheet)
    b = _drop(vol / "Downloads" / "psx" / "B.cue", sheet)
    _drop(vol / "Downloads" / "psx" / "Track.bin", b"track")
    for now in (0, 1, 2):
        await _poll(box, now=now)
    assert {Path(f["path"]).name for f in _inbox_findings()} == {"A.cue", "B.cue"}

    b.unlink()
    for now in (3, 4):
        await _poll(box, now=now)

    assert (vol / "ROMs" / "psx" / "A.cue").exists()
    assert (vol / "ROMs" / "psx" / "Track.bin").exists()
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_a_track_held_by_an_unreadable_sheet_is_taken_once_it_is_gone(vol, db):
    """Once the unreadable sheet is removed, the
    track it held is taken on its own and the stale Finding goes."""
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    cue = _drop(vol / "Downloads" / "psx" / "Game.cue", _oversized_sheet("Track.bin"))
    _drop(vol / "Downloads" / "psx" / "Track.bin", b"track")
    for now in (0, 1, 2):
        await _poll(box, now=now)
    assert any(Path(f["path"]).name == "Track.bin" for f in _inbox_findings())

    cue.unlink()
    for now in (3, 4):
        await _poll(box, now=now)

    assert (vol / "ROMs" / "psx" / "Track.bin").exists()
    assert not any(Path(f["path"]).name == "Track.bin" for f in _inbox_findings())


@pytest.mark.asyncio
async def test_archival_waits_for_a_job_reading_the_original(vol, db):
    """The originals are moved to processed/ only
    under the lock manager's exclusive locks; while another job reads one
    (its input lock), archival is deferred, with no Finding and nothing
    moved, and the next poll after the lock goes moves it."""
    from services.lock_manager import lock_manager

    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    game = _drop(vol / "Downloads" / "ps2" / "Game.iso")
    assert lock_manager.acquire_input_lock(str(game))
    try:
        for now in (0, 1, 2):
            await _poll(box, now=now)
        assert game.exists()
        assert (vol / "ROMs" / "ps2" / "Game.iso").exists()  # placed
        assert _inbox_findings() == []
    finally:
        lock_manager.release_input_lock(str(game))

    await _poll(box, now=3)

    assert not game.exists()
    assert (vol / "Downloads" / "processed" / "ps2" / "Game.iso").exists()
    assert not lock_manager.is_locked(str(game))


@pytest.mark.asyncio
async def test_a_slow_but_moving_listing_isnt_cut_off(vol, db, monkeypatch):
    """The listing reports progress between Images
    through grouping, fingerprinting and the ownership checks, so its stall
    bound measures stalled storage, not the whole pass."""
    import time as clock

    from services import inbox
    from services.inbox import intake

    library = _library(vol)
    box = _inbox(vol, library)
    for n in range(6):
        _drop(vol / "Downloads" / "ps2" / f"Game{n}.iso")
    monkeypatch.setattr(intake, "_LIST_STALL_SECONDS", 0.3)
    real = intake.image_fingerprint

    def quick_but_many(path):
        clock.sleep(0.1)  # each answers in time; all six take 0.6s
        return real(path)

    monkeypatch.setattr(intake, "image_fingerprint", quick_but_many)

    result = await intake.poll(inbox.get(box.id), now=0)

    assert result["error"] is None
    assert result["waiting"] == 6


@pytest.mark.asyncio
async def test_a_track_named_by_an_absolute_path_is_held_by_its_sheet(vol, db):
    """A readable sheet whose track list doesn't
    fully resolve (here, an absolute reference to the track beside it, which
    ``image_members`` drops) is an unknown owner, like an unreadable sheet:
    the loose track stays put, held with a Finding naming the sheet."""
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    track = _drop(vol / "Downloads" / "psx" / "Track.bin", b"track")
    _drop(
        vol / "Downloads" / "psx" / "Game.cue",
        f'FILE "{track}" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'.encode(),
    )

    for now in (0, 1):
        await _poll(box, now=now)

    assert track.read_bytes() == b"track"
    assert not (vol / "ROMs" / "psx" / "Track.bin").exists()
    held = {Path(f["path"]).name: f["reason"] for f in _inbox_findings()}
    assert "Game.cue" in held["Track.bin"]


def _rom_zip(path: Path, data: bytes, *, compression: int) -> Path:
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        zf.writestr("Game.gba", data)
    return path


def _verifies(monkeypatch) -> list[str]:
    """Every Verify intake runs, recorded and answered Intact (7z never runs)."""
    from services.rules import check as checks
    from services.tools import registry

    monkeypatch.setattr(registry, "check_tool", lambda *_a, **_k: SimpleNamespace(id="fake"))
    verified: list[str] = []

    async def verify(check, cancel_event):
        verified.append(check.path)
        return checks.INTACT, "", False

    monkeypatch.setattr(checks, "_verify", verify)
    return verified


@pytest.mark.asyncio
async def test_an_archive_declaring_more_than_the_cap_is_held_unverified(
    vol, db, monkeypatch,
):
    """An archive whose declared uncompressed
    size is over the Inbox cap is held with a Finding naming the limit, and is
    neither verified nor copied."""
    import zipfile

    verified = _verifies(monkeypatch)
    monkeypatch.setattr(settings, "archive_max_total_size", 0)
    monkeypatch.setattr(settings, "archive_inbox_max_total_size", 1024)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    bomb = _rom_zip(
        vol / "Downloads" / "gba" / "Game.zip", os.urandom(4096),
        compression=zipfile.ZIP_STORED,
    )

    for now in (0, 1):
        await _poll(box, now=now)

    assert verified == []
    assert bomb.exists()
    assert not (vol / "ROMs" / "gba").exists()
    [finding] = _inbox_findings()
    assert "CHD_ARCHIVE_INBOX_MAX_TOTAL_SIZE" in finding["reason"]


@pytest.mark.asyncio
async def test_an_archive_over_the_ratio_cap_is_held(vol, db, monkeypatch):
    """an archive that expands more than the Inbox ratio cap
    (200:1 by default) is held, naming the limit."""
    import zipfile

    verified = _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    bomb = _rom_zip(
        vol / "Downloads" / "gba" / "Game.zip", b"\0" * (4 * 1024 * 1024),
        compression=zipfile.ZIP_DEFLATED,
    )

    for now in (0, 1):
        await _poll(box, now=now)

    assert verified == []
    assert bomb.exists()
    [finding] = _inbox_findings()
    assert "CHD_ARCHIVE_INBOX_MAX_RATIO" in finding["reason"]


@pytest.mark.asyncio
async def test_a_normal_rom_zip_is_unpacked_and_taken(vol, db, monkeypatch):
    """an ordinary ROM zip is within every cap. Since #432
    it is unpacked: the ROM in it is checked and copied in, and the zip moves
    to processed/."""
    import zipfile

    verified = _verifies(monkeypatch)
    library = _library(vol)
    box = _inbox(vol, library, settle=0)
    rom = _rom_zip(
        vol / "Downloads" / "gba" / "Game.zip", os.urandom(64 * 1024),
        compression=zipfile.ZIP_DEFLATED,
    )

    for now in range(6):
        await _poll(box, now=now)

    assert [Path(p).name for p in verified] == ["Game.gba"]
    assert (vol / "ROMs" / "gba" / "Game.gba").exists()
    assert not rom.exists()
    assert (vol / "Downloads" / "processed" / "gba" / "Game.zip").exists()
    assert _inbox_findings() == []


@pytest.mark.asyncio
async def test_an_archives_verify_bound_comes_from_its_declared_size(vol, monkeypatch):
    """`7z t`'s bound is sized by what it
    decompresses, the members' declared sizes, never the archive's own
    (small) size."""
    import zipfile

    from services.romz import romz_service

    archive = _rom_zip(
        vol / "Downloads" / "gba" / "Game.zip", b"\0" * (256 * 1024),
        compression=zipfile.ZIP_DEFLATED,
    )
    sized = {}

    async def capture(cmd, *, path, size=None, **kwargs):
        sized["size"] = size
        yield {"type": "complete", "valid": True, "message": "File is Intact"}

    monkeypatch.setattr(romz_service._runner, "capture_verify", capture)

    async for _event in romz_service.verify_stream(str(archive)):
        pass

    assert sized["size"] == 256 * 1024
    assert archive.stat().st_size < 256 * 1024
