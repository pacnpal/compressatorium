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


# CodeRabbit 4577311063: SSE completes j1 and the history prune evicts it
# before the Auto response arrives. With its row gone, the response's older
# `queued` snapshot looked like an unknown job and went back into the active
# queue for good. Eviction leaves a tombstone, and adopt() skips it.
_EVICTED_BEFORE_ADOPT = """
const store = new JobsStore();
store._applyJob({ id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'completed' });
store._applyHistoryOverflow({ generation: 'g', seq: 1, evicted_ids: ['j1'], evicted: {} });
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
store._applyHistoryOverflow({ generation: 'g', seq: 1, evicted_ids: ids, evicted: {} });
process.stdout.write(JSON.stringify({ kept: store._evictedOrder.length }));
"""


def test_eviction_tombstones_are_bounded(tmp_path):
    kept = _evaluate(tmp_path, _TOMBSTONES_ARE_BOUNDED)["kept"]

    assert 0 < kept <= 2000
