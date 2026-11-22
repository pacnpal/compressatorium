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
let parseCompression = (_mode, wire) => ({ codecs: wire ? wire.split(',') : [], level: null });
let formatCompression = (_mode, value) => (value?.codecs ?? []).join(',') || null;
let specsForTarget = () => [];
const hashFor = (view, { tab } = {}) => `#/${view}/${tab ?? ''}`;
const deleteCheckFor = () => 'round_tripped';
const registry = {
  forTool: (id) => ({
    id, compressionStyle: 'multi', defaultCompression: ['zlib'],
    // makeps3iso's folder_to_iso is the registry's folder-input mode.
    modes: id === 'makeps3iso'
      ? [{ mode: 'folder_to_iso', inputKinds: ['directory'] }]
      : [{ mode: 'createcd' }],
  }),
  defaultMode: () => 'createcd',
  all: () => [],
  specFor: () => ({ supportsCompression: true }),
  toolForMode: () => ({ id: 'chdman' }),
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




# --- The abort paths of one submit ------------------------------------------

_ABORT_DURING_THE_SNAPSHOT = """
const store = new ConversionStore();
let releaseSnapshot;
planAnswer = (paths) => new Promise((resolve) => {
  releaseSnapshot = () => resolve({
    recorded: 1, pending: 1,
    recorded_paths: { [paths[0]]: '/out/a.chd' }, recorded_ids: { [paths[0]]: 42 },
  });
});
const controller = new AbortController();
const run = store.submitAuto(['/v/a.iso'], {
  plan: [{ file_path: '/v/a.iso', mode: 'createcd' }],
  rommRepin: true, rommPlatformId: 3, signal: controller.signal,
}).catch(() => null);
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
  converting: store.converting,
}));
"""


def test_an_abort_during_the_snapshot_never_queues_and_retires_the_metadata(tmp_path):
    data = _run(tmp_path, _ABORT_DURING_THE_SNAPSHOT)

    assert data == {
        "result": None, "queued": 0, "retired": [[42]], "errors": [], "converting": False,
    }, data


# the snapshot can stay pending (a stalled RomM). The
# wait for it is abort-aware: closing the panel frees Convert at once, and the
# rows the stalled snapshot records when it finally returns are retired on
# their own, without touching a newer run's state.
_ABORT_WHILE_THE_SNAPSHOT_STALLS = """
const store = new ConversionStore();
let releaseOld;
planAnswer = (paths) => new Promise((resolve) => {
  releaseOld = () => resolve({
    recorded: 1, pending: 1,
    recorded_paths: { [paths[0]]: '/out/a.chd' }, recorded_ids: { [paths[0]]: 42 },
  });
});
const controller = new AbortController();
const run = store.submitAuto(['/v/a.iso'], {
  plan: [{ file_path: '/v/a.iso', mode: 'createcd' }],
  rommRepin: true, rommPlatformId: 3, signal: controller.signal,
}).catch(() => null);
for (let i = 0; i < 5 && !releaseOld; i += 1) {
  await new Promise((resolve) => setTimeout(resolve, 0));  // the snapshot is pending
}
controller.abort();                                        // the panel closes
const result = await run;                                  // no release: it stalls
const freed = { converting: store.converting };

planAnswer = (paths) => ({
  recorded: 1, pending: 7,
  recorded_paths: { [paths[0]]: '/out/b.chd' }, recorded_ids: { [paths[0]]: 77 },
});
autoAnswer = (items) => ({
  plan: [], jobs: [{ id: 'j2', file_path: items[0].file_path, output_path: '/out/b.chd' }],
});
const second = await store.submitAuto(['/v/b.iso'], {
  plan: [{ file_path: '/v/b.iso', mode: 'createcd' }], rommRepin: true, rommPlatformId: 3,
});
const newRun = { pending: store.lastRepinPending, recorded: store.lastRepinRecorded };

releaseOld();                                              // the stalled snapshot returns
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
process.stdout.write(JSON.stringify({
  result, freed, secondQueued: second !== null, newRun,
  afterLate: { pending: store.lastRepinPending, recorded: store.lastRepinRecorded,
               converting: store.converting },
  queued: calls.auto.map((args) => args[0].map((item) => item.file_path)),
  retired: calls.cancel,
}));
"""


def test_an_abort_while_the_snapshot_stalls_frees_convert_and_the_late_rows_retire_alone(
    tmp_path,
):
    data = _run(tmp_path, _ABORT_WHILE_THE_SNAPSHOT_STALLS)

    assert data["result"] is None, data
    assert data["freed"] == {"converting": False}, data
    assert data["secondQueued"] is True, data
    assert data["queued"] == [["/v/b.iso"]], data              # only the new run queued
    assert data["afterLate"] == {
        "pending": data["newRun"]["pending"], "recorded": data["newRun"]["recorded"],
        "converting": False,
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


def test_the_rule_editor_holds_its_level_to_the_codecs_range(tmp_path):
    """A console's level goes through the same clamp, and its codec change
    through the same fit, as the convert panel: both use compression.js."""
    advanced = (_SRC / "components" / "library" / "RuleAdvanced.svelte").read_text(
        encoding="utf-8",
    )
    assert "onchange={(v) => onupdate(toRule(rule.mode, v))}" in advanced
    shared = (_SRC / "tools" / "compression.js").read_text(encoding="utf-8")
    assert "clampLevel(range, level)" in shared and "fitLevel(range, value?.level)" in shared
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
    controlled picker takes from its `value` (the panel passes the store's
    remembered compression for the mode)."""
    picker = _PICKER.read_text(encoding="utf-8")
    assert "const levelRange = $derived(rangeOf(mode, value));" in picker
    assert picker.count("value={level") == 2
    panel = (_SRC / "components" / "panels" / "ConvertPanel.svelte").read_text(encoding="utf-8")
    assert "value={conversion.compressionFor(" in panel
    assert "conversion.setCompressionFor(" in panel


# --- Auto is the way to convert (restructure 2/6) ---------------------------

def test_any_file_may_be_offered_and_a_folder_only_to_a_folder_mode(tmp_path):
    data = _run(tmp_path, """
const store = new ConversionStore();
const gate = (entry) => store.allowsInputEntry(entry);
const best = {
  file: gate({ type: 'file', path: '/v/a.iso' }),
  ps3Folder: gate({ type: 'directory', path: '/v/Game', convertible_by: ['makeps3iso'] }),
  plainFolder: gate({ type: 'directory', path: '/v/stuff', convertible_by: [] }),
  fileToolFolder: gate({ type: 'directory', path: '/v/x', convertible_by: ['chdman'] }),
  archive: gate({ type: 'archive', path: '/v/a.7z' }),
};
// An archive some mode reads whole (an eXoDOS game zip): Best offers it, so
// it can be ticked and its own Convert to picked.
registry.all = () => [{ id: 'exodos', modes: [{ mode: 'exodos_repack', inputExtensions: ['.zip'] }] }];
best.readArchive = gate({ type: 'archive', path: '/v/Sample Quest (1993).zip' });
best.unreadArchive = gate({ type: 'archive', path: '/v/a.7z' });
specsForTarget = (v) => (v === 'romz_extract' ? [{ inputExtensions: ['.zip'] }] : []);
store.chooseTarget('romz_extract');
process.stdout.write(JSON.stringify({ best, unpack: gate({ type: 'archive', path: '/v/a.zip' }) }));
""")

    assert data["best"] == {
        "file": True, "ps3Folder": True, "plainFolder": False,
        "fileToolFolder": False, "archive": False,
        "readArchive": True, "unreadArchive": False,
    }, data
    # A target that takes the archive itself offers it.
    assert data["unpack"] is True, data


def test_the_plan_is_debounced_and_a_newer_request_supersedes_the_older(tmp_path):
    data = _run(tmp_path, """
const store = new ConversionStore();
const signals = [];
api.getAutoPlan = async (paths, outputDir, opts) => {
  calls.autoPlan.push({ paths: [...paths], target: opts.target, overrides: opts.platformOverrides });
  signals.push(opts.signal);
  return paths.map((p) => ({ file_path: p, mode: 'createcd' }));
};
const first = store.requestPlan(['/v/a.iso'], { delay: 0 });
store.chooseTarget('.chd');
store.setOverride(['/v/b.iso'], 'ps2');
const second = store.requestPlan(['/v/b.iso'], { delay: 0 });
const results = [await first, await second];
process.stdout.write(JSON.stringify({
  first: results[0], second: results[1].map((r) => r.file_path),
  // Superseded before it was sent, or aborted in flight.
  aborted: signals.length === 1 || signals[0].aborted,
  sent: calls.autoPlan,
  current: store.hasPlanFor(['/v/b.iso']),
  stale: store.hasPlanFor(['/v/a.iso']),
  planning: store.planning,
}));
""")

    assert data["first"] is None, data              # superseded: never shown
    assert data["aborted"] is True, data            # and its request aborted
    assert data["second"] == ["/v/b.iso"], data
    assert data["sent"][-1] == {
        "paths": ["/v/b.iso"], "target": ".chd", "overrides": {"/v/b.iso": "ps2"},
    }, data
    assert (data["current"], data["stale"], data["planning"]) == (True, False, False), data


def test_a_failed_plan_says_why_and_shows_no_plan(tmp_path):
    data = _run(tmp_path, """
const store = new ConversionStore();
api.getAutoPlan = async () => { throw new Error('volume not answering'); };
const plan = await store.requestPlan(['/v/a.iso'], { delay: 0 });
process.stdout.write(JSON.stringify({ plan, error: store.planError, shown: store.plan }));
""")

    assert data == {"plan": None, "error": "volume not answering", "shown": None}, data


def test_the_submit_sends_the_safety_choices_and_the_plan_inputs(tmp_path):
    data = _run(tmp_path, """
const store = new ConversionStore();
store.chooseTarget('.chd');
store.setOverride(['/v/x.iso'], 'ps2');
const plan = [
  { file_path: '/v/x.iso', mode: 'createdvd' },
  { file_path: '/v/y.iso', mode: null, skip_reason: 'Already CHD' },
];
await store.submitAuto(['/v/x.iso', '/v/y.iso'], {
  plan, outputDir: '/out',
  options: { verifyAfter: true, deleteOnVerify: true, containerOnly: false, split: false },
});
const [items, outputDir, dup, options] = calls.auto[0];
process.stdout.write(JSON.stringify({ items, outputDir, dup, options }));
""")

    assert data["items"] == [{"file_path": "/v/x.iso", "mode": "createdvd"}], data
    assert (data["outputDir"], data["dup"]) == ("/out", "skip"), data
    assert data["options"] == {
        "target": ".chd", "platformOverrides": {"/v/x.iso": "ps2"},
        "verifyAfter": True, "deleteOnVerify": True, "containerOnly": False, "split": False,
    }, data


def test_the_delete_preview_is_one_plan_over_every_deleting_group(tmp_path):
    data = _run(tmp_path, """
const store = new ConversionStore();
api.getDeletePlan = async (paths, mode) => ({
  items: paths.map((p) => ({ source: p, mode })),
  total_delete_count: paths.length * 2,
  blocked: mode === 'dolphin_rvz',
  disallowed_archives: [],
});
const plan = await store.fetchDeletePlan({ createcd: ['/v/a.cue'], dolphin_rvz: ['/v/b.iso'] });
process.stdout.write(JSON.stringify(plan));
""")

    assert data["items"] == [
        {"source": "/v/a.cue", "mode": "createcd"},
        {"source": "/v/b.iso", "mode": "dolphin_rvz"},
    ], data
    assert (data["total_delete_count"], data["blocked"]) == (4, True), data


def test_a_remembered_compression_follows_its_tool_and_replans(tmp_path):
    data = _run(tmp_path, """
const store = new ConversionStore();
registry.toolForMode = (mode) => ({ id: mode.startsWith('dolphin') ? 'dolphin' : 'chdman' });
parseCompression = (mode, wire) => ({ mode, wire: wire ?? null });
formatCompression = (_mode, value) => value.codecs.join(',');
const before = store.prefsVersion;
store.setCompressionFor('createcd', { codecs: ['zlib', 'lzma'] });
process.stdout.write(JSON.stringify({
  prefs: store.prefs,
  cd: store.compressionFor('createcd'),
  rvz: store.compressionFor('dolphin_rvz'),
  bumped: store.prefsVersion > before,
}));
""")

    assert data["prefs"] == {"chdman": "zlib,lzma"}, data
    assert data["cd"] == {"mode": "createcd", "wire": "zlib,lzma"}, data
    assert data["rvz"] == {"mode": "dolphin_rvz", "wire": None}, data
    assert data["bumped"] is True, data


_BROWSER = """
globalThis.window = {};
// The server's copy: reads see it, writes replace it (the PUT is whole-object).
let server = {};
const puts = [];
api.getConversionPrefs = async () => ({ ...server });
api.putConversionPrefs = async (body) => { puts.push(body); server = { ...body }; return body; };
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
    """once the user picked codecs in the Convert panel, a
    compression saved in Settings → Defaults still has to become the live one."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
store.setCompressionFor('createcd', { codecs: ['zlib'] });   // the user's own pick
store.applySavedDefault('chdman', 'lzma');
process.stdout.write(JSON.stringify({ live: store.compressionFor('createcd').codecs }));
""")
    assert data == {"live": ["lzma"]}


def test_a_debounced_preference_survives_leaving_the_page(tmp_path):
    """the debounce lives in the store, not in the Settings
    section, so leaving Settings can't drop it; and a flush sends it at once."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
store.applySavedDefault('chdman', 'lzma');
store.applySavedDefault('dolphin', 'zstd:19');
const before = puts.length;
await store.flushCompressionPrefs();
const flushed = puts.slice();
store.applySavedDefault('chdman', 'zlib');
await new Promise((r) => setTimeout(r, 700));   // past the debounce, no component left
process.stdout.write(JSON.stringify({ before, flushed, later: puts[puts.length - 1] }));
""")
    assert data == {
        "before": 0,
        "flushed": [{"chdman": "lzma", "dolphin": "zstd:19"}],
        "later": {"chdman": "zlib", "dolphin": "zstd:19"},
    }


