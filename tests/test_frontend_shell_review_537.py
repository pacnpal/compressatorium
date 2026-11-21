"""Review fixes on the navigation shell's shared pieces (#537, CodeRabbit).

Folder paths in the folder picker keep a `/` Drive, its confirm button needs a
listed folder, the Add a Library dialog ignores a closed session's answer,
Activity's opening tab holds for the visit, and the defaults store runs one
save at a time. Pure helpers run in Node; the wiring is read from the source.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_registry_derives_186 import _find_node

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"


def _node(tmp_path: Path, body: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "eval.mjs"
    script.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_folder_paths_keep_a_root_drive(tmp_path):
    mod = (_SRC / "util" / "folderPath.js").as_uri()
    got = _node(tmp_path, f"""
import * as f from {json.dumps(mod)};
process.stdout.write(JSON.stringify({{
  root: f.trimPath('/'), trailing: f.trimPath('/data/roms/'), bare: f.trimPath('/data'),
  inRoot: f.isWithin('/data/roms', '/'), atRoot: f.isWithin('/', '/'),
  inDrive: f.isWithin('/data/roms', '/data/'), outside: f.isWithin('/datax', '/data'),
  up: f.parentPath('/data'), upRoot: f.parentPath('/'), upDeep: f.parentPath('/a/b/c'),
  rootCrumbs: f.crumbsFor({{ name: 'All', path: '/' }}, '/data/roms'),
  crumbs: f.crumbsFor({{ name: 'Games', path: '/games/' }}, '/games/psx'),
}}));
""")
    assert got["root"] == "/" and got["trailing"] == "/data/roms" and got["bare"] == "/data"
    assert got["inRoot"] and got["atRoot"] and got["inDrive"] and not got["outside"]
    assert (got["up"], got["upRoot"], got["upDeep"]) == ("/", "/", "/a/b")
    assert got["rootCrumbs"] == [
        {"label": "All", "path": "/"}, {"label": "data", "path": "/data"},
        {"label": "roms", "path": "/data/roms"},
    ]
    assert got["crumbs"] == [
        {"label": "Games", "path": "/games"}, {"label": "psx", "path": "/games/psx"},
    ]


def test_the_folder_picker_uses_the_shared_path_helpers_and_needs_a_listing():
    src = (_SRC / "components" / "ui" / "FolderPicker.svelte").read_text(encoding="utf-8")
    assert "from '$lib/util/folderPath.js'" in src
    assert "function trimmed(" not in src
    # A folder that failed to open can't be confirmed; an empty one can.
    assert "disabled={!path || status !== 'ready' || blocked}" in src


def test_a_closed_add_library_dialog_ignores_its_late_answer():
    src = (_SRC / "components" / "modals" / "AddLibraryModal.svelte").read_text(
        encoding="utf-8",
    )
    assert "const mine = ++session;" in src
    assert "if (mine !== session) return;" in src
    close = src.split("function close()", 1)[1].split("}", 1)[0]
    assert "session++" in close


def test_activitys_opening_tab_holds_for_the_visit():
    src = (_SRC / "components" / "views" / "ActivityView.svelte").read_text(encoding="utf-8")
    assert "const tab = $derived(ui.activityTab ?? opening ?? live);" in src
    assert "if (opening === null && settled) opening = live;" in src


def _defaults_store(tmp_path: Path, scenario: str):
    src = (_SRC / "stores" / "defaults.svelte.js").read_text(encoding="utf-8")
    body = re.sub(r"^import[^\n]*\n", "", src, flags=re.MULTILINE)
    body = body.replace("$state(", "(").replace("export ", "")
    stub = "const api = { putDefaultsPrefs: (v) => globalThis.__api.putDefaultsPrefs(v) };\n"
    before, after = scenario.split("//BODY", 1)
    return _node(tmp_path, stub + before + body + after)


def test_defaults_saves_run_one_at_a_time(tmp_path):
    """Save A fails after save B was asked for: B's change must survive."""
    got = _defaults_store(tmp_path, """
const calls = [];
globalThis.__api = {
  putDefaultsPrefs: (v) => new Promise(
    (resolve, reject) => calls.push({ v: { ...v }, resolve, reject }),
  ),
};
//BODY
const tick = () => new Promise((r) => setImmediate(r));
const a = defaults.save({ delete_on_verify: true }).catch(() => 'failed');
const b = defaults.save({ container_only: true });
await tick();
const inFlight = calls.length;
calls[0].reject(new Error('no'));
await a;
await tick();
calls[1].resolve({ ...calls[1].v });
await b;
process.stdout.write(JSON.stringify({ inFlight, sent: calls[1].v, values: defaults.values }));
""")
    assert got["inFlight"] == 1, got
    assert got["sent"] == {"verify_after": True, "delete_on_verify": False, "container_only": True}
    assert got["values"]["container_only"] is True and got["values"]["delete_on_verify"] is False
