"""Guards for the Rules view (#303), following the RomM view tests.

The Rules view edits one Library's Rules through a *rules source*: the RomM
store for RomM, ``FolderRulesSource`` for a folder Library. The store's
bookkeeping is asserted behaviourally by evaluating
``src/lib/stores/libraries.svelte.js`` with Node (runes reduced, sibling
stores stubbed), as ``test_frontend_romm_store`` does for the RomM store. The
wiring between components is asserted against the source.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_STORE = _SRC / "lib" / "stores" / "libraries.svelte.js"
_RULES_SOURCE = _SRC / "lib" / "rules" / "rulesSource.svelte.js"
_ROMM_STORE = _SRC / "lib" / "stores" / "romm.svelte.js"
_SAVE_QUEUE = _SRC / "lib" / "stores" / "latestSaveQueue.js"
_VIEWS = _SRC / "lib" / "components" / "views"
_EDITOR = _VIEWS / "RuleEditor.svelte"
_RULES_VIEW = _VIEWS / "RulesView.svelte"
_ROMM_VIEW = _VIEWS / "RommView.svelte"
_ROW_MENU = _SRC / "lib" / "components" / "panels" / "RowActionsMenu.svelte"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _plain_js() -> str:
    # FolderRulesSource extends the shared RulesSource, which has no imports.
    queue = _SAVE_QUEUE.read_text(encoding="utf-8").replace("export class ", "class ")
    src = (
        queue + "\n" + _RULES_SOURCE.read_text(encoding="utf-8") + "\n"
        + _STORE.read_text(encoding="utf-8")
    )
    kept = [
        line for line in src.splitlines()
        if not (line.strip().startswith("import ") and line.strip().endswith(";"))
    ]
    body = "\n".join(kept)
    body = re.sub(r"\$state\(", "(", body)
    leftover = [r for r in ("$state(", "$derived(", "$effect(") if r in body]
    assert not leftover, f"libraries.svelte.js grew runes this harness does not reduce: {leftover}"
    return body.replace("export const ", "const ").replace("export class ", "class ")


_STUBS = """
const __calls = [];
const __log = [];
const __defer = (name) => (...args) => new Promise((resolve, reject) => {
  __log.push([name, ...args]);
  __calls.push({ resolve, reject });
});
const api = {
  listLibraries: __defer('listLibraries'),
  saveLibraryRules: __defer('saveLibraryRules'),
  getLibraryRules: __defer('getLibraryRules'),
  previewLibrary: __defer('previewLibrary'),
  runLibrary: __defer('runLibrary'),
  removeLibrary: __defer('removeLibrary'),
  updateLibrary: __defer('updateLibrary'),
  forgetLibraryConverted: __defer('forgetLibraryConverted'),
  getLibraryPlatforms: __defer('getLibraryPlatforms'),
  registerLibrary: __defer('registerLibrary'),
};
const romm = { isRomm: true };
const ui = { navigated: null, navigate(v) { this.navigated = v; } };
const tick = () => new Promise((resolve) => setImmediate(resolve));
"""


def _run(tmp_path: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the Libraries store")
    script = tmp_path / "libraries_store_eval.mjs"
    script.write_text(_STUBS + _plain_js() + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the Libraries store via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


_FOLDER = "{ id: 'folder:3', kind: 'folder', name: 'roms', root: '/v/roms' }"


def test_a_folder_rule_edited_during_a_save_is_not_discarded(tmp_path):
    """The same guard the RomM store has: a save an edit overtook keeps the edit."""
    data = _run(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
source.setRule('ngc', {{ mode: 'dolphin_rvz', enabled: true }});
const saving = source.saveRules();
await tick();
source.setRule('ngc', {{ mode: 'dolphin_rvz', enabled: false }});
__calls[0].resolve({{ rules: {{ ngc: {{ mode: 'dolphin_rvz', enabled: true }} }} }});
await saving;
process.stdout.write(JSON.stringify({{
  rules: source.rules, dirty: source.rulesDirty, call: __log[0],
}}));
""")
    assert data["rules"]["ngc"]["enabled"] is False, data
    assert data["dirty"] is True, data
    # Saved against the folder Library's own endpoint, by its id.
    assert data["call"][0:2] == ["saveLibraryRules", "folder:3"], data