def test_a_save_sends_only_the_tools_changed_here(tmp_path):
    """the PUT replaces the whole object, so a save built
    from an old copy wiped a change another tab (or the CLI) saved meanwhile.
    A queued save reads the server's current copy when it runs and changes
    only the tools edited here; going back to a tool's default drops its key."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
server = { chdman: 'zlib', nsz: 'solid:18' };
await store.loadServerPrefs();
// Elsewhere, after this tab read the prefs: dolphin and nsz change.
server = { chdman: 'zlib', nsz: 'solid:9', dolphin: 'zstd:19' };
store.applySavedDefault('chdman', 'lzma');
await store.flushCompressionPrefs();
const first = puts[puts.length - 1];
store.applySavedDefault('nsz', null);      // back to the tool's own default
await store.flushCompressionPrefs();
process.stdout.write(JSON.stringify({ first, second: puts[puts.length - 1], count: puts.length }));
""")
    assert data == {
        "first": {"chdman": "lzma", "nsz": "solid:9", "dolphin": "zstd:19"},
        "second": {"chdman": "lzma", "dolphin": "zstd:19"},
        "count": 2,
    }


def test_a_compression_save_that_fails_says_so(tmp_path):
    """A preference that doesn't save is reported, never swallowed: the store
    keeps the error for Settings to show (with Try again) and toasts it, which
    covers a save made from the Convert panel too. The next good save clears it."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
const put = api.putConversionPrefs;
api.putConversionPrefs = async () => { throw new Error('disk full'); };
store.applySavedDefault('chdman', 'lzma');
await store.flushCompressionPrefs();
const failed = store.compressionSaveError;
const toasted = toasts.filter(([kind]) => kind === 'error').map(([, m]) => m);
api.putConversionPrefs = put;
await store.retryCompressionSave();
process.stdout.write(JSON.stringify({ failed, toasted, after: store.compressionSaveError, puts }));
""")
    assert data == {
        "failed": "disk full",
        "toasted": ["Couldn't save the compression: disk full"],
        "after": None,
        "puts": [{"chdman": "lzma"}],
    }


