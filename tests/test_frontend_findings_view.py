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
_VIEW = _SRC / "lib" / "components" / "activity" / "NeedsAttention.svelte"
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
  placeFinding: __call('placeFinding'),
};
"""

_ROWS = """[
  { id: 1, kind: 'platform_conflict', library_id: 'folder:1', library_name: 'roms',
    image_key: 'ps2/Misfiled.iso', folder: '/v/roms/ps2', fingerprint: 'f1', dismissed: false },
  { id: 2, kind: 'unsorted', library_id: 'folder:1', library_name: 'roms',
    image_key: 'Mystery.iso', folder: '/v/roms', fingerprint: 'f2', dismissed: false },
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
const before = {_ROWS};
__answers.listFindings = {{ findings: before, dismissed: 0 }};
__answers.dismissFinding = () => {{
  // The server has it now: the next listing says so.
  __answers.listFindings = {{ findings: before.slice(1), dismissed: 1 }};
  return {{ dismissed: 1 }};
}};
await findings.load();
await findings.dismiss(1);
process.stdout.write(JSON.stringify({{
  ids: findings.list.map((f) => f.id), dismissed: findings.dismissed,
  call: __log.find((c) => c[0] === 'dismissFinding'),
  reloaded: __log.filter((c) => c[0] === 'listFindings').length,
}}));
""")
    assert data["ids"] == [2], data
    assert data["dismissed"] == 1, data
    # The row's fingerprint goes with the id, so a stale row acts on nothing else.
    assert data["call"] == ["dismissFinding", 1, "f1"], data
    # Listed again after the mutation succeeded.
    assert data["reloaded"] == 2, data


def test_showing_a_dismissed_finding_again(tmp_path):
    rows = _ROWS.replace("dismissed: false }", "dismissed: true }", 1)
    data = _run(tmp_path, f"""
__answers.listFindings = {{ findings: {rows}, dismissed: 1 }};
__answers.undismissFinding = () => {{
  __answers.listFindings = {{ findings: {_ROWS}, dismissed: 0 }};
  return {{ undismissed: 1 }};
}};
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
    assert data["call"] == ["undismissFinding", 1, "f1"], data


def test_a_listing_that_started_before_a_dismiss_cannot_undo_it(tmp_path):
    """Codex on PR #322: a late pre-dismiss snapshot must not overwrite the dismiss."""
    data = _run(tmp_path, f"""
const before = {_ROWS};
const pending = [];
__answers.listFindings = () => new Promise((resolve) => pending.push(resolve));
__answers.dismissFinding = {{ dismissed: 1 }};
findings.list = before;
const stale = findings.load();           // started before the dismiss
const dismissing = findings.dismiss(1);  // succeeds, then lists again
await new Promise((r) => setImmediate(r));
pending[1]({{ findings: before.slice(1), dismissed: 1 }});
await dismissing;
pending[0]({{ findings: before, dismissed: 0 }});  // the old snapshot, late
await stale;
process.stdout.write(JSON.stringify({{
  ids: findings.list.map((f) => f.id), dismissed: findings.dismissed,
}}));
""")
    assert data == {"ids": [2], "dismissed": 1}, data


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


_DEFERRED_MAPPINGS = """
const __pending = [];
__answers.listPlatformMappings = () => new Promise((resolve, reject) => {
  __pending.push({ resolve, reject });
});
__answers.listPlatforms = [{ slug: 'psp', name: 'PSP' }, { slug: 'ps2', name: 'PS2' }];
const tick = () => new Promise((r) => setImmediate(r));
const row = (platform) => [{ path: '/v/roms/misc', platform }];
"""


def test_a_response_from_an_earlier_opening_is_ignored(tmp_path):
    """each opening of the dialog owns its own read.

    Opened, closed and opened again on the same folder: the first read
    (``ps2``, from before a save) answers after the second (``psp``). The
    dialog shows ``psp``.
    """
    data = _run(tmp_path, _DEFERRED_MAPPINGS + """
findings.openMapping('/v/roms/misc', 'misc');
await tick();
findings.closeMapping();
findings.openMapping('/v/roms/misc', 'misc');
await tick();
__pending[1].resolve(row('psp'));
await tick();
__pending[0].resolve(row('ps2'));
await tick();
process.stdout.write(JSON.stringify({
  current: findings.mappingCurrent, state: findings.mappingState,
}));
""")
    assert data == {"current": "psp", "state": "loaded"}, data


def test_an_error_from_an_earlier_opening_is_ignored(tmp_path):
    """The same ownership check for a failure: an old read's error is dropped."""
    data = _run(tmp_path, _DEFERRED_MAPPINGS + """
findings.openMapping('/v/roms/misc', 'misc');
await tick();
findings.openMapping('/v/roms/misc', 'misc');
await tick();
__pending[1].resolve(row('psp'));
await tick();
__pending[0].reject(new Error('stale failure'));
await tick();
process.stdout.write(JSON.stringify({
  current: findings.mappingCurrent, state: findings.mappingState,
  error: findings.mappingError,
}));
""")
    assert data == {"current": "psp", "state": "loaded", "error": None}, data


def test_a_failed_mapping_read_is_shown_and_can_be_retried(tmp_path):
    """"couldn't load" is not "no mapping", and Retry reads again."""
    data = _run(tmp_path, _DEFERRED_MAPPINGS + """
findings.openMapping('/v/roms/misc', 'misc');
await tick();
__pending[0].reject(new Error('Failed to list the mappings'));
await tick();
const failed = {
  current: findings.mappingCurrent, state: findings.mappingState, error: findings.mappingError,
};
findings.loadCurrentMapping();
await tick();
__pending[1].resolve(row('psp'));
await tick();
process.stdout.write(JSON.stringify({
  failed, retried: { current: findings.mappingCurrent, state: findings.mappingState },
}));
""")
    assert data["failed"] == {
        "current": None, "state": "error", "error": "Failed to list the mappings",
    }, data
    assert data["retried"] == {"current": "psp", "state": "loaded"}, data


def test_the_mapping_dialog_shows_a_read_failure_with_retry():
    modal = _MODAL.read_text(encoding="utf-8")
    assert "findings.mappingState === 'error'" in modal
    assert "findings.loadCurrentMapping()" in modal
    assert ".catch(() => {})" not in modal


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


def test_findings_live_in_activity():
    """Restructure item 8: Findings are Activity's Needs attention tab."""
    activity = (_SRC / "lib" / "components" / "views" / "ActivityView.svelte").read_text(
        encoding="utf-8",
    )
    assert "<NeedsAttention" in activity
    assert "<ActivityView />" in _APP.read_text(encoding="utf-8")
    assert "view: 'activity'" in _SIDEBAR.read_text(encoding="utf-8")


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
    # Restructure PR 4: what's wrong in one sentence, then the fix. First and
    # Last seen move into the row's title.
    for column in ("Library", "Game", "Problem"):
        assert f'role="columnheader">{column}<' in view, column
    for gone in ("First seen", "Last seen", "Kind<"):
        assert f'role="columnheader">{gone}' not in view, gone
    assert "row.first_seen" in view and "row.last_seen" in view


def test_the_mapping_dialog_is_mounted_once_and_offers_clearing():
    assert "<MappingModal" in _APP.read_text(encoding="utf-8")
    modal = _MODAL.read_text(encoding="utf-8")
    assert "findings.setMapping(" in modal
    assert "null" in modal


def test_a_folder_row_offers_set_platform():
    menu = _ROW_MENU.read_text(encoding="utf-8")
    assert "findings.openMapping(" in menu
    assert "Set Platform" in menu


def test_nothing_in_the_findings_ui_deletes_or_moves_a_file_unasked():
    """Only two ways to touch a file, each one a person's choice: Place it
    (the backend's checked move) and Delete through the confirming dialog."""
    for path in (_VIEW, _MODAL, _STORE):
        src = path.read_text(encoding="utf-8")
        for call in ("deleteFile", "renameFile", "bulkDelete"):
            assert call not in src, (path.name, call)
    view = _VIEW.read_text(encoding="utf-8")
    assert view.count("ui.deleteTarget =") == 1
    assert "<DeleteModal" not in view  # mounted once, by App
    assert "api.placeFinding(" in _STORE.read_text(encoding="utf-8")


def test_the_empty_state_does_not_claim_everything_has_a_platform():
    view = _VIEW.read_text(encoding="utf-8")
    assert "Every Image in your folder Libraries has a Platform" not in view
    # An empty list because everything is dismissed says so.
    assert "findings.dismissed" in view


def test_each_row_action_names_its_image():
    view = _VIEW.read_text(encoding="utf-8")
    # Named through `findingName`: the Image key, or a duplicate group's first
    # copy and a count (#388), never a bare content key.
    assert "ariaLabel={`Open the folder of ${findingName(row)}`}" in view
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
    columns = re.search(r"\.row \{[^}]*grid-template-columns:([^;]+);", view, re.DOTALL).group(1)
    assert "auto" not in columns, columns
    assert 'role="columnheader" class="sr-only"' not in view


def test_the_table_fits_beside_the_sidebar_at_the_widest_stacked_break():
    """At 1181px (just above the stacked layout) the fixed tracks must fit.

    The first fixed-track version summed wider than the table, and the action
    buttons were clipped at the right edge of the screenshot.
    """
    view = _VIEW.read_text(encoding="utf-8")
    columns = re.search(r"\.row \{[^}]*grid-template-columns:([^;]+);", view, re.DOTALL).group(1)
    tracks = re.findall(r"minmax\(([\d.]+)rem[^)]*\)|([\d.]+)rem", columns)
    minimum_px = sum(float(a or b) for a, b in tracks) * 16
    gaps_px = (len(tracks) - 1) * 12
    row_padding_px = 2 * 14
    breakpoint = int(re.search(r"@media \(max-width: (\d+)px\)", view).group(1))
    # Viewport just above the break, minus the sidebar (240px) and the view's
    # side padding (2 x 24px).
    available_px = breakpoint + 1 - 240 - 48
    assert minimum_px + gaps_px + row_padding_px <= available_px, (
        minimum_px + gaps_px + row_padding_px, available_px,
    )


# ----------------------------------------------------------------------
# Place it (plan item 13)
# ----------------------------------------------------------------------

_INBOX_ROW = """[
  { id: 7, kind: 'inbox_unsorted', library_id: 'folder:1', library_name: 'roms',
    image_key: 'inbox:1/Drop.iso', path: '/v/dl/Drop.iso', folder: '/v/dl',
    inbox_name: 'dl', relative: 'Drop.iso', fingerprint: 'f7', dismissed: false },
]"""


def test_placing_sends_the_console_and_takes_the_row_off(tmp_path):
    data = _run(tmp_path, f"""
__answers.listFindings = {{ findings: {_INBOX_ROW}, dismissed: 0 }};
__answers.placeFinding = () => {{
  __answers.listFindings = {{ findings: [], dismissed: 0 }};
  return {{ path: '/v/dl/ps2/Drop.iso', platform: 'ps2' }};
}};
await findings.load();
const placed = await findings.place(7, 'ps2');
process.stdout.write(JSON.stringify({{
  placed, ids: findings.list.map((f) => f.id),
  call: __log.find((c) => c[0] === 'placeFinding'),
}}));
""")
    assert data["call"] == ["placeFinding", 7, "ps2"], data
    assert data["placed"]["platform"] == "ps2", data
    assert data["ids"] == [], data


def test_a_refused_place_keeps_the_row_and_says_why(tmp_path):
    data = _run(tmp_path, f"""
__answers.listFindings = {{ findings: {_INBOX_ROW}, dismissed: 0 }};
__answers.placeFinding = new Error('psx/Drop.iso is already in the watch folder');
await findings.load();
let why = null;
try {{ await findings.place(7, 'psx'); }} catch (e) {{ why = e.message; }}
process.stdout.write(JSON.stringify({{ why, ids: findings.list.map((f) => f.id) }}));
""")
    assert data["why"] == "psx/Drop.iso is already in the watch folder", data
    assert data["ids"] == [7], data


def test_needs_attention_speaks_plain_words_and_offers_one_fix_per_kind():
    view = _VIEW.read_text(encoding="utf-8")
    assert "KIND_LABEL" not in view
    for kind, label in (
        ("unsorted", "Unknown console"), ("platform_conflict", "Console unclear"),
        ("not_intact", "Failed its check"), ("source_kept", "Original kept"),
        ("duplicate", "Duplicate"), ("romm_swap", "RomM update unfinished"),
        ("rename_conflict", "Name unclear"),
        ("inbox_unsorted", "Unknown console (watch folder)"),
        ("inbox_conflict", "Console unclear (watch folder)"),
        ("inbox_dat_conflict", "Name unclear (watch folder)"),
        ("inbox_failed", "Couldn't take it in"),
    ):
        entry = re.search(rf"^\s*{kind}: \{{ label: (['\"])(.*?)\1", view, re.MULTILINE)
        assert entry and entry.group(2) == label, kind
    # Place it: a console select and the button, on the two watch-folder kinds.
    assert "This is a…" in view and "Place it" in view
    assert "findings.place(" in view
    assert "Placing ${" in view
    # Never the internal key of a watch-folder file.
    assert "row.inbox_name" in view and "row.relative" in view
    # The failed conversions of the last day, linked to History.
    assert "since: '24h'" in view and "#/activity/history?status=failed" in view


# ----------------------------------------------------------------------
# Activity (restructure PR 4): the summary, Running and History
# ----------------------------------------------------------------------

_ACTIVITY = _SRC / "lib" / "components" / "activity"


def test_the_summary_strip_has_four_stats_each_a_link():
    src = (_ACTIVITY / "ActivitySummary.svelte").read_text(encoding="utf-8")
    for label in ("Needs you", "Running", "Space saved", "Checked"):
        assert f"label: '{label}'" in src, label
    assert "findings.openCount" in src and "ledger.savings" in src
    assert "verification.statuses.size" in src and "?status=checked" in src
    assert not (_SRC / "lib" / "components" / "dashboard").exists()


def test_running_is_the_queue_only():
    src = (_ACTIVITY / "RunningList.svelte").read_text(encoding="utf-8")
    assert "jobs.runningList" in src and "ui.showCancelAll = true" in src
    for gone in ("'completed'", "'failed'", "Show metadata jobs", "MAX_JOB_HISTORY", "Clear"):
        assert gone not in src, gone
    assert 'title="Nothing running"' in src and "Open Files" in src


def test_history_reads_its_status_from_the_url_and_names_the_check():
    src = (_ACTIVITY / "HistoryList.svelte").read_text(encoding="utf-8")
    for column in ("When", "Game", "Console", "Result", "Check"):
        assert f"<th>{column}</th>" in src, column
    for option in ("All", "Done", "Failed", "Checked", "Cancelled"):
        assert f"label: '{option}'" in src, option
    assert "get('status')" in src and "checked: status === 'checked'" in src
    for shown in ("✓ Exact copy", "✓ Checked", "✓ Known good dump"):
        assert shown in src, shown
    assert "Nothing has run yet." in src and "Nothing matches." in src and "Clear filters" in src


# ----------------------------------------------------------------------
# What the deleted views offered, kept; no double submits; no state lost
# ----------------------------------------------------------------------


def test_every_problem_row_still_opens_its_folder():
    """The old Findings view had Open folder on every row: it stays, after the fix."""
    view = _VIEW.read_text(encoding="utf-8")
    assert "{#if about.fix !== 'duplicates' || row.cause?.copies?.length}" not in view
    assert view.count("fileBrowser.openFolder(row.folder)") == 1
    assert "{#if row.folder}" in view


def test_a_row_takes_one_action_at_a_time():
    view = _VIEW.read_text(encoding="utf-8")
    for action in ("async function place(row)", "async function dismiss(row)",
                   "async function undismiss(row)"):
        body = view.split(action, 1)[1].split("\n  }\n", 1)[0]
        assert "if (busy[row.id]) return;" in body, action
        assert "finally" in body, action


def test_the_console_picked_survives_a_tab_switch(tmp_path):
    view = _VIEW.read_text(encoding="utf-8")
    assert "findings.placeChoices" in view and "let picked" not in view
    data = _run(tmp_path, """
findings.placeChoices[7] = 'ps2';
process.stdout.write(JSON.stringify({ kept: findings.placeChoices[7] }));
""")
    assert data["kept"] == "ps2"


def test_a_library_filter_from_the_url_is_always_an_option():
    view = _VIEW.read_text(encoding="utf-8")
    assert "libraryFilter && !seen.has(libraryFilter)" in view


def test_history_keeps_its_filters_and_shows_the_all_time_totals():
    src = (_ACTIVITY / "HistoryList.svelte").read_text(encoding="utf-8")
    assert "<script module>" in src and "const kept = $state(" in src
    assert "ledger.totals" in src


def test_the_summary_keeps_the_savings_split_and_a_running_check():
    src = (_ACTIVITY / "ActivitySummary.svelte").read_text(encoding="utf-8")
    assert "savings?.by_platform" in src and "savings?.by_format" in src
    assert "verification.batchRun" in src


def test_delete_loads_the_real_file_entry_so_the_check_still_applies():
    """DeleteModal asks to acknowledge an unverified
    replacement from the entry's `outputs`. Needs attention loads the file's
    real listing entry first; if it can't, Delete doesn't open."""
    view = _VIEW.read_text(encoding="utf-8")
    body = view.split("async function remove(row)", 1)[1].split("\n  }\n", 1)[0]
    assert "await api.listFiles(" in body
    assert "(e) => e.path === row.path" in body
    assert "ui.deleteTarget = { ...entry" in body
    assert "toast.error(" in body
    # Only the loaded entry reaches the dialog: nothing else assigns it.
    assert view.count("ui.deleteTarget =") == 1
