"""Round-trip check, first tool: Dolphin (#298).

A Dolphin job with delete-source on deletes its Source only when the content
the output decodes to equals the content the Source decodes to. dolphin-tool is
faked at the ``SubprocessRunner.run_capture`` seam, so these tests need no real
binary; the Source ISO side is a real SHA1 of a real temp file.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from services.tools import registry
from services.tools.base import ContentDigest, ContentDigestUnavailable

DOLPHIN_MODES = ("dolphin_rvz", "dolphin_wia", "dolphin_gcz", "dolphin_iso")


# Kept in step with test_round_trip_301 as each tool gains its digest.
ROUND_TRIP_301 = {
    "cso_compress", "cso2_compress", "zso_compress", "dax_compress",
    "z3ds_compress", "jwud_compress", "nsz_compress", "romz_7z", "romz_zip",
    "cso_to_chd", "createhd", "createraw", "copy",
    "rar_extract",
}


def _dolphin():
    return registry.get("dolphin")


def _fake_capture(monkeypatch, *, rc=0, stdout=b"", calls=None):
    """Replace dolphin-tool's one-shot capture; record each command."""
    service = _dolphin()._service

    async def fake(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        return rc, stdout, b""

    monkeypatch.setattr(service._runner, "run_capture", fake)


@pytest.mark.asyncio
async def test_iso_digest_is_the_file_sha1_without_spawning(tmp_path, monkeypatch):
    calls: list = []
    _fake_capture(monkeypatch, calls=calls)
    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"disc bytes" * 1000)

    digest = await _dolphin().content_digest(str(iso))

    assert digest == ContentDigest(
        kind="gc_wii_disc_sha1",
        value=hashlib.sha1(iso.read_bytes()).hexdigest(),
    )
    assert calls == []


@pytest.mark.asyncio
async def test_container_digest_comes_from_dolphin_tool_verify(tmp_path, monkeypatch):
    calls: list = []
    sha1 = "ab" * 20
    # The real format with --algorithm: the bare hash on one line.
    _fake_capture(monkeypatch, stdout=f"{sha1.upper()}\n".encode(), calls=calls)
    rvz = tmp_path / "Game.rvz"
    rvz.write_bytes(b"x")

    digest = await _dolphin().content_digest(str(rvz))

    assert digest == ContentDigest(kind="gc_wii_disc_sha1", value=sha1)
    assert len(calls) == 1
    assert calls[0][-4:] == ["-i", str(rvz), "--algorithm", "sha1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout", [
    f"Problems: none\n{'ab' * 20}\n",
    f"SHA-1: {'ab' * 20}\n",
    f"{'ab' * 20}\n{'ab' * 20}\n",
    f"{'ab' * 20} extra\n",
])
async def test_anything_but_one_bare_hash_line_is_unavailable(
    tmp_path, monkeypatch, stdout,
):
    # The delete gate accepts only the exact shape dolphin-tool prints with
    # --algorithm sha1; stray text around a hash is not a digest.
    _fake_capture(monkeypatch, stdout=stdout.encode())
    rvz = tmp_path / "Game.rvz"
    rvz.write_bytes(b"x")

    with pytest.raises(ContentDigestUnavailable):
        await _dolphin().content_digest(str(rvz))


@pytest.mark.asyncio
async def test_failed_dolphin_tool_verify_is_unavailable(tmp_path, monkeypatch):
    _fake_capture(monkeypatch, rc=1)
    rvz = tmp_path / "Game.rvz"
    rvz.write_bytes(b"x")

    with pytest.raises(ContentDigestUnavailable):
        await _dolphin().content_digest(str(rvz))


@pytest.mark.asyncio
async def test_a_file_dolphin_does_not_read_has_no_digest(tmp_path):
    other = tmp_path / "Game.chd"
    other.write_bytes(b"x")
    assert await _dolphin().content_digest(str(other)) is None


def test_every_dolphin_mode_declares_round_trip():
    for mode in DOLPHIN_MODES:
        assert registry.mode_supports_round_trip(mode), mode


def test_modes_without_a_content_digest_do_not_declare_round_trip():
    # Dolphin (#298), chdman's CD/DVD create modes (#300) and the rest of the
    # delete-source modes (#301) have a content digest on both sides; every
    # other mode keeps the container check.
    with_digest = set(DOLPHIN_MODES) | {"createcd", "createdvd"} | ROUND_TRIP_301
    for spec in registry.mode_specs():
        if spec.mode not in with_digest:
            assert not registry.mode_supports_round_trip(spec.mode), spec.mode


