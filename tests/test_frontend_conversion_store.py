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
  info: (m) => toasts.push(['info', m]),
};
const STORAGE_KEYS = { PRIMARY_TOOL: 'p', AUTO_CONVERT: 'a' };
const readString = (_k, fallback) => fallback;
const writeString = () => {};
const readBool = (_k, fallback) => fallback;
const writeBool = () => {};
const DEFAULT_COMPRESSION_LEVEL_RANGE = { min: 1, max: 22, default: 19 };
const registry = {
  forTool: (id) => ({
    id, compressionStyle: 'multi', defaultCompression: ['zlib'],
    // makeps3iso's folder_to_iso is the registry's folder-input mode.
    modes: id === 'makeps3iso'
      ? [{ mode: 'folder_to_iso', inputKinds: ['directory'] }]
      : [{ mode: 'createcd' }],
  }),
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
    const answer = planAnswer(...args);   // may throw, like a failed request
    events.push(`plan:${args[1]}`);
    return answer;
  },
  async cancelRommRepin(ids) {
    calls.cancel.push(ids);
    return { pending: 0 };
  },
  async createAutoJobs(...args) {
    calls.auto.push(args);
    events.push('queue');
    return autoAnswer(...args);
  },
  async getAutoPlan(paths) {
    calls.autoPlan.push([...paths]);
    return autoPlanAnswer(paths);
  },
};
// One ordered log of the calls whose order matters.
const events = [];
calls.autoPlan = [];
let autoPlanAnswer = (paths) => paths.map((p) => ({ file_path: p, mode: 'createdvd' }));
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
  events,
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
    # Metadata for every target is recorded before anything is queued.
    assert data["events"] == ["plan:createdvd", "plan:dolphin_rvz", "queue"], data
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


_PARTIAL_SNAPSHOT = """
const plan = [
  { file_path: '/v/ps2/a.iso', mode: 'createdvd' },
  { file_path: '/v/gc/b.iso', mode: 'dolphin_rvz' },
];
// The first target's snapshot is saved; the second one's request fails.
planAnswer = (paths, mode) => {
  if (mode === 'dolphin_rvz') throw new Error('RomM went away');
  return {
    recorded: 1, pending: 1,
    recorded_paths: { '/v/ps2/a.iso': '/v/ps2/a.chd' },
    recorded_ids: { '/v/ps2/a.iso': 11 },
  };
};
// ...and then the queue refuses the whole submission.
autoAnswer = () => { throw new Error('queue full'); };
const store = new ConversionStore();
let failed = false;
try {
  await store.submitAuto(['/v/ps2/a.iso', '/v/gc/b.iso'], {
    plan, rommRepin: true, rommPlatformId: 7,
  });
} catch (_e) {
  failed = true;
}
process.stdout.write(JSON.stringify({
  failed,
  cancelled: calls.cancel.flat(),
  warned: toasts.some(([kind, m]) => kind === 'warning' && m.includes('RomM metadata')),
}));
"""


def test_a_failed_later_snapshot_keeps_the_saved_rows_for_reconciliation(tmp_path):
    data = _run(tmp_path, _PARTIAL_SNAPSHOT)

    assert data["failed"] is True, data
    assert data["warned"] is True, data
    # Nothing was queued, so the row the first snapshot saved is retired.
    assert data["cancelled"] == [11], data


def test_a_saved_row_survives_a_later_failed_snapshot_when_its_job_queued(tmp_path):
    scenario = _PARTIAL_SNAPSHOT.replace(
        "autoAnswer = () => { throw new Error('queue full'); };",
        "autoAnswer = () => ({ plan, jobs: [\n"
        "  { id: 'j1', file_path: '/v/ps2/a.iso', output_path: '/v/ps2/a.chd' },\n"
        "] });",
    ).replace(
        "  warned: toasts",
        "  recorded: store.lastRepinRecorded,\n  warned: toasts",
    )
    data = _run(tmp_path, scenario)

    assert data["failed"] is False, data
    assert data["cancelled"] == [], data
    assert data["recorded"] == 1, data


def test_auto_re_records_a_redirected_output_under_its_own_mode(tmp_path):
    data = _run(tmp_path, _AUTO_MISDIRECTED)

    assert data["replanModes"] == ["createdvd", "createdvd"], data
    assert data["replanMisdirected"] == {"/v/ps2/a.iso": "/v/ps2/a_1.chd"}, data
    # The superseded row (11) is retired once the new one exists.
    assert data["cancelled"] == [11], data


