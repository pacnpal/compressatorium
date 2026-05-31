import sys
import zipfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "app"))

from services.archive import archive_service
from services.tools import registry


def test_filters_bin_when_cue_present():
    entries = [
        {"internal_path": "Game/Disc 1.bin", "extension": ".bin"},
        {"internal_path": "Game/Disc 1.cue", "extension": ".cue"},
    ]
    filtered = archive_service._filter_preferred_entries(entries)
    exts = sorted(e["extension"] for e in filtered)
    assert exts == [".cue"]


def test_keeps_bin_when_no_cue_or_gdi():
    entries = [
        {"internal_path": "Game/Disc 1.bin", "extension": ".bin"},
    ]
    filtered = archive_service._filter_preferred_entries(entries)
    exts = sorted(e["extension"] for e in filtered)
    assert exts == [".bin"]


def test_archive_input_extensions_cover_all_source_tools():
    # The archive listing must surface every convertible source — chdman
    # create sources, Dolphin sources, and 3DS sources (issue #113). A bare
    # .chd is an output/recompress target, not a convertible source, so it
    # stays out (chdman copy/extract disallow archive input).
    exts = registry.archive_input_extensions()
    assert {".3ds", ".cci", ".cia"} <= exts          # 3DS (the reported gap)
    assert {".rvz", ".gcz", ".wia", ".wbfs"} <= exts  # Dolphin
    assert {".gdi", ".iso", ".cue", ".bin"} <= exts   # chdman create
    assert ".chd" not in exts


def test_list_zip_surfaces_3ds_member(tmp_path):
    # A zip containing a .3ds file must list it as a convertible member,
    # mirroring how an on-disk .3ds is detected (issue #113).
    archive = tmp_path / "roms.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Game/cart.3ds", b"3ds-bytes")
        zf.writestr("readme.txt", b"ignore me")

    entries = archive_service.list_archive_contents(str(archive))
    members = {e["internal_path"]: e for e in entries}
    assert "Game/cart.3ds" in members
    assert members["Game/cart.3ds"]["extension"] == ".3ds"
    # output_stem flattens the subdir; the z3ds output extension is resolved
    # later from the member's real extension via _output_name_for_member.
    assert members["Game/cart.3ds"]["output_stem"] == "Game_cart"
    assert archive_service._output_name_for_member("Game/cart.3ds") == "Game_cart.3ds"
