"""Auto never picks the eXoDOS repack (integration of #294 and #324).

The repack is manual only. A ``dos`` folder of real eXoDOS game zips, with the
template tree configured so the tool would accept them by hand, is recognised
as Platform ``dos`` and left alone by Auto, with a reason.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from services import platform_detect

from app.models import AutoPlanRequest
from app.routes import convert as convert_routes


@pytest.fixture(autouse=True)
def _fresh_cache():
    platform_detect.clear_cache()
    yield
    platform_detect.clear_cache()


@pytest.fixture(name="dos_folder")
def _dos_folder(tmp_path: Path, monkeypatch):
    dos_tree = tmp_path / "eXoDOS" / "!dos"
    games = tmp_path / "roms" / "dos"
    paths = []
    for top in ("Abaron", "Zork"):
        (dos_tree / top).mkdir(parents=True)
        (dos_tree / top / "dosbox.conf").write_bytes(b"[autoexec]\r\nmount c .\\eXoDOS\\" + top.encode() + b"\r\n")
        games.mkdir(parents=True, exist_ok=True)
        game = games / f"{top} (1995).zip"
        with zipfile.ZipFile(game, "w") as zf:
            zf.writestr(f"{top}/{top.upper()}.EXE", b"exe")
        paths.append(str(game))
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(convert_routes.settings, "exodos_dos_dir", str(dos_tree))

    async def no_saved_prefs(_key):
        return {}

    monkeypatch.setattr(convert_routes.preferences_store, "get", no_saved_prefs)
    return paths


def test_the_repack_takes_these_zips_by_hand(dos_folder):
    from services.tools import registry

    assert all(registry.get("exodos").converts_path(p) for p in dos_folder)


@pytest.mark.asyncio
async def test_auto_over_a_dos_folder_plans_no_repack(dos_folder):
    plan = await convert_routes.auto_plan(AutoPlanRequest(file_paths=dos_folder))

    assert [row.platform for row in plan] == ["dos", "dos"]
    assert [row.mode for row in plan] == [None, None]
    assert all(row.skip_reason for row in plan)
