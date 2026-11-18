"""The File info modal says why a tool isn't ready (#486).

The modal walks the candidate tools' ``getInfo`` bindings and falls back to
the basics the listing knew when none of them answers. A tool whose info route
answers 503 because it isn't ready used to be dropped like a parse failure, so
the user saw "No tool could read this file" and never the server's reason.
``loadInfo`` lives in ``src/lib/util/fileInfo.js`` so it runs under Node here;
skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HELPER = _ROOT / "src" / "lib" / "util" / "fileInfo.js"
_MODAL = _ROOT / "src" / "lib" / "components" / "modals" / "FileInfoModal.svelte"

_NOT_READY = (
    "rom-converto isn't ready: rom-converto at /usr/local/bin/rom-converto "
    "is not executable"
)
_TODAY = "No tool could read this file, showing the basics."


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _run(tmp_path, body: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    assert _HELPER.is_file(), "src/lib/util/fileInfo.js is missing"
    script = tmp_path / "file_info_eval.mjs"
    script.write_text(
        f"import {{ loadInfo }} from {json.dumps(_HELPER.as_uri())};\n"
        # The shape fetchJson throws for a non-2xx: the server's detail as the
        # message, the HTTP status on err.status.
        "const httpError = (status, detail) => {\n"
        "  const e = new Error(detail); e.status = status; return e;\n"
        "};\n"
        "const tool = (id, getInfo) => ({ id, getInfo });\n"
        "const entry = { name: 'game.zar', path: '/v/game.zar', size: 2048 };\n"
        f"const NOT_READY = {json.dumps(_NOT_READY)};\n"
        + body,
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_not_ready_tool_puts_the_servers_reason_in_the_fallback_note(tmp_path):
    out = _run(
        tmp_path,
        "const r = await loadInfo(\n"
        "  [tool('rom_converto', async () => { throw httpError(503, NOT_READY); })],\n"
        "  entry.path, entry,\n"
        ");\n"
        "process.stdout.write(JSON.stringify(r));\n",
    )
    assert out["fellBack"] is True
    assert out["info"]["name"] == "game.zar"
    assert out["info"]["size"] == "2 KB"
    assert _NOT_READY in out["notes"]


def test_a_parse_failure_keeps_todays_note(tmp_path):
    out = _run(
        tmp_path,
        "const r = await loadInfo(\n"
        "  [tool('xiso', async () => { throw httpError(422, 'Not an Xbox image'); }),\n"
        "   tool('cso', async () => { throw new TypeError('network down'); })],\n"
        "  entry.path, entry,\n"
        ");\n"
        "process.stdout.write(JSON.stringify(r));\n",
    )
    assert out["fellBack"] is True
    assert out["notes"] == [_TODAY]


def test_each_tool_that_was_not_ready_is_named_beside_a_parse_failure(tmp_path):
    out = _run(
        tmp_path,
        "const r = await loadInfo(\n"
        "  [tool('xiso', async () => { throw httpError(422, 'Not an Xbox image'); }),\n"
        "   tool('rom_converto', async () => { throw httpError(503, NOT_READY); }),\n"
        "   tool('other', async () => { throw httpError(503, 'Other isn\\'t ready: gone'); })],\n"
        "  entry.path, entry,\n"
        ");\n"
        "process.stdout.write(JSON.stringify(r));\n",
    )
    assert out["notes"] == [_TODAY, _NOT_READY, "Other isn't ready: gone"]


def test_a_later_candidate_that_answers_still_wins_with_no_note(tmp_path):
    out = _run(
        tmp_path,
        "const r = await loadInfo(\n"
        "  [tool('rom_converto', async () => { throw httpError(503, NOT_READY); }),\n"
        "   tool('xiso', async (p) => ({ file: p, files: 3 }))],\n"
        "  entry.path, entry,\n"
        ");\n"
        "process.stdout.write(JSON.stringify(r));\n",
    )
    assert out == {
        "info": {"file": "/v/game.zar", "files": 3}, "fellBack": False, "notes": [],
    }


def test_with_no_basics_the_error_carries_the_not_ready_reason(tmp_path):
    out = _run(
        tmp_path,
        "let msg = null;\n"
        "try {\n"
        "  await loadInfo(\n"
        "    [tool('rom_converto', async () => { throw httpError(503, NOT_READY); })],\n"
        "    '/v/game.zar', null,\n"
        "  );\n"
        "} catch (e) { msg = e.message; }\n"
        "process.stdout.write(JSON.stringify(msg));\n",
    )
    assert _NOT_READY in out


def test_the_modal_uses_the_helper_and_shows_every_note():
    src = _MODAL.read_text(encoding="utf-8")
    assert "from '$lib/util/fileInfo.js'" in src
    assert "catch (_e)" not in src
    assert "{#each notes as note" in src