def test_a_late_boot_read_never_overwrites_a_newer_local_change(tmp_path):
    """the boot read of the remembered compression answers
    after Settings saved lzma. Its old answer must not replace the cache, the
    live choice, or what the pending save sends; nor after a Convert-panel edit."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
const read = api.getConversionPrefs;
let answer;
api.getConversionPrefs = () => new Promise((r) => { answer = r; });
const boot = store.loadServerPrefs();
api.getConversionPrefs = read;
store.applySavedDefault('chdman', 'lzma');
answer({ chdman: 'zlib' });
await boot;
const live = store.compressionFor('createcd').codecs;
await store.flushCompressionPrefs();
const saved = puts[puts.length - 1];

// The same with an edit made in the Convert panel while the read is out.
api.getConversionPrefs = () => new Promise((r) => { answer = r; });
const boot2 = store.loadServerPrefs();
api.getConversionPrefs = read;
store.setCompressionFor('createcd', { codecs: ['huff'] });
answer({ chdman: 'zlib' });
await boot2;
await store.flushCompressionPrefs();
process.stdout.write(JSON.stringify({
  live, saved, live2: store.compressionFor('createcd').codecs, saved2: puts[puts.length - 1],
}));
""")
    assert data == {
        "live": ["lzma"], "saved": {"chdman": "lzma"},
        "live2": ["huff"], "saved2": {"chdman": "huff"},
    }


