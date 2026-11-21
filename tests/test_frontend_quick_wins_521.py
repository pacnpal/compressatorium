"""Quick wins, contrast and plain details (#521, plan items A1, A3 and B7).

The stores and pure helpers are evaluated with Node through the harnesses the
Rules view and tool-availability tests already use; the markup and CSS fixes
are asserted against the source. Skipped where Node is unavailable.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_rules_view import _FOLDER, _find_node
from tests.test_frontend_rules_view import _run as _run_libraries
from tests.test_frontend_tool_availability_496 import _run as _run_ui

_SRC = Path(__file__).resolve().parents[1] / "src"
_LIB = _SRC / "lib"
_COMPONENTS = _LIB / "components"


def _read(*parts: str) -> str:
    return _SRC.joinpath(*parts).read_text(encoding="utf-8")


def _eval(tmp_path: Path, imports: str, body: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "eval.mjs"
    script.write_text(imports + body, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ── A1: broken pointers and small bugs ─────────────────────────────────────


def test_every_view_has_a_top_bar_title(tmp_path):
    data = _run_ui(tmp_path, """
process.stdout.write(JSON.stringify({ views: [...VIEWS], titles: VIEW_TITLES }));
""")
    # The four places plus Help (#534); the old views redirect into them.
    assert data["views"] == ["files", "libraries", "activity", "settings", "help"], data
    for view in data["views"]:
        assert data["titles"].get(view), f"{view} has no top-bar title"
    assert "VIEW_TITLES[view]" in _read("lib", "components", "layout", "TopBar.svelte")


def test_navigating_to_a_library_keeps_its_id_in_the_url(tmp_path):
    data = _run_ui(tmp_path, """
globalThis.window = { location: { hash: '' } };
const store = new UIStore();
store.navigate('libraries', { id: 'folder:3' });
const hash = window.location.hash;
store.applyHash(hash);
process.stdout.write(JSON.stringify({ hash, target: store.libraryTarget }));
""")
    assert data == {"hash": "#/libraries/folder%3A3", "target": "folder:3"}, data


def test_opening_a_library_passes_its_id_to_the_router(tmp_path):
    data = _run_libraries(tmp_path, """
ui.navigate = function (v, opts) { this.navigated = [v, opts]; };
libraries.open('folder:3');
process.stdout.write(JSON.stringify({ view: ui.navigated }));
""")
    assert data["view"] == ["libraries", {"id": "folder:3", "tab": None}], data


def test_the_rules_view_skips_romm_when_it_is_not_set_up(tmp_path):
    data = _run_libraries(tmp_path, f"""
const loading = libraries.load();
await tick();
__calls[0].resolve([{{ id: 'romm', kind: 'romm' }}, {_FOLDER}]);
await loading;
libraries.openOnConfigured(true);
const configured = libraries.selectedId;
libraries.openOnConfigured(false);
const unconfigured = libraries.selectedId;
process.stdout.write(JSON.stringify({{ configured, unconfigured }}));
""")
    assert data == {"configured": "romm", "unconfigured": "folder:3"}, data


def test_the_rules_view_wires_the_opening_choice():
    src = _read("lib", "components", "views", "RulesView.svelte")
    assert "libraries.openOnConfigured(romm.configured)" in src
    # An unconfigured RomM shows one way to connect it, not its Rules.
    assert "{#if rommMissing}" in src
    assert "ui.navigate('libraries', { id: 'romm', tab: 'connection' })" in src


def test_romms_automation_tab_lists_romm_only():
    src = _read("lib", "components", "views", "RulesView.svelte")
    head = src.split('<div class="rules-view"', 1)[1]
    picker = head.index('label="Library"')
    assert head.rfind("{#if !embedded}", 0, picker) > head.rfind("{/if}", 0, picker), (
        "the embedded Rules view still shows the Library picker"
    )
    assert "libraries.open(id)" not in src


def test_set_platform_is_offered_only_inside_a_folder_library(tmp_path):
    data = _run_libraries(tmp_path, f"""
