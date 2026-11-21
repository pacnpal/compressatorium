"""A finished job with a warning resolves its toast as a warning (review r10 I2).

A delete-source job whose Source was kept because another job was reading it
ends COMPLETED with ``job.warning`` (#306). ``completionToast``
(src/lib/util/jobToast.js) decides how the running job's toast resolves; it is
evaluated here with Node. The job row's warning line comes from
``jobDetailLines`` (#323's helper, tests/test_frontend_job_warning.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_TOAST = _SRC / "lib" / "util" / "jobToast.js"
_TRACKER = _SRC / "lib" / "stores" / "jobToasts.svelte.js"


def _node() -> str:
    found = shutil.which("node")
    for cand in (found, os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    pytest.skip("node not available")


def _toasts(tmp_path, jobs: dict) -> dict:
    script = tmp_path / "eval.mjs"
    script.write_text(
        f"import {{ completionToast }} from {json.dumps(_TOAST.as_uri())};\n"
        f"const jobs = {json.dumps(jobs)};\n"
        "const out = Object.fromEntries(Object.entries(jobs).map("
        "([k, j]) => [k, completionToast(j)]));\n"
        "process.stdout.write(JSON.stringify(out));\n",
    )
    proc = subprocess.run(
        [_node(), str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_completed_job_with_a_warning_resolves_as_a_warning(tmp_path):
    kept = "Source kept: it was being read by another job"
    out = _toasts(tmp_path, {
        "kept": {"status": "completed", "output_path": "/v/Game.rvz", "warning": kept},
        "clean": {"status": "completed", "output_path": "/v/Game.rvz", "warning": None},
        "bare": {"status": "completed"},
    })

    assert out["kept"]["kind"] == "warning"
    assert kept in out["kept"]["description"]
    assert out["clean"] == {
        "kind": "success", "description": "→ Game.rvz", "duration": 4000,
    }
    assert out["bare"]["kind"] == "success"
    assert out["bare"]["description"] == "Completed"


def test_the_tracker_resolves_completed_jobs_through_it():
    src = _TRACKER.read_text(encoding="utf-8")
    assert "from '$lib/util/jobToast.js'" in src
    assert "completionToast(job)" in src
    # No data-driven method lookup (Semgrep unsafe-dynamic-method).
    assert "toast[" not in src
