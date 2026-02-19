import pytest

from app import main as main_module


@pytest.mark.asyncio
async def test_health_uses_configured_absolute_igir_path(tmp_path, monkeypatch):
    """Health should check the configured absolute IGIR_PATH, not generic PATH lookup."""
    missing_igir = tmp_path / "missing-igir"
    monkeypatch.setattr(main_module.settings, "igir_path", str(missing_igir))
    monkeypatch.setattr(
        main_module.shutil,
        "which",
        lambda value: "/usr/local/bin/igir" if value == "igir" else None,
    )

    result = await main_module.health_check()

    assert result["igir_available"] is False


@pytest.mark.asyncio
async def test_health_uses_configured_relative_igir_command(monkeypatch):
    """Health should resolve a non-absolute configured command via PATH lookup."""
    monkeypatch.setattr(main_module.settings, "igir_path", "igir-custom")
    monkeypatch.setattr(
        main_module.shutil,
        "which",
        lambda value: "/usr/local/bin/igir-custom" if value == "igir-custom" else None,
    )

    result = await main_module.health_check()

    assert result["igir_available"] is True
