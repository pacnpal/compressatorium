"""Tests for the preferences store (server-persisted UI layout)."""

# ruff: noqa: S101

from pathlib import Path

import pytest

from app.services.preferences_store import PreferencesStore


@pytest.fixture(name="store")
def _store(tmp_path: Path) -> PreferencesStore:
    """A preferences store bound to a temporary SQLite DB."""
    return PreferencesStore(str(tmp_path / "preferences.db"))


@pytest.mark.asyncio
async def test_get_missing_returns_none(store: PreferencesStore) -> None:
    assert await store.get("layout") is None


@pytest.mark.asyncio
async def test_put_then_get_round_trips(store: PreferencesStore) -> None:
    layout = {
        "panels": {"left": 220, "right": 360},
        "columns": {"chdman": {"name": 320, "size": 90, "ext": 70, "status": 160}},
    }
    returned = await store.put("layout", layout)
    assert returned == layout
    assert await store.get("layout") == layout


@pytest.mark.asyncio
async def test_put_overwrites(store: PreferencesStore) -> None:
    await store.put("layout", {"panels": {"left": 220, "right": 360}})
    await store.put("layout", {"panels": {"left": 200, "right": 420}})
    stored = await store.get("layout")
    assert stored == {"panels": {"left": 200, "right": 420}}


@pytest.mark.asyncio
async def test_keys_are_independent(store: PreferencesStore) -> None:
    await store.put("layout", {"a": 1})
    await store.put("other", {"b": 2})
    assert await store.get("layout") == {"a": 1}
    assert await store.get("other") == {"b": 2}


@pytest.mark.asyncio
async def test_conversion_prefs_route_round_trips(tmp_path: Path, monkeypatch) -> None:
    """GET/PUT /preferences/conversion persist per-tool compression strings."""
    from app.models import ConversionPreferences
    from app.routes import preferences as preferences_routes

    store = PreferencesStore(str(tmp_path / "conv.db"))
    monkeypatch.setattr(preferences_routes, "preferences_store", store)

    assert await preferences_routes.get_conversion_preferences() == {}

    prefs = ConversionPreferences.model_validate({"nsz": "block:20", "dolphin": "zstd:19"})
    saved = await preferences_routes.put_conversion_preferences(prefs)
    assert saved == {"nsz": "block:20", "dolphin": "zstd:19"}
    assert await preferences_routes.get_conversion_preferences() == saved
