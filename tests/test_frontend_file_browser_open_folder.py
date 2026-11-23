"""Opening a folder from another view (#304, PR #322 review).

The Findings view's "Open folder" calls ``fileBrowser.openFolder(path)``. It
must load the Volume list and go straight to the Volume and folder it was
asked for. The normal Workspace start (``loadVolumes``) selects and lists the
first Volume, as before. Asserted behaviourally by evaluating
``src/lib/stores/fileBrowser.svelte.js`` with Node, runes reduced and its
sibling stores stubbed, as the other ``test_frontend_*`` store tests do.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from .test_frontend_rules_view import _find_node

_STORE = Path(__file__).resolve().parents[1] / "src" / "lib" / "stores" / "fileBrowser.svelte.js"


def _plain_js() -> str:
    src = _STORE.read_text(encoding="utf-8")
    kept = [
        line for line in src.splitlines()
        if not (line.strip().startswith("import ") and line.strip().endswith(";"))
    ]
    body = re.sub(r"\$state\(", "(", "\n".join(kept))
    leftover = [r for r in ("$state(", "$derived(", "$effect(") if r in body]
    assert not leftover, f"fileBrowser.svelte.js grew runes this harness does not reduce: {leftover}"
    return body.replace("export const ", "const ").replace("export class ", "class ")


_STUBS = """
const SvelteMap = Map;
const STORAGE_KEYS = {};
const readString = () => null;
const writeString = () => {};
const jobs = { hasActive: false };
const conversion = { allowsInput: () => true, allowsInputEntry: () => true };
const ui = { navigated: [], navigate(v) { this.navigated.push(v); } };
const __listed = [];
const api = {
  getVolumes: async () => [
    { name: 'First', path: '/first' },
    { name: 'Target', path: '/target' },
  ],
  // The first Volume sits on a mount that stopped answering.
  listFiles: (path) => {
    __listed.push(path);
    if (path === '/first' || path.startsWith('/first/')) return new Promise(() => {});
    return Promise.resolve({ entries: [{ name: 'x.iso', path: `${path}/x.iso` }] });
  },
  listArchive: async () => ({ entries: [] }),
  getArchiveSummaryBatch: async () => ({}),
  searchFiles: async () => ({ results: [] }),
  getRommRoms: async () => ({ items: [] }),
};
const within = (promise, ms) => Promise.race([
  promise.then(() => 'done'),
  new Promise((resolve) => setTimeout(() => resolve('timed out'), ms)),
]);
"""


def _run(tmp_path: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the fileBrowser store")
    script = tmp_path / "file_browser_eval.mjs"
    script.write_text(_STUBS + _plain_js() + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the fileBrowser store via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_open_folder_goes_straight_to_its_volume_without_listing_the_first(tmp_path):
    data = _run(tmp_path, """
const outcome = await within(fileBrowser.openFolder('/target/roms'), 2000);
process.stdout.write(JSON.stringify({
  outcome,
  volume: fileBrowser.selectedVolume?.path ?? null,
  path: fileBrowser.currentPath,
  listed: __listed,
  navigated: ui.navigated,
  entries: fileBrowser.entries.length,
}));
""")
    assert data["outcome"] == "done", data
    assert data["volume"] == "/target", data
    assert data["path"] == "/target/roms", data
    assert data["listed"] == ["/target/roms"], data
    assert data["navigated"] == ["files"], data
    assert data["entries"] == 1, data


def test_opening_a_folder_link_on_files_pushes_no_history_entry(tmp_path):
    """Files consumes `#/files/<path>` by opening the folder.
    Navigating again there pushed `#/files` on top, so Back looped between the
    two. Already on Files, the folder opens and the URL is left alone."""
    data = _run(tmp_path, """
