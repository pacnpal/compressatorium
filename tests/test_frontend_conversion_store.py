"""Behavioural guards for the convert store's submit paths.

``src/lib/stores/conversion.svelte.js`` queues conversions. When the rows come
from a RomM platform listing it first records each source's RomM metadata
(``planRommRepin``) so the output can be re-identified after RomM rescans, then
reconciles those records against the jobs actually created. Auto must do the
same for each target it planned, through the same code.

Evaluated with Node, like ``test_frontend_romm_store``: imports dropped, runes
reduced to their initial values, sibling modules stubbed. Skipped without Node.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_STORE_JS = Path(__file__).resolve().parents[1] / "src" / "lib" / "stores" / "conversion.svelte.js"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _plain_js() -> str:
    src = _STORE_JS.read_text(encoding="utf-8")
    body = re.sub(r"^import[\s\S]*?from\s+'[^']+';\s*$", "", src, flags=re.MULTILINE)
    body = re.sub(r"\$state\.raw\(", "(", body)
    body = re.sub(r"\$state\(", "(", body)
    leftover = [r for r in ("$state(", "$derived(", "$effect(") if r in body]
    assert not leftover, f"conversion.svelte.js grew runes this harness does not reduce: {leftover}"
    return body


_STUBS = """
const toasts = [];
const toast = {
  success: (m) => toasts.push(['success', m]),
  warning: (m) => toasts.push(['warning', m]),
  error: (m) => toasts.push(['error', m]),
};
const STORAGE_KEYS = { PRIMARY_TOOL: 'p', AUTO_CONVERT: 'a' };
const readString = (_k, fallback) => fallback;
const writeString = () => {};
const readBool = (_k, fallback) => fallback;
const writeBool = () => {};
const DEFAULT_COMPRESSION_LEVEL_RANGE = { min: 1, max: 22, default: 19 };
const registry = {
  forTool: (id) => ({ id, compressionStyle: 'multi', defaultCompression: ['zlib'] }),
  defaultMode: () => 'createcd',
  specFor: () => ({ supportsCompression: true }),
  maxCodecsFor: () => 4,
};
const calls = { plan: [], cancel: [], auto: [] };
let planAnswer = () => ({});
let autoAnswer = () => ({ plan: [], jobs: [] });
const api = {
  async planRommRepin(...args) {
    calls.plan.push(args);
    return planAnswer(...args);
  },
  async cancelRommRepin(ids) {
    calls.cancel.push(ids);
    return { pending: 0 };
  },
  async createAutoJobs(...args) {
    calls.auto.push(args);
    return autoAnswer(...args);
  },
};
const adopted = [];
const jobs = {
  adopt: (list) => adopted.push(...list),
  async createBatch() { return []; },
};
"""


def _run(tmp_path: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the conversion store")
    script = tmp_path / "conversion_store_eval.mjs"
    script.write_text(_STUBS + _plain_js() + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the conversion store via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


_AUTO_REPIN = """
const plan = [
  { file_path: '/v/ps2/a.iso', mode: 'createdvd' },
  { file_path: '/v/gc/b.iso', mode: 'dolphin_rvz' },
  { file_path: '/v/x/c.iso', mode: null, skip_reason: 'Unsorted' },
];
// One plan call per target mode; each records its own sources.
planAnswer = (paths, mode) => {
  const ids = { '/v/ps2/a.iso': 11, '/v/gc/b.iso': 22 };
  const out = { createdvd: '.chd', dolphin_rvz: '.rvz' }[mode];
  return {
    recorded: paths.length,
    pending: 2,
    recorded_paths: Object.fromEntries(paths.map((p) => [p, p.replace(/\\.iso$/, out)])),
    recorded_ids: Object.fromEntries(paths.map((p) => [p, ids[p]])),
  };
};
// The backend queued only a.iso; b.iso was skipped (its output exists).
autoAnswer = () => ({
  plan,
  jobs: [{ id: 'j1', file_path: '/v/ps2/a.iso', output_path: '/v/ps2/a.chd' }],
});

