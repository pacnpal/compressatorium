"""The Rules view shows only the selected Library's placements (#464).

Switching the Library select showed the previous Library's "Where new Images
go" rows under the new heading until the new answer came back, and an answer
for the previous Library that landed after the switch could still be shown.
The placements are now kept with the Library they were loaded for
(``src/lib/util/libraryPlacements.js``), driven here with Node.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MODULE = _ROOT / "src/lib/util/libraryPlacements.js"
_PANEL = _ROOT / "src/lib/components/views/LibraryPlacement.svelte"

_SCRIPT = """
const { PlacementsLoad, placementsOf } = await import(MODULE);
const deferred = () => {
  let ok, no;
  const p = new Promise((a, b) => { ok = a; no = b; });
  return { p, ok, no };
};
const answers = {};
const load = new PlacementsLoad((id) => answers[id].p);
const out = {};
const romm = { placements: [{ id: 'ngc', name: 'GameCube', folder: 'roms/ngc' }] };
const inbox = { placements: [{ id: 'ps2', name: 'PlayStation 2', folder: 'ps2' }] };

// RomM loads and is shown.
answers.romm = deferred();
let first = load.load('romm');
answers.romm.ok(romm);
let loaded = await first;
out.romm = placementsOf(loaded, 'romm')?.placements.map((r) => r.id) ?? null;

// Switch to Inbox Lib: nothing of RomM's is shown before Inbox Lib answers.
out.switched = placementsOf(loaded, 'inbox');

// RomM is reloaded, the switch comes while it is out and Inbox Lib's own
// load waits (the view's listing runs). RomM's late answer is not Inbox Lib's.
answers.romm = deferred();
const late = load.load('romm');
answers.romm.ok(romm);
const lateAnswer = await late;
if (lateAnswer) loaded = lateAnswer;
out.lateRomm = placementsOf(loaded, 'inbox');

// Both out at once: Inbox Lib answers first, RomM's older answer is dropped.
answers.romm = deferred();
answers.inbox = deferred();
const older = load.load('romm');
const newer = load.load('inbox');
answers.inbox.ok(inbox);
const newerAnswer = await newer;
if (newerAnswer) loaded = newerAnswer;
answers.romm.ok(romm);
const olderAnswer = await older;
if (olderAnswer) loaded = olderAnswer;
out.inbox = placementsOf(loaded, 'inbox')?.placements.map((r) => r.id) ?? null;
out.olderDropped = olderAnswer === null;

// A failure is kept with its Library too, and a listing still running is a wait.
answers.inbox = deferred();
const failing = load.load('inbox');
answers.inbox.no(Object.assign(new Error('boom'), { status: 500 }));
loaded = (await failing) ?? loaded;
out.errorHere = placementsOf(loaded, 'inbox')?.error ?? null;
out.errorElsewhere = placementsOf(loaded, 'romm');
answers.inbox = deferred();
const waiting = load.load('inbox');
answers.inbox.no(Object.assign(new Error('listing_in_progress'), { status: 409 }));
out.wait = await waiting;

process.stdout.write(JSON.stringify(out));
"""


def _run(tmp_path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    assert _MODULE.is_file(), "src/lib/util/libraryPlacements.js is missing"
    script = tmp_path / "switch.mjs"
    script.write_text(f"const MODULE = {json.dumps(_MODULE.as_uri())};\n" + _SCRIPT)
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_switch_never_shows_the_previous_librarys_placements(tmp_path):
    out = _run(tmp_path)
    assert out["romm"] == ["ngc"]
    # Before the new Library answers: no rows (the panel shows its loading line).
    assert out["switched"] is None
    # A late answer for the previous Library is not shown under the new one.
    assert out["lateRomm"] is None


def test_a_stale_answer_never_overwrites_the_new_librarys(tmp_path):
    out = _run(tmp_path)
    assert out["inbox"] == ["ps2"]
    assert out["olderDropped"] is True


def test_errors_belong_to_their_library_and_a_running_listing_is_a_wait(tmp_path):
    out = _run(tmp_path)
    assert out["errorHere"] == "boom"
    assert out["errorElsewhere"] is None
    assert out["wait"] is None


def test_the_panel_shows_placements_only_for_the_selected_library():
    panel = _PANEL.read_text(encoding="utf-8")
    assert "new PlacementsLoad(" in panel
    assert "placementsOf(loaded, libraryId)" in panel
