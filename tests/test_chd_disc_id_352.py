"""The disc ID of a converted CHD lives in the app's database (#352).

chdman 0.285 opens a compressed CHD read-only: its v5 header parser sets
``m_allow_writes = !compressed()`` (MAME ``src/lib/util/chd.cpp``), so
``chdman addmeta`` on every CHD this app makes fails with "File not
writeable". The GAME/NAME tag the app tried to embed after each conversion was
therefore never written, and nothing may depend on it.

What replaces it: the disc ID read from the Source at conversion time is
recorded for the output in ``chd_metadata_store`` (the cache ``/api/info`` and
the library scan already read), and a CHD's own sectors, Dreamcast GDRO tag or
companion file supply it otherwise. No chdman metadata command runs for it.

chdman is faked at the ``SubprocessRunner.run_capture`` seam; the real-binary
check is ``tests/test_chd_disc_id_352_real.py``.
"""
from __future__ import annotations

import logging

import pytest

from app.services.tools import registry

from .test_disc_id import _make_chd_v5, _make_iso

SYSTEM_CNF = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\n"


@pytest.fixture(name="db")
def _db(db_path, reset_db_engine):
    from services import db as _db

    _db.init_engine(db_path)
    yield
    reset_db_engine()


@pytest.fixture(name="chdman_calls")
def _chdman_calls(monkeypatch):
    """Every chdman command the code under test spawns, and a fake that
    answers like chdman 0.285 does on a compressed CHD."""
    calls: list[list[str]] = []

    async def fake_run_capture(cmd, **kwargs):
        calls.append(list(cmd))
        if "addmeta" in cmd:
            return 1, b"", b"Error opening CHD file: File not writeable\n"
        return 1, b"", b"Error reading metadata: Metadata not found\n"

    runner = registry.get("chdman")._service.runner
    monkeypatch.setattr(runner, "run_capture", fake_run_capture)
    return calls


def _metadata_commands(calls) -> list[str]:
    return [c for call in calls for c in call if c in ("addmeta", "delmeta", "dumpmeta")]


def _ps2_iso(tmp_path):
    iso = tmp_path / "Game.iso"
    iso.write_bytes(_make_iso({"SYSTEM.CNF": SYSTEM_CNF}))
    return iso


@pytest.mark.asyncio
async def test_post_convert_records_the_sources_disc_id_for_the_output(
    tmp_path, db, chdman_calls,
):
    from services.chd_metadata_store import chd_metadata_store

    iso = _ps2_iso(tmp_path)
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"MComprHD")

    await registry.get("chdman").post_convert(str(iso), str(chd), "createdvd")

    assert await chd_metadata_store.get_disc_id_info(str(chd)) == ("SLUS-20312", None)
    assert await chd_metadata_store.is_disc_id_checked(str(chd))
    # No GAME/NAME tag is read, written or deleted: chdman refuses the write.
    assert _metadata_commands(chdman_calls) == []


@pytest.mark.asyncio
async def test_a_disc_id_that_cannot_be_recorded_is_a_warning_naming_the_output(
    tmp_path, chdman_calls, monkeypatch, caplog,
):
    from services.chd_metadata_store import chd_metadata_store

    async def store_down(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(chd_metadata_store, "update_disc_id_info", store_down)
    iso = _ps2_iso(tmp_path)
    chd = tmp_path / "Game.chd"
    chd.write_bytes(b"MComprHD")

    with caplog.at_level(logging.WARNING):
        # Best effort: the conversion itself still succeeds.
        await registry.get("chdman").post_convert(str(iso), str(chd), "createdvd")

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("Game.chd" in m and "SLUS-20312" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_extract_from_chd_reads_the_disc_not_a_game_tag(tmp_path, chdman_calls):
    from services.disc_id import extract_from_chd

    chd = tmp_path / "Game.chd"
    chd.write_bytes(_make_chd_v5(_make_iso({"SYSTEM.CNF": SYSTEM_CNF})))

    result = await extract_from_chd(str(chd), "chdman")

    assert result and result["game_id"] == "SLUS-20312"
    assert not any("GAME" in call for call in chdman_calls), chdman_calls


@pytest.mark.asyncio
async def test_the_library_scan_stores_the_disc_id_it_reads_without_addmeta(
    tmp_path, monkeypatch, chdman_calls,
):
    from app.routes import info as info_routes

    chd = tmp_path / "Game.chd"
    chd.write_bytes(_make_chd_v5(_make_iso({"SYSTEM.CNF": SYSTEM_CNF})))
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    stored: dict = {}

    async def fresh(_path):
        return False

    async def nothing(*_a, **_k):
        return None

    async def info(_path):
        return {"raw_data": "Metadata:     Tag='DVD '  Index=0  Length=0 bytes"}

    async def set_metadata(path, info, persist=False):
        return {"media_type": "dvd"}

    async def update_disc_id_info(path, game_id, title, persist=True):
        stored[path] = (game_id, title)

    store = info_routes.chd_metadata_store
    monkeypatch.setattr(info_routes.chdman_service, "info", info)
    monkeypatch.setattr(store, "set_metadata", set_metadata)
    monkeypatch.setattr(store, "flush_async", nothing)
    monkeypatch.setattr(store, "is_stale", fresh)
    monkeypatch.setattr(store, "is_disc_id_checked", fresh)
    monkeypatch.setattr(store, "mark_disc_id_checked", nothing)
    monkeypatch.setattr(store, "update_disc_id_info", update_disc_id_info)

    await info_routes.scan_metadata_task(force=False)

    assert list(stored.values()) == [("SLUS-20312", None)]
    assert "addmeta" not in _metadata_commands(chdman_calls)
