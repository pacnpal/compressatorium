"""Guards for the duplicate delete's UI (#389).

The Findings store's delete flow is evaluated with Node, as
``test_frontend_findings_view`` does; the wiring is asserted on the source.
"""
from __future__ import annotations

from pathlib import Path

from .test_frontend_findings_view import _run

_SRC = Path(__file__).resolve().parents[1] / "src"
_VIEW = _SRC / "lib" / "components" / "activity" / "NeedsAttention.svelte"
_MODAL = _SRC / "lib" / "components" / "modals" / "DuplicateDeleteModal.svelte"
_ENDPOINTS = _SRC / "lib" / "api" / "endpoints.js"

# The delete job's terminal state as the jobs store holds it.
_DONE = """{ id: 'j1', mode: 'delete_duplicates', status: 'completed',
  result: { keep: '/v/Game.iso', keeper_changed: null,
    results: [{ path: '/v/Copy.iso', status: 'deleted' }] } }"""

_ROW = """{ id: 7, kind: 'duplicate', library_id: 'folder:1', fingerprint: 'f7',
  image_key: 'raw:aa', keeper: '/v/Game.iso', keeper_reason: 'the smallest copy',
  deletable: true,
  cause: { copies: [{ path: '/v/Game.iso' }, { path: '/v/Copy.iso' }] } }"""


def test_the_plan_is_asked_for_with_the_group_as_shown(tmp_path):
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
__answers.duplicateDeletePlan = {{ items: [{{ source_path: '/v/Copy.iso',
  delete_paths: ['/v/Copy.iso'] }}], blocked: false, total_delete_count: 1 }};
await findings.openCleanup({_ROW});
process.stdout.write(JSON.stringify({{
  call: __log.find((c) => c[0] === 'duplicateDeletePlan'),
  keep: findings.cleanup.keep, total: findings.cleanup.plan.total_delete_count,
}}));
""")
    # The suggested keeper by default, and the fingerprint the user saw.
    assert data["call"] == ["duplicateDeletePlan", 7, "f7", "/v/Game.iso"], data
    assert data["keep"] == "/v/Game.iso" and data["total"] == 1, data


def test_confirm_deletes_exactly_what_the_plan_listed(tmp_path):
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
api.deleteDuplicates = __call('deleteDuplicates');
__answers.listFindings = {{ findings: [], dismissed: 0 }};
__answers.duplicateDeletePlan = {{ items: [
  {{ source_path: '/v/Copy.iso', delete_paths: ['/v/Copy.iso'] }},
  {{ source_path: '/v/Disc.cue', delete_paths: ['/v/Disc.cue', '/v/Disc.bin'] }},
], blocked: false, total_delete_count: 3 }};
__answers.deleteDuplicates = {{ job_id: 'j1' }};
await findings.openCleanup({_ROW});
const started = await findings.confirmCleanup();
const running = {{ deleting: findings.cleanup.deleting, jobId: findings.cleanup.jobId,
  jobs: [...findings.runningDeletes] }};
const settled = findings.noteFinishedDeletes([{_DONE}]);
await Promise.resolve();
process.stdout.write(JSON.stringify({{
  call: __log.find((c) => c[0] === 'deleteDuplicates'), started, running,
  settled: settled.map((s) => s.result.results.length), open: findings.cleanup,
  left: findings.runningDeletes,
  listed: __log.filter((c) => c[0] === 'listFindings').length,
}}));
""")
    assert data["call"] == [
        "deleteDuplicates", 7, "f7", "/v/Game.iso",
        ["/v/Copy.iso", "/v/Disc.cue", "/v/Disc.bin"],
    ], data
    # The route only starts the job: the dialog follows it until it ends.
    assert data["started"] == {"job_id": "j1"}, data
    assert data["running"] == {"deleting": True, "jobId": "j1", "jobs": ["j1"]}, data
    assert data["settled"] == [1], data
    assert data["open"] is None and data["left"] == [], data
    # The Finding is read again once the job ended: it shrinks or clears.
    assert data["listed"] == 1


def test_a_blocked_plan_deletes_nothing(tmp_path):
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
api.deleteDuplicates = __call('deleteDuplicates');
__answers.duplicateDeletePlan = {{ items: [{{ source_path: '/v/Copy.iso',
  delete_paths: ['/v/Copy.iso'], errors: ['Copy.iso changed'] }}], blocked: true }};
