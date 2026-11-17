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
    view = (_SRC / "components" / "views" / "RommView.svelte").read_text(encoding="utf-8")
    browser = (_SRC / "stores" / "fileBrowser.svelte.js").read_text(encoding="utf-8")
    assert "dat_match ??" not in row and "dat_match ??" not in view
    assert "datMatching.seedListing(" in browser
    assert "datMatching.hydrateFinishedJobs(" in view