def test_a_listing_older_than_a_settings_save_keeps_the_saved_settings(tmp_path):
    """Codex 4577832447: a stale read cannot flip a saved schedule switch back.

    A Platform listing starts, the schedule switch is saved on, then the
    listing answers with the Library as it was before the save. The switch
    stays on, and the listing's Platforms are still taken.
    """
    data = _run(tmp_path, f"""
const loadOnce = libraries.load();
await tick();
__calls[0].resolve([{{ ...{_FOLDER}, schedule_enabled: false }}]);
await loadOnce;
const source = libraries.sourceFor('folder:3');
const listing = source.loadPlatforms();
await tick();
const saving = source.setScheduleEnabled(true);
await tick();
__calls[2].resolve({{ ...{_FOLDER}, schedule_enabled: true }});
await saving;
__calls[1].resolve({{
  platforms: [{{ id: 'ngc', name: 'GameCube', images: 2 }}],
  library: {{ ...{_FOLDER}, schedule_enabled: false }},
}});
await listing;
process.stdout.write(JSON.stringify({{
  scheduled: source.scheduleEnabled,
  row: libraries.list.find((lib) => lib.id === 'folder:3').schedule_enabled,
  platforms: source.platforms.map((p) => p.id),
}}));
""")
    assert data["scheduled"] is True, data
    assert data["row"] is True, data
    assert data["platforms"] == ["ngc"], data


def test_a_list_load_older_than_a_settings_save_keeps_the_saved_settings(tmp_path):
    """The same guard on a Library list refresh that started before the save."""
    data = _run(tmp_path, f"""
const loadOnce = libraries.load();
await tick();
__calls[0].resolve([{{ ...{_FOLDER}, schedule_enabled: false }}]);
await loadOnce;
const source = libraries.sourceFor('folder:3');
const reload = libraries.load();
await tick();
const saving = source.setScheduleEnabled(true);
await tick();
__calls[2].resolve({{ ...{_FOLDER}, schedule_enabled: true }});
await saving;
__calls[1].resolve([{{ ...{_FOLDER}, schedule_enabled: false }}]);
await reload;
process.stdout.write(JSON.stringify({{
  scheduled: source.scheduleEnabled,
  row: libraries.list.find((lib) => lib.id === 'folder:3').schedule_enabled,
}}));
""")
    assert data["scheduled"] is True, data
    assert data["row"] is True, data


def test_a_library_registered_during_a_list_load_is_kept(tmp_path):
    """CodeRabbit 4578050384: an older catalogue cannot drop a new Library.

    A list load starts, a folder is registered and selected, then the load
    answers with the catalogue as it was before: the new Library stays, and
    so does the selection.
    """
    data = _run(tmp_path, f"""
const loading = libraries.load();
await tick();
const registering = libraries.register('/v/new');
await tick();
__calls[1].resolve({{ id: 'folder:9', kind: 'folder', name: 'new', root: '/v/new' }});
await registering;
libraries.select('folder:9');
__calls[0].resolve([{_FOLDER}]);
await loading;
process.stdout.write(JSON.stringify({{
  ids: libraries.list.map((lib) => lib.id), selected: libraries.selectedId,
}}));
""")
    assert data["ids"] == ["folder:3", "folder:9"], data
    assert data["selected"] == "folder:9", data


