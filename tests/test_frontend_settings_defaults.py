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
    # The same per-tool preference the Convert panel remembers. The conversion
    # store owns the save (its debounce outlives this section, Codex
    # 4652247060) and applies it live even after an edit (Codex 4652247066);
    # a pending save is flushed before the section reads the server's copy.
    assert "await conversion.flushCompressionPrefs();" in src
    assert "api.getConversionPrefs()" in src
    assert "conversion.applySavedDefaults(" in src
    assert "api.putConversionPrefs(" not in src and "setTimeout(" not in src
    assert "formatCompression(" in src and "parseCompression(" in src
    assert "<CompressionPicker" in src
    # History retention, which nothing in the UI set before.
    assert "api.getLedgerSettings()" in src and "api.putLedgerSettings(" in src
    for label in ("Forever", "30 days", "90 days", "1 year"):
        assert label in src, label
    # Retention saves run in order and only the newest one's answer shows.
    assert "retentionChain = retentionChain.then(" in src
    assert "if (ticket === retentionSaves) retention = retentionStored;" in src
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
    # The in-page nav is sticky on a wide screen, below the sticky top bar
    # (Codex 4652247071), and a section scrolled to stops below it too.
    assert "position: sticky" in src and "1100px" in src
    assert "top: calc(var(--topbar-height) + var(--space-4));" in src
    assert "scroll-margin-top: calc(var(--topbar-height) + var(--space-4));" in src
    # The sections above load on their own and grow after the first scroll, so
    # a deep link keeps its section in place until the page settles or the
    # user scrolls.
    assert "new ResizeObserver(" in src and "SETTLE_MS" in src
    for stop in ("'wheel'", "'touchstart'", "'keydown'"):
        assert stop in src, stop
    # ...which needs <main> not to be a scroll box: the page scrolls, and a
    # sticky child sticks to its nearest scroll box.
    app = (_ROOT / "src" / "App.svelte").read_text(encoding="utf-8")
    main_rule = re.search(r"\n  \.main \{([^}]*)\}", app).group(1)
    assert "overflow-y: auto" not in main_rule and "overflow-x: clip" in main_rule


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


def test_identification_imports_packs_and_syncs_redump():
    """#527's DAT-screen pieces live in Settings → Game identification."""
    ident = _read("settings/Identification.svelte")
    assert "datMatching.importDATs(files)" in ident
    assert 'accept=".dat,.xml,.zip"' in ident and "multiple" in ident
    assert "Get Redump lists for my consoles" in ident
    assert "datMatching.syncRedump()" in ident and "datMatching.loadRedump()" in ident
    # The first-run question moved to Connections (HasheousQuestion).
    assert "hasheousUndecided" not in ident and "Yes, look them up" not in ident


_REDUMP_STUBS = """
class SvelteMap extends Map {}
const untrack = (fn) => fn();
const pending = [];
let syncCalls = 0;
const api = {
  getRedump: () => new Promise((resolve) => pending.push(resolve)),
  syncRedump: () => { syncCalls += 1; return new Promise((resolve) => pending.push(resolve)); },
  getDATStats: async () => ({ total_dats: 0 }),
  getDATRanking: async () => ({ custom: false, dats: [] }),
};
"""

_REDUMP_SCENARIO = """
const out = {};
// Two loads answer out of order: the older answer is dropped.
const a = datMatching.loadRedump();
const b = datMatching.loadRedump();
pending[1]({ systems: [{ name: 'new' }] });
await b;
pending[0]({ systems: [{ name: 'old' }] });
await a;
out.redump = datMatching.redump.systems[0].name;
pending.length = 0;
// A second click while a sync runs starts nothing.
const first = datMatching.syncRedump();
out.second = await datMatching.syncRedump();
out.calls = syncCalls;
pending[0]({ systems: [], message: 'ok' });
// The sync reloads the systems once it's done; answer that too.
for (let i = 0; i < 20 && pending.length < 2; i += 1) await new Promise((r) => setTimeout(r, 0));
pending[1]({ systems: [{ name: 'new' }] });
await first;
console.log(JSON.stringify(out));
"""


def test_redump_loads_keep_the_newest_and_a_sync_never_runs_twice():
    node = _find_node()
    if node is None:
        pytest.skip("Node is not installed")
    script = "\n".join([
        _REDUMP_STUBS,
        _plain(_STORES / "latestSaveQueue.js"),
        _plain(_STORES / "datMatching.svelte.js"),
        "(async () => {",
        _REDUMP_SCENARIO,
        "})().catch((e) => { console.error(e); process.exit(1); });",
    ])
    done = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out == {"redump": "new", "second": None, "calls": 1}
