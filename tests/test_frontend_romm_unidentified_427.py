"""The DAT match store behind "Unidentified in RomM" (#427, Codex on PR #442).

``src/lib/stores/datMatching.svelte.js``, evaluated with Node as in
``test_frontend_dat_ranking_424``:

* A RomM listing ships each row's cached match. Those are seeded into the
  store, which a matching-policy change clears, rather than kept as a
  permanent fallback: a seed taken under an older policy is dropped, so a miss
  recorded before Hasheous was switched on doesn't come back.
* "Recognize these" submits rows from every page, but the file list only
  re-reads the visible ones when the job ends. The store remembers each job it
  started and re-reads every path of it once that job is over.

The views' wiring is asserted against the source. Skipped without Node.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from . import test_frontend_dat_ranking_424 as harness

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"

_STUBS = """
class SvelteMap extends Map {}
const untrack = (fn) => fn();
const lookups = [];
const api = {
  getDATStats: async () => ({ total_dats: 1 }),
  startMatchJob: async (paths) => ({ status: 'started', job_id: 'j1', results: {} }),
  getMatchCache: async (paths) => {
    lookups.push([...paths]);
    return { results: Object.fromEntries(paths.map((p) => [p, { matched: true, game_name: p }])) };
  },
};
const tick = () => new Promise((r) => setTimeout(r, 0));
"""

_SCENARIO = """
const out = {};
const rows = [
  { path: '/a.iso', dat_match: { matched: false } },
  { path: '/b.iso', dat_match: null },
];

// 1. A seed taken under the current policy fills the store.
datMatching.seedListing(rows, datMatching.beginListingRead());
out.seeded = datMatching.matchFor('/a.iso');
out.unseeded = datMatching.matchFor('/b.iso');

// 2. A policy change (Hasheous switched on in another tab) clears the store;
// a seed from a listing loaded before it is dropped.
const before = datMatching.beginListingRead();
datMatching._generation += 1;
datMatching.matches.clear();
datMatching.seedListing(rows, before);
out.afterPolicyChange = datMatching.matchFor('/a.iso');

// 3. The job "Recognize these" starts re-reads all its paths when it ends.
await datMatching.startMatchJob(['/p1.iso', '/p2.iso', '/p3.iso']);
await datMatching.hydrateFinishedJobs([{ id: 'j1', mode: 'dat_match', status: 'running' }]);
out.whileRunning = lookups.length;
await datMatching.hydrateFinishedJobs([{ id: 'j1', mode: 'dat_match', status: 'completed' }]);
out.lookups = lookups;
out.p3 = datMatching.matchFor('/p3.iso');
await datMatching.hydrateFinishedJobs([{ id: 'j1', mode: 'dat_match', status: 'completed' }]);
out.lookupsAfterRepeat = lookups.length;
console.log(JSON.stringify(out));
"""


def _run(scenario: str = _SCENARIO) -> dict:
    node = harness._find_node()
    if node is None:
        pytest.skip("Node is not installed")
    stores = harness._STORES
    script = "\n".join([
        _STUBS,
        harness._plain(stores / "latestSaveQueue.js"),
        harness._plain(stores / "datMatching.svelte.js"),
        "(async () => {",
        scenario,
        "})().catch((e) => { console.error(e); process.exit(1); });",
    ])
    done = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_listing_seed_fills_the_store_under_the_current_policy():
    result = _run()
    assert result["seeded"] == {"matched": False}
    assert result["unseeded"] is None


def test_a_seed_from_before_a_policy_change_is_dropped():
    assert _run()["afterPolicyChange"] is None


def test_every_path_of_a_finished_match_job_is_read_again_once():
    result = _run()
    assert result["whileRunning"] == 0
    assert result["lookups"] == [["/p1.iso", "/p2.iso", "/p3.iso"]]
    assert result["p3"] == {"matched": True, "game_name": "/p3.iso"}
    assert result["lookupsAfterRepeat"] == 1


_RACE = """
// Lookups answer when the test says, in any order.
const waiting = [];
api.getMatchCache = (paths) => new Promise((resolve) => {
  waiting.push({ paths: [...paths], resolve });
});
const answer = (i, results) => waiting[i].resolve({ results });
const out = {};