def test_settings_saves_reach_the_backend_in_click_order(tmp_path):
    """CodeRabbit 4578050386 and 4578075312: one save per Library at a time.

    On, then off. The off save is not sent until the on save has answered,
    so the backend applies them in click order, and the switch shows off,
    as the backend holds it.
    """
    data = _run(tmp_path, f"""
const loadOnce = libraries.load();
await tick();
__calls[0].resolve([{{ ...{_FOLDER}, schedule_enabled: false }}]);
await loadOnce;
const source = libraries.sourceFor('folder:3');
const first = source.setScheduleEnabled(true);
await tick();
const second = source.setScheduleEnabled(false);
await tick();
const sentBeforeFirstAnswered = __log.filter((c) => c[0] === 'updateLibrary').length;
__calls[1].resolve({{ ...{_FOLDER}, schedule_enabled: true }});
await first;
await tick();
const secondSent = __log[2];
__calls[2].resolve({{ ...{_FOLDER}, schedule_enabled: false }});
await second;
process.stdout.write(JSON.stringify({{
  sentBeforeFirstAnswered, secondSent,
  scheduled: source.scheduleEnabled,
  row: libraries.list.find((lib) => lib.id === 'folder:3').schedule_enabled,
}}));
""")
    assert data["sentBeforeFirstAnswered"] == 1, data
    assert data["secondSent"] == [
        "updateLibrary", "folder:3", {"schedule_enabled": False},
    ], data
    assert data["scheduled"] is False, data
    assert data["row"] is False, data


def test_a_failed_latest_save_reloads_the_row(tmp_path):
    """The second save fails while the first is in flight: the row is reloaded.

    On, then off. Off waits for on, which lands (not adopted, it is no longer
    the latest); then off is sent and fails. With the queue drained, the
    store reads the Library back and shows what the backend holds: on.
    """
    data = _run(tmp_path, f"""
const loadOnce = libraries.load();
await tick();
__calls[0].resolve([{{ ...{_FOLDER}, schedule_enabled: false }}]);
await loadOnce;
const source = libraries.sourceFor('folder:3');
const first = source.setScheduleEnabled(true);
await tick();
const second = source.setScheduleEnabled(false).catch((e) => e.message);
await tick();
const sentBeforeFirstAnswered = __log.filter((c) => c[0] === 'updateLibrary').length;
__calls[1].resolve({{ ...{_FOLDER}, schedule_enabled: true }});
await first;
await tick();
__calls[2].reject(new Error('save failed'));
await tick();
const reloadCall = __log[3];
__calls[3].resolve([{{ ...{_FOLDER}, schedule_enabled: true }}]);
const error = await second;
process.stdout.write(JSON.stringify({{
  sentBeforeFirstAnswered, error, reloadCall, scheduled: source.scheduleEnabled,
  row: libraries.list.find((lib) => lib.id === 'folder:3').schedule_enabled,
}}));
""")
    assert data["sentBeforeFirstAnswered"] == 1, data
    assert data["error"] == "save failed", data
    assert data["reloadCall"] == ["listLibraries"], data
    assert data["scheduled"] is True, data
    assert data["row"] is True, data


def test_a_listing_answered_after_removal_does_not_bring_the_library_back(tmp_path):
    """Codex 4576901574: a late response for a removed Library is ignored.

    Start a Platform listing, remove the Library, then the listing answers
    with its Library row: the picker stays empty and the source is dropped.
    """
    data = _run(tmp_path, f"""
const loadOnce = libraries.load();
await tick();
__calls[0].resolve([{_FOLDER}]);
await loadOnce;
const source = libraries.sourceFor('folder:3');
const listing = source.loadPlatforms();
await tick();
const removing = libraries.remove('folder:3');
await tick();
__calls[2].resolve({{}});
await removing;
__calls[1].resolve({{ platforms: [], library: {{ ...{_FOLDER}, schedule_enabled: true }} }});
await listing;
libraries.adopt({{ ...{_FOLDER}, name: 'late update' }});
process.stdout.write(JSON.stringify({{
  list: libraries.list, source: libraries.sourceFor('folder:3'),
  scheduled: source.scheduleEnabled,
}}));
""")
    assert data["list"] == [], data
    assert data["source"] is None, data
    assert data["scheduled"] is False, data


