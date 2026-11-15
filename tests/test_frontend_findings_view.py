"""Guards for the Findings view and folder Platform mappings (#304).

The store's bookkeeping is asserted behaviourally by evaluating
``src/lib/stores/findings.svelte.js`` with Node (runes reduced, the API
stubbed), as ``test_frontend_rules_view`` does for the Libraries store. The
wiring between components is asserted against the source.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from .test_frontend_rules_view import _find_node

_SRC = Path(__file__).resolve().parents[1] / "src"
_STORE = _SRC / "lib" / "stores" / "findings.svelte.js"
_VIEW = _SRC / "lib" / "components" / "views" / "FindingsView.svelte"
_MODAL = _SRC / "lib" / "components" / "modals" / "MappingModal.svelte"
_ROW_MENU = _SRC / "lib" / "components" / "panels" / "RowActionsMenu.svelte"
_APP = _SRC / "App.svelte"
_UI = _SRC / "lib" / "stores" / "ui.svelte.js"
_SIDEBAR = _SRC / "lib" / "components" / "layout" / "Sidebar.svelte"


def _plain_js() -> str:
    src = _STORE.read_text(encoding="utf-8")
    kept = [
        line for line in src.splitlines()
        if not (line.strip().startswith("import ") and line.strip().endswith(";"))
    ]
    body = re.sub(r"\$state\(", "(", "\n".join(kept))
    leftover = [r for r in ("$state(", "$derived(", "$effect(") if r in body]
    assert not leftover, f"findings.svelte.js grew runes this harness does not reduce: {leftover}"
    return body.replace("export const ", "const ").replace("export class ", "class ")


_STUBS = """
const __log = [];
const __answers = {};
const __call = (name) => async (...args) => {
  __log.push([name, ...args]);
  const answer = __answers[name];
  if (answer instanceof Error) throw answer;
  return typeof answer === 'function' ? answer(...args) : answer;
};
const api = {
  listFindings: __call('listFindings'),
  dismissFinding: __call('dismissFinding'),
  undismissFinding: __call('undismissFinding'),
  checkFindings: __call('checkFindings'),
  listPlatforms: __call('listPlatforms'),
  setPlatformMapping: __call('setPlatformMapping'),
  listPlatformMappings: __call('listPlatformMappings'),
};
"""

_ROWS = """[
  { id: 1, kind: 'platform_conflict', library_id: 'folder:1', library_name: 'roms',
    image_key: 'ps2/Misfiled.iso', folder: '/v/roms/ps2', dismissed: false },
  { id: 2, kind: 'unsorted', library_id: 'folder:1', library_name: 'roms',
    image_key: 'Mystery.iso', folder: '/v/roms', dismissed: false },
]"""


def _run(tmp_path: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the Findings store")
    script = tmp_path / "findings_store_eval.mjs"
    script.write_text(_STUBS + _plain_js() + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the Findings store via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_loading_lists_the_findings_and_the_dismissed_count(tmp_path):
    data = _run(tmp_path, f"""
__answers.listFindings = {{ findings: {_ROWS}, dismissed: 3 }};
await findings.load();
process.stdout.write(JSON.stringify({{
  ids: findings.list.map((f) => f.id), dismissed: findings.dismissed, call: __log[0],
}}));
""")
    assert data["ids"] == [1, 2], data
    assert data["dismissed"] == 3, data
    assert data["call"] == ["listFindings", False], data


def test_dismissing_takes_the_finding_off_the_list(tmp_path):
    data = _run(tmp_path, f"""
__answers.listFindings = {{ findings: {_ROWS}, dismissed: 0 }};
__answers.dismissFinding = {{ dismissed: 1 }};
await findings.load();
await findings.dismiss(1);
process.stdout.write(JSON.stringify({{
  ids: findings.list.map((f) => f.id), dismissed: findings.dismissed,
  call: __log.find((c) => c[0] === 'dismissFinding'),
}}));
""")
    assert data["ids"] == [2], data
    assert data["dismissed"] == 1, data
    assert data["call"] == ["dismissFinding", 1], data


def test_showing_a_dismissed_finding_again(tmp_path):
    rows = _ROWS.replace("dismissed: false }", "dismissed: true }", 1)
    data = _run(tmp_path, f"""
