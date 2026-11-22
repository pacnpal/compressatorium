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
    for read in ("const snapshot = [...paths];", "let outputDir = conversion.outputDir",
                 "const platformId = fileBrowser.rommPlatformId;",
                 "let choice = { ...safety", "platformOverrides: conversion.overridesFor(snapshot)",
                 "submitAbort = controller;"):
        assert read in before_await, read
    # A big selection takes the options shown with its plan, once confirmed.
    assert "if (!(await waiting) || controller.signal.aborted) return;" in run
    assert "{@render optionsBody()}" in _PANEL.split("<BaseModal", 1)[1]
    assert "conversion.fetchDeletePlan(groups, outputDir)" in run
    assert "rommPlatformId: platformId," in run


def test_a_delete_preview_never_opens_after_the_panel_is_gone():
    """the run's abort is checked between the preview
    request and the confirmation."""
    run = _PANEL.split("async function run() {", 1)[1]
    after_fetch = run.split("conversion.fetchDeletePlan(groups, outputDir)", 1)[1]
    guard = after_fetch.split("ui.deletePlanPromptOpen = true;", 1)[0]
    assert "if (controller.signal.aborted) {" in guard
    assert "conversion.clearDeletePlan();" in guard


def test_cancelling_the_folder_picker_keeps_next_to_the_original():
    """the other-folder radio only opens the picker;
    it reads as chosen once a folder is."""
    assert "e.preventDefault();\n            setWhere('folder');" in _PANEL
    assert "checked={!!outputDir}" in _PANEL


def test_a_reloading_romm_catalog_keeps_the_picked_target():
    """A RomM catalog that reloads keeps the picked target."""
    effect = _PANEL.split("// A RomM catalog reloading offers nothing for a moment", 1)[1]
    assert "if (catalogLoading) return;" in effect.split("untrack", 1)[0]


def test_leaving_the_view_answers_an_open_dialog():
    """Unmounting mid-dialog answers it "no", so `ui.planPromptOpen` and
    `ui.deletePlanPromptOpen` never stay set and hold App's refreshes."""
    teardown = _PANEL.split("onDestroy(() => {", 1)[1].split("});", 1)[0]
    assert "if (planPrompt) resolvePlanPrompt(false);" in teardown
    assert "if (deletePrompt) resolveDeletePlan(false);" in teardown
    assert "ui.planPromptOpen = false;" in _PANEL
    assert "ui.deletePlanPromptOpen = false;" in _PANEL


def test_a_big_plan_dialog_plans_again_when_its_options_change():
    """above the live limit the plan stays live while its
    dialog is open, and Convert takes the plan as it is when confirmed."""
    effect = _PANEL.split("const key = conversion.keyFor(now);", 1)[1].split("});", 1)[0]
    assert "now.length > PLAN_LIVE_LIMIT && !planPrompt" in effect
    run = _PANEL.split("async function run() {", 1)[1]
    confirmed = run.split("if (!(await waiting) || controller.signal.aborted) return;", 1)[1]
    assert "conversion.hasPlanFor(snapshot) ? conversion.plan : null" in confirmed
    assert "outputDir = conversion.outputDir || null;" in confirmed


def test_the_big_plan_dialog_offers_pick_console_too():
    """the dialog's plan gets the same Pick console
    callbacks as the inline plan; a pick re-plans through the live effect."""
    dialog = _PANEL.split("<BaseModal", 1)[1].split("</BaseModal>", 1)[0]
    plan = dialog.split("<ConvertPlan", 1)[1].split("/>", 1)[0]
    for prop in ("onoverride={(list, slug) => conversion.setOverride(list, slug)}",
                 "{mapFolder}", "onmap={openMapping}"):
        assert prop in plan, prop


def test_a_big_plan_confirm_takes_the_picks_made_in_its_dialog():
    """Convert to and Pick console are re-read with the
    folder and safety choices once the dialog is confirmed."""
    run = _PANEL.split("async function run() {", 1)[1]
    confirmed = run.split("if (!(await waiting) || controller.signal.aborted) return;", 1)[1]
    confirmed = confirmed.split("const rows = ", 1)[0]
    assert "target: conversion.target || null," in confirmed
    assert "platformOverrides: conversion.overridesFor(snapshot)," in confirmed


def test_the_options_stay_mounted_while_the_plan_is_made_again():
    """a compression edit re-plans; the Options keep
    showing the last plan's pickers meanwhile, so none unmounts mid-edit."""
    rows = "const optionRows = $derived((shownPlan ?? []).filter((row) => row.mode));"
    assert rows in _PANEL
    counts = _PANEL.split("const modeCounts = $derived.by(() => {", 1)[1].split("});", 1)[0]
    pickers = _PANEL.split("const pickers = $derived.by(() => {", 1)[1].split("});", 1)[0]
    assert "for (const row of optionRows)" in counts
    assert "for (const row of optionRows)" in pickers
