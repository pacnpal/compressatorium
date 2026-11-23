"""A refused delete says why (#563).

``POST /api/files/delete-batch`` keeps a file a sheet or playlist still needs
and says so in each result's ``error``. The confirmation used to show only
"Deleted 0; 2 failed". ``src/lib/util/deleteOutcome.js`` turns the answer into
the toast, with the backend's reasons; evaluated with Node.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_delete_check_298 import _find_node

_SRC = Path(__file__).resolve().parents[1] / "src" / "lib"
_HELPER = _SRC / "util" / "deleteOutcome.js"

_KEPT_CUE = (
    "Final Quest.m3u still needs Final Quest (Disc 1).cue and isn't being deleted; "
    "Final Quest (Disc 1).cue is kept"
)
_KEPT_BIN = (
    "Final Quest (Disc 1).cue still needs Final Quest (Disc 1).bin and isn't being "
    "deleted; Final Quest (Disc 1).bin is kept"
)


def _outcome(tmp_path, result, paths):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "eval.mjs"
    script.write_text(
        f"import {{ deleteOutcome }} from {json.dumps(_HELPER.as_uri())};\n"
        f"const out = deleteOutcome({json.dumps(result)}, {json.dumps(paths)});\n"
        "process.stdout.write(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_refused_delete_names_what_still_needs_the_file(tmp_path):
    cue, bin_ = "/v/PS/Final Quest (Disc 1).cue", "/v/PS/Final Quest (Disc 1).bin"
    out = _outcome(tmp_path, {
        "total": 2, "success": 0, "failed": 2,
        "results": [
            {"path": cue, "success": False, "error": _KEPT_CUE},
            {"path": bin_, "success": False, "error": _KEPT_BIN},
        ],
    }, [cue, bin_])

    assert out["kind"] == "warning"
    assert out["title"] == "Kept 2 files"
    assert _KEPT_CUE in out["description"] and _KEPT_BIN in out["description"]
    assert out["removed"] == []


def test_a_partial_delete_counts_both_and_forgets_only_what_went(tmp_path):
    out = _outcome(tmp_path, {
        "total": 2, "success": 1, "failed": 1,
        "results": [
            {"path": "/v/a.iso", "success": True},
            {"path": "/v/b.cue", "success": False, "error": "b.m3u still needs b.cue"},
        ],
    }, ["/v/a.iso", "/v/b.cue"])

    assert (out["kind"], out["title"]) == ("warning", "Deleted 1; kept 1")
    assert out["description"] == "b.m3u still needs b.cue."
    assert out["removed"] == ["/v/a.iso"]


def test_many_reasons_show_the_first_three_and_a_count(tmp_path):
    results = [
        {"path": f"/v/{n}.cue", "success": False, "error": f"{n}.m3u still needs {n}.cue"}
        for n in "abcde"
    ]
    out = _outcome(tmp_path, {"total": 5, "success": 0, "failed": 5, "results": results},
                   [r["path"] for r in results])

    assert out["description"].count("still needs") == 3
    assert out["description"].endswith("…and 2 more.")


def test_a_clean_delete_is_a_plain_success(tmp_path):
    out = _outcome(tmp_path, {
        "total": 1, "success": 1, "failed": 0,
        "results": [{"path": "/v/a.iso", "success": True}],
    }, ["/v/a.iso"])

    assert out == {"kind": "success", "title": "Deleted 1 file", "description": None,
                   "removed": ["/v/a.iso"]}


def test_an_answer_without_results_forgets_every_requested_path(tmp_path):
    out = _outcome(tmp_path, {"success": 2}, ["/v/a.iso", "/v/b.iso"])

    assert (out["kind"], out["title"], out["removed"]) == (
        "success", "Deleted 2 files", ["/v/a.iso", "/v/b.iso"],
    )


def test_the_bulk_delete_dialog_toasts_the_outcome():
    modal = (_SRC / "components" / "modals" / "BulkDeleteModal.svelte").read_text(
        encoding="utf-8",
    )
    assert "deleteOutcome(" in modal
    assert "failed`" not in modal  # no more bare "Deleted N; M failed"
