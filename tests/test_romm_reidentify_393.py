"""Skip the metadata re-pin where RomM re-identifies a format itself (#393).

Spike #382 found that RomM 5.4.1, with title-id extraction on, moves a
missing ROM onto its converted file by title id for PS2 CHD, PSP CHD, PSP CSO
and GameCube RVZ, keeping the id and every piece of user data. It doesn't for
PS2 CSO/ZSO, PSP ZSO or GameCube WIA/GCZ. With the swap switch off, a
conversion whose (Platform, output format) is on that list for the connected
RomM's version skips its re-pin, and the job and the Ledger say why. With the
swap on, the swap runs as before; an unknown version, a format not on the
list, or a heartbeat that can't be read keeps today's re-pin.

The jobs run the real pipeline (convert route -> planner -> job manager) on a
temp Volume against :class:`tests.fake_romm.FakeRomm`; only the tool's convert,
post-convert and verify, and the Round-trip comparison, are stubbed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from services import db as _db
from services import job_manager as job_manager_module
from services.round_trip import RoundTripOutcome

from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
from app.routes import convert as convert_routes

from .fake_romm import FakeRomm

PS2 = {"id": 3, "slug": "ps2", "fs_slug": "ps2", "name": "PlayStation 2"}
PSP = {"id": 5, "slug": "psp", "fs_slug": "psp", "name": "PlayStation Portable"}
SOURCE_BYTES = b"ISO-" * 1024
OUTPUT_BYTES = b"CSO-output"
ROM_ID = 21
REIDENTIFY = "romm_reidentify"


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    """A Volume holding RomM's library (``romm/psp`` and ``romm/ps2``, each
    with a ``Game.iso``) and a database."""
    from services import libraries
    from services.romm import settings as romm_settings

    vol = tmp_path / "vol"
    for slug in ("psp", "ps2"):
        (vol / "romm" / slug).mkdir(parents=True)
        (vol / "romm" / slug / "Game.iso").write_bytes(SOURCE_BYTES)
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(vol))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(vol))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "data_dir", str(tmp_path / "config"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    yield vol
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest_asyncio.fixture(name="romm")
async def _romm(vol, monkeypatch):
    """A RomM 5.4.1 with title-id extraction on, holding ROM 21 for
    ``psp/Game.iso`` and ROM 22 for ``ps2/Game.iso``, with the swap off; the
    job manager's terminal listeners as the server registers them."""
    from services import ledger, libraries
    from services.romm import settings as romm_settings

    fake = FakeRomm(
        version="5.4.1", title_id_extraction=True,
        platforms=[PSP, PS2], library=vol / "romm",
    ).start()
    fake.add_rom(ROM_ID, PSP, "Game.iso", igdb_id=4242)
    fake.add_rom(ROM_ID + 1, PS2, "Game.iso", igdb_id=4343)
    await romm_settings.save({
        "url": fake.url, "token": "rmm_x", "library_root": str(vol / "romm"),
        "swap_keep_rom_id": False,
    })
    _stub_tools(monkeypatch)
    manager = convert_routes.job_manager
    manager.add_terminal_listener(ledger.record)
    libraries.attach(manager)
    yield fake
    manager.remove_terminal_listener(ledger.record)
    manager.remove_terminal_listener(libraries.note_job_finished)
    from services.romm import reidentify

    manager.remove_terminal_listener(reidentify.note_job_finished)
    fake.stop()


def _stub_tools(monkeypatch) -> None:
    async def fake_convert(input_path, output_path, mode, *, compression=None,
                           split=False, cancel_event=None):
        Path(output_path).write_bytes(OUTPUT_BYTES)
        yield {"progress": 100, "message": "done"}

    async def noop_post_convert(input_path, output_path, mode, *, within=None):
        return None

    async def fake_verify(path, *, cancel_event=None):
        return {"valid": True, "message": "ok"}

    async def fake_round_trip(tool, source_path, output_path, **_kw):
        return RoundTripOutcome(passed=True)

    for tool in convert_routes.registry.all():
        monkeypatch.setitem(vars(tool), "convert", fake_convert)
        monkeypatch.setitem(vars(tool), "post_convert", noop_post_convert)
        monkeypatch.setitem(vars(tool), "verify", fake_verify)
    monkeypatch.setattr(job_manager_module, "round_trip", fake_round_trip)
    monkeypatch.setattr(
        job_manager_module.registry, "mode_supports_round_trip", lambda _mode: True,
    )