const store = new ConversionStore();
await store.submitAuto(
  ['/v/ps2/a.iso', '/v/gc/b.iso', '/v/x/c.iso'],
  { plan, rommRepin: true, rommPlatformId: 7 },
);

process.stdout.write(JSON.stringify({
  plans: calls.plan.map(([paths, mode, outDir, dup, platform]) => ({
    paths, mode, outDir, dup, platform,
  })),
  planBeforeQueue: calls.plan.length > 0 && calls.auto.length === 1,
  cancelled: calls.cancel.flat(),
  recorded: store.lastRepinRecorded,
  adopted: adopted.map((j) => j.id),
}));
"""


def test_auto_records_romm_metadata_per_target_and_reconciles(tmp_path):
    data = _run(tmp_path, _AUTO_REPIN)

    assert data["plans"] == [
        {"paths": ["/v/ps2/a.iso"], "mode": "createdvd", "outDir": None,
         "dup": "skip", "platform": 7},
        {"paths": ["/v/gc/b.iso"], "mode": "dolphin_rvz", "outDir": None,
         "dup": "skip", "platform": 7},
    ], data
    assert data["planBeforeQueue"] is True, data
    # b.iso never became a job, so its row is retired; a.iso's is kept.
    assert data["cancelled"] == [22], data
    assert data["recorded"] == 1, data
    assert data["adopted"] == ["j1"], data


_AUTO_MISDIRECTED = """
const plan = [{ file_path: '/v/ps2/a.iso', mode: 'createdvd' }];
planAnswer = (paths, mode, _o, _d, _p, misdirected) => {
  const dest = misdirected ? misdirected['/v/ps2/a.iso'] : '/v/ps2/a.chd';
  return {
    recorded: 1, pending: 1,
    recorded_paths: { '/v/ps2/a.iso': dest },
    recorded_ids: { '/v/ps2/a.iso': misdirected ? 12 : 11 },
  };
};
// The queue wrote somewhere else (Rename picked a new name).
autoAnswer = () => ({
  plan,
  jobs: [{ id: 'j1', file_path: '/v/ps2/a.iso', output_path: '/v/ps2/a_1.chd' }],
});
const store = new ConversionStore();
await store.submitAuto(['/v/ps2/a.iso'], { plan, rommRepin: true, rommPlatformId: 7 });
process.stdout.write(JSON.stringify({
  replanModes: calls.plan.map((args) => args[1]),
  replanMisdirected: calls.plan[1]?.[5] ?? null,
  cancelled: calls.cancel.flat(),
}));
"""


_MANUAL_REPIN = """
planAnswer = (paths) => ({
  recorded: paths.length, pending: 1,
  recorded_paths: { '/v/a.iso': '/v/a.chd' }, recorded_ids: { '/v/a.iso': 5 },
});
jobs.createBatch = async () => [{ id: 'j', file_path: '/v/a.iso', output_path: '/v/a.chd' }];
const store = new ConversionStore();
store.mode = 'createdvd';
await store.submit(['/v/a.iso'], { duplicateAction: 'rename', rommRepin: true, rommPlatformId: 3 });
process.stdout.write(JSON.stringify({ plans: calls.plan, cancelled: calls.cancel.flat() }));
"""


def test_manual_submit_still_records_once_for_the_selected_mode(tmp_path):
    data = _run(tmp_path, _MANUAL_REPIN)

    # The same five arguments as before the shared path existed.
    assert data["plans"] == [[["/v/a.iso"], "createdvd", None, "rename", 3]], data
    assert data["cancelled"] == [], data


def test_auto_re_records_a_redirected_output_under_its_own_mode(tmp_path):
    data = _run(tmp_path, _AUTO_MISDIRECTED)

    assert data["replanModes"] == ["createdvd", "createdvd"], data
    assert data["replanMisdirected"] == {"/v/ps2/a.iso": "/v/ps2/a_1.chd"}, data
    # The superseded row (11) is retired once the new one exists.
    assert data["cancelled"] == [11], data
