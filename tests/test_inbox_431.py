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

import os
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
def _db_fixture(tmp_path: Path):
    from services import inbox, libraries
    from services.inbox import intake
    from services.romm import settings as romm_settings

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
    for kind in ("inbox_unsorted", "inbox_conflict", "inbox_failed"):
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
    """Codex 4603069915: the move to processed/ is anchored to the Inbox, so a
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
    """Codex 4603071785: the Library holds the old contents, so a replaced
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
    """Codex 4603071794: Game.iso.0 and .1 are one Image, listed by its parts."""
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
    """Codex 4603071801: the held paths live in memory; the stored Findings are
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
    """Codex 4603071815: Inbox and Library saves share one registration lock,
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
    """Codex 4603071827: whether the Rule's mode takes the file can read its
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
    """Codex 4603071837: the Intact check runs where the mode supports it
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
