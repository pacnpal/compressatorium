"""The Convert panel (restructure 2/6, plan item 4): Auto is the way to convert.

The panel plans the selection as it changes and queues that plan through
``/api/jobs/auto``; there is no tool picker, no Auto checkbox and no free-text
output folder. Its moving parts are tested where they live (the store in
``test_frontend_conversion_store``, the listing in
``test_frontend_game_rows_5``); these read the panel for the wiring between
them, which a Node run of a component's script can't follow reactively.
"""
from __future__ import annotations

from pathlib import Path

_LIB = Path(__file__).resolve().parents[1] / "src" / "lib"
_PANEL = (_LIB / "components" / "panels" / "ConvertPanel.svelte").read_text(encoding="utf-8")
_PLAN = (_LIB / "components" / "panels" / "ConvertPlan.svelte").read_text(encoding="utf-8")


def test_the_old_pickers_are_gone():
    for gone in ("ModeSelect", "AutoPlanModal", "DuplicateModal", "conversion.auto",
                 "Output directory", "runAuto", "conversion.submit("):
        assert gone not in _PANEL, gone
    for name in ("panels/ModeSelect.svelte", "modals/AutoPlanModal.svelte"):
        assert not (_LIB / "components" / name).exists(), name


def test_convert_to_lists_the_formats_a_ready_tool_makes_from_the_selection():
    assert "convertTargets({" in _PANEL
    assert "entries: selected.length ? selected : null" in _PANEL
    assert "ui.visibleTools.map((t) => t.id)" in _PANEL
    # Best is the default, and comes back once nothing is selected.
    assert "if (count === 0) untrack(() => conversion.chooseTarget(''));" in _PANEL


def test_the_plan_follows_the_selection_and_stops_above_the_live_limit():
    assert "conversion.keyFor(now)" in _PANEL
    assert "now.length > PLAN_LIVE_LIMIT" in _PANEL
    assert "conversion.requestPlan(now)" in _PANEL
    assert "The plan shows when you press Convert." in _PANEL
    # Unmounting stops a plan on its way and a submit waiting on RomM.
    assert "conversion.cancelPlan();" in _PANEL
    assert "submitAbort?.abort();" in _PANEL


def test_safety_starts_from_the_defaults_and_reaches_the_submit():
    assert "conversion.seedSafety(defaults.values)" in _PANEL
    assert "<SafetyOptions" in _PANEL
    for sent in ("verifyAfter:", "deleteOnVerify:", "containerOnly:", "split:"):
        assert sent in _PANEL, sent
    assert "conversion.submitAuto(snapshot, {" in _PANEL


def test_delete_previews_only_the_groups_that_can_delete():
    assert "registry.specFor(row.mode)?.supportsDeleteOnVerify" in _PANEL
    assert "conversion.fetchDeletePlan(groups, outputDir)" in _PANEL
    assert "<DeletePlanModal" in _PANEL


def test_the_new_files_go_next_to_the_original_or_to_a_picked_folder():
    assert "Next to the original" in _PANEL
    assert "In another folder…" in _PANEL
    assert "<FolderPickerModal" in _PANEL
    assert 'type="text"' not in _PANEL


def test_the_plan_groups_by_console_and_format_and_offers_pick_console():
    assert "go|${row.platform ?? ''}|${format}" in _PLAN
    assert "Pick console" in _PLAN
    assert "Working out the best format…" in _PLAN
    assert "Couldn't plan this: {error}" in _PLAN
    assert "None of these can be converted here." in _PLAN


def test_one_click_starts_one_run_and_queues_what_was_planned():
    """A second click while the plan dialog or the delete preview is open
    starts nothing, and the folder, RomM platform, safety choices, target and
    picked consoles are read before the first await."""
    assert "|| inFlight," in _PANEL
    assert "inFlight = true;" in _PANEL and "inFlight = false;" in _PANEL
    run = _PANEL.split("async function run() {", 1)[1]
    before_await = run.split("await ", 1)[0]
    for read in ("const snapshot = [...paths];", "const outputDir = conversion.outputDir",
                 "const platformId = fileBrowser.rommPlatformId;",
                 "const choice = { ...safety", "platformOverrides: conversion.overridesFor(snapshot)"):
        assert read in before_await, read
    assert "conversion.fetchDeletePlan(groups, outputDir)" in run
    assert "rommPlatformId: platformId," in run
