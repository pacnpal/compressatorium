"""Three small bugs the smoke run found.

- Reset to default also clears the "Compatible with..." choice.
- A History Status picked by hand drops the link's ``?status=``, so a reload
  keeps it.
- The Library's "games need a console" banner leaves out games ignored in
  Activity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from . import test_folder_library_303 as folder_library_303
from .test_folder_library_303 import _register
from .test_platform_detect import _blank

# #303's `db` fixture: pytest registers it under its own name from this binding.
_db_fixture = folder_library_303._db_fixture

SRC = Path(__file__).resolve().parent.parent / "src" / "lib" / "components"


def test_reset_clears_the_compatible_with_choice() -> None:
    picker = (SRC / "panels" / "CompressionPicker.svelte").read_text(encoding="utf-8")
    reset = picker[picker.index('class="reset-btn"'):]
    reset = reset[:reset.index("Reset to default")]
    assert "compatId = ''" in reset


def test_a_status_picked_by_hand_replaces_the_one_in_the_url() -> None:
    history = (SRC / "activity" / "HistoryList.svelte").read_text(encoding="utf-8")
    refilter = history[history.index("function refilter()"):]
    refilter = refilter[:refilter.index("\n  }\n")]
    assert "syncUrlStatus()" in refilter
    forget = history[history.index("function syncUrlStatus()"):]
    forget = forget[:forget.index("\n  }\n")]
    assert "key !== 'status'" in forget
    # The new pick is written back; only All leaves no `status`.
    assert "status ? [['status', status]] : []" in forget
    # Not pushState/location.hash: those would re-run the route or add history.
    assert "history.replaceState" in forget


@pytest.mark.asyncio
async def test_an_ignored_game_is_not_counted_as_needing_a_console(db: Path) -> None:
    from services import findings, libraries

    _blank(db / "roms" / "misc" / "A.iso")
    _blank(db / "roms" / "misc" / "B.iso")
    await _register(db / "roms")
    library = libraries.folder_libraries()[0]
    assert library.summary()["unsorted"] == 2

    row = next(r for r in findings.list_findings() if r["path"].endswith("A.iso"))
    assert findings.dismiss(row["id"], fingerprint=row["fingerprint"])

    assert library.summary()["unsorted"] == 1
    findings.undismiss(row["id"])
    assert library.summary()["unsorted"] == 2
