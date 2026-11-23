"""A job's toast names it the way its row does (#565).

A watch folder's copy, a check Rule's run and the other external jobs have a
placeholder ``file_path`` (``/__external_jobs__/<id>``), so the live toast,
which titled every job with the last part of its path, read "5d7c1691". Job
rows already used the job's own ``filename``. Both now ask ``jobName``
(``src/lib/api/format.js``); evaluated with Node.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_delete_check_298 import _find_node

_LIB = Path(__file__).resolve().parents[1] / "src" / "lib"


def _names(tmp_path, cases):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "eval.mjs"
    script.write_text(
        f"import {{ jobName }} from {json.dumps((_LIB / 'api' / 'format.js').as_uri())};\n"
        f"const cases = {json.dumps(cases)};\n"
        "process.stdout.write(JSON.stringify(cases.map(([job, opts]) => jobName(job, opts))));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_an_external_job_is_named_by_its_filename(tmp_path):
    copy = {"id": "5d7c1691", "filename": "shadow-colossus.iso",
            "file_path": "/__external_jobs__/5d7c1691", "mode": "inbox_copy"}
    convert = {"id": "a1", "filename": "ignored.cue",
               "file_path": "/data/PS/Final Quest (Disc 1).cue", "mode": "createcd"}

    assert _names(tmp_path, [
        [copy, {"external": True}],
        [convert, {"external": False}],
        [{"file_path": "/data/a.iso"}, {"external": True}],
        [{}, {}],
    ]) == ["shadow-colossus.iso", "Final Quest (Disc 1).cue", "a.iso", ""]


def test_the_toast_and_the_row_both_ask_job_name():
    toasts = (_LIB / "stores" / "jobToasts.svelte.js").read_text(encoding="utf-8")
    row = (_LIB / "components" / "panels" / "JobRow.svelte").read_text(encoding="utf-8")
    for source in (toasts, row):
        assert "jobName(job, { external: !" in source
    # The toast's subtitle names an external job's kind, as the row does.
    assert "EXTERNAL_MODE_LABELS" in toasts
    assert "file_path?.split(" not in toasts + row
