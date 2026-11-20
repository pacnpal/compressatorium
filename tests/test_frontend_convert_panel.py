"""ConvertPanel cancels its Auto run when it unmounts.

If the panel unmounts while the Auto plan request is loading, the run must not
stay suspended at a confirmation no mounted modal can answer: that left the
store's ``autoInFlight`` true and Start disabled until a reload. The panel
hands ``runAuto`` an AbortSignal and aborts it in ``onDestroy``.

Evaluated with Node: the conversion store (as ``test_frontend_conversion_store``
reduces it) and the panel's ``<script>`` (as ``test_frontend_romm_views``
reduces it), sibling stores stubbed, ``onDestroy`` recorded so the test can
unmount.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_frontend_conversion_store import _STUBS, _find_node, _plain_js
from .test_frontend_romm_views import _script_of

_PANEL = (
    Path(__file__).resolve().parents[1]
    / "src" / "lib" / "components" / "panels" / "ConvertPanel.svelte"
)

_PANEL_STUBS = """
const __teardown = [];
const onDestroy = (fn) => { __teardown.push(fn); };
const __cleared = [];
const fileBrowser = {
  selectedFiles: new Set(['/v/a.iso']),
  convertibleSelection: [{ path: '/v/a.iso' }],
  rommPlatformId: null,
  searchMode: false,
  clearSelection() { __cleared.push(true); },
};
const romm = { setPendingRepins() {} };
const ui = {};
"""

_UNMOUNT_MID_PLAN = """
let answerPlan;
autoPlanAnswer = (paths) => new Promise((resolve) => {
  answerPlan = () => resolve(paths.map((p) => ({ file_path: p, mode: 'createdvd' })));
});
const submitted = handleAutoSubmit();
await new Promise((resolve) => setTimeout(resolve, 0));   // the plan is loading
const during = conversion.autoInFlight;
for (const fn of __teardown) fn();                        // the panel unmounts
await submitted;
const afterUnmount = conversion.autoInFlight;
answerPlan();                                             // the plan lands late
await new Promise((resolve) => setTimeout(resolve, 0));
process.stdout.write(JSON.stringify({
  during,
  afterUnmount,
  teardowns: __teardown.length,
  modalOpened: autoPlanPrompt !== null,
  queued: calls.auto.length,
  cleared: __cleared.length,
  // What the remounted panel's Start button is gated on.
  startDisabled: selectedPaths.length === 0 || conversion.converting
    || conversion.autoInFlight,
}));
"""


def test_unmounting_mid_plan_cancels_the_run_and_frees_start(tmp_path):
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate ConvertPanel")
    script = tmp_path / "convert_panel_eval.mjs"
    script.write_text(
        _STUBS + _PANEL_STUBS + _plain_js() + _script_of(_PANEL) + _UNMOUNT_MID_PLAN,
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate ConvertPanel via node:\n{proc.stderr}")
    data = json.loads(proc.stdout)

    assert data["during"] is True, data
    assert data["teardowns"] >= 1, data          # the panel registers a teardown
    assert data["afterUnmount"] is False, data
    assert data["modalOpened"] is False, data    # no modal after the abort
    assert data["queued"] == 0, data
    assert data["cleared"] == 0, data            # nothing queued, selection kept
    assert data["startDisabled"] is False, data