def _record_repin(romm: FakeRomm, rom_id: int, source: Path, mode: ConversionMode) -> int:
    """The re-pin row the plan route writes before the batch is submitted."""
    from services.romm import repin as romm_repin

    output = source.with_suffix(".cso")
    return romm_repin.record(
        romm.roms[rom_id], str(output), {"igdb_id": romm.roms[rom_id]["igdb_id"]},
        mode.value, source_path=str(source),
    )


async def _convert(source: Path, mode=ConversionMode.CSO_COMPRESS, *, delete=True):
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=mode,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=delete,
    ))
    manager = convert_routes.job_manager
    await manager._process_job(job.id)
    # The terminal listeners' work (the Ledger write, the re-pin settle).
    for _ in range(3):
        await asyncio.gather(*list(manager._background_tasks), return_exceptions=True)
    return job


def _reidentify_records(job) -> list[dict]:
    return [r for r in job.referrers_updated if r.get("referrer") == REIDENTIFY]


def _ledger_records(job) -> list[dict]:
    with _db.SessionLocal() as session:
        entry = session.query(_db.LedgerEntry).filter_by(job_id=job.id).one()
        return [r for r in entry.referrers_updated if r.get("referrer") == REIDENTIFY]


def _row(row_id: int):
    with _db.SessionLocal() as session:
        row = session.get(_db.RommRepin, row_id)
        return row.state, row.detail


# ---- skipped -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_psp_cso_with_the_swap_off_skips_the_re_pin_and_says_why(vol, romm):
    source = vol / "romm" / "psp" / "Game.iso"
    row_id = _record_repin(romm, ROM_ID, source, ConversionMode.CSO_COMPRESS)

    job = await _convert(source)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert not source.exists()
    assert (source.parent / "Game.cso").read_bytes() == OUTPUT_BYTES
    # RomM wasn't asked to rename anything: the swap is off.
    assert romm.renames == []
    # The re-pin is closed, with the reason.
    state, detail = _row(row_id)
    assert state == "done"
    assert "RomM 5.4.1" in detail and "title id" in detail
    # The job and the Ledger say why.
    assert "RomM 5.4.1" in job.message and "re-pin" in job.message, job.message
    (record,) = _reidentify_records(job)
    assert record["status"] == "updated"
    assert record["rom_id"] == ROM_ID
    assert "RomM 5.4.1" in record["note"]
    (persisted,) = _ledger_records(job)
    assert persisted["note"] == record["note"]


# ---- not skipped -------------------------------------------------------------


@pytest.mark.asyncio
async def test_ps2_cso_with_the_swap_off_still_re_pins(vol, romm):
    source = vol / "romm" / "ps2" / "Game.iso"
    row_id = _record_repin(romm, ROM_ID + 1, source, ConversionMode.CSO_COMPRESS)

    job = await _convert(source)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert not source.exists()
    assert _row(row_id)[0] == "pending"
    assert _reidentify_records(job) == []
    assert job.warning is None, job.warning
    assert _ledger_records(job) == []


@pytest.mark.asyncio
async def test_with_the_swap_on_psp_cso_still_swaps(vol, romm):
    from services.romm import settings as romm_settings

    await romm_settings.save({"swap_keep_rom_id": True})
    source = vol / "romm" / "psp" / "Game.iso"
    row_id = _record_repin(romm, ROM_ID, source, ConversionMode.CSO_COMPRESS)

    job = await _convert(source)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert romm.renames == [(ROM_ID, "Game.cso")]
    assert romm.roms[ROM_ID]["fs_name"] == "Game.cso"
    assert _reidentify_records(job) == []
    (swap,) = [r for r in job.referrers_updated if r["referrer"] == "romm_swap"]
    assert swap["kept_rom_id"] is True
    # Settled by the swap, not by the skip.
    state, detail = _row(row_id)
    assert state == "done"
    assert "renamed" in detail


