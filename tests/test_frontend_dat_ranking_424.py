"""The DAT order in the DAT store survives a slow load (#424, CodeRabbit on PR #435).

``src/lib/stores/datMatching.svelte.js`` loads the DAT order with ``loadDATs``
and saves it with ``setDATRanking``. A load that started before a save but
answered after it must not put the old order (or ``custom: false``) back on
screen, and an older load must not replace a newer one.

Evaluated with Node like the other store tests: imports dropped, runes reduced
to their initial values, the API stubbed with promises the test settles in
whatever order it needs, and the shared ``latestSaveQueue.js`` loaded as it
ships. Skipped without Node.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_STORES = Path(__file__).resolve().parents[1] / "src" / "lib" / "stores"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _plain(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    body = re.sub(r"^import[\s\S]*?from\s+'[^']+';\s*$", "", src, flags=re.MULTILINE)
    body = re.sub(r"^export\s+", "", body, flags=re.MULTILINE)
    body = re.sub(r"\$state\(", "(", body)
    leftover = [r for r in ("$state(", "$derived(", "$effect(") if r in body]
    assert not leftover, f"{path.name} grew runes this harness does not reduce: {leftover}"
    return body


_STUBS = """
class SvelteMap extends Map {}
const untrack = (fn) => fn();
const pending = [];
function deferred(name) {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  pending.push({ name, resolve });
  return promise;
}
const api = {
  getDATRanking: () => deferred('get'),
  setDATRanking: () => deferred('put'),
  resetDATRanking: () => deferred('delete'),
  getDATStats: async () => ({ total_dats: 2 }),
};
const tick = () => new Promise((r) => setTimeout(r, 0));
// Answers the oldest pending request of *name*, or the newest with `newest`.
async function settle(name, value, newest = false) {
  for (let i = 0; i < 50 && !pending.some((p) => p.name === name); i += 1) await tick();
  const matching = pending.map((p, i) => (p.name === name ? i : -1)).filter((i) => i >= 0);
  const index = matching.length ? matching[newest ? matching.length - 1 : 0] : -1;
  if (index < 0) throw new Error(`no pending ${name}`);
  pending.splice(index, 1)[0].resolve(value);
  for (let i = 0; i < 5; i += 1) await tick();
}
"""

_SCENARIO = """
const OLD = { custom: false, dats: [{ id: 'a' }, { id: 'b' }] };
const NEW = { custom: true, dats: [{ id: 'b', rank: 0 }, { id: 'a', rank: 1 }] };
const ids = () => datMatching.dats.map((d) => d.id).join(',');
const out = {};

// 1. A load started before a save answers after it.
const load = datMatching.loadDATs();
const save = datMatching.setDATRanking(['b', 'a']);
await settle('put', NEW);
await save;
await settle('get', OLD);
await load;
out.afterStaleLoad = { order: ids(), custom: datMatching.datsRankingCustom };

// 2. Two loads answer out of order: the newer request answers first, the
// older one last, and the older answer is dropped.
const first = datMatching.loadDATs();
const second = datMatching.loadDATs();
for (let i = 0; i < 10; i += 1) await tick();
await settle('get', NEW, true);
await settle('get', OLD);
await Promise.all([first, second]);
out.outOfOrderLoads = { order: ids(), custom: datMatching.datsRankingCustom };
console.log(JSON.stringify(out));
"""


def _run(scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("Node is not installed")
    script = "\n".join([
        _STUBS,
        _plain(_STORES / "latestSaveQueue.js"),
        _plain(_STORES / "datMatching.svelte.js"),
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


def test_a_load_that_predates_a_save_does_not_undo_it():
    result = _run(_SCENARIO)
    assert result["afterStaleLoad"] == {"order": "b,a", "custom": True}


def test_an_older_load_does_not_replace_a_newer_one():
    result = _run(_SCENARIO)
    assert result["outOfOrderLoads"] == {"order": "b,a", "custom": True}