// The job "Recognize these" started covered an off-page ROM and a visible one.
await datMatching.startMatchJob(['/off.iso', '/on.iso']);
// It ends: the store reads the whole job back, then the file list reads the
// visible page.
const full = datMatching.hydrateFinishedJobs([{ id: 'j1', status: 'completed' }]);
const visible = datMatching.hydrate(['/on.iso']);
for (let i = 0; i < 5; i += 1) await tick();
// The visible page answers first, then the whole job.
answer(1, { '/on.iso': { matched: true, game_name: 'newer' } });
await visible;
answer(0, {
  '/off.iso': { matched: true, game_name: 'Off Page' },
  '/on.iso': { matched: true, game_name: 'older' },
});
await full;
out.off = datMatching.matchFor('/off.iso');
out.on = datMatching.matchFor('/on.iso');
console.log(JSON.stringify(out));
"""


def test_a_full_job_read_is_not_lost_to_a_page_read_that_answered_first():
    """Codex on PR #442: the visible page's read answering first threw away
    the whole job's read, off-page ROMs included, and the job was already
    forgotten. Answers are ordered per path: the page's newer answer for its
    own row stands, and the off-page row still lands."""
    result = _run(_RACE)
    assert result["off"] == {"matched": True, "game_name": "Off Page"}
    assert result["on"] == {"matched": True, "game_name": "newer"}


_LISTING_ORDER = """
const out = {};
// This tab cached a miss for an off-page ROM; another client then recognized
// it, and a refreshed listing carries the hit.
await datMatching.hydrate(['/off.iso']);
datMatching.matches.set('/off.iso', { matched: false });
datMatching.seedListing(
  [{ path: '/off.iso', dat_match: { matched: true, game_name: 'Hit' } }],
  datMatching.beginListingRead(),
);
out.refreshedHit = datMatching.matchFor('/off.iso');

// A newer listing with no match for a path clears the stale hit.
datMatching.seedListing([{ path: '/off.iso', dat_match: null }], datMatching.beginListingRead());
out.cleared = datMatching.matchFor('/off.iso');

// A listing read issued before a lookup that answered first doesn't undo it.
const older = datMatching.beginListingRead();
await datMatching.hydrate(['/x.iso']);
datMatching.seedListing([{ path: '/x.iso', dat_match: { matched: false } }], older);
out.olderListing = datMatching.matchFor('/x.iso');
console.log(JSON.stringify(out));
"""


def test_a_listing_orders_against_the_store_per_path_like_a_lookup():
    """Codex on PR #442: seeding skipped any path the store already had, so a
    refreshed listing's newer hit never landed and its null couldn't clear a
    removed verdict. A listing read takes its place in the same per-path
    order as a lookup: newer replaces (null clears), older doesn't."""
    result = _run(_LISTING_ORDER)
    assert result["refreshedHit"] == {"matched": True, "game_name": "Hit"}
    assert result["cleared"] is None
    assert result["olderListing"] == {"matched": True, "game_name": "/x.iso"}


def test_the_views_read_the_store_and_not_the_listing_snapshot():
    row = (_SRC / "components" / "panels" / "FileRow.svelte").read_text(encoding="utf-8")
    view = (_SRC / "components" / "library" / "RommBrowse.svelte").read_text(encoding="utf-8")
    browser = (_SRC / "stores" / "fileBrowser.svelte.js").read_text(encoding="utf-8")
    assert "dat_match ??" not in row and "dat_match ??" not in view
    assert "datMatching.seedListing(" in browser
    assert "datMatching.hydrateFinishedJobs(" in view


_UTIL = _SRC / "util" / "datMatchable.js"


