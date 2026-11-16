"""Guards for the duplicate finder's UI (#388).

The Findings store keeps each Library's last find-duplicates run, evaluated
with Node as ``test_frontend_findings_view`` does. The wiring (the Rules view's
Find duplicates button, the Findings view's groups) is asserted on the source.
"""
from __future__ import annotations

from pathlib import Path

from .test_frontend_findings_view import _run

_SRC = Path(__file__).resolve().parents[1] / "src"
_VIEW = _SRC / "lib" / "components" / "views" / "FindingsView.svelte"
_RULES = _SRC / "lib" / "components" / "views" / "RulesView.svelte"
_ENDPOINTS = _SRC / "lib" / "api" / "endpoints.js"
_JOBS = _SRC / "lib" / "stores" / "jobConstants.js"


def test_the_store_keeps_each_librarys_last_run(tmp_path):
    data = _run(tmp_path, """
__answers.listFindings = {
  findings: [{ id: 7, kind: 'duplicate', library_id: 'folder:1', image_key: 'raw:ab',
    cause: { copies: [{ path: '/v/a.iso' }, { path: '/v/a.rvz' }] }, fingerprint: 'f' }],
  dismissed: 0,
  duplicate_runs: [{ library_id: 'folder:1', not_checked: { '.cso': 2 }, groups: 1 }],
};
await findings.load();
__answers.listFindings = { findings: [], dismissed: 0 };
const first = findings.duplicateRuns.map((r) => r.not_checked);
await findings.load();
process.stdout.write(JSON.stringify({ first, after: findings.duplicateRuns }));
""")
    assert data["first"] == [{".cso": 2}], data
    # An answer without runs (an older backend) shows none, not the last ones.
    assert data["after"] == [], data


def test_a_finished_duplicate_run_lists_the_findings_again(tmp_path):
    """CodeRabbit review 5925361964: the open Findings view lists again when a
    find-duplicates run ends, not only when it is reopened."""
    data = _run(tmp_path, """
__answers.listFindings = { findings: [], dismissed: 0 };
const loads = () => __log.filter((c) => c[0] === 'listFindings').length;
const first = findings.noteFinishedRuns(['a']);        // what was there on open
const same = findings.noteFinishedRuns(['a']);
const fresh = findings.noteFinishedRuns(['a', 'b']);   // a run just ended
await Promise.resolve();
const cleared = findings.noteFinishedRuns(['b']);      // a job cleared from the list
process.stdout.write(JSON.stringify({ first, same, fresh, cleared, loads: loads() }));
""")
    assert data == {
        "first": False, "same": False, "fresh": True, "cleared": False, "loads": 1,
    }, data
    view = _VIEW.read_text(encoding="utf-8")
    # Fed from the jobs store, the same signal the Dashboard and file list use.
    assert "j.mode === 'find_duplicates'" in view
    assert "TERMINAL_STATUSES.has(j.status)" in view
    assert "findings.noteFinishedRuns(" in view


def test_the_findings_view_shows_each_copy_and_what_was_not_checked() -> None:
    view = _VIEW.read_text(encoding="utf-8")
    assert "duplicate: 'Duplicate'" in view
    assert "row.cause?.copies" in view
    assert "copy.container" in view and "bytes(copy.size)" in view
    assert "findings.duplicateRuns" in view and "notChecked(run)" in view
    # What the run could not see, and the groups it kept as they were.
    assert "run.unlisted" in view and "run.held" in view
    # A duplicate is about the file, not its Platform, and it is read-only.
    assert "row.kind !== 'duplicate'" in view
    assert "deletePlan" not in view and "deleteFile" not in view


def test_the_rules_view_starts_a_run_for_the_library() -> None:
    rules = _RULES.read_text(encoding="utf-8")
    assert "api.findDuplicates(lib.id)" in rules
    assert "Find duplicates" in rules
    endpoints = _ENDPOINTS.read_text(encoding="utf-8")
    assert "/find-duplicates`" in endpoints
    # The run's job has a name in the Jobs panel.
    assert "find_duplicates: 'Find duplicates'" in _JOBS.read_text(encoding="utf-8")