def test_digests_of_different_kinds_are_never_equal():
    a = ContentDigest(kind="gc_wii_disc_sha1", value="ab" * 20)
    b = ContentDigest(kind="cd_track_sha1", value="ab" * 20)
    with pytest.raises(ValueError):
        a.same_content(b)
    assert a.same_content(ContentDigest(kind="gc_wii_disc_sha1", value="ab" * 20))
    assert not a.same_content(ContentDigest(kind="gc_wii_disc_sha1", value="cd" * 20))


# ---------------------------------------------------------------------------
# The stored verification says which check passed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stored_verification_records_which_check_passed(db_path, reset_db_engine):
    from services import db as _db
    from services.verification_store import verification_store

    _db.init_engine(db_path)
    try:
        await verification_store.mark_verified("/data/gc/a.rvz", check="round_tripped")
        await verification_store.mark_verified("/data/gc/b.rvz")
        a = await verification_store.get_record("/data/gc/a.rvz")
        b = await verification_store.get_record("/data/gc/b.rvz")
        assert a["check_passed"] == "round_tripped"
        # A manual Verify (and every caller that names no check) is Intact.
        assert b["check_passed"] == "intact"
    finally:
        reset_db_engine()


@pytest.mark.asyncio
async def test_verified_listing_says_which_check_each_output_passed(
    tmp_path, monkeypatch, db_path, reset_db_engine,
):
    from routes import info as info_routes
    from services import db as _db
    from services.verification_store import verification_store

    _db.init_engine(db_path)
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    rt, intact = tmp_path / "a.rvz", tmp_path / "b.chd"
    rt.write_bytes(b"a")
    intact.write_bytes(b"b")
    try:
        await verification_store.mark_verified(str(rt), check="round_tripped")
        await verification_store.mark_verified(str(intact))
        listing = await info_routes.list_verified()
    finally:
        reset_db_engine()

    assert sorted(listing["verified"]) == sorted([str(rt), str(intact)])
    assert listing["checks"] == {str(rt): "round_tripped", str(intact): "intact"}


def test_migration_backfills_existing_verifications_as_intact(db_path, reset_db_engine):
    from alembic import command
    from services import db as _db
    from sqlalchemy import text

    engine = _db.init_engine(db_path, create_schema=False)
    try:
        cfg = _db._alembic_config(engine)
        with engine.connect() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "0009")
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO verifications (chd_path, source_path, verified_at) "
                "VALUES ('/data/old.chd', NULL, '2026-01-01T00:00:00Z')"
            ))

        _db.apply_migrations()

        with engine.connect() as conn:
            value = conn.execute(text(
                "SELECT check_passed FROM verifications WHERE chd_path = '/data/old.chd'"
            )).scalar_one()
        assert value == "intact"
    finally:
        reset_db_engine()


# ---------------------------------------------------------------------------
# End to end: POST /jobs -> plan_job -> job_manager -> delete decision
# ---------------------------------------------------------------------------


@pytest.fixture(name="e2e")
def _e2e(tmp_path, monkeypatch, db_path, reset_db_engine):
    """Confine the route to ``tmp_path``; stub the converters and their
    container checks; fake dolphin-tool's hash run at the runner seam."""
    from services import db as _db

    from app.routes import convert as convert_routes

    _db.init_engine(db_path)
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)

    converts: list[str] = []

    async def fake_convert(input_path, output_path, mode="", *, compression=None,
                           block_size=None, cancel_event=None):
        converts.append(output_path)
        Path(output_path).write_bytes(b"converted output")
        yield {"progress": 100, "message": "done"}

    async def container_check_passes(path, *, cancel_event=None):
        return {"valid": True, "message": "ok"}

    for tool_id in ("dolphin", "z3ds"):
        service = registry.get(tool_id)._service
        monkeypatch.setattr(service, "convert", fake_convert)
        monkeypatch.setattr(service, "verify", container_check_passes)

    state = {"stdout": b"", "rc": 0, "calls": [], "converts": converts}
    service = _dolphin()._service

    async def fake_capture(cmd, **kwargs):
        state["calls"].append(cmd)
        return state["rc"], state["stdout"], b""

    monkeypatch.setattr(service._runner, "run_capture", fake_capture)
    state["tmp_path"] = tmp_path
    state["routes"] = convert_routes
    yield state
    reset_db_engine()


