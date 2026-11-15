"""A PS3 ISO read-back that hits an I/O error is no verdict (#306)."""
from __future__ import annotations

import asyncio

import pytest
from services import ps3
from services.disc_id import read_iso_file
from services.makeps3iso import MakePs3IsoService


def test_an_io_error_in_the_read_back_is_inconclusive(tmp_path, monkeypatch):
    def unreadable(path, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(ps3, "ps3_iso_title_id", unreadable)
    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"x")

    result = asyncio.run(MakePs3IsoService().verify(str(iso)))

    assert result["valid"] is False
    assert result.get("inconclusive") is True
    assert "Input/output error" in result["message"]


def test_a_readable_iso_without_a_title_id_is_a_verdict(tmp_path):
    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"\0" * 64 * 1024)

    result = asyncio.run(MakePs3IsoService().verify(str(iso)))

    assert result["valid"] is False
    assert "inconclusive" not in result


def test_strict_read_iso_file_raises_on_io_errors(tmp_path):
    missing = str(tmp_path / "gone.iso")
    assert read_iso_file(missing, ["PS3_GAME", "PARAM.SFO"]) is None
    with pytest.raises(OSError):
        read_iso_file(missing, ["PS3_GAME", "PARAM.SFO"], strict=True)