__answers.listFindings = {{ findings: {rows}, dismissed: 1 }};
__answers.undismissFinding = {{ undismissed: 1 }};
findings.showDismissed = true;
await findings.load();
await findings.undismiss(1);
process.stdout.write(JSON.stringify({{
  rows: findings.list.map((f) => [f.id, f.dismissed]), dismissed: findings.dismissed,
  call: __log.find((c) => c[0] === 'undismissFinding'),
}}));
""")
    assert data["rows"] == [[1, False], [2, False]], data
    assert data["dismissed"] == 0, data
    assert data["call"] == ["undismissFinding", 1], data


def test_showing_dismissed_findings_asks_for_them(tmp_path):
    data = _run(tmp_path, """
__answers.listFindings = { findings: [], dismissed: 0 };
findings.showDismissed = true;
await findings.load();
process.stdout.write(JSON.stringify({ call: __log[0] }));
""")
    assert data["call"] == ["listFindings", True], data


def test_setting_a_mapping_saves_it_then_reloads_the_findings(tmp_path):
    data = _run(tmp_path, """
__answers.setPlatformMapping = { library_id: 'folder:1', folder: 'misc', platform: 'psp' };
__answers.listFindings = { findings: [], dismissed: 0 };
const saved = await findings.setMapping('/v/roms/misc', 'psp');
process.stdout.write(JSON.stringify({ saved, calls: __log.map((c) => c[0]), first: __log[0] }));
""")
    assert data["first"] == ["setPlatformMapping", "/v/roms/misc", "psp"], data
    assert data["calls"] == ["setPlatformMapping", "listFindings"], data
    assert data["saved"]["platform"] == "psp", data


def test_a_mapping_saved_during_a_walk_leaves_findings_pending(tmp_path):
    data = _run(tmp_path, """
__answers.setPlatformMapping = {
  library_id: 'folder:1', folder: 'misc', platform: 'psp',
  walk: { id: 'folder:1', status: 'listing_in_progress',
          message: 'A listing is running; Findings refresh after it.' },
};
__answers.listFindings = { findings: [], dismissed: 0 };
await findings.setMapping('/v/roms/misc', 'psp');
const pending = findings.pending;
await findings.load();
process.stdout.write(JSON.stringify({ pending, after: findings.pending }));
""")
    assert data["pending"] == "A listing is running; Findings refresh after it.", data
    # Pending holds until the walk is known to have finished (a Check now or
    # a later load that the user asks for clears it).
    assert data["after"] == data["pending"], data


def test_check_now_clears_the_pending_note(tmp_path):
    data = _run(tmp_path, """
__answers.checkFindings = { libraries: [{ id: 'folder:1', status: 'ok' }] };
__answers.listFindings = { findings: [], dismissed: 0 };
findings.pending = 'A listing is running; Findings refresh after it.';
await findings.check();
process.stdout.write(JSON.stringify({ pending: findings.pending }));
""")
    assert data["pending"] is None, data


def test_an_older_listing_never_replaces_a_newer_one(tmp_path):
    """Rapid "Show dismissed" toggles: only the last request's answer is kept."""
    data = _run(tmp_path, """
const waits = [];
__answers.listFindings = (include) => new Promise((resolve) => waits.push([include, resolve]));
findings.showDismissed = true;
const first = findings.load();
findings.showDismissed = false;
const second = findings.load();
waits[1][1]({ findings: [{ id: 2, dismissed: false }], dismissed: 1 });
await second;
waits[0][1]({ findings: [{ id: 1, dismissed: true }, { id: 2, dismissed: false }], dismissed: 1 });
await first;
process.stdout.write(JSON.stringify({ ids: findings.list.map((f) => f.id) }));
""")
    assert data["ids"] == [2], data


def test_the_mapping_dialog_knows_the_folders_current_platform(tmp_path):
    data = _run(tmp_path, """
__answers.listPlatformMappings = [
  { library_id: 'folder:1', folder: 'misc', path: '/v/roms/misc', platform: 'psp' },
];
const here = await findings.currentMapping('/v/roms/misc');
const none = await findings.currentMapping('/v/roms/other');
process.stdout.write(JSON.stringify({ here, none }));
""")
    assert data == {"here": "psp", "none": None}, data


