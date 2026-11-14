"""The verification store keeps check values that arrive mid-load (#298).

``loadVerified()`` replays ``/api/verified`` over the store. A job's
``complete`` event can land while that request is in flight and record a
Round-tripped output; the replay must keep both the path and its check, or the
badge falls back to Intact until the next load.

Evaluated with Node like ``test_frontend_romm_store.py``: imports dropped,
``$state(x)`` reduced to ``x``, Svelte's reactive collections stubbed with the
plain ones. Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_STORE_JS = (
    Path(__file__).resolve().parents[1]
    / "src" / "lib" / "stores" / "verification.svelte.js"
)


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
    kept = [
        line for line in src.splitlines()
        if not (line.strip().startswith("import ") and line.strip().endswith(";"))
    ]
    body = re.sub(r"\$state\(", "(", "\n".join(kept))
    assert "$derived" not in body and "$effect" not in body
    return body


_STUBS = """
const SvelteMap = Map;
const SvelteSet = Set;
const registry = {};
let __resolve;
const api = {
  getVerifiedCHDs: () => new Promise((resolve) => { __resolve = resolve; }),
};
"""


def _run(tmp_path: Path, scenario: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the verification store")
    script = tmp_path / "verification_store_eval.mjs"
    script.write_text(_STUBS + _plain_js() + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the verification store:\n{proc.stderr}")
    return json.loads(proc.stdout)


_MID_FLIGHT = """
const store = new VerificationStore();
store.statuses.add('/v/old.chd');
store.checks.set('/v/old.chd', 'intact');
store.statuses.add('/v/redone.rvz');
store.checks.set('/v/redone.rvz', 'intact');

const load = store.loadVerified();
// While /api/verified is in flight, two `complete` events land (the same
// writes jobs.svelte.js makes): a new Round-tripped output, and a re-run that
// upgrades a path the store already had.
store.statuses.add('/v/new.rvz');
store.checks.set('/v/new.rvz', 'round_tripped');
store.checks.set('/v/redone.rvz', 'round_tripped');

// The server's answer predates both.
__resolve({
  verified: ['/v/old.chd', '/v/redone.rvz'],
  checks: { '/v/old.chd': 'intact', '/v/redone.rvz': 'intact' },
});
await load;

process.stdout.write(JSON.stringify({
  old: store.checkFor('/v/old.chd'),
  fresh: store.checkFor('/v/new.rvz'),
  redone: store.checkFor('/v/redone.rvz'),
}));
"""


def test_a_check_recorded_during_a_load_survives_the_replay(tmp_path):
    out = _run(tmp_path, _MID_FLIGHT)
    assert out == {"old": "intact", "fresh": "round_tripped", "redone": "round_tripped"}


_STATUS_AND_CHECK_MOVE_TOGETHER = """
const store = new VerificationStore();
store.markChecked('/v/a.rvz', 'round_tripped');
store.markChecked('/v/b.rvz', 'round_tripped');
store.markChecked('/v/c.rvz', 'round_tripped');

// A same-format rename keeps the check that passed.
store.move('/v/a.rvz', '/v/renamed.rvz', { keep: true });
// A cross-format rename drops it.
store.move('/v/b.rvz', '/v/b.iso', { keep: false });
// A deleted file forgets its check, so nothing stale is inherited later.
store.forget('/v/c.rvz');
// A manual Verify is a container check: the backend stores intact.
store.markChecked('/v/c.rvz');

process.stdout.write(JSON.stringify({
  oldA: store.checkFor('/v/a.rvz'),
  renamed: store.checkFor('/v/renamed.rvz'),
  oldB: store.checkFor('/v/b.rvz'),
  crossFormat: store.checkFor('/v/b.iso'),
  reverified: store.checkFor('/v/c.rvz'),
}));
"""


def test_status_and_check_move_together(tmp_path):
    out = _run(tmp_path, _STATUS_AND_CHECK_MOVE_TOGETHER)
    assert out == {
        "oldA": None, "renamed": "round_tripped",
        "oldB": None, "crossFormat": None,
        "reverified": "intact",
    }