def test_the_shared_matchable_rule_leaves_out_archives_and_folders():
    """Codex on PR #442: the match job hashes a zip's or 7z's compressed bytes,
    so an archived ROM in a DAT was recorded as a miss and never offered
    again. One rule, shared by the file list and "Recognize these", says
    which rows the matcher can identify."""
    node = harness._find_node()
    if node is None:
        pytest.skip("Node is not installed")
    src = _UTIL.read_text(encoding="utf-8") if _UTIL.exists() else ""
    script = src.replace("export ", "") + """
const exts = ['.iso', '.chd', '.zip', '.7z'];
const row = (name, extra = {}) => ({ path: `/r/${name}`, type: 'file',
  extension: name.slice(name.lastIndexOf('.')), ...extra });
console.log(JSON.stringify({
  iso: isDatMatchable(row('a.iso'), exts),
  zip: isDatMatchable(row('a.zip'), exts),
  sevenZip: isDatMatchable(row('a.7z'), exts),
  archiveRow: isDatMatchable(row('a.iso', { type: 'archive' }), exts),
  folder: isDatMatchable(row('Game.iso', { type: 'directory' }), exts),
  split: isDatMatchable(row('a.iso', { split_parts: 2 }), exts),
  unknown: isDatMatchable(row('a.txt'), exts),
}));
"""
    done = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "iso": True, "zip": False, "sevenZip": False, "archiveRow": False,
        "folder": False, "split": False, "unknown": False,
    }


def test_the_file_list_and_the_romm_view_share_that_rule():
    file_list = (_SRC / "components" / "panels" / "FileList.svelte").read_text(encoding="utf-8")
    view = (_SRC / "components" / "library" / "RommBrowse.svelte").read_text(encoding="utf-8")
    for source in (file_list, view):
        assert "isDatMatchable(" in source
    # No second copy of the check in the file list.
    assert "matchExtSet" not in file_list
    # The view says archived ROMs aren't recognized yet.
    assert "aren't recognized yet" in view


def _util(scenario: str) -> dict:
    node = harness._find_node()
    if node is None:
        pytest.skip("Node is not installed")
    script = _UTIL.read_text(encoding="utf-8").replace("export ", "") + scenario
    done = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_ids_are_wanted_only_while_re_pinning_is_on_too():
    """CodeRabbit on PR #442: the backend writes Hasheous ids only while
    re-pinning is on as well, so the view must not offer a refresh otherwise."""
    result = _util("""
const legacy = { matched: true, source: 'hasheous' };
console.log(JSON.stringify({
  both: wantsHasheousIds({ hasheous_write_ids: true, repin_enabled: true }),
  repinOff: wantsHasheousIds({ hasheous_write_ids: true, repin_enabled: false }),
  switchOff: wantsHasheousIds({ hasheous_write_ids: false, repin_enabled: true }),
  none: wantsHasheousIds(null),
  unknown: needsDatMatch(null, false),
  legacyWanted: needsDatMatch(legacy, true),
  legacyNotWanted: needsDatMatch(legacy, false),
  withIds: needsDatMatch({ ...legacy, provider_ids: {} }, true),
  miss: needsDatMatch({ matched: false }, true),
}));
""")
    assert result == {
        "both": True, "repinOff": False, "switchOff": False, "none": False,
        "unknown": True, "legacyWanted": True, "legacyNotWanted": False,
        "withIds": False, "miss": False,
    }