def test_check_now_walks_then_reloads(tmp_path):
    data = _run(tmp_path, """
__answers.checkFindings = { libraries: [{ id: 'folder:1', status: 'ok' }] };
__answers.listFindings = { findings: [], dismissed: 0 };
await findings.check();
process.stdout.write(JSON.stringify({ calls: __log.map((c) => c[0]), checking: findings.checking }));
""")
    assert data["calls"] == ["checkFindings", "listFindings"], data
    assert data["checking"] is False, data


def test_a_failed_load_keeps_the_error(tmp_path):
    data = _run(tmp_path, """
__answers.listFindings = new Error('boom');
await findings.load();
process.stdout.write(JSON.stringify({ error: findings.error, loading: findings.loading }));
""")
    assert data == {"error": "boom", "loading": False}, data


def test_platforms_are_fetched_once(tmp_path):
    data = _run(tmp_path, """
__answers.listPlatforms = [{ slug: 'psp', name: 'PlayStation Portable' }];
await findings.loadPlatforms();
await findings.loadPlatforms();
process.stdout.write(JSON.stringify({
  n: __log.filter((c) => c[0] === 'listPlatforms').length, platforms: findings.platforms,
}));
""")
    assert data["n"] == 1, data
    assert data["platforms"] == [{"slug": "psp", "name": "PlayStation Portable"}], data


# ----------------------------------------------------------------------
# wiring
# ----------------------------------------------------------------------


def test_findings_is_a_view_of_its_own():
    assert "'findings'" in _UI.read_text(encoding="utf-8")
    assert "FindingsView" in _APP.read_text(encoding="utf-8")
    assert "go('findings')" in _SIDEBAR.read_text(encoding="utf-8")


def test_each_finding_links_to_the_actions_that_fix_it():
    view = _VIEW.read_text(encoding="utf-8")
    # Open the folder in the file browser, set a mapping, dismiss.
    assert "fileBrowser.openFolder(" in view
    assert "findings.openMapping(" in view
    assert "findings.dismiss(" in view
    # A dismissed row (shown with "Show dismissed") can be shown again.
    assert "findings.undismiss(" in view
    assert "Show again" in view
    # A mapping saved while a walk runs says the Findings are pending.
    assert "findings.pending" in view
    for column in ("Library", "Image", "Kind", "First seen", "Last seen"):
        assert column in view, column


def test_the_mapping_dialog_is_mounted_once_and_offers_clearing():
    assert "<MappingModal" in _APP.read_text(encoding="utf-8")
    modal = _MODAL.read_text(encoding="utf-8")
    assert "findings.setMapping(" in modal
    assert "null" in modal


def test_a_folder_row_offers_set_platform():
    menu = _ROW_MENU.read_text(encoding="utf-8")
    assert "findings.openMapping(" in menu
    assert "Set Platform" in menu


def test_nothing_in_the_findings_ui_deletes_or_moves_a_file():
    for path in (_VIEW, _MODAL, _STORE):
        src = path.read_text(encoding="utf-8")
        for call in ("deleteFile", "renameFile", "bulkDelete", "deleteTarget"):
            assert call not in src, (path.name, call)


def test_the_empty_state_does_not_claim_everything_has_a_platform():
    view = _VIEW.read_text(encoding="utf-8")
    assert "Every Image in your folder Libraries has a Platform" not in view
    # An empty list because everything is dismissed says so.
    assert "findings.dismissed" in view


def test_each_row_action_names_its_image():
    view = _VIEW.read_text(encoding="utf-8")
    assert "ariaLabel={`Open the folder of ${row.image_key}`}" in view
    assert "ariaLabel" in (_SRC / "lib" / "components" / "ui" / "Button.svelte").read_text(
        encoding="utf-8",
    )


def test_check_now_names_libraries_and_counts_them_right():
    view = _VIEW.read_text(encoding="utf-8")
    assert "could not be checked" in view
    assert "Libraries could not be checked" in view



def test_the_header_row_lines_up_with_the_rows():
    """Each row is its own grid: every track must be sized the same in every row.

    An `auto` track sizes to that row's content, and a visually hidden
    (absolutely positioned) header cell takes no track at all, so the header
    drifted left of the rows in the first screenshots.
    """
    view = _VIEW.read_text(encoding="utf-8")
    columns = re.search(r"\.row \{[^}]*grid-template-columns:([^;]+);", view, re.S).group(1)
    assert "auto" not in columns, columns
    assert 'role="columnheader" class="sr-only"' not in view
