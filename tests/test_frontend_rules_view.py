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
    src = _RULES_SOURCE.read_text(encoding="utf-8") + "\n" + _STORE.read_text(encoding="utf-8")
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
