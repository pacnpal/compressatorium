"""The finish says what it did, in numbers (#595).

- A finished job's toast names its new file, whether it was checked, and the
  space it saved (the Ledger's definition: a completed job that deleted its
  original). A run of several games ends with one summary line, which the
  Convert panel shows too; kept originals add what deleting them would save.
- Archive rows show their size.
- Activity's Checked count is History's checked total, and Space saved says
  its scope.

``src/lib/util/runResult.js`` is pure, so it runs under Node here; skipped
when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_LIB = _ROOT / "src" / "lib"
_RESULT = _LIB / "util" / "runResult.js"
_TOAST = _LIB / "util" / "jobToast.js"

MB = 1024 * 1024


def _read(rel: str) -> str:
    return (_LIB / rel).read_text(encoding="utf-8")


def _node() -> str:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    pytest.skip("node not available")
    return ""


def _eval(tmp_path, helper: Path, names: str, body: str):
    node = _node()
    assert helper.is_file(), f"{helper} is missing"
    script = tmp_path / "eval.mjs"
    script.write_text(
        f"import {{ {names} }} from {json.dumps(helper.as_uri())};\n" + body,
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _job(**kw) -> str:
    base = {
        "id": "j", "status": "completed", "output_path": "/v/Game.chd",
        "input_bytes": 5 * MB, "output_size": 3 * MB, "deleted_paths": [],
        "check_passed": None,
    }
    base.update(kw)
    return json.dumps(base)


def test_saved_space_follows_the_ledger(tmp_path):
    out = _eval(tmp_path, _RESULT, "jobSaved, jobStillToSave", f"""
const deleted = {_job(deleted_paths=["/v/Game.cue"])};
const kept = {_job()};
const failed = {_job(status="failed", deleted_paths=["/v/x"])};
const unknown = {_job(input_bytes=None, deleted_paths=["/v/x"])};
process.stdout.write(JSON.stringify([
  jobSaved(deleted), jobSaved(kept), jobSaved(failed), jobSaved(unknown),
  jobStillToSave(deleted), jobStillToSave(kept),
]));
""")
    assert out == [2 * MB, 0, 0, 0, 0, 2 * MB]


def test_a_job_toast_says_checked_and_saved(tmp_path):
    out = _eval(tmp_path, _TOAST, "completionToast", f"""
process.stdout.write(JSON.stringify([
  completionToast({_job(deleted_paths=["/v/Game.cue"], check_passed="round_tripped")}),
  completionToast({_job(check_passed="intact")}),
  completionToast({_job(input_bytes=None)}),
]));
""")
    assert out[0]["kind"] == "success"
    assert out[0]["description"] == "→ Game.chd · checked · saved 2 MB"
    assert out[1]["description"] == "→ Game.chd · checked · 2 MB smaller, original kept"
    assert out[2]["description"] == "→ Game.chd"


def test_a_run_ends_with_one_line(tmp_path):
    out = _eval(tmp_path, _RESULT, "runResult", f"""
const a = {_job(id="a", deleted_paths=["/v/a"], check_passed="round_tripped")};
const b = {_job(id="b", deleted_paths=["/v/b"], check_passed="recognized")};
const c = {_job(id="c", check_passed="intact")};
const d = {_job(id="d", status="failed")};
const e = {_job(id="e", status="processing")};
process.stdout.write(JSON.stringify([
  runResult([a]),
  runResult([a, b]),
  runResult([a, c]),
  runResult([a, d]),
  runResult([a, e]),
  runResult([{_job(id="f")}]),
]));
""")
    assert out[0] == "1 game converted and checked · saved 2 MB"
    assert out[1] == "2 games converted and checked · saved 4 MB"
    assert out[2] == (
        "2 games converted and checked · saved 2 MB"
        " · 2 MB more to save by deleting the originals"
    )
    assert out[3] == "1 game converted and checked · saved 2 MB · 1 failed"
    # Still running: no line yet.
    assert out[4] is None
    assert out[5] == "1 game converted · 2 MB more to save by deleting the originals"


def test_the_store_remembers_the_last_run_and_the_toasts_sum_it_up():
    store = _read("stores/conversion.svelte.js")
    assert "lastRun = $state(null)" in store
    assert "this.lastRun = { ids }" in store
    assert "jobToasts.trackRun(" in store
    toasts = _read("stores/jobToasts.svelte.js")
    assert "trackRun(" in toasts
    assert "runResult(" in toasts
    # A run of one game says it in its own toast; a summary would repeat it.
    assert "ids.length < 2" in toasts


def test_the_convert_panel_shows_the_last_runs_line():
    panel = _read("components/panels/ConvertPanel.svelte")
    assert "runResult(" in panel
    assert "conversion.lastRun" in panel
    assert 'class="result"' in panel


def test_archive_rows_show_their_size():
    row = _read("components/panels/FileRow.svelte")
    assert "isFile || isArchive || parts.length ? formatSize(entry?.size)" in row


def test_activity_counts_agree_with_history():
    summary = _read("components/activity/ActivitySummary.svelte")
    # History's own checked total, not this browser's check results.
    assert "ledger.checkedTotal" in summary
    assert "verification.statuses.size" not in summary
    # Space saved names its scope.
    assert "ledger.savings?.conversions" in summary
    store = _read("stores/ledger.svelte.js")
    assert "checkedTotal = $state(null)" in store
    assert "getLedgerPage({ checked: true, limit: 1 })" in store
