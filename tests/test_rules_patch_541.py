"""The console table saves one console at a time (#541, Codex 4653298940).

``PATCH .../rules`` merges the consoles it names into the stored Rules
(``null`` removes one), so a table that saved before the Rules had loaded, or
any partial map, can never wipe the other consoles' settings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .test_folder_library_303 import _db_fixture, _register, _tree

__all__ = ["_db_fixture", "_tree"]


@pytest.mark.asyncio
async def test_a_patch_changes_only_the_consoles_it_names(tree: Path) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {
        "rules": {"ps2": {"mode": "createdvd", "enabled": True},
                  "ngc": {"mode": "dolphin_rvz", "enabled": True}},
        "check_rules": {"ps2": {"enabled": True}},
    })

    saved = await routes.patch_library_rules(lib["id"], {
        "rules": {"ngc": {"mode": "dolphin_rvz", "enabled": False}},
    })

    assert saved["rules"]["ps2"]["mode"] == "createdvd"
    assert saved["rules"]["ngc"]["enabled"] is False
    assert saved["check_rules"]["ps2"]["enabled"] is True


@pytest.mark.asyncio
async def test_null_removes_one_console_and_empty_changes_nothing(tree: Path) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {
        "rules": {"ps2": {"mode": "createdvd", "enabled": True},
                  "ngc": {"mode": "dolphin_rvz", "enabled": True}},
        "check_rules": {"ps2": {"enabled": True}, "ngc": {"enabled": True}},
    })

    await routes.patch_library_rules(lib["id"], {"rules": {}, "check_rules": {}})
    saved = await routes.patch_library_rules(lib["id"], {
        "rules": {"ngc": None}, "check_rules": {"ps2": None},
    })

    assert set(saved["rules"]) == {"ps2"}
    assert set(saved["check_rules"]) == {"ngc"}


@pytest.mark.asyncio
async def test_a_patch_is_held_to_the_librarys_policies(tree: Path) -> None:
    from fastapi import HTTPException
    from routes import libraries as routes

    lib = await _register(tree)
    with pytest.raises(HTTPException) as err:
        await routes.patch_library_rules(lib["id"], {
            "rules": {"ps2": {"mode": "createdvd", "duplicate_action": "overwrite"}},
        })
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_romm_takes_a_patch_too() -> None:
    from routes import romm as routes

    assert callable(routes.patch_romm_rules)