def test_a_read_that_started_before_a_save_is_dropped(tmp_path):
    """CodeRabbit 4576895559: a stale loadRules() response cannot undo a save.

    The read of configuration A starts, B is saved and lands, then A's response
    arrives: the editor keeps B.
    """
    data = _run(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
const loading = source.loadRules();
await tick();
source.setRule('ngc', {{ mode: 'dolphin_wia', enabled: true }});
const saving = source.saveRules();
await tick();
__calls[1].resolve({{ rules: {{ ngc: {{ mode: 'dolphin_wia', enabled: true }} }} }});
await saving;
__calls[0].resolve({{
  rules: {{ ngc: {{ mode: 'dolphin_rvz', enabled: true }} }}, state: {{ ngc: {{ x: 1 }} }},
}});
await loading;
process.stdout.write(JSON.stringify({{
  rules: source.rules, dirty: source.rulesDirty, state: source.ruleState,
}}));
""")
    assert data["rules"]["ngc"]["mode"] == "dolphin_wia", data
    assert data["dirty"] is False, data


def test_saves_reach_the_backend_in_the_order_they_were_made(tmp_path):
    """CodeRabbit 4576895561: one save at a time per source, in invocation order.

    Save A, edit, save B: B is not sent until A has answered, so an older
    save can never land after a newer one. B then submits the latest rules.
    """
    data = _run(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
source.setRule('ngc', {{ mode: 'dolphin_rvz', enabled: true }});
const first = source.saveRules();
await tick();
source.setRule('ngc', {{ mode: 'dolphin_wia', enabled: true }});
const second = source.saveRules();
await tick();
const sentBeforeFirstAnswered = __log.length;
const savingWhileQueued = source.rulesSaving;
__calls[0].resolve({{ rules: {{ ngc: {{ mode: 'dolphin_rvz', enabled: true }} }} }});
await first;
await tick();
const secondSent = __log[1];
__calls[1].resolve({{ rules: {{ ngc: {{ mode: 'dolphin_wia', enabled: true }} }} }});
await second;
process.stdout.write(JSON.stringify({{
  sentBeforeFirstAnswered, savingWhileQueued, secondSent,
  rules: source.rules, dirty: source.rulesDirty, saving: source.rulesSaving,
}}));
""")
    assert data["sentBeforeFirstAnswered"] == 1, data
    assert data["savingWhileQueued"] is True, data
    assert data["secondSent"][2]["ngc"]["mode"] == "dolphin_wia", data
    assert data["rules"]["ngc"]["mode"] == "dolphin_wia", data
    assert data["dirty"] is False, data
    assert data["saving"] is False, data


def test_a_refreshed_list_updates_a_cached_source_and_keeps_its_edits(tmp_path):
    """Codex 4576870640: a cached source follows the Library's refreshed settings.

    The schedule switch flipped elsewhere (another tab) shows after
    ``libraries.load()``, and the unsaved Rule edit survives the refresh.
    """
    data = _run(tmp_path, f"""
const loadOnce = libraries.load();
await tick();
__calls[0].resolve([{{ ...{_FOLDER}, schedule_enabled: false }}]);
await loadOnce;
const source = libraries.sourceFor('folder:3');
source.setRule('ngc', {{ mode: 'dolphin_rvz', enabled: true }});
const before = source.scheduleEnabled;

const loadAgain = libraries.load();
await tick();
__calls[1].resolve([{{ ...{_FOLDER}, schedule_enabled: true }}]);
await loadAgain;
process.stdout.write(JSON.stringify({{
  before, after: source.scheduleEnabled,
  same: libraries.sourceFor('folder:3') === source,
  rules: source.rules, dirty: source.rulesDirty,
}}));
""")
    assert data["before"] is False, data
    assert data["after"] is True, data
    assert data["same"] is True, data
    assert data["rules"]["ngc"] == {"mode": "dolphin_rvz", "enabled": True}, data
    assert data["dirty"] is True, data