@pytest.mark.asyncio
async def test_an_unknown_romm_version_re_pins(vol, romm):
    romm.version = "5.5.0"
    source = vol / "romm" / "psp" / "Game.iso"
    row_id = _record_repin(romm, ROM_ID, source, ConversionMode.CSO_COMPRESS)

    job = await _convert(source)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert not source.exists()
    assert _row(row_id)[0] == "pending"
    assert _reidentify_records(job) == []
    assert job.warning is None, job.warning


@pytest.mark.asyncio
async def test_a_heartbeat_that_cant_be_read_re_pins(vol, romm):
    romm.heartbeat_status = 500
    source = vol / "romm" / "psp" / "Game.iso"
    row_id = _record_repin(romm, ROM_ID, source, ConversionMode.CSO_COMPRESS)

    job = await _convert(source)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert not source.exists()
    assert _row(row_id)[0] == "pending"
    assert _reidentify_records(job) == []


@pytest.mark.asyncio
async def test_title_id_extraction_off_re_pins(vol, romm):
    romm.title_id_extraction = False
    source = vol / "romm" / "psp" / "Game.iso"
    row_id = _record_repin(romm, ROM_ID, source, ConversionMode.CSO_COMPRESS)

    job = await _convert(source)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert _row(row_id)[0] == "pending"
    assert _reidentify_records(job) == []


@pytest.mark.asyncio
async def test_a_kept_source_re_pins(vol, romm):
    """RomM re-identifies only once the Source is gone: a conversion that
    keeps it keeps its re-pin."""
    source = vol / "romm" / "psp" / "Game.iso"
    row_id = _record_repin(romm, ROM_ID, source, ConversionMode.CSO_COMPRESS)

    job = await _convert(source, delete=False)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert source.exists()
    assert _row(row_id)[0] == "pending"
    assert _reidentify_records(job) == []


# ---- the list ----------------------------------------------------------------


def test_the_list_holds_the_spikes_findings_for_romm_5_4_1():
    from services.romm import reidentify

    assert reidentify.REIDENTIFIED["5.4.1"] == frozenset({
        ("ps2", ".chd"), ("psp", ".chd"), ("psp", ".cso"), ("ngc", ".rvz"),
    })


def test_every_listed_format_is_one_a_registered_mode_writes():
    from services.romm import reidentify
    from services.tools import registry

    written = {
        ext for tool in registry.all() for spec in tool.modes
        for ext in registry.mode_output_extensions(spec.mode)
    }
    for pairs in reidentify.REIDENTIFIED.values():
        for _platform, ext in pairs:
            assert ext in written, ext


@pytest.mark.parametrize(("beat", "slug", "mode", "output", "skipped"), [
    ({"SYSTEM": {"VERSION": "5.4.1"}, "FILESYSTEM": {"TITLE_ID_EXTRACTION_ENABLED": True}},
     "ngc", "dolphin_rvz", "/v/ngc/Game.rvz", True),
    ({"SYSTEM": {"VERSION": "5.4.1"}, "FILESYSTEM": {"TITLE_ID_EXTRACTION_ENABLED": True}},
     "ngc", "dolphin_wia", "/v/ngc/Game.wia", False),
    ({"SYSTEM": {"VERSION": "5.4.1"}, "FILESYSTEM": {"TITLE_ID_EXTRACTION_ENABLED": True}},
     "psp", "zso_compress", "/v/psp/Game.zso", False),
    # The heartbeat must say title-id extraction is on.
    ({"SYSTEM": {"VERSION": "5.4.1"}}, "psp", "cso_compress", "/v/psp/Game.cso", False),
    # No version: fail closed.
    ({"FILESYSTEM": {"TITLE_ID_EXTRACTION_ENABLED": True}},
     "psp", "cso_compress", "/v/psp/Game.cso", False),
    # An output the mode doesn't write, or no mode at all.
    ({"SYSTEM": {"VERSION": "5.4.1"}, "FILESYSTEM": {"TITLE_ID_EXTRACTION_ENABLED": True}},
     "psp", "cso_compress", "/v/psp/Game.rvz", False),
    ({"SYSTEM": {"VERSION": "5.4.1"}, "FILESYSTEM": {"TITLE_ID_EXTRACTION_ENABLED": True}},
     "psp", None, "/v/psp/Game.cso", False),
])
def test_the_decision(beat, slug, mode, output, skipped):
    from services.romm import reidentify

    assert (reidentify.reason(beat, slug, mode, output) is not None) is skipped
