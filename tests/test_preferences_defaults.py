"""The Defaults preference (restructure PR 1, spec §3.6 and §8.1).

``GET /api/preferences/defaults`` answers the model's defaults when nothing is
stored, and ``PUT`` stores the three safety choices new conversions start with.
"""


from pathlib import Path

import pytest
from pydantic import ValidationError

from app.routes import preferences as preferences_routes
from app.services.preferences_store import PreferencesStore


@pytest.fixture(name="store")
def _store(tmp_path: Path, monkeypatch) -> PreferencesStore:
    store = PreferencesStore(str(tmp_path / "defaults.db"))
    monkeypatch.setattr(preferences_routes, "preferences_store", store)
    return store


@pytest.mark.asyncio
async def test_nothing_stored_answers_the_safe_defaults(store) -> None:
    assert await preferences_routes.get_defaults_preferences() == {
        "verify_after": True,
        "delete_on_verify": False,
        "container_only": False,
    }


@pytest.mark.asyncio
async def test_a_put_round_trips(store) -> None:
    prefs = preferences_routes.DefaultsPreferences(verify_after=True, delete_on_verify=True)
    saved = await preferences_routes.put_defaults_preferences(prefs)
    assert saved == {"verify_after": True, "delete_on_verify": True, "container_only": False}
    assert await preferences_routes.get_defaults_preferences() == saved
    assert await store.get("defaults") == saved


@pytest.mark.asyncio
async def test_a_partial_stored_row_is_filled_from_the_model(store) -> None:
    await store.put("defaults", {"delete_on_verify": True, "stale_key": 1})
    assert await preferences_routes.get_defaults_preferences() == {
        "verify_after": True,
        "delete_on_verify": True,
        "container_only": False,
    }


@pytest.mark.asyncio
async def test_an_unreadable_stored_row_answers_the_safe_defaults(store) -> None:
    await store.put("defaults", {"delete_on_verify": "sometimes"})
    assert (await preferences_routes.get_defaults_preferences())["delete_on_verify"] is False


def test_unknown_or_wrong_typed_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        preferences_routes.DefaultsPreferences.model_validate({"verify_after": "maybe"})
    with pytest.raises(ValidationError):
        preferences_routes.DefaultsPreferences.model_validate({"surprise": True})