def test_the_platforms_response_updates_the_library(tmp_path):
    """``loadPlatforms`` adopts the Library its response carries."""
    data = _run(tmp_path, f"""
const loadOnce = libraries.load();
await tick();
__calls[0].resolve([{{ ...{_FOLDER}, schedule_enabled: false }}]);
await loadOnce;
const source = libraries.sourceFor('folder:3');
source.setRule('ngc', {{ mode: 'dolphin_rvz', enabled: true }});
const listing = source.loadPlatforms();
await tick();
__calls[1].resolve({{
  platforms: [], unsorted: 0, conflicted: 0, unreadable: 0,
  library: {{ ...{_FOLDER}, schedule_enabled: true }},
}});
await listing;
process.stdout.write(JSON.stringify({{
  scheduled: source.scheduleEnabled,
  row: libraries.list.find((lib) => lib.id === 'folder:3').schedule_enabled,
  dirty: source.rulesDirty,
}}));
""")
    assert data["scheduled"] is True, data
    assert data["row"] is True, data
    assert data["dirty"] is True, data


def test_folder_rules_offer_what_the_backend_allows(tmp_path):
    """The duplicate policies come from the Library's options: skip only for a folder."""
    data = _run(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
const loading = source.loadRules();
await tick();
__calls[0].resolve({{
  rules: {{}}, state: {{}}, defaults: {{ duplicate_action: 'skip' }},
  options: {{ orders: ['name'], duplicate_actions: ['skip'], days: [0] }},
}});
await loading;
process.stdout.write(JSON.stringify({{
  policies: source.ruleOptions.duplicate_actions,
  datWarning: source.losesDatMatch({{ outputExt: '.rvz' }}),
}}));
""")
    assert data["policies"] == ["skip"], data
    assert data["datWarning"] is False, data


def test_preview_calls_preview_and_run_calls_run(tmp_path):
    data = _run(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
const preview = source.previewSweep();
await tick();
__calls[0].resolve({{ dry_run: true, queued: 2 }});
await preview;
process.stdout.write(JSON.stringify({{ log: __log, result: source.sweepResult }}));
""")
    assert [call[0] for call in data["log"]] == ["previewLibrary"], data
    assert data["log"][0][1] == "folder:3", data
    assert data["result"]["dry_run"] is True, data


def test_the_rules_view_falls_back_to_romm(tmp_path):
    """A selected Library that is gone (removed elsewhere) falls back to RomM."""
    data = _run(tmp_path, f"""
libraries.select('folder:9');
const loading = libraries.load();
await tick();
__calls[0].resolve([{{ id: 'romm', kind: 'romm' }}, {_FOLDER}]);
await loading;
const fellBack = libraries.selectedId;
libraries.select('folder:3');
const kept = libraries.selectedId;
const isFolderSource = libraries.sourceFor('folder:3') instanceof FolderRulesSource;
const rommSource = libraries.sourceFor('romm').isRomm === true;
const removing = libraries.remove('folder:3');
await tick();
__calls[1].resolve({{}});
await removing;
process.stdout.write(JSON.stringify({{
  fellBack, kept, isFolderSource, rommSource,
  afterRemove: libraries.selectedId, left: libraries.list.map((l) => l.id),
}}));
""")
    assert data["fellBack"] == "romm", data
    assert data["kept"] == "folder:3", data
    assert data["isFolderSource"] is True, data
    assert data["rommSource"] is True, data
    assert data["afterRemove"] == "romm", data
    assert data["left"] == ["romm"], data


def test_counts_say_one_image_and_two_images(tmp_path):
    data = _run(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
process.stdout.write(JSON.stringify([source.countLabel(1), source.countLabel(2)]));
""")
    assert data == ["1 Image", "2 Images"]


def test_held_images_are_counted_and_released(tmp_path):
    """Images held after a failed conversion can be seen and retried (round 2, N3)."""
    data = _run(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
source.ruleState = {{ ps2: {{ failed: {{ 'ps2/Ico.iso': {{ fingerprint: 'x' }} }} }} }};
const before = source.heldCountFor('ps2');
const none = source.heldCountFor('ngc');
const forgetting = source.forgetConverted(['ps2']);
await tick();
__calls[0].resolve({{ cleared: 1, state: {{ ps2: {{}} }} }});
await forgetting;
process.stdout.write(JSON.stringify({{
  before, none, after: source.heldCountFor('ps2'), call: __log[0],
}}));
""")
    assert data["before"] == 1, data
    assert data["none"] == 0, data
    assert data["after"] == 0, data
    assert data["call"] == ["forgetLibraryConverted", "folder:3", ["ps2"]], data