const loading = libraries.load();
await tick();
__calls[0].resolve([{{ id: 'romm', kind: 'romm' }}, {_FOLDER}]);
await loading;
const at = (p) => libraries.folderLibraryFor(p)?.id ?? null;
process.stdout.write(JSON.stringify({{
  root: at('/v/roms'), inside: at('/v/roms/psx'), outside: at('/v/romsx'),
  elsewhere: at('/v/downloads'), none: at(''),
}}));
""")
    assert data == {
        "root": "folder:3", "inside": "folder:3", "outside": None,
        "elsewhere": None, "none": None,
    }, data
    menu = _read("lib", "components", "panels", "RowActionsMenu.svelte")
    assert "libraries.folderLibraryFor(path)" in menu
    assert "{#if canSetPlatform}" in menu


def test_no_pointer_names_a_settings_section_that_does_not_exist():
    for path in (
        ("lib", "components", "views", "RommView.svelte"),
        ("lib", "stores", "romm.svelte.js"),
    ):
        src = _read(*path)
        assert "Settings →" not in src, path
        assert "in Settings" not in src, path
        assert not re.search(r"Settings\s+→\s+Metadata", src), path


def test_the_sidebar_has_no_tool_section():
    """The per-tool section is gone (#534): the Convert-to list reaches every tool."""
    src = _read("lib", "components", "layout", "Sidebar.svelte")
    assert 'class="section-title">Tool<' not in src


def test_the_main_area_draws_no_ring_when_it_takes_focus():
    src = _read("App.svelte")
    assert "inset 0 0 0 3px var(--accent)" not in src
    assert ".main:focus, .main:focus-visible { box-shadow: none; }" in src


def test_the_breadcrumb_has_a_row_of_its_own():
    src = _read("lib", "components", "panels", "FileList.svelte")
    crumbs = src.split("  .crumbs {", 1)[1].split("}", 1)[0]
    # The toolbar wraps (restructure 2/6); the breadcrumb takes a full row.
    assert "flex: 1 0 100%;" in crumbs


# ── A3: contrast, focus and motion ─────────────────────────────────────────


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _light_tokens() -> dict[str, str]:
    css = (_SRC / "styles" / "tokens.css").read_text(encoding="utf-8")
    light = css.split(":root.dark {", 1)[0]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", light))


@pytest.mark.parametrize(
    "token", ["--text-3", "--success-text", "--warning-text", "--info-text"],
)
def test_light_mode_text_passes_aa(token):
    tokens = _light_tokens()
    for surface in ("--surface-1", "--surface-2"):
        ratio = _contrast(tokens[token], tokens[surface])
        assert ratio >= 4.5, f"{token} on {surface} is {ratio:.2f}:1"


def test_status_colours_are_not_used_as_text():
    bare = re.compile(r"(?<![-\w])color: var\(--(success|warning|info)\)")
    for path in _LIB.rglob("*.svelte"):
        hit = bare.search(path.read_text(encoding="utf-8"))
        assert not hit, f"{path.relative_to(_SRC)} uses a fill colour as text: {hit.group(0)}"


def test_the_select_keeps_the_focus_ring():
    src = _read("lib", "components", "ui", "Select.svelte")
    focus = src.split("select:focus-visible {", 1)[1].split("}", 1)[0]
    assert "box-shadow: var(--focus-ring);" in focus
    menu = _read("lib", "components", "panels", "RowActionsMenu.svelte")
    item = menu.split(":global(.row-actions-item[data-highlighted]) {", 1)[1].split("}", 1)[0]
    assert "box-shadow" in item


def test_reduced_motion_keeps_busy_feedback():
    css = (_SRC / "styles" / "tokens.css").read_text(encoding="utf-8")
    reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    for selector in (".spinner .ring", ".spin", ".indeterminate"):
        assert selector in reduced, f"{selector} stops under reduced motion"
    assert "@keyframes reduced-busy" in css


def test_cards_and_callouts_have_no_accent_stripe():
    card = _read("lib", "components", "dashboard", "StatCard.svelte")
    assert "border-top: 3px" not in card
    help_view = _read("lib", "components", "views", "HelpView.svelte")
    assert "border-left: 3px" not in help_view


# ── B7: no internals shown ─────────────────────────────────────────────────


def test_file_info_shows_labels_and_hides_empty_fields(tmp_path):
    helper = (_LIB / "util" / "fileInfo.js").as_uri()
    data = _eval(tmp_path, f"import {{ infoRows }} from {json.dumps(helper)};\n", """
const out = infoRows({
  input_file: '/v/roms/Game.chd', hunk_size: 19584, total_hunks: '', chd_size: null,
  game_id: 'SLUS-00001', title: '', raw_data: 'chdman - MAME Compressed Hunks...',
  tracks: [{ type: 'MODE1' }], extra: {},
}, '/v/roms/Game.chd');
process.stdout.write(JSON.stringify(out));
""")
    assert data["rows"] == [
        {"key": "hunk_size", "label": "Hunk size", "value": 19584},
        {"key": "game_id", "label": "Game ID", "value": "SLUS-00001"},
    ], data
    assert [row["label"] for row in data["details"]] == ["Raw data", "Tracks"], data
    modal = _read("lib", "components", "modals", "FileInfoModal.svelte")
    assert "infoRows(info, target?.path)" in modal
    assert "<summary>Details</summary>" in modal
    assert "text-transform: uppercase" not in modal


def test_a_source_row_does_not_inherit_its_outputs_check():
    src = _read("lib", "components", "panels", "FileRow.svelte")
    assert "const check = $derived(verification.checkFor(path) ?? null);" in src
    assert "found.push(verification.checkFor(out.path))" not in src
    # The output's own badge carries its tick instead.
    assert "verification.checkFor(out.path)" in src


def test_job_toasts_and_rows_name_the_file_not_its_path(tmp_path):
    util = _LIB / "util"
    data = _eval(
        tmp_path,
        f"import {{ completionToast }} from {json.dumps((util / 'jobToast.js').as_uri())};\n"
        f"import {{ jobDetailLines }} from {json.dumps((util / 'jobDetail.js').as_uri())};\n",
        """
const job = { status: 'completed', output_path: '/mnt/roms/psx/Fake Game (USA).chd' };
process.stdout.write(JSON.stringify({
  toast: completionToast(job).description, row: jobDetailLines(job)[0].text,
}));
""",
    )
    assert data == {"toast": "→ Fake Game (USA).chd", "row": "→ Fake Game (USA).chd"}, data
