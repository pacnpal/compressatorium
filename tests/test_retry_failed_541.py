"""Try them again releases only the failed holds (#541, CodeRabbit 4652729342).

"Forget what's been converted" clears a console's converted history and its
holds; "Try them again" must not, or a game already converted under a rename or
overwrite Rule loses its protection and is converted again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .test_folder_library_303 import _db_fixture, _register, _tree

__all__ = ["_db_fixture", "_tree"]


@pytest.mark.asyncio
async def test_retrying_failed_games_keeps_what_was_converted(tree: Path) -> None:
    from routes import libraries as routes
    from services import libraries
    from services.rules import store

    lib = await _register(tree)
    library = libraries.get(lib["id"])
    await store.write(store.STATE_KEY, library, {"ps2": {
        "converted": {"ps2/Ico.iso": {"output": "ps2/Ico.chd"}},
        "failed": {"ps2/Bad.iso": {"fingerprint": "x"}},
    }})

    answer = await routes.forget_library_converted(
        lib["id"], {"platform_ids": ["ps2"], "failed_only": True},
    )

    assert answer["state"]["ps2"]["converted"] == {"ps2/Ico.iso": {"output": "ps2/Ico.chd"}}
    assert "failed" not in answer["state"]["ps2"]
    assert answer["cleared"] == 1


@pytest.mark.asyncio
async def test_forgetting_still_clears_both(tree: Path) -> None:
    from routes import libraries as routes
    from services import libraries
    from services.rules import store

    lib = await _register(tree)
    library = libraries.get(lib["id"])
    await store.write(store.STATE_KEY, library, {"ps2": {
        "converted": {"ps2/Ico.iso": {"output": "ps2/Ico.chd"}},
        "failed": {"ps2/Bad.iso": {"fingerprint": "x"}},
    }})

    answer = await routes.forget_library_converted(lib["id"], {"platform_ids": ["ps2"]})

    assert "converted" not in answer["state"]["ps2"]
    assert "failed" not in answer["state"]["ps2"]


def test_try_them_again_asks_for_the_holds_only() -> None:
    src = (Path(__file__).resolve().parents[1] / "src/lib/components/library/RuleAdvanced.svelte")
    text = src.read_text(encoding="utf-8")
    body = text[text.index("async function retryFailed()"):]
    body = body[: body.index("\n  }\n")]
    assert "{ failedOnly: true }" in body