async def _run(e2e, source, mode, **request_fields):
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest

    routes = e2e["routes"]
    request = JobCreateRequest(
        file_path=str(source),
        mode=ConversionMode(mode),
        duplicate_action=DuplicateAction.SKIP,
        **request_fields,
    )
    job = await routes.create_job(request)
    await routes.job_manager._process_job(job.id)
    return job


def _source_iso(e2e, payload=b"the disc" * 4096):
    iso = e2e["tmp_path"] / "Game.iso"
    iso.write_bytes(payload)
    return iso, hashlib.sha1(payload).hexdigest()


@pytest.mark.asyncio
async def test_matching_digests_delete_the_source_and_say_round_tripped(e2e):
    from services import ledger
    from services.verification_store import verification_store

    iso, sha1 = _source_iso(e2e)
    e2e["stdout"] = f"{sha1}\n".encode()

    job = await _run(e2e, iso, "dolphin_rvz", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert not iso.exists()
    assert job.check_passed == "round_tripped"
    assert ledger.row_from_job(job)["check_passed"] == "round_tripped"
    record = await verification_store.get_record(job.output_path)
    assert record["check_passed"] == "round_tripped"
    # Only the output went through dolphin-tool; the Source ISO is hashed directly.
    assert [c[c.index("-i") + 1] for c in e2e["calls"]] == [job.output_path]


@pytest.mark.asyncio
async def test_mismatched_digests_keep_the_source_and_say_the_content_differs(e2e):
    from services import ledger

    iso, _ = _source_iso(e2e)
    e2e["stdout"] = f"{'0' * 40}\n".encode()

    job = await _run(e2e, iso, "dolphin_rvz", delete_on_verify=True)

    assert job.status.value == "failed"
    assert iso.exists()
    assert "content differs" in job.error_message
    assert "Source kept" in job.error_message
    # The output did pass its container check, and that is all it passed.
    assert job.check_passed == "intact"
    row = ledger.row_from_job(job)
    assert row["check_passed"] == "intact" and row["source_deleted"] is False


@pytest.mark.asyncio
async def test_a_mismatched_output_is_not_stored_as_a_checked_result(e2e):
    """A Round-trip mismatch must not leave the output recorded as checked:
    a later keep-Source overwrite job re-converts instead of reusing it."""
    from services.verification_store import verification_store

    from app.models import ConversionMode

    iso, _ = _source_iso(e2e)
    e2e["stdout"] = f"{'0' * 40}\n".encode()
    failed = await _run(e2e, iso, "dolphin_rvz", delete_on_verify=True)
    assert failed.status.value == "failed"
    assert await verification_store.get_record(failed.output_path) is None

    manager = e2e["routes"].job_manager
    again = await manager.create_job(
        str(iso), ConversionMode.DOLPHIN_RVZ,
        output_path=failed.output_path, allow_overwrite=True, verify_after=True,
    )
    await manager._process_job(again.id)

    assert again.status.value == "completed", again.error_message
    assert "skipped re-conversion" not in again.message
    assert e2e["converts"] == [failed.output_path, failed.output_path]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stdout", "rc"), [
    (f"{'0' * 40}\n".encode(), 0),   # mismatch
    (b"", 1),                        # unreadable digest
])
async def test_a_failed_round_trip_drops_a_record_left_by_an_earlier_run(
    e2e, stdout, rc,
):
    """No overwrite here, so nothing else clears the store first: the
    Round-trip failure itself must drop the earlier record."""
    from services.verification_store import verification_store

    iso, _ = _source_iso(e2e)
    output = iso.with_suffix(".rvz")
    await verification_store.mark_verified(
        str(output), source_path=str(iso), produced_meta={"mode": "dolphin_rvz"},
        check="round_tripped",
    )
    e2e["stdout"], e2e["rc"] = stdout, rc

    job = await _run(e2e, iso, "dolphin_rvz", delete_on_verify=True)

    assert job.status.value == "failed"
    assert job.output_path == str(output)
    assert iso.exists()
    assert await verification_store.get_record(str(output)) is None


