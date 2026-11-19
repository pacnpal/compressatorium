"""The workspace and the toast for companion rows and batch skips (#504).

A listing row the backend marks ``companion_of`` (a later RAR volume, a split
Wii U dump's part) is part of its set: never tickable on its own, with a note
naming the Source. A batch submit asks the backend for its skips and the
toast says why each file was left out. The stores are evaluated with Node
like the other ``test_frontend_*`` store tests; skipped without Node.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_frontend_conversion_store import _run as _run_conversion
from .test_frontend_file_browser_open_folder import _STUBS as _BROWSER_STUBS
from .test_frontend_file_browser_open_folder import _plain_js as _browser_js
from .test_frontend_jobs_store import _SRC as _STORES
from .test_frontend_jobs_store import _STUBS as _JOBS_STUBS
from .test_frontend_jobs_store import _plain as _plain_store
from .test_frontend_rules_view import _find_node

_LIB = Path(__file__).resolve().parents[1] / "src" / "lib"


def _node_eval(tmp_path: Path, source: str) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "eval_504.mjs"
    script.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.fail(proc.stderr)
    return json.loads(proc.stdout)


def test_a_companion_row_cannot_be_ticked(tmp_path):
    data = _node_eval(tmp_path, _BROWSER_STUBS + _browser_js() + """
const first = { name: 'x.part1.rar', path: '/v/x.part1.rar', type: 'archive' };
const later = {
  name: 'x.part2.rar', path: '/v/x.part2.rar', type: 'file',
  companion_of: '/v/x.part1.rar',
};
const plain = { name: 'y.iso', path: '/v/y.iso', type: 'file' };
fileBrowser.entries = [first, later, plain];
fileBrowser.toggleSelect(later);
const afterToggle = [...fileBrowser.selectedFiles.keys()];
fileBrowser.toggleSelectAll?.();
process.stdout.write(JSON.stringify({
  later: fileBrowser.isSelectable(later),
  plain: fileBrowser.isSelectable(plain),
  first: fileBrowser.isSelectable(first),
  afterToggle,
  selectable: fileBrowser.selectableEntries.map((e) => e.name),
  afterAll: [...fileBrowser.selectedFiles.keys()],
}));
""")

    assert data["later"] is False, data
    assert data["plain"] is True, data
    assert data["first"] is True, data
    assert data["afterToggle"] == [], data
    assert "x.part2.rar" not in data["selectable"], data
    assert "/v/x.part2.rar" not in data["afterAll"], data


def test_a_companion_row_names_the_source_it_belongs_to():
    row = (_LIB / "components" / "panels" / "FileRow.svelte").read_text(encoding="utf-8")

    assert "entry?.companion_of" in row
    assert "part of" in row


_SKIPS = """
const seen = [];
toast.warning = (m, o) => seen.push(['warning', m, o?.description ?? null]);
toast.success = (m, o) => seen.push(['success', m, o?.description ?? null]);
const store = new ConversionStore();
store.mode = 'rar_extract';
"""


def test_the_toast_says_why_each_item_was_skipped(tmp_path):
    data = _run_conversion(tmp_path, _SKIPS + """
jobs.createBatch = async () => ({
  jobs: [],
  skipped: [{
    path: '/v/x.part2.rar', code: 'rar_set_not_extractable',
    reason: 'x.part2.rar is a later volume of a RAR set; select its first volume, x.part1.rar',
  }],
});
await store.submit(['/v/x.part2.rar']);
process.stdout.write(JSON.stringify({ seen }));
""")

    [(kind, title, description)] = data["seen"]
    assert kind == "warning", data
    assert title == "No jobs queued (all 1 skipped)", data
    assert "later volume of a RAR set" in description, data
    assert "x.part1.rar" in description, data


def test_the_toast_names_the_first_few_reasons_and_counts_the_rest(tmp_path):
    data = _run_conversion(tmp_path, _SKIPS + """
const paths = ['/v/a.iso', '/v/b.iso', '/v/c.iso', '/v/d.iso', '/v/e.iso', '/v/k.iso'];
jobs.createBatch = async () => ({
  jobs: [{ id: 'j', file_path: '/v/k.iso', output_path: '/v/k.chd' }],
  skipped: paths.slice(0, 5).map((p) => ({
    path: p, code: 'file_not_found', reason: 'File not found',
  })),
});
await store.submit(paths);
process.stdout.write(JSON.stringify({ seen }));
""")

    [(kind, title, description)] = data["seen"]
    assert kind == "success", data
    assert title == "Queued 1 of 6 job(s); 5 skipped", data
    assert description.count("File not found") == 3, data
    assert "a.iso" in description and "c.iso" in description, data
    assert "d.iso" not in description, data
    assert "2 more" in description, data


def test_a_batch_with_nothing_skipped_has_no_reasons(tmp_path):
    data = _run_conversion(tmp_path, _SKIPS + """
jobs.createBatch = async () => ({
  jobs: [{ id: 'j', file_path: '/v/a.iso', output_path: '/v/a.chd' }], skipped: [],
});
await store.submit(['/v/a.iso']);
process.stdout.write(JSON.stringify({ seen }));
""")

    assert data["seen"] == [["success", "Queued 1 job(s)", None]], data


def test_the_jobs_store_asks_for_the_skips_and_returns_them(tmp_path):
    data = _node_eval(tmp_path, _JOBS_STUBS + """
const sent = [];
api.createBatchJobs = async (...args) => {
  sent.push(args);
  return {
    jobs: [{ id: 'j1', file_path: '/v/a.iso', mode: 'createdvd', status: 'queued' }],
    skipped: [{ path: '/v/b.iso', reason: 'File not found', code: 'file_not_found' }],
  };
};
""" + _plain_store(_STORES / "jobConstants.js") + _plain_store(_STORES / "jobs.svelte.js") + """
const store = new JobsStore();
const result = await store.createBatch(['/v/a.iso', '/v/b.iso'], 'createdvd');
process.stdout.write(JSON.stringify({
  jobs: result.jobs.map((j) => j.id),
  skipped: result.skipped,
  listed: store.jobs.map((j) => j.id),
}));
""")

    assert data["jobs"] == ["j1"], data
    assert data["skipped"] == [
        {"path": "/v/b.iso", "reason": "File not found", "code": "file_not_found"},
    ], data
    assert data["listed"] == ["j1"], data


def test_the_batch_request_opts_into_the_skip_report():
    endpoints = (_LIB / "api" / "endpoints.js").read_text(encoding="utf-8")

    assert "/jobs/batch?report_skipped=true" in endpoints
