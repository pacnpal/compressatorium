"""Tests for the unified ``post_convert`` disc-ID seam (#181, #352).

``ChdmanTool.post_convert`` is the single home for conversion-time disc IDs. A
direct ``createcd`` / ``createdvd`` job reaches it through
``job_manager._process_job`` (``registry.for_mode(mode).post_convert(...)``) and
a ``cso_to_chd`` chain reaches it through ``ChainTool``'s final step.

Since #352 it records the Source's disc ID in the app's database rather than
writing a GAME/NAME tag into the CHD, which chdman refuses on a compressed CHD.
These tests exercise the hook directly with ``extract_from_source`` and the
recording step (``_record_disc_id``) stubbed; the database write itself is
covered in ``test_chd_disc_id_352.py``. The chain's *routing* into the hook is
asserted in ``test_chain_service.py::test_chain_tags_final_chd_via_post_convert``.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import app.services.tools.chdman as chdman_mod
from app.services.tools import registry


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def chd_out(tmp_path):
    """A stand-in output CHD on disk (presence is all post_convert checks)."""
    out = tmp_path / "Game.chd"
    out.write_bytes(b"MComprHD")
    return out


@pytest.fixture
def patch_record(monkeypatch):
    """Stub the Source reader and the recording step.

    Returns ``(set_source, recorded)`` where ``recorded`` is a list each
    ``_record_disc_id`` call appends ``(path, game_id, title)`` to.
    """
    state: dict = {"source": None}
    recorded: list[tuple] = []

    def _extract(path, **_kwargs):  # sync: called via run_in_threadpool
        return state["source"]

    async def _record(path, game_id, title):
        recorded.append((path, game_id, title))

    monkeypatch.setattr(chdman_mod, "extract_from_source", _extract)
    monkeypatch.setattr(chdman_mod, "_record_disc_id", _record)

    def set_source(info):
        state["source"] = info

    return set_source, recorded


@pytest.mark.parametrize("mode", ["createcd", "createdvd"])
def test_post_convert_records_the_disc_id_for_disc_modes(
    mode, tmp_path, chd_out, patch_record,
):
    set_source, recorded = patch_record
    set_source({"game_id": "SLUS-20312", "title": "Demo Disc"})

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), mode))

    assert recorded == [(str(chd_out), "SLUS-20312", "Demo Disc")]


def test_post_convert_records_no_title_when_the_source_has_none(
    tmp_path, chd_out, patch_record,
):
    set_source, recorded = patch_record
    set_source({"game_id": "SLUS-20312"})  # no title key

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), "createdvd"))

    assert recorded == [(str(chd_out), "SLUS-20312", None)]


@pytest.mark.parametrize(
    "mode", ["createraw", "createhd", "createld", "extractcd", "extractdvd", "copy"],
)
def test_post_convert_noop_for_non_disc_modes(
    mode, tmp_path, chd_out, patch_record, monkeypatch,
):
    set_source, recorded = patch_record
    set_source({"game_id": "SLUS-20312"})

    extracted: list[str] = []
    monkeypatch.setattr(
        chdman_mod, "extract_from_source",
        lambda p, **_kw: extracted.append(p),
    )

    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), mode))

    # Non-CD/DVD modes carry no disc serial: the source is never even read.
    assert extracted == []
    assert recorded == []


def test_post_convert_noop_when_source_has_no_disc_id(tmp_path, chd_out, patch_record):
    set_source, recorded = patch_record
    set_source(None)  # extract_from_source found nothing

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    _run(registry.get("chdman").post_convert(str(src), str(chd_out), "createdvd"))

    assert recorded == []


def test_post_convert_missing_output_is_noop(tmp_path, patch_record):
    set_source, recorded = patch_record
    set_source({"game_id": "SLUS-20312"})

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    missing = tmp_path / "missing.chd"  # never created
    _run(registry.get("chdman").post_convert(str(src), str(missing), "createdvd"))

    assert recorded == []


def test_a_source_read_error_is_a_warning_not_a_failed_job(
    tmp_path, chd_out, monkeypatch, caplog,
):
    """Best effort, but never silent: the job goes on and the log says why."""
    def _boom(path, **_kwargs):
        raise OSError("source unreadable")

    monkeypatch.setattr(chdman_mod, "extract_from_source", _boom)

    src = tmp_path / "Game.iso"
    src.write_bytes(b"x")
    with caplog.at_level(logging.WARNING):
        # Must not raise.
        _run(registry.get("chdman").post_convert(str(src), str(chd_out), "createdvd"))

    assert any(
        "Disc ID not read" in r.getMessage() and "Game.chd" in r.getMessage()
        for r in caplog.records if r.levelno >= logging.WARNING
    )


def test_chain_tool_post_convert_is_noop(tmp_path):
    """ChainTool inherits the BaseTool no-op: when job_manager calls
    post_convert for a chain mode, it does nothing (the chain already recorded
    its output's disc ID via the final step during the conversion itself)."""
    out = tmp_path / "Game.chd"
    out.write_bytes(b"MComprHD")
    src = tmp_path / "Game.cso"
    src.write_bytes(b"x")
    # No exception, returns None.
    assert _run(
        registry.for_mode("cso_to_chd").post_convert(
            str(src), str(out), "cso_to_chd",
        )
    ) is None
