"""A Rule the sweep skips is logged once per reason, naming it (#480).

The sweep re-checks a conversion Rule before it lists anything: is its tool
installed, is its output folder inside a Volume, and (once it has listed) is
its mode one for the Platform. A Rule that fails one is skipped without a run
recorded, so it is due again on the next tick and is retried as soon as the
tool or folder is fixed. Each skip used to log on every one of those ticks,
under ``romm_auto:`` whatever the Library, and without naming the Library.

Now each is logged like the other Rules a sweep can't run (#477): once, under
the Library's own prefix, naming the Library and the Platform, and again only
when the reason changes or clears. The sweep's ``errors`` are unchanged.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from services.preferences_store import preferences_store
from services.tools import registry

from .test_folder_library_303 import _db_fixture, _queue, _ready, _register, _tree

__all__ = ["_db_fixture", "_queue", "_ready", "_tree"]


@pytest.fixture(autouse=True)
def _nothing_reported_yet(monkeypatch):
    """Library ids repeat across tests, each in its own database."""
    from services.rules import sweep

    monkeypatch.setattr(sweep, "_reported_unrunnable", {})
    monkeypatch.setattr(sweep, "_reported_skips", {})


@pytest.fixture(name="tool_ready")
def _tool_ready(monkeypatch):
    """Whether every tool reads as installed: flip ``state["ready"]``."""
    state = {"ready": True}

    async def ready(_tool, **_kwargs):
        return state["ready"]

    monkeypatch.setattr(registry, "tool_is_ready", ready)
    return state


def _skips(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]


async def _store(lib_id: str, rules: dict) -> None:
    """*rules* stored as they are, as a blob edited straight in the database."""
    await preferences_store.put("rules", {f"{lib_id}:{k}": v for k, v in rules.items()})


def _assert_names_the_rule(line: str) -> None:
    assert line.startswith("folder_library:"), line
    assert "romm_auto" not in line
    assert "roms" in line and "ps2" in line, line


@pytest.mark.asyncio
async def test_an_output_folder_outside_the_volumes_is_logged_once(
    tree: Path, tool_ready, queue, caplog,
) -> None:
    from routes import libraries as routes
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    await _store(lib["id"], {
        "ps2": {"enabled": True, "mode": "createdvd", "output_dir": "/outside/tz480"},
    })
    library = libraries.get(lib["id"])

    with caplog.at_level(logging.DEBUG):
        results = [await sweep.sweep(library) for _ in range(3)]
        # Run now is the same Rule skipped for the same reason.
        results.append(
            await routes.run_library(lib["id"], routes.SweepRequest(platform_ids=["ps2"])),
        )

    assert queue == []
    for result in results:
        assert result["errors"] == [
            {"platform_id": "ps2", "error": "output_dir_outside_volumes"},
        ]
    lines = _skips(caplog)
    assert len(lines) == 1, lines
    _assert_names_the_rule(lines[0])
    assert "/outside/tz480" in lines[0]


@pytest.mark.asyncio
async def test_an_output_folder_that_does_not_answer_is_logged_once(
    tree: Path, tool_ready, queue, caplog, monkeypatch,
) -> None:
    from services import libraries
    from services.rules import sweep

    def not_answering(_path):
        raise OSError("stale file handle")

    monkeypatch.setattr(sweep, "is_within_configured_volumes", not_answering)
    lib = await _register(tree)
    await _store(lib["id"], {
        "ps2": {"enabled": True, "mode": "createdvd", "output_dir": str(tree)},
    })
    library = libraries.get(lib["id"])

    with caplog.at_level(logging.DEBUG):
        results = [await sweep.sweep(library) for _ in range(3)]

    for result in results:
        assert result["errors"] == [
            {"platform_id": "ps2", "error": "output_dir_outside_volumes"},
        ]
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    _assert_names_the_rule(warnings[0])
    assert "not answering" in warnings[0]


@pytest.mark.asyncio
async def test_a_missing_tool_is_logged_once_and_again_after_it_clears(
    tree: Path, tool_ready, queue, caplog,
) -> None:
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    await _store(lib["id"], {"ps2": {"enabled": True, "mode": "createdvd"}})
    library = libraries.get(lib["id"])

    tool_ready["ready"] = False
    with caplog.at_level(logging.DEBUG):
        results = [await sweep.sweep(library) for _ in range(3)]

    assert queue == []
    for result in results:
        assert result["errors"] == [{"platform_id": "ps2", "error": "tool_not_ready"}]
    lines = _skips(caplog)
    assert len(lines) == 1, lines
    _assert_names_the_rule(lines[0])
    assert "chdman" in lines[0] and "not installed" in lines[0]

    # Still retried on the next tick: once the tool is back, the Rule runs.
    caplog.clear()
    tool_ready["ready"] = True
    with caplog.at_level(logging.DEBUG):
        await sweep.sweep(library, ignore_schedule=True)
    assert [Path(p).name for p in queue] == ["Ico.iso"]
    assert _skips(caplog) == []

    # Gone again: said again.
    tool_ready["ready"] = False
    with caplog.at_level(logging.DEBUG):
        await sweep.sweep(library, ignore_schedule=True)
        await sweep.sweep(library, ignore_schedule=True)
    lines = _skips(caplog)
    assert len(lines) == 1 and "not installed" in lines[0], lines


@pytest.mark.asyncio
async def test_a_new_reason_is_logged_when_the_old_one_is_fixed(
    tree: Path, tool_ready, queue, caplog,
) -> None:
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    await _store(lib["id"], {
        "ps2": {"enabled": True, "mode": "createdvd", "output_dir": "/outside/tz480"},
    })
    library = libraries.get(lib["id"])

    tool_ready["ready"] = False
    with caplog.at_level(logging.DEBUG):
        await sweep.sweep(library)
        await sweep.sweep(library)
        tool_ready["ready"] = True
        await sweep.sweep(library)
        await sweep.sweep(library)

    lines = _skips(caplog)
    assert len(lines) == 2, lines
    assert "not installed" in lines[0] and "/outside/tz480" in lines[1], lines


@pytest.mark.asyncio
async def test_a_mode_for_another_platform_is_logged_once(
    tree: Path, tool_ready, queue, caplog,
) -> None:
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    await _store(lib["id"], {"ps2": {"enabled": True, "mode": "jwud_compress"}})
    library = libraries.get(lib["id"])

    with caplog.at_level(logging.DEBUG):
        results = [await sweep.sweep(library) for _ in range(3)]

    assert queue == []
    for result in results:
        assert result["errors"] == [
            {"platform_id": "ps2", "error": "tool_wrong_for_platform"},
        ]
    lines = _skips(caplog)
    assert len(lines) == 1, lines
    _assert_names_the_rule(lines[0])
    assert "jwud_compress" in lines[0]


@pytest.mark.asyncio
async def test_a_skipped_rule_says_its_dropped_codec_only_when_it_runs(
    tree: Path, tool_ready, queue, caplog,
) -> None:
    """What the sanitizer dropped is said once per sweep that runs the Rule
    (#477). A Rule skipped every tick doesn't run, so it doesn't say it then."""
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    await _store(lib["id"], {
        "ps2": {"enabled": True, "mode": "createdvd", "compression": "cdzl"},
    })
    library = libraries.get(lib["id"])

    tool_ready["ready"] = False
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            await sweep.sweep(library)
    assert [m for m in caplog.messages if "cdzl" in m] == []

    tool_ready["ready"] = True
    with caplog.at_level(logging.DEBUG):
        await sweep.sweep(library)
    assert len([m for m in caplog.messages if "cdzl" in m]) == 1, caplog.messages


@pytest.mark.asyncio
async def test_a_removed_library_forgets_what_it_reported(
    tree: Path, tool_ready, queue,
) -> None:
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    await _store(lib["id"], {"ps2": {"enabled": True, "mode": "createdvd"}})
    library = libraries.get(lib["id"])
    tool_ready["ready"] = False
    await sweep.sweep(library)
    assert any(key[0] == lib["id"] for key in sweep._reported_skips)

    await sweep.forget_library(library)

    assert not any(key[0] == lib["id"] for key in sweep._reported_skips)
