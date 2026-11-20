"""One Settings page (restructure PR 5, spec §4.5).

- Settings → Defaults writes the shared ``defaults`` store (the one the Convert
  panel and a new console row start from), the remembered per-format
  compression, and how long History is kept.
- Turning Hasheous on tests it straight away (``datMatching.setHasheousEnabled``),
  so the switch says whether Hasheous answered without a Test button.
- The page is five sections, each with its own deep link.

The store scenario is evaluated with Node like the other store tests (imports
dropped, runes reduced, the API stubbed); skipped without Node.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "lib"
_STORES = _SRC / "stores"
_COMPONENTS = _SRC / "components"


def _read(rel: str) -> str:
    return (_COMPONENTS / rel).read_text(encoding="utf-8")


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
    return body


_STUBS = """
class SvelteMap extends Map {}
const untrack = (fn) => fn();
const calls = [];
let testAnswer = { ok: true, latency_ms: 12 };
const api = {
  setHasheousEnabled: async (enabled) => ({ enabled, url: 'https://h', overridden: true }),
  getDATStats: async () => ({ total_dats: 0, hasheous_enabled: true }),
  testHasheous: async () => {
    calls.push('test');
    if (testAnswer instanceof Error) throw testAnswer;
    return testAnswer;
  },
};
const tick = () => new Promise((r) => setTimeout(r, 0));
"""

_SCENARIO = """
const out = {};
await datMatching.setHasheousEnabled(true);
for (let i = 0; i < 5; i += 1) await tick();
out.on = { calls: calls.length, result: datMatching.hasheousTest };

await datMatching.setHasheousEnabled(false);
for (let i = 0; i < 5; i += 1) await tick();
out.off = { calls: calls.length, result: datMatching.hasheousTest };

testAnswer = new Error('timed out');
await datMatching.setHasheousEnabled(true);
for (let i = 0; i < 5; i += 1) await tick();
out.failed = datMatching.hasheousTest;
console.log(JSON.stringify(out));
"""


def test_turning_hasheous_on_tests_it_and_off_clears_the_result():
    node = _find_node()
    if node is None:
        pytest.skip("Node is not installed")
    script = "\n".join([
        _STUBS,
        _plain(_STORES / "latestSaveQueue.js"),
        _plain(_STORES / "datMatching.svelte.js"),
        "(async () => {",
        _SCENARIO,
        "})().catch((e) => { console.error(e); process.exit(1); });",
    ])
    done = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["on"] == {"calls": 1, "result": {"ok": True, "latency_ms": 12}}
    # Off sends nothing and forgets the last answer.
    assert out["off"] == {"calls": 1, "result": None}
    assert out["failed"] == {"ok": False, "error": "timed out"}


def test_the_defaults_section_writes_the_shared_store():
    src = _read("settings/Defaults.svelte")
    assert "import { defaults } from '$lib/stores/defaults.svelte.js'" in src
    assert "<SafetyOptions" in src
    # The store's fields, both ways: read to draw, saved on change.
    for field in ("verify_after", "delete_on_verify", "container_only"):
        assert f"defaults.values.{field}" in src, field
    assert "defaults.save({" in src
    assert "verify_after: s.checkAfter" in src
    assert "delete_on_verify: s.deleteOriginal" in src
    assert "container_only: s.quickCheck" in src


def test_the_defaults_section_edits_the_remembered_compression_and_history():
    src = _read("settings/Defaults.svelte")
    # The same per-tool preference the Convert panel remembers, merged (the PUT
    # replaces the whole object) and handed back to the Convert panel's store.
    assert "api.getConversionPrefs()" in src and "api.putConversionPrefs(" in src
    assert "formatCompression(" in src and "parseCompression(" in src
    assert "conversion.loadServerPrefs()" in src
    assert "<CompressionPicker" in src
    # History retention, which nothing in the UI set before.
    assert "api.getLedgerSettings()" in src and "api.putLedgerSettings(" in src
    for label in ("Forever", "30 days", "90 days", "1 year"):
        assert label in src, label
    endpoints = (_SRC / "api" / "endpoints.js").read_text(encoding="utf-8")
    assert "getLedgerSettings:" in endpoints and "putLedgerSettings(" in endpoints
    assert "/ledger/settings" in endpoints


def test_settings_is_five_sections_with_deep_links():
    src = _read("views/SettingsView.svelte")
    for section in ("connections", "identification", "notifications", "defaults", "converters"):
        assert f"id: '{section}'" in src, section
    assert 'id="settings-{s.id}"' in src or all(
        f'id="settings-{s}"' in src
        for s in ("connections", "identification", "notifications", "defaults", "converters")
    )
    for part in ("<Connections />", "<Identification />", "<NotificationSettings />",
                 "<Defaults />", "<Converters />"):
        assert part in src, part
    # The in-page nav is sticky on a wide screen.
    assert "position: sticky" in src and "1100px" in src


def test_hasheous_lives_in_connections_and_identification_is_trimmed():
    conn = _read("settings/Connections.svelte")
    assert "Identify games online (Hasheous)" in conn
    assert "datMatching.setHasheousEnabled(" in conn
    assert "<HasheousQuestion" in conn
    assert "Reached Hasheous" in conn and "Couldn't reach Hasheous" in conn
    assert "Set up from a Library's watch folders." in conn
    ident = _read("settings/Identification.svelte")
    assert "Hasheous" not in ident
    # The hash-count tiles, the env-var sentence and the second hidden label go.
    assert "total_sha1_hashes" not in ident and "COMPRESSATORIUM_" not in ident
    assert 'aria-label="Import DAT file"' not in ident
    assert "<summary>Name order</summary>" in ident
    assert not (_COMPONENTS / "views" / "DATView.svelte").exists()
