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
datMatching.seedListing(rows, datMatching.policyGeneration);
out.seeded = datMatching.matchFor('/a.iso');
out.unseeded = datMatching.matchFor('/b.iso');

// 2. A policy change (Hasheous switched on in another tab) clears the store;
// a seed from a listing loaded before it is dropped.
const before = datMatching.policyGeneration;
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


def _run() -> dict:
    node = harness._find_node()
    if node is None:
        pytest.skip("Node is not installed")
    stores = harness._STORES
    script = "\n".join([
        _STUBS,
        harness._plain(stores / "latestSaveQueue.js"),
        harness._plain(stores / "datMatching.svelte.js"),
        "(async () => {",
        _SCENARIO,
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


def test_the_views_read_the_store_and_not_the_listing_snapshot():
    row = (_SRC / "components" / "panels" / "FileRow.svelte").read_text(encoding="utf-8")
    view = (_SRC / "components" / "views" / "RommView.svelte").read_text(encoding="utf-8")
    browser = (_SRC / "stores" / "fileBrowser.svelte.js").read_text(encoding="utf-8")
    assert "dat_match ??" not in row and "dat_match ??" not in view
    assert "datMatching.seedListing(" in browser
    assert "datMatching.hydrateFinishedJobs(" in view
