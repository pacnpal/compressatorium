"""Route tests for the Xbox tool: ``GET /api/xiso-info`` and the verify trio."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routes import info as info_routes
from .test_xiso_service import build_image


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch):
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_xiso_info_describes_an_xbox_image(volume):
    src = volume / "Halo 3.iso"
    build_image(src, layout="XGD2")

    result = await info_routes.get_xiso_info(path=str(src))

    assert result.console == "Xbox 360"
    assert result.layout == "XGD2"
    assert result.file_count == 3
    assert result.trimmed is False


@pytest.mark.asyncio
async def test_xiso_info_is_a_422_for_another_consoles_iso(volume):
    # 422, not 500: the file is the problem, and the Info modal moves on to
    # the next tool claiming `.iso` when this one declines.
    src = volume / "psp.iso"
    src.write_bytes(b"\0" * 0x30000)
    with pytest.raises(HTTPException) as exc:
        await info_routes.get_xiso_info(path=str(src))
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_xiso_info_rejects_other_extensions(volume):
    src = volume / "game.wud"
    src.write_bytes(b"x")
    with pytest.raises(HTTPException) as exc:
        await info_routes.get_xiso_info(path=str(src))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_xiso_info_refuses_paths_outside_the_volumes(volume, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "g.iso"
    build_image(outside, layout="XGD2")
    with pytest.raises(HTTPException) as exc:
        await info_routes.get_xiso_info(path=str(outside))
    assert exc.value.status_code == 403


def test_verify_routes_are_registered():
    cfg = info_routes._VERIFY_CONFIG["xiso"]
    assert cfg.url_prefix == "xiso-"
    paths = {route.path for route in info_routes.router.routes}
    assert {"/xiso-info", "/xiso-verify", "/xiso-verify/events"} <= paths
    assert callable(info_routes.verify_xiso)


@pytest.mark.asyncio
async def test_verify_guard_accepts_a_real_world_xiso_name(volume):
    """The compound ``.xiso.iso`` passes the shared verify-route guard.

    It used to compare ``os.path.splitext(path)[1]`` (``.iso``) against the
    claim, so every XISO got a 400 -- which the browser's EventSource reports
    only as "Verification connection error".
    """
    img = volume / "Halo 3 (USA, Europe) (En,Ja,Fr,De,Es,It,Pt,Zh,Ko)-full.xiso.iso"
    build_image(img, layout="XISO", optimized=True)
    tool = info_routes.registry.get("xiso")
    cfg = info_routes._VERIFY_CONFIG["xiso"]
    await info_routes._guard_verify_path(str(img), tool, cfg)  # no raise

    plain = volume / "Halo 3.iso"
    build_image(plain, layout="XISO", optimized=True)
    with pytest.raises(HTTPException) as exc:
        await info_routes._guard_verify_path(str(plain), tool, cfg)
    assert exc.value.status_code == 400
