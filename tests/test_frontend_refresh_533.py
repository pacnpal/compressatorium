"""The web UI's refreshes after #533 (CodeRabbit 4650577592, 4650577595).

* The Ledger cards keep each good read when another of the three fails.
* The file listing refreshes on a new finished job, keyed on job identity
  like the dashboard: at the history cap one finished job replaces another
  and the counts stay flat.

Evaluated with Node like the other store tests; skipped where it is missing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_jobs_store import _plain

_SRC = Path(__file__).resolve().parents[1] / "src"
_STORES = _SRC / "lib" / "stores"


def _node(tmp_path: Path, script: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    path = tmp_path / "eval.mjs"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [node, str(path)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_failed_totals_read_keeps_the_good_ones(tmp_path):
    data = _node(tmp_path, """
const api = {
  getLedgerRecent: async () => [{ id: 'r1' }],
  getLedgerSavings: async () => ({ saved: 7 }),
  getLedgerTotals: async () => { throw new Error('503'); },
};
""" + _plain(_STORES / "ledger.svelte.js") + """
ledger.totals = { completed: 3 };
await ledger._load();
process.stdout.write(JSON.stringify({
  recent: ledger.recent, savings: ledger.savings, totals: ledger.totals,
}));
""")

    assert data == {"recent": [{"id": "r1"}], "savings": {"saved": 7}, "totals": {"completed": 3}}


def test_a_late_ledger_read_never_overwrites_a_newer_one(tmp_path):
    data = _node(tmp_path, """
let calls = 0;
const api = {
  getLedgerRecent: async () => {
    calls += 1;
    const mine = calls;
    await new Promise((r) => setTimeout(r, mine === 1 ? 30 : 0));
    return [{ id: `r${mine}` }];
  },
  getLedgerSavings: async () => null,
  getLedgerTotals: async () => ({ jobs: { completed: 1 } }),
};
""" + _plain(_STORES / "ledger.svelte.js") + """
await Promise.all([ledger._load(), ledger._load()]);
process.stdout.write(JSON.stringify(ledger.recent));
""")

    assert data == [{"id": "r2"}]


def test_finished_job_ids_change_when_one_replaces_another(tmp_path):
    data = _node(tmp_path, _plain(_STORES / "jobConstants.js") + """
const before = terminalJobIds([
  { id: 'a', status: 'completed' }, { id: 'b', status: 'processing' },
]);
const after = terminalJobIds([
  { id: 'c', status: 'failed' }, { id: 'b', status: 'cancelled' },
]);
process.stdout.write(JSON.stringify({ before, after }));
""")

    assert data == {"before": ["a"], "after": ["c", "b"]}


def test_the_file_listing_refreshes_on_job_identity_not_counts():
    app = (_SRC / "App.svelte").read_text(encoding="utf-8")
    # The space-saved card moved from the Dashboard to Activity (#534).
    dashboard = (_SRC / "lib" / "components" / "views" / "ActivityView.svelte").read_text(
        encoding="utf-8",
    )

    assert "terminalJobIds(" in app
    assert "completedCount" not in app and "_lastTerminal" not in app
    assert "terminalJobIds(" in dashboard