_CODERABBIT = """
const out = {};
const waiting = [];
api.getMatchCache = (paths) => new Promise((resolve, reject) => {
  waiting.push({ paths: [...paths], resolve, reject });
});
api.startMatchJob = (paths) => new Promise((resolve) => {
  waiting.push({ paths: [...paths], resolve, job: true });
});
const settleNext = async (value) => {
  for (let i = 0; i < 50 && !waiting.length; i += 1) await tick();
  waiting.shift().resolve(value);
  for (let i = 0; i < 5; i += 1) await tick();
};

// 1. A lookup issued after a listing read, answering after the listing landed
// with no row for a path, clears what the older listing put there.
const listing = datMatching.beginListingRead();
const lookup = datMatching.hydrate(['/gone.iso']);
datMatching.seedListing([{ path: '/gone.iso', dat_match: { matched: true } }], listing);
await settleNext({ results: {} });
await lookup;
out.reconciled = datMatching.matchFor('/gone.iso');

// 2. A match job's idle answer is ordered by when it was asked: a lookup
// asked after it and answered first keeps its newer answer.
const job = datMatching.startMatchJob(['/x.iso']);
for (let i = 0; i < 5; i += 1) await tick();
const later = datMatching.hydrate(['/x.iso']);
waiting.splice(1, 1)[0].resolve({ results: { '/x.iso': { matched: true, game_name: 'new' } } });
await later;
await settleNext({ status: 'idle', results: { '/x.iso': { matched: false } } });
await job;
out.ordered = datMatching.matchFor('/x.iso');

// 3. A finished job's read that fails is retried, then lands.
datMatching._jobHydrateRetryMs = 1;
const started = datMatching.startMatchJob(['/r.iso']);
await settleNext({ status: 'started', job_id: 'j2', results: {} });
await started;
const read = datMatching.hydrateFinishedJobs([{ id: 'j2', status: 'completed' }]);
for (let i = 0; i < 50 && !waiting.length; i += 1) await tick();
waiting.shift().reject(new Error('network'));
await settleNext({ results: { '/r.iso': { matched: true, game_name: 'Retried' } } });
await read;
out.retried = datMatching.matchFor('/r.iso');
console.log(JSON.stringify(out));
"""


def test_coderabbit_ordering_and_retry_cases():
    """CodeRabbit on PR #442: a lookup reconciles every path it is newest for,
    a match job's answer takes its place when it was asked, and a finished
    job's read is retried when it fails."""
    result = _run(_CODERABBIT)
    assert result["reconciled"] is None
    assert result["ordered"] == {"matched": True, "game_name": "new"}
    assert result["retried"] == {"matched": True, "game_name": "Retried"}


_EXHAUSTED = """
const out = {};
let failing = true;
const calls = [];
api.startMatchJob = async () => ({ status: 'started', job_id: 'j3', results: {} });
api.getMatchCache = async (paths) => {
  calls.push([...paths]);
  if (failing) throw new Error('network');
  return { results: { '/off.iso': { matched: true, game_name: 'Late' } } };
};
datMatching._jobHydrateRetryMs = 1;
await datMatching.startMatchJob(['/off.iso']);
const ended = [{ id: 'j3', status: 'completed' }];
// Every try fails: the job stays tracked, nothing loops on its own.
await datMatching.hydrateFinishedJobs(ended);
out.triesWhenFailing = calls.length;
out.afterFailing = datMatching.matchFor('/off.iso');
for (let i = 0; i < 20; i += 1) await tick();
out.noLoop = calls.length;
// The next real trigger (another job ending, a refresh) reads it back.
failing = false;
await datMatching.hydrateFinishedJobs(ended);
out.afterTrigger = datMatching.matchFor('/off.iso');
await datMatching.hydrateFinishedJobs(ended);
out.callsAfterDone = calls.length;
console.log(JSON.stringify(out));
"""


def test_a_job_whose_reads_all_fail_is_read_on_the_next_trigger():
    """CodeRabbit on PR #442: once every retry failed, the job was forgotten
    and its off-page rows were never read. It stays tracked, without a loop of
    its own, and the next trigger reads it back."""
    result = _run(_EXHAUSTED)
    assert result["triesWhenFailing"] == 3
    assert result["afterFailing"] is None
    assert result["noLoop"] == 3
    assert result["afterTrigger"] == {"matched": True, "game_name": "Late"}
    assert result["callsAfterDone"] == 4


def test_a_job_that_ended_before_it_was_tracked_is_read_back():
    """CodeRabbit on PR #442: the jobs list may already show the job ended
    when "Recognize these" learns its id, and nothing re-runs the effect."""
    view = (_SRC / "components" / "library" / "RommBrowse.svelte").read_text(encoding="utf-8")
    start = view.index("async function handleRecognize()")
    body = view[start:view.index("\n  }\n", start)]
    assert "datMatching.hydrateFinishedJobs(jobs.jobs)" in body
    assert "wantsHasheousIds(" in view and "needsDatMatch(" in view
