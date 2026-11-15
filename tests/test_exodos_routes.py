"""Route tests for the eXoDOS tool: the verify trio on a ``.dosz``.

Real products from the real service; only the verification store is stubbed,
so the test sees whether a passing check is recorded.
"""
from __future__ import annotations

import zipfile
from unittest.mock import AsyncMock, Mock

import pytest

from app.routes import info as info_routes


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch):
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    store = Mock()
    store.mark_verified = AsyncMock()
    monkeypatch.setattr(info_routes, "verification_store", store)
    return {"root": tmp_path, "store": store}


def _product(path, *, members=None, compression=zipfile.ZIP_DEFLATED):
    members = members or {"dosbox.conf": b"[autoexec]\r\nmount c .\\\r\n", "GAME.EXE": b"g" * 5000}
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def test_verify_routes_are_registered():
    cfg = info_routes._VERIFY_CONFIG["exodos"]
    assert cfg.url_prefix == "exodos-"
    paths = {route.path for route in info_routes.router.routes}
    assert {
        "/exodos-verify", "/exodos-verify/events", "/exodos-verify-batch/events",
    } <= paths
    assert callable(info_routes.verify_exodos)


@pytest.mark.asyncio
async def test_web_verify_passes_a_good_product_and_records_it(volume):
    path = _product(volume["root"] / "Abaron (1995).dosz")

    result = await info_routes.verify_exodos(path=path)

    assert result["valid"] is True
    volume["store"].mark_verified.assert_awaited_once_with(path)


@pytest.mark.asyncio
async def test_web_verify_fails_a_corrupt_product_and_records_nothing(volume):
    path = volume["root"] / "Broken.dosz"
    _product(path, compression=zipfile.ZIP_STORED)
    raw = bytearray(path.read_bytes())
    raw[raw.index(b"g" * 100) + 10] ^= 0xFF
    path.write_bytes(bytes(raw))

    result = await info_routes.verify_exodos(path=str(path))

    assert result["valid"] is False
    assert "GAME.EXE" in result["message"]
    volume["store"].mark_verified.assert_not_awaited()
