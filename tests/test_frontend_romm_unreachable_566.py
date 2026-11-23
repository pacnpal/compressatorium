"""RomM that isn't answering says so, once (#566).

With RomM set up but not answering, the RomM page's header said "Connected to
127.0.0.1:9" (it only asked whether RomM was set up), and Browse showed
"Couldn't reach RomM. Could not reach RomM. On the RomM page, ..." after
cutting the status error at its first colon, which for a 404 left only the
end of the sentence. ``src/lib/util/rommState.js`` says both; evaluated with
Node.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_delete_check_298 import _find_node

_LIB = Path(__file__).resolve().parents[1] / "src" / "lib"

# What the backend's _safe_error says (app/routes/romm.py).
_UNREACHABLE = (
    "Could not reach RomM. On the RomM page, check the RomM URL, and that RomM is running."
)
_NOT_FOUND = (
    "RomM returned 404. On the RomM page, check the RomM URL: it's the address you "
    "open RomM at."
)


def _eval(tmp_path, expr: str):
    node = _find_node()
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "eval.mjs"
    script.write_text(
        f"import {{ rommWhere, rommProblem }} from "
        f"{json.dumps((_LIB / 'util' / 'rommState.js').as_uri())};\n"
        f"process.stdout.write(JSON.stringify({expr}));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_header_says_whether_romm_answers(tmp_path):
    url = "http://127.0.0.1:9"
    assert _eval(tmp_path, f"""[
      rommWhere({{ configured: true, connected: false }}, {json.dumps(url)}),
      rommWhere({{ configured: true, connected: true }}, {json.dumps(url)}),
      rommWhere({{ configured: true, connected: true }}, 'not a url'),
      rommWhere({{ configured: false, connected: false }}, ''),
      rommWhere(null, {json.dumps(url)}),
    ]""") == [
        "Not answering at 127.0.0.1:9",
        "Connected to 127.0.0.1:9",
        "Connected",
        "Not connected",
        "",
    ]


def test_browse_shows_the_status_error_once_and_whole(tmp_path):
    out = _eval(tmp_path, f"""[
      rommProblem({{ error: {json.dumps(_UNREACHABLE)} }}),
      rommProblem({{ error: {json.dumps(_NOT_FOUND)} }}),
      rommProblem({{ error: null }}),
      rommProblem(null),
    ]""")

    assert out == [_UNREACHABLE, _NOT_FOUND, "Couldn't reach RomM.", "Couldn't reach RomM."]


def test_the_romm_page_and_browse_use_them():
    view = (_LIB / "components" / "views" / "LibraryView.svelte").read_text(encoding="utf-8")
    browse = (_LIB / "components" / "library" / "RommBrowse.svelte").read_text(encoding="utf-8")
    assert "rommWhere(romm.status, romm.settings?.url)" in view
    assert "Connected to ${" not in view
    assert "rommProblem(status)" in browse
    assert ".replace(/^[^:]*:" not in browse