await findings.openCleanup({_ROW});
const result = await findings.confirmCleanup();
process.stdout.write(JSON.stringify({{
  result, called: __log.some((c) => c[0] === 'deleteDuplicates'),
}}));
""")
    assert data == {"result": None, "called": False}, data


def test_the_view_offers_the_delete_only_where_the_backend_does() -> None:
    view = _VIEW.read_text(encoding="utf-8")
    assert "row.kind === 'duplicate' && row.deletable" in view
    assert "findings.openCleanup(row)" in view
    assert "row.keeper_reason" in view and "row.not_deletable" in view
    # A keeper not yet proven says what to do first (#389, Codex
    # PRRT_kwDOQ9nULM64Lh-l); the delete itself is refused by the backend.
    assert "row.keeper_problem" in view
    assert "<DuplicateDeleteModal" in view
    modal = _MODAL.read_text(encoding="utf-8")
    # Confirm is off until there is a plan, and while it is blocked.
    assert "disabled={!plan || blocked || !!open?.error || !!open?.deleting || !!open?.done}" in modal
    endpoints = _ENDPOINTS.read_text(encoding="utf-8")
    assert "/delete-plan`" in endpoints
    assert "'X-CHD-Action-Confirm': CONFIRM.DELETE_FILE" in endpoints



def test_the_keeper_cannot_change_while_its_delete_runs(tmp_path):
    """CodeRabbit 4593240111: picking another keeper during the delete would
    drop the running delete's state; it is ignored, and the selector is off."""
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
let finish;
api.deleteDuplicates = () => new Promise((resolve) => {{ finish = resolve; }});
__answers.listFindings = {{ findings: [], dismissed: 0 }};
__answers.duplicateDeletePlan = {{ items: [{{ source_path: '/v/Copy.iso',
  delete_paths: ['/v/Copy.iso'] }}], blocked: false, total_delete_count: 1 }};
await findings.openCleanup({_ROW});
const running = findings.confirmCleanup();
await findings.openCleanup({_ROW}, '/v/Copy.iso');
const during = {{ keep: findings.cleanup.keep, deleting: findings.cleanup.deleting }};
finish({{ job_id: 'j1' }});
await running;
process.stdout.write(JSON.stringify({{
  during, plans: __log.filter((c) => c[0] === 'duplicateDeletePlan').length,
}}));
""")
    assert data["during"] == {"keep": "/v/Game.iso", "deleting": True}, data
    assert data["plans"] == 1, data
    assert "disabled={!!open.deleting}" in _MODAL.read_text(encoding="utf-8")


def test_a_delete_answer_for_a_closed_dialog_writes_nothing(tmp_path):
    """CodeRabbit 4593240130: the dialog was closed and another one opened
    while the delete ran; its answer must not close or mark the new one."""
    other = _ROW.replace("id: 7", "id: 8").replace("'f7'", "'f8'")
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
let fail;
api.deleteDuplicates = () => new Promise((_resolve, reject) => {{ fail = reject; }});
__answers.listFindings = {{ findings: [], dismissed: 0 }};
__answers.duplicateDeletePlan = {{ items: [{{ source_path: '/v/Copy.iso',
  delete_paths: ['/v/Copy.iso'] }}], blocked: false, total_delete_count: 1 }};
await findings.openCleanup({_ROW});
const running = findings.confirmCleanup().catch(() => null);
findings.closeCleanup();
await findings.openCleanup({other});
fail(new Error('refused'));
await running;
process.stdout.write(JSON.stringify({{
  id: findings.cleanup?.row?.id, error: findings.cleanup?.error ?? null,
  deleting: findings.cleanup?.deleting,
}}));
""")
    assert data == {"id": 8, "error": None, "deleting": False}, data


def test_cleanup_is_offered_only_with_a_keeper() -> None:
    """CodeRabbit 4593240120: a group whose every copy failed its check has no
    keeper, so no Delete others button."""
    view = _VIEW.read_text(encoding="utf-8")
    assert "row.kind === 'duplicate' && row.deletable && row.keeper" in view



def test_closing_and_reopening_during_a_delete_keeps_it_locked(tmp_path):
    """CodeRabbit 4593355703: the delete in flight belongs to the Finding, not
    the dialog. Closed and reopened while it runs, the dialog shows it as
    deleting, keeps its keeper, and plans nothing new until it settles."""
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
let finish;
api.deleteDuplicates = (...args) => {{
  __log.push(['deleteDuplicates', ...args]);
  return new Promise((resolve) => {{ finish = resolve; }});
}};
__answers.listFindings = {{ findings: [], dismissed: 0 }};
__answers.duplicateDeletePlan = {{ items: [{{ source_path: '/v/Copy.iso',
  delete_paths: ['/v/Copy.iso'] }}], blocked: false, total_delete_count: 1 }};