@pytest.mark.asyncio
async def test_a_no_op_rerun_reports_the_stored_check(e2e, monkeypatch):
    """Round-tripped, then the delete itself is refused: the output's record
    stays round_tripped. A keep-Source re-run takes the no-op fast path, and
    its completion event must carry that stored check, not imply Intact."""
    from app.models import ConversionMode

    iso, sha1 = _source_iso(e2e)
    e2e["stdout"] = f"{sha1}\n".encode()
    manager = e2e["routes"].job_manager
    # The delete step refuses (another job holds the Source) after the
    # Round-trip passed; the Source itself is untouched, so the reuse
    # snapshot is recorded.
    monkeypatch.setattr(manager, "_is_path_in_use_by_other_job", lambda *_: True)
    first = await _run(e2e, iso, "dolphin_rvz", delete_on_verify=True)
    assert first.status.value == "failed" and iso.exists()
    assert "in use by another job" in first.error_message
    assert first.check_passed == "round_tripped"
    monkeypatch.setattr(manager, "_is_path_in_use_by_other_job", lambda *_: False)

    events: list[dict] = []
    original = manager._notify_subscribers

    async def capture(job_id, payload):
        events.append(payload)
        return await original(job_id, payload)

    monkeypatch.setattr(manager, "_notify_subscribers", capture)
    again = await manager.create_job(
        str(iso), ConversionMode.DOLPHIN_RVZ,
        output_path=first.output_path, allow_overwrite=True, verify_after=True,
    )
    await manager._process_job(again.id)

    assert "skipped re-conversion" in again.message, (again.status, again.error_message)
    complete = next(e for e in events if e.get("type") == "complete")
    assert complete["check_passed"] == "round_tripped"


@pytest.mark.asyncio
async def test_a_digest_that_cannot_be_read_keeps_the_source(e2e):
    iso, _ = _source_iso(e2e)
    e2e["rc"] = 1

    job = await _run(e2e, iso, "dolphin_rvz", delete_on_verify=True)

    assert job.status.value == "failed"
    assert iso.exists()
    assert "Round-trip" in job.error_message and "Source kept" in job.error_message


@pytest.mark.asyncio
async def test_cancel_during_the_comparison_keeps_the_source_without_a_verdict(
    e2e, monkeypatch,
):
    from utils.delete_plan import build_delete_snapshot

    from app.models import ConversionMode

    iso, _ = _source_iso(e2e)
    manager = e2e["routes"].job_manager
    service = _dolphin()._service

    async def cancel_mid_hash(cmd, **kwargs):
        # Cancel lands while dolphin-tool is hashing the output; the runner
        # reports the abort as a None returncode.
        manager._cancel_events[job.id].set()
        return None, b"", b""

    monkeypatch.setattr(service._runner, "run_capture", cancel_mid_hash)
    job = await manager.create_job(
        str(iso), ConversionMode.DOLPHIN_RVZ,
        output_path=str(iso.with_suffix(".rvz")),
        delete_on_verify=True,
        delete_snapshot=build_delete_snapshot(str(iso)),
    )
    await manager._process_job(job.id)

    assert job.status.value == "cancelled"
    assert iso.exists()
    assert job.error_message is None
    # The container check did pass before Cancel; the Round-trip gave no
    # verdict, so the record says Intact, never Round-tripped or none.
    from services import ledger
    assert job.check_passed == "intact"
    assert ledger.row_from_job(job)["check_passed"] == "intact"


@pytest.mark.asyncio
async def test_container_only_opt_out_skips_the_comparison(e2e):
    iso, _ = _source_iso(e2e)

    job = await _run(
        e2e, iso, "dolphin_rvz", delete_on_verify=True, container_only=True,
    )

    assert job.status.value == "completed", job.error_message
    assert not iso.exists()
    assert job.container_only is True
    assert job.check_passed == "intact"
    assert e2e["calls"] == []


@pytest.mark.asyncio
async def test_a_mode_without_a_digest_keeps_the_container_check(e2e, monkeypatch):
    rom = e2e["tmp_path"] / "Game.3ds"
    rom.write_bytes(b"cartridge")
    # z3ds Round-trips since #301; a mode with no digest stands in for it.
    monkeypatch.setattr(
        registry, "mode_supports_round_trip", lambda mode: mode != "z3ds_compress",
    )

    job = await _run(e2e, rom, "z3ds_compress", delete_on_verify=True)

    assert job.status.value == "completed", job.error_message
    assert not rom.exists()
    assert job.check_passed == "intact"


@pytest.mark.asyncio
async def test_a_job_that_keeps_its_source_runs_only_the_intact_check(e2e, monkeypatch):
    from app.models import ConversionMode

    iso, _ = _source_iso(e2e)
    reads: list[str] = []
    tool = _dolphin()
    original = tool.content_digest

    async def counting(path, *, cancel_event=None):
        reads.append(path)
        return await original(path, cancel_event=cancel_event)

    monkeypatch.setattr(tool, "content_digest", counting)
    manager = e2e["routes"].job_manager
    job = await manager.create_job(
        str(iso), ConversionMode.DOLPHIN_RVZ,
        output_path=str(iso.with_suffix(".rvz")), verify_after=True,
    )
    await manager._process_job(job.id)

    assert job.status.value == "completed", job.error_message
    assert iso.exists()
    assert job.check_passed == "intact"
    assert reads == [] and e2e["calls"] == []


