"""A malformed sheet never breaks detection, and never gets cached half-read."""
from __future__ import annotations

import pytest
from services import platform_detect
from utils.sheet_text import gdi_tracks


@pytest.fixture(autouse=True)
def _volume(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    platform_detect.clear_cache()
    yield
    platform_detect.clear_cache()


def test_a_superscript_size_field_is_not_a_number():
    [track] = gdi_tracks("1\n1 0 4 ² track01.bin 0\n")

    assert (track.name, track.sector_size) == ("track01.bin", None)


def test_a_malformed_gdi_never_breaks_detection(tmp_path):
    gdi = tmp_path / "dc" / "game.gdi"
    gdi.parent.mkdir()
    # 0xB2 reads as a superscript two in the Latin-1 spelling the readers try.
    gdi.write_bytes(b"1\n1 0 4 \xb2 track01.bin 0\n")

    result = platform_detect.detect_platform(str(gdi))

    assert result.platform == "dc"          # the folder still answers


def test_a_failing_header_reads_is_not_cached(tmp_path):
    path = tmp_path / "ps2" / "game.iso"
    path.parent.mkdir()
    path.write_bytes(b"\0" * 64)

    class Broken:
        id = "broken"
        input_extensions = frozenset({".iso"})
        platform_slugs = frozenset()

        def header_platform(self, _path):
            return None

        def header_reads(self, _path):
            raise ValueError("bad sheet")

    result = platform_detect.detect_platform(str(path), tools=[Broken()])

    assert result.platform == "ps2"
    assert str(path) not in platform_detect._cache