await findings.openCleanup({_ROW});
const running = findings.confirmCleanup();
findings.closeCleanup();
await findings.openCleanup({_ROW});
const reopened = {{ keep: findings.cleanup.keep, deleting: findings.cleanup.deleting }};
await findings.openCleanup({_ROW}, '/v/Copy.iso');
const picked = findings.cleanup.keep;
const again = await findings.confirmCleanup();
finish({{ job_id: 'j1' }});
await running;
const stillRunning = findings.cleanup?.deleting;
findings.noteFinishedDeletes([{_DONE}]);
process.stdout.write(JSON.stringify({{
  reopened, picked, again, stillRunning, open: findings.cleanup,
  plans: __log.filter((c) => c[0] === 'duplicateDeletePlan').length,
  deletes: __log.filter((c) => c[0] === 'deleteDuplicates').length,
}}));
""")
    assert data["reopened"] == {"keep": "/v/Game.iso", "deleting": True}, data
    assert data["picked"] == "/v/Game.iso", data
    assert data["again"] is None, data
    assert data["plans"] == 1 and data["deletes"] == 1, data
    # Started, not finished: the job runs on until the jobs store sees it end.
    assert data["stillRunning"] is True, data
    # Settled: the reopened dialog for that Finding closes.
    assert data["open"] is None, data
    modal = _MODAL.read_text(encoding="utf-8")
    # Closing does not stop the delete, and the button says so.
    assert "open?.deleting || open?.done ? 'Close' : 'Cancel'" in modal



def test_a_keeper_changed_during_the_delete_leaves_a_warning(tmp_path):
    """Codex 4593499350: the backend says the copy kept no longer looked like
    the file it hashed once the last delete was done. The dialog stays open
    and says so, instead of closing as a plain success."""
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
api.deleteDuplicates = __call('deleteDuplicates');
__answers.listFindings = {{ findings: [], dismissed: 0 }};
__answers.duplicateDeletePlan = {{ items: [{{ source_path: '/v/Copy.iso',
  delete_paths: ['/v/Copy.iso'] }}], blocked: false, total_delete_count: 1 }};
__answers.deleteDuplicates = {{ job_id: 'j1' }};
await findings.openCleanup({_ROW});
await findings.confirmCleanup();
const [{{ result }}] = findings.noteFinishedDeletes([{{ id: 'j1', mode: 'delete_duplicates',
  status: 'completed', result: {{ keep: '/v/Game.iso',
    results: [{{ path: '/v/Copy.iso', status: 'deleted' }}],
    keeper_changed: 'the copy to keep was replaced or changed' }} }}]);
process.stdout.write(JSON.stringify({{
  changed: result.keeper_changed, open: !!findings.cleanup,
  warning: findings.cleanup?.warning ?? null, deleting: findings.cleanup?.deleting,
}}));
""")
    assert data["changed"] == "the copy to keep was replaced or changed", data
    assert data["open"] is True and data["deleting"] is False, data
    assert "Game.iso" in data["warning"] and "check it" in data["warning"], data
    view = _VIEW.read_text(encoding="utf-8")
    assert "result?.keeper_changed" in view
    modal = _MODAL.read_text(encoding="utf-8")
    assert "open.warning" in modal



def test_a_failed_delete_job_shows_why_and_others_are_ignored(tmp_path):
    """The job refused the delete (a sheet added since the run, a copy
    replaced): the dialog shows its reason. A delete job this page did not
    start settles nothing."""
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
api.deleteDuplicates = __call('deleteDuplicates');
__answers.listFindings = {{ findings: [], dismissed: 0 }};
__answers.duplicateDeletePlan = {{ items: [{{ source_path: '/v/Copy.iso',
  delete_paths: ['/v/Copy.iso'] }}], blocked: false, total_delete_count: 1 }};
__answers.deleteDuplicates = {{ job_id: 'j1' }};
await findings.openCleanup({_ROW});
await findings.confirmCleanup();
const foreign = findings.noteFinishedDeletes([{{ id: 'other', mode: 'delete_duplicates',
  status: 'completed', result: {{ results: [] }} }}]);
const settled = findings.noteFinishedDeletes([{{ id: 'j1', mode: 'delete_duplicates',
  status: 'failed', error_message: 'The delete is blocked: Track.bin is a track' }}]);
process.stdout.write(JSON.stringify({{
  foreign: foreign.length, settled: settled.length,
  error: findings.cleanup?.error ?? null, deleting: findings.cleanup?.deleting,
}}));
""")
    assert data == {
        "foreign": 0, "settled": 1,
        "error": "The delete is blocked: Track.bin is a track", "deleting": False,
    }, data


def test_the_view_follows_the_delete_job() -> None:
    view = _VIEW.read_text(encoding="utf-8")
    assert "j.mode === 'delete_duplicates'" in view
    assert "findings.noteFinishedDeletes(ended)" in view
    modal = _MODAL.read_text(encoding="utf-8")
    # The dialog shows the job's progress while it runs.
    assert "job?.message" in modal and "job.progress" in modal
    constants = (_SRC / "lib" / "stores" / "jobConstants.js").read_text(encoding="utf-8")
    assert "delete_duplicates: 'Delete duplicates'" in constants
