"""The jobs store never lets an older HTTP snapshot undo a newer SSE state.

``adopt`` takes the jobs an Auto response returns. If the job already finished
over SSE before that response arrived, the response's ``queued`` snapshot is
older and must not put the job back in the active queue: the same freshness
rule ``refresh()`` applies. Evaluated with Node like the other store tests.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib" / "stores"


def _plain(path: Path) -> str:
    body = re.sub(r"^import[\s\S]*?from\s+'[^']+';\s*$", "", path.read_text(), flags=re.MULTILINE)
    body = re.sub(r"\$state\.raw\(", "(", body)
    body = re.sub(r"\$state\(", "(", body)
    return body.replace("export const ", "const ").replace("export {", "const __unused = {")


_STUBS = """
const SvelteMap = Map;
const SvelteSet = Set;
const api = {};
const STORAGE_KEYS = { SHOW_METADATA_JOBS: 'm' };
const readBool = (_k, fallback) => fallback;
const writeBool = () => {};
const verification = {};
const ui = {};
"""

_SCENARIO = """
const store = new JobsStore();
// SSE already delivered the finished job.
store._applyJob({ id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'completed' });
// The Auto HTTP response, built earlier, arrives with the queued snapshot.
store.adopt([
  { id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'queued' },
  { id: 'j2', file_path: '/v/b.iso', mode: 'createdvd', status: 'queued' },
]);
// And an active job the response reports as finished heals forward.
store._applyJob({ id: 'j3', file_path: '/v/c.iso', mode: 'createdvd', status: 'queued' });
store.adopt([{ id: 'j3', file_path: '/v/c.iso', mode: 'createdvd', status: 'completed' }]);
process.stdout.write(JSON.stringify(Object.fromEntries(store.jobs.map((j) => [j.id, j.status]))));
"""


def test_adopt_never_replaces_a_terminal_job_with_an_older_active_snapshot(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "jobs_store_eval.mjs"
    script.write_text(
        _STUBS + _plain(_SRC / "jobConstants.js") + _plain(_SRC / "jobs.svelte.js") + _SCENARIO,
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30,
                          check=False)
    if proc.returncode != 0:
        pytest.fail(proc.stderr)
    statuses = json.loads(proc.stdout)

    assert statuses == {"j1": "completed", "j2": "queued", "j3": "completed"}


def _evaluate(tmp_path: Path, scenario: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "jobs_store_eval.mjs"
    script.write_text(
        _STUBS + _plain(_SRC / "jobConstants.js") + _plain(_SRC / "jobs.svelte.js") + scenario,
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30,
                          check=False)
    if proc.returncode != 0:
        pytest.fail(proc.stderr)
    return json.loads(proc.stdout)


# SSE completes j1 and the history prune evicts it
# before the Auto response arrives. With its row gone, the response's older
# `queued` snapshot looked like an unknown job and went back into the active
# queue for good. A finished job the job list no longer has leaves a
# tombstone, and adopt() skips it.
_EVICTED_BEFORE_ADOPT = """
const store = new JobsStore();
store._applyJob({ id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'completed' });
api.getJobs = async () => [];                 // the cap evicted j1
await store.refresh();
store.adopt([
  { id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'queued' },
  { id: 'j2', file_path: '/v/b.iso', mode: 'createdvd', status: 'queued' },
]);
process.stdout.write(JSON.stringify(Object.fromEntries(store.jobs.map((j) => [j.id, j.status]))));
"""


def test_a_job_evicted_before_adopt_is_never_brought_back(tmp_path):
    assert _evaluate(tmp_path, _EVICTED_BEFORE_ADOPT) == {"j2": "queued"}


_TOMBSTONES_ARE_BOUNDED = """
const store = new JobsStore();
const ids = Array.from({ length: 5000 }, (_v, i) => `old${i}`);
store._rememberEvicted(ids);
process.stdout.write(JSON.stringify({ kept: store._evictedOrder.length }));
"""


def test_eviction_tombstones_are_bounded(tmp_path):
    kept = _evaluate(tmp_path, _TOMBSTONES_ARE_BOUNDED)["kept"]

    assert 0 < kept <= 2000


# refresh() infers an eviction only for jobs it knew
# before it asked. A job created and finished over SSE while getJobs() was in
# flight is simply newer than that snapshot: it stays, and is not tombstoned.
_SSE_JOB_DURING_REFRESH = """
const store = new JobsStore();
store._applyJob({ id: 'old', file_path: '/v/o.iso', mode: 'createdvd', status: 'completed' });
api.getJobs = async () => {
  // While the request is in flight, SSE delivers a new job's whole life.
  store._applyJob({ id: 'j9', file_path: '/v/n.iso', mode: 'createdvd', status: 'queued' });
  store._applyJob({ id: 'j9', file_path: '/v/n.iso', mode: 'createdvd', status: 'completed' });
  return [];                                  // built before j9 existed
};
await store.refresh();
process.stdout.write(JSON.stringify({
  ids: store.jobs.map((j) => j.id),
  tombstoned: Object.keys(store._evictedIds).sort(),
}));
"""


def test_a_job_born_during_refresh_is_neither_dropped_nor_tombstoned(tmp_path):
    data = _evaluate(tmp_path, _SSE_JOB_DURING_REFRESH)

    assert data == {"ids": ["j9"], "tombstoned": ["old"]}, data


# Clear completed removes finished jobs on the
# backend too. A late Auto response listing one must not bring it back as an
# unknown queued row: the cleared ids are tombstoned like evictions.
_CLEARED_BEFORE_ADOPT = """
const store = new JobsStore();
store._applyJob({ id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'completed' });
store._applyJob({ id: 'j2', file_path: '/v/b.iso', mode: 'createdvd', status: 'processing' });
api.deleteCompletedJobs = async () => ({
  deleted: ['j1'],
});
await store.clearCompleted();
store.adopt([
  { id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'queued' },
  { id: 'j2', file_path: '/v/b.iso', mode: 'createdvd', status: 'queued' },
]);
process.stdout.write(JSON.stringify(Object.fromEntries(store.jobs.map((j) => [j.id, j.status]))));
"""


def test_a_cleared_job_is_not_restored_by_a_late_auto_response(tmp_path):
    assert _evaluate(tmp_path, _CLEARED_BEFORE_ADOPT) == {"j2": "processing"}


# #337 (CodeRabbit on #320): Clear completed removed every job that was
# terminal *locally* when its response arrived. A job that finished over SSE
# while the request was in flight was still running when the backend cleared,
# so the backend kept it, yet the store tombstoned and dropped it, and a late
# Auto response could no longer adopt it. The response names what the backend
# deleted (`deleted`); only those ids are removed and tombstoned.
_COMPLETED_WHILE_CLEARING = """
const store = new JobsStore();
store._applyJob({ id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'completed' });
store._applyJob({ id: 'j2', file_path: '/v/b.iso', mode: 'createdvd', status: 'processing' });
api.deleteCompletedJobs = async () => {
  // The backend skipped j2 (still running), then SSE completes it in transit.
  store._applyJob({ id: 'j2', file_path: '/v/b.iso', mode: 'createdvd', status: 'completed' });
  return { deleted: ['j1'] };
};
await store.clearCompleted();
const afterClear = Object.fromEntries(store.jobs.map((j) => [j.id, j.status]));
store.adopt([{ id: 'j2', file_path: '/v/b.iso', mode: 'createdvd', status: 'queued' }]);
process.stdout.write(JSON.stringify({
  afterClear,
  afterAdopt: Object.fromEntries(store.jobs.map((j) => [j.id, j.status])),
  tombstoned: Object.keys(store._evictedIds).sort(),
}));
"""


def test_clear_completed_removes_only_what_the_backend_deleted(tmp_path):
    data = _evaluate(tmp_path, _COMPLETED_WHILE_CLEARING)

    assert data == {
        "afterClear": {"j2": "completed"},
        "afterAdopt": {"j2": "completed"},
        "tombstoned": ["j1"],
    }, data


# One ordering rule for every way a job reaches the store: an id the backend
# has deleted (cleared or evicted) never comes back, whichever path delivers a
# copy built before the deletion. A late SSE frame and a late REST snapshot are
# held to the same tombstone `adopt()` already honours.
_LATE_COPY_AFTER_CLEAR = """
const store = new JobsStore();
store._applyJob({ id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'processing' });
store._applyJob({ id: 'j2', file_path: '/v/b.iso', mode: 'createdvd', status: 'processing' });
// j1 finished on the backend and the clear deleted it before its SSE frame
// reached this tab.
api.deleteCompletedJobs = async () => ({
  deleted: ['j1'],
});
await store.clearCompleted();
const late = { id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'completed' };
const path = %(path)r;
if (path === 'sse') {
  store._applyJob(late);
} else if (path === 'refresh') {
  api.getJobs = async () => [late, store._byId.get('j2')];   // built before the clear
    await store.refresh();
} else {
  store.adopt([late]);
}
process.stdout.write(JSON.stringify(store.jobs.map((j) => j.id)));
"""


@pytest.mark.parametrize("path", ["sse", "refresh", "adopt"])
def test_a_job_the_clear_deleted_never_comes_back(tmp_path, path):
    assert _evaluate(tmp_path, _LATE_COPY_AFTER_CLEAR % {"path": path}) == ["j2"]


# #425: a job one request started, followed until it
# ends whoever is looking. A short job can end before the job stream
# subscribes to it, so it is read from the API: each read joins the list, a
# failed read is tried again, and a job the server no longer has stops it.
_TRACKED = """
const store = new JobsStore();
const answers = %(answers)s;
let calls = 0;
api.getJob = async (id) => {
  const next = answers[Math.min(calls, answers.length - 1)];
  calls += 1;
  if (next.fail) { const err = new Error('read failed'); err.status = next.fail; throw err; }
  return { id, file_path: '/__external_jobs__/' + id, mode: 'dat_rename', ...next };
};
const ended = await store.track('r1', { interval: 1 });
process.stdout.write(JSON.stringify({
  calls, ended: ended && ended.status,
  listed: store.jobs.map((j) => [j.id, j.status]),
}));
"""


def test_a_tracked_job_is_read_until_it_ends_through_failed_reads(tmp_path):
    answers = (
        "[{ fail: 500 }, { status: 'processing' }, { fail: 502 }, { status: 'completed' }]"
    )
    data = _evaluate(tmp_path, _TRACKED % {"answers": answers})

    assert data == {"calls": 4, "ended": "completed", "listed": [["r1", "completed"]]}


def test_a_tracked_job_the_server_no_longer_has_stops_the_tracking(tmp_path):
    data = _evaluate(tmp_path, _TRACKED % {"answers": "[{ status: 'processing' }, { fail: 404 }]"})

    assert data["calls"] == 2 and data["ended"] is None, data
