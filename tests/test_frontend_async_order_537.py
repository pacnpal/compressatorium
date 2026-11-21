"""Async ordering in the shell's new pieces (#537 review round 4).

Activity's opening tab waits for the authoritative findings read and the
first jobs snapshot; a stale CHD scan status never overrides a scan that just
started and a failed poll keeps polling; Add a Library can't confirm twice; a
RomM catalog that is still switching platforms can't be converted; and the
defaults store ignores a load that answers after a save began.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_registry_derives_186 import _find_node

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _store(tmp_path: Path, rel: str, stubs: str, scenario: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    body = re.sub(r"^import[^\n]*\n", "", _read(rel), flags=re.MULTILINE)
    body = body.replace("$state(", "(").replace("export ", "")
    script = tmp_path / "eval.mjs"
    script.write_text(stubs + body + scenario, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_DEFERRED = """
const __calls = {};
function __defer(name) {
  return (...args) => new Promise((resolve, reject) => {
    (__calls[name] ??= []).push({ args, resolve, reject });
  });
}
const tick = () => new Promise((r) => setImmediate(r));
"""


def test_a_stale_scan_status_never_overrides_a_scan_that_started(tmp_path):
    """CodeRabbit 4651856954: the mount's status read answers after Scan."""
    got = _store(tmp_path, "stores/chdMetadata.svelte.js", _DEFERRED + """
const SvelteMap = Map;
const api = {
  getScanStatus: __defer('status'), scanMetadata: __defer('scan'),
  getCHDMetadataBatch: async () => ({}),
};
""", """
const s = chdMetadata;
const mountRead = s.pollStatus();
await tick();
const started = s.startScan();
await tick();
__calls.scan[0].resolve({ started: true });
await started;
__calls.status[0].resolve({ scanning: false, total: 0 });
await mountRead;
process.stdout.write(JSON.stringify({ running: s.scanRunning }));
""")
    assert got == {"running": True}, got


def test_a_failed_poll_keeps_the_scan_running(tmp_path):
    """CodeRabbit 4651856958: one failed status read must not end polling."""
    got = _store(tmp_path, "stores/chdMetadata.svelte.js", _DEFERRED + """
const SvelteMap = Map;
const api = {
  getScanStatus: __defer('status'), scanMetadata: async () => ({ started: true }),
  getCHDMetadataBatch: async () => ({}),
};
""", """
const s = chdMetadata;
await s.startScan();
const poll = s.pollStatus();
await tick();
__calls.status[0].reject(new Error('blip'));
await poll;
process.stdout.write(JSON.stringify({ running: s.scanRunning }));
""")
    assert got == {"running": True}, got


def test_a_defaults_load_answering_after_a_save_is_dropped(tmp_path):
    got = _store(tmp_path, "stores/defaults.svelte.js", _DEFERRED + """
const api = { getDefaultsPrefs: __defer('get'), putDefaultsPrefs: __defer('put') };
""", """
const loading = defaults.load();
await tick();
const saving = defaults.save({ delete_on_verify: true });
await tick();
__calls.put[0].resolve({ ...__calls.put[0].args[0] });
await saving;
__calls.get[0].resolve({ verify_after: true, delete_on_verify: false, container_only: false });
await loading;
process.stdout.write(JSON.stringify(defaults.values));
""")
    assert got["delete_on_verify"] is True, got


def test_findings_count_their_authoritative_reads():
    """Codex 4651838336 / CodeRabbit 4651856938: only a read that wrote the
    list counts, so a superseded read can't stand for the latest one."""
    store = _read("stores/findings.svelte.js")
    assert "reads = $state(0);" in store
    load = store.split("async load() {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (request === this.#request) this.reads += 1;" in load
    jobs = _read("stores/jobs.svelte.js")
    assert "hydrated = $state(false);" in jobs
    assert "this.hydrated = true;" in jobs.split("async refresh() {", 1)[1].split("\n  }\n", 1)[0]


def test_activity_decides_its_tab_after_both_reads():
    src = _read("components/views/ActivityView.svelte")
    assert "const settled = $derived(findings.reads > readsAtMount && jobs.hydrated);" in src
    assert "if (opening === null && settled) opening = live;" in src


def test_add_library_confirms_once():
    """CodeRabbit 4651856945: a second confirm while one runs does nothing."""
    src = _read("components/modals/AddLibraryModal.svelte")
    assert "if (pending) return;" in src
    assert "pending = true;" in src


def test_a_switching_romm_catalog_cannot_convert():
    """CodeRabbit 4651856950: while a new platform's catalog loads, the old
    rows can't be converted with the new platform's tools."""
    # Restructure 2/6: the panel's own Convert-to list replaces ModeSelect.
    panel = _read("components/panels/ConvertPanel.svelte")
    assert "romm.platforms.find((p) => p.id === fileBrowser.rommPlatformId)" in panel
    assert "fileBrowser.rommPlatformId !== null && (!platform || fileBrowser.loading)" in panel
    assert "catalogLoading ? [] : convertTargets({" in panel
    assert "|| catalogLoading" in panel


def test_make_this_folder_a_library_registers_once():
    src = _read("components/views/FilesView.svelte")
    body = src.split("async function makeLibrary() {", 1)[1]
    assert body.lstrip().startswith("if (making) return;")