_KEPT_AFTER_FAILURE = """
const plan = [
  { file_path: '/v/ps2/a.iso', mode: 'createdvd' },
  { file_path: '/v/gc/b.iso', mode: 'dolphin_rvz' },
];
planAnswer = (paths, mode) => {
  const ids = { '/v/ps2/a.iso': 11, '/v/gc/b.iso': 22 };
  const out = { createdvd: '.chd', dolphin_rvz: '.rvz' }[mode];
  return {
    recorded: paths.length, pending: 2,
    recorded_paths: Object.fromEntries(paths.map((p) => [p, p.replace(/\\.iso$/, out)])),
    recorded_ids: Object.fromEntries(paths.map((p) => [p, ids[p]])),
  };
};
// The route's answer when the dolphin_rvz group is refused after the
// createdvd job already finished: a 409 whose detail carries the kept job in
// the shape a success returns (what client.fetchJson attaches as err.detail).
autoAnswer = () => {
  const err = new Error('taken by another job. Already finished and kept: a.iso.');
  err.status = 409;
  err.detail = {
    message: err.message,
    jobs: [{ id: 'j1', file_path: '/v/ps2/a.iso', output_path: '/v/ps2/a.chd' }],
  };
  throw err;
};
const store = new ConversionStore();
let failed = false;
try {
  await store.submitAuto(['/v/ps2/a.iso', '/v/gc/b.iso'], {
    plan, rommRepin: true, rommPlatformId: 7,
  });
} catch (_e) {
  failed = true;
}
process.stdout.write(JSON.stringify({
  failed,
  cancelled: calls.cancel.flat(),
  adopted: adopted.map((j) => j.id),
  recorded: store.lastRepinRecorded,
}));
"""


def test_a_job_kept_after_an_auto_failure_keeps_its_re_pin_record(tmp_path):
    data = _run(tmp_path, _KEPT_AFTER_FAILURE)

    assert data["failed"] is True, data
    # a.iso's job finished and was kept: its snapshot (11) stays. Only b.iso's
    # (22), which never became a job, is retired.
    assert data["cancelled"] == [22], data
    assert data["adopted"] == ["j1"], data
    assert data["recorded"] == 1, data


_EXPLICIT_TARGET = """
const store = new ConversionStore();
store.auto = true;
store.setMode('createcd');            // programmatic (platform adoption): Auto stays on
const afterSetMode = store.auto;
store.chooseTarget('cso_compress');   // an explicit choice in any selector
process.stdout.write(JSON.stringify({
  afterSetMode,
  auto: store.auto,
  mode: store.mode,
  toasts,
}));
"""


def test_an_explicit_target_choice_turns_auto_off_and_says_so(tmp_path):
    data = _run(tmp_path, _EXPLICIT_TARGET)

    assert data["afterSetMode"] is True, data
    assert data["auto"] is False, data
    assert data["mode"] == "cso_compress", data
    assert any(kind == "info" and "Auto is off" in msg for kind, msg in data["toasts"]), data


def test_every_target_selector_goes_through_choose_target():
    src = Path(__file__).resolve().parents[1] / "src" / "lib" / "components"
    for rel in ("panels/ModeSelect.svelte", "views/RommView.svelte"):
        text = (src / rel).read_text(encoding="utf-8")
        assert "conversion.chooseTarget(" in text, rel
        assert "conversion.setMode(" not in text, rel


_AUTO_SELECTION = """
const store = new ConversionStore();
store.auto = true;
const gate = (entry) => store.allowsInputEntry(entry);
process.stdout.write(JSON.stringify({
  file: gate({ type: 'file', path: '/v/a.iso' }),
  ps3Folder: gate({ type: 'directory', path: '/v/Game', convertible_by: ['makeps3iso'] }),
  plainFolder: gate({ type: 'directory', path: '/v/stuff', convertible_by: [] }),
  fileToolFolder: gate({ type: 'directory', path: '/v/x', convertible_by: ['chdman'] }),
  archive: gate({ type: 'archive', path: '/v/a.zip' }),
}));
"""


def test_auto_offers_a_folder_only_a_folder_mode_can_convert(tmp_path):
    data = _run(tmp_path, _AUTO_SELECTION)

    assert data == {
        "file": True,
        # The backend marked it convertible by a tool with a folder-input mode.
        "ps3Folder": True,
        "plainFolder": False,
        "fileToolFolder": False,
        "archive": False,
    }, data


_RUN_AUTO_GUARD = """
const store = new ConversionStore();
let release;
const confirm = () => new Promise((resolve) => { release = resolve; });
const selection = ['/v/a.iso'];
const first = store.runAuto({ paths: selection, rommPlatformId: null, confirm });
// A second Start while the first is still planning / waiting for confirmation.
const second = await store.runAuto({ paths: ['/v/b.iso'], rommPlatformId: null, confirm });
const inFlight = store.autoInFlight;
// The user changes the selection while the plan modal is open.
selection.push('/v/c.iso');
await new Promise((resolve) => setTimeout(resolve, 0));   // the modal is up
release(true);
await first;
process.stdout.write(JSON.stringify({
  second,
  inFlight,
  after: store.autoInFlight,
  planned: calls.autoPlan,
  queued: calls.auto.map((args) => args[0]),
}));
"""


