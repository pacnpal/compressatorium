"""A Rule the sweep skips for its tool gives the tool's own reason (#484).

The once-per-reason skip line (#480) said ``nsz is not installed`` for a
Switch Rule on a server with nsz installed and no ``prod.keys``, and ``z3ds is
not installed to check its Images`` for a 3DS check Rule with ``zstd``
missing. The plugin already says why: ``not_ready_reason`` for converting,
``verify_not_ready_reason`` for checking. The line now says it the way the
CLI and Auto do, ``<display name> isn't ready: <reason>``, built in one place
(:func:`services.tools.not_ready_message`) so the three can't drift.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from services.preferences_store import preferences_store
from services.tools import BaseTool, not_ready_message, registry
from services.tools.chdman import ChdmanTool

from .test_folder_library_303 import _db_fixture, _queue, _register, _tree

__all__ = ["_db_fixture", "_queue", "_tree"]


@pytest.fixture(autouse=True)
def _nothing_reported_yet(monkeypatch):
    """Library ids repeat across tests, each in its own database."""
    from services.rules import sweep

    monkeypatch.setattr(sweep, "_reported_unrunnable", {})
    monkeypatch.setattr(sweep, "_reported_skips", {})


@pytest.fixture(name="ready")
def _ready(monkeypatch):
    """Which tools read as ready, by id and purpose; none by default.

    ``ready["probes"]`` counts the probes, and ``ready["on_probe"]``, when
    set, runs on each one: what a plugin that learns its reason while it
    probes (rom-converto) does.
    """
    state: dict = {"convert": set(), "verify": set(), "probes": 0, "on_probe": None}

    async def tool_is_ready(tool, *, purpose: str = "convert") -> bool:
        state["probes"] += 1
        if state["on_probe"] is not None:
            state["on_probe"](tool)
        return tool.id in state[purpose]

    monkeypatch.setattr(registry, "tool_is_ready", tool_is_ready)
    return state


def _skips(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]


async def _store(lib_id: str, rules: dict) -> None:
    await preferences_store.put("rules", {f"{lib_id}:{k}": v for k, v in rules.items()})


async def _library(tree: Path, rules: dict):
    from services import libraries

    lib = await _register(tree)
    await _store(lib["id"], rules)
    return libraries.get(lib["id"])


async def _check_library(tree: Path, platform: str):
    from routes import libraries as routes
    from services import libraries

    lib = await _register(tree)
    await routes.put_library_rules(
        lib["id"], {"check_rules": {platform: {"enabled": True}}},
    )
    return libraries.get(lib["id"])


# ---- the shared text ------------------------------------------------------


def test_the_message_is_the_display_name_and_the_plugins_reason() -> None:
    nsz = registry.get("nsz")
    assert not_ready_message(nsz) == f"Switch isn't ready: {nsz.not_ready_reason}"
    assert "prod.keys" in not_ready_message(nsz)


def test_the_verify_message_reads_the_verify_reason() -> None:
    z3ds = registry.get("z3ds")
    assert z3ds.verify_not_ready_reason != z3ds.not_ready_reason
    assert not_ready_message(z3ds, purpose="verify") == (
        "3DS isn't ready: zstd is not installed, so 3DS files cannot be checked"
    )
    assert not_ready_message(z3ds) == f"3DS isn't ready: {z3ds.not_ready_reason}"


def test_a_reason_that_cannot_be_read_falls_back_to_the_default(monkeypatch) -> None:
    def broken(_self):
        raise RuntimeError("probe state gone")

    monkeypatch.setattr(ChdmanTool, "not_ready_reason", property(broken))
    chdman = registry.get("chdman")
    assert not_ready_message(chdman) == (
        f"CHDMAN isn't ready: {BaseTool.not_ready_reason}"
    )
    assert not_ready_message(chdman, purpose="verify") == (
        f"CHDMAN isn't ready: {BaseTool.not_ready_reason}"
    )


# ---- a conversion Rule ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_conversion_rule_logs_the_tools_own_reason(
    tree: Path, ready, queue, caplog,
) -> None:
    from services.rules import sweep

    (tree / "switch").mkdir()
    (tree / "switch" / "Game.nsp").write_bytes(b"\0" * 1024)
    library = await _library(tree, {"switch": {"enabled": True, "mode": "nsz_compress"}})

    with caplog.at_level(logging.DEBUG):
        results = [await sweep.sweep(library) for _ in range(3)]

    assert queue == []
    for result in results:
        assert result["errors"] == [{"platform_id": "switch", "error": "tool_not_ready"}]
    lines = _skips(caplog)
    assert len(lines) == 1, lines
    nsz = registry.get("nsz")
    assert lines[0].endswith(f"is skipped: Switch isn't ready: {nsz.not_ready_reason}")
    assert "prod.keys" in lines[0]
    assert "not installed" not in lines[0]


@pytest.mark.asyncio
async def test_the_reason_is_read_after_the_readiness_check(
    tree: Path, ready, queue, caplog, monkeypatch,
) -> None:
    """A plugin that learns its reason while it probes (rom-converto caches the
    last probe's answer) is read after the probe, and a reason that changes
    between ticks is said once more, then once."""
    from services.rules import sweep

    chdman = registry.get("chdman")
    reasons = iter(["the first probe", "the first probe", "the second probe",
                    "the second probe", "the second probe"])

    def probe(tool) -> None:
        if tool is chdman:
            monkeypatch.setattr(chdman, "not_ready_reason", next(reasons), raising=False)

    ready["on_probe"] = probe
    library = await _library(tree, {"ps2": {"enabled": True, "mode": "createdvd"}})

    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            await sweep.sweep(library, ignore_schedule=True)

    lines = _skips(caplog)
    assert len(lines) == 2, lines
    assert lines[0].endswith("is skipped: CHDMAN isn't ready: the first probe"), lines
    assert lines[1].endswith("is skipped: CHDMAN isn't ready: the second probe"), lines


@pytest.mark.asyncio
async def test_a_reason_that_raises_does_not_crash_the_tick(
    tree: Path, ready, queue, caplog, monkeypatch,
) -> None:
    from services.rules import sweep

    def broken(_self):
        raise RuntimeError("probe state gone")

    monkeypatch.setattr(ChdmanTool, "not_ready_reason", property(broken))
    library = await _library(tree, {"ps2": {"enabled": True, "mode": "createdvd"}})

    with caplog.at_level(logging.DEBUG):
        result = await sweep.sweep(library)

    assert result["errors"] == [{"platform_id": "ps2", "error": "tool_not_ready"}]
    lines = _skips(caplog)
    assert len(lines) == 1, lines
    assert lines[0].endswith(f"is skipped: CHDMAN isn't ready: {BaseTool.not_ready_reason}")


# ---- a check Rule ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_check_rule_logs_the_verify_reason(
    tree: Path, ready, queue, caplog,
) -> None:
    """z3ds converts without zstd but can't check: the verify reason is said."""
    from services.rules import sweep

    (tree / "3ds").mkdir()
    (tree / "3ds" / "Game.zcci").write_bytes(b"\0" * 1024)
    library = await _check_library(tree, "3ds")
    ready["convert"].add("z3ds")

    with caplog.at_level(logging.DEBUG):
        results = [await sweep.sweep(library, ignore_schedule=True) for _ in range(3)]

    for result in results:
        assert {"platform_id": "3ds", "error": "tool_not_ready"} in result["errors"]
    lines = _skips(caplog)
    assert len(lines) == 1, lines
    assert "check Rule" in lines[0]
    assert lines[0].endswith(
        "is skipped: 3DS isn't ready: zstd is not installed, so 3DS files cannot be checked",
    ), lines
    assert "not installed to check" not in lines[0]


@pytest.mark.asyncio
async def test_a_check_rule_names_each_verifier_with_its_own_reason(
    tree: Path, ready, queue, caplog, monkeypatch,
) -> None:
    from services.rules import sweep

    (tree / "ps2" / "Ico.chd").write_bytes(b"\0" * 1024)
    (tree / "ps2" / "Okami.cso").write_bytes(b"\0" * 1024)
    chdman, maxcso = registry.get("chdman"), registry.get("cso")
    monkeypatch.setattr(chdman, "not_ready_reason", "chdman is gone", raising=False)
    monkeypatch.setattr(maxcso, "not_ready_reason", "maxcso is gone", raising=False)
    library = await _check_library(tree, "ps2")

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            await sweep.sweep(library, ignore_schedule=True)

    lines = _skips(caplog)
    assert len(lines) == 1, lines
    assert lines[0].endswith(
        "is skipped: CHDMAN isn't ready: chdman is gone; CSO isn't ready: maxcso is gone",
    ), lines

    # One of them back: only the other is said, once.
    caplog.clear()
    ready["verify"].add("cso")
    with caplog.at_level(logging.DEBUG):
        for _ in range(2):
            await sweep.sweep(library, ignore_schedule=True)
    lines = _skips(caplog)
    assert len(lines) == 1, lines
    assert lines[0].endswith("is skipped: CHDMAN isn't ready: chdman is gone"), lines