@pytest.mark.asyncio
async def test_batch_route_carries_the_container_only_opt_out(e2e):
    from app.models import BatchJobCreateRequest, ConversionMode, DuplicateAction

    sources = []
    for name in ("A.iso", "B.iso"):
        path = e2e["tmp_path"] / name
        path.write_bytes(name.encode())
        sources.append(str(path))

    jobs = await e2e["routes"].create_batch_jobs(BatchJobCreateRequest(
        file_paths=sources, mode=ConversionMode.DOLPHIN_RVZ,
        duplicate_action=DuplicateAction.SKIP,
        delete_on_verify=True, container_only=True,
    ))
    for job in jobs:
        await e2e["routes"].job_manager._process_job(job.id)

    assert [j.container_only for j in jobs] == [True, True]
    assert [j.check_passed for j in jobs] == ["intact", "intact"]
    assert e2e["calls"] == []


@pytest.mark.asyncio
async def test_a_failed_round_trip_keeps_what_made_the_output(e2e):
    """A failed check keeps provenance (#305 ruling, on #306): after a
    Round-trip mismatch the output is not verified, but its record still
    names the mode that made it, so the next check goes to that tool."""
    from services.verification_store import verification_store

    iso, _ = _source_iso(e2e)
    e2e["stdout"] = f"{'0' * 40}\n".encode()

    job = await _run(e2e, iso, "dolphin_rvz", delete_on_verify=True)

    assert job.status.value == "failed"
    assert await verification_store.get_record(job.output_path) is None
    assert not await verification_store.is_verified(job.output_path)
    meta = verification_store.produced_meta_blocking(job.output_path)
    assert (meta or {}).get("mode") == "dolphin_rvz"


# ---------------------------------------------------------------------------
# within: where a tool may read a Source's sheet and tracks (#294 contract)
# ---------------------------------------------------------------------------


class _RecordingTool:
    """Records each content_digest call; both sides digest the same."""

    display_name = "Recorder"

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    async def verify_timeout(self, path, *, cancel_event=None):
        return 0

    async def source_problem(self, path, *, cancel_event=None, within=None):
        return None

    async def content_digest(self, path, *, cancel_event=None, within=None):
        self.calls.append((path, within))
        return ContentDigest(kind="k", value="v")


@pytest.mark.asyncio
async def test_round_trip_passes_within_to_the_source_side_only():
    from services.round_trip import round_trip

    tool = _RecordingTool()
    outcome = await round_trip(
        tool, "/scratch/job/disc.cue", "/vol/disc.chd", within=["/scratch/job"],
    )

    assert outcome.passed
    # The Source may live in the job's extraction folder; the output is
    # always in a Volume, so it gets the Volume default (None).
    assert tool.calls == [
        ("/scratch/job/disc.cue", ["/scratch/job"]),
        ("/vol/disc.chd", None),
    ]


@pytest.mark.asyncio
async def test_an_archive_member_source_is_digested_within_its_extraction_folder(
    e2e, monkeypatch,
):
    import os
    import zipfile

    from app.models import ConversionMode, DuplicateAction, JobCreateRequest

    tmp_path = e2e["tmp_path"]
    archive = tmp_path / "Game.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Game.iso", b"the disc" * 4096)
    e2e["stdout"] = f"{hashlib.sha1(b'the disc' * 4096).hexdigest()}\n".encode()

    seen: list[tuple[str, object]] = []
    tool = _dolphin()
    original = tool.content_digest

    async def recording(path, *, cancel_event=None, within=None):
        seen.append((path, within))
        return await original(path, cancel_event=cancel_event, within=within)

    monkeypatch.setattr(tool, "content_digest", recording)
    routes = e2e["routes"]
    job = await routes.create_job(JobCreateRequest(
        file_path=f"{archive}::Game.iso", mode=ConversionMode.DOLPHIN_RVZ,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    await routes.job_manager._process_job(job.id)

    assert job.status.value == "completed", job.error_message
    (source_path, source_within), (output_path, output_within) = seen
    assert source_within == [os.path.dirname(source_path)]
    assert output_path == job.output_path and output_within is None
