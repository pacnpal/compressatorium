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

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"
_STORE_JS = _SRC / "stores" / "conversion.svelte.js"
# The store's one real (unstubbed) sibling: the shared level fit/clamp, which
# has no imports and is loaded as it ships.
_LEVELS_JS = _SRC / "tools" / "levels.js"
_PICKER = _SRC / "components" / "panels" / "CompressionPicker.svelte"


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
  codecsForMode: () => [],
  // registry.js's own rule, over whatever codecsForMode a test sets.
  offeredCodecs(mode, selection) {
    const offered = this.codecsForMode(mode).map((c) => c.value);
    return offered.length ? selection.filter((c) => offered.includes(c)) : [...selection];
  },
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
    levels = re.sub(r"^export ", "", _LEVELS_JS.read_text(encoding="utf-8"), flags=re.MULTILINE)
    script.write_text(_STUBS + levels + _plain_js() + scenario, encoding="utf-8")
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
// The route queues a.iso and leaves b.iso out (its plan changed since it
// was confirmed, #520): b.iso is reported in `skipped`, not refused as a 409.
autoAnswer = () => ({
  plan,
  jobs: [{ id: 'j1', file_path: '/v/ps2/a.iso', output_path: '/v/ps2/a.chd' }],
  skipped: [{ path: '/v/gc/b.iso', reason: 'Its plan changed', code: 'plan_changed' }],
});
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
  toasts,
}));
"""


def test_an_image_auto_left_out_retires_only_its_own_re_pin_record(tmp_path):
    data = _run(tmp_path, _KEPT_AFTER_FAILURE)

    assert data["failed"] is False, data
    # a.iso became a job: its snapshot (11) stays. Only b.iso's (22), which
    # Auto left out, is retired.
    assert data["cancelled"] == [22], data
    assert data["adopted"] == ["j1"], data
    assert data["recorded"] == 1, data
    assert any(
        kind == "success" and "1 skipped" in msg for kind, msg in data["toasts"]
    ), data


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
  queued: calls.auto.map((args) => args[0].map((item) => item.file_path)),
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
  throw new Error('the queue did not answer');
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

    # Nothing was queued, so the snapshot is retired and the badge is told the
    # count the retirement answered (0), not the stale 4 from before it.
    assert data["badge"] == [0], data
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
  queued: calls.auto.map((args) => args[0].map((item) => item.file_path)),
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


# --- The target is captured before the first await (CodeRabbit 4576895563/5) --
# The user can change the mode, its options or the output folder while the
# RomM snapshot or the Auto plan request is pending. What is queued must be
# what was planned: everything target-dependent is read once, up front.

_SUBMIT_CAPTURES_TARGET = """
registry.specFor = () => ({
  supportsCompression: true, supportsDeleteOnVerify: true,
  supportsSplit: true, supportsRoundTrip: true,
});
const store = new ConversionStore();
store.mode = 'createcd';
store.outputDir = '/out/old';
store.deleteOnVerify = true;
store.split = false;
store.containerOnly = false;
let release;
planAnswer = (paths) => new Promise((resolve) => {
  release = () => resolve({
    recorded: 1, pending: 1,
    recorded_paths: { [paths[0]]: '/out/old/a.chd' },
    recorded_ids: { [paths[0]]: 7 },
  });
});
const batches = [];
jobs.createBatch = async (paths, mode, opts) => {
  batches.push({ paths, mode, ...opts });
  return [{ id: 'j1', file_path: paths[0], output_path: '/out/old/a.chd' }];
};
const run = store.submit(['/v/a.iso'], { rommRepin: true, rommPlatformId: 3 });
await new Promise((resolve) => setTimeout(resolve, 0));   // the snapshot is pending
// The user switches target, options and folder meanwhile.
store.mode = 'createdvd';
store.outputDir = '/out/new';
store.deleteOnVerify = false;
store.split = true;
store.containerOnly = true;
release();
await run;
process.stdout.write(JSON.stringify({
  planned: calls.plan.map((args) => [args[1], args[2]]),
  batch: batches[0],
}));
"""


def test_submit_queues_the_target_it_planned(tmp_path):
    data = _run(tmp_path, _SUBMIT_CAPTURES_TARGET)

    assert data["planned"] == [["createcd", "/out/old"]], data
    batch = data["batch"]
    assert batch["mode"] == "createcd", data
    assert batch["outputDir"] == "/out/old", data
    assert batch["deleteOnVerify"] is True, data
    assert batch["split"] is False, data
    assert batch["containerOnly"] is False, data


_RUN_AUTO_CAPTURES_OUTPUT_DIR = """
const store = new ConversionStore();
store.outputDir = '/out/old';
let releasePlan;
const planDirs = [];
api.getAutoPlan = (paths, outputDir) => {
  planDirs.push(outputDir);
  return new Promise((resolve) => {
    releasePlan = () => resolve(paths.map((p) => ({ file_path: p, mode: 'createdvd' })));
  });
};
planAnswer = (paths) => ({
  recorded: 1, pending: 1,
  recorded_paths: { [paths[0]]: '/out/old/a.chd' }, recorded_ids: { [paths[0]]: 9 },
});
autoAnswer = (paths) => ({
  plan: [], jobs: [{ id: 'j1', file_path: paths[0], output_path: '/out/old/a.chd' }],
});
const run = store.runAuto({
  paths: ['/v/a.iso'], rommPlatformId: 3, confirm: () => Promise.resolve(true),
});
await new Promise((resolve) => setTimeout(resolve, 0));   // the plan is loading
store.outputDir = '/out/new';                             // edited meanwhile
releasePlan();
await run;
process.stdout.write(JSON.stringify({
  planDirs,
  repinDirs: calls.plan.map((args) => args[2]),
  queueDirs: calls.auto.map((args) => args[1]),
}));
"""


def test_run_auto_uses_the_output_folder_it_previewed(tmp_path):
    data = _run(tmp_path, _RUN_AUTO_CAPTURES_OUTPUT_DIR)

    assert data == {
        "planDirs": ["/out/old"], "repinDirs": ["/out/old"], "queueDirs": ["/out/old"],
    }, data


# --- The server enforces the confirmed plan (Codex 4576967499) ----------------
# Only the confirmed plan's actionable rows are sent, each with the target the
# user saw; an Image the preview skipped is never sent. When the server's
# replan differs it answers 409 with the new plan, and the flow shows that plan
# and asks again.

_RUN_AUTO_SENDS_CONFIRMED_ITEMS = """
const store = new ConversionStore();
autoPlanAnswer = () => [
  { file_path: '/v/a.iso', mode: 'createdvd' },
  { file_path: '/v/x.iso', mode: null, how: 'unsorted', skip_reason: 'Unsorted' },
];
await store.runAuto({ paths: ['/v/a.iso', '/v/x.iso'], confirm: () => Promise.resolve(true) });
process.stdout.write(JSON.stringify({ sent: calls.auto.map((args) => args[0]) }));
"""


def test_run_auto_sends_only_the_confirmed_actionable_items(tmp_path):
    data = _run(tmp_path, _RUN_AUTO_SENDS_CONFIRMED_ITEMS)

    assert data["sent"] == [[{"file_path": "/v/a.iso", "mode": "createdvd"}]], data


_RUN_AUTO_REPORTS_A_CHANGED_PLAN = """
const store = new ConversionStore();
autoPlanAnswer = () => [{ file_path: '/v/a.iso', mode: 'createdvd' }];
autoAnswer = () => ({
  plan: [{ file_path: '/v/a.iso', mode: null, skip_reason: 'Its plan changed' }],
  jobs: [],
  skipped: [{ path: '/v/a.iso', reason: 'Its plan changed', code: 'plan_changed' }],
});
let asked = 0;
const confirm = () => { asked += 1; return Promise.resolve(true); };
const result = await store.runAuto({ paths: ['/v/a.iso'], confirm });
process.stdout.write(JSON.stringify({
  asked, sent: calls.auto.length, queued: result !== null,
  inFlight: store.autoInFlight, toasts,
}));
"""


def test_a_changed_plan_is_reported_not_asked_again(tmp_path):
    """The server leaves a changed Image out and says so (#520): one confirm,
    one request, and a toast that names it."""
    data = _run(tmp_path, _RUN_AUTO_REPORTS_A_CHANGED_PLAN)

    assert data["asked"] == 1 and data["sent"] == 1, data
    assert data["inFlight"] is False, data
    assert ["warning", "No jobs queued (all 1 skipped)"] in data["toasts"], data


# --- Auto turned off mid-run; abort during the RomM snapshot ------------------
# CodeRabbit 4577045040: picking a target by hand turns Auto off; a run whose
# plan was still loading then ends without a modal or a queue.
# CodeRabbit 4577045042: an abort while the RomM snapshot is being taken stops
# the run before createAutoJobs, and the recorded metadata is retired.

_RUN_AUTO_ENDS_WHEN_AUTO_TURNS_OFF = """
const store = new ConversionStore();
store.auto = true;
let answerPlan;
autoPlanAnswer = (paths) => new Promise((resolve) => {
  answerPlan = () => resolve(paths.map((p) => ({ file_path: p, mode: 'createdvd' })));
});
let asked = 0;
const run = store.runAuto({
  paths: ['/v/a.iso'], confirm: () => { asked += 1; return Promise.resolve(true); },
});
await new Promise((resolve) => setTimeout(resolve, 0));   // the plan is loading
store.chooseTarget('createcd');                           // a hand-picked target
answerPlan();
const result = await run;
process.stdout.write(JSON.stringify({
  auto: store.auto, asked, result, queued: calls.auto.length,
  inFlight: store.autoInFlight, plan: store.autoPlan,
}));
"""


def test_turning_auto_off_while_the_plan_loads_ends_the_run(tmp_path):
    data = _run(tmp_path, _RUN_AUTO_ENDS_WHEN_AUTO_TURNS_OFF)

    assert data == {
        "auto": False, "asked": 0, "result": None, "queued": 0,
        "inFlight": False, "plan": None,
    }, data


_RUN_AUTO_ABORT_DURING_THE_SNAPSHOT = """
const store = new ConversionStore();
let releaseSnapshot;
planAnswer = (paths) => new Promise((resolve) => {
  releaseSnapshot = () => resolve({
    recorded: 1, pending: 1,
    recorded_paths: { [paths[0]]: '/out/a.chd' }, recorded_ids: { [paths[0]]: 42 },
  });
});
const controller = new AbortController();
const run = store.runAuto({
  paths: ['/v/a.iso'], rommPlatformId: 3, confirm: () => Promise.resolve(true),
  signal: controller.signal,
});
for (let i = 0; i < 5 && !releaseSnapshot; i += 1) {
  await new Promise((resolve) => setTimeout(resolve, 0));  // the snapshot is pending
}
controller.abort();
releaseSnapshot();
const result = await run;
process.stdout.write(JSON.stringify({
  result,
  queued: calls.auto.length,
  retired: calls.cancel,
  errors: toasts.filter(([kind]) => kind === 'error'),
  inFlight: store.autoInFlight,
}));
"""


def test_an_abort_during_the_snapshot_never_queues_and_retires_the_metadata(tmp_path):
    data = _run(tmp_path, _RUN_AUTO_ABORT_DURING_THE_SNAPSHOT)

    assert data == {
        "result": None, "queued": 0, "retired": [[42]], "errors": [], "inFlight": False,
    }, data


# CodeRabbit 4578050381: the snapshot can stay pending (a stalled RomM). The
# wait for it is abort-aware: closing the panel frees Start at once (neither
# autoInFlight nor converting stays set), and the rows the stalled snapshot
# records when it finally returns are retired on their own, without touching
# a newer run's state.
_RUN_AUTO_ABORT_WHILE_THE_SNAPSHOT_STALLS = """
const store = new ConversionStore();
let releaseOld;
planAnswer = (paths) => new Promise((resolve) => {
  releaseOld = () => resolve({
    recorded: 1, pending: 1,
    recorded_paths: { [paths[0]]: '/out/a.chd' }, recorded_ids: { [paths[0]]: 42 },
  });
});
const controller = new AbortController();
const run = store.runAuto({
  paths: ['/v/a.iso'], rommPlatformId: 3, confirm: () => Promise.resolve(true),
  signal: controller.signal,
});
for (let i = 0; i < 5 && !releaseOld; i += 1) {
  await new Promise((resolve) => setTimeout(resolve, 0));  // the snapshot is pending
}
controller.abort();                                        // the panel closes
const result = await run;                                  // no release: it stalls
const freed = { inFlight: store.autoInFlight, converting: store.converting };

// A new run on the returning panel, with a snapshot that answers.
planAnswer = (paths) => ({
  recorded: 1, pending: 7,
  recorded_paths: { [paths[0]]: '/out/b.chd' }, recorded_ids: { [paths[0]]: 77 },
});
autoAnswer = (items) => ({
  plan: [], jobs: [{ id: 'j2', file_path: items[0].file_path, output_path: '/out/b.chd' }],
});
const second = await store.runAuto({
  paths: ['/v/b.iso'], rommPlatformId: 3, confirm: () => Promise.resolve(true),
});
const newRun = { pending: store.lastRepinPending, recorded: store.lastRepinRecorded };

releaseOld();                                              // the stalled snapshot returns
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
process.stdout.write(JSON.stringify({
  result, freed, secondQueued: second !== null, newRun,
  afterLate: { pending: store.lastRepinPending, recorded: store.lastRepinRecorded,
               converting: store.converting, inFlight: store.autoInFlight },
  queued: calls.auto.map((args) => args[0].map((item) => item.file_path)),
  retired: calls.cancel,
}));
"""


def test_an_abort_while_the_snapshot_stalls_frees_start_and_the_late_rows_retire_alone(
    tmp_path,
):
    data = _run(tmp_path, _RUN_AUTO_ABORT_WHILE_THE_SNAPSHOT_STALLS)

    assert data["result"] is None, data
    assert data["freed"] == {"inFlight": False, "converting": False}, data
    assert data["secondQueued"] is True, data
    assert data["queued"] == [["/v/b.iso"]], data              # only the new run queued
    assert data["afterLate"] == {
        "pending": data["newRun"]["pending"], "recorded": data["newRun"]["recorded"],
        "converting": False, "inFlight": False,
    }, data
    assert [42] in data["retired"], data                       # the late rows, retired
    assert all(77 not in ids for ids in data["retired"]), data # the new run's row kept


# --- #337: a RomM snapshot's order is the run's, not its requests' -----------
# A run plans one request per target mode. The server orders snapshots by the
# generation a plan was given when it started, and refuses to let an older one
# supersede a newer run's row. So every request of a run carries the
# generation its first request was given: a later group sent after another run
# began is still as old as its own run. And an aborted run's planner sends no
# further groups at all, whatever its pending request does when it returns.

_RUN_CARRIES_ITS_GENERATION = """
planAnswer = (paths, mode) => ({
  recorded: 1, pending: 1,
  recorded_paths: { [paths[0]]: paths[0] + '.out' },
  recorded_ids: { [paths[0]]: mode === 'createdvd' ? 1 : 2 },
  generation: mode === 'createdvd' ? 5 : 99,
});
const plan = [
  { file_path: '/v/ps2/a.iso', mode: 'createdvd' },
  { file_path: '/v/gc/b.iso', mode: 'dolphin_rvz' },
];
autoAnswer = () => ({ plan, jobs: [] });
const store = new ConversionStore();
await store.submitAuto(['/v/ps2/a.iso', '/v/gc/b.iso'], {
  plan, rommRepin: true, rommPlatformId: 7,
});
process.stdout.write(JSON.stringify({
  generations: calls.plan.map((args) => [args[1], args[6] ?? null]),
}));
"""


def test_every_snapshot_request_of_a_run_carries_the_run_s_generation(tmp_path):
    data = _run(tmp_path, _RUN_CARRIES_ITS_GENERATION)

    assert data["generations"] == [["createdvd", None], ["dolphin_rvz", 5]], data


_ABORTED_PLANNER_SENDS_NO_MORE_GROUPS = """
let releaseFirst;
planAnswer = (paths, mode) => {
  const answer = {
    recorded: 1, pending: 1,
    recorded_paths: { [paths[0]]: paths[0] + '.out' },
    recorded_ids: { [paths[0]]: mode === 'createdvd' ? 1 : 2 },
    generation: 5,
  };
  if (mode !== 'createdvd') return answer;
  return new Promise((resolve) => { releaseFirst = () => resolve(answer); });
};
const plan = [
  { file_path: '/v/ps2/a.iso', mode: 'createdvd' },
  { file_path: '/v/gc/b.iso', mode: 'dolphin_rvz' },
];
const controller = new AbortController();
const store = new ConversionStore();
const run = store.submitAuto(['/v/ps2/a.iso', '/v/gc/b.iso'], {
  plan, rommRepin: true, rommPlatformId: 7, signal: controller.signal,
}).catch((e) => ({ aborted: Boolean(e?.aborted) }));
for (let i = 0; i < 5 && !releaseFirst; i += 1) {
  await new Promise((resolve) => setTimeout(resolve, 0));
}
controller.abort();                       // the panel closes mid-snapshot
const result = await run;
releaseFirst();                           // the first group's request returns late
for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
process.stdout.write(JSON.stringify({
  result,
  modes: calls.plan.map((args) => args[1]),
  queued: calls.auto.length,
  retired: calls.cancel,
}));
"""


def test_an_aborted_run_s_planner_sends_no_further_snapshot_requests(tmp_path):
    data = _run(tmp_path, _ABORTED_PLANNER_SENDS_NO_MORE_GROUPS)

    assert data["result"] == {"aborted": True}, data
    # Only the request already in flight: no second group after the abort.
    assert data["modes"] == ["createdvd"], data
    assert data["queued"] == 0, data
    # What the late request did record is still retired.
    assert data["retired"] == [[1]], data


def test_a_codec_picked_for_rvz_never_reaches_wia(tmp_path):
    """#345, codex 4581559119: the codec selection is kept per tool, so zstd
    picked for RVZ is still selected when the mode turns to WIA, which
    dolphin-tool refuses with zstd. The store sends the mode's own default
    instead; RVZ still gets what was picked."""
    data = _run(tmp_path, """
const CODECS = {
  dolphin_rvz: ['none', 'zstd', 'bzip2', 'lzma', 'lzma2'],
  dolphin_wia: ['none', 'bzip2', 'lzma', 'lzma2'],
};
const DEFAULTS = { dolphin_rvz: ['zstd'], dolphin_wia: ['none'] };
registry.specFor = () => ({ supportsCompression: true, supportsCompressionLevel: true });
registry.codecsForMode = (m) => (CODECS[m] ?? []).map((value) => ({ value }));
registry.defaultCompressionForMode = (m) => DEFAULTS[m];
registry.levelRangeFor = (_m, codec) => (
  codec === 'zstd' ? { min: 1, max: 22, default: 19 } : { min: 1, max: 9, default: 5 }
);
const store = new ConversionStore();
store.compressionSelection = ['zstd'];
store.compressionLevel = '19';
store.mode = 'dolphin_rvz';
const rvz = store.compressionValue;
store.mode = 'dolphin_wia';
const wia = store.compressionValue;
store.compressionSelection = ['lzma2'];
const chosen = store.compressionValue;
process.stdout.write(JSON.stringify({ rvz, wia, chosen }));
""")
    assert data == {"rvz": "zstd:19", "wia": "none", "chosen": "lzma2:5"}


def test_a_chdman_codec_the_mode_cannot_use_is_never_sent(tmp_path):
    """#471: the chdman codec list is kept per tool, so CD codecs picked for
    Create CD are still selected when the mode turns to Create DVD, which
    chdman refuses them on (a DVD hunk is not whole CD frames). The store sends
    only the codecs the mode takes, and nothing (chdman's own) when none is
    left; Create CD still gets what was picked."""
    data = _run(tmp_path, """
const GENERAL = ['none', 'zlib', 'zstd', 'lzma', 'huff', 'flac'];
const CODECS = {
  createcd: [...GENERAL, 'cdzl', 'cdzs', 'cdlz', 'cdfl'],
  createdvd: GENERAL,
};
registry.codecsForMode = (m) => (CODECS[m] ?? []).map((value) => ({ value }));
const store = new ConversionStore();
store.compressionSelection = ['cdlz', 'zlib', 'cdfl'];
store.mode = 'createcd';
const cd = store.compressionValue;
store.mode = 'createdvd';
const dvd = store.compressionValue;
store.compressionSelection = ['cdlz', 'cdzl'];
const onlyCd = store.compressionValue;
store.compressionSelection = ['none'];
const none = store.compressionValue;
// Four CD codecs kept from Create CD don't fill Create DVD's four-codec cap.
store.compressionSelection = ['cdlz', 'cdzl', 'cdzs', 'cdfl'];
store.toggleCodec('zlib');
const toggled = store.compressionSelection;
process.stdout.write(JSON.stringify({ cd, dvd, onlyCd, none, toggled }));
""")
    assert data == {
        "cd": "cdlz,zlib,cdfl", "dvd": "zlib", "onlyCd": None, "none": "none",
        "toggled": ["zlib"],
    }


def test_switching_codec_resets_a_level_the_new_codec_refuses(tmp_path):
    """dolphin-tool takes zstd at 1-22 but lzma2 only at 1-9: switching from
    zstd at 19 to lzma2 lands on lzma2's own default, never 19. A level the new
    codec takes is kept."""
    data = _run(tmp_path, """
const RANGES = {
  zstd: { min: 1, max: 22, default: 19 },
  lzma2: { min: 1, max: 9, default: 5 },
  bzip2: { min: 1, max: 9, default: 5 },
};
registry.specFor = () => ({ supportsCompression: true, supportsCompressionLevel: true });
registry.codecsForMode = () => ['none', 'zstd', 'lzma2', 'bzip2'].map((value) => ({ value }));
registry.defaultCompressionForMode = () => ['zstd'];
registry.levelRangeFor = (_mode, codec) => RANGES[codec];
const store = new ConversionStore();
store.mode = 'dolphin_rvz';
store.setSingleCodec('zstd');
store.setCompressionLevel('19');
store.setSingleCodec('lzma2');
const reset = { level: store.compressionLevel, sent: store.compressionValue };
store.setCompressionLevel('7');
store.setSingleCodec('bzip2');
const kept = store.compressionLevel;
process.stdout.write(JSON.stringify({ reset, kept }));
""")
    assert data == {"reset": {"level": "5", "sent": "lzma2:5"}, "kept": "7"}


_DOLPHIN_LEVELS = """
const CODECS = {
  dolphin_rvz: ['none', 'zstd', 'bzip2', 'lzma', 'lzma2'],
  dolphin_wia: ['none', 'bzip2', 'lzma', 'lzma2'],
};
const DEFAULTS = { dolphin_rvz: ['zstd'], dolphin_wia: ['none'] };
registry.specFor = (m) => (CODECS[m]
  ? { mode: m, supportsCompression: true, supportsCompressionLevel: true } : null);
registry.codecsForMode = (m) => (CODECS[m] ?? []).map((value) => ({ value }));
registry.defaultCompressionForMode = (m) => DEFAULTS[m];
registry.levelRangeFor = (_m, codec) => (
  codec === 'zstd' ? { min: 1, max: 22, default: 19 } : { min: 1, max: 9, default: 5 }
);
const store = new ConversionStore();
store.setMode('dolphin_rvz');
"""


def test_a_mode_switch_keeps_the_level_in_the_codecs_range(tmp_path):
    """RVZ+lzma2 at 19 switched to WIA: the codec carries over, and the level,
    which both the slider and the number box read, becomes lzma2's default
    instead of staying at a 19 the backend refuses."""
    data = _run(tmp_path, _DOLPHIN_LEVELS + """
store.setSingleCodec('lzma2');
// Held from before the ranges (a saved preference, or zstd at 19).
store.compressionLevel = '19';
store.setMode('dolphin_wia');
process.stdout.write(JSON.stringify({
  codec: store.singleCodec, level: store.compressionLevel,
  range: store.levelRange, sent: store.compressionValue,
}));
""")
    assert data == {
        "codec": "lzma2", "level": "5",
        "range": {"min": 1, "max": 9, "default": 5}, "sent": "lzma2:5",
    }


def test_a_typed_level_is_held_to_the_codecs_range(tmp_path):
    """The number box takes typing, so 19 on lzma2 is pulled to 9 rather than
    held where the slider can't show it. A blank box mid-edit stays blank."""
    data = _run(tmp_path, _DOLPHIN_LEVELS + """
store.setSingleCodec('lzma2');
store.setCompressionLevel('19');
const typed = store.compressionLevel;
store.setCompressionLevel('');
const blank = store.compressionLevel;
store.setSingleCodec('zstd');
store.setCompressionLevel('19');
process.stdout.write(JSON.stringify({ typed, blank, zstd: store.compressionLevel }));
""")
    assert data == {"typed": "9", "blank": "", "zstd": "19"}


def test_the_rule_editor_holds_its_level_to_the_codecs_range(tmp_path):
    """RuleEditor's level box goes through the same clamp, and its codec
    change through the same fit, as the convert panel."""
    editor = (_SRC / "components" / "views" / "RuleEditor.svelte").read_text(encoding="utf-8")
    assert "onchange={(e) => setRuleLevel(platform.id, rule, e.currentTarget.value)}" in editor
    assert "clampLevel(range, value)" in editor and "fitLevel(range, level)" in editor
    data = _run(tmp_path, """
const lzma2 = { min: 1, max: 9, default: 5 };
process.stdout.write(JSON.stringify({
  typed: clampLevel(lzma2, '19'), low: clampLevel(lzma2, '0'), blank: clampLevel(lzma2, ''),
  refit: fitLevel(lzma2, 19), kept: fitLevel(lzma2, 7),
}));
""")
    assert data == {"typed": 9, "low": 1, "blank": None, "refit": 5, "kept": 7}


def test_the_picker_reads_one_level_and_range():
    """The slider and the number box read the same value and range, which the
    controlled picker takes from its `value` (the panel passes the store's)."""
    picker = _PICKER.read_text(encoding="utf-8")
    assert "const levelRange = $derived(rangeOf(mode, value));" in picker
    assert picker.count("value={level") == 2
    panel = (_SRC / "components" / "panels" / "ConvertPanel.svelte").read_text(encoding="utf-8")
    assert "value={conversion.compressionChoice}" in panel


_BROWSER = """
globalThis.window = {};
const puts = [];
api.putConversionPrefs = async (body) => { puts.push(body); return body; };
"""


def _run_in_browser(tmp_path: Path, scenario: str) -> dict:
    """As `_run`, but with `window` defined so the store saves to the server."""
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the conversion store")
    script = tmp_path / "conversion_store_browser.mjs"
    levels = re.sub(r"^export ", "", _LEVELS_JS.read_text(encoding="utf-8"), flags=re.MULTILINE)
    script.write_text(_STUBS + _BROWSER + levels + _plain_js() + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the conversion store via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_a_default_saved_in_settings_applies_even_after_an_edit(tmp_path):
    """Codex 4652247066: once the user picked codecs in the Convert panel, a
    compression saved in Settings → Defaults still has to become the live one."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
store.setCompression(['zlib']);           // the user's own pick: touched
store.applySavedDefaults({ chdman: 'lzma' });
process.stdout.write(JSON.stringify({ live: store.compressionSelection }));
""")
    assert data == {"live": ["lzma"]}


def test_a_debounced_preference_survives_leaving_the_page(tmp_path):
    """Codex 4652247060: the debounce lives in the store, not in the Settings
    section, so leaving Settings can't drop it; and a flush sends it at once."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
store.applySavedDefaults({ chdman: 'lzma', dolphin: 'zstd:19' });
const before = puts.length;
await store.flushCompressionPrefs();
const flushed = puts.slice();
store.applySavedDefaults({ chdman: 'zlib' });
await new Promise((r) => setTimeout(r, 700));   // past the debounce, no component left
process.stdout.write(JSON.stringify({ before, flushed, later: puts[puts.length - 1] }));
""")
    assert data == {
        "before": 0,
        "flushed": [{"chdman": "lzma", "dolphin": "zstd:19"}],
        "later": {"chdman": "zlib"},
    }