ui.activeView = 'files';
const outcome = await within(fileBrowser.openFolder('/target/roms'), 2000);
process.stdout.write(JSON.stringify({
  outcome, path: fileBrowser.currentPath, navigated: ui.navigated,
}));
""")
    assert data == {"outcome": "done", "path": "/target/roms", "navigated": []}, data


def test_the_workspace_start_still_selects_and_lists_the_first_volume(tmp_path):
    data = _run(tmp_path, """
const outcome = await within(fileBrowser.loadVolumes(), 300);
process.stdout.write(JSON.stringify({
  outcome, volume: fileBrowser.selectedVolume?.path ?? null, listed: __listed,
}));
""")
    # The first Volume's listing never answers, so loadVolumes waits on it.
    assert data["outcome"] == "timed out", data
    assert data["volume"] == "/first", data
    assert data["listed"] == ["/first"], data


def test_a_superseded_open_folder_does_not_win(tmp_path):
    """only the latest Open folder navigates.

    With no Volumes loaded yet, two Open folder actions each fetch the Volume
    list. The second fetch answers first and opens its folder; when the first
    answers after it, that older request goes nowhere.
    """
    data = _run(tmp_path, """
const __volumeCalls = [];
api.getVolumes = () => new Promise((resolve) => { __volumeCalls.push(resolve); });
const tick = () => new Promise((r) => setImmediate(r));
const volumes = [{ name: 'Target', path: '/target' }];
const first = fileBrowser.openFolder('/target/a');
await tick();
const second = fileBrowser.openFolder('/target/b');
await tick();
__volumeCalls[1](volumes);
await within(second, 2000);
__volumeCalls[0](volumes);
await within(first, 2000);
process.stdout.write(JSON.stringify({
  path: fileBrowser.currentPath, listed: __listed, navigated: ui.navigated,
}));
""")
    assert data["path"] == "/target/b", data
    assert data["listed"] == ["/target/b"], data
    assert data["navigated"] == ["files"], data


# --------------------------------------------------------------------------- #
# #571: Files picks the first Volume however the list got loaded
# --------------------------------------------------------------------------- #

def test_files_picks_the_first_volume_when_another_view_loaded_the_list(tmp_path):
    """Libraries, Needs attention and watch folders fetch the Volume list for
    their own use without picking one; Files opened after them showed "This
    folder is empty." with nothing picked."""
    data = _run(tmp_path, """
await fileBrowser.fetchVolumes();
const before = fileBrowser.selectedVolume;
const outcome = await within(fileBrowser.startFiles(), 300);
process.stdout.write(JSON.stringify({
  before: before ?? null, outcome,
  volume: fileBrowser.selectedVolume?.path ?? null, listed: __listed,
}));
""")
    assert data["before"] is None, data
    assert data["volume"] == "/first", data
    assert data["listed"] == ["/first"], data


def test_files_start_leaves_an_open_folder_and_a_folder_link_alone(tmp_path):
    data = _run(tmp_path, """
await fileBrowser.fetchVolumes();
fileBrowser.currentPath = '/target/roms';
await within(fileBrowser.startFiles(), 300);
const kept = fileBrowser.currentPath;
fileBrowser.currentPath = null;
ui.filesPath = '/target/roms';
await within(fileBrowser.startFiles(), 300);
process.stdout.write(JSON.stringify({
  kept, linked: fileBrowser.selectedVolume?.path ?? null, listed: __listed,
}));
""")
    assert data == {"kept": "/target/roms", "linked": None, "listed": []}, data


def test_files_start_with_no_list_yet_still_loads_and_picks(tmp_path):
    data = _run(tmp_path, """
await within(fileBrowser.startFiles(), 300);
process.stdout.write(JSON.stringify({
  volume: fileBrowser.selectedVolume?.path ?? null, listed: __listed,
}));
""")
    assert data == {"volume": "/first", "listed": ["/first"]}, data


def test_the_volume_list_starts_files():
    src = (_STORE.parents[1] / "components" / "panels" / "VolumeList.svelte").read_text(
        encoding="utf-8",
    )
    assert "fileBrowser.startFiles()" in src