def test_an_older_delete_plan_never_replaces_a_newer_one(tmp_path):
    """The same rule for the delete plan: two asks answering out of order keep
    the newer plan, and the older one's failure doesn't clear it."""
    data = _run(tmp_path, """
const store = conversion;
const answers = [];
api.getDeletePlan = () => new Promise((resolve, reject) => answers.push({ resolve, reject }));
const older = store.fetchDeletePlan({ createcd: ['/a.iso'] }).catch(() => 'older failed');
const newer = store.fetchDeletePlan({ createcd: ['/b.iso'] });
answers[1].resolve({ items: ['newer'] });
await newer;
answers[0].reject(new Error('gone'));
const olderResult = await older;
process.stdout.write(JSON.stringify({ plan: store.deletePlan.items, olderResult }));
""")
    assert data == {"plan": ["newer"], "olderResult": None}


def test_a_compression_change_is_saved_before_the_plan_and_the_queue_read_it(tmp_path):
    """the Auto routes read the saved compression, so a
    plan or a queue sent inside the save's debounce used the old one."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
const order = [];
api.putConversionPrefs = async (body) => { order.push(['put', body.chdman]); server = { ...body }; return body; };
api.getAutoPlan = async () => { order.push(['plan', server.chdman]); return [{ file_path: '/v/a.iso', mode: 'createcd' }]; };
api.createAutoJobs = async () => { order.push(['queue', server.chdman]); return { jobs: [], plan: [] }; };
store.setCompressionFor('createcd', { codecs: ['lzma'] });
const plan = await store.requestPlan(['/v/a.iso'], { delay: 0 });
store.setCompressionFor('createcd', { codecs: ['zlib'] });
await store.submitAuto(['/v/a.iso'], { plan });
process.stdout.write(JSON.stringify(order));
""")
    assert data == [["put", "lzma"], ["plan", "lzma"], ["put", "zlib"], ["queue", "zlib"]], data