def test_the_editor_shows_held_images_whatever_the_policy():
    src = _EDITOR.read_text(encoding="utf-8")
    assert "r.skipped_failed" in src, "the sweep summary drops held Images"
    block = re.search(
        r"\{#if source\.heldCountFor\(platform\.id\) > 0\}(.*?)\{/if\}", src, re.DOTALL,
    )
    assert block, "no held-Images control in the editor"
    assert "retryFailed(platform.id)" in block.group(1)
    # Not nested under the overwrite/rename-only history block.
    before = src[: block.start()]
    assert before.rfind("{#if rule.duplicate_action !== 'skip'}") < before.rfind("{/if}")


def test_a_listing_still_running_is_a_wait_not_an_error(tmp_path):
    data = _run(tmp_path, f"""
const source = new FolderRulesSource({_FOLDER});
const api2 = api;
api2.getLibraryPlatforms = async () => {{
  const err = new Error('listing_in_progress');
  err.status = 409;
  throw err;
}};
await source.loadPlatforms();
const busy = {{ busy: source.platformsBusy, error: source.platformsError }};
api2.getLibraryPlatforms = async () => ({{ platforms: [{{ id: 'ps2', images: 1 }}] }});
await source.loadPlatforms();
process.stdout.write(JSON.stringify({{
  ...busy, after: {{ busy: source.platformsBusy, n: source.platforms.length }},
}}));
""")
    assert data["busy"] is True, data
    assert data["error"] is None, data
    assert data["after"] == {"busy": False, "n": 1}, data


def test_the_rules_view_offers_a_retry_while_a_listing_runs():
    src = _RULES_VIEW.read_text(encoding="utf-8")
    block = re.search(r"\{:else if source\.platformsBusy\}(.*?)\{:else", src, re.DOTALL)
    assert block, "no neutral state for a listing still running"
    assert "Listing still running, try again shortly" in block.group(1)
    assert "source.loadPlatforms()" in block.group(1)
    assert "error" not in block.group(1), "the wait is styled as an error"


def test_the_sweep_summary_names_every_reason_a_library_gives():
    """Round 3: a mount blip and a running listing each have their own words."""
    src = _EDITOR.read_text(encoding="utf-8")
    labels = re.search(r"const SWEEP_ERROR_LABELS = \{(.*?)\n  \};", src, re.DOTALL)
    assert labels, "SWEEP_ERROR_LABELS moved"
    for code in ("root_unavailable", "listing_in_progress"):
        assert f"{code}:" in labels.group(1), f"no label for {code}"
    assert "still running, try again shortly" in labels.group(1)
    assert "r.stopped_reason === 'not_configured'" in src
    # PR #320 (Codex security): a sheet whose tracks leave the volumes.
    assert "r.skipped_unsafe" in src


def test_the_deferred_romm_load_stops_once_the_view_is_gone():
    """Codex 4576551537: the RomM view's lifecycle guard, after every await."""
    src = _RULES_VIEW.read_text(encoding="utf-8")
    body = re.search(r"async function loadRomm\(\) \{(.*?)\n  \}", src, re.DOTALL)
    assert body, "loadRomm moved"
    load = body.group(1)
    first_await = load.index("await ")
    guard = load.find("if (!alive) return;", first_await)
    enters = load.index("enteredRomm = true;")
    platforms = load.index("romm.loadPlatforms()")
    # Checked after the first await and before anything enters RomM mode.
    assert first_await < guard < enters < platforms, load
    assert "alive = false;" in src, "the view never marks itself gone"


def test_folder_endpoints_read_the_librarys_own_id():
    """Codacy: no constructor-scoped closure; the endpoints read the reactive library."""
    src = _STORE.read_text(encoding="utf-8")
    assert "const id = () =>" not in src
    assert "api.getLibraryRules(this.id)" in src


