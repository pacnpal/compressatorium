"""A finished job's warning is shown on its row (PR #324 review).

``jobDetailLines`` (src/lib/util/jobDetail.js) decides which detail lines a
job row shows; it is evaluated here with Node. JobRow is checked to render
those lines, so a completed job with a caveat no longer looks like a plain
success.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_DETAIL = _SRC / "lib" / "util" / "jobDetail.js"
_JOB_ROW = _SRC / "lib" / "components" / "panels" / "JobRow.svelte"


def _node() -> str:
    found = shutil.which("node")
    for cand in (found, os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    pytest.skip("node not available")


def _lines(tmp_path, jobs: dict) -> dict:
    script = tmp_path / "eval.mjs"
    script.write_text(
        f"import {{ jobDetailLines }} from {json.dumps(_DETAIL.as_uri())};\n"
        f"const jobs = {json.dumps(jobs)};\n"
        "const out = Object.fromEntries(Object.entries(jobs).map("
        "([k, j]) => [k, jobDetailLines(j)]));\n"
        "process.stdout.write(JSON.stringify(out));\n",
    )
    proc = subprocess.run(
        [_node(), str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_completed_job_shows_its_warning_under_the_output(tmp_path):
    lines = _lines(tmp_path, {
        "warned": {"status": "completed", "output_path": "/v/Game.dosz",
                   "warning": "Repacked, but it may not start."},
        "clean": {"status": "completed", "output_path": "/v/Game.dosz", "warning": None},
    })

    assert lines["warned"] == [
        {"kind": "output", "text": "→ Game.dosz"},
        {"kind": "warning", "text": "Repacked, but it may not start."},
    ]
    assert lines["clean"] == [{"kind": "output", "text": "→ Game.dosz"}]


def test_other_states_keep_their_lines(tmp_path):
    lines = _lines(tmp_path, {
        "running": {"status": "processing", "message": "Repacking Abaron"},
        "failed": {"status": "failed", "error_message": "boom"},
        "queued": {"status": "queued"},
    })

    assert lines["running"] == [{"kind": "message", "text": "Repacking Abaron"}]
    assert lines["failed"] == [{"kind": "error", "text": "boom"}]
    assert lines["queued"] == []


def test_the_job_row_renders_the_detail_lines():
    src = _JOB_ROW.read_text(encoding="utf-8")
    assert "from '$lib/util/jobDetail.js'" in src
    assert "jobDetailLines(job)" in src
    assert "line.kind === 'warning'" in src
