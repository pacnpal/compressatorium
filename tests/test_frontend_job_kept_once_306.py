"""A job row shows why its Source was kept exactly once (#295 + #306).

Every kept-Source ending sets ``job.warning`` (so the CLI reports it) and also
puts the same reason in ``job.message``. JobRow is rendered here server-side
through the project's own Vite config, and the reason must appear once.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = """
import { pathToFileURL } from 'node:url';
const { createServer } = await import(
  pathToFileURL(ROOT + '/node_modules/vite/dist/node/index.js').href);
const server = await createServer({
  root: ROOT, configFile: ROOT + '/vite.config.js', logLevel: 'silent',
  appType: 'custom', server: { middlewareMode: true, hmr: false, ws: false },
});
try {
  const { render } = await server.ssrLoadModule('svelte/server');
  const JobRow = (await server.ssrLoadModule('/src/lib/components/panels/JobRow.svelte')).default;
  const out = {};
  for (const [name, job] of Object.entries(JOBS)) {
    out[name] = render(JobRow, { props: { job } }).body;
  }
  process.stdout.write(JSON.stringify(out));
} finally {
  await server.close();
}
"""


def _node() -> str:
    found = shutil.which("node")
    for cand in (found, os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    pytest.skip("node not available")


def _render(tmp_path, jobs: dict) -> dict:
    if not (_ROOT / "node_modules" / "vite").exists():
        pytest.skip("node_modules not installed")
    script = tmp_path / "render.mjs"
    script.write_text(
        f"const ROOT = {json.dumps(str(_ROOT))};\nconst JOBS = {json.dumps(jobs)};\n"
        + _SCRIPT,
    )
    proc = subprocess.run(
        [_node(), str(script)], capture_output=True, text=True, timeout=120,
        check=False, cwd=_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


REASON = "converted, source kept: couldn't update Game.m3u (is read-only)"


def _kept_job(**extra) -> dict:
    return {
        "id": "j1", "status": "completed", "mode": "createcd",
        "file_path": "/v/Game (Disc 1).cue", "output_path": "/v/Game (Disc 1).chd",
        "delete_on_verify": True, "deleted_paths": [], "message": REASON, **extra,
    }


def test_a_kept_source_reason_is_shown_once(tmp_path):
    bodies = _render(tmp_path, {
        "warned": _kept_job(warning=REASON),
        # A job from before every kept ending set a warning still shows why.
        "message_only": _kept_job(warning=None),
    })

    assert bodies["warned"].count(REASON) == 1, bodies["warned"]
    assert bodies["message_only"].count(REASON) == 1, bodies["message_only"]


def test_a_deleted_source_shows_no_kept_line(tmp_path):
    bodies = _render(tmp_path, {
        "deleted": _kept_job(deleted_paths=["/v/Game (Disc 1).cue"], message="done"),
    })

    assert "kept-message" not in bodies["deleted"]
