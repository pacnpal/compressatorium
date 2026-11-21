"""What the Rule sanitizer drops or refuses is logged where a sweep runs it (#477).

``normalize_rule`` runs on every read of the Rules: each GET, the editor,
startup and every scheduler tick. It used to log each thing it dropped or
refused (a codec the mode can't use, an unknown mode, a mode that can't run
unattended, a refused output folder or delete-on-verify, a bad timezone or
filter) on each of those reads, under ``romm_auto:`` and without naming the
Library or the Platform. A Rule stored before #474 logged a line a minute.

The sanitizer now only records what it changed on the Rule. A sweep that runs
the Rule logs it once, naming the Library and the Platform, under a prefix
that fits the Library. A Rule it refuses outright is never run, so the sweep
reports it once per stored value instead.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from services.preferences_store import preferences_store

from .test_folder_library_303 import _db_fixture, _ready, _register, _tree
from .test_rules_codec_check_474 import _queue

__all__ = ["_db_fixture", "_queue", "_ready", "_tree"]

# One stored Rule for each thing the sanitizer drops or refuses.
_STORED = {
    # A codec Create DVD can't use (#474): dropped, the Rule still runs.
    "ps2": {"enabled": True, "mode": "createdvd", "compression": "cdzl"},
    # A mode no tool provides: the Rule is refused outright.
    "ngc": {"enabled": True, "mode": "not_a_real_mode"},
    # A mode that can't run unattended: refused outright.
    "psx": {"enabled": True, "mode": "copy"},
    # Delete-on-verify on a check that is only structural: refused.
    "wiiu": {
        "enabled": True, "mode": "jwud_compress", "compression": "noverify",
        "delete_on_verify": True,
    },
    # A timezone the host has no data for: UTC instead.
    "dc": {"enabled": True, "mode": "cso_compress", "timezone": "Mars/Olympus_Mons"},
    # A filter that does not compile: refused, and the Rule paused.
    "psp": {"enabled": True, "mode": "cso_compress", "include_pattern": "(USA"},
}


@pytest.fixture(autouse=True)
def _nothing_reported_yet(monkeypatch):
    """Each test starts from a process that has reported nothing: Library ids
    repeat across tests, each in its own database."""
    from services.rules import sweep

    monkeypatch.setattr(sweep, "_reported_unrunnable", {})


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


async def _store(lib_id: str, rules: dict, **others: dict) -> None:
    """*rules* as *lib_id*'s stored Rules, and *others* as other Libraries'."""
    stored = {f"{lib_id}:{k}": v for k, v in rules.items()}
    for other, more in others.items():
        stored.update({f"{other}:{k}": v for k, v in more.items()})
    await preferences_store.put("rules", stored)


@pytest.mark.asyncio
async def test_reading_the_rules_logs_nothing_however_often(tree: Path, caplog) -> None:
    from routes import libraries as routes
    from routes import libraries as lib_routes

    lib = await _register(tree)
    await _store(lib["id"], _STORED, romm={
        "9": {"enabled": True, "mode": "createdvd", "compression": "cdzl"},
        "10": {"enabled": True, "mode": "not_a_real_mode"},
    })

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            rules = (await routes.get_library_rules(lib["id"]))["rules"]
            await lib_routes.get_library_rules("romm")

    assert _warnings(caplog) == []
    # What was dropped is still on the Rule, for the editor.
    assert rules["ps2"]["dropped_codecs"] == ["cdzl"]
    assert rules["wiiu"]["unsafe_delete_on_verify"] is True
    assert rules["dc"]["timezone"] == "UTC"
    assert rules["dc"]["dropped_timezone"] == "Mars/Olympus_Mons"
    assert rules["psp"]["invalid_pattern"] is True
    assert "ngc" not in rules and "psx" not in rules


@pytest.mark.asyncio
async def test_a_scheduler_tick_that_runs_nothing_logs_nothing(
    tree: Path, ready, queue, caplog,
) -> None:
    """A tick sweeps the Library, and a Rule that isn't due isn't run: no line."""
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    # Enabled, on no weekday: never due on the schedule.
    await _store(lib["id"], {
        "ps2": {"enabled": True, "mode": "createdvd", "compression": "cdzl", "days": []},
    })
    library = libraries.get(lib["id"])

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            await sweep.sweep(library)

    assert queue == []
    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_sweep_that_runs_a_rule_with_a_dropped_codec_logs_one_line(
    tree: Path, ready, queue, caplog,
) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    await _store(lib["id"], {
        "ps2": {"enabled": True, "mode": "createdvd", "compression": "cdzl"},
    })

    with caplog.at_level(logging.DEBUG):
        await routes.run_library(lib["id"], routes.SweepRequest(platform_ids=["ps2"]))

    assert ("Ico.iso", None) in queue
    lines = [m for m in _warnings(caplog) if "cdzl" in m]
    assert len(lines) == 1, _warnings(caplog)
    [line] = lines
    # The Library, by name, and the Platform; a folder Library's prefix.
    assert "roms" in line and "ps2" in line and "Create DVD" in line
    assert line.startswith("folder_library:"), line
    assert "romm_auto" not in line

    # Once per sweep: the next run logs it once more, not once per read.
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await routes.get_library_rules(lib["id"])
        await routes.run_library(lib["id"], routes.SweepRequest(platform_ids=["ps2"]))
    assert len([m for m in _warnings(caplog) if "cdzl" in m]) == 1, _warnings(caplog)


@pytest.mark.asyncio
async def test_each_change_a_run_rule_had_is_named_on_its_line(
    tree: Path, ready, queue, caplog,
) -> None:
    """Refused delete-on-verify and a dropped timezone are reported the same way."""
    from routes import libraries as routes

    lib = await _register(tree)
    await _store(lib["id"], {
        "ps2": {
            "enabled": True, "mode": "createdvd", "compression": "cdzl",
            "timezone": "Mars/Olympus_Mons",
        },
    })

    with caplog.at_level(logging.DEBUG):
        await routes.run_library(lib["id"], routes.SweepRequest(platform_ids=["ps2"]))

    lines = [m for m in _warnings(caplog) if "ps2" in m and "roms" in m]
    assert len(lines) == 1, _warnings(caplog)
    assert "cdzl" in lines[0] and "Mars/Olympus_Mons" in lines[0]


@pytest.mark.asyncio
async def test_a_rule_refused_outright_is_reported_once_until_it_changes(
    tree: Path, ready, queue, caplog,
) -> None:
    """No sweep runs a Rule the sanitizer refuses, so the sweep reports it once
    for each stored value: not on every tick, and again once it changes."""
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    await _store(lib["id"], {
        "ngc": {"enabled": True, "mode": "not_a_real_mode"},
        "psx": {"enabled": True, "mode": "copy"},
    })
    library = libraries.get(lib["id"])

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            await sweep.sweep(library)

    lines = _warnings(caplog)
    assert len(lines) == 2, lines
    [unknown] = [m for m in lines if "not_a_real_mode" in m]
    [unattended] = [m for m in lines if "'copy'" in m]
    for line in (unknown, unattended):
        assert "roms" in line and line.startswith("folder_library:"), line
    assert "ngc" in unknown and "psx" in unattended

    caplog.clear()
    await _store(lib["id"], {
        "ngc": {"enabled": True, "mode": "another_unreal_mode"},
        "psx": {"enabled": True, "mode": "copy"},
    })
    with caplog.at_level(logging.DEBUG):
        await sweep.sweep(library)
        await sweep.sweep(library)

    lines = _warnings(caplog)
    assert len(lines) == 1 and "another_unreal_mode" in lines[0], lines


@pytest.mark.asyncio
async def test_a_rule_paused_for_a_refused_value_is_reported_once(
    tree: Path, ready, queue, caplog,
) -> None:
    """A Rule paused for a filter it refused is skipped by every sweep, so it
    is reported the same way as one refused outright: once, until fixed."""
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    await _store(lib["id"], {
        "ps2": {"enabled": True, "mode": "createdvd", "include_pattern": "(USA"},
    })
    library = libraries.get(lib["id"])

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            await sweep.sweep(library)

    assert queue == []
    lines = _warnings(caplog)
    assert len(lines) == 1, lines
    assert "ps2" in lines[0] and "roms" in lines[0] and "paused" in lines[0]

    # Fixed, then broken again: reported again.
    caplog.clear()
    await _store(lib["id"], {"ps2": {"enabled": False, "mode": "createdvd"}})
    await sweep.sweep(library)
    await _store(lib["id"], {
        "ps2": {"enabled": True, "mode": "createdvd", "include_pattern": "(USA"},
    })
    with caplog.at_level(logging.DEBUG):
        await sweep.sweep(library)
    assert len(_warnings(caplog)) == 1, _warnings(caplog)


@pytest.mark.asyncio
async def test_a_romm_rule_is_reported_under_romms_own_prefix(
    db: Path, ready, queue, caplog,
) -> None:
    from services.romm.library import romm_library
    from services.rules import sweep

    await _store("romm", {"10": {"enabled": True, "mode": "not_a_real_mode"}})

    with caplog.at_level(logging.DEBUG):
        # RomM isn't configured here, so the sweep ends there: after the Rules
        # were read, which is where a Rule no sweep runs is reported.
        await sweep.sweep(romm_library)
        await sweep.sweep(romm_library)

    lines = _warnings(caplog)
    assert len(lines) == 1, lines
    assert lines[0].startswith("romm_auto:") and "RomM" in lines[0] and "10" in lines[0]


# ---- what the save changed survives the round trip (Codex on #479) ---------
#
# A save stores the Rule as the sanitizer left it: `timezone: "UTC"`, delete
# off, no output folder. Re-reading that finds nothing to drop, so a field the
# sanitizer recorded at the save is gone by the sweep. Each change is reported
# exactly once over save, read and sweep: by the save when a re-read loses it,
# by the sweep when the stored Rule still carries it.

_ROUND_TRIPS = [
    # (platform, submitted rule, what the line names)
    ("ps2", {"mode": "createdvd", "timezone": "Mars/Olympus_Mons"}, "Mars/Olympus_Mons"),
    ("wiiu", {
        "mode": "jwud_compress", "compression": "noverify", "delete_on_verify": True,
    }, "delete-on-verify"),
    ("ps2", {"mode": "createdvd", "output_dir": "/somewhere/else"}, "/somewhere/else"),
    ("ps2", {"mode": "createdvd", "include_pattern": "(USA"}, "filter pattern"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("platform", "submitted", "named"), _ROUND_TRIPS)
async def test_a_saved_change_is_reported_once_over_save_read_and_sweep(
    tree: Path, ready, queue, caplog, platform: str, submitted: dict, named: str,
) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    with caplog.at_level(logging.DEBUG):
        await routes.put_library_rules(
            lib["id"], {"rules": {platform: {"enabled": True, **submitted}}},
        )
        for _ in range(2):
            await routes.get_library_rules(lib["id"])
        await routes.run_library(lib["id"], routes.SweepRequest(platform_ids=[platform]))
        await routes.run_library(lib["id"], routes.SweepRequest())

    lines = [m for m in _warnings(caplog) if named in m]
    assert len(lines) == 1, _warnings(caplog)
    assert platform in lines[0] and "roms" in lines[0], lines[0]
    assert lines[0].startswith("folder_library:"), lines[0]


@pytest.mark.asyncio
async def test_codecs_dropped_by_a_lenient_save_are_reported_once(
    tree: Path, ready, queue, caplog,
) -> None:
    """The editor's save refuses the codec (#474); a lenient save (RomM's
    ``set_rules``) drops it, and stores the Rule without it."""
    from routes import libraries as routes
    from services import libraries
    from services.rules import sweep

    lib = await _register(tree)
    library = libraries.get(lib["id"])
    with caplog.at_level(logging.DEBUG):
        await sweep.set_rules(library, {
            "ps2": {"enabled": True, "mode": "createdvd", "compression": "cdzl"},
        })
        await routes.get_library_rules(lib["id"])
        await routes.run_library(lib["id"], routes.SweepRequest(platform_ids=["ps2"]))

    lines = [m for m in _warnings(caplog) if "cdzl" in m]
    assert len(lines) == 1, _warnings(caplog)
    assert "ps2" in lines[0] and "roms" in lines[0], lines[0]


@pytest.mark.asyncio
async def test_a_check_rules_dropped_timezone_is_reported_once_from_its_save(
    tree: Path, caplog,
) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    with caplog.at_level(logging.DEBUG):
        await routes.put_library_rules(lib["id"], {"check_rules": {
            "ps2": {"enabled": False, "timezone": "Mars/Olympus_Mons"},
        }})
        await routes.get_library_rules(lib["id"])

    lines = [m for m in _warnings(caplog) if "Mars/Olympus_Mons" in m]
    assert len(lines) == 1, _warnings(caplog)
    assert "check" in lines[0] and "ps2" in lines[0], lines[0]
