import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "app"))

from services.archive import archive_service


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