def test_a_compression_that_did_not_save_queues_nothing(tmp_path):
    """the Auto route queues with the saved compression,
    so when the change can't be saved the run stops and says so; the choice
    is kept for Try again."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
api.putConversionPrefs = async () => { throw new Error('disk full'); };
let queued = 0;
api.createAutoJobs = async () => { queued += 1; return { jobs: [] }; };
store.setCompressionFor('createcd', { codecs: ['lzma'] });
let failed = false;
try {
  await store.submitAuto(['/v/a.iso'], { plan: [{ file_path: '/v/a.iso', mode: 'createcd' }] });
} catch (_e) {
  failed = true;
}
process.stdout.write(JSON.stringify({
  failed, queued, kept: store.prefs.chdman,
  errors: toasts.filter(([kind]) => kind === 'error').map(([, m]) => m),
}));
""")
    assert data["failed"] is True and data["queued"] == 0, data
    assert data["kept"] == "lzma", data
    assert any("Couldn't save the compression" in m for m in data["errors"]), data


def test_a_run_aborted_while_the_compression_saves_queues_nothing(tmp_path):
    """the panel closes while the debounced save is sent;
    the run stops there, and the queue request carries the run's signal."""
    data = _run_in_browser(tmp_path, """
const store = conversion;
let landed;
api.putConversionPrefs = (body) => new Promise((r) => { landed = () => r(body); });
let queued = 0;
const signals = [];
api.createAutoJobs = async (_i, _o, _d, opts) => { queued += 1; signals.push(opts.signal); return { jobs: [] }; };
store.setCompressionFor('createcd', { codecs: ['lzma'] });
const controller = new AbortController();
const run = store.submitAuto(['/v/a.iso'], {
  plan: [{ file_path: '/v/a.iso', mode: 'createcd' }], signal: controller.signal,
}).catch((e) => ({ aborted: Boolean(e?.aborted) }));
for (let i = 0; i < 20 && !landed; i += 1) await new Promise((r) => setTimeout(r, 0));
controller.abort();
landed();
const result = await run;

// A run that isn't aborted hands its signal to the queue request.
api.putConversionPrefs = async (body) => body;
const second = new AbortController();
await store.submitAuto(['/v/b.iso'], {
  plan: [{ file_path: '/v/b.iso', mode: 'createcd' }], signal: second.signal,
});
process.stdout.write(JSON.stringify({
  result, queued, passed: signals[0] === second.signal,
}));
""")
    assert data == {"result": {"aborted": True}, "queued": 1, "passed": True}, data


