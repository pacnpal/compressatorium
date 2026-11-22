"""Which check the UI promises will gate a delete-source job (#298).

The convert panel and the delete confirmation both state the check a Source is
deleted on. They read it from one pure helper, ``src/lib/util/deleteCheck.js``,
so the container-only opt-out changes both, and the confirmation can never
promise a Round-trip the job will not run. Evaluated with Node; skipped when
Node is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_HELPER = (
    Path(__file__).resolve().parents[1] / "src" / "lib" / "util" / "deleteCheck.js"
)


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def test_the_promised_check_follows_mode_support_and_the_opt_out(tmp_path):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    assert _HELPER.is_file(), "src/lib/util/deleteCheck.js is missing"
    script = tmp_path / "delete_check_eval.mjs"
    script.write_text(
        f"import {{ deleteCheckFor }} from {json.dumps(_HELPER.as_uri())};\n"
        "const cases = [[true, false], [true, true], [false, false], [false, true]];\n"
        "process.stdout.write(JSON.stringify(cases.map(([s, c]) =>"
        " deleteCheckFor({ supportsRoundTrip: s, containerOnly: c }))));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "round_tripped",   # round-trip mode, no opt-out
        "intact",          # round-trip mode, container-only opt-out
        "intact",          # mode without a digest
        "intact",          # opt-out on a mode without a digest is moot
    ]


def test_a_recognized_mode_promises_the_recognized_check(tmp_path):
    """#423: a mode whose proof is Recognized promises that check, and the
    container-only opt-out (a Round-trip opt-out) changes nothing."""
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "delete_check_recognized.mjs"
    script.write_text(
        f"import {{ deleteCheckFor, DELETE_CHECK_HINTS }} from {json.dumps(_HELPER.as_uri())};\n"
        "const out = [false, true].map((c) => deleteCheckFor("
        "{ supportsRoundTrip: false, containerOnly: c, deleteProof: 'recognized' }));\n"
        "out.push(deleteCheckFor({ supportsRoundTrip: true, containerOnly: false,"
        " deleteProof: 'round_trip' }));\n"
        "out.push(Object.keys(DELETE_CHECK_HINTS).sort().join(','));\n"
        "process.stdout.write(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "recognized", "recognized", "round_tripped", "intact,recognized,round_tripped",
    ]


def test_the_panel_and_the_rule_editor_say_which_proof_each_mode_uses():
    src = Path(__file__).resolve().parents[1] / "src" / "lib" / "components"
    # The panel's delete copy is SafetyOptions'; the check it rests on is
    # named in the delete preview, over every group the plan deletes.
    store = (src.parent / "stores" / "conversion.svelte.js").read_text(encoding="utf-8")
    assert "CHECK_STRENGTH.find((c) => checks.includes(c))" in store
    table = (src / "library" / "RuleTable.svelte").read_text(encoding="utf-8")
    assert "DELETE_CHECK_HINTS[ruleDeleteCheck(spec, rule)]" in table
    modal = (src / "modals" / "DeletePlanModal.svelte").read_text(encoding="utf-8")
    assert "conversion.deleteCheck === 'recognized'" in modal