def test_the_rules_screenshot_finds_its_row_by_platform_id():
    """The shot opens the ps2 row by id; rows show display names, not slugs."""
    editor = _EDITOR.read_text(encoding="utf-8")
    assert "data-platform-id={platform.id}" in editor
    shots = (_SRC.parent / "shots.yml").read_text(encoding="utf-8")
    assert """.row-toggle[data-platform-id="ps2"]""" in shots
    assert "b.textContent.includes('ps2')" not in shots


def test_a_deep_link_change_reselects_while_the_view_stays_mounted():
    """Codex 4576629013: #/rules/folder:1 -> #/rules/folder:2 (or Back) reselects."""
    src = _RULES_VIEW.read_text(encoding="utf-8")
    effect = re.search(
        r"\$effect\(\(\) => \{\s*if \(embedded\) return;\s*"
        r"const target = ui\.rulesTarget;(.*?)\n  \}\);",
        src, re.DOTALL,
    )
    assert effect, "the selection does not follow ui.rulesTarget"
    assert "libraries.select(target)" in effect.group(1)
    # Not only once on mount.
    assert "else if (ui.rulesTarget) libraries.select(ui.rulesTarget);" not in src


def test_both_libraries_share_one_rules_source():
    """The Rule bookkeeping exists once (fix round 1, I1)."""
    folder = _STORE.read_text(encoding="utf-8")
    romm = _ROMM_STORE.read_text(encoding="utf-8")
    assert "class FolderRulesSource extends RulesSource" in folder
    assert "class RommStore extends RulesSource" in romm
    for store in (folder, romm):
        for method in ("async saveRules(", "async loadRules(", "async runSweep(", "setRule("):
            assert method not in store, f"{method} is copied into a store again"


def test_opening_rules_on_a_library_navigates_there(tmp_path):
    data = _run(tmp_path, """
libraries.open('folder:3');
process.stdout.write(JSON.stringify({ selected: libraries.selectedId, view: ui.navigated }));
""")
    assert data == {"selected": "folder:3", "view": "rules"}, data


# ----------------------------------------------------------------------
# wiring, asserted against the source
# ----------------------------------------------------------------------


def test_the_romm_entry_point_opens_the_rules_view_on_romm():
    romm_view = _ROMM_VIEW.read_text(encoding="utf-8")
    assert "<RulesView embedded />" in romm_view, (
        "the RomM view's Automation tab no longer opens the Rules view"
    )
    rules_view = _RULES_VIEW.read_text(encoding="utf-8")
    assert "if (embedded) libraries.select(ROMM_LIBRARY_ID);" in rules_view, (
        "the embedded Rules view does not open on the RomM Library"
    )


def test_the_editor_reads_policies_from_the_library():
    """No hard-coded policy list the folder Library could not honour."""
    src = _EDITOR.read_text(encoding="utf-8")
    assert "source.ruleOptions?.duplicate_actions" in src
    assert not re.search(r"\bromm\.[a-zA-Z]+\(", src), (
        "the editor reaches past its source into the RomM store"
    )


def test_the_rules_view_has_a_library_picker():
    src = _RULES_VIEW.read_text(encoding="utf-8")
    assert re.search(r'<Select\s+label="Library"', src), "the Library picker is gone"
    assert "onchange={pick}" in src
    # Inside the RomM view, another Library opens the full Rules view.
    assert "if (embedded && id !== ROMM_LIBRARY_ID) libraries.open(id);" in src


def test_folders_offer_register_as_a_library():
    src = _ROW_MENU.read_text(encoding="utf-8")
    assert "Register as a Library" in src
    assert "entry?.type === 'directory' && !inArchive" in src, (
        "Register as a Library is offered on something other than a folder"
    )
    assert "libraries.register(path)" in src


def test_rules_is_a_view_of_its_own():
    ui = (_SRC / "lib" / "stores" / "ui.svelte.js").read_text(encoding="utf-8")
    assert "'rules'" in ui.split("const VIEWS", 1)[1].split("\n", 1)[0]
    sidebar = (_SRC / "lib" / "components" / "layout" / "Sidebar.svelte").read_text(
        encoding="utf-8",
    )
    assert "go('rules')" in sidebar