def test_run_auto_blocks_a_second_start_and_submits_the_planned_selection(tmp_path):
    data = _run(tmp_path, _RUN_AUTO_GUARD)

    assert data["second"] is None, data           # refused, not queued twice
    assert data["inFlight"] is True, data
    assert data["after"] is False, data            # released in finally
    assert data["planned"] == [["/v/a.iso"]], data
    assert data["queued"] == [["/v/a.iso"]], data  # the snapshot, not the live selection


_RUN_AUTO_BADGE_ON_FAILURE = """
planAnswer = (paths) => ({
  recorded: 1, pending: 4,
  recorded_paths: { '/v/a.iso': '/v/a.chd' }, recorded_ids: { '/v/a.iso': 11 },
});
autoAnswer = () => {
  const err = new Error('later group refused. Already finished and kept: a.iso.');
  err.detail = { message: err.message, jobs: [
    { id: 'j1', file_path: '/v/a.iso', output_path: '/v/a.chd' },
  ] };
  throw err;
};
const badge = [];
const store = new ConversionStore();
await store.runAuto({
  paths: ['/v/a.iso'], rommPlatformId: 7,
  confirm: async () => true,
  onRepinPending: (n) => badge.push(n),
});
process.stdout.write(JSON.stringify({ badge, after: store.autoInFlight }));
"""


def test_run_auto_updates_the_repin_badge_even_when_queueing_fails(tmp_path):
    data = _run(tmp_path, _RUN_AUTO_BADGE_ON_FAILURE)

    assert data["badge"] == [4], data
    assert data["after"] is False, data


# --- An aborted Auto run (Codex 4576827983) ----------------------------------
# The panel passes an AbortSignal and aborts it when it unmounts. An aborted
# run never opens the confirmation, resolves a pending one as cancelled, and
# clears autoInFlight, so Start works again when the panel comes back. The
# stubbed getAutoPlan ignores the signal, so the store must not rely on the
# request itself rejecting.

_RUN_AUTO_ABORT_MID_PLAN = """
const store = new ConversionStore();
let answerPlan;
autoPlanAnswer = (paths) => new Promise((resolve) => {
  answerPlan = () => resolve(paths.map((p) => ({ file_path: p, mode: 'createdvd' })));
});
let confirmCalls = 0;
const confirm = () => { confirmCalls += 1; return Promise.resolve(true); };
const controller = new AbortController();
const run = store.runAuto({ paths: ['/v/a.iso'], confirm, signal: controller.signal });
await new Promise((resolve) => setTimeout(resolve, 0));   // the plan is loading
const during = store.autoInFlight;
controller.abort();                                       // the panel unmounts
const result = await run;
const afterAbort = store.autoInFlight;
answerPlan();                                             // the plan lands late
await new Promise((resolve) => setTimeout(resolve, 0));
// Back on the workspace: Start runs a fresh flow.
autoPlanAnswer = (paths) => paths.map((p) => ({ file_path: p, mode: 'createdvd' }));
const again = await store.runAuto({ paths: ['/v/b.iso'], confirm: () => Promise.resolve(true) });
process.stdout.write(JSON.stringify({
  during, result, afterAbort, confirmCalls,
  planAfter: store.autoPlan,
  queued: calls.auto.map((args) => args[0]),
  againQueued: again !== null,
  errors: toasts.filter(([kind]) => kind === 'error'),
}));
"""


def test_an_aborted_auto_run_mid_plan_never_confirms_and_frees_start(tmp_path):
    data = _run(tmp_path, _RUN_AUTO_ABORT_MID_PLAN)

    assert data["during"] is True, data
    assert data["result"] is None, data
    assert data["afterAbort"] is False, data       # Start is enabled again
    assert data["confirmCalls"] == 0, data         # the modal never opened
    assert data["planAfter"] is None, data         # the late plan was dropped
    assert data["queued"] == [["/v/b.iso"]], data  # only the fresh run queued
    assert data["againQueued"] is True, data
    assert data["errors"] == [], data


_RUN_AUTO_ABORT_IN_CONFIRM = """
const store = new ConversionStore();
let opened = false;
const confirm = () => { opened = true; return new Promise(() => {}); };  // never answered
const controller = new AbortController();
const run = store.runAuto({ paths: ['/v/a.iso'], confirm, signal: controller.signal });
await new Promise((resolve) => setTimeout(resolve, 0));   // the modal is up
controller.abort();
const result = await run;
process.stdout.write(JSON.stringify({
  opened, result, after: store.autoInFlight,
  planAfter: store.autoPlan,
  queued: calls.auto.length,
}));
"""


def test_an_aborted_auto_run_resolves_a_pending_confirm_as_cancelled(tmp_path):
    data = _run(tmp_path, _RUN_AUTO_ABORT_IN_CONFIRM)

    assert data == {
        "opened": True, "result": None, "after": False, "planAfter": None, "queued": 0,
    }, data
