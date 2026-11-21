"""Four places plus Help, and every old route redirected (restructure PR 1, item 8).

``src/lib/router/routes.js`` is pure, so it is imported with Node as it ships.
The ui store and the hash router are asserted against the source for the
wiring. Skipped where Node is unavailable.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_tool_availability_496 import _find_node
from tests.test_frontend_tool_availability_496 import _run as _run_ui

_SRC = Path(__file__).resolve().parents[1] / "src"
_ROUTES = _SRC / "lib" / "router" / "routes.js"

# Spec §1.3, every row, plus the ids and paths an old link can carry.
REDIRECTS = {
    "": "#/files",
    "#": "#/files",
    "#/": "#/files",
    "#/workspace": "#/files",
    "#/workspace/chdman": "#/files",
    "#/workspace/dolphin": "#/files",
    "#/dashboard": "#/activity",
    "#/romm": "#/libraries/romm",
    "#/rules": "#/libraries",
    "#/rules/folder%3A3": "#/libraries/folder%3A3/automation",
    "#/rules/romm": "#/libraries/romm/automation",
    "#/findings": "#/activity/attention",
    "#/ledger": "#/activity/history",
    "#/dat": "#/settings/identification",
    "#/nowhere": "#/files",
    "#/nowhere/at/all": "#/files",
}

CURRENT = [
    "#/files", "#/files/%2Fdata%2Froms", "#/libraries", "#/libraries/romm",
    "#/libraries/folder%3A3/automation", "#/activity", "#/activity/running",
    "#/activity/attention/folder%3A3", "#/settings", "#/settings/converters", "#/help",
]


def _eval(tmp_path: Path, body: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "routes_eval.mjs"
    script.write_text(
        f"import * as r from {json.dumps(_ROUTES.as_uri())};\n{body}", encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_every_old_route_redirects(tmp_path):
    got = _eval(tmp_path, f"""
const cases = {json.dumps(list(REDIRECTS))};
const out = Object.fromEntries(cases.map((h) => [h, r.legacyRedirect(h)]));
process.stdout.write(JSON.stringify(out));
""")
    assert got == REDIRECTS


def test_current_routes_are_left_alone(tmp_path):
    got = _eval(tmp_path, f"""
process.stdout.write(JSON.stringify({json.dumps(CURRENT)}.map((h) => r.legacyRedirect(h))));
""")
    assert got == [None] * len(CURRENT)


def test_parse_reads_each_place(tmp_path):
    got = _eval(tmp_path, """
const cases = ['#/files', '#/files/%2Fdata%2Froms%20(USA)', '#/libraries/folder%3A3',
  '#/libraries/romm/connection', '#/libraries/romm/bogus', '#/activity/attention/romm',
  '#/activity/history?status=failed', '#/activity/bogus', '#/settings/defaults',
  '#/settings/nope', '#/help', '#/nowhere'];
process.stdout.write(JSON.stringify(cases.map((h) => r.parseHash(h))));
""")
    none = {"id": None, "tab": None, "path": None}
    assert got == [
        {**none, "view": "files"},
        {**none, "view": "files", "path": "/data/roms (USA)"},
        {**none, "view": "libraries", "id": "folder:3"},
        {**none, "view": "libraries", "id": "romm", "tab": "connection"},
        {**none, "view": "libraries", "id": "romm"},
        {**none, "view": "activity", "tab": "attention", "id": "romm"},
        {**none, "view": "activity", "tab": "history"},
        {**none, "view": "activity"},
        {**none, "view": "settings", "tab": "defaults"},
        {**none, "view": "settings"},
        {**none, "view": "help"},
        {**none, "view": "files"},
    ]


def test_hash_for_and_parse_round_trip(tmp_path):
    got = _eval(tmp_path, """
const cases = [
  ['files', {}], ['files', { path: '/data/My Games/psx' }],
  ['libraries', {}], ['libraries', { id: 'romm' }],
  ['libraries', { id: 'folder:12', tab: 'automation' }],
  ['activity', {}], ['activity', { tab: 'running' }],
  ['activity', { tab: 'attention', id: 'folder:2' }],
  ['settings', {}], ['settings', { tab: 'identification' }], ['help', {}],
];
process.stdout.write(JSON.stringify(cases.map(([v, o]) => {
  const hash = r.hashFor(v, o);
  return [hash, r.parseHash(hash), r.legacyRedirect(hash)];
})));
""")
    for (hash, parsed, redirect), (view, opts) in zip(got, [
        ("files", {}), ("files", {"path": "/data/My Games/psx"}),
        ("libraries", {}), ("libraries", {"id": "romm"}),
        ("libraries", {"id": "folder:12", "tab": "automation"}),
        ("activity", {}), ("activity", {"tab": "running"}),
        ("activity", {"tab": "attention", "id": "folder:2"}),
        ("settings", {}), ("settings", {"tab": "identification"}), ("help", {}),
    ]):
        assert redirect is None, hash
        assert parsed == {"view": view, "id": None, "tab": None, "path": None, **opts}, hash


def test_views_and_titles_match():
    got = _ROUTES.read_text(encoding="utf-8")
    assert "export const VIEWS" in got and "export const VIEW_TITLES" in got
    assert "import " not in got, "routes.js must stay import-free for the Node tests"


def test_the_ui_store_routes_old_links_and_tracks_each_place(tmp_path):
    data = _run_ui(tmp_path, """
globalThis.window = { location: { hash: '' } };
const store = new UIStore();
const seen = [];
for (const h of ['#/rules/folder%3A3', '#/activity/attention/romm', '#/files/%2Fdata',
                 '#/settings/converters', '#/workspace/chdman']) {
  store.applyHash(h);
  seen.push([store.activeView, store.libraryTarget, store.libraryTab, store.activityTab,
             store.activityLibrary, store.filesPath, store.settingsSection]);
}
store.navigate('libraries', { id: 'folder:3', tab: 'automation' });
process.stdout.write(JSON.stringify({ seen, hash: window.location.hash }));
""")
    assert data["seen"] == [
        ["libraries", "folder:3", "automation", None, None, None, None],
        ["activity", None, None, "attention", "romm", None, None],
        ["files", None, None, None, None, "/data", None],
        ["settings", None, None, None, None, None, "converters"],
        ["files", None, None, None, None, None, None],
    ]
    assert data["hash"] == "#/libraries/folder%3A3/automation"


def test_the_router_replaces_an_old_hash_instead_of_pushing_it():
    src = (_SRC / "lib" / "router" / "hashRouter.js").read_text(encoding="utf-8")
    assert "legacyRedirect(" in src
    assert "history.replaceState(null, '', redirect)" in src
    assert "DEFAULT_HASH" in src