def test_the_target_names_its_format_for_a_format_or_a_mode(tmp_path):
    """RomM's summary counts only outputs of the format the
    panel would make, whether Convert to is a format or a mode."""
    data = _run(tmp_path, """
const store = new ConversionStore();
registry.specFor = (mode) => (mode === 'dolphin_rvz' ? { outputExt: '.rvz' } : null);
const best = store.targetExt;
store.chooseTarget('.chd');
const format = store.targetExt;
store.chooseTarget('dolphin_rvz');
process.stdout.write(JSON.stringify({ best, format, mode: store.targetExt }));
""")
    assert data == {"best": None, "format": ".chd", "mode": ".rvz"}, data


def test_an_abort_while_the_queue_request_is_out_keeps_the_romm_rows(tmp_path):
    """once the queue request has gone out, the server
    may have made the jobs, so their RomM rows are kept (they age out on
    their own if not); an abort before it went out still retires them."""
    data = _run(tmp_path, """
planAnswer = (paths) => ({
  recorded: 1, pending: 1,
  recorded_paths: { [paths[0]]: '/out/a.chd' }, recorded_ids: { [paths[0]]: 42 },
});
const controller = new AbortController();
api.createAutoJobs = (_i, _o, _d, opts) => new Promise((_resolve, reject) => {
  opts.signal.addEventListener('abort', () => reject(new Error('aborted')));
  controller.abort();
});
const store = new ConversionStore();
const result = await store.submitAuto(['/v/a.iso'], {
  plan: [{ file_path: '/v/a.iso', mode: 'createcd' }],
  rommRepin: true, rommPlatformId: 3, signal: controller.signal,
}).catch((e) => ({ aborted: Boolean(e?.aborted) }));
process.stdout.write(JSON.stringify({
  result, retired: calls.cancel, errors: toasts.filter(([k]) => k === 'error'),
}));
""")
    assert data == {"result": {"aborted": True}, "retired": [], "errors": []}, data
